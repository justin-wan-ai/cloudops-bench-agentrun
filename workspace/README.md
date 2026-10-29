# Cloud-OpsBench × AgentRun（SRE 根因诊断迁移）

把 Cloud-OpsBench 的**自由 ReAct 诊断 agent** 迁移成 **「固定 SOP + 类型化 jev 判定 + escalate 兜底」**，
并按递归自我改进（RSI）方式迭代 SOP 本体；数据划分遵循 `coding-agent-rsi-data-partitioning-guide.md`。

- 方法论：`agentrun.md`（只读）
- 数据集：`../repo/`（Cloud-OpsBench，754 case，只读资产）
- jev 客户端：`jev.py`（阿里云 decision model，真实调用，无 mock）
- 模型：DeepSeek（`.env` 里的 `DEEPSEEK_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL`）

## 当前最好：v0.13（六轮爬山，R1–R7 采纳，R8/R9 否决）

| 集合 | n | CA | FA | JRA | MC | EE | 步/题 |
|---|---|---|---|---|---|---|---|
| id · evolve | 115 | 0.965 | 0.957 | **0.957** | 0.875 | 0.689 | 4.59 |
| id · val | 43 | 0.907 | 0.884 | **0.861** | 0.853 | 0.665 | 4.74 |
| id · sealed（只跑一次） | 391 | 0.972 | 0.972 | **0.964** | 0.870 | 0.746 | 4.33 |
| ood · cross-system | 84 | 0.214 | 0.155 | 0.155 | 0.587 | 0.555 | 4.44 |
| ood · unseen-type | 36 | 0.056 | 0.028 | 0.028 | 0.463 | 0.355 | 5.03 |
| **全量 754** | 754 | — | — | **0.714** | — | — | 4.14 |

机制口径 = 0 模型调用。对照基线（原生 ReAct + deepseek-flash，5 题种子集）：JRA 1.00 / MC 0.793 /
17 次 LLM 调用 / 134.8k token / 58s —— 迁移后主路径模型调用 17 → 0、token −100%、墙钟 −99%。

**完整结项报告：`08_final_report.md`**（含六轮轨迹、25 条负结果、剩余 216 题的对策与天花板估算）。

## 文档索引

| 文件 | 内容 |
|---|---|
| `08_final_report.md` | ★ 最终报告（先读这个） |
| `state.md` | 活状态：当前阶段 / 下一步 / 阻塞项 |
| `00_charter.md` | P0 立项：任务画像、就绪度、基线、失败模式 |
| `01_assets.md` | P1 资产清单、GT 隔离、恢复演练 |
| `02_eval.md` | P2 评测协议：三口径、冻结集、确定性、门禁 |
| `03_trace.md` | P3 轨迹 schema（官方兼容 + `agentrun` 决策块） |
| `04_distill.md` | P4 蒸馏：槽位化、计划、jev 判定点、覆盖度 |
| `05_runner.md` | P5 Runner：DSL 驱动、策略归属、门禁证据 |
| `06_ledger.md` | P7 台账：18 轮实验 + 25 条负结果 |
| `07_partition.md` | 数据划分（v5）+ R1–R9 各轮结果 + 四道门 |
| `dsl_versions/README.md` | 版本 ↔ 成绩 ↔ 语法说明 |
| `coding-agent-rsi-data-partitioning-guide.md` | 划分方法论（外部输入） |

## 目录

```
workspace/
├── agentrun/               # 执行器与工具链
│   ├── env.py benchbridge.py anchors.py evidence.py state.py
│   ├── plans.py            # probe/gate/signal 语义（无策略表）
│   ├── rules.py            # 证据 → 候选根因（规则引擎）
│   ├── decide.py           # 五段式 decide 执行器
│   ├── dsl_parse.py dsl_check.py runner.py
│   ├── judge.py            # escalate 的 judge agent（DeepSeek）
│   ├── baseline.py         # 改造前基线（原生 ReAct）
│   ├── score.py diag.py coverage.py compare.py
│   ├── partition.py critic.py jevlib.py
├── dsl_versions/           # v0.13 权威 + 历史版本 + ablation/ + 版本表
├── eval/                   # partition_v5.json + tasks_{evolve,val,sealed}_all + 各 scope + ood
├── traces/  runs/          # 轨迹与评测输出（可直接删除重跑）
├── jev/{req,ans,audit}/    # pass A 请求 / 答案库 / 审计
└── .env                    # 凭据（不入库）
```

## 一键复现（机制口径）

```bash
cd /Users/justinwan/work/workspace/cloudops_bench_rsi

python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --tasks-file workspace/eval/tasks_evolve_all.json --jev-mode mechanism \
        --save-root workspace/traces/final_evolve
python3 -m workspace.agentrun.score --label final --root workspace/traces/final_evolve \
        --layout agentrun --tasks-file workspace/eval/tasks_evolve_all.json --out workspace/runs/score_final.json

python3 -m workspace.agentrun.coverage --dsl workspace/dsl_versions/v0.13.dsl.txt --out workspace/runs/coverage_final.json
python3 -m workspace.agentrun.critic   --dsl workspace/dsl_versions/v0.13.dsl.txt
python3 -m workspace.agentrun.dsl_check --dsl workspace/dsl_versions/v0.13.dsl.txt --review
```

## 已知边界

- 冻结快照：结论对快照时刻成立；只读诊断，不做写操作与播报；不覆盖 SEV1。
- 剩余 216 题需要新能力（code agent / 更深依赖图 / DB 工具面）——见 `08_final_report.md` §6。
- 早期版本的 5 题数字属于 `evolve`（训练集），不是泛化结论。
