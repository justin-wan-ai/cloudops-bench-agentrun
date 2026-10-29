"""AgentRun runner (DSL v0.5): flow in, trace out.

    python3 -m workspace.agentrun.runner --dsl dsl_versions/v0.5.dsl.txt \
        --jev-mode live --save-root traces/agentrun_v05_live --jev-req jev/req/v05_live.jsonl

Execution is a straight walk over the DSL `flow:` list; each node kind below has one
implementation, and nothing about *which* node runs, *what* a decision maps to, or *what*
happens on abstention lives outside the DSL.  `python -m workspace.agentrun.dsl_check`
prints the compiled tables for review.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import evidence as ev  # noqa: F401  (re-exported for nodes that need parsing)
from .anchors import extract_anchors
from .benchbridge import SnapshotTools
from .decide import JevTransport, run_decide
from .dsl_parse import Dsl, load_dsl
from .env import RUNS, TASKS_FILE, agent_model, case_path
from .judge import escalate
from .plans import build_plan, census_plan
from .rules import derive_candidates as derive_rules
from .state import CaseState


# ---------------------------------------------------------------- refs


def make_resolver(state: CaseState, params: Dict[str, Any], bindings: Dict[str, Dict[str, Any]]):
    def resolve(ref: str) -> Any:
        if not isinstance(ref, str):
            return ref
        if ref.startswith("$params."):
            return params[ref.split(".", 1)[1]]
        if ref.startswith("state."):
            return getattr(state, ref.split(".", 1)[1], None)
        if ref.startswith("$"):
            node, _, field_name = ref[1:].partition(".")
            return (bindings.get(node) or {}).get(field_name)
        return ref

    return resolve


def apply_state_wiring(dsl: Dsl, state: CaseState, resolve) -> None:
    """Copy decision outputs into the runtime slots the rule engine reads."""
    for slot, ref in dsl.state.items():
        value = resolve(ref)
        if slot == "picked":
            if isinstance(value, int) and 0 <= value < len(state.candidates):
                state.picked = state.candidates[value]
            else:
                state.picked = None
        elif slot == "family":
            state.family = str(value or "")
        elif slot == "route_probs":
            state.route_probs = dict(value or {})
        elif slot == "pick_dominant":
            state.pick_dominant = bool(value)
        elif slot == "verify_status":
            state.verify_verdict = str(value) if value is not None else None
            state.verified = state.verify_verdict == "verified"
        elif slot == "verify_prob":
            state.verify_prob = float(value) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------- node kinds


def node_probes(node: Dict[str, Any], state: CaseState, params: Dict[str, Any]) -> None:
    if node["plan"] == "census":
        steps = state.census
        wave = 0
    else:
        wave = int(state.wave)
        steps = [step for step in state.plan if int(step.get("wave") or 1) == wave]
    if not steps:
        state.note("probes", plan=node["plan"], wave=wave, executed=0, why="plan exhausted")
        state.no_more_probes = True
        return
    executed, skipped = [], []
    for step in steps:
        gate = step.get("when")
        if callable(gate) and not gate(state):
            skipped.append({"step": step["name"], "why": f"gate {step['gate']}"})
            continue
        key = step["key"] if step["key"] not in state.obs else f"{step['key']}@{wave}"
        args = step["args"] if isinstance(step["args"], dict) else step["args"](state)
        if args is None:
            skipped.append({"step": step["name"], "why": "args not applicable"})
            continue
        text = state.observe(key, step["tool"], args, purpose=step["purpose"])
        if text.startswith("Error:"):
            # a probe that silently returns an error poisons every downstream rule
            state.notes.append(f"tool_error {step['tool']} {args}: {text[:120]}")
            state.note("probe_error", step=step["name"], tool=step["tool"], arguments=args, error=text[:200])
        executed.append({"key": key, "tool": step["tool"], "arguments": args, "gate": step["gate"]})
    if not executed and wave > 0:
        # A wave can be empty because this step's gate is closed; the plan continues.
        # Only when no later wave has any step left are we truly exhausted.
        later = [step for step in state.plan if int(step.get("wave") or 1) > wave]
        if not later:
            state.no_more_probes = True
    state.note("probes", plan=node["plan"], wave=wave, executed=len(executed),
               steps=executed, skipped=skipped)


def node_extract_anchors(node: Dict[str, Any], state: CaseState) -> Dict[str, Any]:
    state.note("code", id=node["id"], anchors=state.anchors.to_dict())
    return {"anchors": state.anchors}


def node_build_plan(node: Dict[str, Any], state: CaseState, params: Dict[str, Any], resolve) -> Dict[str, Any]:
    family = str(resolve(node["family"]) or "")
    if family not in params["_plans"]:
        raise KeyError(f"{node['id']}: no plans entry for family {family!r}")
    state.plan = build_plan(family, params["_plans"], int(params["steps_per_wave"]))
    state.note("code", id=node["id"], family=family,
               plan=[{"step": step["name"], "gate": step["gate"], "wave": step["wave"]} for step in state.plan])
    return {"plan": state.plan}


def node_derive_candidates(node: Dict[str, Any], state: CaseState, params: Dict[str, Any]) -> Dict[str, Any]:
    raw = derive_rules(state)
    merged = state.add_candidates(raw)
    state.note("code", id=node["id"], raw=len(raw), kept=len(merged),
               top=[cand.to_dict() for cand in merged[: int(params["cands_cap"])]])
    return {"candidates": merged}


def node_escalate(node: Dict[str, Any], state: CaseState, params: Dict[str, Any], resolve,
                  transport_mode: str = "live") -> Optional[Dict[str, Any]]:
    reasons = [reason for reason in node["when"] if _reason(reason, state, params)]
    if state.family == "codedefect" and params.get("judge_on_codedefect", True) and not any(
            cand.root_cause.startswith("code_") and cand.score >= 0.8 for cand in state.candidates):
        reasons.append("codedefect_open_ended")
    if reasons and params.get("escalate", True) and transport_mode != "live":
        # mechanism/answers modes must stay model-free: the judge is a *live* action.
        # Skipping it is recorded, never silently downgraded to a guess.
        state.escalate_reason = ",".join(sorted(set(reasons)))
        state.note("escalate", id=node["id"], fired=True, reasons=sorted(set(reasons)),
                   skipped=f"mode={transport_mode}")
        return {"answer": None}
    if not reasons or not params.get("escalate", True):
        state.note("escalate", id=node["id"], fired=bool(reasons), reasons=reasons,
                   why="disabled" if reasons and not params.get("escalate", True) else "no reason")
        return {"answer": None}
    budget = int(resolve(node["budget"]))
    state.escalated = True
    state.escalate_reason = ",".join(sorted(set(reasons)))
    state.note("escalate", id=node["id"], fired=True, reasons=sorted(set(reasons)), budget=budget)
    answer = escalate(state, agent_model(), budget=budget)
    if answer:
        state.final = answer
    return {"answer": answer}


def _symptom_only(state: CaseState) -> bool:
    """App-level anomaly, no deterministic explanation, inside the SOP's own system."""
    if state.candidates:
        return False
    if state.scope_system and state.system != state.scope_system:
        return False
    if state.family not in {"performance", "service", "runtime"}:
        return False
    return bool(re.search(r"LATENCY_DEGRADATION|ERROR_SPIKE|RESOURCE_SATURATION", state.obs.get("alerts", "")))


