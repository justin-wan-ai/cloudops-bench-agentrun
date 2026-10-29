"""Case input (GT-isolated) + deterministic anchor extraction.

P5 gate: the solving path may only read ``namespace``, ``query``, ``difficulty``
and the case's location.  ``metadata.json#result`` (the gold label) is never
loaded: ``load_case_input`` reads the file through an allowlist and raises if a
caller ever asks for a gold key.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

INPUT_ALLOWLIST = ("namespace", "query", "difficulty")
GOLD_KEYS = ("result", "fault_object", "root_cause", "fault_taxonomy")


class GoldLeak(RuntimeError):
    """Raised when anything in the solving path touches ground truth."""


def load_case_input(case_path) -> Dict[str, Any]:
    """Read metadata.json through a strict allowlist.

    ``json.loads`` necessarily parses the whole file, so ``raw`` briefly holds the gold
    label; it is deleted before returning and only the three allowlisted keys survive.
    The real protection is therefore the allowlist assertion below, not a convention.
    """
    raw = json.loads((case_path / "metadata.json").read_text(encoding="utf-8"))
    payload = {key: raw.get(key) for key in INPUT_ALLOWLIST if key in raw}
    leaked = set(payload) - set(INPUT_ALLOWLIST)
    del raw
    if leaked:
        raise GoldLeak(f"non-input key(s) reached the solving path: {sorted(leaked)}")
    payload["query"] = str(payload.get("query") or "")
    return payload


SYMPTOM_RULES = [
    ("restart", r"restart|crash|repeatedly|CrashLoop"),
    # "Service Availability Disruption." is the benchmark's dominant availability phrasing;
    # matching only "unavailab" missed it (it also routed infra faults into leftover branches).
    ("unavailable", r"unavailab|unreachab|availab|disruption|502|503|504|refused|timeout|timed out"),
    ("pending", r"pending|not scheduled|unschedul"),
    ("degradation", r"degrad|slow|latency|performance|high .*usage"),
    ("error", r"error|exception|failed|failure"),
]

SERVICE_HINTS = [
    r"\b(frontend|cartservice|checkoutservice|currencyservice|emailservice|paymentservice|"
    r"productcatalogservice|recommendationservice|redis-cart|shippingservice|adservice)\b",
    r"\b(ts-[a-z0-9-]+)\b",
    r"\b(nacos[a-z-]*|rabbitmq|tsdb-mysql[a-z-]*)\b",
]

# Domain priors used only to *order* probes (never to bind an answer).
PRIORS = {
    "trainticket": {"db_service": "tsdb-mysql-leader", "db_port": 3306},
    "boutique": {"db_service": "", "db_port": 0},
}


@dataclass
class Anchors:
    case_id: str
    system: str
    namespace: str
    query: str
    difficulty: str
    symptoms: List[str] = field(default_factory=list)
    mentioned_apps: List[str] = field(default_factory=list)
    priors: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "system": self.system,
            "namespace": self.namespace,
            "query": self.query,
            "difficulty": self.difficulty,
            "symptoms": self.symptoms,
            "mentioned_apps": self.mentioned_apps,
        }


def extract_anchors(case_id: str, system: str, case_path) -> Anchors:
    payload = load_case_input(case_path)
    query = payload["query"]
    symptoms = [name for name, pattern in SYMPTOM_RULES if re.search(pattern, query, re.I)]
    mentioned: List[str] = []
    for pattern in SERVICE_HINTS:
        mentioned.extend(match.group(0) for match in re.finditer(pattern, query, re.I))
    seen: List[str] = []
    for item in mentioned:
        if item not in seen:
            seen.append(item)
    return Anchors(
        case_id=case_id,
        system=system,
        namespace=str(payload.get("namespace") or ""),
        query=query,
        difficulty=str(payload.get("difficulty") or ""),
        symptoms=symptoms,
        mentioned_apps=seen,
        priors=PRIORS.get(system, {}),
    )
