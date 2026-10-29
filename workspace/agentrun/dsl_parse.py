"""DSL v0.5 -> compiled workflow.  Five-section decision nodes, explicit wiring.

Design rules (approved):
  * YAML, no custom parser;
  * every `decide` node declares exactly five things:
        ask+space   what is asked, and the option space (source / order / render / other)
        input       which state fields enter the payload (field by field)
        pre         ordered mechanical guards -> action  (decisions taken WITHOUT the model)
        post        exhaustive model-answer -> action map (decisions taken ABOUT the model)
        out         named outputs, bound for downstream `$id.field` references
  * `$id.field` / `state.slot` / `$params.key` references make data flow explicit;
  * only one numeric comparison is allowed: `top_over_second >= N`;
  * everything referenced by name (guard / action / probe / gate / signal) is validated
    at load time; nothing may fall back to a hidden default.

`state:` wires decision outputs to the runtime slots that the rule engine reads, so no
wiring is hidden inside Python.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import yaml

NODE_KINDS = {"extract_anchors", "probes", "build_plan", "derive_candidates", "decide",
              "loop", "escalate", "normalize_answer", "persist_trace"}

USE_ACTIONS = {"answer", "first_option", "stored", "previous", "none"}
STATE_SLOTS = {
    "family": str,
    "route_probs": dict,
    "picked": "index",
    "pick_dominant": bool,
    "verify_status": str,
    "verify_prob": "float|none",
}
STOP_CONDITIONS = {"plan_exhausted", "verified_and_model"}
ESCALATE_REASONS = {"no_candidates", "not_verified", "family_unknown", "waves_exhausted",
                    "conf_low", "contradicted", "weak_after_waves", "codedefect_open_ended",
                    "symptom_only"}
OUT_SOURCES = {"choice", "distribution", "reason", "dominance_flag", "p_supported"}
REQUIRES = {"beats_alternatives"}
# Bare names in `input:` mean "this anchor field" -- the only free-text input there is.
ANCHOR_FIELDS = {"query", "system", "namespace"}
INPUT_TOKENS = {"space_items", "signal_reasons", "top_observations(3)", "picked_item"}

SCALAR_PARAMS = {"cands_cap": int, "max_waves": int, "min_waves": int, "steps_per_wave": int,
                 "escalate_budget": int, "dominance_ratio": float, "conf_min": float}
BOOL_PARAMS = ("escalate", "judge_on_codedefect", "jev_route", "jev_pick", "jev_verify")

SOP_FIELDS = ("sop", "scope", "owner", "approved_by", "reviewed_at", "change_ticket", "boundaries")
DEFAULT_PARAMS: Dict[str, Any] = {
    "cands_cap": 8, "max_waves": 5, "min_waves": 1, "steps_per_wave": 1,
    "dominance_ratio": 1.5, "conf_min": 0.0, "escalate": True, "escalate_budget": 8,
    "judge_on_codedefect": True, "jev_route": True, "jev_pick": True, "jev_verify": True,
    "route_fallback": "runtime",
}

_REF_RE = re.compile(r"^\$(?P<node>[A-Za-z_][\w]*)\.(?P<field>[A-Za-z_][\w]*)$")
_STATE_RE = re.compile(r"^state\.(?P<slot>[a-z_]+)$")
_PARAM_RE = re.compile(r"^\$params\.(?P<key>[a-z_]+)$")
_GUARD_CMP = re.compile(r"^top_over_second\s*(>=|>)\s*(\$params\.[a-z_]+|\d+(?:\.\d+)?)$")
_GUARD_REF_CMP = re.compile(r"^\$(?P<node>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)\s*==\s*(?P<value>[\w.-]+)$")
NAMED_GUARDS = {"no_options", "jev_disabled", "has_stored_answer", "unchanged_since_last", "no_pick"}


class DslError(ValueError):
    pass


@dataclass
class Dsl:
    path: str
    version: str
    params: Dict[str, Any]
    state: Dict[str, str]
    plans: Dict[str, List[Dict[str, Any]]]
    flow: List[Dict[str, Any]]
    sop: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def decide_nodes(self) -> List[Dict[str, Any]]:
        out = [node for node in self.flow if node["kind"] == "decide"]
        for node in self.flow:
            if node["kind"] == "loop":
                out.extend(child for child in node["body"] if child["kind"] == "decide")
        return out

    def node_ids(self) -> List[str]:
        out: List[str] = []
        for node in self.flow:
            out.append(node["id"])
            if node["kind"] == "loop":
                out.extend(child["id"] for child in node["body"])
        return out


# ---------------------------------------------------------------- refs / guards


def check_ref(value: str, available: Mapping[str, set], where: str) -> str:
    """Validate one reference-ish string; return its normalised form."""
    if not isinstance(value, str):
        raise DslError(f"{where}: reference must be a string, got {value!r}")
    if _PARAM_RE.match(value):
        return value
    if _STATE_RE.match(value):
        slot = _STATE_RE.match(value).group("slot")  # type: ignore[union-attr]
        if slot not in STATE_SLOTS:
            raise DslError(f"{where}: unknown state slot {value!r} (known: {sorted(STATE_SLOTS)})")
        return value
    if _REF_RE.match(value):
        node, field_name = _REF_RE.match(value).group("node", "field")  # type: ignore[union-attr]
        if node not in available:
            raise DslError(f"{where}: unknown node reference {value!r} (known earlier ids: {sorted(available)})")
        if field_name not in available[node]:
            raise DslError(f"{where}: node {node!r} has no output {field_name!r} (has {sorted(available[node])})")
        return value
    raise DslError(f"{where}: {value!r} is not a reference ($node.field / state.slot / $params.key)")


def check_guard(expr: str, available: Mapping[str, set], where: str) -> Optional[Tuple[str, Any]]:
    """Return ('named', name) | ('cmp', (op, n)) | ('ref', value) | ('ref_eq', (value, literal))."""
    if expr in NAMED_GUARDS:
        return ("named", expr)
    match = _GUARD_CMP.match(expr)
    if match:
        raw = match.group(2)
        threshold: Any = raw if raw.startswith("$") else float(raw)
        return ("cmp", (match.group(1), threshold))
    match = _GUARD_REF_CMP.match(expr)
    if match:
        ref = f"${match.group('node')}.{match.group('field')}"
        check_ref(ref, available, where)
        return ("ref_eq", (ref, match.group("value")))
    if expr.startswith("$"):
        return ("ref", check_ref(expr, available, where))
    raise DslError(f"{where}: unknown guard {expr!r}. Allowed: {sorted(NAMED_GUARDS)}, "
                   "'top_over_second >= N', '$node.field', '$node.field == literal'")


def check_action(action: Any, available: Mapping[str, set], where: str) -> Dict[str, Any]:
    if not isinstance(action, dict) or "use" not in action:
        raise DslError(f"{where}: action must be a mapping with `use:`")
    use = action["use"]
    if isinstance(use, str) and use.startswith("$"):
        check_ref(use, available, f"{where}.use")
    elif use not in USE_ACTIONS:
        # a literal value (e.g. `verified`, `supported`) is allowed and stays verbatim
        if not isinstance(use, str) or not re.fullmatch(r"[A-Za-z][\w-]*", use):
            raise DslError(f"{where}: unknown use {use!r}; allowed actions {sorted(USE_ACTIONS)} or a literal")
    extra = set(action) - {"use", "why", "dominant", "continue_loop", "escalate", "require"}
    if extra:
        raise DslError(f"{where}: unknown action keys {sorted(extra)}")
    if "dominant" in action and action["dominant"] not in {"true", "auto", True, False}:
        raise DslError(f"{where}: dominant must be true|auto")
    if "require" in action and action["require"] not in REQUIRES:
        raise DslError(f"{where}: unknown require {action['require']!r}; known {sorted(REQUIRES)}")
    return dict(action)


# ---------------------------------------------------------------- parsing


def _step_list(raw: Any, where: str) -> List[Dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise DslError(f"{where} must be a non-empty list of {probe, gate} steps")
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"step": item, "gate": ["always"]})
        elif isinstance(item, dict) and "step" in item:
            gate = item.get("gate") or "always"
            gates = [str(gate)] if isinstance(gate, str) else [str(name) for name in gate]
            if not gates:
                raise DslError(f"{where}: empty gate list")
            out.append({"step": str(item["step"]), "gate": gates})
        else:
            raise DslError(f"{where}: bad step {item!r}")
    return out


def _parse_space(raw: Any, where: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise DslError(f"{where}.space must be a mapping")
    source = str(raw.get("from") or "")
    space: Dict[str, Any] = {"from": source, "other": bool(raw.get("other", True)),
                             "render": list(raw.get("render") or [])}
    if source == "signals":
        order = raw.get("order")
        if not isinstance(order, list) or not order:
            raise DslError(f"{where}.space.order is required for from: signals")
        entries = []
        for item in order:
            if isinstance(item, dict) and "when" in item and "value" in item:
                entries.append({"when": str(item["when"]), "value": str(item["value"])})
            else:
                raise DslError(f"{where}.space.order entries need {when, value}, got {item!r}")
        space["order"] = entries
    elif source == "candidates":
        space["limit"] = str(raw.get("limit") or "$params.cands_cap")
    elif source == "fixed":
        options = raw.get("options")
        if not isinstance(options, list) or not options:
            raise DslError(f"{where}.space.options is required for from: fixed")
        space["options"] = [str(option) for option in options]
    else:
        raise DslError(f"{where}.space.from must be signals|candidates|fixed, got {source!r}")
    return space


def _parse_flow(raw: Any, available: Dict[str, set], params: Dict[str, Any], path: str) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise DslError("flow must be a non-empty list")
    nodes: List[Dict[str, Any]] = []
    for item in raw:
        node = _parse_node(item, available, params, path)
        nodes.append(node)
    return nodes


def _parse_node(item: Any, available: Dict[str, set], params: Dict[str, Any], path: str) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise DslError(f"flow entries must be mappings, got {item!r}")
    node_id = str(item.get("id") or "")
    kind = str(item.get("do") or "")
    if not node_id or node_id in available:
        raise DslError(f"node id {node_id!r} is missing or duplicated")
    if kind not in NODE_KINDS:
        raise DslError(f"{node_id}: unknown do {kind!r}; known {sorted(NODE_KINDS)}")
    where = f"{path}:{node_id}"

    if kind == "decide":
        node = _parse_decide(item, node_id, available, where, params)
    elif kind == "probes":
        plan = str(item.get("plan") or "")
        if plan not in {"census", "family"}:
            raise DslError(f"{where}: probes.plan must be census|family")
        wave = item.get("wave", 0 if plan == "census" else "current")
        node = {"id": node_id, "kind": kind, "plan": plan, "wave": wave, "out": {}}
    elif kind == "build_plan":
        family = str(item.get("family") or "")
        check_ref(family, available, f"{where}.family")
        node = {"id": node_id, "kind": kind, "family": family,
                "out": {str(k): str(v) for k, v in (item.get("out") or {}).items()}}
    elif kind == "derive_candidates":
        node = {"id": node_id, "kind": kind,
                "out": {str(k): str(v) for k, v in (item.get("out") or {}).items()}}
    elif kind == "extract_anchors":
        node = {"id": node_id, "kind": kind,
                "out": {str(k): str(v) for k, v in (item.get("out") or {}).items()}}
    elif kind == "normalize_answer":
        node = {"id": node_id, "kind": kind,
                "out": {str(k): str(v) for k, v in (item.get("out") or {}).items()}}
    elif kind == "persist_trace":
        node = {"id": node_id, "kind": kind, "out": {}}
    elif kind == "escalate":
        reasons = [str(x) for x in (item.get("when") or [])]
        unknown = [reason for reason in reasons if reason not in ESCALATE_REASONS]
        if unknown:
            raise DslError(f"{where}: unknown escalate reason(s) {unknown}; known {sorted(ESCALATE_REASONS)}")
        if not reasons:
            raise DslError(f"{where}: escalate.when must list at least one reason")
        node = {"id": node_id, "kind": kind, "when": reasons,
                "to": str(item.get("to") or "judge_agent"),
                "budget": str(item.get("budget") or "$params.escalate_budget"),
                "out": {str(k): str(v) for k, v in (item.get("out") or {}).items()}}
    else:  # loop
        body = _parse_flow(item.get("body"), available, params, f"{where}.body")
        stop = [str(x) for x in (item.get("stop_when") or [])]
        unknown = [cond for cond in stop if cond not in STOP_CONDITIONS]
        if unknown:
            raise DslError(f"{where}: unknown stop_when {unknown}; known {sorted(STOP_CONDITIONS)}")
        node = {"id": node_id, "kind": kind, "body": body, "stop_when": stop,
                "max_waves": str(item.get("max_waves") or "$params.max_waves"),
                "min_waves": str(item.get("min_waves") or "$params.min_waves")}

    # register outputs so later nodes may reference them
    available[node_id] = set(node.get("out") or {})
    return node


def _parse_decide(item: Any, node_id: str, available: Dict[str, set], where: str,
                  params: Dict[str, Any]) -> Dict[str, Any]:
    ask = item.get("ask")
    if not isinstance(ask, str) or not ask.strip():
        raise DslError(f"{where}: decide.ask is required (the question sent to the model)")
    intent = item.get("intent")
    refute_if = item.get("refute_if")
    for name, value in (("intent", intent), ("refute_if", refute_if)):
        if not isinstance(value, str) or not value.strip():
            raise DslError(f"{where}: decide.{name} is required -- the reviewer needs the purpose and "
                           "the falsification condition next to the decision table")
    space = _parse_space(item.get("space"), where)

    # validate signals used by the option space
    if space["from"] == "signals":
        from . import plans  # local import to avoid an import cycle
        for entry in space["order"]:
            if entry["when"] not in plans.SIGNALS:
                raise DslError(f"{where}.space.order: unknown signal {entry['when']!r}; "
                               f"known {sorted(plans.SIGNALS)}")
            family = entry["value"]
            if family not in plans.FAMILY_DESCRIPTIONS:
                raise DslError(f"{where}.space.order: unknown family {family!r}")
        space["signals"] = [entry["when"] for entry in space["order"]]

    raw_input = item.get("input")
    if not isinstance(raw_input, dict) or not raw_input:
        raise DslError(f"{where}: decide.input is required (field-by-field payload mapping)")
    payload_input: Dict[str, Any] = {}
    for key, spec in raw_input.items():
        if isinstance(spec, list):
            fields = []
            for value in spec:
                value = str(value)
                if value in INPUT_TOKENS or value in ANCHOR_FIELDS:
                    fields.append(value)
                else:
                    fields.append(check_ref(value, available, f"{where}.input.{key}"))
            payload_input[str(key)] = fields
        elif isinstance(spec, str):
            payload_input[str(key)] = spec if spec in INPUT_TOKENS or spec in ANCHOR_FIELDS else check_ref(
                spec, available, f"{where}.input.{key}")
        else:
            raise DslError(f"{where}.input.{key}: bad spec {spec!r}")
    if space["from"] == "candidates" and "space_items" not in str(payload_input):
        raise DslError(f"{where}: candidates space needs `space_items` in input")
    if space["from"] == "signals" and "signal_reasons" not in str(payload_input):
        raise DslError(f"{where}: signals space needs `signal_reasons` in input")

    pre_raw = item.get("pre") or []
    if not isinstance(pre_raw, list):
        raise DslError(f"{where}.pre must be a list")
    pre = []
    for index, rule in enumerate(pre_raw):
        if not isinstance(rule, dict) or "if" not in rule or "then" not in rule:
            raise DslError(f"{where}.pre[{index}] must be {{if: <guard>, then: <action>}}")
        guard = check_guard(str(rule["if"]), available, f"{where}.pre[{index}]")
        action = check_action(rule["then"], available, f"{where}.pre[{index}]")
        pre.append({"if": str(rule["if"]), "guard": guard, "action": action})

    post_raw = item.get("post")
    if not isinstance(post_raw, dict) or not post_raw:
        raise DslError(f"{where}: decide.post is required (exhaustive answer -> action map)")
    post = {}
    for key, action in post_raw.items():
        post[str(key)] = check_action(action, available, f"{where}.post.{key}")
    if "error" not in post:
        raise DslError(f"{where}: post.error is required (what happens when the model call fails)")
    if space["from"] == "fixed":
        missing = [opt for opt in space["options"] if opt not in post]
        if missing and "default" not in post:
            raise DslError(f"{where}: post must map every fixed option {space['options']} (missing {missing}) "
                           "or provide `default:`")
    else:
        if "default" not in post:
            raise DslError(f"{where}: post.default is required for a dynamic option space")
    if space["other"] and "other" not in post:
        raise DslError(f"{where}: space.other is true, so post.other is required (explicit abstain handling)")

    enabled = str(item.get("enabled") or f"$params.jev_{_kind_of(node_id)}")
    if not _PARAM_RE.match(enabled):
        raise DslError(f"{where}.enabled must be a $params.* reference, got {enabled!r}")

    out_raw = item.get("out")
    if not isinstance(out_raw, dict) or not out_raw:
        raise DslError(f"{where}: decide.out is required (named outputs for downstream references)")
    out = {}
    for key, source in out_raw.items():
        source = str(source)
        if source not in OUT_SOURCES:
            raise DslError(f"{where}.out.{key}: unknown source {source!r}; known {sorted(OUT_SOURCES)}")
        out[str(key)] = source

    return {"id": node_id, "kind": "decide", "ask": ask.strip(),
            "intent": intent.strip(), "refute_if": refute_if.strip(),
            "space": space, "input": payload_input, "pre": pre, "post": post,
            "out": out, "enabled": enabled}


def _parse_state(raw: Any, available: Dict[str, set]) -> Dict[str, str]:
    if not isinstance(raw, dict) or not raw:
        raise DslError("state: wiring block is required (slot -> $node.field)")
    wiring: Dict[str, str] = {}
    for slot, ref in raw.items():
        slot = str(slot)
        if slot not in STATE_SLOTS:
            raise DslError(f"state: unknown slot {slot!r}; known {sorted(STATE_SLOTS)}")
        wiring[slot] = check_ref(str(ref), available, f"state.{slot}")
    return wiring


def load_dsl(path: Path | str) -> Dsl:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DslError("DSL must be a mapping")

    params = dict(DEFAULT_PARAMS)
    params.update({key: value for key, value in (data.get("params") or {}).items()})
    for key, coerce in SCALAR_PARAMS.items():
        if key in params:
            try:
                params[key] = coerce(params[key])
            except (TypeError, ValueError) as exc:
                raise DslError(f"params.{key} must be {coerce.__name__}: {exc}") from exc
    for key in BOOL_PARAMS:
        if key in params:
            params[key] = str(params[key]).strip().lower() in {"1", "true", "yes", "on"}

    plans_raw = data.get("plans")
    if not isinstance(plans_raw, dict) or not plans_raw:
        raise DslError("plans: block is required (census + one entry per family)")
    from . import plans as plan_registry

    plans: Dict[str, List[Dict[str, Any]]] = {}
    for name, steps in plans_raw.items():
        name = str(name)
        plans[name] = _step_list(steps, f"plans.{name}")
        try:
            plan_registry.compile_steps(plans[name], 1, f"plans.{name}")
        except KeyError as exc:
            raise DslError(str(exc).strip('"')) from exc
    unknown = [name for name in plans if name != "census" and name not in plan_registry.FAMILY_DESCRIPTIONS]
    if unknown:
        raise DslError(f"plans: unknown families {unknown}; known {sorted(plan_registry.FAMILY_DESCRIPTIONS)}")
    if "census" not in plans:
        raise DslError("plans.census is required")

    available: Dict[str, set] = {}
    flow = _parse_flow(data.get("flow"), available, params, str(path))
    # `state:` may reference loop-body nodes, so validate after the flow is parsed
    state = _parse_state(data.get("state"), available)

    families_used = {entry["value"] for node in _iter_decide(flow) for entry in node["space"].get("order", [])}
    missing = [family for family in families_used if family not in plans]
    if missing:
        raise DslError(f"decide.space references famil(ies) {missing} that have no plans entry")
    fallback = str(params.get("route_fallback"))
    if fallback not in plans:
        raise DslError(f"params.route_fallback {fallback!r} has no plans entry")
    for node in _iter_decide(flow):
        for rule in node["pre"]:
            action = rule["action"]
            if isinstance(action.get("use"), str) and _PARAM_RE.match(action["use"]):
                key = _PARAM_RE.match(action["use"]).group("key")  # type: ignore[union-attr]
                if key not in params:
                    raise DslError(f"{node['id']}.pre: unknown param {key!r}")
    sop = _parse_sop(data, path)
    return Dsl(path=str(path), version=str(data.get("version") or "0"), params=params,
               state=state, plans=plans, flow=flow, sop=sop, raw=data)


def _kind_of(node_id: str) -> str:
    for kind in ("route", "pick", "verify"):
        if kind in node_id:
            return kind
    return node_id


def _parse_sop(data: Dict[str, Any], path: Path) -> Dict[str, Any]:
    missing = [field_name for field_name in SOP_FIELDS if field_name not in data]
    if missing:
        raise DslError(f"{path}: missing SOP front-matter {missing}. This file is meant to be reviewed "
                       "and signed off by humans, so ownership/rationale cannot be optional.")
    scope = data["scope"]
    if not isinstance(scope, dict) or "system" not in scope:
        raise DslError(f"{path}: scope must be a mapping with at least {{system: ...}}")
    approved = data["approved_by"]
    if not isinstance(approved, list) or not approved:
        raise DslError(f"{path}: approved_by must list at least one reviewer")
    if not str(data["owner"]).strip():
        raise DslError(f"{path}: owner is required")
    if not str(data["boundaries"]).strip():
        raise DslError(f"{path}: boundaries is required -- an SOP must say what it does NOT cover")
    sop = {}
    for field_name in SOP_FIELDS:
        value = data[field_name]
        if hasattr(value, "isoformat"):        # YAML turns 2026-10-23 into datetime.date
            value = value.isoformat()
        sop[field_name] = value
    return sop


def _iter_decide(flow: List[Dict[str, Any]]):
    for node in flow:
        if node["kind"] == "decide":
            yield node
        elif node["kind"] == "loop":
            yield from _iter_decide(node["body"])
