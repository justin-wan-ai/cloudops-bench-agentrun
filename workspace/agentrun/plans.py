"""Plans: which probes to run for which fault family, and how to shortlist families.

This is the "prune exploration" half of AgentRun: the free agent discovers these
probe sequences by trial and error on every case; here they are compiled once
(P4 distillation) into ordered, parameterised plan steps.

A plan step is ``{"key", "tool", "args", "purpose"}`` where ``args`` may be a
callable ``state -> dict | None``; returning ``None`` skips the step.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import evidence as ev
from .state import CaseState

FAMILY_DESCRIPTIONS = {
    "admission": "the API server / quota / RBAC rejected the object, so it was never created (missing pods, quota exceeded, missing ServiceAccount)",
    "scheduling": "the pod exists but no node accepted it, so it stays Pending (affinity, selector, taints, capacity, PV/PVC binding)",
    "startup": "the container image or its mounting never started (image reference/registry/pull secret, volume mount, missing Secret)",
    "runtime": "the container starts but crashes or never becomes Ready (probes, memory limit, DB config, sidecar conflict, zero replicas)",
    "service": "pods are healthy but traffic does not reach them (selector, port mapping, env address, DNS, gateway route)",
    "performance": "the workload runs but is saturated or impaired (pod CPU, network delay, packet loss)",
    "infrastructure": "a node-level component is down (containerd, kubelet, kube-proxy, kube-scheduler)",
    "codedefect": "the application's own code is wrong or wasteful (busy loop, excessive file I/O, leak, wrong return/parameters, artificial delay)",
}

# Static domain prior (public service list), used only to notice *absent* workloads.
EXPECTED_APPS = {
    "boutique": ["adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice", "frontend",
                 "paymentservice", "productcatalogservice", "recommendationservice", "redis-cart",
                 "shippingservice"],
}

SEVERITY = {
    "Pending": 0,
    "CrashLoopBackOff": 1,
    "ImagePullBackOff": 2,
    "ErrImagePull": 2,
    "CreateContainerConfigError": 3,
    "Error": 4,
    "Running": 5,
}


def pod_severity(pod: Dict[str, Any]) -> int:
    ready_ok = pod.get("ready_ok")
    total = pod.get("ready_total")
    not_ready = ready_ok is not None and total is not None and ready_ok < total
    status = str(pod.get("status") or "")
    if status in SEVERITY:
        return SEVERITY[status]
    return 4 if not_ready else 9


# ------------------------------------------------------------------ signals


def analyse(state: CaseState) -> Dict[str, Any]:
    pods = ev.parse_pods(state.get("pods"))
    deployments = ev.parse_deployments(state.get("deployments"))
    alerts = ev.parse_alerts(state.get("alerts"))

    not_ready = [p for p in pods if p["ready_ok"] is not None and p["ready_total"] is not None and p["ready_ok"] < p["ready_total"]]
    pending = [p for p in not_ready if p["status"] == "Pending"]
    image = [p for p in pods if p["status"] in {"ImagePullBackOff", "ErrImagePull", "InvalidImageName"}]
    restarting = [p for p in pods if p["status"] in {"CrashLoopBackOff", "Error", "CreateContainerConfigError"}]
    # "scaled to zero / never created" -- NOT "ready=0 while a pod exists", which is a
    # runtime/startup fault with a different mechanism.
    zero_deploy = [d for d in deployments if d["ready_total"] == 0]
    pod_apps = {p["app"] for p in pods}
    missing_apps = [d["name"] for d in deployments if d["name"] not in pod_apps]
    if not deployments and pods:
        # static prior: the boutique system has exactly these 11 services, so "a pod
        # is missing" is decidable without the deployment table (trainticket has
        # infra pods whose names do not match the service list, so we skip it there).
        expected = EXPECTED_APPS.get(state.system, [])
        missing_apps = [app for app in expected if app not in pod_apps]

    alert_apps: List[str] = []
    alert_kinds: List[str] = []
    for alert in alerts.get("alerts", []) or []:
        if not isinstance(alert, dict):
            continue
        name = str(alert.get("name") or alert.get("entry_service") or "")
        if name:
            alert_apps.append(name)
        for fragment in (alert.get("evidence") or []):
            text = str(fragment)
            for kind in re.findall(r"([A-Z_]{4,})\s*\[([a-z_]+)\]", text):
                alert_kinds.append(f"{kind[0].lower()}[{kind[1]}]")
            alert_kinds.extend(re.findall(r'"?(?:raw_error|message)"?\s*[:=]\s*"?([A-Za-z]+Error)', text))
        if alert.get("raw_error"):
            alert_kinds.append(str(alert["raw_error"])[:60])

    suspect_pods = sorted(not_ready or image or restarting, key=pod_severity)
    suspect_app = ""
    if suspect_pods:
        suspect_app = suspect_pods[0]["app"]
    elif missing_apps:
        suspect_app = missing_apps[0]
    elif alert_apps:
        suspect_app = alert_apps[0]

    return {
        "pods": pods,
        "deployments": deployments,
        "alerts": alerts,
        "not_ready": not_ready,
        "pending": pending,
        "image": image,
        "restarting": restarting,
        "zero_deploy": zero_deploy,
        "missing_apps": missing_apps,
        "alert_apps": sorted(set(alert_apps)),
        "alert_kinds": sorted(set(k for k in alert_kinds if k)),
        "alert_count": len(alerts.get("alerts", []) or []),
        "suspect_pods": suspect_pods,
        "suspect_app": suspect_app,
        "all_healthy": not (not_ready or image or restarting or zero_deploy or missing_apps),
    }


# ---------------------------------------------------------------------------
# Named registries.  The DSL selects *which* of these to use and in what order;
# the code only implements their semantics.  No policy lives below this line.
# ---------------------------------------------------------------------------

GATES: Dict[str, Callable[[CaseState], bool]] = {}


_GATES_CACHE: Dict[str, Callable[[CaseState], bool]] = {}


def gate(name: str) -> Callable[[CaseState], Callable[[CaseState], bool]]:
    def register(fn: Callable[[CaseState], bool]) -> Callable[[CaseState], bool]:
        GATES[name] = fn
        _GATES_CACHE[name] = fn
        return fn

    return register


@gate("pending_claim")
def _g_pending_claim(state: CaseState) -> bool:
    rows = ev.parse_table(_first_value(state, "pvcs"))
    return any(str(row.get("STATUS", "")).lower() == "pending" for row in rows)


@gate("always")
def _g_always(state: CaseState) -> bool:
    return True


@gate("healthy_system")
def _g_healthy(state: CaseState) -> bool:
    """Alerts only earn evidence when no pod-level fault is visible."""
    return analyse(state)["all_healthy"]


@gate("weak")
def _g_weak(state: CaseState) -> bool:
    return not any(cand.score >= 0.8 for cand in state.candidates)


@gate("weak_strong")
def _g_weak_strong(state: CaseState) -> bool:
    return not any(cand.score >= 0.9 for cand in state.candidates)


@gate("weak_very_strong")
def _g_weak_very_strong(state: CaseState) -> bool:
    return not any(cand.score >= 0.95 for cand in state.candidates)


@gate("probe_evidence")
def _g_probe(state: CaseState) -> bool:
    text = _describe_text(state)
    return bool(re.search(r"probe failed|Ready:\s+False|Unhealthy", text, re.I))


@gate("restart_evidence")
def _g_restart(state: CaseState) -> bool:
    signals = analyse(state)
    return bool(signals["restarting"]) or any(
        str(pod.get("restarts") or "0") not in {"0", ""} for pod in signals["pods"]
    )


@gate("runtime_stall")
def _g_runtime_stall(state: CaseState) -> bool:
    """Pod stuck before the container starts (runtime layer), not an image/secret error."""
    text = _describe_text(state)
    if re.search(r"ImagePull|ErrImagePull|CreateContainerConfigError|InvalidImageName", text, re.I):
        return False
    # look at the pod/container *state* lines only: "Unknown" appears in unrelated prose
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("Status:", "State:", "Reason:")) and re.search(
                r"Init:\d|ContainerCreating|PodInitializing|ContainerCannotRun|Unknown", stripped):
            return True
    return False


@gate("unscheduled_without_events")
def _g_unscheduled(state: CaseState) -> bool:
    """Pod Pending with no node assigned and the scheduler said nothing at all."""
    text = _describe_text(state)
    if not text:
        return False
    # The interesting case is the *silent* one: a Pending pod that never got a node and
    # for which the scheduler never even logged a FailedScheduling event.
    return "FailedScheduling" not in text


@gate("connectivity_node_failure")
def _g_connectivity_node_failure(state: CaseState) -> bool:
    return bool(_connectivity_by_node(_first_value(state, "connectivity")))


@gate("node_reported_unreachable")
def _g_node_unreachable(state: CaseState) -> bool:
    """Only the scheduler's message counts -- every pod tolerates not-ready by default."""
    for key, value in state.obs.items():
        if key.startswith("sig:") or not value or not (key.startswith("describe:") or key == "events"):
            continue
        for event in ev.parse_events(value):
            if event.get("reason") == "FailedScheduling" and re.search(
                    r"node\.kubernetes\.io/(unreachable|not-ready)", event.get("message", ""), re.I):
                return True
    return False


