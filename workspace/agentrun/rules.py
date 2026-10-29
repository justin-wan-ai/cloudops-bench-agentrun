"""Deterministic rule engine: evidence -> ranked root-cause candidates.

Every rule is a pure function of the observations collected so far plus the
anchors.  Rules never look at ground truth; they encode *mechanism* knowledge
(kubectl semantics), which is what makes them transferable across cases.

Scores are priors for ordering only (iron rule 3: priors may sort candidates, they
must not silently become the answer -- the answer still goes through jev(pick) /
jev(verify) or escalate).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from . import evidence as ev
from .anchors import Anchors
from . import benchbridge  # noqa: F401  (sys.path side effect: repo agent dir)
from .plans import analyse
from .state import Candidate, CaseState

from runtime.contracts import SYSTEM_VALID_SERVICES as VALID_SERVICES  # type: ignore  # noqa: E402

VALID_ROOT_CAUSES = {
    "namespace_cpu_quota_exceeded", "namespace_memory_quota_exceeded", "namespace_pod_quota_exceeded",
    "namespace_service_quota_exceeded", "namespace_storage_quota_exceeded", "missing_service_account",
    "node_cordon_mismatch", "node_affinity_mismatch", "node_selector_mismatch", "pod_anti_affinity_conflict",
    "taint_toleration_mismatch", "cpu_capacity_mismatch", "memory_capacity_mismatch", "node_network_delay",
    "node_network_packet_loss", "containerd_unavailable", "kubelet_unavailable", "kube_proxy_unavailable",
    "kube_scheduler_unavailable", "image_registry_dns_failure", "incorrect_image_reference",
    "missing_image_pull_secret", "pvc_selector_mismatch", "pvc_storage_class_mismatch",
    "pvc_access_mode_mismatch", "pvc_capacity_mismatch", "pv_binding_occupied",
    "volume_mount_permission_denied", "container_memory_limit_too_low", "liveness_probe_incorrect_protocol",
    "liveness_probe_incorrect_port", "liveness_probe_incorrect_timing", "readiness_probe_incorrect_protocol",
    "readiness_probe_incorrect_port", "readiness_probe_incorrect_timing", "service_selector_mismatch",
    "service_port_mapping_mismatch", "service_protocol_mismatch", "service_env_var_address_mismatch",
    "pod_cpu_overload", "pod_network_delay", "service_sidecar_port_conflict",
    "service_dns_resolution_failure", "mysql_invalid_credentials", "mysql_invalid_port",
    "missing_secret_binding", "db_connection_exhaustion", "db_readonly_mode", "gateway_misrouted",
    "deployment_zero_replicas", "code_busy_loop", "code_memory_leak", "code_artificial_delay",
    "code_excessive_file_reads", "code_excessive_file_writes", "code_wrong_return",
    "code_missing_parameter", "code_wrong_argument_order",
}


ROOT_CAUSE_FAMILY = {
    "admission": ("namespace_", "missing_service_account"),
    "scheduling": ("node_cordon", "node_affinity", "node_selector", "pod_anti_affinity", "taint_toleration",
                   "cpu_capacity", "memory_capacity", "pvc_", "pv_binding"),
    "startup": ("image_", "volume_mount_", "missing_secret_binding"),
    "runtime": ("liveness_probe", "readiness_probe", "container_memory_limit", "mysql_", "db_",
                "service_sidecar_port_conflict", "deployment_zero_replicas"),
    "service": ("service_selector", "service_port_mapping", "service_protocol", "service_env_var",
                "service_dns_resolution", "gateway_misrouted"),
    "performance": ("pod_cpu_overload", "pod_network_delay", "node_network_delay", "node_network_packet_loss"),
    "infrastructure": ("containerd_", "kubelet_", "kube_proxy_", "kube_scheduler_"),
    "codedefect": ("code_",),
}


def family_of(root_cause: str) -> str:
    for family, prefixes in ROOT_CAUSE_FAMILY.items():
        if any(root_cause.startswith(prefix) for prefix in prefixes):
            return family
    return ""


SYSTEM_KEY = {"boutique": "boutique", "trainticket": "train-ticket"}


def _app(state: CaseState, name: str = "") -> str:
    """``app/<service>`` only for services the benchmark accepts as a target."""
    candidate = name or analyse(state)["suspect_app"]
    valid = VALID_SERVICES.get(SYSTEM_KEY.get(state.system, state.system), [])
    return f"app/{candidate}" if candidate in valid else ""


def _push(out: List[Candidate], root_cause: str, obj: str, score: float, rule: str, evidence: str) -> None:
    if root_cause not in VALID_ROOT_CAUSES or not obj:
        return
    out.append(Candidate(root_cause=root_cause, object=obj, score=score, evidence=evidence.strip()[:240], rule=rule))


def derive_candidates(state: CaseState) -> List[Candidate]:
    out: List[Candidate] = []
    signals = analyse(state)
    anchors = state.anchors
    describe_pod_key = next((k for k in state.obs if k.startswith("describe:pod")), "")
    describe_pod = state.get(describe_pod_key)
    describe_deployment = _first(state, "describe:deployment")
    events_text = " ".join(
        [_first(state, "events"), describe_deployment, describe_pod] + [m.get("message", "") for m in ev.parse_events(describe_pod)]
    )
    # NB: an observation that starts with "Error:" is a tool failure, not evidence -- letting
    # its text through once made a "GetErrorLogs does not support 'frontend'" message match the
    # gateway rule (the allowed-service list contains ts-route-*), which answered 17 zero-replica
    # cases as gateway_misrouted.
    errors_text = _clean_text(_first(state, "errors")) + "\n" + _clean_text(_first(state, "recent"))
    app_yaml = ev.parse_app_yaml(_first(state, "appyaml"))
    code_text = _first(state, "source")

    # ---------------------------------------------------------------- R1 quota
    for match in re.finditer(r"exceeded quota:\s*([\w.\-]+),?\s*requested:\s*([^,]+),\s*used:\s*([^,]+),\s*limited:\s*(\S+)", events_text):
        quota_name, requested = match.group(1), match.group(2).lower()
        root_cause = "namespace_memory_quota_exceeded"
        if "cpu" in requested:
            root_cause = "namespace_cpu_quota_exceeded"
        elif "storage" in requested or "ephemeral" in requested:
            root_cause = "namespace_storage_quota_exceeded"
        elif "pod" in requested:
            root_cause = "namespace_pod_quota_exceeded"
        elif "service" in requested:
            root_cause = "namespace_service_quota_exceeded"
        elif "memory" in requested or "memory" in quota_name:
            root_cause = "namespace_memory_quota_exceeded"
        _push(out, root_cause, f"namespace/{anchors.namespace}", 0.95, "quota_event",
              f"{quota_name}: {requested} requested, {match.group(3)} used, limit {match.group(4)}")

    # ---------------------------------------------------------------- R1b quota object
    # When admission rejects an *object kind*, the ResourceQuota row itself names the
    # exhausted resource ("services: 11/11", "requests.storage: 9Gi/10Gi").
    quota_rows = ev.parse_table(_first(state, "resourcequota"))
    services_table = {row.get("NAME") for row in ev.parse_table(_first(state, "services"))}
    if services_table and quota_rows:
        pod_apps = {ev.owner_app(row.get("NAME", "")) for row in ev.parse_table(_first(state, "pods"))}
        missing_service = sorted(app for app in pod_apps - services_table if app)
        quota_services = [row for row in quota_rows if "services" in f"{row.get('REQUEST','')} {row.get('LIMIT','')}"]
        if missing_service and quota_services:
            _push(out, "namespace_service_quota_exceeded", f"namespace/{anchors.namespace}", 0.88, "quota_object",
                  f"Service object missing for running workload(s) {missing_service}; "
                  f"ResourceQuota {quota_services[0].get('NAME')} covers services")
    pending_pvc = any(row.get("STATUS", "").lower() == "pending"
                      for row in ev.parse_table(_first(state, "pvcs")))
    existing_services = {row.get("NAME") for row in ev.parse_table(_first(state, "services"))}
    for row in quota_rows:
        cell = f"{row.get('REQUEST', '')} {row.get('LIMIT', '')}"
        used_hard = re.search(r"(\S+):\s*(\S+)/(\S+)", cell)
        exhausted = bool(used_hard and used_hard.group(2) == used_hard.group(3))
        resource = (used_hard.group(1) if used_hard else row.get("REQUEST", "")) or ""
        root_cause = ""
        if "services" in resource and (exhausted or (
                signals["missing_apps"] and not existing_services)):
            root_cause = "namespace_service_quota_exceeded"
        elif "storage" in resource and (pending_pvc or exhausted):
            root_cause = "namespace_storage_quota_exceeded"
        elif "pods" in resource and exhausted:
            root_cause = "namespace_pod_quota_exceeded"
        elif "memory" in resource and exhausted:
            root_cause = "namespace_memory_quota_exceeded"
        elif "cpu" in resource and exhausted:
            root_cause = "namespace_cpu_quota_exceeded"
        if root_cause:
            _push(out, root_cause, f"namespace/{anchors.namespace}", 0.9, "quota_object",
                  f"ResourceQuota {row.get('NAME')} reports {resource} at {used_hard.group(2) if used_hard else '?'}"
                  f"/{used_hard.group(3) if used_hard else '?'}")

    # ---------------------------------------------------------------- R2 service account
    if re.search(r"serviceaccount\s+\"[^\"]+\"\s+not found|error looking up service account", events_text, re.I):
        _push(out, "missing_service_account", _app(state), 0.9, "sa_event", "ServiceAccount referenced by the pod does not exist")

    # ---------------------------------------------------------------- R3 scheduling
    # Evidence scoping matters here: the old version classified from *any* text that
    # mentioned "selector"/"volume" (including Deployment.Selector), which made every
    # Pending pod look like a selector/PVC mismatch.  We now use
    #   (a) only the scheduler's own FailedScheduling message, and
    #   (b) a constraint solver over pod spec x node inventory (labels / taints / capacity).
    sched_msgs = _scheduler_messages(state)
    sched_text = " ".join(sched_msgs)
    if sched_text:
        pod = _pod_constraints(describe_pod)
        nodes = _cluster_nodes(state)
        verdict = _scheduling_verdict(sched_text, pod, nodes, describe_pod)
        if verdict:
            root_cause, score, why = verdict
            _push(out, root_cause, _app(state), score, "scheduling_solver", why)

    # PVC subfamily: driven by the PVC/PV/StorageClass tables, not by prose.
    pvc_verdict = _pvc_verdict(state, sched_text)
    for root_cause, score, why in pvc_verdict:
        _push(out, root_cause, _app(state), score, "pvc_binding", why)

    # ---------------------------------------------------------------- R4 image pull
    image_text = describe_pod + " " + events_text
    if re.search(r"ImagePullBackOff|ErrImagePull|Failed to pull image|InvalidImageName", image_text, re.I):
        if re.search(r"no such host|server misbehaving|i/o timeout|lookup .* on |dns", image_text, re.I):
            root_cause = "image_registry_dns_failure"
        # a bare "403 Forbidden" on a HEAD/manifest request means the *tag* does not exist
        # (incorrect reference); only a failed *authorization* means the pull secret is missing.
        elif re.search(r"unauthorized|authentication required|pull access denied|no basic auth|"
                       r"failed to authorize|anonymous token", image_text, re.I):
            root_cause = "missing_image_pull_secret"
        else:
            root_cause = "incorrect_image_reference"
        _push(out, root_cause, _app(state), 0.85, "image_pull", _first_match(image_text, r"(Failed to pull image[^.]{0,120}|Error: ImagePullBackOff\S*)") or "image pull failure")

    # ---------------------------------------------------------------- R5 probes
    containers = ev.parse_containers(describe_pod)
    probe_candidates = _probe_candidates(containers, describe_pod, app_yaml)
    for root_cause, score, why in probe_candidates:
        _push(out, root_cause, _app(state), score, "probe_config", why)

    # ---------------------------------------------------------------- R6 memory limit
    if re.search(r"OOMKilled|out of memory|Cannot allocate memory", describe_pod + errors_text, re.I):
        _push(out, "container_memory_limit_too_low", _app(state), 0.9, "oom", "container terminated with OOMKilled")

    # ---------------------------------------------------------------- R7 volume permission
    if re.search(r"permission denied", errors_text + " " + describe_pod, re.I) and re.search(r"mount|volume", errors_text + " " + describe_pod, re.I):
        _push(out, "volume_mount_permission_denied", _app(state), 0.85, "volume_perm", "volume mount rejected with permission denied")

    # ---------------------------------------------------------------- R8 missing secret
    if re.search(r"CreateContainerConfigError", describe_pod, re.I) and re.search(r"secret", describe_pod, re.I):
        _push(out, "missing_secret_binding", _app(state), 0.85, "secret_binding", "pod references a Secret that cannot be mounted")

    # ---------------------------------------------------------------- R9 database
    dns_symptom = bool(re.search(r"UnknownHost|no such host|name or service not known|"
                                 r"Temporary failure in name resolution|lookup \S+ on", errors_text, re.I))
    if re.search(r"mysql|jdbc|hikari|sqlstate|db2|database", errors_text, re.I) and not dns_symptom:
        db = anchors.priors.get("db_service") or ""
        if re.search(r"access denied for user|invalid credentials|authentication failed", errors_text, re.I):
            _push(out, "mysql_invalid_credentials", _app(state), 0.9, "db_auth", "MySQL rejected the credentials")
        elif re.search(r"too many connections|connection pool exhausted|cannot get connection", errors_text, re.I):
            _push(out, "db_connection_exhaustion", _app(state), 0.9, "db_pool", "connection pool exhausted")
        elif re.search(r"read[- ]only", errors_text, re.I):
            _push(out, "db_readonly_mode", _app(state), 0.85, "db_readonly", "database is in read-only mode")
        elif re.search(r"communications link failure|connection refused", errors_text, re.I):
            reachable = "Connection Succeeded" in _first(state, "connectivity")
            score = 0.85 if reachable else 0.6
            _push(out, "mysql_invalid_port", _app(state), score, "db_port",
                  f"JDBC connect refused while {db or 'the datastore'} answers on its standard port" if reachable
                  else "JDBC connect refused; datastore reachability unknown")

    # ---------------------------------------------------------------- R10 zero replicas
    for row in ev.parse_table(_first(state, "deployments")):
        if str(row.get("READY", "")).strip() == "0/0":
            _push(out, "deployment_zero_replicas", _app(state, row.get("NAME", "")), 0.92, "zero_replicas",
                  f"deployment {row.get('NAME')} has 0 desired replicas (READY 0/0)")
    for deployment in signals["deployments"]:
        if deployment["ready_total"] == 0:
            _push(out, "deployment_zero_replicas", f"app/{deployment['name']}", 0.6, "zero_replicas",
                  f"deployment {deployment['name']} has no replicas")
    if ev.yaml_replicas(app_yaml) == 0:
        app_name = str((app_yaml.get("metadata") or {}).get("name") or "")
        _push(out, "deployment_zero_replicas", f"app/{app_name}", 0.65, "zero_replicas_yaml", "spec.replicas: 0 in the app manifest")

    # ---------------------------------------------------------------- R11 service routing
    services = ev.parse_table(_first(state, "services"))
    endpoints = _first(state, "endpoints")
    if _first(state, "services"):
        selector_miss = bool(re.search(r"<none>|none", endpoints, re.I))
        if selector_miss and signals["all_healthy"]:
            _push(out, "service_selector_mismatch", _app(state), 0.6, "endpoints_empty",
                  "service endpoints are empty while the backing pods are Running")
            _push(out, "service_port_mapping_mismatch", _app(state), 0.5, "endpoints_empty",
                  "empty endpoints may also mean the Service port does not map to the container port")
        if re.search(r"Connection (refused|failed)|no route", _first(state, "connectivity"), re.I):
            _push(out, "service_port_mapping_mismatch", _app(state), 0.65, "connectivity_refused",
                  "in-cluster TCP check to the service port failed")
    if re.search(r"no such host|dns|name resolution|SERVFAIL", errors_text, re.I):
        _push(out, "service_dns_resolution_failure", _app(state), 0.75, "dns_log", "DNS resolution failure in application logs")
    gateway_services = [row.get("NAME") for row in ev.parse_table(_first(state, "services"))
                        if re.search(r"gateway|ingress", str(row.get("NAME") or ""), re.I)]
    if gateway_services and re.search(r"404|502|503|route", errors_text, re.I):
        # the victim of a bad route is the gateway itself, not whichever service looks slow
        _push(out, "gateway_misrouted", _app(state, gateway_services[0]), 0.8, "gateway",
              f"gateway service {gateway_services[0]} reports routing failures")
    for env_name, env_value in _env_addresses(app_yaml):
        if services and not any(str(env_value).split(":")[0] in row.get("NAME", "") for row in services):
            _push(out, "service_env_var_address_mismatch", _app(state), 0.6, "env_addr",
                  f"{env_name}={env_value} does not match any Service in the namespace")
            break

    # ---------------------------------------------------------------- R12 performance
    # Alerts are the only telemetry in this benchmark.  Three mechanisms are separable:
    #   RESOURCE_SATURATION[cpu_cfs] on a service      -> pod_cpu_overload (throttling)
    #   NETWORK_QUALITY_DEGRADED on N nodes           -> node_network_* (>=2 nodes = packet loss,
    #                                                    a delay injection only shows on one)
    #   latency spikes only, no node signal           -> the *leaf* of the latency graph is the
    #                                                    injected pod (callers inherit its latency)
    # R8 tried to make this system-agnostic; the trainticket latency alerts are system-wide and
    # its service graph is one level deep, so the rules answered confidently-wrong there (R24).
    # Keep the alert-vocabulary rules inside the SOP's declared system.
    in_scope = not state.scope_system or state.system == state.scope_system
    alerts = (signals["alerts"].get("alerts") or []) if in_scope else []
    latency: Dict[str, float] = {}
    cpu_throttled: List[str] = []
    node_retrans: Dict[str, float] = {}
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        name = str(alert.get("name") or "")
        kind = str(alert.get("type") or "")
        evidence = [str(item) for item in (alert.get("evidence") or [])]
        joined = " ".join(evidence)
        if kind == "Service":
            if re.search(r"RESOURCE_SATURATION \[cpu", joined):
                cpu_throttled.append(name)
            for item in evidence:
                match = re.search(r"p\d+latency: Normal=[\d.]+ms -> Current=([\d.]+)ms", item)
                if match:
                    latency[name] = max(latency.get(name, 0.0), float(match.group(1)))
        elif kind == "Node" and "TCP Retransmission" in joined:
            match = re.search(r"Current=([\d.]+)retrans", joined)
            node_retrans[name] = float(match.group(1)) if match else 0.0

    for app_name in cpu_throttled:
        _push(out, "pod_cpu_overload", _app(state, app_name), 0.9, "alert_cpu_throttle",
              f"{app_name} is CPU-throttled (cpu_cfs saturation alert)")
    if len(node_retrans) >= 2:
        worst = max(node_retrans, key=lambda node: node_retrans[node])
        _push(out, "node_network_packet_loss", f"node/{worst}", 0.88, "alert_node_retrans",
              f"{len(node_retrans)} nodes show TCP retransmission spikes; {worst} is worst "
              f"({node_retrans[worst]:.2f}/s)")
    elif len(node_retrans) == 1:
        node = next(iter(node_retrans))
        _push(out, "node_network_delay", f"node/{node}", 0.88, "alert_node_retrans",
              f"{node} shows TCP retransmissions with no peer node affected (delay, not loss)")
    elif latency and not cpu_throttled:
        leaf = _latency_leaf(state, latency)
        if leaf:
            _push(out, "pod_network_delay", _app(state, leaf), 0.85, "alert_latency_leaf",
                  f"{leaf} is the deepest service with a latency spike "
                  f"({latency.get(leaf, 0):.0f}ms); its callers only inherit it")

    # ---------------------------------------------------------------- R13 infrastructure
    # systemd status lines are the ground truth here: "Status: ● <name>.service" = active,
    # "Status: ○ ..." = inactive/failed.  The probe/rule mapping is 1:1 with the taxonomy.
    for key, value in state.obs.items():
        if not key.startswith("node:") or key.startswith("sig:") or not value:
            continue
        component = key.split(":", 1)[1]
        if component not in {"kubelet", "containerd", "kube-proxy", "kube-scheduler"}:
            continue
        node_match = re.search(r"Node:\s*([a-z0-9-]+)", value)
        node = node_match.group(1) if node_match else "worker-01"
        inactive = ("\u25cb" in value) or bool(re.search(r"Active:\s*(inactive|failed|activating)", value))
        if inactive:
            root_cause = component.replace("-", "_") + "_unavailable"
            _push(out, root_cause, f"node/{node}", 0.95, "node_component",
                  f"{component}.service is inactive on {node}")

    # ------------------------------------------------- R11b node-scoped connectivity
    # CheckServiceConnectivity returns a per-node debug-pod verdict; one failing node while
    # others succeed is kube-proxy on that node, not an application routing defect.
    by_node = {}
    for key, value in state.obs.items():
        if key.startswith("connectivity") and not key.startswith("sig:") and value:
            current = ""
            for line in value.splitlines():
                match = re.search(r"deployed on ([a-z0-9-]+):", line)
                if match:
                    current = match.group(1)
                    continue
                if current and "Connection Succeeded" in line:
                    by_node[current] = True
                elif current and "Connection Failed" in line:
                    by_node[current] = False
    if by_node and any(by_node.values()) and not all(by_node.values()):
        bad = sorted(node for node, ok in by_node.items() if not ok)
        _push(out, "kube_proxy_unavailable", f"node/{bad[0]}", 0.9, "connectivity_by_node",
              f"debug pod on {bad[0]} cannot reach the service while "
              f"{sorted(n for n, ok in by_node.items() if ok)} can")

    # ------------------------------------------------- R11c service configuration
    # `GetAppYAML` returns Deployment + Service + ServiceAccount.  The Service document gives
    # selector / ports / protocol; the Deployment document gives the *_ADDR wiring.
    # Only the service route collects the service inventory + pod labels.  Other families
    # also fetch app_yaml, and judging their Service document without that inventory is how
    # R3 first "fixed" service while breaking 11 startup cases.
    labels = ev.pod_labels(_first(state, "pods:labels"))
    services = {row.get("NAME"): row for row in ev.parse_table(_first(state, "services"))}
    service_route = bool(services) and bool(labels)
    for key, value in (state.obs.items() if service_route else []):
        if not key.startswith("appyaml") or key.startswith("sig:") or not value:
            continue
        docs = ev.parse_app_docs(value)
        if not docs:
            continue
        service_doc = ev.yaml_doc_of_kind(docs, "Service")
        deployment = ev.parse_app_yaml(value)
        app_name = str((deployment.get("metadata") or {}).get("name") or "")
        if not app_name:
            continue

        selector = ev.yaml_service_selector(service_doc)
        pods_for_selector = [item for item in labels
                             if all(item.get(k) == v for k, v in selector.items())] if selector else []
        # pod labels are the authoritative source; empty Endpoints is a consequence, and the
        # endpoints probe is not part of this plan any more.
        selector_and_endpoints = bool(selector) and not pods_for_selector
        if selector_and_endpoints:
            missing = sorted({key for key in selector if not any(item.get(key) == selector[key] for item in labels)})
            _push(out, "service_selector_mismatch", _app(state, app_name), 0.93, "service_config",
                  f"Service {app_name} selects {selector} but no pod carries {missing}; endpoints are empty")

        container_ports = {int(port.get("containerPort")) for container in ev.yaml_containers(deployment)
                           for port in (container.get("ports") or []) if isinstance(port, dict)
                           and port.get("containerPort")}
        container_port_names = {str(port.get("name")) for container in ev.yaml_containers(deployment)
                                for port in (container.get("ports") or []) if isinstance(port, dict)
                                and port.get("name")}
        for port in ev.yaml_service_ports(service_doc):
            protocol = str(port.get("protocol") or "TCP").upper()
            if protocol == "UDP":
                _push(out, "service_protocol_mismatch", _app(state, app_name), 0.9, "service_config",
                      f"Service {app_name} exposes {port.get('port')}/UDP while the app speaks gRPC over TCP")
            target = port.get("targetPort")
            if isinstance(target, str) and not target.isdigit():
                if target not in container_port_names:
                    _push(out, "service_port_mapping_mismatch", _app(state, app_name), 0.9, "service_config",
                          f"Service {app_name} targetPort {target!r} is not a port name of the container "
                          f"(container port names: {sorted(container_port_names) or 'none'})")
            elif isinstance(target, int) and container_ports and target not in container_ports:
                _push(out, "service_port_mapping_mismatch", _app(state, app_name), 0.88, "service_config",
                      f"Service {app_name} targets container port {target} but the container exposes "
                      f"{sorted(container_ports)}")

        for container in ev.yaml_containers(deployment):
            for item in container.get("env") or []:
                if not isinstance(item, dict):
                    continue
                env_name = str(item.get("name") or "")
                addr = str(item.get("value") or "")
                if not re.search(r"_ADDR$|_URL$", env_name) or ":" not in addr:
                    continue
                host, _, port = addr.rpartition(":")
                if host not in services:
                    pod_apps = {ev.owner_app(row.get("NAME", "")) for row in ev.parse_table(_first(state, "pods"))}
                    if host in pod_apps:
                        # the Service object was never created for a running workload: that is
                        # an admission/quota symptom, not a wrong env value (see quota rule)
                        continue
                    if host in VALID_SERVICES.get(SYSTEM_KEY.get(state.system, state.system), []):
                        _push(out, "service_env_var_address_mismatch", _app(state, app_name), 0.9, "env_addr",
                              f"{app_name}.{env_name}={addr!r}: no Service named {host!r} exists")
                else:
                    real = re.search(r"(\d+)/", services[host].get("PORT(S)", ""))
                    if real and port and port != real.group(1):
                        _push(out, "service_env_var_address_mismatch", _app(state, app_name), 0.88, "env_addr",
                              f"{app_name}.{env_name}={addr!r} but Service {host} listens on {real.group(1)}")

    # ------------------------------------------- R6 limits / sidecar port (Deployment doc)
    if app_yaml:
        for container in ev.yaml_containers(app_yaml):
            limits = ((container.get("resources") or {}).get("limits") or {})
            limit_mem = str(limits.get("memory") or "")
            if re.fullmatch(r"\d+Mi", limit_mem) and int(limit_mem[:-2]) <= 32:
                app_name = str((app_yaml.get("metadata") or {}).get("name") or "")
                _push(out, "container_memory_limit_too_low", _app(state, app_name), 0.9, "memory_floor",
                      f"{app_name} has a memory limit of {limit_mem}, below any plausible runtime footprint")
        annotations = ((((app_yaml.get("spec") or {}).get("template") or {}).get("metadata") or {})
                       .get("annotations") or {})
        status_port = None
        for value in annotations.values():
            match = re.search(r'"statusPort"\s*:\s*(\d+)', str(value))
            if match:
                status_port = int(match.group(1))
        if status_port:
            container_ports = {int(port.get("containerPort")) for container in ev.yaml_containers(app_yaml)
                               for port in (container.get("ports") or []) if isinstance(port, dict)
                               and port.get("containerPort")}
            if status_port in container_ports:
                app_name = str((app_yaml.get("metadata") or {}).get("name") or "")
                _push(out, "service_sidecar_port_conflict", _app(state, app_name), 0.9, "sidecar_status_port",
                      f"sidecar statusPort {status_port} collides with the app container port on {app_name}")

    # ---------------------------------------------------------------- R14 code
    # the *code*-defect signatures are boutique-only, keep them scoped
    code_scope = not state.scope_system or state.system == state.scope_system
    alert_text = " ".join(str(item) for item in alerts) if code_scope else ""
    # (a) anomaly signatures that only a code-level cause explains
    for root_cause, pattern in (
        ("code_excessive_file_writes", r"FILESYSTEM_WRITE_SURGE|fs_write"),
        ("code_excessive_file_reads", r"FILESYSTEM_READ|fs_read"),
        ("code_memory_leak", r"MEMORY_LEAK|heap_growth|HEAP_GROWTH"),
    ):
        if re.search(pattern, alert_text, re.I):
            for app in signals["alert_apps"] or [signals["suspect_app"]]:
                _push(out, root_cause, f"app/{app}", 0.8, "code_alert_signature",
                      f"anomaly signature {pattern.split('|')[0]} on a running pod")
    # (b) source scan confirmations
    if code_text:
        hits = ev.code_hits(
            code_text,
            {
                "code_excessive_file_writes": r"os\.Create\(|os\.WriteFile\(|ioutil\.WriteFile\(",
                "code_excessive_file_reads": r"ioutil\.ReadFile\(|os\.ReadFile\(",
                "code_busy_loop": r"for\s*\{[^}]{0,400}\}",
                "code_artificial_delay": r"time\.Sleep\(\s*[1-9]\d*\s*\*\s*time\.(Second|Millisecond)",
                "code_memory_leak": r"global\w*\s*=\s*append\(|map\[[^\]]+\][^=\n]*=\s*append\(",
            },
        )
        for root_cause in hits:
            _push(out, root_cause, _app(state), 0.7, "code_scan", f"source scan hit for {root_cause}")

    # ---------------------------------------------------------------- family consistency
    # The route decision already narrowed the mechanism domain; candidates that belong
    # to another family are still kept (they are rank 2/3 material and the escalate
    # fallback), but they must not outrank the in-family explanation.
    for candidate in out:
        if not state.family:
            continue
        if family_of(candidate.root_cause) == state.family:
            candidate.score = min(0.99, candidate.score + 0.12)
        else:
            candidate.score = max(0.05, candidate.score - 0.08)

    # ---------------------------------------------------------------- anchors bias
    mentioned = {_app(state, name) for name in anchors.mentioned_apps}
    for candidate in out:
        if candidate.object in mentioned:
            candidate.score = min(0.99, candidate.score + 0.05)
        if candidate.object == f"namespace/{anchors.namespace}" and "quota" in candidate.root_cause:
            candidate.score = min(0.99, candidate.score + 0.02)
    return out


# ------------------------------------------------------------------ helpers


def _first(state: CaseState, prefix: str) -> str:
    for key, value in state.obs.items():
        if key.startswith(prefix) and not key.startswith("sig:"):
            return value
    return ""


def _scheduler_messages(state: CaseState) -> List[str]:
    """Only the scheduler's own FailedScheduling lines (never Deployment/Pod prose)."""
    messages: List[str] = []
    for key, value in state.obs.items():
        if key.startswith("sig:") or not value:
            continue
        if not (key.startswith("describe:") or key == "events"):
            continue
        for event in ev.parse_events(value):
            if event.get("reason") in {"FailedScheduling", "FailedCreatePodSandBox"}:
                messages.append(event["message"])
        if key == "events":
            for line in value.splitlines():
                if "FailedScheduling" in line:
                    messages.append(line.strip())
    return messages


