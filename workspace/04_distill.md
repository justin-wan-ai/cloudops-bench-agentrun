# P4 蒸馏：从轨迹到 DSL

## 1. 输入泛化（槽位化）

RCA 的自由文本只有一句 query，其余全部可从输入确定性抽取：

| 槽位 | 来源 | 抽取方式 | 例 |
|---|---|---|---|
| namespace | `metadata.json` | 字段 | `boutique` |
| system | case 路径 | 字段 | `boutique` / `trainticket` |
| 症状类 | query | 正则（restart / unavailable / pending / degradation / error） | `unavailable` |
| 点名服务 | query | 服务名词典正则 | 通常为空 |
| 领域先验 | 静态 | `PRIORS`（trainticket 的 DB 服务名/端口、boutique 的 11 个服务清单） | `tsdb-mysql-leader:3306` |

> 原则：能确定性抽的绝不交给模型。`agentrun/anchors.py` 只读 allowlist 字段，读 gold 会抛 `GoldLeak`。

## 2. 剪枝与直连（策略已全部搬进 DSL）

baseline 每个 case 的探索动作被编译成**固定计划**。v0.4 起，这些计划与闸门**写在 DSL 里**
（`dsl_versions/v0.6.dsl.txt` 的 `plans:` / `decide.space` / `decide.pre` / `decide.post`，外加 SOP 头与 `intent`/`refute_if`），Python 只提供
「命名 probe / gate / signal 的语义实现」：

```yaml
plans:
  census:                            # wave 0
    - {step: pods,   gate: always}
    - {step: alerts, gate: healthy_system}
  runtime:
    - {step: describe_pod, gate: always}
    - {step: app_yaml,     gate: probe_or_restart_or_weak}
    - {step: error_logs,   gate: restart_or_weak_strong}
```

判定点是五段式机器段（`ask+space / input / pre / post / out`）**加两个人类段**（`intent` 目的、`refute_if` 证伪条件），
例：`pick` 的机械规则 `top_over_second >= $params.dominance_ratio → first_option`、弃权 `other → first_option` 都写在节点里；
SOP 的归属/签核/边界写在文件头的 front-matter。见 `dsl_versions/README.md`。

| family | 计划（顺序 + 闸门） | 对应 baseline 探索 |
|---|---|---|
| census | `pods(always)` → `alerts(healthy_system)` | `GetResources(pods, -o wide)` |
| admission | `deployments(always)` → `events(always)` → `resourcequota(weak_strong)` → `describe_deployment(weak_very_strong)` | deployments + describe deployment/replicaset |
| scheduling | `describe_pod(always)` → `pvcs(volume_evidence)` → `cluster_config(always)` | describe pod + cluster config |
| startup | `describe_pod(always)` → `app_yaml(weak_strong)` → `error_logs(weak)` | describe pod + yaml |
| runtime | `describe_pod(always)` → `app_yaml(probe_or_restart_or_weak)` → `error_logs(restart_or_weak_strong)` | describe pod + yaml + logs |
| service | `services(always)` → `endpoints(always)` → `app_yaml(weak)` → `connectivity(weak_strong)` → `deps(weak_strong)` | services/endpoints/connectivity |
| performance | `recent_logs(always)` → `describe_pod(weak)` → `deps(weak)` | logs + describe |
| infrastructure | `node_status(always)` → `cluster_config(weak)` | node status |
| codedefect | `code_files(always)` → `source_code(always)` → `recent_logs(weak_strong)` | alerts + logs + code |

闸门语义（`plans.GATES`）：`healthy_system` = 没有 pod 级故障；`probe_evidence` = describe 里出现
probe 失败/Ready=False；`restart_evidence` = 有重启或 CrashLoop；`weak*` = 当前还没有 score ≥ 0.8/0.9/0.95
的候选；`volume_evidence` = 事件/describe 提到 pvc/volume。

> 这次搬家是可验证的：把 `alerts` 的闸门从 `healthy_system` 改成 `always`（`ablation/alerts_always.dsl.txt`），
> 调用 18→22、EE 0.833→0.680、MC 不变——说明闸门是真起作用的策略，而不是注释。

## 3. jev 判定点（每个只做一件事）

| 节点 | 形态 | 输入（受限） | 输出 | 弃权语义 |
|---|---|---|---|---|
| `jev(route)` | choice | query + 机械短名单（family + 机械理由） | family | `other` → 机械首选 |
| `jev(pick)` | choice | ≤`cands_cap`(8) 候选（含 rule + 证据摘要 + 先验分） | 下标 | `other` → 机械首选 |
| `jev(verify)` | choice 三分类 | 假设 + 3 条压缩观测 | supported / insufficient / contradicted | 无法判定 → `insufficient`（继续探查，不升级） |

**重要机制（手工写进 DSL 注释）**：
- 候选**支配**（top ≥ `dominance_ratio` × second）时**不调模型**（模式 B；且参考手册的负结果「单候选语义闸门 −71pp」）；
- 同一证据签名不重复判定（`status_signature` 缓存），保证 RAR=0；
- `verify` 用三分类而非绝对概率（见 `06_ledger.md` 负结果 R1）。

## 4. 熔断升级（predicate 可机械判定）

```
escalate if  no_candidates | contradicted | family_unknown | weak_after_waves
（另：codedefect 且无强 code 候选时才升级；已有 code_* ≥0.8 则不升级）
```

## 5. 覆盖度（P4 核心门禁）

在 5 条 dev 样本上：

