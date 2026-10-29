# Cloud-OpsBench × AgentRun：SRE 根因诊断迁移（最终报告）

> 收尾文档。六轮爬山（R1–R7 采纳，R8/R9 试点否决），当前最好 **v0.13**：
> 全量 754 题机制口径 **0.714**（基线 0.435），可调参池 `id` 内 **0.954**，全程 **0 模型调用**。

## 1. 做了什么

把 Cloud-OpsBench 的自由 ReAct 诊断 agent，迁移成 **「固定 SOP + 类型化 jev 判定 + escalate 兜底」**，
并用递归自我改进的方式迭代 SOP 本身（DSL + 规则引擎），全程遵守 `coding-agent-rsi-data-partitioning-guide.md`
的数据划分纪律。

| 层 | 产物 |
|---|---|
| DSL（SOP 本体，可人工评审签核） | `dsl_versions/v0.13.dsl.txt`（v0.1–v0.14 全部保留，见 `dsl_versions/README.md`） |
| 执行器 | `agentrun/{dsl_parse,decide,runner,plans,rules,evidence,judge,dsl_check}.py` |
| 评测/诊断 | `agentrun/{score,diag,coverage,partition,critic,compare,jevlib,baseline}.py` |
| 数据划分 | `eval/partition_v5.json` + `eval/tasks_*.json` |
| 台账 | `06_ledger.md`（18 条实验 + 25 条负结果）、`07_partition.md`（划分与各轮结果） |

## 2. 最终成绩（全部机制口径，0 模型调用）

| 集合 | n | CA | FA | JRA | MC | EE | 步/题 | 说明 |
|---|---|---|---|---|---|---|---|---|
| **id · evolve** | 115 | 0.965 | 0.957 | **0.957** | 0.875 | 0.689 | 4.59 | 每轮调参用 |
| **id · val** | 43 | 0.907 | 0.884 | **0.861** | 0.853 | 0.665 | 4.74 | 接受/回滚判据 |
| **id · sealed** | 391 | 0.972 | 0.972 | **0.964** | 0.870 | 0.746 | 4.33 | 定稿后跑一次 |
| ood · cross-system | 84 | 0.214 | 0.155 | 0.155 | 0.587 | 0.555 | 4.44 | 只做熔断 |
| ood · unseen-type | 36 | 0.056 | 0.028 | 0.028 | 0.463 | 0.355 | 5.03 | 承诺不碰 |
| **全量 754** | 754 | — | — | **0.714** | — | — | 4.14 | 候选率 0.836 |

对照基线（原生 ReAct，deepseek-flash，5 题种子集）：CA/FA/JRA 1.00、MC 0.793、EE 0.900、17 次工具/17 次 LLM、
134.8k token、58s。**迁移后：主路径模型调用 17 → 0，token −100%，墙钟 −99%，而 sealed 391 题的 JRA 达 0.964。**

生产口径实测（`tasks_val_service`，4 题，v0.13，live jev + escalate judge）：
JRA 0.750、12 次模型调用、41.8k in / 5.5k out token、44.5s（其中 1 题升级到 judge 占 43s）；
同一批用答案库回放（`--jev-mode answers`）：JRA 0.750 **完全一致**、**0 次模型调用 / 0 token / 0.6s**。
→ 主路径成本几乎为零，模型只在歧义点与升级路径上花钱。

## 3. 六轮轨迹

| 轮 | 目标 | 关键机制 | id 内 JRA | 全量 754 |
|---|---|---|---|---|
| 起点 | — | 5 题 MVP（v0.1–v0.6：DSL/策略进 DSL/五段式/可签核 SOP） | — | 0.435（v0.6 扫描） |
| R1 | scheduling | 证据只取 `FailedScheduling` + 节点可行性求解（selector/affinity→taint→cordon→capacity）+ PVC 对象分类 | 0.86 | 0.480 |
| R2 | infrastructure | 节点组件矩阵（`●/○`）+ 只对节点级症状付费 + 逐节点连通性 → kube-proxy | 0.86 | 0.531 |
| R3 | service | 入口日志定位依赖 → Service 文档（selector/port/targetPort/protocol）+ Deployment env 走线 | 0.93 | 0.598 |
| R4 | performance | 告警三机制（cpu_cfs / 节点 retrans 计数 / 延迟叶子） | 0.93 | 0.613 |
| R5 | OOD + 成本 | 网关受害者归位、DNS≠端口、探针归属、`no_candidate_yet`、**gate 列表改 AND** | 0.93 | 0.651 |
| R6 | 五簇补齐 | 配额对象点名资源、缺 Service→配额、镜像 403 语义、内存物理下限、istio statusPort | 0.953 | 0.716 |
| **R7** | 零副本 + 证据卫生 | `READY 0/0`、过滤 `Error:` 文本、按系统取入口服务、`max_waves` 10 | **0.954** | **0.714** |
| R8 | trainticket perf | 词表相同但依赖图仅 1 层 → "延迟叶子"失效 | 回滚 | — |
| R9 | code 家族交给 judge | judge 从不读源码、0 次提交、~30k token/题 | 否决 | — |

（R7 的全量 754 略低于 R6，是因为移除了"工具报错文本当证据"带来的 17 个**蒙对**——见 §5。）

## 4. 方法论要点（可复用）

