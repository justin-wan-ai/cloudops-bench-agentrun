"""Regularization critic: forbid case-specific literals inside the SOP.

The partition lets us split *within* a fault type, which is only sound if the rules
cannot memorise a particular case.  This critic scans the SOP (DSL) and the rule engine
for literals that can only come from one snapshot:

  * pod names            checkoutservice-74fc7d4686-9vlrn
  * case ids / paths     boutique/runtime/32, "32"
  * quota / secret names memory-request-quota
  * timestamps, IPs      Fri, 21 Nov 2025 18:30:56, 172.20.2.80
  * equality against a case identifier

    python3 -m workspace.agentrun.critic [--dsl ...] [--code ...]

Exit code 1 means "this change is overfit to a case" -> do not accept the version.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from .env import TASKS_FILE, WORKSPACE

PATTERNS: List[Tuple[str, str]] = [
    ("pod_name", r"\b[a-z][a-z0-9-]{2,}-[0-9a-f]{6,10}-[a-z0-9]{4,6}\b"),
    ("ipv4", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    ("timestamp", r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"),
    ("rfc_date", r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+\w{3}\s+\d{4}"),
    ("quota_name", r"\b[a-z][a-z0-9-]*-quota\b"),
    ("secret_name", r"\bts-[a-z-]+-mysql\b"),
    ("case_id", r"\b(?:boutique|trainticket)/[a-z]+/\d+\b"),
    ("case_equality", r"case_id\s*==|case\s*==\s*['\"](?:boutique|trainticket)/"),
]

DEFAULT_TARGETS = [
    WORKSPACE / "dsl_versions" / "v0.6.dsl.txt",
    WORKSPACE / "agentrun" / "rules.py",
    WORKSPACE / "agentrun" / "plans.py",
]

# The evolve split is where an overfit would come from; scan for its case-derived tokens too.
def evolve_tokens() -> set[str]:
    try:
        tasks = json.loads((WORKSPACE / "eval" / "tasks_evolve_scheduling.json").read_text())
    except OSError:
        return set()
    tokens: set[str] = set()
    for case in tasks.get("cases", []):
        # only full ids: a bare "/3" would match arithmetic all over the code
        tokens.add(case["id"])
    return tokens


def scan(path: Path) -> List[Tuple[int, str, str]]:
    hits: List[Tuple[int, str, str]] = []
    if not path.is_file():
        return hits
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for label, pattern in PATTERNS:
            for match in re.finditer(pattern, line):
                hits.append((number, label, match.group(0)))
    return hits


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsl", default="")
    parser.add_argument("--code", action="append", default=[])
    args = parser.parse_args(argv)

    targets = [Path(args.dsl)] if args.dsl else list(DEFAULT_TARGETS)
    targets += [Path(item) for item in args.code]

    tokens = evolve_tokens()
    total = 0
    for path in targets:
        hits = scan(path)
        if tokens:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if path.is_file() else []:
                for token in tokens:
                    if token and token in line:
                        hits.append((number, "evolve_case_token", token))
        for number, label, value in sorted(set(hits)):
            total += 1
            print(f"FAIL  {path.name}:{number}  {label}: {value}")
        print(f"  {'clean' if not hits else 'HITS'} {path}")
    print(f"-- {total} case-specific literal(s)")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