SERVICE_NOUNS = {
    "cart": "cartservice", "products": "productcatalogservice", "product": "productcatalogservice",
    "currencies": "currencyservice", "currency": "currencyservice",
    "recommendations": "recommendationservice", "recommendation": "recommendationservice",
    "shipping": "shippingservice", "shipping quote": "shippingservice",
    "ads": "adservice", "ad": "adservice", "email": "emailservice",
    "payment": "paymentservice", "checkout": "checkoutservice",
}


def _service_mentions(state: CaseState) -> List[str]:  # noqa: D401
    """Order candidate apps by the entry-service error patterns, most frequent first.

    Extraction order per pattern (highest count first):
      1. "dial tcp <cluster-ip>:<port>"  -> Service whose CLUSTER-IP matches
      2. "lookup <name>"                 -> Service by DNS name
      3. "could not retrieve <noun>"     -> noun -> app map
      4. a literal app name in the text
    The entry service itself is appended last (its own env wiring is a suspect too).
    """
    raw = _first_value(state, "errors:frontend")
    services = ev.parse_table(_first_value(state, "services"))
    by_ip = {row.get("CLUSTER-IP"): row.get("NAME") for row in services if row.get("CLUSTER-IP")}
    by_name = {row.get("NAME") for row in services}

    patterns: List[tuple[int, str]] = []
    try:
        payload = json.loads(raw)
        for item in payload.get("patterns") or []:
            patterns.append((int(item.get("count") or 0), str(item.get("sample") or "")))
    except Exception:
        patterns.append((1, raw))
    patterns.sort(key=lambda item: -item[0])

    ordered: List[str] = []

    def add(app: str) -> None:
        if app and app in by_name and app != "frontend" and app not in ordered:
            ordered.append(app)

    for _count, text in patterns:
        for ip in re.findall(r"dial tcp (\d+\.\d+\.\d+\.\d+):", text):
            add(by_ip.get(ip, ""))
        for name in re.findall(r"lookup ([a-z][a-z0-9-]+)", text):
            add(name)
        for noun, app in SERVICE_NOUNS.items():
            if re.search(rf"could not retrieve {noun}\b", text, re.I):
                add(app)
        for name in by_name:
            if re.search(rf"\b{name}\b", text):
                add(name)
        if ordered:
            break
    return ordered + ["frontend"]


