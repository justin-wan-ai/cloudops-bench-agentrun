"""Runtime state for one AgentRun case: observations, candidates, decisions.

``CaseState.observe`` is the only way to get evidence.  It caches by
``(tool, arguments)`` so a repeated probe is a cache hit, not a duplicate tool
call -- this is what keeps the redundant-action rate at 0 by construction.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .anchors import Anchors
from .benchbridge import SnapshotTools


@dataclass
class Candidate:
    root_cause: str
    object: str
    score: float
    evidence: str
    rule: str

    @property
    def key(self) -> str:
        return f"{self.object}|{self.root_cause}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "root_cause": self.root_cause,
            "object": self.object,
            "score": round(self.score, 3),
            "evidence": self.evidence,
            "rule": self.rule,
        }


@dataclass
class CaseState:
    case_id: str
    system: str
    category: str
    anchors: Anchors
    tools: SnapshotTools

    obs: Dict[str, str] = field(default_factory=dict)
    obs_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    census: List[Dict[str, Any]] = field(default_factory=list)
    scope_system: str = ""          # DSL `scope.system`: rules keyed on system-specific
                                    # vocabularies (alert formats, entry service) must respect it
    plan: List[Dict[str, Any]] = field(default_factory=list)
    plan_cursor: int = 0
    wave: int = 0
    jev: List[Dict[str, Any]] = field(default_factory=list)
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    family: str = ""
    route_probs: Dict[str, float] = field(default_factory=dict)
    picked: Optional[Candidate] = None
    pick_why: str = ""
    verify_prob: Optional[float] = None
    verified: bool = False
    escalated: bool = False
    escalate_reason: str = ""
    escalate_steps: int = 0
    final: Optional[Dict[str, Any]] = None
    notes: List[str] = field(default_factory=list)
    tokens: Dict[str, int] = field(default_factory=lambda: {"in": 0, "out": 0})
    model_calls: int = 0

    # ------------------------------------------------------------ tool access

    def _signature(self, tool: str, args: Dict[str, Any]) -> str:
        return f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"

    def observe(self, key: str, tool: str, args: Dict[str, Any], purpose: str = "") -> str:
        signature = self._signature(tool, args)
        cache_key = f"sig:{signature}"
        if cache_key in self.obs:
            self.obs[key] = self.obs[cache_key]
            return self.obs[cache_key]

        result = self.tools.call(tool, args)
        text = str(result.get("observation") or "") if result.get("success") else ""
        error = result.get("error")
        self.calls.append(
            {
                "key": key,
                "tool": tool,
                "arguments": args,
                "purpose": purpose,
                "ok": bool(result.get("success")),
                "error": error,
                "observation_chars": len(text),
            }
        )
        self.obs[key] = text
        self.obs[cache_key] = text
        self.obs_meta[key] = {"tool": tool, "arguments": args, "purpose": purpose, "error": error}
        return text

    # ------------------------------------------------------------ bookkeeping

    def note(self, node: str, **payload: Any) -> None:
        self.nodes.append({"node": node, **payload})

    def add_candidates(self, items: List[Candidate]) -> List[Candidate]:
        merged: Dict[str, Candidate] = {cand.key: cand for cand in self.candidates}
        for cand in items:
            existing = merged.get(cand.key)
            if existing is None or cand.score > existing.score:
                merged[cand.key] = cand
        self.candidates = sorted(merged.values(), key=lambda c: (-c.score, c.root_cause))
        return self.candidates

    def status_signature(self) -> str:
        digest = hashlib.sha256(
            json.dumps([self.family, [c.to_dict() for c in self.candidates]], sort_keys=True).encode()
        ).hexdigest()[:12]
        return digest

    def has(self, key: str) -> bool:
        return bool(self.obs.get(key))

    def get(self, key: str) -> str:
        return self.obs.get(key, "")


def resolve_args(spec: Any, state: CaseState) -> Optional[Dict[str, Any]]:
    """Plan step arguments are either a dict or a callable(state) -> dict|None."""
    if callable(spec):
        try:
            return spec(state) or None
        except Exception:
            return None
    return dict(spec or {}) or None


def as_callable(value: Any) -> Callable[[CaseState], Optional[Dict[str, Any]]]:
    return value if callable(value) else (lambda _state, _value=value: dict(_value or {}))
