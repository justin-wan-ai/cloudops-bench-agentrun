# DSL 版本 ↔ 成绩 ↔ 可审性

**v0.13 是唯一权威版本**（六轮爬山后的定稿）：它本身就是可评审、可签核的 SOP（不是从代码渲染出来的文档）。
`v0.1–v0.5` 归档，**当前 runner 拒绝加载**（缺 SOP front-matter / `intent` / `refute_if`，或使用旧的复合 gate 名）。

| 文件 | 机制 CA/MC/EE/Steps | 生产 CA/MC/模型调用 | 状态 |
|---|---|---|---|
| `v0.1` / `v0.2` / `v0.3` | 见 `06_ledger.md` | — | 归档（策略在 Python） |
| `v0.4` | 1.00 / 0.820 / 0.833 / 3.6 | 1.00 / 0.820 / 3\* | 归档（策略在 DSL，判定逻辑在 Python） |
| `v0.5` | 1.00 / 0.820 / 0.833 / 3.6 | 1.00 / 0.820 / 7 | 归档（五段式；但没有"给评审看的那两句话"） |
| `v0.6` | 1.00 / 0.820 / 0.833 / 3.6 | 1.00 / 0.820 / 7 | 归入 R1 前基线 |
| `v0.7` | scheduling: evolve **0.963** / val **0.917** / sealed **0.992** | 同机制口径 | R1（scheduling 取证与判定） |
| `v0.8` | infrastructure: evolve **1.000** / val **0.750** / sealed **0.917** | 同机制口径 | R2（节点组件矩阵 + 逐节点连通性） |
| `v0.9` | service: evolve **0.917** / val **0.750** / sealed **0.921** | 同机制口径 | R3（service 配置与 env 走线） |
| `v0.10` | performance: evolve **0.900** / val **0.500** / sealed **0.800** | 同机制口径 | R4（告警三机制 + 延迟叶子） |
| `v0.11` | service 6.42 steps、infra sealed 0.958、ood_cross 0.381；all-evolve 0.857 | 同机制口径 | R5（OOD 治理 + 成本） |
| `v0.12` | all-evolve 0.955 / val 0.857 / sealed(378) 0.963 | 同机制口径 | R6（配额/镜像/内存/侧车/探针归属） |
| `v0.13` | all-evolve **0.957** / val **0.861** / sealed(391) **0.964**；id 内 **0.954**；全量 754 **0.714** | 同机制口径 | ★当前最好（R7：零副本 + 证据卫生 + 入口服务 + `max_waves` 10） |
| `v0.14` | R8/R9 试点（`symptom_only` 谓词） | — | 归档：R8 回滚、R9 否决（ledger #17/#18、R24/R25） |

\* v0.4 及更早只统计 `status=ans` 的判定，弃权那次真实往返被漏计（3 vs 实际 7）。v0.5 起口径修正。

## v0.7 的 scheduling 计划（R1 改动的可见部分）

```yaml
  scheduling:
    - {step: describe_pod,    gate: always}
    - {step: cluster_config,  gate: always}      # 节点清单：labels/taints/allocatable
    - {step: pvcs,            gate: volume_evidence}
    - {step: pvs,             gate: pending_claim}   # 只在有 Pending claim 时才花
    - {step: storage_classes, gate: pending_claim}
    - {step: describe_pvc,    gate: pending_claim}
```

`pending_claim` 是 R1 新增的原子 gate；`max_waves` 提到 8，因为「一波一探查」下最长计划是 6 步。

## v0.8 的节点级取证（R2）

```yaml
  infrastructure:
    - {step: describe_pod,    gate: always}          # 停滞的 pod 在哪个节点
    - {step: cluster_config,  gate: always}          # 节点 labels/taints/allocatable
    - {step: node_kubelet,    gate: node_scoped_symptom}
    - {step: node_containerd, gate: [node_scoped_symptom, node_all_healthy]}
    - {step: node_kube_proxy, gate: [node_scoped_symptom, node_all_healthy]}
    - {step: node_scheduler,  gate: [node_scoped_symptom, node_all_healthy]}
  scheduling:
    ...
    - {step: node_scheduler,  gate: unscheduled_without_events}   # Pending 且 scheduler 一声不吭
  service:
    - {step: connectivity,    gate: always}          # 逐节点 TCP 结论是主证据
    ...
    - {step: node_kube_proxy, gate: connectivity_node_failure}
  runtime / startup:
    - {step: node_kubelet,    gate: runtime_stall}   # 容器根本没起来，且镜像/密钥都正常
```

