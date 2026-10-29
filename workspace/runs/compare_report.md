# Cloud-OpsBench × AgentRun：前后轨迹对比（n=5 随机 case）

样本集：`eval/tasks_5.json`（seed=42 随机抽样，5 个不同 category，跨 2 个系统）。评测尺子：Cloud-OpsBench 官方 `evaluation.py`（CA/FA/JRA + MC/EOC/ECR/EE/RAR），两侧走同一条评分代码路径。

## 1. 三口径总表

| 配置 | CA | FA | JRA | MC | EOC | ECR | EE | Steps | RAR | 工具调用 | 模型调用 | tokens(in/out) | 墙钟 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline（原生 ReAct agent, deepseek-flash） | 1.00 | 1.00 | 1.00 | 0.793 | 0.653 | 0.40 | 0.900 | 3.40 | 0.000 | 17 | 17 | 134781/7505 | 58.1s |
| agentrun v0.6 机制侧（jev=mechanism, 无模型调用） | 1.00 | 1.00 | 1.00 | 0.820 | 0.680 | 0.60 | 0.833 | 3.60 | 0.000 | 18 | 0 | 0/0 | 0.3s |
| agentrun v0.6 生产（jev=live, 真实 decision model） | 1.00 | 1.00 | 1.00 | 0.820 | 0.680 | 0.60 | 0.833 | 3.60 | 0.000 | 18 | 7 | 1593/0 | 1.9s |
| agentrun v0.6 回放（jev=answers, 答案库重放） | 1.00 | 1.00 | 1.00 | 0.820 | 0.680 | 0.60 | 0.833 | 3.60 | 0.000 | 18 | 0 | 0/0 | 0.5s |

## 2. 逐 case：轨迹形状

### trainticket/runtime/61  (easy, query: `Service Availability Disruption.`)

- baseline：**5 步** / 44522 in-tokens / 11.8s
- agentrun 机制侧：**4 步** / 0 token / 0.09s / family=`runtime` / pick=`app/ts-delivery-service` + `mysql_invalid_port`（rule: db_port）
- agentrun 生产：4 步 / 1 次模型调用 / 0.37s / escalate=False

  - baseline 轨迹：`GetResources(resource_type=pods,output_wide=True) → GetErrorLogs(service_name=ts-delivery-service) → GetAppYAML(app_name=ts-delivery-service) → GetResources(resource_type=services,name=tsdb-mysql-leader,output_yaml=True) → Submit(key_evidence_summary=Only ts-delivery-service is unhealthy (CrashLoopBackOff). Its logs show HikariPool/JDBC 'Connection refused' to MySQL on startup, causing app startup failure. The service's Secret ts-delivery-mysql sets DELIVERY_MYSQL_PORT='34006', but the target Service tsdb-mysql-leader exposes port 3306. The port mismatch produces the refused connections and repeated crashes.,top_3_predictions=[{'rank': 1, 'fault_object': 'app/ts-delivery-service', 'root_cause': 'mysql_invalid_port'}, {'rank': 2, 'fault_object': 'app/ts-delivery-service', 'root_cause': 'service_port_mapping_mismatch'}, {'rank': 3, 'fault_object': 'app/ts-delivery-service', 'root_cause': 'missing_secret_binding'}])`
  - agentrun 轨迹：`GetResources(resource_type=pods) → DescribeResource(resource_type=pods,name=ts-delivery-service-787cbf858d-2hs67) → GetAppYAML(app_name=ts-delivery-service) → GetErrorLogs(service_name=ts-delivery-service)`

### boutique/codedefect/60  (hard, query: `Service Performance Degradation.`)

- baseline：**5 步** / 28706 in-tokens / 13.1s
- agentrun 机制侧：**4 步** / 0 token / 0.08s / family=`codedefect` / pick=`app/frontend` + `code_excessive_file_writes`（rule: code_alert_signature）
- agentrun 生产：4 步 / 3 次模型调用 / 0.72s / escalate=False

  - baseline 轨迹：`GetAlerts() → GetErrorLogs(service_name=frontend) → ListCodeFiles(app_name=frontend) → GetSourceCode(app_name=frontend,file_path=handlers.go) → Submit(key_evidence_summary=Frontend alerts showed a new filesystem write surge (171371.76B/s peak 414371.37B/s) and CPU saturation increase, while users saw 503 errors. Source inspection of frontend handlers.go revealed chooseAd creates /tmp/tmp.csv and writes 30,000 CSV rows on each ad request, matching the observed excessive file-write behavior and causing service performance degradation.,top_3_predictions=[{'rank': 1, 'fault_object': 'app/frontend', 'root_cause': 'code_excessive_file_writes'}, {'rank': 2, 'fault_object': 'app/frontend', 'root_cause': 'pod_cpu_overload'}, {'rank': 3, 'fault_object': 'app/frontend', 'root_cause': 'code_busy_loop'}])`
  - agentrun 轨迹：`GetResources(resource_type=pods) → GetAlerts() → ListCodeFiles(app_name=frontend) → GetSourceCode(app_name=frontend,file_path=handlers.go)`

