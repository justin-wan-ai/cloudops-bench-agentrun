"""Score a trace tree with the *official* Cloud-OpsBench evaluator.

Both sides of the comparison are scored by the same code path
(``repo/agents/cloudops_agent/evaluation.py``), so CA/FA/JRA/MC/EOC/ECR/EE and the
redundant-action rate are directly comparable.  We only add cost metrics
(tool calls, model calls, tokens, wall time) that the official scorer ignores.

    python3 -m workspace.agentrun.score --label baseline \
        --root traces/baseline --layout baseline --out runs/score_baseline.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import benchbridge  # noqa: F401  (sys.path side effect)
from .env import BENCH_ROOT, PROCESS_LABEL, TASKS_FILE, agent_model

_SPEC = importlib.util.spec_from_file_location(
    "cloudops_eval", BENCH_ROOT / "agents" / "cloudops_agent" / "evaluation.py"
)
official = importlib.util.module_from_spec(_SPEC)
sys.modules["cloudops_eval"] = official
_SPEC.loader.exec_module(official)  # type: ignore[union-attr]

METRICS = ("CA", "FA", "JRA", "MC", "EOC", "ECR", "EE")


def trace_path(root: Path, case: Dict[str, Any], layout: str, model: str) -> Path:
    if layout == "baseline":
        return root / case["system"] / model / case["category"] / case["case"] / f"{case['case']}.json"
    return root / case["system"] / case["category"] / case["case"] / f"{case['case']}.json"


def agent_root(root: Path, case: Dict[str, Any], layout: str, model: str) -> Path:
    if layout == "baseline":
        return root / case["system"] / model / case["category"]
    return root / case["system"] / case["category"]


def cost_of(trace: Dict[str, Any]) -> Dict[str, Any]:
    if "agentrun" in trace:
        block = trace["agentrun"]
        return {
            "tool_calls": block.get("tool_calls", len(trace.get("steps", []))),
            "model_calls": block.get("model_calls", 0),
            "tokens_in": block.get("tokens_in", 0),
            "tokens_out": block.get("tokens_out", 0),
            "wall_s": block.get("wall_s"),
        }
    steps = trace.get("steps", []) or []
    return {
        "tool_calls": len([s for s in steps if s.get("action_type") == "tool"]),
        "model_calls": len([s for s in steps if s.get("action_type") in {"tool", "invalid", "final"}]),
        "tokens_in": sum(int(s.get("input_tokens") or 0) for s in steps),
        "tokens_out": sum(int(s.get("output_tokens") or 0) for s in steps),
        "wall_s": round(sum(float(s.get("model_latency") or 0) + float(s.get("tool_latency") or 0) for s in steps), 2),
    }


def score(label: str, root: Path, layout: str, tasks: List[Dict[str, Any]], model: str) -> Dict[str, Any]:
    per_case: List[Dict[str, Any]] = []
    for case in tasks:
        path = trace_path(root, case, layout, model)
        detail = official.evaluate_case(case["case"], PROCESS_LABEL / case["system"] / case["category"],
                                        agent_root(root, case, layout, model))
        trace = json.loads(path.read_text()) if path.is_file() else {}
        steps = trace.get("steps", []) or []
        tool_steps = [s for s in steps if s.get("action_type") == "tool" and s.get("action_name")]
        invalid, _ = official.invalid_tool_actions(trace) if trace else (0, [])
        row = {
            "case": case["id"],
            "exists": path.is_file(),
            **{metric: round(float(detail["metrics"].get(metric, 0.0)), 4) for metric in METRICS},
            "steps": len(tool_steps),
            "RAR": round(official.redundant_action_rate(tool_steps), 4) if tool_steps else 0.0,
            "invalid_actions": invalid,
            **cost_of(trace),
        }
        if "agentrun" in trace:
            row["family"] = trace["agentrun"].get("family")
            row["escalated"] = trace["agentrun"].get("escalated")
        per_case.append(row)

    aggregate: Dict[str, Any] = {}
    for metric in METRICS:
        aggregate[metric] = round(sum(row[metric] for row in per_case) / max(1, len(per_case)), 4)
    aggregate["steps"] = round(sum(row["steps"] for row in per_case) / max(1, len(per_case)), 2)
    aggregate["RAR"] = round(sum(row["RAR"] for row in per_case) / max(1, len(per_case)), 4)
    aggregate["tool_calls"] = sum(int(row["tool_calls"] or 0) for row in per_case)
    aggregate["model_calls"] = sum(int(row["model_calls"] or 0) for row in per_case)
    aggregate["tokens_in"] = sum(int(row["tokens_in"] or 0) for row in per_case)
    aggregate["tokens_out"] = sum(int(row["tokens_out"] or 0) for row in per_case)
    aggregate["wall_s"] = round(sum(float(row["wall_s"] or 0) for row in per_case), 2)
    aggregate["n"] = len(per_case)
    return {"label": label, "root": str(root), "layout": layout, "per_case": per_case, "aggregate": aggregate}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="run")
    parser.add_argument("--root", required=True)
    parser.add_argument("--layout", choices=["baseline", "agentrun"], default="agentrun")
    parser.add_argument("--model", default="")
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--tasks-file", default=str(TASKS_FILE))
    args = parser.parse_args(argv)

    tasks = json.loads(Path(args.tasks_file).read_text())["cases"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        tasks = [c for c in tasks if c["id"] in wanted]
    model = args.model or agent_model().model

    report = score(args.label, Path(args.root), args.layout, tasks, model)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    agg = report["aggregate"]
    print(f"== {report['label']} (n={agg['n']}) ==")
    print("  " + "  ".join(f"{m}={agg[m]:.3f}" for m in METRICS))
    print(f"  steps={agg['steps']} RAR={agg['RAR']} tool_calls={agg['tool_calls']} "
          f"model_calls={agg['model_calls']} tok_in={agg['tokens_in']} tok_out={agg['tokens_out']} wall={agg['wall_s']}s")
    for row in report["per_case"]:
        print(f"  {row['case']:<24} CA={row['CA']:.0f} FA={row['FA']:.0f} JRA={row['JRA']:.0f} "
              f"MC={row['MC']:.2f} steps={row['steps']:>2} tools={row['tool_calls']:>2} "
              f"tok={row['tokens_in']}/{row['tokens_out']} {row.get('family','')}")
    print(f"[OUT] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