新增原子 gate：`runtime_stall`、`unscheduled_without_events`、`node_scoped_symptom`、`node_all_healthy`、
`node_reported_unreachable`、`connectivity_refused`、`connectivity_node_failure`、`pending_claim`。

## v0.9 的 service 取证（R3）

```yaml
  service:
    - {step: pods_labels,      gate: always}      # selector 需要 pod labels（与 -o wide 互斥，故单独取）
    - {step: services,         gate: always}
    - {step: error_logs_entry, gate: always}      # 入口服务日志点名故障依赖
    - {step: app_yaml_c1,      gate: service_target_found}          # Service 文档 + Deployment 文档
    - {step: app_yaml_c2,      gate: [service_target_found, service_config_clean]}
    - {step: app_yaml_c3,      gate: [service_target_found, service_config_clean]}   # 入口自身也是嫌疑人
    - {step: connectivity,     gate: weak_strong}
    - {step: node_kube_proxy,  gate: connectivity_node_failure}
```

判定顺序：Service 文档的 selector/targetPort/protocol → Deployment 的 `*_ADDR`（host 必须在 Service 表里、端口必须一致）
→ 都干净才继续看下一个候选 app。

## v0.10 的 performance 取证（R4）

```yaml
  performance:
    - {step: deps_entry,   gate: always}    # 调用图：谁把延迟传给谁
    - {step: describe_pod, gate: weak}      # limits vs 节流交叉验证
```

判定（全部来自 census 的 alerts）：
`RESOURCE_SATURATION[cpu_cfs]` → `pod_cpu_overload`；Node 级 `TCP Retransmission` 计数 1 个 → `node_network_delay`、
≥2 个 → `node_network_packet_loss`（取 retrans 最高节点）；都没有 → 延迟服务集合里**没有延迟下游的那个叶子** → `pod_network_delay`。

## v0.11 的两条语义修正（R5）

```yaml
  service:
    - {step: app_yaml_c2, gate: [service_target_found, no_candidate_yet]}   # 列表 = AND
    - {step: connectivity, gate: [no_candidate_yet]}
```

- **gate 列表是 AND**（"所有闸门都成立才执行"）。原来的实现是 OR，导致"已经拿到候选还继续取证"，
  还掩盖了 `runtime_stall` 漏判 `PodInitializing` 的 bug。真正的 OR 只有两个显式复合 gate：
  `probe_or_restart_or_weak`、`restart_or_weak_strong`。
- **停止规则**：`no_candidate_yet` —— 一旦有候选就停止付费取证（service 每题 −1.2 次调用）。

## v0.12 的 admission / service 取证（R6）

```yaml
  admission:
    - {step: deployments,     gate: always}
    - {step: events,          gate: always}
    - {step: resourcequota,   gate: always}          # 配额对象点名叫哪个资源被耗尽
    - {step: services,        gate: always}          # 哪种对象没被创建
    - {step: pvcs,            gate: volume_evidence}
    - {step: describe_deployment, gate: weak_very_strong}
  service:
    - {step: pods_labels,     gate: always}
    - {step: services,        gate: always}
    - {step: error_logs_entry, gate: always}
    - {step: resourcequota,   gate: service_missing_for_pod}   # "有 pod 没 Service" → 查配额
    ...
```

## 一个文件 = 一段 SOP，按值班顺序读

```
1. front matter  sop / scope / owner / approved_by / reviewed_at / change_ticket / boundaries
2. flow          症状登记 → route（哪类故障）→ 取证循环（probe/derive/pick/verify）→ escalate → 结论
3. plans         附录 A：每个家族要取证什么，什么条件下才值得花这次调用
4. state         附录 B：判定结果落到哪个运行时槽位
5. params        附录 C：可调数值
```

头部的 `boundaries` 是"本 SOP 不做什么"（当前：只读诊断，不执行写操作、不播报、不覆盖 SEV1），
`approved_by` + `reviewed_at` + `change_ticket` 让"改 SOP = 提变更单，diff 即变更内容"。

## `decide` 节点：五个机器段 + 两个人类段