def _service_target(state: CaseState) -> str:
    mentions = _service_mentions(state)
    return mentions[0] if mentions else ""


@gate("service_target_found")
def _g_service_target(state: CaseState) -> bool:
    return bool(_service_target(state))


@gate("service_config_clean")
def _g_service_config_clean(state: CaseState) -> bool:
    return not any(cand.rule == "service_config" for cand in state.candidates)


def _pod_apps(state: CaseState) -> set:
    return {ev.owner_app(row.get("NAME", "")) for row in ev.parse_table(_first_value(state, "pods" if "pods" in state.obs else "pods:labels"))}


@gate("service_missing_for_pod")
def _g_service_missing(state: CaseState) -> bool:
    """A workload exists but its Service object does not -> the Service was never created."""
    services = {row.get("NAME") for row in ev.parse_table(_first_value(state, "services"))}
    if not services:
        return False
    return bool(_pod_apps(state) - services)


@gate("no_candidate_yet")
def _g_no_candidate(state: CaseState) -> bool:
    """Cheapest possible stop rule: once a candidate exists, stop paying for evidence."""
    return not state.candidates


@gate("node_scoped_symptom")
def _g_node_scoped(state: CaseState) -> bool:
    """Only pay for the node matrix when the symptom is actually node-scoped."""
    return (_GATES_CACHE.get("runtime_stall", lambda _s: False)(state)
            or _GATES_CACHE.get("unscheduled_without_events", lambda _s: False)(state)
            or _GATES_CACHE.get("node_reported_unreachable", lambda _s: False)(state)
            or _GATES_CACHE.get("connectivity_node_failure", lambda _s: False)(state))