### boutique/admission/32  (medium, query: `Service Availability Disruption.`)

- baseline：**5 步** / 27237 in-tokens / 13.2s
- agentrun 机制侧：**3 步** / 0 token / 0.03s / family=`admission` / pick=`namespace/boutique` + `namespace_memory_quota_exceeded`（rule: quota_event）
- agentrun 生产：3 步 / 1 次模型调用 / 0.25s / escalate=False

  - baseline 轨迹：`GetResources(resource_type=pods,output_wide=True) → GetResources(resource_type=deployments,output_wide=True) → DescribeResource(resource_type=deployment,name=shippingservice) → DescribeResource(resource_type=replicaset,name=shippingservice-696cc9d869) → Submit(key_evidence_summary=shippingservice has 0/1 available replicas and its ReplicaSet repeatedly fails to create pods. Events show: 'pods ... is forbidden: exceeded quota: memory-request-quota, requested: requests.memory=300Mi, used: requests.memory=3000Mi, limited: requests.memory=3Gi'. This confirms the namespace memory request quota is exhausted, preventing shippingservice pod creation and causing the availability disruption.,top_3_predictions=[{'rank': 1, 'fault_object': 'namespace/boutique', 'root_cause': 'namespace_memory_quota_exceeded'}, {'rank': 2, 'fault_object': 'app/shippingservice', 'root_cause': 'memory_capacity_mismatch'}, {'rank': 3, 'fault_object': 'app/shippingservice', 'root_cause': 'deployment_zero_replicas'}])`
  - agentrun 轨迹：`GetResources(resource_type=pods) → GetResources(resource_type=deployments) → GetResources(resource_type=events)`

### boutique/scheduling/109  (easy, query: `Partial Service Unreachability.`)

- baseline：**4 步** / 20148 in-tokens / 7.9s
- agentrun 机制侧：**4 步** / 0 token / 0.03s / family=`scheduling` / pick=`app/recommendationservice` + `node_selector_mismatch`（rule: scheduling_event）
- agentrun 生产：4 步 / 1 次模型调用 / 0.29s / escalate=False

  - baseline 轨迹：`GetResources(resource_type=pods,output_wide=True) → DescribeResource(resource_type=pod,name=recommendationservice-54c95657c8-r4jxw) → GetClusterConfiguration() → Submit(key_evidence_summary=recommendationservice pod is Pending. DescribeResource shows Node-Selectors: disktype=ssd and a FailedScheduling event: '0/4 nodes are available: 1 node(s) were unschedulable, 3 node(s) didn't match Pod's node affinity/selector'. GetClusterConfiguration shows worker-01/02/03 are Ready, untainted, but carry label key 'disk-type: ssd' (hyphen) rather than 'disktype: ssd'. The selector key mismatch makes the pod unschedulable on all workers, so recommendationservice has no running instance, causing partial service unreachability.,top_3_predictions=[{'rank': 1, 'fault_object': 'app/recommendationservice', 'root_cause': 'node_selector_mismatch'}, {'rank': 2, 'fault_object': 'app/recommendationservice', 'root_cause': 'node_affinity_mismatch'}, {'rank': 3, 'fault_object': 'node/master', 'root_cause': 'node_cordon_mismatch'}])`
  - agentrun 轨迹：`GetResources(resource_type=pods) → DescribeResource(resource_type=pods,name=recommendationservice-54c95657c8-r4jxw) → GetResources(resource_type=pvcs) → GetClusterConfiguration()`

### boutique/runtime/32  (easy, query: `Service abnormal restart.`)

- baseline：**3 步** / 14168 in-tokens / 12.0s
- agentrun 机制侧：**3 步** / 0 token / 0.05s / family=`runtime` / pick=`app/checkoutservice` + `readiness_probe_incorrect_protocol`（rule: probe_config）
- agentrun 生产：3 步 / 1 次模型调用 / 0.31s / escalate=False

  - baseline 轨迹：`GetResources(resource_type=pods) → DescribeResource(resource_type=pod,name=checkoutservice-74fc7d4686-9vlrn) → Submit(key_evidence_summary=checkoutservice pod is Running but not Ready (0/1) with repeated readiness probe failures against http://172.20.2.80:5050/health. The error 'malformed HTTP response' containing binary gRPC frame bytes shows the probe is using HTTP/1 while the container serves gRPC. The liveness probe is already configured as grpc, further supporting a protocol mismatch in the readiness probe.,top_3_predictions=[{'rank': 1, 'fault_object': 'app/checkoutservice', 'root_cause': 'readiness_probe_incorrect_protocol'}, {'rank': 2, 'fault_object': 'app/checkoutservice', 'root_cause': 'liveness_probe_incorrect_protocol'}, {'rank': 3, 'fault_object': 'app/checkoutservice', 'root_cause': 'readiness_probe_incorrect_port'}])`
  - agentrun 轨迹：`GetResources(resource_type=pods) → DescribeResource(resource_type=pods,name=checkoutservice-74fc7d4686-9vlrn) → GetAppYAML(app_name=checkoutservice)`


## 3. jev 决策面（v0.3 live，真实 decision model）

