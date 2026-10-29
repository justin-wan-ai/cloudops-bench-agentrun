"""Generate the before/after trajectory comparison report (markdown).

Reads only the artifacts on disk (score JSONs + traces + marginal-credit reports),
so every number in the report is reproducible by re-running the three commands
listed at the bottom of the output.

    python3 -m workspace.agentrun.compare --out runs/compare_report.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from .env import RUNS, TASKS_FILE, WORKSPACE, TRACES_BASELINE

TRACES_AGENTRUN = WORKSPACE / "traces"

BASELINE_MODEL = "deepseek-flash"


def load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def trace(root: Path, case: Dict[str, Any], layout: str) -> Dict[str, Any]:
    if layout == "baseline":
        path = root / case["system"] / BASELINE_MODEL / case["category"] / case["case"] / f"{case['case']}.json"
    else:
        path = root / case["system"] / case["category"] / case["case"] / f"{case['case']}.json"
    return load(path) if path.is_file() else {}


def call_sequence(trace_data: Dict[str, Any]) -> str:
    steps = [s for s in trace_data.get("steps", []) if s.get("action_name")]
    parts = []
    for step in steps:
        args = step.get("action_input") or {}
        compact = ",".join(f"{k}={v}" for k, v in args.items() if k not in {"namespace"} and v not in (None, "", False))
        parts.append(f"{step['action_name']}({compact})" if compact else f"{step['action_name']}()")
    return " → ".join(parts)


def fmt(value: Any, digits: int = 3) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def table(header: List[str], rows: List[List[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        out.append("| " + " | ".join(fmt(cell) for cell in row) + " |")
    return "\n".join(out)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(RUNS / "compare_report.md"))
    args = parser.parse_args(argv)

    cases = json.loads(TASKS_FILE.read_text())["cases"]
    scores = {
        "baseline": load(RUNS / "score_baseline.json"),
        "agentrun_mechanism": load(RUNS / "score_agentrun_v06_mechanism.json"),
        "agentrun_live": load(RUNS / "score_agentrun_v06_live.json"),
        "agentrun_answers": load(RUNS / "score_agentrun_v06_answers.json"),
    }
    traces = {
        "baseline": {c["id"]: trace(TRACES_BASELINE, c, "baseline") for c in cases},
        "agentrun_mechanism": {c["id"]: trace(TRACES_AGENTRUN / "agentrun_v06_mechanism", c, "agentrun") for c in cases},
        "agentrun_live": {c["id"]: trace(TRACES_AGENTRUN / "agentrun_v06_live", c, "agentrun") for c in cases},
    }
    credit_mech = load(RUNS / "marginal_credit_v06_mechanism.json") if (RUNS / "marginal_credit_v06_mechanism.json").is_file() else None
    credit_base = load(RUNS / "marginal_credit_baseline.json")
    ablations = {name: load(RUNS / f"score_{name}.json") for name in ("ablation_v06_alerts", "ablation_v06_minw2")
                 if (RUNS / f"score_{name}.json").is_file()}

    lines: List[str] = []
    lines.append("# Cloud-OpsBench × AgentRun：前后轨迹对比（n=5 随机 case）\n")
    lines.append(f"样本集：`eval/tasks_5.json`（seed=42 随机抽样，5 个不同 category，跨 2 个系统）。"
                 f"评测尺子：Cloud-OpsBench 官方 `evaluation.py`（CA/FA/JRA + MC/EOC/ECR/EE/RAR），"
                 f"两侧走同一条评分代码路径。\n")

    lines.append("## 1. 三口径总表\n")
    rows = []
    for name, label in (("baseline", "baseline（原生 ReAct agent, deepseek-flash）"),
                        ("agentrun_mechanism", "agentrun v0.6 机制侧（jev=mechanism, 无模型调用）"),
                        ("agentrun_live", "agentrun v0.6 生产（jev=live, 真实 decision model）"),
                        ("agentrun_answers", "agentrun v0.6 回放（jev=answers, 答案库重放）")):
        agg = scores[name]["aggregate"]
        rows.append([label, f"{agg['CA']:.2f}", f"{agg['FA']:.2f}", f"{agg['JRA']:.2f}", f"{agg['MC']:.3f}",
                     f"{agg['EOC']:.3f}", f"{agg['ECR']:.2f}", f"{agg['EE']:.3f}", f"{agg['steps']:.2f}",
                     agg["RAR"], agg["tool_calls"], agg["model_calls"],
                     f"{agg['tokens_in']}/{agg['tokens_out']}", f"{agg['wall_s']:.1f}s"])
    lines.append(table(["配置", "CA", "FA", "JRA", "MC", "EOC", "ECR", "EE", "Steps", "RAR", "工具调用",
                        "模型调用", "tokens(in/out)", "墙钟"], rows))
    lines.append("")

    lines.append("## 2. 逐 case：轨迹形状\n")
    for case in cases:
        base = traces["baseline"][case["id"]]
        mech = traces["agentrun_mechanism"][case["id"]]
        live = traces["agentrun_live"][case["id"]]
        extra = mech.get("agentrun", {})
        answer = json.loads(mech.get("final_answer", "{}"))
        rank1 = (answer.get("top_3_predictions") or [{}])[0]
        lines.append(f"### {case['id']}  ({case['difficulty']}, query: `{(base.get('metadata') or {}).get('query', '')}`)\n")
        lines.append(f"- baseline：**{len(base.get('steps', []))} 步** / "
                     f"{sum(int(s.get('input_tokens') or 0) for s in base.get('steps', []))} in-tokens / "
                     f"{round(sum(float(s.get('model_latency') or 0) + float(s.get('tool_latency') or 0) for s in base.get('steps', [])), 1)}s")
        lines.append(f"- agentrun 机制侧：**{len(mech.get('steps', []))} 步** / 0 token / {extra.get('wall_s')}s / "
                     f"family=`{extra.get('family')}` / pick=`{rank1.get('fault_object')}` + `{rank1.get('root_cause')}`"
                     f"（rule: {(extra.get('picked') or {}).get('rule')}）")
        lines.append(f"- agentrun 生产：{len(live.get('steps', []))} 步 / {live.get('agentrun', {}).get('model_calls')} 次模型调用 / "
                     f"{live.get('agentrun', {}).get('wall_s')}s / escalate={live.get('agentrun', {}).get('escalated')}")
        lines.append(f"\n  - baseline 轨迹：`{call_sequence(base)}`")
        lines.append(f"  - agentrun 轨迹：`{call_sequence(mech)}`\n")
    lines.append("")

    lines.append("## 3. jev 决策面（v0.3 live，真实 decision model）\n")
    rows = []
    for case in cases:
        live = traces["agentrun_live"][case["id"]].get("agentrun", {})
        jev = live.get("jev", [])
        route = next((j for j in jev if j["kind"] == "route"), {})
        picks = [j for j in jev if j["kind"] == "pick"]
        verifies = [j for j in jev if j["kind"] == "verify"]
        rows.append([case["id"], route.get("choice") or "", route.get("status") or "",
                     f"{route.get('probabilities') or ''}"[:60],
                     f"{len([p for p in picks if p.get('status') == 'ans'])} 次模型 pick / {len(picks)} 次",
                     f"{len([v for v in verifies if v.get('status') == 'ans'])} 次模型 verify / {len(verifies)} 次",
                     live.get("escalated"), live.get("model_calls")])
    lines.append(table(["case", "route 选择", "route 状态", "route 概率", "pick", "verify", "escalate", "模型调用"], rows))
    lines.append("")

    lines.append("## 4. 证据边际贡献（每个工具调用赚到几个 milestone）\n")
    def credit_rows(report: Dict[str, Any], label: str) -> List[List[Any]]:
        out = []
        for item in report["cases"]:
            out.append([item["case"], label, item["calls"], item["credited_calls"], item["credit_ratio"],
                        " ".join(f"{row['tool']}:{row['credited']}" for row in item["trace"])])
        return out
    rows = credit_rows(credit_base, "baseline")
    if credit_mech:
        rows += credit_rows(credit_mech, "agentrun v0.6 机制侧")
    lines.append(table(["case", "配置", "调用数", "有贡献调用", "比例", "明细"], rows))
    lines.append("")

    if ablations:
        lines.append("## 5. P5 门禁：DSL/参数敏感性（改一处，行为必须变）\n")
        rows = []
        base = scores["agentrun_mechanism"]["aggregate"]
        rows.append(["v0.6 基线", base["CA"], base["MC"], base["steps"], base["tool_calls"], "—"])
        for name, payload in ablations.items():
            agg = payload["aggregate"]
            rows.append([name, agg["CA"], agg["MC"], agg["steps"], agg["tool_calls"],
                         "改参数/删节点后轨迹与成绩变化" if agg["CA"] != base["CA"] or agg["tool_calls"] != base["tool_calls"] else "无变化（该死变量）"])
        lines.append(table(["变体", "CA", "MC", "Steps", "工具调用", "判定"], rows))
        lines.append("")

    lines.append("## 6. 复现命令\n")
    lines.append("```bash")
    lines.append("# 0) 基线（原生 ReAct agent）")
    lines.append("python3 -m workspace.agentrun.baseline --max-iterations 20")
    lines.append("python3 -m workspace.agentrun.score --label baseline --root workspace/traces/baseline \\")
    lines.append("        --layout baseline --out workspace/runs/score_baseline.json")
    lines.append("# 1) 机制侧（0 模型调用）")
    lines.append("python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.4.dsl.txt \\")
    lines.append("        --jev-mode mechanism --save-root workspace/traces/agentrun_v06_mechanism \\")
    lines.append("        --eval-out workspace/runs/agentrun_v06_mechanism.json")
    lines.append("# 2) 生产（真实 jev）")
    lines.append("python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.4.dsl.txt --jev-mode live \\")
    lines.append("        --save-root workspace/traces/agentrun_v06_live --jev-req workspace/jev/req/v06_live.jsonl")
    lines.append("# 3) 答案库回放（pass A -> library -> pass B，0 token 复现）")
    lines.append("python3 -m workspace.agentrun.jevlib --root workspace/traces/agentrun_v06_live --out workspace/jev/ans/v4.json")
    lines.append("python3 -m workspace.agentrun.runner --dsl workspace/dsl_versions/v0.4.dsl.txt --jev-mode answers \\")
    lines.append("        --jev-in workspace/jev/ans/v4.json --save-root workspace/traces/agentrun_v06_answers")
    lines.append("# 4) 诊断（每个工具调用的边际证据贡献）")
    lines.append("python3 -m workspace.agentrun.diag --root workspace/traces/agentrun_v06_mechanism --layout agentrun \\")
    lines.append("        --out workspace/runs/marginal_credit_v06_mechanism.json")
    lines.append("```")
    lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OUT] {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
