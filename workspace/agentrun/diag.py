"""P7 diag: marginal evidence credit of each tool call in a trace.

For every step we re-run the official milestone matcher on the prefix and report
how many milestone groups the call added.  Zero-credit calls are the pruning
targets (this is how v0.2 was derived from the v0.1 traces).

    python3 -m workspace.agentrun.diag --root traces/agentrun_v02_mechanism --layout agentrun \
        --out runs/marginal_credit_v02.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import score as S
from .env import PROCESS_LABEL, TASKS_FILE
from .score import agent_root, trace_path


def credit(trace: Dict[str, Any], label_path: Path) -> List[Dict[str, Any]]:
    raw = json.loads(label_path.read_text())
    for milestone in raw.get("milestones", []) or []:
        milestone.setdefault("description", "")
        milestone.setdefault("role", "")
    annotation = S.official.CaseAnnotation.from_dict(raw)
    steps = S.official.agent_trajectory(trace)
    rows: List[Dict[str, Any]] = []
    previous = 0
    for index in range(len(steps)):
        result = S.official.evaluate_trajectory(annotation, steps[: index + 1])
        established = getattr(result, "established_milestones", None) or getattr(result, "established", [])
        rows.append({
            "i": index,
            "tool": steps[index].tool_call.tool_name,
            "arguments": dict(steps[index].tool_call.arguments),
            "credited": len(established) - previous,
        })
        previous = len(established)
    return rows


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--layout", choices=["baseline", "agentrun"], default="agentrun")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--out", required=True)
    parser.add_argument("--tasks-file", default=str(TASKS_FILE))
    args = parser.parse_args(argv)

    tasks = json.loads(Path(args.tasks_file).read_text())["cases"]
    report = {"root": args.root, "cases": []}
    for case in tasks:
        path = trace_path(Path(args.root), case, args.layout, args.model)
        label = PROCESS_LABEL / case["system"] / case["category"] / case["case"] / "milestone.json"
        trace = json.loads(path.read_text())
        rows = credit(trace, label)
        credited = sum(1 for row in rows if row["credited"] > 0)
        report["cases"].append({
            "case": case["id"],
            "calls": len(rows),
            "credited_calls": credited,
            "credit_ratio": round(credited / max(1, len(rows)), 3),
            "trace": rows,
        })
        print(f"{case['id']:<24} calls={len(rows)} credited={credited} "
              + " ".join(f"{row['tool']}:{row['credited']}" for row in rows))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    total_calls = sum(c["calls"] for c in report["cases"])
    total_credit = sum(c["credited_calls"] for c in report["cases"])
    print(f"TOTAL calls={total_calls} credited={total_credit} ratio={total_credit / max(1, total_calls):.3f} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
