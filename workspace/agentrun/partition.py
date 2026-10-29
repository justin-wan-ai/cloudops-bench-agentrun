"""Build the RSI data partition (suit x split) and the per-round task files.

Strategy (see 07_partition.md):
  * the leakage unit is the **fault type** (the "topic"); a case's type is never split
    across an OOD suite and an evolve set;
  * every case gets exactly one `suit` and one `split`, so nothing is accidentally used;
  * within an in-distribution type, cases are split evolve/val/sealed with a fixed seed
    (a type has few cases; type-disjoint splits would make it impossible to measure the
    fit we are actually improving -- the regularization critic + lint is what stops
    case-literal memorisation instead).

    python3 -m workspace.agentrun.partition --out eval/partition_v1.json \
        --round scheduling [--emit]
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List

from .env import BENCHMARK, COVERAGE, PROCESS_LABEL, WORKSPACE

# --- suits that never participate in hillclimbing -----------------------------
# cross-system: Java/Spring/Nacos stack; only reachable via the mechanism, not via rules
OOD_CROSS_SYSTEM = {"trainticket": ["service", "performance"]}   # R8 rolled back (R24)
# types we deliberately do not touch in v1: they need capabilities the SOP lacks
OOD_UNSEEN = {
    # v2 partition: the three network-latency types moved into `id` for round R4 *before* any
    # performance rule existed (no tuning had touched them), and the committed OOD set is now
    # the liveness-probe family + code_wrong_return.
    "liveness_probe_incorrect_protocol", "liveness_probe_incorrect_port", "liveness_probe_incorrect_timing",
    "code_wrong_return",
}
# quarantined: known-broken, no plan yet -> not scored at all until a round adopts them
QUARANTINE = {
    "code_busy_loop", "code_memory_leak", "code_artificial_delay", "code_excessive_file_reads",
    "code_missing_parameter", "code_wrong_argument_order",
    "db_readonly_mode", "db_connection_exhaustion",
}

# round = one evolve scope; 3 evolve + 1 val per fault type by default
DEFAULT_PER_TYPE = (3, 1)


def load_cases() -> List[Dict[str, Any]]:
    cases = []
    for meta in sorted(BENCHMARK.glob("*/*/*/metadata.json")):
        system, category, case = meta.parts[-4], meta.parts[-3], meta.parts[-2]
        payload = json.loads(meta.read_text())
        truth = payload.get("result", {})
        cases.append({
            "case": f"{system}/{category}/{case}", "system": system, "category": category,
            "difficulty": payload.get("difficulty"),
            "fault_type": truth.get("root_cause"), "fault_target": truth.get("fault_object"),
        })
    return cases


def load_coverage() -> Dict[str, Dict[str, Any]]:
    if not COVERAGE.is_file():
        return {}
    payload = json.loads(COVERAGE.read_text())
    return {row["case"]: row for row in payload.get("cases", [])}


def assert_disjoint_suits() -> None:
    cross = {ft for types in OOD_CROSS_SYSTEM.values() for ft in types}
    overlap = (set(OOD_UNSEEN) & QUARANTINE) | (set(OOD_UNSEEN) & cross) | (QUARANTINE & cross)
    if overlap:
        raise SystemExit(f"OOD/quarantine sets overlap: {sorted(overlap)}")


def suit_of(case: Dict[str, Any]) -> str:
    if case["system"] in OOD_CROSS_SYSTEM and case["category"] in OOD_CROSS_SYSTEM[case["system"]]:
        return "ood_cross_system"
    if case["fault_type"] in OOD_UNSEEN:
        return "ood_unseen_type"
    if case["fault_type"] in QUARANTINE:
        return "quarantine"
    return "id"


def build(cases: List[Dict[str, Any]], per_type: tuple[int, int], seed: int) -> Dict[str, Any]:
    rng = random.Random(seed)
    by_type: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for case in cases:
        by_type[f"{case['system']}/{case['fault_type']}"].append(case)

    roles: Dict[str, Dict[str, Any]] = {}
    n_evolve, n_val = per_type
    for key, group in sorted(by_type.items()):
        ordered = sorted(group, key=lambda item: item["case"])
        rng.shuffle(ordered)
        # small types must still contribute a val and a sealed case, otherwise the
        # split silently degrades into "everything is trainable"
        available = len(ordered)
        evolve_here = min(n_evolve, max(0, available - 2))
        val_here = min(n_val, max(0, available - evolve_here - 1))
        suit = suit_of(ordered[0])
        for index, case in enumerate(ordered):
            if suit != "id":
                split = "sealed"                      # OOD / quarantine: never tuned on
            elif index < evolve_here:
                split = "evolve"
            elif index < evolve_here + val_here:
                split = "val"
            else:
                split = "sealed"
            roles[case["case"]] = {"suit": suit, "split": split, "scope": case["category"],
                                   "fault_type": case["fault_type"], "system": case["system"]}
    return roles


def summarize(roles: Dict[str, Dict[str, Any]], coverage: Dict[str, Dict[str, Any]]) -> str:
    suits = collections.Counter(role["suit"] for role in roles.values())
    splits = collections.Counter(role["split"] for role in roles.values() if role["suit"] == "id")
    lines = [f"cases: {len(roles)}",
             "suits: " + "  ".join(f"{name}={count}" for name, count in sorted(suits.items())),
             "id splits: " + "  ".join(f"{name}={splits[name]}" for name in ("evolve", "val", "sealed")),
             "",
             f"{'type':<46}{'suit':<18}{'evolve':>7}{'val':>5}{'sealed':>8}{'cand':>6}{'ok':>6}"]
    by_type: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for case, role in roles.items():
        by_type[f"{role['system']}/{role['fault_type']}"].append(role)
    for key, group in sorted(by_type.items()):
        counts = collections.Counter(role["split"] for role in group)
        cand = [coverage[case]["candidates"] > 0 for case in
                (c for c, role in roles.items() if f"{role['system']}/{role['fault_type']}" == key) if case in coverage]
        ok = [coverage[case]["correct"] for case in
              (c for c, role in roles.items() if f"{role['system']}/{role['fault_type']}" == key) if case in coverage]
        lines.append(f"{key:<46}{group[0]['suit']:<18}{counts['evolve']:>7}{counts['val']:>5}"
                     f"{counts['sealed']:>8}"
                     f"{(sum(cand) / len(cand) if cand else 0):>6.2f}{(sum(ok) / len(ok) if ok else 0):>6.2f}")
    return "\n".join(lines)


def task_file(roles: Dict[str, Dict[str, Any]], cases: List[Dict[str, Any]], predicate) -> Dict[str, Any]:
    selected = [case for case in cases if predicate(roles[case["case"]], case["case"])]
    return {"n": len(selected), "cases": [
        {"id": case["case"], "system": case["system"], "category": case["category"],
         "case": case["case"].split("/")[-1], "difficulty": case["difficulty"]} for case in selected]}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(WORKSPACE / "eval" / "partition_v1.json"))
    parser.add_argument("--round", default="", help="scope to materialise as evolve/val task files")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--evolve-per-type", type=int, default=DEFAULT_PER_TYPE[0])
    parser.add_argument("--val-per-type", type=int, default=DEFAULT_PER_TYPE[1])
    parser.add_argument("--emit", action="store_true", help="write eval/tasks_*.json for this round")
    args = parser.parse_args(argv)

    assert_disjoint_suits()
    cases = load_cases()
    coverage = load_coverage()
    roles = build(cases, (args.evolve_per_type, args.val_per_type), args.seed)

    payload = {"seed": args.seed, "per_type": {"evolve": args.evolve_per_type, "val": args.val_per_type},
               "oods": {"cross_system": OOD_CROSS_SYSTEM, "unseen_types": sorted(OOD_UNSEEN),
                        "quarantine": sorted(QUARANTINE)},
               "roles": roles}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.round:
        scope = args.round

        def in_scope(role: Dict[str, Any], _case: str) -> bool:
            return role["suit"] == "id" and role["scope"] == scope

        for split in ("evolve", "val", "sealed"):
            path = WORKSPACE / "eval" / f"tasks_{split}_{scope}.json"
            data = task_file(roles, cases, lambda role, case, split=split: in_scope(role, case) and role["split"] == split)
            data["round"] = scope
            data["split"] = split
            if args.emit:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {split:<7} scope={scope:<14} n={data['n']:>3}  -> {path.name if args.emit else '(not written)'}")

    if args.emit:
        for suit in ("ood_cross_system", "ood_unseen_type", "quarantine"):
            path = WORKSPACE / "eval" / f"tasks_{suit}.json"
            data = task_file(roles, cases, lambda role, case, suit=suit: role["suit"] == suit)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"  {suit:<18} n={data['n']:>3}  -> {path.name}")

    print(summarize(roles, coverage))
    print(f"[OUT] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