| 指标 | 结果 | 要求 |
|---|---|---|
| 可编译率（能生成 ≥1 条计划） | **754/754 = 100%** | ≥70% ✅ |
| 候选含答案率（gold ∈ 候选） | 754 题候选率 **0.836**（`coverage_754_v013.json`）（`runs/coverage_754_v013.json`（`candidates` 字段）+ `runs/score_r8c_evolve_all.json` 的 candidates 字段可查） | ≥80% ✅ |
| 唯一/支配候选直选正确率 | evolve 115 题 JRA **0.957** | ≥90% ✅ |
| 需 escalate 比例 | 机制口径 0%；live 口径仅 code 试点触发（已否决，见 ledger R25） | ≤30% ✅ |

> n=5 的覆盖度不能外推；结论是「DSL 形状成立、可编译、可回放」，不是「能覆盖 754 case」。

## 6. 熔断升级（predicate 可机械判定）之外的策略归属

v0.5 的划分：**DSL 声明「探测什么、什么条件下探测、问模型的原文、机械规则、答案映射、输出绑定」，代码只实现「怎么探测、怎么判 gate/signal/guard」**。
所以新增一个家族 = 在 `plans:` 加一段 + 在 `decide(route).space.order` 指向它；新增一个机械规则 = 在对应节点的 `pre` 加一条；
新增一个闸门条件 = 在 `plans.GATES` 注册语义、在 DSL 里按名字引用。两边都不允许偷偷用默认值（缺计划 / 缺 `post.error` / 写错 guard 都是 `DslError`）。

## 7. 可审性（SOP）门禁

- [x] 文件头有 `sop/scope/owner/approved_by/reviewed_at/change_ticket/boundaries`，缺则加载失败
- [x] 每个 decide 有 `intent` + `refute_if`（评审无需外部文档）
- [x] `dsl_check --review` 输出 `0 fail`（死分支/悬空接线/重复 guard 会被挑出）
- [x] 不生成第二份文档（Markdown 导出被否决，见 ledger R8）

## 7.5 规则引擎最终形态（`agentrun/rules.py`，22 组规则）

| 组 | 触发证据 | 产出 |
|---|---|---|
| R1 配额事件 / R1b 配额对象 | `exceeded quota:` 事件；`ResourceQuota` 行（`services: 11/11`、`requests.storage`） | `namespace_{cpu,memory,pod,service,storage}_quota_exceeded` |
| R2 ServiceAccount | `serviceaccount "x" not found` | `missing_service_account` |
| R3 调度求解器 | 只取 `FailedScheduling` 消息 + 节点 labels/taints/capacities | anti-affinity / taint / selector / affinity / cordon / cpu|memory capacity |
| R3b PVC | pvc+pv+storageclass 表 | storage_class / binding_occupied / selector（capacity vs access-mode **不可判**） |
| R4 镜像 | `Failed to pull` 细分：dns / authorize / tag 不存在 | `image_registry_dns_failure` / `missing_image_pull_secret` / `incorrect_image_reference` |
| R5 探针 | 探针 scheme/port 与**该 pod 全部容器端口** | `{readiness,liveness}_probe_incorrect_{protocol,port}` |
| R6 OOM / 卷 / Secret | OOMKilled / permission denied / CreateContainerConfigError | 对应 runtime 根因 |
| R9 DB | jdbc 错误，且**无 DNS 症状** | mysql_invalid_{credentials,port} / db_connection_exhaustion / db_readonly_mode |
| R10 零副本 | deployment `READY 0/0` 或 `spec.replicas: 0` | `deployment_zero_replicas` |
| R11 路由 | Service 文档（selector⊆pod labels、targetPort、protocol）＋ env `*_ADDR` 与真实 Service:端口 | `service_*_mismatch` / `gateway_misrouted`（受害者=网关服务） |
| R11b 连通性 | 逐节点 debug-pod 结果 | `kube_proxy_unavailable @ node` |
| R12 性能 | `RESOURCE_SATURATION[cpu_cfs]`；Node 级 retrans（1 节点=delay，≥2=loss）；延迟叶子 | `pod_cpu_overload` / `node_network_*` / `pod_network_delay` |
| R13 节点组件 | `CheckNodeServiceStatus` 的 `●/○` | `{kubelet,containerd,kube-proxy,kube-scheduler}_unavailable` |
| R14 代码 | 告警签名（FILESYSTEM_WRITE/READ/heap）＋ 源码扫描 | `code_excessive_file_{writes,reads}` 等（仅 file_writes 稳定） |
| R6b 资源 | memory limit ≤32Mi；istio `statusPort` 撞 containerPort | `container_memory_limit_too_low` / `service_sidecar_port_conflict` |
| 归一化 | 家族一致性 ±0.12、锚点偏置 +0.05、`_app()` 只接受官方服务名 | 候选排序 |

## 8. 门禁

- [x] v0.4 可执行（不是伪代码）；v0.1–v0.3 已归档，行为在 `dsl_versions/ablation/` 下以 DSL 复现
- [x] 每个 decide 有输入/输出契约（DSL 的五段式本身 + `dsl_parse` 加载期校验，`dsl_check` 可打印成表）
- [x] 每个 escalate 有可机械判定 predicate（`dsl_parse.KNOWN_PREDICATES` 白名单，写错直接报错）
- [x] 策略（census / family_plan / route_priority / route_fallback / gate）全部在 DSL；probe/gate/signal 名字在**加载期**校验
- [x] 规则不读 GT（`anchors.load_case_input` 的 allowlist + `_app()` 只接受官方服务名）