@gate("connectivity_refused")
def _g_connectivity_refused(state: CaseState) -> bool:
    return bool(re.search(r"Connection (refused|failed)|no route|timed out", _first_value(state, "connectivity"), re.I))


@gate("node_all_healthy")
def _g_node_all_healthy(state: CaseState) -> bool:
    """True while no probed component has been reported as inactive."""
    for key, value in state.obs.items():
        if key.startswith("node:") and "\u25cb" in value:
            return False
    return True


@gate("probe_or_restart_or_weak")
def _g_probe_restart_weak(state: CaseState) -> bool:
    """Documented composite (OR of three atomic gates): is a probe/limits cross-check worth it?"""
    return _g_probe(state) or _g_restart(state) or not any(c.score >= 0.8 for c in state.candidates)


@gate("restart_or_weak_strong")
def _g_restart_weak_strong(state: CaseState) -> bool:
    """Documented composite (OR): logs are worth fetching after a restart or while undecided."""
    return _g_restart(state) or not any(c.score >= 0.9 for c in state.candidates)


@gate("volume_evidence")
def _g_volume(state: CaseState) -> bool:
    # must be about *claims*, not about the ubiquitous "Volumes:" section of every pod
    text = _describe_text(state) + " " + state.get("events")
    return bool(re.search(r"persistentvolumeclaims?|\bpvcs?\b|unbound immediate", text, re.I))


# --- probe definitions: name -> (observation key, tool, args resolver, purpose)

PROBES: Dict[str, Dict[str, Any]] = {}


def probe(name: str, key: str, tool: str, args: Any, purpose: str) -> None:
    PROBES[name] = {"name": name, "key": key, "tool": tool, "args": args, "purpose": purpose}


def _first_value(state: CaseState, prefix: str) -> str:
    for key, value in state.obs.items():
        if key.startswith(prefix) and not key.startswith("sig:"):
            return value
    return ""


def _describe_text(state: CaseState) -> str:
    return "".join(value for key, value in state.obs.items()
                   if key.startswith("describe:") and not key.startswith("sig:"))


def _suspect_app(state: CaseState) -> str:
    signals = analyse(state)
    return signals["suspect_app"] or (state.anchors.mentioned_apps[:1] or [""])[0]


def _args_pods(state: CaseState) -> Dict[str, Any]:
    # -o wide: the node/IP columns are what the process annotations expect for
    # node-scoped symptoms, and they cost nothing extra.
    return {"namespace": state.anchors.namespace, "resource_type": "pods", "output_wide": True}


def _args_pods_labels(state: CaseState) -> Dict[str, Any]:
    # separate call: kubectl rejects --show-labels together with -o wide
    return {"namespace": state.anchors.namespace, "resource_type": "pods", "show_labels": True}


def _args_deployments(state: CaseState) -> Dict[str, Any]:
    return {"namespace": state.anchors.namespace, "resource_type": "deployments"}


def _args_events(state: CaseState) -> Dict[str, Any]:
    return {"namespace": state.anchors.namespace, "resource_type": "events"}


def _args_resourcequota(state: CaseState) -> Dict[str, Any]:
    return {"namespace": state.anchors.namespace, "resource_type": "resourcequota"}


def _args_namespaces(state: CaseState) -> Dict[str, Any]:
    return {"resource_type": "namespaces", "name": state.anchors.namespace}


def _args_pvcs(state: CaseState) -> Dict[str, Any]:
    # NOTE: "pvcs" is not a valid alias in the benchmark's v1 tool; it silently errored
    # for every case until R1.  Use the canonical name.
    return {"namespace": state.anchors.namespace, "resource_type": "persistentvolumeclaims"}


