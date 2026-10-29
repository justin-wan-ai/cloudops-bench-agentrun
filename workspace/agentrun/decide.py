"""Generic `decide` executor: the DSL's five sections drive everything.

For a node declared as::

    - id: pick
      do: decide
      ask: ...                 # question text
      space: {...}             # option space (signals | candidates | fixed)
      input: {...}             # payload fields
      pre: [{if: guard -> action}, ...]
      post: {default|option|other|error|unavailable: action, ...}
      out: {picked: choice, ...}

this module does exactly: build options -> build payload -> walk `pre` -> (maybe) ask the
model -> map the answer through `post` -> emit the declared outputs.

There is no branch in here that the DSL cannot see: every mechanical shortcut is a `pre`
rule, every answer outcome is a `post` entry, and every output name comes from `out`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..jev import JevClient, JevConfig, JevError
from .state import Candidate, CaseState

ABSTAIN = "other"


# ---------------------------------------------------------------- transport


class JevTransport:
    """Model transport + answer library. Modes: live | mechanism | answers."""

    def __init__(self, mode: str = "live", answers: Optional[Dict[str, Any]] = None, tag: str = "test"):
        self.mode = mode
        self.tag = tag
        answers = answers or {}
        self.library = {"route": dict(answers.get("route") or {}),
                        "pick": dict(answers.get("pick") or {}),
                        "verify": dict(answers.get("verify") or {})} \
            if any(key in answers for key in ("route", "pick", "verify")) \
            else {"route": {}, "pick": dict(answers), "verify": {}}
        self.client: Optional[JevClient] = None
        if mode == "live":
            self.client = JevClient(JevConfig())
        self.requests: List[Dict[str, Any]] = []

    def available(self) -> bool:
        return self.mode == "live" and self.client is not None

    def stored(self, rid: str, kind: str) -> Any:
        return self.library.get(kind, {}).get(rid)

    def ask(self, rid: str, kind: str, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        """Return (body, error). body None + error '' means "model not available"."""
        self.requests.append({"rid": rid, "kind": kind,
                              "state": payload["state"], "questions": payload["questions"]})
        if not self.available():
            return None, ""
        assert self.client is not None
        try:
            body = self.client.system_one(payload["state"], payload["questions"])
        except JevError as exc:
            return None, str(exc)[:200]
        return body, ""

    def usage(self) -> Dict[str, Any]:
        return self.client.usage() if self.client else {}


# ---------------------------------------------------------------- options


@dataclass
class Option:
    value: Any                 # what gets selected (family / candidate index / verdict)
    label: str                 # what the model sees and answers
    meta: Dict[str, Any] = field(default_factory=dict)
    score: Optional[float] = None
    reason: str = ""


def build_options(node: Dict[str, Any], state: CaseState, params: Dict[str, Any]) -> List[Option]:
    from . import plans

    space = node["space"]
    source = space["from"]
    if source == "signals":
        options: List[Option] = []
        seen = set()
        for entry in space["order"]:
            reason = plans.SIGNALS[entry["when"]](state)
            if not reason or entry["value"] in seen:
                continue
            seen.add(entry["value"])
            options.append(Option(value=entry["value"], label=entry["value"],
                                  meta={"signal": entry["when"], "description":
                                        plans.FAMILY_DESCRIPTIONS.get(entry["value"], "")},
                                  reason=reason))
        return options[:4]

    if source == "candidates":
        limit = int(params["cands_cap"])
        options = []
        for index, cand in enumerate(state.candidates[:limit]):
            options.append(Option(value=index, label=f"c{index}", score=cand.score,
                                  meta={"root_cause": cand.root_cause, "object": cand.object,
                                        "rule": cand.rule, "evidence": cand.evidence},
                                  reason=cand.root_cause))
        return options

    if source == "fixed":
        return [Option(value=name, label=name) for name in space["options"]]

    raise KeyError(f"unknown space source {source!r}")


def _is_dominant(options: List[Option], ratio: float) -> bool:
    if not options:
        return False
    if len(options) == 1:
        return True
    top = options[0].score or 0.0
    second = options[1].score or 0.0
    return top >= ratio * max(second, 1e-6)


# ---------------------------------------------------------------- guards


def evaluate_guard(guard: Tuple[str, Any], state: CaseState, options: List[Option],
                   params: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    kind, payload = guard
    if kind == "named":
        if payload == "no_options":
            return not options
        if payload == "jev_disabled":
            return not bool(ctx["enabled"])
        if payload == "has_stored_answer":
            return ctx.get("stored") is not None
        if payload == "unchanged_since_last":
            return ctx.get("unchanged", False)
        if payload == "no_pick":
            return state.picked is None
        raise KeyError(payload)
    if kind == "cmp":
        op, raw_threshold = payload
        threshold = float(ctx["resolve"](str(raw_threshold)) if str(raw_threshold).startswith("$")
                          else raw_threshold)
        if len(options) < 2:
            first, second = (options[0].score if options else 0.0) or 0.0, 0.0
        else:
            first, second = options[0].score or 0.0, options[1].score or 0.0
        return first >= threshold * max(second, 1e-6) if op == ">=" else first > threshold * max(second, 1e-6)
    if kind == "ref":
        return bool(ctx["resolve"](payload))
    if kind == "ref_eq":
        ref, literal = payload
        return str(ctx["resolve"](ref)) == str(literal)
    raise KeyError(kind)


# ---------------------------------------------------------------- payload


def _render_option(option: Option, fields: List[str]) -> Dict[str, Any]:
    rendered = {"id": option.label}
    if "family" in fields or not fields:
        rendered.setdefault("family", option.label)
    for name in fields:
        if name in option.meta:
            rendered[name] = option.meta[name]
        elif name == "reason":
            rendered["reason"] = option.reason
        elif name == "score" and option.score is not None:
            rendered["score"] = round(option.score, 2)
    return rendered


def _observations(state: CaseState, limit: int = 3, chars: int = 320) -> List[str]:
    snippets: List[str] = []
    wanted = ("pods", "alerts", "describe:", "events", "resourcequota", "services", "endpoints", "errors")
    for key, text in state.obs.items():
        if key.startswith("sig:") or not text or not key.startswith(wanted):
            continue
        snippets.append(f"[{key}] " + " ".join(text.split())[:chars])
        if len(snippets) >= limit:
            break
    return snippets


def build_payload(node: Dict[str, Any], state: CaseState, options: List[Option],
                  resolve) -> Dict[str, Any]:
    space = node["space"]
    rendered = [_render_option(option, space["render"]) for option in options]
    signal_reasons = {option.value: option.reason for option in options} if space["from"] == "signals" else {}

    def value_of(spec: Any) -> Any:
        if isinstance(spec, list):
            return [value_of(item) for item in spec]
        if spec == "space_items":
            return rendered
        if spec == "signal_reasons":
            return signal_reasons
        if spec == "top_observations(3)":
            return _observations(state)
        if spec in {"query", "system", "namespace"}:
            return getattr(state.anchors, spec)
        if spec == "picked_item":
            picked = state.picked
            return ({"root_cause": picked.root_cause, "object": picked.object,
                     "rule": picked.rule, "evidence": picked.evidence} if picked else None)
        return resolve(spec)

    state_block = {key: value_of(spec) for key, spec in node["input"].items()}

    criteria = {option.label: (option.meta.get("description") or option.reason or option.label)
                for option in options}
    if space["other"]:
        criteria[ABSTAIN] = "none of these options fits"
    return {
        "state": state_block,
        "questions": {
            "answer": {
                "type": "choice",
                "instructions": node["ask"],
                "criteria": criteria,
            }
        },
    }


# ---------------------------------------------------------------- executor


def run_decide(node: Dict[str, Any], state: CaseState, params: Dict[str, Any],
               transport: JevTransport, bindings: Dict[str, Dict[str, Any]],
               resolve, wave: int = 0) -> Dict[str, Any]:
    """Execute one decide node; returns {out_name: value, ...} plus `_trace`."""
    node_id = node["id"]
    rid = f"{transport.tag}:{state.case_id}#{node_id}" + (f"{wave}" if wave else "")
    options = build_options(node, state, params)
    ctx = {
        "resolve": resolve,
        "stored": transport.stored(rid, _kind_of(node_id)),
        "unchanged": state.status_signature() == getattr(state, f"last_sig_{node_id}", None),
        "enabled": bool(resolve(node["enabled"])),
    }
    setattr(state, f"last_sig_{node_id}", state.status_signature())

    entry: Dict[str, Any] = {"rid": rid, "kind": _kind_of(node_id), "node": node_id,
                             "mode": transport.mode, "options": [option.label for option in options]}

    # ---- walk the pre table: the first guard that fires decides without the model
    action: Optional[Dict[str, Any]] = None
    fired = ""
    for rule in node["pre"]:
        if evaluate_guard(rule["guard"], state, options, params, ctx):
            action, fired = rule["action"], rule["if"]
            break

    body: Optional[Dict[str, Any]] = None
    error = ""
    if action is None:
        # ---- ask the model (the only branch that spends tokens)
        if not transport.available():
            action, fired, entry["status"] = node["post"].get("unavailable", node["post"]["error"]), "unavailable", "mechanical"
        else:
            payload = build_payload(node, state, options, resolve)
            body, error = transport.ask(rid, _kind_of(node_id), payload)
            if body is None:
                action, fired = node["post"]["error"], f"error: {error}"
                entry["status"] = "error"
                entry["error"] = error

    chosen: Optional[Option] = None
    if action is None:
        answers = (body or {}).get("answers") or {}
        answer = answers.get("answer") or {}
        raw = answer.get("choice")
        entry.update({"raw_answer": raw, "confidence": answer.get("confidence"),
                      "probabilities": answer.get("probabilities") or {},
                      "usage": (body or {}).get("usage")})
        chosen = _match(options, raw)
        if raw == ABSTAIN or chosen is None:
            key = ABSTAIN if raw == ABSTAIN else "other"
            action, fired = node["post"].get(key, node["post"].get("default", node["post"]["error"])), \
                f"answer={raw!r}"
            if raw != ABSTAIN and chosen is None and "other" not in node["post"]:
                entry["status"] = "abstain"
        else:
            action = node["post"].get(chosen.value if isinstance(chosen.value, str) else chosen.label,
                                      node["post"].get("default", node["post"]["error"]))
            fired = f"answer={raw!r}"
        entry.setdefault("status", "ans")

    use = action["use"]
    value = _apply(use, chosen, options, params, ctx, bindings, node_id)
    dominant = action.get("dominant")
    if dominant == "auto":
        dominant_flag = _is_dominant(options, float(params["dominance_ratio"]))
    else:
        dominant_flag = bool(dominant)

    previous = bindings.get(node_id) or {}
    if use == "previous":
        value = previous.get("__value__")
        dominant_flag = bool(previous.get("__dominant__"))
        entry["status"] = entry.get("status") or "mechanical"
    entry.update({"status": entry.get("status", "mechanical"), "guard": fired,
                  "use": use, "choice": value, "why": action.get("why") or fired,
                  "dominant": dominant_flag,
                  "continue_loop": bool(action.get("continue_loop")),
                  "escalate": bool(action.get("escalate"))})

    # `require:` is the only post-model check: it can demote a positive verdict
    if action.get("require") == "beats_alternatives":
        probabilities = entry.get("probabilities") or {}
        supported = float(probabilities.get("supported") or 0.0)
        rivals = max(float(probabilities.get("insufficient") or 0.0),
                     float(probabilities.get("contradicted") or 0.0))
        if supported < rivals or supported < float(params["conf_min"]):
            entry["why"] = "supported but did not beat alternatives"
            value = "unverified"
            entry["require_failed"] = True

    out: Dict[str, Any] = {}
    for name, source in node["out"].items():
        if source == "choice":
            out[name] = value
        elif source == "distribution":
            out[name] = entry.get("probabilities") or {}
        elif source == "reason":
            out[name] = entry["why"]
        elif source == "dominance_flag":
            out[name] = dominant_flag
        elif source == "p_supported":
            probabilities = entry.get("probabilities") or {}
            out[name] = float(probabilities.get("supported") or 0.0) if probabilities else None
    out["_trace"] = entry
    # remembered so `use: previous` can replay the last decision of this node
    out["__value__"] = out.get(next(iter(node["out"]), ""), value) if node["out"] else value
    out["__dominant__"] = dominant_flag
    return out


def _kind_of(node_id: str) -> str:
    for kind in ("route", "pick", "verify"):
        if kind in node_id:
            return kind
    return node_id


def _match(options: List[Option], raw: Any) -> Optional[Option]:
    if raw is None:
        return None
    text = str(raw).strip()
    for option in options:
        if text == option.label or text.lower() == option.label.lower():
            return option
    return None


def _apply(use: Any, chosen: Optional[Option], options: List[Option], params: Dict[str, Any],
           ctx: Dict[str, Any], bindings: Dict[str, Dict[str, Any]], node_id: str) -> Any:
    if isinstance(use, str) and use.startswith("$params."):
        return params[use.split(".", 1)[1]]
    if use == "answer":
        return chosen.value if chosen is not None else None
    if use == "first_option":
        return options[0].value if options else None
    if use == "stored":
        return ctx["stored"]
    if use == "previous":
        return (bindings.get(node_id) or {}).get("_previous")
    if use == "none":
        return None
    return use  # literal (e.g. `verified`, `supported`)
