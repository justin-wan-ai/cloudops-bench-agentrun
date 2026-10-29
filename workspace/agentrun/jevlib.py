"""Build a jev answer library from a live run (pass A -> answer library -> pass B).

    python3 -m workspace.agentrun.jevlib --root traces/agentrun_v02_live --out jev/ans/v1.json

The library is keyed by the stable rid, so replaying it (``--jev-mode answers``)
reproduces every decision without calling the model.  This is the P5 replay gate
and the P6 answer-governance artifact at the same time.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from .env import TASKS_FILE
from .score import trace_path


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args(argv)

    cases = json.loads(TASKS_FILE.read_text())["cases"]
    library: Dict[str, Dict[str, Any]] = {"route": {}, "pick": {}, "verify": {}}
    audit: List[Dict[str, Any]] = []
    for case in cases:
        path = trace_path(Path(args.root), case, "agentrun", args.model)
        if not path.is_file():
            print(f"[MISS] {case['id']}")
            continue
        trace = json.loads(path.read_text())
        for entry in trace.get("agentrun", {}).get("jev", []):
            rid, kind = entry.get("rid"), entry.get("kind")
            if kind == "route" and entry.get("status") in {"ans", "mechanical", "abstain"}:
                library["route"][rid] = entry.get("choice")
            elif kind == "pick" and entry.get("status") == "ans":
                library["pick"][rid] = int(entry.get("choice"))
            elif kind == "verify" and entry.get("status") == "ans" and entry.get("prob") is not None:
                library["verify"][rid] = float(entry["prob"])
            audit.append({"rid": rid, "kind": kind, "status": entry.get("status"),
                          "choice": entry.get("choice"), "prob": entry.get("prob"),
                          "mode": entry.get("mode"), "usage": entry.get("usage")})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8")
    audit_path = out.with_name(out.stem + "_audit.json")
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[LIB] route={len(library['route'])} pick={len(library['pick'])} verify={len(library['verify'])}")
    print(f"[OUT] {out}")
    print(f"[AUDIT] {audit_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