def _args_pvs(state: CaseState) -> Dict[str, Any]:
    # labels matter: a labelled static PV only binds to a claim whose selector matches
    return {"resource_type": "persistentvolumes", "show_labels": True}


def _args_storage_classes(state: CaseState) -> Dict[str, Any]:
    return {"resource_type": "storageclass"}


def _pending_pvc(state: CaseState) -> str:
    for row in ev.parse_table(_first_value(state, "pvcs")):
        if row.get("STATUS", "").lower() == "pending":
            return row.get("NAME", "")
    return ""


def _args_describe_pvc(state: CaseState) -> Optional[Dict[str, Any]]:
    name = _pending_pvc(state)
    if not name:
        return None
    return {"namespace": state.anchors.namespace, "resource_type": "persistentvolumeclaims", "name": name}


def _args_services(state: CaseState) -> Dict[str, Any]:
    return {"namespace": state.anchors.namespace, "resource_type": "services"}


def _args_endpoints(state: CaseState) -> Dict[str, Any]:
    return {"namespace": state.anchors.namespace, "resource_type": "endpoints"}


def _args_describe_pod(state: CaseState) -> Optional[Dict[str, Any]]:
    pods = analyse(state)["suspect_pods"]
    if not pods:
        return None
    return {"namespace": state.anchors.namespace, "resource_type": "pods", "name": pods[0]["name"]}


def _args_describe_deployment(state: CaseState) -> Optional[Dict[str, Any]]:
    signals = analyse(state)
    target = ""
    if signals["zero_deploy"]:
        target = signals["zero_deploy"][0]["name"]
    elif signals["missing_apps"]:
        target = signals["missing_apps"][0]
    elif signals["suspect_app"]:
        target = signals["suspect_app"]
    return {"namespace": state.anchors.namespace, "resource_type": "deployments", "name": target} if target else None


def _args_app_yaml(state: CaseState) -> Optional[Dict[str, Any]]:
    app = _suspect_app(state)
    return {"app_name": app} if app else None


def _args_error_logs(state: CaseState) -> Optional[Dict[str, Any]]:
    app = _suspect_app(state)
    return {"namespace": state.anchors.namespace, "service_name": app} if app else None


def _args_connectivity(state: CaseState) -> Optional[Dict[str, Any]]:
    app = _suggest_connectivity_target(state)
    if not app:
        return None
    # the Service table already lists the exposed port(s): "9555/TCP"
    port = 0
    for row in ev.parse_table(_first_value(state, "services")):
        if row.get("NAME") == app:
            match = re.search(r"(\d+)/", row.get("PORT(S)", ""))
            if match:
                port = int(match.group(1))
            break
    if not port:
        containers = ev.yaml_containers(ev.parse_app_yaml(_first_value(state, "appyaml")))
        if containers:
            ports = containers[0].get("ports") or []
            if ports and isinstance(ports[0], dict) and ports[0].get("containerPort"):
                port = int(ports[0]["containerPort"])
    return {"namespace": state.anchors.namespace, "service_name": app, "port": port or 80}


_CODE_HINTS = [
    (r"write|disk|io", r"handler|rpc|main|service"),
    (r"read", r"handler|rpc|main"),
    (r"cpu|loop|latency|slow", r"handler|rpc|main|middleware"),
    (r"memory|leak", r"handler|rpc|main"),
]


def _args_code_files(state: CaseState) -> Optional[Dict[str, Any]]:
    app = _suspect_app(state)
    return {"app_name": app} if app else None


def _args_source_code(state: CaseState) -> Optional[Dict[str, Any]]:
    app = _suspect_app(state)
    if not app:
        return None
    files = ev.parse_code_files(_first_value(state, "codefiles"))
    if not files:
        return None
    signals = analyse(state)
    symptom = " ".join(state.anchors.symptoms + state.anchors.mentioned_apps + signals["alert_kinds"])
    preferred: List[str] = []
    for pattern, file_hint in _CODE_HINTS:
        if re.search(pattern, symptom, re.I):
            preferred.extend(f["path"] for f in files if re.search(file_hint, f["path"]))
    preferred.extend(f["path"] for f in files if re.search(r"handler|rpc|main|service", f["path"]))
    for path in preferred:
        if any(f["path"] == path for f in files):
            return {"app_name": app, "file_path": path}
    return {"app_name": app, "file_path": files[0]["path"]}


