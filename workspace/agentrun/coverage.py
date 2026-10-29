"""Coverage scan: run the mechanism path over the whole benchmark, grouped by fault type.

Purpose: choose a *data partition* from evidence instead of by feel.  It answers, per
(system, fault_type): how many cases exist, whether the rule engine produced any
candidate, whether rank-1 was right, and whether the flow would escalate.

This reads `process-label/` for scoring only (never inside the solving path).

    python3 -m workspace.agentrun.coverage --out runs/coverage_754.json [--limit N]
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from .decide import JevTransport
from .dsl_parse import load_dsl
from .env import BENCHMARK, DSL_V01, PROCESS_LABEL, TASKS_FILE, WORKSPACE
from .runner import run_case


def iter_cases() -> List[Dict[str, str]]:
    cases = []
    for meta in sorted(BENCHMARK.glob("*/*/*/metadata.json")):
        system, category, case = meta.parts[-4], meta.parts[-3], meta.parts[-2]
        cases.append({"id": f"{system}/{category}/{case}", "system": system,
                      "category": category, "case": case})
    return cases


def gold(case: Dict[str, str]) -> Dict[str, str]:
    path = PROCESS_LABEL / case["system"] / case["category"] / case["case"] / "milestone.json"
    payload = json.loads(path.read_text())
    return payload.get("result", {})


def smoke(case_id: str) -> int:
    """Run every registered probe once and fail on tool errors.

    Guards the failure mode that cost R1/R3 a whole round each: a probe whose *arguments*
    are invalid (bad alias, mutually exclusive flags) returns "Error: ..." which the runner
    happily stores as an observation, silently blinding every downstream rule.
    """
    from .plans import PROBES
    from .state import CaseState
    from .anchors import extract_anchors
    from .benchbridge import SnapshotTools
    from .env import case_path

    system, category, case = case_id.split("/")
    path = case_path(system, category, case)
    state = CaseState(case_id=case_id, system=system, category=category,
                      anchors=extract_anchors(case_id, system, path),
                      tools=SnapshotTools(path, system, category))
    failures = 0
    for name, spec in sorted(PROBES.items()):
        args = spec["args"] if isinstance(spec["args"], dict) else spec["args"](state)
        if args is None:
            print(f"  skip  {name:<18} (args not applicable to this case)")
            continue
        result = state.tools.call(spec["tool"], args)
        text = str(result.get("observation") or "")
        if not result.get("success") or text.startswith("Error:"):
            failures += 1
            print(f"  FAIL  {name:<18} {spec['tool']} {args} -> {text[:120]}")
        else:
            print(f"  ok    {name:<18} {spec['tool']} {args} -> {len(text)}c")
    print(f"-- {failures} probe failure(s) on {case_id}")
    return 1 if failures else 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsl", default=str(WORKSPACE / "dsl_versions" / "v0.6.dsl.txt"))
    parser.add_argument("--out", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--smoke", default="", help="run every probe once on this case id")
    args = parser.parse_args(argv)

    if args.smoke:
        return smoke(args.smoke)
    if not args.out:
        raise SystemExit("--out is required for a coverage scan")

    dsl = load_dsl(args.dsl)
    transport = JevTransport(mode="mechanism", tag="scan")
    cases = iter_cases()
    if args.limit:
        cases = cases[: args.limit]

    import time
    rows = []
    for index, case in enumerate(cases, 1):
        t0 = time.perf_counter()
        trace = run_case(case, dsl, transport, "scan")
        extra = trace["agentrun"]
        answer = json.loads(trace["final_answer"])
        rank1 = (answer.get("top_3_predictions") or [{}])[0]
        truth = gold(case)
        rows.append({
            "case": case["id"], "system": case["system"], "category": case["category"],
            "fault_type": truth.get("root_cause"), "fault_target": truth.get("fault_object"),
            "family": extra["family"], "candidates": len(extra["candidates"]),
            "pick": (extra["picked"] or {}).get("root_cause"),
            "pick_object": (extra["picked"] or {}).get("object"),
            "correct": (str(rank1.get("fault_object")) == str(truth.get("fault_object"))
                        and str(rank1.get("root_cause")) == str(truth.get("root_cause"))),
            "would_escalate": bool(extra["escalated"])
                              or (extra["family"] == "codedefect"
                                  and not any(c.get("root_cause", "").startswith("code_")
                                              for c in extra["candidates"])),
            "tools": extra["tool_calls"],
        })
        import time as _t
        elapsed = _t.perf_counter() - t0
        if args.progress or elapsed > 5:
            print(f"  [{index}/{len(cases)}] {case['id']} {elapsed:.1f}s", flush=True)

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = f"{row['system']}/{row['fault_type']}"
        bucket = grouped.setdefault(key, {"system": row["system"], "fault_type": row["fault_type"],
                                         "n": 0, "candidate": 0, "correct": 0, "escalate": 0})
        bucket["n"] += 1
        bucket["candidate"] += 1 if row["candidates"] else 0
        bucket["correct"] += 1 if row["correct"] else 0
        bucket["escalate"] += 1 if row["would_escalate"] else 0
    for bucket in grouped.values():
        for field in ("candidate", "correct", "escalate"):
            bucket[f"{field}_rate"] = round(bucket[field] / bucket["n"], 3)

    total = len(rows)
    summary = {
        "dsl": args.dsl, "n": total,
        "candidate_rate": round(sum(1 for r in rows if r["candidates"]) / max(1, total), 3),
        "correct_rate": round(sum(1 for r in rows if r["correct"]) / max(1, total), 3),
        "escalate_rate": round(sum(1 for r in rows if r["would_escalate"]) / max(1, total), 3),
        "by_type": dict(sorted(grouped.items())),
        "cases": rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"== coverage n={total} candidate={summary['candidate_rate']} "
          f"correct={summary['correct_rate']} escalate={summary['escalate_rate']}")
    print(f"{'fault_type':<42} {'n':>4} {'cand':>5} {'ok':>4} {'esc':>4}")
    for key, bucket in summary["by_type"].items():
        print(f"{key:<42} {bucket['n']:>4} {bucket['candidate']:>5} {bucket['correct']:>4} {bucket['escalate']:>4}")
    print(f"[OUT] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
