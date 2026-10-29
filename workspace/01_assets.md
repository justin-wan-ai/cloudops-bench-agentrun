# P1 资产清单

所有资产在持久目录 `workspace/`（无 `/tmp`）。数据快照当只读用。

| 资产 | 路径 | 来源 | 校验 | 恢复步骤 |
|---|---|---|---|---|
| benchmark 数据（754 case） | `../repo/benchmark/` | GitHub `LLM4Ops/Cloud-OpsBench` | 目录数 754 | `git clone --depth 1` 重下（3.6 GB） |
| 过程标注 milestones | `../repo/process-label/` | 同上 | 每个 case 有 `milestone.json` | 同上 |
| 诊断工具（模拟 kubectl） | `../repo/agents/cloudops_agent/{tools,runtime}` | 同上 | import 后 `len(registry)` = 12/10 | 同上 |
| 冻结评测集（5 case） | `eval/tasks_5.json` | seed=42 随机抽样 | 5 条 + 采样元数据 | 重跑抽样脚本（README §4） |
| baseline 轨迹 | `traces/baseline/<system>/deepseek-flash/<cat>/<case>/` | 原生 agent | 与官方 `evaluation.py` 兼容 | `python3 -m workspace.agentrun.baseline` |
| agentrun 轨迹 | `traces/{final_evolve,final_live,r8c_*}/`（可删可重跑） | runner | 同 schema | `python3 -m workspace.agentrun.runner` |
| jev 请求（pass A） | `jev/req/{v0*,final_live}.jsonl` | runner `--jev-req` | 每行一个受限请求 | 重跑 live |
| jev 答案库 | `jev/ans/v*.json` + `*_audit.json` | `agentrun/jevlib.py` | route/pick/verify 计数 | 从 live trace 重建 |
| 评测结果 | `runs/score_*.json`, `runs/compare_report.md` | `score.py` / `compare.py` | 可重算 | 见 README |
| DSL 版本 | `dsl_versions/v0.13.dsl.txt`（权威）+ `ablation/v06_*.dsl.txt` | 本仓库 | `dsl_check` 加载期校验 + `--review` lint 通过 | git |
| DSL 归档 | `dsl_versions/v0.1–v0.5.dsl.txt` | 本仓库 | **不可加载**（旧语法/缺 front-matter，符合预期） | git（历史证据） |

## GT 隔离（不是提示词隔离）

- 求解路径只通过 `anchors.load_case_input()` 读 `metadata.json`，**allowlist** = `namespace / query / difficulty`；
  读取 `result / fault_object / root_cause / fault_taxonomy` 会抛 `GoldLeak`。
- gold 只出现在两处，均在求解路径之外：
  1. `repo/process-label/**/milestone.json`（只在 `score.py` / `diag.py` 里读）；
  2. `repo/benchmark/**/metadata.json#result`（求解路径从不读）。
- 工具调用只能通过官方 `SnapshotTools`（`ToolExecutor`），与 baseline 完全同源，避免「换个工具就变简单」。

## 恢复演练（P1 门禁）

```bash
# 从零恢复数据 + 复跑一条 case，确认闭环
python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.13.dsl.txt \
        --jev-mode mechanism --only boutique/runtime/32 --save-root /tmp/rca_smoke
python3 -m workspace.agentrun.score --label smoke --root /tmp/rca_smoke --layout agentrun \
        --only boutique/runtime/32 --out /tmp/rca_smoke/score.json
# 结果应为 CA=FA=JRA=1，steps=3，tokens=0
```

> `/tmp` 只用于一次性 smoke，不作为存储。
