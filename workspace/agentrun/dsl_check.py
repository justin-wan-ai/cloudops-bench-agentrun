"""Review a DSL file: print the compiled procedure, or lint it like an SOP.

    python3 -m workspace.agentrun.dsl_check --dsl dsl_versions/v0.6.dsl.txt
    python3 -m workspace.agentrun.dsl_check --dsl dsl_versions/v0.6.dsl.txt --review

`--review` does not render a second document: it answers the questions an SRE asks
while signing an SOP off (dead branch, unreachable rule, wiring that a later node
depends on, missing rationale), and exits non-zero on hard failures.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Tuple

from .dsl_parse import Dsl, DslError, load_dsl
from .env import WORKSPACE


def render_action(action: Dict[str, Any]) -> str:
    parts = [f"use={action['use']}"]
    for key in ("dominant", "require", "continue_loop", "escalate", "why"):
        if key in action and action[key] not in (False, None):
            parts.append(f"{key}={action[key]}")
    return "  ".join(parts)


def describe(dsl: Dsl) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "path": dsl.path, "version": dsl.version, "sop": dsl.sop,
        "params": dict(dsl.params), "state": dsl.state,
        "plans": {name: [{"step": step["step"], "gate": step["gate"]} for step in steps]
                  for name, steps in dsl.plans.items()},
        "flow": [], "decide": [],
    }
    for node in dsl.flow:
        if node["kind"] != "loop":
            out["flow"].append(f"{node['id']}:{node['kind']}")
            continue
        out["flow"].append(f"{node['id']}:loop(" + ", ".join(f"{c['id']}:{c['kind']}" for c in node["body"]) + ")")
    for node in dsl.decide_nodes():
        out["decide"].append({
            "id": node["id"], "enabled": node["enabled"], "ask": node["ask"],
            "intent": node["intent"], "refute_if": node["refute_if"],
            "space": node["space"], "input": node["input"],
            "pre": [{"if": rule["if"], "then": render_action(rule["action"])} for rule in node["pre"]],
            "post": {key: render_action(action) for key, action in node["post"].items()},
            "out": node["out"],
        })
    return out


def print_human(report: Dict[str, Any]) -> None:
    sop = report["sop"]
    print(f"== SOP {sop['sop']}  (DSL v{report['version']})  {report['path']}")
    print(f"scope      : {sop['scope']}")
    print(f"owner      : {sop['owner']}   approved_by={sop['approved_by']}   reviewed_at={sop['reviewed_at']}"
          f"   ticket={sop['change_ticket']}")
    print(f"boundaries : {' '.join(str(sop['boundaries']).split())[:160]}")
    print("state      : " + " ; ".join(f"{slot} <- {ref}" for slot, ref in report["state"].items()))
    print("flow       : " + " -> ".join(report["flow"]))
    for node in report["decide"]:
        space = node["space"]
        print(f"\n[{node['id']}] decide   enabled={node['enabled']}")
        print(f"  intent    : {' '.join(node['intent'].split())[:150]}")
        print(f"  refute_if : {' '.join(node['refute_if'].split())[:150]}")
        print(f"  ask       : {node['ask'].splitlines()[0][:110]}")
        if space["from"] == "signals":
            print(f"  space     : signals  other={space['other']}")
            for entry in space["order"]:
                print(f"              {entry['when']:<20} -> {entry['value']}")
        elif space["from"] == "candidates":
            print(f"  space     : candidates  limit={space['limit']}  other={space['other']}")
        else:
            print(f"  space     : fixed  options={space['options']}  other={space['other']}")
        for index, rule in enumerate(node["pre"], 1):
            print(f"  pre  {index:<2}   : if {rule['if']:<44} then {rule['then']}")
        for key, action in node["post"].items():
            print(f"  post.{key:<12}: {action}")
        print("  out       : " + "  ".join(f"{name}={source}" for name, source in node["out"].items()))


def lint(dsl: Dsl) -> List[Tuple[str, str, str]]:
    """Return [(level, where, message)]; level in FAIL/WARN/INFO."""
    findings: List[Tuple[str, str, str]] = []

    def add(level: str, where: str, message: str) -> None:
        findings.append((level, where, message))

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(dsl.sop["reviewed_at"])):
        add("FAIL", "reviewed_at", "must be an ISO date (YYYY-MM-DD) so the review can expire")
    family_scope = str((dsl.sop["scope"] or {}).get("family") or "")
    if family_scope and family_scope not in dsl.plans:
        add("FAIL", "scope.family", f"{family_scope!r} has no plans entry, so this SOP can never run that scope")

    reasons = {reason for node in dsl.flow if node["kind"] == "escalate" for reason in node["when"]}
    if "contradicted" in reasons and "verify_status" not in dsl.state:
        add("FAIL", "escalate", "`contradicted` is used but state.verify_status is not wired")
    if "weak_after_waves" in reasons and "max_waves" not in dsl.params:
        add("FAIL", "escalate", "`weak_after_waves` needs params.max_waves")

    decide_ids = [node["id"] for node in dsl.decide_nodes()]
    if len(decide_ids) != len(set(decide_ids)):
        add("FAIL", "flow", "decide node ids are not unique")

    for node in dsl.decide_nodes():
        where = node["id"]
        guards = [rule["if"] for rule in node["pre"]]
        if not node["pre"]:
            add("WARN", where, "no pre rules: every call would spend a model round-trip")
        for index, guard in enumerate(guards):
            # a duplicated guard is the only *provably* unreachable rule: the earlier copy
            # already returns.  (no_options / no_pick do NOT shadow later rules -- when they
            # do not match, evaluation simply continues.)
            if guards.count(guard) > 1 and guards.index(guard) != index:
                add("WARN", where, f"pre[{index}] duplicates an earlier guard {guard!r} (unreachable)")
        space = node["space"]
        if any(guard.startswith("top_over_second") for guard in guards) and space["from"] != "candidates":
            add("FAIL", where, "top_over_second only makes sense for a candidates space")
        if not space.get("render") and space["from"] == "candidates":
            add("WARN", where, "space.render is empty: the model would only see option ids")
        uses = {action.get("use") for action in node["post"].values()}
        if "previous" in uses and "unchanged_since_last" not in guards:
            add("INFO", where, "post uses `previous` but no unchanged_since_last rule can produce it")
        if "stored" in uses and "has_stored_answer" not in guards:
            add("INFO", where, "post uses `stored` but no has_stored_answer rule can produce it")
        if space["from"] == "fixed":
            extra = [key for key in node["post"] if key not in space["options"]
                     and key not in {"default", "other", "error", "unavailable"}]
            if extra:
                add("FAIL", where, f"post keys {extra} are not options of this space (dead branches)")
        families = [entry["value"] for entry in space.get("order", [])]
        duplicates = {family for family in families if families.count(family) > 1}
        if duplicates:
            add("INFO", where, f"several signals map to the same family {sorted(duplicates)} -- "
                               "only the first firing signal is used per family")
        if space["other"] and "other" not in node["post"]:
            add("FAIL", where, "space.other=true but post.other missing")

    rules_src = ""
    try:
        rules_src = (WORKSPACE / "agentrun" / "rules.py").read_text(encoding="utf-8")
    except OSError:
        pass
    if rules_src:
        from .plans import PROBES

        for name, steps in dsl.plans.items():
            for step in steps:
                key = PROBES[step["step"]]["key"]
                if key.startswith("describe:"):
                    used = key.split(":", 1)[0] in rules_src
                else:
                    used = f'"{key}"' in rules_src
                if not used:
                    add("INFO", f"plans.{name}", f"step {step['step']} fetches {key!r} but no rule reads it")
    for name, steps in dsl.plans.items():
        for step in steps:
            if step["gate"] == ["always"] and name != "census" and len(steps) > 1:
                add("INFO", f"plans.{name}", f"step {step['step']} is ungated: it always spends a call")
    return findings


def print_review(findings: List[Tuple[str, str, str]]) -> int:
    order = {"FAIL": 0, "WARN": 1, "INFO": 2}
    findings = sorted(findings, key=lambda item: (order[item[0]], item[1]))
    counts = {level: sum(1 for finding in findings if finding[0] == level) for level in order}
    for level, where, message in findings:
        print(f"{level:<5} {where:<18} {message}")
    print(f"-- {counts['FAIL']} fail / {counts['WARN']} warn / {counts['INFO']} info")
    return 1 if counts["FAIL"] else 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsl", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--review", action="store_true", help="lint the SOP instead of printing it")
    args = parser.parse_args(argv)
    try:
        dsl = load_dsl(args.dsl)
    except DslError as exc:
        print(f"[DSL ERROR] {exc}", file=sys.stderr)
        return 2
    if args.review:
        return print_review(lint(dsl))
    report = describe(dsl)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_human(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