1. **数据划分**：泄漏单元是**故障类型**（topic）。题型内分 `evolve/val/sealed`（n≥5→3/1/余，小题型自适应，保证每型至少 1 题 sealed），题型间划 `id / ood_cross_system / ood_unseen_type / quarantine`。每轮只激活一个 scope（20–100 题）。
2. **四道门**：正则检查器 `critic`（禁 case 字面量）、噪声门控（val 净 ≥2 题）、OOD 熔断（允许不涨，不允许"自信答错"上升）、sealed 终局。
3. **DSL = 可签核 SOP**：front-matter（scope/owner/approved_by/reviewed_at/change_ticket/boundaries）+ 每个判定的 `intent`/`refute_if` + 五段式机器段（`ask+space / input / pre / post / out`）。加载期校验，`dsl_check --review` 做 SOP lint。
4. **诊断先行**：每轮先用 `coverage`（全量候选率/正确率）+ `diag`（每次工具调用的边际证据贡献）定位，再改一处。
5. **口径纪律**：机制侧（0 模型调用）/ 含答案（jev 答案库回放）/ 生产（真实 decision model + judge）三口径不混用。

## 5. 负结果（25 条，节选最关键）

| # | 尝试 | 结果 | 结论 |
|---|---|---|---|
| R1/R2 | 用 `jev(verify)` 的绝对概率做闸门 / 对支配候选加语义闸门 | 全量误升级 / 成本 +1 收益 0 | 三分类 + 尺度无关判定；支配即答案 |
| R9 | `GetResources(resource_type="pvcs")` | 别名不存在，**静默返回错误**，整个 PVC 子族在无证据下判定 | 工具参数取值也要自检 |
| R12 | `output_wide` + `show_labels` 同时给 | kubectl 拒绝，pods 表空 → 27/27 scheduling 改道 | 拆探针 + `Error:` 观测不得入证据 |
| R14 | `yaml.safe_load` 解多文档 manifest | 抛错返回 `{}` → selector/env 规则全死 | `safe_load_all` |
| R18 | gate 列表按 OR 实现（本应 AND） | 拿到候选仍继续取证；还掩盖 `runtime_stall` 漏 `PodInitializing` | AND 语义 + 两个显式复合 gate |
| R23 | 让工具报错文本进证据 | `Allowed: ['ts-route-*']` 命中 gateway 规则，17 个零副本 case 被判成网关 | 过滤 `Error:` |
| R24 | 把 performance 词表推广到 trainticket | 依赖图 1 层 + 全系统延迟告警 → 3/4 答错 | 跨系统复用需先确认**证据结构**相同 |
| R25 | 用现有 judge 处理 code 缺陷 | 0/7 正确、0 提交、~30k token/题 | 需专门的 code-diagnosis agent |

## 6. 剩余 216 题：需要什么

| 池 | 题数 | 缺什么 |
|---|---|---|
| `trainticket/pod_network_delay` | 47 | 更深的依赖图或 per-node 延迟证据（当前工具面无） |
| code 家族（除 file_writes） | 85 | 专用 code agent（强制读源码 + 必须提交契约，R25） |
| `trainticket/gateway_misrouted` | 19 | 用告警的 `url`/`business_action` 形状重建路由证据（需先重划分区） |
| `trainticket/db_readonly_mode` / `db_connection_exhaustion` | 15 | DB 会话类工具（benchmark 未提供） |
| liveness 探针余量 | 15 | 探针归属的剩余情形 |
| 观测上限 | ~8 | PVC requested size / probe timing / 无节点告警的节点延迟（已写进 `boundaries`，不再投） |

**天花板估算**：现有能力（规则 + 现有工具面）约 **0.72**（已到）；补 19+15+15 ≈ **0.78**；
新增 code agent（+65）≈ **0.87**；DB 与 trainticket perf 需要新工具面才可能继续。

## 7. 复现

```bash
cd /Users/justinwan/work/workspace/cloudops_bench_rsi

# 机制侧（0 模型调用）—— 单轮
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --tasks-file workspace/eval/tasks_evolve_all.json --jev-mode mechanism \
        --save-root workspace/traces/final_evolve
python3 -m workspace.agentrun.score --label final --root workspace/traces/final_evolve \
        --layout agentrun --tasks-file workspace/eval/tasks_evolve_all.json --out workspace/runs/score_final.json

# 全量 754 扫描（约 2–4 分钟）
python3 -m workspace.agentrun.coverage --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --out workspace/runs/coverage_final.json

# 门禁
python3 -m workspace.agentrun.critic   --dsl workspace/dsl_versions/v0.13.dsl.txt
python3 -m workspace.agentrun.dsl_check --dsl workspace/dsl_versions/v0.13.dsl.txt --review
python3 -m workspace.agentrun.coverage --smoke boutique/service/5

# 生产口径（真实 decision model + judge）
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --tasks-file workspace/eval/tasks_val_all.json --jev-mode live \
        --save-root workspace/traces/final_live --jev-req workspace/jev/req/final_live.jsonl
```

## 8. 边界与风险

- 输入是**冻结快照**：结论对快照时刻成立，恢复后需重新取证。
- 只读诊断：不执行写操作（重启/回滚/扩缩容）、不做播报；处置仍需人工 runbook。
- 不覆盖 SEV1（需 incident commander）。
- `id` 的 sealed 391 题只跑过一次（v0.13）；后续任何改动都必须重跑 sealed，且不得据其调参。
- 分数对 n 敏感：evolve 115 题，1 题 = 0.87pp；val 43 题，1 题 = 2.3pp。