```yaml
- id: pick
  do: decide
  intent: |                     # ← 给评审：这个判定为什么存在
    在"证据支持的候选根因"里选出唯一一个对象+根因。候选已按规则分排序，
    顺序只是先验；模型用来处理"两条候选都说得通"的情形，不负责发现候选。
  refute_if: |                  # ← 给评审：什么证据会推翻它
    若被选中对象的 describe 显示 Ready=True、无重启、无异常事件，则这条候选不成立。
  ask: |                        # ① 问模型什么
  space: {from: candidates, limit: $params.cands_cap, render: [...], other: true}   # ② 选项空间
  input: {incident: [...], candidates: space_items}                                  # ③ payload 字段
  pre:                                                                               # ④ 机械决策表
    - if: top_over_second >= $params.dominance_ratio
      then: {use: first_option, dominant: true, why: dominance}
  post:                                                                              # ⑤ 答案映射（穷举）
    default: {use: answer, dominant: auto}
    other:   {use: first_option, dominant: auto, why: abstained}
    error:   {use: first_option, dominant: auto, why: transport_error}
  out: {picked: choice, dominant: dominance_flag, pick_why: reason}                  # ⑥ 输出绑定
```

## 词表收缩到"原子"级（v0.6）

| 类别 | 词表 | 变化 |
|---|---|---|
| guard | `no_options` `jev_disabled` `has_stored_answer` `unchanged_since_last` `no_pick` `top_over_second >= N` `$node.field` `$node.field == literal` | 不变 |
| use | `answer` `first_option` `stored` `previous` `none` `$params.key` / 字面量 | 不变 |
| **gate（8，原子）** | `always` `healthy_system` `weak` `weak_strong` `weak_very_strong` `probe_evidence` `restart_evidence` `volume_evidence` | 删除 `probe_or_restart_or_weak`、`restart_or_weak_strong` 两个复合名，改为**多 gate = OR**：`gate: [probe_evidence, restart_evidence, weak]` |
| stop | `plan_exhausted` `verified_and_model` | 不变 |
| escalate reason | `no_candidates` `contradicted` `family_unknown` `weak_after_waves`（另有 4 个备用） | 不变 |

## 评审：`dsl_check` 两种模式

```bash
python3 -m workspace.agentrun.dsl_check --dsl dsl_versions/v0.6.dsl.txt           # 打印 SOP 全文（含 intent/refute_if）
python3 -m workspace.agentrun.dsl_check --dsl dsl_versions/v0.6.dsl.txt --review  # 像审 SOP 一样挑错
```

`--review` 的回答的是评审会问的问题，**不生成第二份文档**：

| 检查 | 级别 |
|---|---|
| `reviewed_at` 是否 ISO 日期、`approved_by` 非空、`boundaries` 非空 | FAIL（缺则加载期直接报错） |
| `scope.family` 是否有 `plans` 条目（否则该 SOP 跑不到自己的范围） | FAIL |
| 用 `contradicted` 升级但没接 `state.verify_status` | FAIL |
| `post` 里出现固定选项之外的键（死分支） | FAIL |
| `space.other=true` 但缺 `post.other` | FAIL |
| `top_over_second` 用在非 candidates 空间 | FAIL |
| 重复 guard（后面的永远不可达） | WARN |
| `space.render` 为空（模型只看得到 id） | WARN |
| `post` 用 `previous`/`stored` 但无对应 pre 规则 | INFO |
| 非 census 的步骤 `gate: always`（一定花一次调用） | INFO |
| 多个 signal 指向同一家族（只有第一个命中的生效） | INFO |

当前 v0.6：`0 fail / 0 warn / 13 info`。

## 校验与消融

| 破坏方式 | 结果 |
|---|---|
| 删 `route` 节点 | `plan.family: unknown node reference '$route.family'` |
| 删 `approved_by`（签核缺失） | `missing SOP front-matter ['approved_by'] …` |
| 删 `pick.intent` | `decide.intent is required -- the reviewer needs the purpose and the falsification condition` |
| `post` 加不存在的选项键 | lint `FAIL … dead branches`（退出码 1） |
| 重复 guard | lint `WARN … duplicates an earlier guard (unreachable)` |
| `ablation/v06_alerts_always`（`alerts` gate→always） | 调用 18→22、EE 0.833→**0.680** |
| `ablation/v06_min_waves2`（5→2，= v0.2 生产行为） | 生产 MC 0.820→**0.753**、ECR 0.6→0.4 |

## 归档政策

- 每个 `vX.Y` 都是**当时**的可执行 SOP；加载期校验保证「缺签核/缺 intent/写错名字」直接失败。
- 历史版本只在做对照实验时运行（旧版本依赖当时的规则实现，跨大版本可能不再一致——以 ledger 记录的数字为准）。
- 当前权威版本 = `v0.13`；`v0.14` 只记录被否决的试点，不要再复用。