def _connectivity_by_node(report: str) -> Dict[str, bool]:
    """{"worker-02": False} for nodes whose debug pod failed the TCP test."""
    result: Dict[str, bool] = {}
    current = ""
    for line in str(report or "").splitlines():
        match = re.search(r"deployed on ([a-z0-9-]+):", line)
        if match:
            current = match.group(1)
            continue
        if current and "Connection Succeeded" in line:
            result[current] = True
        elif current and "Connection Failed" in line:
            result[current] = False
    return result


def _suggest_connectivity_target(state: CaseState) -> str:
    """Prefer the suspect app; otherwise a gRPC backend (the HTTP entry point never resolves)."""
    app = _suspect_app(state)
    if app:
        return app
    rows = ev.parse_table(_first_value(state, "services"))
    for row in rows:
        name = row.get("NAME", "")
        if name and name not in {"frontend", "redis-cart"}:
            return name
    return rows[0].get("NAME", "") if rows else ""


def _suspect_node(state: CaseState) -> str:
    """Node hosting the stalled pod; falls back to worker-01 when the pod never landed."""
    match = re.search(r"^Node:\s+([a-z0-9-]+)/", _describe_text(state), re.MULTILINE)
    return match.group(1) if match else "worker-01"


def _args_node_kubelet(state: CaseState) -> Dict[str, Any]:
    return {"node_name": _suspect_node(state), "service_name": "kubelet"}


def _args_node_containerd(state: CaseState) -> Dict[str, Any]:
    return {"node_name": _suspect_node(state), "service_name": "containerd"}


def _args_node_kube_proxy(state: CaseState) -> Dict[str, Any]:
    return {"node_name": _suspect_node(state), "service_name": "kube-proxy"}


def _args_node_scheduler(state: CaseState) -> Dict[str, Any]:
    return {"node_name": "master", "service_name": "kube-scheduler"}


ENTRY_SERVICE = {"boutique": "frontend", "trainticket": "ts-ui-dashboard"}


def _args_error_logs_entry(state: CaseState) -> Optional[Dict[str, Any]]:
    entry = ENTRY_SERVICE.get(state.system)
    return {"namespace": state.anchors.namespace, "service_name": entry} if entry else None


def _args_describe_service(state: CaseState) -> Optional[Dict[str, Any]]:
    target = _service_target(state)
    if not target:
        return None
    return {"namespace": state.anchors.namespace, "resource_type": "services", "name": target}


def _app_yaml_args_at(index: int):
    def resolver(state: CaseState) -> Optional[Dict[str, Any]]:
        mentions = _service_mentions(state)
        if index >= len(mentions):
            return None
        return {"app_name": mentions[index]}
    return resolver


def _args_deps(state: CaseState) -> Optional[Dict[str, Any]]:
    app = _suspect_app(state)
    return {"service_name": app} if app else None


probe("pods", "pods", "GetResources", _args_pods, "pod readiness census")
probe("pods_labels", "pods:labels", "GetResources", _args_pods_labels, "pod labels (service selector check)")
probe("alerts", "alerts", "GetAlerts", {}, "anomaly signature (routes performance/code families)")
probe("deployments", "deployments", "GetResources", _args_deployments, "which deployment never got a pod")
probe("events", "events", "GetResources", _args_events, "quota/RBAC rejection surfaces as Warning events")
probe("resourcequota", "resourcequota", "GetResources", _args_resourcequota, "quota headroom")
probe("namespaces", "namespaces", "GetResources", _args_namespaces, "namespace status")
probe("pvcs", "pvcs", "GetResources", _args_pvcs, "PVC status/capacity/class table")
probe("pvs", "pvs", "GetResources", _args_pvs, "PV capacity/access modes/claim binding")
probe("storage_classes", "storage_classes", "GetResources", _args_storage_classes, "existing StorageClasses")
probe("describe_pvc", "describe:pvc", "DescribeResource", _args_describe_pvc, "the pending PVC object")
probe("services", "services", "GetResources", _args_services, "service ports and selectors")
probe("endpoints", "endpoints", "GetResources", _args_endpoints, "endpoint population")
probe("describe_pod", "describe:pod", "DescribeResource", _args_describe_pod,
      "probe config, container state, restart reasons, scheduler events")