def _cluster_nodes(state: CaseState) -> List[Dict[str, Any]]:
    raw = _first(state, "cluster")
    if not raw:
        return []
    try:
        import ast

        data = ast.literal_eval(raw)
    except Exception:
        return []
    return [node for node in (data.get("nodes") or []) if isinstance(node, dict)]


def _pod_constraints(describe_pod: str) -> Dict[str, Any]:
    selector: Dict[str, str] = {}
    for line in (describe_pod or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Node-Selectors:"):
            value = stripped.split(":", 1)[1].strip()
            if value and value != "<none>":
                for pair in value.split(","):
                    if "=" in pair:
                        key, _, val = pair.partition("=")
                        selector[key.strip()] = val.strip()
    return {"selector": selector, "has_affinity": bool(re.search(r"^\s*Affinity:", describe_pod or "", re.M)),
            "raw": describe_pod or ""}


def _blocking_t(taints: List[str], pod_raw: str) -> List[str]:
    """Non-standard taints the pod does not tolerate (cordon is handled separately)."""
    blocking = []
    for taint in taints or []:
        name = str(taint).split(":")[0]
        if name.startswith("node.kubernetes.io/"):
            continue            # cordon / not-ready / unreachable: own mechanisms
        if name not in (pod_raw or ""):
            blocking.append(str(taint))
    return blocking


def _scheduling_verdict(sched_text: str, pod: Dict[str, Any], nodes: List[Dict[str, Any]],
                        describe_pod: str) -> Optional[tuple[str, float, str]]:
    """Ordered scheduler semantics, using only scheduler text + node inventory.

      1. the scheduler names anti-affinity                  -> pod_anti_affinity_conflict
      2. a non-standard taint on a candidate, not tolerated  -> taint_toleration_mismatch
      3. a hard label constraint no node satisfies           -> node_selector_mismatch
      4. every label-compatible node is cordoned              -> node_cordon_mismatch
      5. the scheduler blames affinity while the pod declares
         no nodeSelector at all                               -> node_affinity_mismatch
      6. Insufficient <resource>                              -> *_capacity_mismatch

    Missing data must not become a verdict: nodes with an empty label set are treated as
    "unknown", not "does not match" (case 155 has an unlabelled tainted node).
    """
    if re.search(r"anti-affinity", sched_text, re.I):
        return ("pod_anti_affinity_conflict", 0.9, "scheduler reports pod anti-affinity rules blocking placement")

    # A standard unreachable/not-ready taint means the node stopped reporting: that is an
    # infrastructure symptom, not a toleration misconfiguration.  Leave it to the node
    # component probes instead of answering "taint".
    if re.search(r"node\.kubernetes\.io/(unreachable|not-ready)", sched_text, re.I):
        return None
    if re.search(r"untolerated taint", sched_text, re.I):
        return ("taint_toleration_mismatch", 0.9,
                "scheduler names an untolerated taint as the blocking reason")

    workers = [node for node in nodes if node.get("role") != "master"] if nodes else []
    selector = pod["selector"]
    labelled = [node for node in workers if node.get("labels")]
    unknown_label = [node for node in workers if not node.get("labels")]

    if selector:
        label_ok = [node for node in labelled
                    if all(str(node.get("labels", {}).get(key)) == value for key, value in selector.items())]
        if not label_ok and not unknown_label:
            wanted = ",".join(f"{key}={value}" for key, value in selector.items())
            return ("node_selector_mismatch", 0.92,
                    f"no node carries the pod's nodeSelector {wanted} "
                    f"(labels present: {sorted({k for node in labelled for k in node.get('labels', {})})})")
    else:
        label_ok = labelled or workers
        if pod["has_affinity"]:
            label_ok = [node for node in label_ok if _affinity_ok(describe_pod, node)]
            if not label_ok:
                return ("node_affinity_mismatch", 0.88, "node affinity rules match no node")

    candidates = label_ok or workers
    tainted = [node for node in candidates if _blocking_t(node.get("taints"), describe_pod)]
    if tainted:
        return ("taint_toleration_mismatch", 0.88,
                f"node {tainted[0]['name']} carries {tainted[0].get('taints')} which the pod does not tolerate")

    schedulable = [node for node in candidates
                   if not any("unschedulable" in str(t) for t in (node.get("taints") or []))]
    if candidates and not schedulable:
        return ("node_cordon_mismatch", 0.9,
                f"all {len(candidates)} label-compatible node(s) carry node.kubernetes.io/unschedulable")

    if not selector and re.search(r"node affinity/selector|node affinity", sched_text, re.I):
        # the scheduler blames affinity/selector but the pod has no nodeSelector: the
        # constraint must be an affinity rule that this snapshot does not render.
        return ("node_affinity_mismatch", 0.82,
                "scheduler blames node affinity/selector while the pod declares no nodeSelector")

    if re.search(r"Insufficient cpu", sched_text, re.I):
        return ("cpu_capacity_mismatch", 0.85, "scheduler reports insufficient CPU on candidate nodes")
    if re.search(r"Insufficient memory", sched_text, re.I):
        return ("memory_capacity_mismatch", 0.85, "scheduler reports insufficient memory on candidate nodes")
    return None


def _affinity_ok(describe_pod: str, node: Dict[str, Any]) -> bool:
    """Best-effort evaluation of rendered nodeAffinity matchExpressions (In/NotIn/Exists/DoesNotExist)."""
    labels = node.get("labels") or {}
    block = _section_affinity(describe_pod)
    if not block:
        return True
    for key, op, values in re.findall(r"key=([\w./-]+),?\s*operator=(In|NotIn|Exists|DoesNotExist)"
                                      r"(?:,?\s*values=\[([^\]]*)\])?", block):
        values = [item.strip().strip("'\"") for item in (values or "").split(",") if item.strip()]
        present = key in labels
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and present and labels.get(key) in values:
            return False
        if op == "Exists" and not present:
            return False
        if op == "DoesNotExist" and present:
            return False
    return True


def _section_affinity(text: str) -> str:
    index = str(text or "").find("Affinity:")
    if index < 0:
        return ""
    tail = str(text)[index:]
    stop = tail.find("Tolerations:")
    return tail[: stop if stop > 0 else 1200]


def _pvc_verdict(state: CaseState, sched_text: str) -> List[tuple[str, float, str]]:
    pvc_rows = ev.parse_table(_first(state, "pvcs"))
    pending = [row for row in pvc_rows if row.get("STATUS", "").lower() == "pending"]
    if not pending and "PersistentVolumeClaims" not in sched_text:
        return []
    if not pvc_rows:
        return []
    pv_rows = ev.parse_table(_first(state, "pvs"))
    sc_names = {row.get("NAME") for row in ev.parse_table(_first(state, "storage_classes"))}
    bound_elsewhere = [row for row in pvc_rows
                       if row.get("STATUS", "").lower() == "bound" and row not in pending]
    target = pending[0] if pending else {}
    target_class = target.get("STORAGECLASS", "") or ""

    findings: List[tuple[str, float, str]] = []
    # a PV already bound to a *different* PVC while ours is Pending -> the PV is occupied
    occupied = [pv for pv in pv_rows
                if pv.get("STATUS", "").lower() == "bound" and pv.get("CLAIM")
                and target.get("NAME") not in (pv.get("CLAIM") or "")]
    if bound_elsewhere and occupied:
        findings.append(("pv_binding_occupied", 0.9,
                         f"PV {occupied[0]['NAME']} is already bound to {occupied[0]['CLAIM']}; "
                         f"{target.get('NAME')} stays Pending"))
    # storage class: the claim asks for one the cluster or the PV cannot satisfy
    pv_classes = {pv.get("STORAGECLASS", "") for pv in pv_rows}
    if target_class and target_class not in sc_names:
        findings.append(("pvc_storage_class_mismatch", 0.9,
                         f"PVC asks for storageClassName {target_class!r} which is not in "
                         f"{sorted(sc_names) or 'any StorageClass'}"))
    elif target_class and pv_classes and target_class not in pv_classes:
        findings.append(("pvc_storage_class_mismatch", 0.85,
                         f"PVC storageClassName {target_class!r} != PV storageClassName {sorted(pv_classes)}"))
    labelled_pvs = [pv for pv in pv_rows if str(pv.get("LABELS") or "").strip() not in {"", "<none>"}]
    if not findings and labelled_pvs:
        findings.append(("pvc_selector_mismatch", 0.82,
                         f"PV {labelled_pvs[0]['NAME']} is labelled {labelled_pvs[0]['LABELS']} "
                         f"(a static-provisioning selector); the claim selects nothing"))
    if not findings and pending and pv_rows:
        # The claim's *requested* size/access mode is not exposed by any tool in this
        # benchmark, so capacity vs access-mode is not decidable here.  Emit both at the
        # same score and let jev(pick) / escalate handle it -- do not fake certainty.
        findings.append(("pvc_capacity_mismatch", 0.6,
                         "no PV satisfies the claim; requested size/access mode are not exposed by the tools "
                         "(capacity vs access-mode is undecidable on this surface; capacity listed first by "
                         "convention only)"))
        findings.append(("pvc_access_mode_mismatch", 0.6,
                         "no PV satisfies the claim; requested size/access mode are not exposed by the tools"))
    return findings


def _latency_graph(text: str) -> Dict[str, set]:
    """Parse the indented tree from GetServiceDependencies into child -> {parents}."""
    graph: Dict[str, set] = {}
    parent_of: Dict[int, str] = {}
    for line in str(text or "").splitlines():
        match = re.match(r"^(?P<indent>[\s│├└─]*)(?P<name>[a-z][a-z0-9-]+)(?:\s|<|$)", line)
        if not match:
            continue
        depth = len(match.group("indent")) // 4
        name = match.group("name")
        graph.setdefault(name, set())
        parent_of[depth] = name
        if depth > 0 and depth - 1 in parent_of:
            parent = parent_of[depth - 1]
            if parent != name:
                graph[name].add(parent)
    return graph


def _latency_leaf(state: CaseState, latency: Dict[str, float]) -> str:
    """The latency-spiked service that has no latency-spiked downstream dependency."""
    text = ""
    for key, value in state.obs.items():
        if key.startswith("deps") and not key.startswith("sig:") and value:
            text += value + "\n"
    graph = _latency_graph(text)
    spikes = {name for name in latency if name in VALID_SERVICES.get(SYSTEM_KEY.get(state.system, state.system), [])}
    if not spikes:
        return ""
    children_of: Dict[str, set] = {}
    for child, parents in graph.items():
        for parent in parents:
            children_of.setdefault(parent, set()).add(child)
    leaves = [name for name in spikes if not (children_of.get(name, set()) & spikes)]
    if leaves:
        return max(leaves, key=lambda name: latency.get(name, 0.0))
    return max(spikes, key=lambda name: latency.get(name, 0.0))


def _endpoints_empty_for(state: CaseState, app: str) -> bool:
    for row in ev.parse_table(_first(state, "endpoints")):
        if row.get("NAME") == app:
            return str(row.get("ENDPOINTS", "")).strip() in {"", "<none>"}
    return False


def _clean_text(text: str) -> str:
    return "" if str(text or "").lstrip().startswith("Error:") else str(text or "")


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text or "", re.I)
    return match.group(0).strip() if match else ""


