# P0 立项书：Cloud-OpsBench RCA → AgentRun

## 1. 任务

- **名称**：Kubernetes 故障根因分析（RCA），从「自由 ReAct agent 每次重新探索」迁移为「固定流程 + jev 微判定 + escalate 兜底」。
- **业务价值**：SRE 告警处置是高频、schema 稳定、答案可精确匹配的工作；单次成本与延迟才是瓶颈（不是缺智能）。
- **当前做法**：`repo/agents/cloudops_agent`（Skill-free baseline），每个 case 用 ReAct 循环自行决定 12 个诊断工具，平均 3–8 步、每步一次完整 LLM 往返。
- **期望做法**：95% case 走「census → family 路由 → 参数化探查 → 规则候选 → jev 判定」，仅疑难升级给 judge agent。

## 2. 输入

| 项 | 值 |
|---|---|
| 输入形态 | 冻结的 incident snapshot（`raw_data/` + `tool_cache.json`）+ 一句高层症状 query + namespace |
| 规模与频率 | 754 case（57 类故障，2 个系统），可无限重复回放 |
| 稳定性 | schema 稳定；工具集固定（boutique 12 / trainticket 10） |

## 3. 输出与验收

- 输出：`{key_evidence_summary, top_3_predictions[{rank, fault_object, root_cause}]}`（Rank 1 计分）。
- gold：`process-label/<system>/<category>/<case>/milestone.json`（`result` 字段）+ `benchmark/.../metadata.json#result`。
- 自动判定：官方 `evaluation.py` 精确匹配（`normalize_text` 后全等）。
- 过程判定：milestone 匹配 → MC / EOC / ECR / EE，重复动作 RAR。

## 4. 数据

- 可用样本：754（本次冻结 5 条，见 `eval/tasks_5.json`，seed=42 随机）。
- 敏感性：无 PII；日志为合成微服务日志。GT 与求解路径物理分离（只读 allowlist）。

## 5. 基线（改造前，实测）

`python3 -m workspace.agentrun.baseline --max-iterations 20`（DeepSeek `deepseek-flash`）

| 指标 | 值 |
|---|---|
| CA / FA / JRA | 1.00 / 1.00 / 1.00 |
| MC / EOC / ECR / EE | 0.793 / 0.653 / 0.40 / 0.900 |
| 平均工具步数 | 3.4（含 1 次 `Submit` 工具调用） |
| 模型调用次数 | 17（≈ 每步一次完整对话往返） |
| tokens | 134,781 in / 7,505 out |
| 墙钟 | 58.1s（5 case） |

**失败模式（前 5 类，本样本观测 + 官方全量结论交叉）**

1. **每步重付一次上下文**：单 step 输入 4–14k token，主要成本不是推理而是「重复喂工具描述 + 历史」。
2. **探索性调用**：`GetResources(pods, -o wide)`、`GetClusterConfiguration` 等在不同 case 间反复「重新发现」同一事实。
3. **答案与证据脱耦**：结论正确但过程只覆盖部分 milestone（本样本 2/5 case ECR=0）。
4. **不可回放**：同一 case 重跑路径不同（LLM 采样），过程分抖动。
5. **升级无闸门**：没有「不确定就交给更贵的 agent」的可判定 predicate。

## 6. 边界

- 不追求在这 5 题上超过满分 baseline 的准确率；目标是**成本结构 + 过程可归因 + 可回放**。
- 不覆盖官方 57 类全部故障的规则化；未覆盖走 escalate（结构性兜底）。
- 不用 test GT 调规则：规则只编码 kubectl 语义与告警形状。
- **策略与判定不进代码**：走哪些节点、每个家族探哪几步、每步受哪个 gate 约束、问模型的原文、
  机械规则（`pre`）、答案映射（`post`）、输出绑定（`out`）必须写在 `dsl_versions/*.dsl.txt`；
  Python 只实现「怎么探、怎么判 guard/gate/signal」。加载期校验，缺一块即 `DslError`。
- **DSL 自身可审（SOP）**：文件头写归属/签核/边界，每个判定写 `intent` 与 `refute_if`；
  评审读 DSL，**不生成第二份 Markdown**（否则必然漂移）。

> 迁移后的最终成绩见 `08_final_report.md`：**全量 754 机制口径 0.714**（基线 0.435），
> id 池 0.954、sealed 391 题 0.964、主路径模型调用 0、4.14 步/题。
