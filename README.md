# Cloud-OpsBench × AgentRun

把 [Cloud-OpsBench](https://github.com/LLM4Ops/Cloud-OpsBench) 的自由 ReAct 诊断 agent，迁移成
**「固定 SOP + 类型化 jev 判定 + escalate 兜底」**，并用递归自我改进（RSI）的方式迭代 SOP 本体。
SOP 写在一份**可人工评审签核的 DSL** 里（`workspace/dsl_versions/v0.13.dsl.txt`），不是代码。

**结果**：全量 754 题（机制口径，0 模型调用）根因诊断正确率 **0.435 → 0.714**；
可调参池 `id` 内 **0.954**，sealed 391 题 **0.964**；主路径模型调用 17 → **0**。

| 集合 | n | JRA | MC | EE | 步/题 |
|---|---|---|---|---|---|
| id · evolve | 115 | **0.957** | 0.875 | 0.689 | 4.59 |
| id · val | 43 | **0.861** | 0.853 | 0.665 | 4.74 |
| id · sealed（只跑一次） | 391 | **0.964** | 0.870 | 0.746 | 4.33 |
| ood · cross-system | 84 | 0.155 | 0.587 | 0.555 | 4.44 |
| ood · unseen-type | 36 | 0.028 | 0.463 | 0.355 | 5.03 |
| **全量 754** | 754 | **0.714** | — | — | 4.14 |

## 从这里开始

| 想了解 | 读 |
|---|---|
| **结项报告**（六轮轨迹、25 条负结果、剩余 216 题的对策、天花板估算） | [`workspace/08_final_report.md`](workspace/08_final_report.md) |
| 项目总览、目录、一键复现 | [`workspace/README.md`](workspace/README.md) |
| 当前状态与下一步 | [`workspace/state.md`](workspace/state.md) |
| 数据划分策略（RSI 纪律：evolve/val/sealed + OOD + 四道门） | [`workspace/07_partition.md`](workspace/07_partition.md) |
| 实验台账（18 轮 + 25 条负结果） | [`workspace/06_ledger.md`](workspace/06_ledger.md) |
| DSL 语法（五段式判定 + `intent`/`refute_if` + 签核头） | [`workspace/dsl_versions/README.md`](workspace/dsl_versions/README.md) |
| 方法论原稿（外部输入） | [`workspace/agentrun.md`](workspace/agentrun.md) |

## 目录

```
.
├── workspace/              # 项目本体（代码 / DSL / 评测 / 文档 / 结果）
│   ├── agentrun/           # 执行器：DSL 解析、五段式 decide、规则引擎、jev、judge、评测、划分、lint
│   ├── dsl_versions/       # v0.13 权威 SOP + 历史版本 + ablation + 版本表
│   ├── eval/               # partition_v5.json + 每轮任务文件
│   ├── runs/               # ★ 每轮测试结果（score / coverage / compare / final_live）
│   ├── traces/             # 轨迹证据（baseline、r8c_*、final_*）
│   ├── jev/                # pass A 请求 / 答案库 / 审计
│   └── 00_charter.md … 08_final_report.md
└── repo/                   # Cloud-OpsBench 数据集（git-ignored，3.8 GB，见下）
```

## 复现

```bash
# 1) 数据集（未随仓库分发）
git clone --depth 1 https://github.com/LLM4Ops/Cloud-OpsBench.git repo

# 2) 凭据
cp workspace/.env.template workspace/.env && $EDITOR workspace/.env

# 3) 机制口径（0 模型调用）：单轮 evolve 集
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --tasks-file workspace/eval/tasks_evolve_all.json --jev-mode mechanism \
        --save-root workspace/traces/final_evolve
python3 -m workspace.agentrun.score --label final --root workspace/traces/final_evolve \
        --layout agentrun --tasks-file workspace/eval/tasks_evolve_all.json \
        --out workspace/runs/score_final.json

# 4) 全量 754 扫描（约 2–4 分钟）
python3 -m workspace.agentrun.coverage --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --out workspace/runs/coverage_final.json

# 5) 门禁：禁 case 字面量 / SOP lint / 探针自检
python3 -m workspace.agentrun.critic    --dsl workspace/dsl_versions/v0.13.dsl.txt
python3 -m workspace.agentrun.dsl_check --dsl workspace/dsl_versions/v0.13.dsl.txt --review
python3 -m workspace.agentrun.coverage  --smoke boutique/service/5
```

## 边界

- 输入是**冻结快照**（`benchmark/` 每个 case 一个 tool cache 快照）：结论对快照时刻成立。
- **只读诊断**：不执行写操作（重启/回滚/扩缩容）、不做对外播报；处置仍走人工 runbook；不覆盖 SEV1。
- 剩余 216 题需要新能力（专用 code agent、更深的依赖图、DB 会话工具），逐类列在结项报告 §6。
- 早期 5 题数字属于 `evolve`（训练集），不是泛化结论。

## 致谢与授权

- 数据集：[Cloud-OpsBench](https://github.com/LLM4Ops/Cloud-OpsBench)（MIT），**未包含在本仓库**。
- 方法论：`workspace/agentrun.md`（项目方提供的外部输入，随仓库保留以便复现决策链）。
- 本项目代码：MIT（见 `LICENSE`）。