| case | route 选择 | route 状态 | route 概率 | pick | verify | escalate | 模型调用 |
|---|---|---|---|---|---|---|---|
| trainticket/runtime/61 | runtime | ans | {'runtime': 0.73, 'infrastructure': 0.01, 'other': 0.26} | 0 次模型 pick / 4 次 | 0 次模型 verify / 4 次 | False | 1 |
| boutique/codedefect/60 | codedefect | ans | {'codedefect': 0.03, 'performance': 0.04, 'infrastructure':  | 1 次模型 pick / 3 次 | 1 次模型 verify / 3 次 | False | 3 |
| boutique/admission/32 | admission | ans | {'admission': 0.02, 'infrastructure': 0.05, 'other': 0.94} | 0 次模型 pick / 3 次 | 0 次模型 verify / 3 次 | False | 1 |
| boutique/scheduling/109 | scheduling | ans | {'scheduling': 0.3, 'runtime': 0.05, 'infrastructure': 0.04, | 0 次模型 pick / 4 次 | 0 次模型 verify / 4 次 | False | 1 |
| boutique/runtime/32 | runtime | ans | {'runtime': 0.31, 'infrastructure': 0.04, 'other': 0.65} | 0 次模型 pick / 3 次 | 0 次模型 verify / 3 次 | False | 1 |

## 4. 证据边际贡献（每个工具调用赚到几个 milestone）

| case | 配置 | 调用数 | 有贡献调用 | 比例 | 明细 |
|---|---|---|---|---|---|
| trainticket/runtime/61 | baseline | 4 | 4 | 1.000 | GetResources:1 GetErrorLogs:1 GetAppYAML:1 GetResources:1 |
| boutique/codedefect/60 | baseline | 4 | 3 | 0.750 | GetAlerts:1 GetErrorLogs:0 ListCodeFiles:1 GetSourceCode:1 |
| boutique/admission/32 | baseline | 4 | 2 | 0.500 | GetResources:0 GetResources:1 DescribeResource:0 DescribeResource:1 |
| boutique/scheduling/109 | baseline | 3 | 3 | 1.000 | GetResources:1 DescribeResource:1 GetClusterConfiguration:1 |
| boutique/runtime/32 | baseline | 2 | 2 | 1.000 | GetResources:1 DescribeResource:1 |
| trainticket/runtime/61 | agentrun v0.6 机制侧 | 4 | 3 | 0.750 | GetResources:1 DescribeResource:0 GetAppYAML:1 GetErrorLogs:1 |
| boutique/codedefect/60 | agentrun v0.6 机制侧 | 4 | 3 | 0.750 | GetResources:0 GetAlerts:1 ListCodeFiles:1 GetSourceCode:1 |
| boutique/admission/32 | agentrun v0.6 机制侧 | 3 | 2 | 0.667 | GetResources:0 GetResources:1 GetResources:1 |
| boutique/scheduling/109 | agentrun v0.6 机制侧 | 4 | 3 | 0.750 | GetResources:1 DescribeResource:1 GetResources:0 GetClusterConfiguration:1 |
| boutique/runtime/32 | agentrun v0.6 机制侧 | 3 | 3 | 1.000 | GetResources:1 DescribeResource:1 GetAppYAML:1 |

## 5. P5 门禁：DSL/参数敏感性（改一处，行为必须变）

| 变体 | CA | MC | Steps | 工具调用 | 判定 |
|---|---|---|---|---|---|
| v0.6 基线 | 1.000 | 0.820 | 3.600 | 18 | — |
| ablation_v06_alerts | 1.000 | 0.820 | 4.400 | 22 | 改参数/删节点后轨迹与成绩变化 |
| ablation_v06_minw2 | 1.000 | 0.753 | 3.400 | 17 | 改参数/删节点后轨迹与成绩变化 |

## 6. 复现命令

```bash
# 0) 基线（原生 ReAct agent）
python3 -m workspace.agentrun.baseline --max-iterations 20
python3 -m workspace.agentrun.score --label baseline --root workspace/traces/baseline \
        --layout baseline --out workspace/runs/score_baseline.json
# 1) 机制侧（0 模型调用）
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.4.dsl.txt \
        --jev-mode mechanism --save-root workspace/traces/agentrun_v06_mechanism \
        --eval-out workspace/runs/agentrun_v06_mechanism.json
# 2) 生产（真实 jev）
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.4.dsl.txt --jev-mode live \
        --save-root workspace/traces/agentrun_v06_live --jev-req workspace/jev/req/v06_live.jsonl
# 3) 答案库回放（pass A -> library -> pass B，0 token 复现）
python3 -m workspace.agentrun.jevlib --root workspace/traces/agentrun_v06_live --out workspace/jev/ans/v4.json
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.4.dsl.txt --jev-mode answers \
        --jev-in workspace/jev/ans/v4.json --save-root workspace/traces/agentrun_v06_answers
# 4) 诊断（每个工具调用的边际证据贡献）
python3 -m workspace.agentrun.diag --root workspace/traces/agentrun_v06_mechanism --layout agentrun \
        --out workspace/runs/marginal_credit_v06_mechanism.json
```