def _reason(reason: str, state: CaseState, params: Dict[str, Any]) -> bool:
    if reason == "no_candidates":
        return not state.candidates
    if reason == "not_verified":
        return not state.verified
    if reason == "family_unknown":
        return bool(state.family) and state.family not in params["_plans"]
    if reason == "waves_exhausted":
        return state.wave >= int(params["max_waves"])
    if reason == "conf_low":
        return state.verify_prob is not None and state.verify_prob < float(params["conf_min"])
    if reason == "contradicted":
        return getattr(state, "verify_verdict", None) == "contradicted"
    if reason == "weak_after_waves":
        return state.wave >= int(params["max_waves"]) and not any(cand.score >= 0.8 for cand in state.candidates)
    if reason == "codedefect_open_ended":
        return True
    if reason == "symptom_only":
        return _symptom_only(state)
    if reason == "not_verified":
        return True
    raise KeyError(reason)


def node_normalize_answer(node: Dict[str, Any], state: CaseState, params: Dict[str, Any]) -> Dict[str, Any]:
    candidates = state.candidates[: max(3, int(params["cands_cap"]))]
    first = None
    if state.final:
        first = {"fault_object": state.final["fault_object"], "root_cause": state.final["root_cause"],
                 "summary": state.final.get("key_evidence_summary", "")}
    elif state.picked is not None:
        first = {"fault_object": state.picked.object, "root_cause": state.picked.root_cause,
                 "summary": state.picked.evidence}
    elif candidates:
        first = {"fault_object": candidates[0].object, "root_cause": candidates[0].root_cause,
                 "summary": candidates[0].evidence}

    predictions: List[Dict[str, Any]] = []
    if first:
        predictions.append({"rank": 1, "fault_object": first["fault_object"], "root_cause": first["root_cause"]})
    for cand in candidates:
        if len(predictions) >= 3:
            break
        if first and (cand.object, cand.root_cause) == (first["fault_object"], first["root_cause"]):
            continue
        predictions.append({"rank": len(predictions) + 1, "fault_object": cand.object, "root_cause": cand.root_cause})
    while len(predictions) < 3 and predictions:
        predictions.append({"rank": len(predictions) + 1,
                            "fault_object": predictions[0]["fault_object"],
                            "root_cause": predictions[0]["root_cause"]})
    payload = {"key_evidence_summary": (first or {}).get("summary", "no evidence-grounded hypothesis"),
               "top_3_predictions": predictions}
    state.final = payload
    state.note("code", id=node["id"], predictions=predictions)
    return {"final": payload}