probe("describe_deployment", "describe:deployment", "DescribeResource", _args_describe_deployment,
      "replica accounting for the starved deployment")
probe("app_yaml", "appyaml", "GetAppYAML", _args_app_yaml, "declared probes / limits / image / env")
probe("error_logs", "errors", "GetErrorLogs", _args_error_logs, "application error logs")
probe("recent_logs", "recent", "GetRecentLogs", _args_error_logs, "request latency / saturation logs")
probe("connectivity", "connectivity", "CheckServiceConnectivity", _args_connectivity,
      "in-cluster TCP reachability")
probe("deps", "deps", "GetServiceDependencies", _args_deps, "upstream/downstream call graph")
def _args_deps_entry(state: CaseState) -> Optional[Dict[str, Any]]:
    entry = ENTRY_SERVICE.get(state.system)
    if not entry or (state.scope_system and state.system != state.scope_system):
        return None
    return {"service_name": entry}


probe("deps_entry", "deps:entry", "GetServiceDependencies", _args_deps_entry,
      "whole call graph from the entrypoint")
probe("error_logs_entry", "errors:frontend", "GetErrorLogs", _args_error_logs_entry,
      "entry-service errors: which dependency is failing")
probe("describe_service", "describe:service", "DescribeResource", _args_describe_service,
      "selector / port / targetPort / protocol of the failing service")
probe("app_yaml_c1", "appyaml:c1", "GetAppYAML", _app_yaml_args_at(0), "env wiring of the first mentioned app")
probe("app_yaml_c2", "appyaml:c2", "GetAppYAML", _app_yaml_args_at(1), "env wiring of the second mentioned app")
probe("app_yaml_c3", "appyaml:c3", "GetAppYAML", _app_yaml_args_at(2), "env wiring of the entry service")
probe("code_files", "codefiles", "ListCodeFiles", _args_code_files, "available source files")
probe("source_code", "source", "GetSourceCode", _args_source_code,
      "inspect the most symptom-relevant file")
probe("node_kubelet", "node:kubelet", "CheckNodeServiceStatus", _args_node_kubelet,
      "kubelet status on the node hosting the stalled pod")
# v0.7 name, kept so archived DSLs stay loadable
probe("node_status", "node:kubelet", "CheckNodeServiceStatus", _args_node_kubelet,
      "kubelet status on the node hosting the stalled pod")
probe("node_containerd", "node:containerd", "CheckNodeServiceStatus", _args_node_containerd,
      "container runtime status on the same node")
probe("node_kube_proxy", "node:kube-proxy", "CheckNodeServiceStatus", _args_node_kube_proxy,
      "kube-proxy status on the same node")
probe("node_scheduler", "node:kube-scheduler", "CheckNodeServiceStatus", _args_node_scheduler,
      "kube-scheduler status on master")
probe("cluster_config", "cluster", "GetClusterConfiguration", {}, "cluster-wide node state")


# --- route signals: name -> (family, reason builder); order comes from the DSL

SIGNALS: Dict[str, Callable[[CaseState], Optional[str]]] = {}


def signal(name: str) -> Callable[[Callable[[CaseState], Optional[str]]], Callable[[CaseState], Optional[str]]]:
    def register(fn: Callable[[CaseState], Optional[str]]) -> Callable[[CaseState], Optional[str]]:
        SIGNALS[name] = fn
        return fn

    return register


@signal("pending_pods")
def _s_pending(state: CaseState) -> Optional[str]:
    pending = analyse(state)["pending"]
    if not pending:
        return None
    return f"pod {pending[0]['name']} is Pending ({pending[0]['ready']})"


@signal("image_pull")
def _s_image(state: CaseState) -> Optional[str]:
    image = analyse(state)["image"]
    return f"pod {image[0]['name']} status {image[0]['status']}" if image else None


@signal("not_ready_or_crash")
def _s_not_ready(state: CaseState) -> Optional[str]:
    signals = analyse(state)
    pods = signals["restarting"] or signals["not_ready"]
    if not pods:
        return None
    return f"pod {pods[0]['name']} {pods[0]['status']} ready={pods[0]['ready']}"