def _has_node_selector(describe_pod: str) -> bool:
    for line in (describe_pod or "").splitlines():
        if line.strip().startswith("Node-Selectors:"):
            value = line.split(":", 1)[1].strip()
            return bool(value) and value not in {"<none>", "none"}
    return False


def _env_addresses(app_yaml: Dict[str, Any]) -> List[tuple[str, str]]:
    pairs: List[tuple[str, str]] = []
    for container in ev.yaml_containers(app_yaml):
        for item in container.get("env") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if re.search(r"_ADDR$|_URL$|_HOST$", name) and ":" in value:
                pairs.append((name, value))
    return pairs


def _probe_candidates(containers: List[Dict[str, Any]], describe_pod: str, app_yaml: Dict[str, Any]) -> List[tuple[str, float, str]]:
    """Compare probe declarations against each other and against the container port."""
    findings: List[tuple[str, float, str]] = []
    events = ev.parse_events(describe_pod)
    failed_readiness = any("readiness probe failed" in e["message"].lower() for e in events)
    failed_liveness = any("liveness probe failed" in e["message"].lower() for e in events)

    for container in containers:
        probes = container.get("probes") or {}
        if not probes:
            continue
        port_field = str(container.get("port") or "")
        container_port = int(m.group(1)) if (m := re.search(r"(\d+)", port_field)) else None
        readiness, liveness = probes.get("readiness"), probes.get("liveness")
        for kind, probe, failed in (("readiness", readiness, failed_readiness), ("liveness", liveness, failed_liveness)):
            if not probe:
                continue
            other = liveness if kind == "readiness" else readiness
            other_kind = "liveness" if kind == "readiness" else "readiness"
            # protocol mismatch: same port, different scheme between the two probes,
            # and the container serves a gRPC port (the other probe says grpc).
            if other and probe.get("port") == other.get("port") and probe.get("scheme") != other.get("scheme"):
                wrong_grpc = probe.get("scheme") != "grpc" and other.get("scheme") == "grpc"
                if wrong_grpc:
                    findings.append((
                        f"{kind}_probe_incorrect_protocol",
                        0.9 if failed else 0.55,
                        f"{kind} probe uses {probe.get('scheme')} on :{probe.get('port')} while the {other_kind} probe (same port) uses grpc",
                    ))
            all_ports = {int(m.group(1)) for other in containers
                         for m in [re.search(r"(\d+)", str(other.get("port") or ""))] if m}
            if (probe.get("port") and all_ports and probe["port"] not in all_ports):
                findings.append((
                    f"{kind}_probe_incorrect_port",
                    0.8 if failed else 0.45,
                    f"{kind} probe targets :{probe.get('port')} but the container listens on :{container_port}",
                ))
            # NB: probe *timing* faults need an app-start baseline this benchmark does not
            # expose; guessing from initialDelay produced only false positives (R18).

    # manifest cross-check: probe declared in the app YAML but missing in the live pod
    for container in ev.yaml_containers(app_yaml):
        for kind in ("readinessProbe", "livenessProbe"):
            probe = container.get(kind)
            if not isinstance(probe, dict):
                continue
            port = ((probe.get("httpGet") or {}).get("port") or (probe.get("grpc") or {}).get("port") or (probe.get("tcpSocket") or {}).get("port"))
            if isinstance(port, int) and container.get("ports"):
                declared = {int(p.get("containerPort", 0)) for p in container["ports"] if isinstance(p, dict)}
                if declared and port not in declared:
                    label = "readiness" if kind.startswith("readiness") else "liveness"
                    findings.append((f"{label}_probe_incorrect_port", 0.7, f"manifest {kind} targets :{port}, container exposes {sorted(declared)}"))
    deduped: Dict[str, tuple[str, float, str]] = {}
    for root_cause, score, why in findings:
        if root_cause not in deduped or score > deduped[root_cause][1]:
            deduped[root_cause] = (root_cause, score, why)
    return list(deduped.values())


def _to_ms(value: str) -> Optional[int]:
    match = re.fullmatch(r"(\d+)(ms|s)", str(value))
    if not match:
        return None
    amount = int(match.group(1))
    return amount * 1000 if match.group(2) == "s" else amount