# ---------------------------------------------------------------- driver


def run_case(case: Dict[str, Any], dsl: Dsl, transport: JevTransport, tag: str) -> Dict[str, Any]:
    started = time.perf_counter()
    path = case_path(case["system"], case["category"], case["case"])
    state = CaseState(case_id=case["id"], system=case["system"], category=case["category"],
                      anchors=extract_anchors(case["id"], case["system"], path),
                      tools=SnapshotTools(path, case["system"], case["category"]))
    params = dict(dsl.params)
    params["_plans"] = dsl.plans
    scope_system = str((dsl.sop.get("scope") or {}).get("system") or "")
    state.scope_system = scope_system
    state.census = census_plan(dsl.plans["census"], int(params["steps_per_wave"]))
    bindings: Dict[str, Dict[str, Any]] = {}
    executed: List[str] = []

    def resolve(ref: str) -> Any:
        return make_resolver(state, params, bindings)(ref)

    def run_node(node: Dict[str, Any], wave: int) -> None:
        node_id, kind = node["id"], node["kind"]
        executed.append(f"{node_id}:{kind}")
        if kind == "extract_anchors":
            bindings[node_id] = node_extract_anchors(node, state)
        elif kind == "probes":
            node_probes(node, state, params)
        elif kind == "build_plan":
            bindings[node_id] = node_build_plan(node, state, params, resolve)
        elif kind == "derive_candidates":
            bindings[node_id] = node_derive_candidates(node, state, params)
        elif kind == "decide":
            out = run_decide(node, state, params, transport, bindings, resolve, wave)
            entry = out.pop("_trace")
            state.jev.append(entry)
            state.note("decide", id=node_id, rid=entry["rid"], status=entry["status"],
                       guard=entry.get("guard"), use=entry.get("use"), choice=entry.get("choice"),
                       dominant=entry.get("dominant"), why=entry.get("why"))
            if entry.get("status") == "ans" and entry.get("mode") == "live":
                state.model_calls += 1
                state.tokens["in"] += int((entry.get("usage") or {}).get("input_tokens") or 0)
            bindings[node_id] = out
            apply_state_wiring(dsl, state, resolve)
        elif kind == "escalate":
            bindings[node_id] = node_escalate(node, state, params, resolve, transport.mode) or {"answer": None}
        elif kind == "normalize_answer":
            bindings[node_id] = node_normalize_answer(node, state, params)
        elif kind == "persist_trace":
            state.note("artifact", id=node_id, persisted=False, why="written by the CLI driver")
        else:  # pragma: no cover
            raise RuntimeError(f"unhandled node kind {kind}")

    for node in dsl.flow:
        if node["kind"] != "loop":
            run_node(node, state.wave)
            continue
        max_waves = min(int(resolve(node["max_waves"])), int(params["max_waves"]))
        min_waves = int(resolve(node["min_waves"]))
        for wave in range(1, max_waves + 1):
            state.wave = wave
            for child in node["body"]:
                run_node(child, wave)
                apply_state_wiring(dsl, state, resolve)
            if getattr(state, "no_more_probes", False) and "plan_exhausted" in node["stop_when"]:
                state.note("loop", id=node["id"], stop="plan_exhausted", wave=wave)
                break
            if ("verified_and_model" in node["stop_when"] and state.verified
                    and state.picked is not None and transport.mode != "mechanism" and wave >= min_waves):
                state.note("loop", id=node["id"], stop="verified_and_model", wave=wave)
                break
            if any(entry.get("escalate") for entry in state.jev):
                state.note("loop", id=node["id"], stop="escalate_flag", wave=wave)
                break
        else:
            state.note("loop", id=node["id"], stop="max_waves", wave=state.wave)

    wall = time.perf_counter() - started
    return _build_trace(state, case, dsl, wall, executed, tag, path, bindings)


