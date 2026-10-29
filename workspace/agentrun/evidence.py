"""Deterministic evidence parsers for Cloud-OpsBench snapshot observations.

Everything here is a pure function over an observation string: no model, no
network, no ground truth.  These are the ``code`` nodes' workhorses, and they are
what makes the AgentRun path replayable.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------- kubectl tables


_COL_RE = re.compile(r"\S+")


def parse_table(text: str) -> List[Dict[str, str]]:
    """Parse fixed-width kubectl output into row dicts keyed by header name."""
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    header_index = next((i for i, line in enumerate(lines) if line.split()[:1] == ["NAME"]), None)
    if header_index is None:
        return []
    header = lines[header_index]
    spans = [(match.group(0), match.start()) for match in _COL_RE.finditer(header)]
    rows: List[Dict[str, str]] = []
    for line in lines[header_index + 1:]:
        if line.lstrip().startswith("Error") or line.split()[:1] == ["NAME"]:
            continue
        row: Dict[str, str] = {}
        for index, (column, start) in enumerate(spans):
            end = spans[index + 1][1] if index + 1 < len(spans) else len(line)
            row[column] = line[start:end].strip()
        if row.get("NAME"):
            rows.append(row)
    return rows


def parse_pods(text: str) -> List[Dict[str, Any]]:
    pods = []
    for row in parse_table(text):
        ready = row.get("READY", "")
        ready_ok, ready_total = (None, None)
        match = re.fullmatch(r"(\d+)/(\d+)", ready.replace(" ", ""))
        if match:
            ready_ok, ready_total = int(match.group(1)), int(match.group(2))
        pods.append(
            {
                "name": row.get("NAME", ""),
                "ready": ready,
                "ready_ok": ready_ok,
                "ready_total": ready_total,
                "status": row.get("STATUS", ""),
                "restarts": row.get("RESTARTS", ""),
                "age": row.get("AGE", ""),
                "app": owner_app(row.get("NAME", "")),
            }
        )
    return pods


def parse_deployments(text: str) -> List[Dict[str, Any]]:
    deployments = []
    for row in parse_table(text):
        ready = row.get("READY", "")
        match = re.fullmatch(r"(\d+)/(\d+)", ready.replace(" ", ""))
        deployments.append(
            {
                "name": row.get("NAME", ""),
                "ready": ready,
                "ready_ok": int(match.group(1)) if match else None,
                "ready_total": int(match.group(2)) if match else None,
                "up_to_date": row.get("UP-TO-DATE", ""),
                "available": row.get("AVAILABLE", ""),
            }
        )
    return deployments


def owner_app(pod_name: str) -> str:
    """``checkoutservice-74fc7d4686-9vlrn`` -> ``checkoutservice``."""
    name = str(pod_name or "").strip()
    if not name:
        return ""
    parts = name.split("-")
    if len(parts) >= 3 and re.fullmatch(r"[a-z0-9]{6,10}", parts[-2]) and re.fullmatch(r"[a-z0-9]{4,6}", parts[-1]):
        return "-".join(parts[:-2])
    if len(parts) >= 2 and re.fullmatch(r"[a-z0-9]{5,10}", parts[-1]):
        return "-".join(parts[:-1])
    return name


# ---------------------------------------------------------------- alerts


def parse_alerts(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {"status": "unknown", "alerts": []}
    data: Any = None
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = ast.literal_eval(raw)
        except Exception:
            data = None
    if isinstance(data, dict):
        alerts = data.get("alerts")
        if isinstance(alerts, list):
            return {"status": str(data.get("status") or "unknown"), "alerts": alerts}
        # boutique sometimes returns a bare anomaly object
        return {"status": "has_anomalies", "alerts": [data]}
    if isinstance(data, list):
        return {"status": "has_anomalies", "alerts": data}
    if re.search(r"No (active )?(metric )?(anomal|error)", raw, re.I):
        return {"status": "clean", "alerts": []}
    return {"status": "text", "alerts": [], "text": raw[:400]}


# ---------------------------------------------------------------- describe output


def _section(text: str, start: str, stops: tuple[str, ...]) -> str:
    index = text.find(start)
    if index < 0:
        return ""
    end = len(text)
    for stop in stops:
        position = text.find(stop, index + len(start))
        if position >= 0:
            end = min(end, position)
    return text[index:end]


def parse_events(text: str) -> List[Dict[str, str]]:
    block = _section(text, "Events:", ())
    events: List[Dict[str, str]] = []
    for line in block.splitlines()[1:]:
        match = re.match(r"\s+(Normal|Warning)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)$", line)
        if not match:
            continue
        events.append(
            {
                "type": match.group(1),
                "reason": match.group(2),
                "age": match.group(3),
                "from": match.group(4),
                "message": match.group(5).strip(),
            }
        )
    return events


_PROBE_RE = re.compile(r"(Liveness|Readiness):\s+(\S+)\s+(.*)$", re.MULTILINE)


def parse_probes(text: str) -> Dict[str, Dict[str, Any]]:
    probes: Dict[str, Dict[str, Any]] = {}
    for match in _PROBE_RE.finditer(str(text or "")):
        kind, scheme, spec = match.group(1).lower(), match.group(2).lower(), match.group(3)
        port_match = re.search(r":(\d+)", spec) or re.search(r"port=(\d+)", spec)
        probes[kind] = {
            "scheme": scheme,
            "raw": f"{scheme} {spec}".strip(),
            "port": int(port_match.group(1)) if port_match else None,
            "timeout": _kv(spec, "timeout"),
            "delay": _kv(spec, "delay"),
            "period": _kv(spec, "period"),
        }
    return probes


def _kv(text: str, key: str) -> Optional[str]:
    match = re.search(rf"{key}=(\S+)", text)
    return match.group(1) if match else None


def parse_containers(text: str) -> List[Dict[str, Any]]:
    lines = str(text or "").splitlines()
    containers: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    in_containers = False
    for line in lines:
        if line.startswith("Containers:"):
            in_containers = True
            continue
        if in_containers and line.startswith("Conditions:"):
            in_containers = False
        if not in_containers:
            continue
        name_match = re.match(r"\s{2}([A-Za-z0-9._-]+):\s*$", line)
        if name_match:
            current = {"name": name_match.group(1), "raw": ""}
            containers.append(current)
            continue
        if current is None:
            continue
        current["raw"] += line + "\n"
        stripped = line.strip()
        if stripped.startswith("Ready:"):
            current["ready"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Restart Count:"):
            current["restart_count"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Image:"):
            current["image"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Port:"):
            current["port"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("State:"):
            current["state"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Reason:"):
            current["state_reason"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Last State:"):
            current["last_state"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Exit Code:"):
            current["exit_code"] = stripped.split(":", 1)[1].strip()
    for container in containers:
        container["probes"] = parse_probes(container.get("raw", ""))
        container["limits"] = _limits(container.get("raw", ""))
    return containers


def _limits(raw: str) -> Dict[str, str]:
    limits: Dict[str, str] = {}
    block = _section(raw, "Limits:", ("Requests:", "Liveness:", "Readiness:", "Environment:"))
    for line in block.splitlines()[1:]:
        parts = line.strip().split(":", 1)
        if len(parts) == 2:
            limits[parts[0].strip()] = parts[1].strip()
    return limits


def parse_pod_selectors(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in str(text or "").splitlines():
        stripped = line.strip()
        for key in ("Node-Selectors:", "Tolerations:", "Node:"):
            if stripped.startswith(key):
                result[key.rstrip(":")] = stripped.split(":", 1)[1].strip()
    return result


def parse_conditions(text: str) -> Dict[str, str]:
    conditions: Dict[str, str] = {}
    block = _section(text, "Conditions:", ("Volumes:", "Events:", "QoS Class:"))
    for line in block.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in {"Type"}:
            conditions[parts[0]] = parts[1]
    return conditions


# ---------------------------------------------------------------- yaml / code


def parse_app_docs(text: str) -> List[Dict[str, Any]]:
    """``GetAppYAML`` returns a multi-document manifest (Deployment + Service + ServiceAccount).

    Using ``yaml.safe_load`` here raised on the second document and silently returned {} --
    which is why every env/selector rule was dead.  Parse all documents instead.
    """
    payload = re.sub(r"^GetAppYAML:.*?\n", "", str(text or ""), flags=re.DOTALL)
    try:
        import yaml

        return [doc for doc in yaml.safe_load_all(payload) if isinstance(doc, dict)]
    except Exception:
        return []


def parse_app_yaml(text: str) -> Dict[str, Any]:
    """Backwards-compatible: the Deployment document (or the first one)."""
    docs = parse_app_docs(text)
    for doc in docs:
        if doc.get("kind") == "Deployment":
            return doc
    return docs[0] if docs else {}


def yaml_doc_of_kind(docs: List[Dict[str, Any]], kind: str) -> Dict[str, Any]:
    for doc in docs:
        if str(doc.get("kind", "")).lower() == kind.lower():
            return doc
    return {}


def yaml_service_ports(service_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = (service_doc or {}).get("spec") or {}
    return [port for port in (spec.get("ports") or []) if isinstance(port, dict)]


def yaml_service_selector(service_doc: Dict[str, Any]) -> Dict[str, str]:
    spec = (service_doc or {}).get("spec") or {}
    return {str(k): str(v) for k, v in (spec.get("selector") or {}).items()}


def pod_labels(text: str) -> List[Dict[str, str]]:
    """LABELS column of ``GetResources(pods, show_labels)`` -> [{k: v}, ...]."""
    out: List[Dict[str, str]] = []
    for row in parse_table(text):
        raw = str(row.get("LABELS") or "").strip()
        labels: Dict[str, str] = {}
        if raw and raw != "<none>":
            for pair in re.split(r"[,\s]+", raw):
                if "=" in pair:
                    key, _, value = pair.partition("=")
                    labels[key.strip()] = value.strip()
        labels["__name__"] = row.get("NAME", "")
        out.append(labels)
    return out


def yaml_containers(yaml_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = (((yaml_doc or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return [item for item in (spec.get("containers") or []) if isinstance(item, dict)]


def yaml_replicas(yaml_doc: Dict[str, Any]) -> Optional[int]:
    spec = (yaml_doc or {}).get("spec") or {}
    value = spec.get("replicas")
    return int(value) if isinstance(value, int) else None


def parse_code_files(text: str) -> List[Dict[str, str]]:
    try:
        data = json.loads(str(text or "").strip())
    except Exception:
        return []
    files = data.get("files") if isinstance(data, dict) else None
    return [f for f in (files or []) if isinstance(f, dict)]


def code_hits(source: str, patterns: Dict[str, str]) -> List[str]:
    hits = []
    for label, pattern in patterns.items():
        if re.search(pattern, source or ""):
            hits.append(label)
    return hits
