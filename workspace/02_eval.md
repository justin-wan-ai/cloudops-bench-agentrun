# P2 评测协议

## 1. 一把尺子

两侧都用 **Cloud-OpsBench 官方评测器**：`repo/agents/cloudops_agent/evaluation.py`
（`score.py` 用 `importlib` 加载同一文件，不复制逻辑）。因此 CA/FA/JRA/MC/EOC/ECR/EE/RAR 定义完全一致。

| 指标 | 含义 | 方向 |
|---|---|---|
| CA / FA / JRA | 组件 / 故障类型 / 联合正确率（Rank 1） | ↑ |
| MC | milestone 覆盖率 | ↑ |
| EOC | 证据顺序一致性 | ↑ |
| ECR | 证据闭包（是否全部 milestone 组完成，0/1） | ↑ |
| EE | 证据效率 = 有贡献调用 / 总调用 | ↑ |
| Steps | 工具调用步数 | ↓ |
| RAR | 重复动作率 | ↓ |

## 2. 冻结集（v1 划分见 `07_partition.md`）

`eval/tasks_5.json`（旧种子集，现已并入 `evolve`）：`random.Random(42).sample(benchmark/*/*/*, 5)`，5 个不同 category、跨 2 个系统：

| case | 难度 | GT 组件 | GT 根因 |
|---|---|---|---|
| trainticket/runtime/61 | easy | `app/ts-delivery-service` | `mysql_invalid_port` |
| boutique/codedefect/60 | hard | `app/frontend` | `code_excessive_file_writes` |
| boutique/admission/32 | medium | `namespace/boutique` | `namespace_memory_quota_exceeded` |
| boutique/scheduling/109 | easy | `app/recommendationservice` | `node_selector_mismatch` |
| boutique/runtime/32 | easy | `app/checkoutservice` | `readiness_probe_incorrect_protocol` |

> GT 只用于**事后评分**与 P7 诊断，不参与规则编写（规则只编码 kubectl 语义；见 `04_distill.md` 的对照）。

## 3. 三口径（永不混用）

```bash
# 机制侧：不调模型，只跑确定性流程 + 机械回退
runner --jev-mode mechanism
# 含答案：注入 jev 答案库回放（pass A 请求 -> jevlib -> pass B）
runner --jev-mode answers --jev-in jev/ans/v1.json   # jevlib 从 live trace 生成的答案库
# 生产：真实 decision model + escalate judge
runner --jev-mode live
```

## 4. 确定性与噪声

- 同配置跑两次（`traces/determinism_a|b`）：**5/5 case 的决策、候选、答案、步数完全一致**，仅 `wall_s` 不同。
- 答案库回放 vs live：**5/5 决策序列与最终答案完全一致**，0 model call。
- n=5 的噪声：**1 题 = 20pp**。因此本项目的判定按「非退化 + 机制可解释」，不按 +3pp 显著性。

## 5. 双口径输出格式

`runs/score_*.json`：

```json
{"label": "final_live", "aggregate": {"CA":0.91,"MC":0.85,"steps":4.7,"model_calls":44,"tokens_in":26000},
 "per_case":[{"case":"boutique/runtime/32","CA":1,"MC":1.0,"steps":3,"RAR":0.0,"tool_calls":3,"family":"runtime"}]}
```

## 5.5 四道门（RSI 纪律，可执行）

| 门 | 命令 | 判定 |
|---|---|---|
| 正则检查器（禁 case 字面量） | `python3 -m workspace.agentrun.critic --dsl …` | 0 hit |
| 噪声门控 | val 集净提升 ≥2 题且不退化 | 单题提升不算收益 |
| OOD 熔断 | `tasks_ood_*` 上"自信答错"率不得上升（允许分数不涨） | 违反即回滚（R8 因此回滚） |
| Sealed 终局 | 定稿后跑一次 `tasks_sealed_all.json`，不参与选择 | 发布验收 |
| 探针自检 | `coverage --smoke <case>` | 任何探针返回 `Error:` 即失败 |

## 6. 门禁

- [x] 一键复现（README 五条命令）
- [x] 两次运行差异 = 0（除墙钟）
- [x] 双/三口径都有数字
- [x] 分型（family）/ 逐 case / 成本均可输出
- [x] 结果落盘并带 DSL 版本与 params（trace 的 `agentrun.params`）