def _build_trace(state: CaseState, case: Dict[str, Any], dsl: Dsl, wall: float,
                 executed: List[str], tag: str, path: Path,
                 bindings: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    steps = []
    for index, call in enumerate(state.calls):
        steps.append({"step_id": index, "action_type": "tool", "action_name": call["tool"],
                      "action_input": call["arguments"], "observation": state.obs.get(call["key"], ""),
                      "error": call.get("error"), "purpose": call.get("purpose", ""), "tool_latency": None})
    payload = dict(state.final or {"key_evidence_summary": "no hypothesis", "top_3_predictions": []})
    return {
        "case_id": case["id"], "system_name": case["system"], "question": state.anchors.query,
        "case_path": str(path), "max_steps": dsl.params["max_waves"], "current_step": len(steps),
        "finished": True, "stop_reason": "submit",
        "final_answer": json.dumps(payload, ensure_ascii=False),
        "metadata": {"namespace": state.anchors.namespace, "query": state.anchors.query,
                     "fault_category": case["category"], "model_name": f"agentrun-v{dsl.version}"},
        "steps": steps,
        "agentrun": {
            "tag": tag, "dsl": dsl.path, "dsl_version": dsl.version,
            "sop": dsl.sop, "params": dsl.params,
            "nodes_executed": executed,
            "census": [{"step": step["name"], "gate": step["gate"]} for step in state.census],
            "plan": [{"step": step["name"], "gate": step["gate"], "wave": step["wave"]} for step in state.plan],
            "family": state.family,
            "state_wiring": dsl.state,
            "bindings": {key: {k: (v if isinstance(v, (int, float, str, bool, type(None), dict)) else str(v))
                               for k, v in value.items() if not k.startswith("__")}
                         for key, value in bindings.items()},
            "picked": state.picked.to_dict() if state.picked else None,
            "pick_why": getattr(state, "pick_why", ""),
            "verify_prob": state.verify_prob,
            "verify_verdict": getattr(state, "verify_verdict", None),
            "verified": state.verified,
            "escalated": state.escalated, "escalate_reason": state.escalate_reason,
            "escalate_steps": state.escalate_steps,
            "waves": state.wave,
            "candidates": [cand.to_dict() for cand in state.candidates],
            "jev": state.jev, "nodes": state.nodes, "notes": state.notes,
            "tool_calls": len(state.calls), "model_calls": state.model_calls,
            "tokens_in": state.tokens["in"], "tokens_out": state.tokens["out"],
            "wall_s": round(wall, 2),
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsl", default=str(RUNS.parent / "dsl_versions" / "v0.5.dsl.txt"))
    parser.add_argument("--tasks-file", default=str(TASKS_FILE))
    parser.add_argument("--save-root", default=str(RUNS.parent / "traces" / "agentrun_v05_live"))
    parser.add_argument("--tag", default="test")
    parser.add_argument("--jev-mode", choices=["live", "mechanism", "answers"], default="live")
    parser.add_argument("--jev-in", default="")
    parser.add_argument("--jev-req", default="")
    parser.add_argument("--only", default="")
    parser.add_argument("--eval-out", default="")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args(argv)

    dsl = load_dsl(args.dsl)
    for override in args.set:
        key, _, value = override.partition("=")
        key, value = key.strip(), value.strip()
        if key not in dsl.params:
            raise SystemExit(f"unknown param {key!r}")
        template = dsl.params[key]
        if isinstance(template, bool):
            dsl.params[key] = value.lower() in {"1", "true", "yes", "on"}
        elif isinstance(template, int):
            dsl.params[key] = int(value)
        elif isinstance(template, float):
            dsl.params[key] = float(value)
        else:
            dsl.params[key] = value
        print(f"[DSL] override {key}={dsl.params[key]!r}")

    answers = json.loads(Path(args.jev_in).read_text()) if args.jev_in and Path(args.jev_in).is_file() else {}
    transport = JevTransport(mode=args.jev_mode, answers=answers, tag=args.tag)

    cases = json.loads(Path(args.tasks_file).read_text())["cases"]
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        cases = [case for case in cases if case["id"] in wanted]

    save_root = Path(args.save_root)
    print(f"[PLAN] n={len(cases)} dsl={dsl.path} v{dsl.version} jev_mode={args.jev_mode} save_root={save_root}",
          flush=True)
    traces = []
    for index, case in enumerate(cases, 1):
        trace = run_case(case, dsl, transport, args.tag)
        out_dir = save_root / case["system"] / case["category"] / case["case"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{case['case']}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
        traces.append(trace)
        extra = trace["agentrun"]
        picked = extra["picked"] or {}
        print(f"[{index}/{len(cases)}] {case['id']:<24} family={extra['family']:<12} "
              f"tools={extra['tool_calls']} model={extra['model_calls']} esc={int(extra['escalated'])} "
              f"verify={extra['verify_prob']} pick={picked.get('root_cause')} {picked.get('object')} "
              f"{extra['wall_s']}s", flush=True)

    if args.jev_req:
        req_path = Path(args.jev_req)
        req_path.parent.mkdir(parents=True, exist_ok=True)
        with req_path.open("w", encoding="utf-8") as handle:
            for request in transport.requests:
                handle.write(json.dumps(request, ensure_ascii=False) + "\n")
        print(f"[JEV] wrote {len(transport.requests)} requests -> {req_path}")
    if args.eval_out:
        out = Path(args.eval_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"dsl": dsl.path, "version": dsl.version, "params": dsl.params,
                                   "jev_mode": args.jev_mode, "jev_usage": transport.usage(),
                                   "cases": [{"id": trace["case_id"], **{key: trace["agentrun"][key] for key in
                                              ("family", "tool_calls", "model_calls", "tokens_in", "tokens_out",
                                               "wall_s", "escalated", "verified", "verify_prob")}}
                                             for trace in traces]}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        print(f"[OUT] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