@signal("no_pods_created")
def _s_no_pods(state: CaseState) -> Optional[str]:
    signals = analyse(state)
    if signals["missing_apps"]:
        return f"deployment {signals['missing_apps'][0]} has no pods at all"
    if signals["zero_deploy"]:
        return f"deployment {signals['zero_deploy'][0]['name']} has no replicas"
    return None


@signal("code_shaped_alert")
def _s_code_alert(state: CaseState) -> Optional[str]:
    kinds = " ".join(analyse(state)["alert_kinds"]).lower()
    if re.search(r"filesystem|fs_write|fs_read|heap|leak", kinds):
        return f"code-shaped anomaly signature: {kinds[:80]}"
    return None


@signal("saturation_alert")
def _s_saturation(state: CaseState) -> Optional[str]:
    signals = analyse(state)
    kinds = " ".join(signals["alert_kinds"]).lower()
    if signals["alert_count"] and re.search(r"saturat|cpu|network|delay|loss|latency", kinds):
        return f"{signals['alert_count']} anomaly alert(s): {kinds[:80]}"
    return None


@signal("healthy_unreachable")
def _s_healthy_unreachable(state: CaseState) -> Optional[str]:
    signals = analyse(state)
    if signals["all_healthy"] and "unavailable" in state.anchors.symptoms:
        return "symptom=unreachable and no pod-level fault observed"
    return None


@signal("healthy_degraded")
def _s_healthy_degraded(state: CaseState) -> Optional[str]:
    signals = analyse(state)
    if signals["all_healthy"] and ("degradation" in state.anchors.symptoms or signals["alert_count"]):
        return "symptom=degradation and no pod-level fault observed"
    return None


@signal("residual")
def _s_residual(state: CaseState) -> Optional[str]:
    return "node-level component outage is always a residual hypothesis"


def shortlist(state: CaseState, priority: List[Tuple[str, str]], fallback: str) -> List[Tuple[str, str]]:
    """Evaluate the DSL's route priority list in order; return [(family, why)]."""
    out: List[Tuple[str, str]] = []
    for signal_name, family in priority:
        if signal_name not in SIGNALS:
            raise KeyError(f"unknown route signal {signal_name!r}")
        reason = SIGNALS[signal_name](state)
        if reason and all(family != existing[0] for existing in out):
            out.append((family, reason))
    if not out:
        out.append((fallback, f"no deterministic signal matched; route_fallback={fallback}"))
    return out[:4]


def compile_steps(step_names: List[Dict[str, str]], steps_per_wave: int,
                  where: str) -> List[Dict[str, Any]]:
    """Turn DSL step references into executable probe steps (validated)."""
    compiled: List[Dict[str, Any]] = []
    for index, item in enumerate(step_names):
        name = item["step"]
        if name not in PROBES:
            raise KeyError(f"{where}: unknown probe {name!r}; known: {sorted(PROBES)}")
        raw_gates = item.get("gate") or ["always"]
        gate_names = [raw_gates] if isinstance(raw_gates, str) else list(raw_gates)
        for gate_name in gate_names:
            if gate_name not in GATES:
                raise KeyError(f"{where}: unknown gate {gate_name!r}; known: {sorted(GATES)}")
        gate_fns = [GATES[name] for name in gate_names]
        spec = PROBES[name]
        compiled.append(
            {
                "name": name,
                "key": spec["key"],
                "tool": spec["tool"],
                "args": spec["args"],
                "purpose": spec["purpose"],
                # a gate list is a conjunction: every listed gate must hold
                "when": (lambda state, fns=tuple(gate_fns): all(fn(state) for fn in fns)),
                "gate": " | ".join(gate_names),
                "wave": 0 if where == "census" else 1 + (index // max(1, steps_per_wave)),
            }
        )
    return compiled


def census_plan(table: List[Dict[str, str]], steps_per_wave: int) -> List[Dict[str, Any]]:
    return compile_steps(table, steps_per_wave, "census")


def build_plan(family: str, table: Dict[str, List[Dict[str, str]]], steps_per_wave: int) -> List[Dict[str, Any]]:
    """Compile the DSL-declared plan for a family. Unknown family is an error."""
    if family not in table:
        raise KeyError(f"no family_plan for family {family!r}; known: {sorted(table)}")
    return compile_steps(table[family], steps_per_wave, f"family_plan.{family}")
