import os
import re
import time
import base64
import smtplib
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# ======================================================================
# ✅ NEW: ANALYSIS HELPERS (SAFE ADD — NO BREAK RISK)
# ======================================================================

def _calculate_health_score(cpu_waste, memory_waste, total_cost):
    if total_cost == 0:
        return 100
    waste_percent = ((cpu_waste + memory_waste) / total_cost) * 100
    return max(0, round(100 - waste_percent, 2))


def _generate_summary(total_cost, cpu_waste, memory_waste):
    savings = cpu_waste + memory_waste
    percent = (savings / total_cost * 100) if total_cost else 0

    return f"""
📊 PROJECT SUMMARY

- Total Cost: ${round(total_cost, 2)}
- Estimated Waste: ${round(savings, 2)}
- Potential Savings: ~{round(percent, 2)}%
"""


def _generate_insights(cpu_util, mem_util):
    insights = []

    if cpu_util < 20:
        insights.append("CPU utilization is low → over-provisioning")

    if mem_util < 20:
        insights.append("Memory utilization is low → over-provisioning")

    if not insights:
        insights.append("Resources look well optimized")

    return "\n".join(f"- {i}" for i in insights)


def _generate_recommendations(cpu_util, mem_util):
    recs = []

    if cpu_util < 20:
        recs.append("Reduce CPU allocation by 30–40%")

    if mem_util < 20:
        recs.append("Reduce Memory allocation by 20–30%")

    if not recs:
        recs.append("No optimization required")

    return "\n".join(f"- {r}" for r in recs)


def _generate_top_findings(cpu_util, mem_util):
    findings = []

    if cpu_util < 20:
        findings.append("High CPU over-provisioning detected")

    if mem_util < 20:
        findings.append("High Memory over-provisioning detected")

    findings.append("Cluster optimization opportunity exists")

    return "\n".join(f"{i+1}. {f}" for i, f in enumerate(findings[:3]))


# ======================================================================
# REPORT CONFIG
# ======================================================================

REPORT_PATH = Path(__file__).resolve().parents[1] / "Formal_RCA_Report.pdf"


# ======================================================================
# CLOUDOPTIX PROJECT CONTEXT HELPERS
# ======================================================================

def _get_cloudoptix_context() -> dict:
    """
    Reads project details submitted from UI through project_context.py.

    Expected UI values:
    - project_id
    - billing_table
    - namespace
    - region / location / zone
    - billing_project_id optional
    """
    try:
        from .project_context import get_project_context
        context = get_project_context()
        return context or {}
    except Exception:
        return {}


def _get_context_value(key: str, default=None):
    context = _get_cloudoptix_context()
    value = context.get(key)
    return value if value not in (None, "") else default


def _get_context_project_id() -> str | None:
    return _get_context_value("project_id")


def _get_context_billing_table() -> str | None:
    return _get_context_value("billing_table")


def _get_context_namespace(default: str = "default") -> str:
    return _get_context_value("namespace", default)


def _get_context_location() -> str | None:
    return (
        _get_context_value("location")
        or _get_context_value("region")
        or _get_context_value("zone")
    )


def _get_bigquery_client_project() -> str | None:
    """
    Billing BigQuery table can be in same project or separate FinOps/billing project.
    If UI provides billing_project_id, use it.
    Otherwise use project_id.
    """
    return _get_context_value("billing_project_id") or _get_context_project_id()


# ======================================================================
# FORMAT / PARSE HELPERS
# ======================================================================

def _parse_cpu_to_millicores(cpu_value: str | None) -> float:
    if not cpu_value:
        return 0.0

    raw_value = str(cpu_value).strip()

    if not raw_value:
        return 0.0

    try:
        if raw_value.endswith("m"):
            return max(0.0, float(raw_value[:-1]))

        if raw_value.endswith("n"):
            return max(0.0, float(raw_value[:-1]) / 1_000_000)

        if raw_value.endswith("u"):
            return max(0.0, float(raw_value[:-1]) / 1_000)

        return max(0.0, float(raw_value) * 1000)

    except Exception:
        return 0.0


def _parse_memory_to_bytes(memory_value: str | None) -> int:
    if not memory_value:
        return 0

    raw_value = str(memory_value).strip()

    if not raw_value:
        return 0

    match = re.match(r"^(?P<number>-?\d+(?:\.\d+)?)(?P<unit>[A-Za-z]+)?$", raw_value)

    if not match:
        return 0

    number = float(match.group("number"))
    unit = match.group("unit") or ""

    multipliers = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "Ti": 1024 ** 4,
        "Pi": 1024 ** 5,
        "Ei": 1024 ** 6,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "T": 1000 ** 4,
        "P": 1000 ** 5,
        "E": 1000 ** 6,
        "": 1,
    }

    multiplier = multipliers.get(unit)

    if multiplier is None:
        return 0

    return int(number * multiplier)


def _bytes_to_mebibytes(value: int | float) -> float:
    return float(value) / (1024 * 1024)


def _format_millicores_value(
    value: float | int | None,
    default: str = "not set",
    zero_is_value: bool = True,
) -> str:
    if value is None:
        return default

    numeric_value = float(value)

    if numeric_value < 0:
        return default

    if numeric_value == 0 and not zero_is_value:
        return default

    rounded = round(numeric_value, 1)
    return f"{int(rounded)}m" if rounded.is_integer() else f"{rounded}m"


def _format_kubectl_style_cpu(value: float | int | None, default: str = "not set") -> str:
    if value is None:
        return default

    numeric_value = float(value)

    if numeric_value < 0:
        return default

    rounded_up = (
        0
        if numeric_value == 0
        else max(
            1,
            int(numeric_value) if numeric_value.is_integer() else int(numeric_value) + 1,
        )
    )

    return f"{rounded_up}m"


def _format_mebibytes_value(
    value: float | int | None,
    default: str = "not set",
    zero_is_value: bool = True,
) -> str:
    if value is None:
        return default

    numeric_value = float(value)

    if numeric_value < 0:
        return default

    if numeric_value == 0 and not zero_is_value:
        return default

    rounded = round(numeric_value, 1)
    return f"{int(rounded)}Mi" if rounded.is_integer() else f"{rounded}Mi"


def _format_storage_bytes(
    value: int | float | None,
    default: str = "not set",
    zero_is_value: bool = True,
) -> str:
    if value is None:
        return default

    numeric_value = float(value)

    if numeric_value < 0:
        return default

    if numeric_value == 0 and not zero_is_value:
        return default

    tebibytes = numeric_value / (1024 ** 4)
    gibibytes = numeric_value / (1024 ** 3)
    mebibytes = numeric_value / (1024 ** 2)

    if tebibytes >= 1:
        rounded = round(tebibytes, 1)
        return f"{int(rounded)}Ti" if rounded.is_integer() else f"{rounded}Ti"

    if gibibytes >= 1:
        rounded = round(gibibytes, 1)
        return f"{int(rounded)}Gi" if rounded.is_integer() else f"{rounded}Gi"

    rounded = round(mebibytes, 1)
    return f"{int(rounded)}Mi" if rounded.is_integer() else f"{rounded}Mi"


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days}d")

    if hours:
        parts.append(f"{hours}h")

    if minutes:
        parts.append(f"{minutes}m")

    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def _percentile(values: list[float], percentile_value: int) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)

    rank = max(
        0,
        min(
            len(ordered) - 1,
            round((percentile_value / 100) * (len(ordered) - 1)),
        ),
    )

    return float(ordered[rank])


def _build_text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]

    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(str(value)))

    def format_row(values: list[str]) -> str:
        padded = [str(value).ljust(widths[index]) for index, value in enumerate(values)]
        return " | ".join(padded)

    separator = "-+-".join("-" * width for width in widths)

    lines = [format_row(headers), separator]
    lines.extend(format_row(row) for row in rows)

    return "\n".join(lines)


# ======================================================================
# KUBERNETES CLIENT HELPERS
# ======================================================================

def _load_kube_configuration():
    from kubernetes import config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _load_kube_clients():
    from kubernetes import client

    _load_kube_configuration()
    return client.CoreV1Api(), client.CustomObjectsApi()


def _load_all_kube_clients():
    from kubernetes import client

    _load_kube_configuration()
    return client.CoreV1Api(), client.CustomObjectsApi(), client.AppsV1Api()


def _get_pod_status(pod) -> str:
    for container_status in pod.status.container_statuses or []:
        waiting = getattr(container_status.state, "waiting", None)

        if waiting and waiting.reason:
            return waiting.reason

    return pod.status.phase or "Unknown"


def _derive_workload_name(pod) -> str:
    owner_references = getattr(pod.metadata, "owner_references", None) or []

    if owner_references:
        owner = owner_references[0]
        owner_name = owner.name or pod.metadata.name

        if owner.kind == "ReplicaSet":
            match = re.match(r"^(?P<deployment>.+)-[a-f0-9]{8,10}$", owner_name)

            if match:
                return match.group("deployment")

        return owner_name

    return pod.metadata.name


def _derive_workload_name_from_pod_name(pod_name: str) -> str:
    deployment_match = re.match(r"^(?P<workload>.+)-[a-f0-9]{8,10}-[a-z0-9]{5}$", pod_name)

    if deployment_match:
        return deployment_match.group("workload")

    statefulset_match = re.match(r"^(?P<workload>.+)-\d+$", pod_name)

    if statefulset_match:
        return statefulset_match.group("workload")

    return pod_name


def _get_active_cluster_location() -> str | None:
    configured_location = _get_context_location()

    if configured_location:
        return configured_location

    try:
        core_v1, _ = _load_kube_clients()
        nodes = core_v1.list_node(watch=False).items
    except Exception:
        return None

    zone_labels = set()

    for node in nodes:
        labels = getattr(node.metadata, "labels", {}) or {}
        zone_value = (
            labels.get("topology.kubernetes.io/zone")
            or labels.get("failure-domain.beta.kubernetes.io/zone")
        )

        if zone_value:
            zone_labels.add(zone_value)

    if len(zone_labels) == 1:
        return next(iter(zone_labels))

    return None


# ======================================================================
# CURRENT KUBERNETES RESOURCE COLLECTION
# ======================================================================

def _collect_pod_resource_samples(namespace: str | None = None) -> tuple[list[dict], str | None]:
    try:
        core_v1, custom_api = _load_kube_clients()
    except Exception as exc:
        return [], f"Kubernetes client configuration failed: {exc}"

    try:
        if namespace:
            metrics_response = custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
            )
        else:
            metrics_response = custom_api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
            )
    except Exception as exc:
        return [], f"Could not query metrics.k8s.io for pod usage: {exc}"

    metric_items = metrics_response.get("items", [])

    if not metric_items:
        return [], "The Kubernetes metrics API returned no pod metrics."

    try:
        if namespace:
            pod_items = core_v1.list_namespaced_pod(namespace, watch=False).items
            pvc_items = core_v1.list_namespaced_persistent_volume_claim(namespace, watch=False).items
        else:
            pod_items = core_v1.list_pod_for_all_namespaces(watch=False).items
            pvc_items = core_v1.list_persistent_volume_claim_for_all_namespaces(watch=False).items
    except Exception as exc:
        return [], f"Could not list pod specs: {exc}"

    pod_specs = {
        (pod.metadata.namespace, pod.metadata.name): pod
        for pod in pod_items
    }

    pvc_capacities = {
        (claim.metadata.namespace, claim.metadata.name): _parse_memory_to_bytes(
            (claim.status.capacity or {}).get("storage")
        )
        for claim in pvc_items
    }

    samples = []

    for item in metric_items:
        metadata = item.get("metadata", {})
        pod_namespace = metadata.get("namespace", namespace or "default")
        pod_name = metadata.get("name", "unknown-pod")
        sample_timestamp = item.get("timestamp", "unknown")
        sample_window = item.get("window", "unknown")
        containers = item.get("containers", [])

        usage_millicores = sum(
            _parse_cpu_to_millicores(container.get("usage", {}).get("cpu"))
            for container in containers
        )

        memory_usage_bytes = sum(
            _parse_memory_to_bytes(container.get("usage", {}).get("memory"))
            for container in containers
        )

        pod_spec = pod_specs.get((pod_namespace, pod_name))

        request_millicores = 0
        limit_millicores = 0
        memory_request_bytes = 0
        memory_limit_bytes = 0
        status = "Unknown"
        ready_text = "0/0"
        workload_name = pod_name
        pod_ip = "unknown"
        node_name = "unknown"
        restart_count = 0
        pvc_claim_names = []
        pvc_capacity_bytes = 0

        if pod_spec is not None:
            for container in pod_spec.spec.containers:
                resources = container.resources

                if resources and resources.requests:
                    request_millicores += _parse_cpu_to_millicores(resources.requests.get("cpu"))
                    memory_request_bytes += _parse_memory_to_bytes(resources.requests.get("memory"))

                if resources and resources.limits:
                    limit_millicores += _parse_cpu_to_millicores(resources.limits.get("cpu"))
                    memory_limit_bytes += _parse_memory_to_bytes(resources.limits.get("memory"))

            ready_count = sum(
                1
                for status_item in (pod_spec.status.container_statuses or [])
                if status_item.ready
            )

            total_count = len(pod_spec.status.container_statuses or [])

            ready_text = f"{ready_count}/{total_count}" if total_count else "0/0"
            status = _get_pod_status(pod_spec)
            workload_name = _derive_workload_name(pod_spec)
            pod_ip = getattr(pod_spec.status, "pod_ip", None) or "unknown"
            node_name = getattr(pod_spec.spec, "node_name", None) or "unknown"

            restart_count = sum(
                status_item.restart_count
                for status_item in (pod_spec.status.container_statuses or [])
            )

            for volume in pod_spec.spec.volumes or []:
                persistent_claim = getattr(volume, "persistent_volume_claim", None)

                if not persistent_claim or not persistent_claim.claim_name:
                    continue

                claim_name = persistent_claim.claim_name
                pvc_claim_names.append(claim_name)
                pvc_capacity_bytes += pvc_capacities.get((pod_namespace, claim_name), 0)

        samples.append(
            {
                "namespace": pod_namespace,
                "pod_name": pod_name,
                "workload_name": workload_name,
                "usage_millicores": usage_millicores,
                "memory_usage_bytes": memory_usage_bytes,
                "request_millicores": request_millicores,
                "limit_millicores": limit_millicores,
                "memory_request_bytes": memory_request_bytes,
                "memory_limit_bytes": memory_limit_bytes,
                "status": status,
                "ready": ready_text,
                "restart_count": restart_count,
                "pod_ip": pod_ip,
                "node_name": node_name,
                "pvc_claims": pvc_claim_names,
                "pvc_capacity_bytes": pvc_capacity_bytes,
                "sample_timestamp": sample_timestamp,
                "sample_window": sample_window,
            }
        )

    return samples, None


def _group_current_workload_resources(namespace: str) -> tuple[dict[str, dict], str | None]:
    try:
        core_v1, _, apps_v1 = _load_all_kube_clients()
    except Exception as exc:
        return {}, f"Kubernetes client configuration failed: {exc}"

    try:
        pods = core_v1.list_namespaced_pod(namespace, watch=False).items
    except Exception as exc:
        return {}, f"Could not list pods in namespace {namespace}: {exc}"

    grouped = defaultdict(
        lambda: {
            "pods": 0,
            "desired_replicas": None,
            "workload_kind": "Workload",
            "cpu_request_millicores": 0,
            "cpu_limit_millicores": 0,
            "memory_request_bytes": 0,
            "memory_limit_bytes": 0,
        }
    )

    try:
        deployments = apps_v1.list_namespaced_deployment(namespace, watch=False).items
    except Exception:
        deployments = []

    try:
        statefulsets = apps_v1.list_namespaced_stateful_set(namespace, watch=False).items
    except Exception:
        statefulsets = []

    for deployment in deployments:
        grouped[deployment.metadata.name]["desired_replicas"] = deployment.spec.replicas
        grouped[deployment.metadata.name]["workload_kind"] = "Deployment"

    for statefulset in statefulsets:
        grouped[statefulset.metadata.name]["desired_replicas"] = statefulset.spec.replicas
        grouped[statefulset.metadata.name]["workload_kind"] = "StatefulSet"

    for pod in pods:
        workload_name = _derive_workload_name(pod)
        workload = grouped[workload_name]
        workload["pods"] += 1

        for container in pod.spec.containers:
            resources = container.resources

            if not resources:
                continue

            if resources.requests:
                workload["cpu_request_millicores"] += _parse_cpu_to_millicores(resources.requests.get("cpu"))
                workload["memory_request_bytes"] += _parse_memory_to_bytes(resources.requests.get("memory"))

            if resources.limits:
                workload["cpu_limit_millicores"] += _parse_cpu_to_millicores(resources.limits.get("cpu"))
                workload["memory_limit_bytes"] += _parse_memory_to_bytes(resources.limits.get("memory"))

    return dict(grouped), None


# ======================================================================
# CLOUD MONITORING HISTORICAL METRICS
# ======================================================================

def _extract_monitoring_point_value(point) -> float:
    value_kind = point.value._pb.WhichOneof("value")

    if not value_kind:
        return 0.0

    return float(getattr(point.value, value_kind))


def _query_historical_metric_series(
    metric_type: str,
    namespace: str,
    days: int,
    aligner,
) -> tuple[dict[str, list[float]], str | None]:
    from google.cloud import monitoring_v3

    project_id = _get_context_project_id()

    if not project_id:
        return {}, "project_id is missing. Please submit project details from UI first."

    client = monitoring_v3.MetricServiceClient()
    now = int(time.time())

    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": now},
            "start_time": {"seconds": now - (days * 86400)},
        }
    )

    alignment_seconds = 21600 if days > 30 else 3600

    aggregation = monitoring_v3.Aggregation(
        {
            "alignment_period": {"seconds": alignment_seconds},
            "per_series_aligner": aligner,
        }
    )

    cluster_location = _get_active_cluster_location()

    metric_filter = (
        f'metric.type = "{metric_type}" '
        f'AND resource.labels.namespace_name = "{namespace}"'
    )

    if cluster_location:
        metric_filter += f' AND resource.labels.location = "{cluster_location}"'

    try:
        results = client.list_time_series(
            request={
                "name": f"projects/{project_id}",
                "filter": metric_filter,
                "interval": interval,
                "aggregation": aggregation,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
    except Exception as exc:
        return {}, str(exc)

    grouped = defaultdict(list)

    for series in results:
        pod_name = series.resource.labels.get("pod_name", "unknown-pod")
        container_name = series.resource.labels.get("container_name", "")

        if container_name == "POD":
            continue

        workload_name = _derive_workload_name_from_pod_name(pod_name)

        for point in series.points:
            point_value = _extract_monitoring_point_value(point)

            if point_value >= 0:
                grouped[workload_name].append(point_value)

    return dict(grouped), None


def _query_pod_metric_series_since_timestamp(
    metric_type: str,
    namespace: str,
    start_timestamp_seconds: int,
    aligner,
    alignment_seconds: int,
    pod_names: set[str],
) -> tuple[dict[str, list[float]], str | None]:
    from google.cloud import monitoring_v3

    project_id = _get_context_project_id()

    if not project_id:
        return {}, "project_id is missing. Please submit project details from UI first."

    client = monitoring_v3.MetricServiceClient()
    now = int(time.time())

    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": now},
            "start_time": {"seconds": start_timestamp_seconds},
        }
    )

    aggregation = monitoring_v3.Aggregation(
        {
            "alignment_period": {"seconds": alignment_seconds},
            "per_series_aligner": aligner,
        }
    )

    cluster_location = _get_active_cluster_location()

    metric_filter = (
        f'metric.type = "{metric_type}" '
        f'AND resource.labels.namespace_name = "{namespace}"'
    )

    if cluster_location:
        metric_filter += f' AND resource.labels.location = "{cluster_location}"'

    try:
        results = client.list_time_series(
            request={
                "name": f"projects/{project_id}",
                "filter": metric_filter,
                "interval": interval,
                "aggregation": aggregation,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
    except Exception as exc:
        return {}, str(exc)

    grouped = defaultdict(list)

    for series in results:
        pod_name = series.resource.labels.get("pod_name", "unknown-pod")

        if pod_name not in pod_names:
            continue

        container_name = series.resource.labels.get("container_name", "")

        if container_name == "POD":
            continue

        for point in series.points:
            point_value = _extract_monitoring_point_value(point)

            if point_value >= 0:
                grouped[pod_name].append(point_value)

    return dict(grouped), None


# ======================================================================
# LIVE COST SNAPSHOT
# ======================================================================

def fetch_cost_optimization_snapshot(namespace: str = "default"):
    namespace = namespace or _get_context_namespace("default")

    if namespace == "default":
        namespace = _get_context_namespace("default")

    samples, error = _collect_pod_resource_samples(namespace=namespace)

    if error:
        return f"COST OPTIMIZATION SNAPSHOT: unavailable. {error}"

    app_samples = [sample for sample in samples if sample["namespace"] == namespace]

    if not app_samples:
        return f"COST OPTIMIZATION SNAPSHOT: no pod metrics were returned for namespace {namespace}."

    grouped = defaultdict(
        lambda: {
            "pods": 0,
            "usage_millicores": 0,
            "memory_usage_bytes": 0,
            "request_millicores": 0,
            "limit_millicores": 0,
            "memory_request_bytes": 0,
            "memory_limit_bytes": 0,
            "pvc_capacity_bytes": 0,
            "statuses": [],
        }
    )

    for sample in app_samples:
        workload = grouped[sample["workload_name"]]
        workload["pods"] += 1
        workload["usage_millicores"] += sample["usage_millicores"]
        workload["memory_usage_bytes"] += sample["memory_usage_bytes"]
        workload["request_millicores"] += sample["request_millicores"]
        workload["limit_millicores"] += sample["limit_millicores"]
        workload["memory_request_bytes"] += sample["memory_request_bytes"]
        workload["memory_limit_bytes"] += sample["memory_limit_bytes"]
        workload["pvc_capacity_bytes"] += sample["pvc_capacity_bytes"]
        workload["statuses"].append(sample["status"])

    sample_timestamp = next(
        (sample["sample_timestamp"] for sample in app_samples if sample.get("sample_timestamp")),
        "unknown",
    )

    sample_window = next(
        (sample["sample_window"] for sample in app_samples if sample.get("sample_window")),
        "unknown",
    )

    sections = [
        "## Cost Optimization Snapshot",
        f"- Project ID: {_get_context_project_id() or 'not provided from UI'}",
        f"- Namespace: {namespace}",
        "- Scope: live Kubernetes metrics and pod specs only.",
        f"- Sample Time: {sample_timestamp}",
        f"- Sample Window: {sample_window}",
        "",
    ]

    workload_rows = []

    for workload_name in sorted(grouped):
        workload = grouped[workload_name]

        request_millicores = workload["request_millicores"]
        usage_millicores = workload["usage_millicores"]
        memory_usage_mib = _bytes_to_mebibytes(workload["memory_usage_bytes"])
        memory_request_bytes = workload["memory_request_bytes"]
        pvc_capacity_bytes = workload["pvc_capacity_bytes"]

        unhealthy = [
            status
            for status in workload["statuses"]
            if status not in {"Running", "Completed", "Succeeded"}
        ]

        if request_millicores <= 0:
            utilization_text = "unavailable"
            recommendation = "Define CPU requests before rightsizing."
        else:
            utilization = round((usage_millicores / request_millicores) * 100, 1)
            utilization_text = f"{utilization}%"

            if unhealthy:
                recommendation = "Do not optimize yet; stabilize unhealthy pods first."
            elif utilization <= 20:
                recommendation = "Likely over-provisioned in current snapshot."
            elif utilization <= 60:
                recommendation = "Normal operating band in snapshot."
            else:
                recommendation = "Usage is meaningful; preserve headroom."

        workload_rows.append(
            [
                workload_name,
                str(workload["pods"]),
                _format_kubectl_style_cpu(usage_millicores),
                _format_mebibytes_value(memory_usage_mib),
                _format_millicores_value(request_millicores or None, zero_is_value=False),
                _format_storage_bytes(memory_request_bytes or None, zero_is_value=False),
                _format_storage_bytes(pvc_capacity_bytes or None, zero_is_value=False),
                utilization_text,
                "blocked" if unhealthy else "clear",
                recommendation,
            ]
        )

    sections.append("### Workloads")

    sections.append(
        "```text\n"
        + _build_text_table(
            [
                "Workload",
                "Pods",
                "Current CPU",
                "Current Memory",
                "CPU Request",
                "Memory Request",
                "PVC Capacity",
                "CPU Utilization",
                "Status",
                "Recommendation",
            ],
            workload_rows,
        )
        + "\n```"
    )

    pod_resource_rows = []
    pod_infra_rows = []

    for sample in sorted(app_samples, key=lambda item: item["pod_name"]):
        pod_resource_rows.append(
            [
                sample["pod_name"],
                _format_kubectl_style_cpu(sample["usage_millicores"]),
                _format_mebibytes_value(_bytes_to_mebibytes(sample["memory_usage_bytes"])),
                _format_millicores_value(sample["request_millicores"] or None, zero_is_value=False),
                _format_storage_bytes(sample["memory_request_bytes"] or None, zero_is_value=False),
                sample["ready"],
                sample["status"],
                str(sample["restart_count"]),
            ]
        )

        pod_infra_rows.append(
            [
                sample["pod_name"],
                sample["node_name"],
                sample["pod_ip"],
                ", ".join(sample["pvc_claims"]) if sample["pvc_claims"] else "none",
                _format_storage_bytes(sample["pvc_capacity_bytes"] or None, zero_is_value=False),
            ]
        )

    sections.append("")
    sections.append("### Exact Pod Resource Details")

    sections.append(
        "```text\n"
        + _build_text_table(
            [
                "Pod",
                "Current CPU",
                "Current Memory",
                "CPU Request",
                "Memory Request",
                "Ready",
                "Status",
                "Restarts",
            ],
            pod_resource_rows,
        )
        + "\n```"
    )

    sections.append("")
    sections.append("### Exact Pod Placement And Storage Details")

    sections.append(
        "```text\n"
        + _build_text_table(
            ["Pod", "Node", "Pod IP", "PVC Claims", "PVC Capacity"],
            pod_infra_rows,
        )
        + "\n```"
    )

    return "\n".join(sections)


def fetch_all_workload_statuses(namespace: str = "default"):
    namespace = namespace or _get_context_namespace("default")

    if namespace == "default":
        namespace = _get_context_namespace("default")

    return fetch_cost_optimization_snapshot(namespace=namespace)


# ======================================================================
# HISTORICAL RESOURCE ANALYSIS
# ======================================================================

def _build_historical_window_candidates(requested_days: int) -> list[int]:
    primary_window = min(requested_days, 60)

    candidates = [primary_window]

    if primary_window > 30:
        candidates.append(30)

    if primary_window > 7:
        candidates.append(7)

    return candidates


def fetch_historical_resource_analysis(namespace: str = "default", days: int = 60):
    from google.cloud import monitoring_v3

    namespace = namespace or _get_context_namespace("default")

    if namespace == "default":
        namespace = _get_context_namespace("default")

    try:
        requested_days = int(days)
    except Exception:
        return "HISTORICAL RESOURCE ANALYSIS: invalid days value."

    if requested_days < 1:
        return "HISTORICAL RESOURCE ANALYSIS: days must be greater than zero."

    if requested_days > 60:
        requested_days = 60

    current_resources, current_resource_error = _group_current_workload_resources(namespace)

    if current_resource_error:
        return f"HISTORICAL RESOURCE ANALYSIS: unavailable. {current_resource_error}"

    if not current_resources:
        return f"HISTORICAL RESOURCE ANALYSIS: no workloads found in namespace {namespace}."

    selected_cpu_series = {}
    selected_memory_series = {}
    selected_days = requested_days
    selected_cpu_error = None
    selected_memory_error = None

    for candidate_days in _build_historical_window_candidates(requested_days):
        cpu_series, cpu_error = _query_historical_metric_series(
            metric_type="kubernetes.io/container/cpu/core_usage_time",
            namespace=namespace,
            days=candidate_days,
            aligner=monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
        )

        memory_series, memory_error = _query_historical_metric_series(
            metric_type="kubernetes.io/container/memory/used_bytes",
            namespace=namespace,
            days=candidate_days,
            aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
        )

        matched_workloads = (set(cpu_series) | set(memory_series)) & set(current_resources)

        selected_cpu_series = cpu_series
        selected_memory_series = memory_series
        selected_days = candidate_days
        selected_cpu_error = cpu_error
        selected_memory_error = memory_error

        if matched_workloads:
            break

    sections = [
        "## Historical Resource Analysis",
        f"- Project ID: {_get_context_project_id() or 'not provided from UI'}",
        f"- Namespace: {namespace}",
        f"- Requested Window: {requested_days}d",
        f"- Analysis Window Used: {selected_days}d",
        "- Data Source: Google Cloud Monitoring plus current Kubernetes resource settings.",
        "",
    ]

    if selected_cpu_error:
        sections.append(f"- CPU History Warning: {selected_cpu_error}")

    if selected_memory_error:
        sections.append(f"- Memory History Warning: {selected_memory_error}")

    history_rows = []
    recommendation_rows = []

    for workload_name in sorted(current_resources):
        resource_state = current_resources[workload_name]

        cpu_values_millicores = [
            value * 1000
            for value in selected_cpu_series.get(workload_name, [])
        ]

        memory_values_bytes = selected_memory_series.get(workload_name, [])

        cpu_p50 = _percentile(cpu_values_millicores, 50)
        cpu_p95 = _percentile(cpu_values_millicores, 95)
        cpu_peak = max(cpu_values_millicores, default=0.0)

        memory_p50_mib = _percentile(
            [value / (1024 * 1024) for value in memory_values_bytes],
            50,
        )

        memory_p95_mib = _percentile(
            [value / (1024 * 1024) for value in memory_values_bytes],
            95,
        )

        memory_peak_mib = max(
            (value / (1024 * 1024) for value in memory_values_bytes),
            default=0.0,
        )

        pods = max(resource_state.get("pods", 0), 1)

        per_pod_cpu_request = resource_state["cpu_request_millicores"] / pods
        per_pod_memory_request_mib = (
            resource_state["memory_request_bytes"] / pods / (1024 * 1024)
            if resource_state["memory_request_bytes"]
            else 0
        )

        suggested_cpu = "keep current"
        suggested_mem = "keep current"
        confidence = "unavailable"

        if cpu_values_millicores:
            confidence = "medium"
            suggested_cpu_value = max(cpu_p95 * 1.25, cpu_p50 * 1.4, 25)

            if per_pod_cpu_request > 0 and suggested_cpu_value < per_pod_cpu_request * 0.9:
                suggested_cpu = _format_millicores_value(suggested_cpu_value)

        if memory_values_bytes:
            confidence = "medium"
            suggested_mem_value = max(memory_p95_mib * 1.2, memory_p50_mib * 1.35, 64)

            if per_pod_memory_request_mib > 0 and suggested_mem_value < per_pod_memory_request_mib * 0.9:
                suggested_mem = _format_mebibytes_value(suggested_mem_value)

        if len(cpu_values_millicores) >= 100 and len(memory_values_bytes) >= 100:
            confidence = "high"

        history_rows.append(
            [
                workload_name,
                resource_state.get("workload_kind", "Workload"),
                str(resource_state["pods"]),
                _format_millicores_value(resource_state["cpu_request_millicores"] or None, zero_is_value=False),
                _format_millicores_value(resource_state["cpu_limit_millicores"] or None, zero_is_value=False),
                _format_millicores_value(cpu_p50, default="not set", zero_is_value=bool(cpu_values_millicores)),
                _format_millicores_value(cpu_p95, default="not set", zero_is_value=bool(cpu_values_millicores)),
                _format_millicores_value(cpu_peak, default="not set", zero_is_value=bool(cpu_values_millicores)),
                _format_mebibytes_value(
                    resource_state["memory_request_bytes"] / (1024 * 1024)
                    if resource_state["memory_request_bytes"]
                    else None,
                    zero_is_value=False,
                ),
                _format_mebibytes_value(memory_p50_mib, default="not set", zero_is_value=bool(memory_values_bytes)),
                _format_mebibytes_value(memory_p95_mib, default="not set", zero_is_value=bool(memory_values_bytes)),
                _format_mebibytes_value(memory_peak_mib, default="not set", zero_is_value=bool(memory_values_bytes)),
                f"CPU {len(cpu_values_millicores)}, Mem {len(memory_values_bytes)}",
            ]
        )

        recommendation_rows.append(
            [
                workload_name,
                suggested_cpu,
                suggested_mem,
                confidence,
                "Review with owner before applying changes.",
            ]
        )

    sections.append("### Historical Resource Profile")

    sections.append(
        "```text\n"
        + _build_text_table(
            [
                "Workload",
                "Type",
                "Pods",
                "CPU Req",
                "CPU Limit",
                "CPU p50",
                "CPU p95",
                "CPU Peak",
                "Mem Req",
                "Mem p50",
                "Mem p95",
                "Mem Peak",
                "Coverage Points",
            ],
            history_rows,
        )
        + "\n```"
    )

    sections.append("")
    sections.append("### Rightsizing Recommendations")

    sections.append(
        "```text\n"
        + _build_text_table(
            [
                "Workload",
                "Suggested CPU Req",
                "Suggested Mem Req",
                "Confidence",
                "Approval Note",
            ],
            recommendation_rows,
        )
        + "\n```"
    )

    return "\n".join(sections)


# ======================================================================
# POD USAGE SINCE START
# ======================================================================

def fetch_pod_usage_since_start(namespace: str = "default"):
    from google.cloud import monitoring_v3

    namespace = namespace or _get_context_namespace("default")

    if namespace == "default":
        namespace = _get_context_namespace("default")

    try:
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return f"POD USAGE SINCE START: unavailable. Kubernetes config failed: {exc}"

    try:
        pods = core_v1.list_namespaced_pod(namespace, watch=False).items
    except Exception as exc:
        return f"POD USAGE SINCE START: could not list pods in namespace {namespace}: {exc}"

    if not pods:
        return f"POD USAGE SINCE START: no pods found in namespace {namespace}."

    current_pods = []

    for pod in pods:
        start_time = getattr(pod.status, "start_time", None) or getattr(pod.metadata, "creation_timestamp", None)

        if start_time:
            current_pods.append(pod)

    if not current_pods:
        return f"POD USAGE SINCE START: no pod start timestamps found in namespace {namespace}."

    earliest_start = min(
        pod.status.start_time or pod.metadata.creation_timestamp
        for pod in current_pods
    )

    earliest_start_seconds = int(earliest_start.timestamp())
    pod_names = {pod.metadata.name for pod in current_pods}
    alignment_seconds = 300

    cpu_series, cpu_error = _query_pod_metric_series_since_timestamp(
        metric_type="kubernetes.io/container/cpu/core_usage_time",
        namespace=namespace,
        start_timestamp_seconds=earliest_start_seconds,
        aligner=monitoring_v3.Aggregation.Aligner.ALIGN_DELTA,
        alignment_seconds=alignment_seconds,
        pod_names=pod_names,
    )

    memory_mean_series, memory_error = _query_pod_metric_series_since_timestamp(
        metric_type="kubernetes.io/container/memory/used_bytes",
        namespace=namespace,
        start_timestamp_seconds=earliest_start_seconds,
        aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
        alignment_seconds=alignment_seconds,
        pod_names=pod_names,
    )

    live_samples, live_error = _collect_pod_resource_samples(namespace=namespace)
    live_samples_by_pod = {sample["pod_name"]: sample for sample in live_samples}

    sections = [
        "## Pod Usage Since Start",
        f"- Project ID: {_get_context_project_id() or 'not provided from UI'}",
        f"- Namespace: {namespace}",
        "- Definition: current pod instances from pod start time until now.",
        f"- Alignment Window: {alignment_seconds}s",
        "",
    ]

    if cpu_error:
        sections.append(f"- CPU History Warning: {cpu_error}")

    if memory_error:
        sections.append(f"- Memory History Warning: {memory_error}")

    if live_error:
        sections.append(f"- Live Snapshot Warning: {live_error}")

    now = datetime.now(timezone.utc)

    pod_rows = []

    for pod in sorted(current_pods, key=lambda item: item.metadata.name):
        pod_name = pod.metadata.name
        start_time = pod.status.start_time or pod.metadata.creation_timestamp
        age_seconds = max(1.0, (now - start_time).total_seconds())

        memory_values = memory_mean_series.get(pod_name, [])
        cpu_values = cpu_series.get(pod_name, [])

        live_sample = live_samples_by_pod.get(pod_name, {})
        current_cpu = live_sample.get("usage_millicores")

        average_memory_mib = (
            _bytes_to_mebibytes(sum(memory_values) / len(memory_values))
            if memory_values
            else None
        )

        cpu_core_seconds = sum(cpu_values)

        average_cpu_millicores = (
            (cpu_core_seconds / age_seconds) * 1000
            if cpu_values
            else None
        )

        pod_rows.append(
            [
                pod_name,
                _format_duration(age_seconds),
                _format_kubectl_style_cpu(current_cpu),
                _format_millicores_value(average_cpu_millicores, default="unavailable"),
                _format_mebibytes_value(average_memory_mib, default="unavailable"),
            ]
        )

    sections.append("### Pods")

    sections.append(
        "```text\n"
        + _build_text_table(
            [
                "Pod",
                "Pod Age",
                "Current CPU",
                "Avg CPU Since Start",
                "Avg Memory Since Start",
            ],
            pod_rows,
        )
        + "\n```"
    )

    return "\n".join(sections)


# ======================================================================
# INFRASTRUCTURE EVALUATION
# ======================================================================

def evaluate_cluster_infrastructure(namespace: str = "default") -> str:
    namespace = namespace or _get_context_namespace("default")

    if namespace == "default":
        namespace = _get_context_namespace("default")

    try:
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return f"Kubernetes client configuration failed: {exc}"

    sections = [
        "## Infrastructure Evaluation Snapshot",
        f"- Project ID: {_get_context_project_id() or 'not provided from UI'}",
        f"- Namespace: {namespace}",
        "",
    ]

    try:
        nodes = core_v1.list_node().items
        node_rows = []

        for node in nodes:
            node_rows.append(
                [
                    node.metadata.name,
                    node.status.allocatable.get("cpu", "unknown"),
                    node.status.allocatable.get("memory", "unknown"),
                ]
            )

        sections.append("### Nodes")

        if node_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Node", "Allocatable CPU", "Allocatable Memory"],
                    node_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No nodes found.")

    except Exception as exc:
        sections.append(f"Could not list nodes: {exc}")

    sections.append("")
    sections.append("### Persistent Volume Claims")

    try:
        pvcs = core_v1.list_namespaced_persistent_volume_claim(namespace).items

        if not pvcs:
            sections.append("No PersistentVolumeClaims found.")
        else:
            pvc_rows = []

            for pvc in pvcs:
                pvc_rows.append(
                    [
                        pvc.metadata.name,
                        pvc.spec.resources.requests.get("storage", "unknown"),
                        pvc.status.phase,
                    ]
                )

            sections.append(
                "```text\n"
                + _build_text_table(
                    ["PVC", "Requested Storage", "Status"],
                    pvc_rows,
                )
                + "\n```"
            )

    except Exception as exc:
        sections.append(f"Could not list PVCs: {exc}")

    return "\n".join(sections)


# ======================================================================
# BILLING VS UTILIZATION HEURISTIC
# ======================================================================

def analyze_billing_vs_utilization(namespace: str = "default") -> str:
    from google.cloud import monitoring_v3
    import time

    namespace = namespace or _get_context_namespace("default")

    if namespace == "default":
        namespace = _get_context_namespace("default")

    project_id = _get_context_project_id()

    if not project_id:
        return "❌ Project ID missing. Please enter project details in UI."

    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/{project_id}"

    now = int(time.time())

    interval = monitoring_v3.TimeInterval({
        "end_time": {"seconds": now},
        "start_time": {"seconds": now - 3600},
    })

    # ✅ Common metric fetch function
    def get_metric(metric):
        try:
            aggregation = monitoring_v3.Aggregation({
                "alignment_period": {"seconds": 60},
                "per_series_aligner": (
                    monitoring_v3.Aggregation.Aligner.ALIGN_RATE
                    if "cpu" in metric
                    else monitoring_v3.Aggregation.Aligner.ALIGN_MEAN
                )
            })

            results = client.list_time_series(
                request={
                    "name": project_name,
                    "filter": f'metric.type="{metric}" AND resource.labels.namespace_name="{namespace}"',
                    "interval": interval,
                    "aggregation": aggregation,
                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                }
            )

            total = 0.0
            count = 0

            for series in results:
                for point in series.points:
                    value_kind = point.value._pb.WhichOneof("value")
                    value = getattr(point.value, value_kind)

                    if value >= 0:
                        total += value
                        count += 1

            return (total / count) if count else 0.0

        except Exception as e:
            print("⚠️ Metric fetch error:", e)
            return 0.0

    # ✅ Fetch metrics
    cpu_used = get_metric("kubernetes.io/container/cpu/core_usage_time")
    cpu_requested_raw = get_metric("kubernetes.io/container/cpu/request_cores")

    mem_used = get_metric("kubernetes.io/container/memory/used_bytes")
    mem_requested = get_metric("kubernetes.io/container/memory/request_bytes")

    # ✅ Convert memory
    mem_used_gb = mem_used / (1024 ** 3)
    mem_requested_gb = mem_requested / (1024 ** 3)

    # ✅ HANDLE MISSING CPU REQUEST (NO HARDCODING ✅)
    cpu_requested = cpu_requested_raw if cpu_requested_raw > 0 else None

    # ✅ UTILIZATION (SAFE)
    cpu_util = (
        (cpu_used / cpu_requested * 100)
        if cpu_requested is not None
        else None
    )

    mem_util = (
        (mem_used_gb / mem_requested_gb * 100)
        if mem_requested_gb > 0
        else 0
    )

    # ✅ WASTE (SAFE)
    cpu_waste = (
        max(0, 100 - cpu_util)
        if cpu_util is not None
        else None
    )

    mem_waste = max(0, 100 - mem_util)

    # ✅ OVERALL
    valid_values = [v for v in [cpu_waste, mem_waste] if v is not None]
    overall = sum(valid_values) / len(valid_values) if valid_values else 0

    health = max(0, 100 - overall)

    # ✅ FORMAT TEXT SAFELY
    def fmt(val, suffix=""):
        return f"{val:.2f}{suffix}" if val is not None else "Not available"

    cpu_requested_text = (
        f"{cpu_requested:.4f} cores" if cpu_requested is not None else "Not available"
    )

    # ✅ WARNING MESSAGE
    warning_text = ""
    if cpu_requested is None:
        warning_text = "⚠️ CPU request metrics not available from Cloud Monitoring.\n"

    summary = f"""
📊 PROJECT SUMMARY

- CPU Waste: {fmt(cpu_waste, "%")}
- Memory Waste: {fmt(mem_waste, "%")}
- Overall Inefficiency: {overall:.2f}%
"""

    return f"""
{summary}

🏥 CLUSTER HEALTH SCORE
- Score: {health:.2f} / 100

{warning_text}

--------------------------------------------------

## Cloud Monitoring Based Analysis ✅

- Project ID: {project_id}
- Namespace: {namespace}

### Resource Usage

- CPU Used: {cpu_used:.4f} cores
- CPU Requested: {cpu_requested_text}
- Memory Used: {mem_used_gb:.4f} GiB
- Memory Requested: {mem_requested_gb:.4f} GiB

### Utilization

- CPU Utilization: {fmt(cpu_util, "%")}
- Memory Utilization: {mem_util:.2f}%

✅ No kubectl dependency
✅ Works for private clusters
✅ Uses Cloud Monitoring API
"""

# ======================================================================
# KUBERNETES SECRET UTILITY
# ======================================================================

def manage_kubernetes_secret(
    action: str,
    namespace: str,
    secret_name: str,
    key_values: str = None,
    approved: bool = False,
) -> str:
    from kubernetes import client

    namespace = namespace or _get_context_namespace("default")

    if not approved:
        return (
            "APPROVAL REQUIRED: Creating or deleting Kubernetes secrets is a write operation. "
            "Please confirm approval before executing."
        )

    try:
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return f"Kubernetes client configuration failed: {exc}"

    if action == "delete":
        try:
            core_v1.delete_namespaced_secret(secret_name, namespace)
            return f"SUCCESS: Secret '{secret_name}' deleted from namespace '{namespace}'."
        except Exception as exc:
            return f"Failed to delete secret: {exc}"

    if action == "create":
        if not key_values:
            return "ERROR: key_values is required to create a secret."

        try:
            data_dict = {}

            for pair in key_values.split(","):
                key, value = pair.split("=", 1)
                data_dict[key.strip()] = base64.b64encode(value.strip().encode("utf-8")).decode("utf-8")

            secret = client.V1Secret(
                api_version="v1",
                kind="Secret",
                metadata=client.V1ObjectMeta(name=secret_name),
                data=data_dict,
            )

            core_v1.create_namespaced_secret(namespace=namespace, body=secret)

            return f"SUCCESS: Secret '{secret_name}' created in namespace '{namespace}'."

        except Exception as exc:
            return f"Failed to create secret: {exc}"

    return "ERROR: Invalid action. Use 'create' or 'delete'."


# ======================================================================
# BASIC GCP RESOURCE DISCOVERY
# ======================================================================

def scan_gcp_resources(project_id: str = None) -> str:
    from google.cloud import compute_v1, storage

    project_id = project_id or _get_context_project_id()

    if not project_id:
        return "GCP SCAN FAILED: project_id is missing. Please submit project details from UI first."

    sections = [
        "## GCP Resource Discovery",
        f"- Project ID: {project_id}",
        "- Scope: Compute Engine Instances and Cloud Storage Buckets.",
        "",
    ]

    try:
        storage_client = storage.Client(project=project_id)
        buckets = list(storage_client.list_buckets())

        bucket_rows = []

        for bucket in buckets:
            bucket_rows.append(
                [
                    bucket.name,
                    bucket.location,
                    bucket.storage_class,
                ]
            )

        sections.append("### Cloud Storage Buckets")

        if bucket_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Bucket Name", "Location", "Default Storage Class"],
                    bucket_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No storage buckets found.")

    except Exception as exc:
        sections.append(f"Could not scan Cloud Storage buckets: {exc}")

    sections.append("")

    try:
        compute_client = compute_v1.InstancesClient()
        request = compute_v1.AggregatedListInstancesRequest(project=project_id)
        iterator = compute_client.aggregated_list(request=request)

        vm_rows = []

        for zone, response in iterator:
            if response.instances:
                zone_name = zone.split("/")[-1]

                for instance in response.instances:
                    machine_type = instance.machine_type.split("/")[-1]

                    vm_rows.append(
                        [
                            instance.name,
                            zone_name,
                            machine_type,
                            instance.status,
                        ]
                    )

        sections.append("### Compute Engine VMs")

        if vm_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["VM Name", "Zone", "Machine Type", "Status"],
                    vm_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No standalone VMs found.")

    except Exception as exc:
        sections.append(f"Could not scan Compute Engine VMs: {exc}")

    return "\n".join(sections)


# ======================================================================
# FULL GCP RESOURCE DISCOVERY
# ======================================================================

def scan_full_gcp_resources(project_id: str = None) -> str:
    """
    Full GCP Resource Discovery for CloudOptix.

    Scans:
    - Cloud Storage Buckets
    - Compute Engine VMs
    - VPC Networks
    - Subnets
    - Firewall Rules
    - GKE Clusters
    - Artifact Registry repositories
    """

    from google.cloud import compute_v1
    from google.cloud import storage

    project_id = project_id or _get_context_project_id()
    location = _get_context_location() or "us-central1"

    if not project_id:
        return "FULL GCP SCAN FAILED: project_id is missing. Please submit project details from UI first."

    sections = [
        "## Full GCP Resource Discovery",
        f"- Project ID: {project_id}",
        f"- Artifact Registry Location Used: {location}",
        "",
    ]

    total_count = 0

    # ------------------------------------------------------------------
    # Cloud Storage Buckets
    # ------------------------------------------------------------------
    try:
        storage_client = storage.Client(project=project_id)
        buckets = list(storage_client.list_buckets())

        bucket_rows = []

        for bucket in buckets:
            bucket_rows.append(
                [
                    bucket.name,
                    bucket.location,
                    bucket.storage_class,
                ]
            )

        total_count += len(bucket_rows)

        sections.append("### Cloud Storage Buckets")

        if bucket_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Bucket Name", "Location", "Storage Class"],
                    bucket_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No Cloud Storage buckets found.")

    except Exception as exc:
        sections.append(f"Cloud Storage scan failed: {exc}")

    sections.append("")

    # ------------------------------------------------------------------
    # Compute Engine VMs
    # ------------------------------------------------------------------
    try:
        compute_client = compute_v1.InstancesClient()
        request = compute_v1.AggregatedListInstancesRequest(project=project_id)
        iterator = compute_client.aggregated_list(request=request)

        vm_rows = []

        for zone, response in iterator:
            if response.instances:
                zone_name = zone.split("/")[-1]

                for instance in response.instances:
                    vm_rows.append(
                        [
                            instance.name,
                            zone_name,
                            instance.machine_type.split("/")[-1],
                            instance.status,
                        ]
                    )

        total_count += len(vm_rows)

        sections.append("### Compute Engine VMs")

        if vm_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["VM Name", "Zone", "Machine Type", "Status"],
                    vm_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No Compute Engine VMs found.")

    except Exception as exc:
        sections.append(f"Compute Engine VM scan failed: {exc}")

    sections.append("")

    # ------------------------------------------------------------------
    # VPC Networks
    # ------------------------------------------------------------------
    try:
        network_client = compute_v1.NetworksClient()
        networks = list(network_client.list(project=project_id))

        network_rows = []

        for network in networks:
            routing_mode = "unknown"

            try:
                if network.routing_config:
                    routing_mode = str(network.routing_config.routing_mode)
            except Exception:
                pass

            network_rows.append(
                [
                    network.name,
                    str(network.auto_create_subnetworks),
                    routing_mode,
                ]
            )

        total_count += len(network_rows)

        sections.append("### VPC Networks")

        if network_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["VPC Name", "Auto Subnetworks", "Routing Mode"],
                    network_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No VPC networks found.")

    except Exception as exc:
        sections.append(f"VPC network scan failed: {exc}")

    sections.append("")

    # ------------------------------------------------------------------
    # Subnets across all regions
    # ------------------------------------------------------------------
    try:
        subnet_client = compute_v1.SubnetworksClient()
        request = compute_v1.AggregatedListSubnetworksRequest(project=project_id)
        iterator = subnet_client.aggregated_list(request=request)

        subnet_rows = []

        for region, response in iterator:
            if response.subnetworks:
                region_name = region.split("/")[-1]

                for subnet in response.subnetworks:
                    subnet_rows.append(
                        [
                            subnet.name,
                            region_name,
                            subnet.ip_cidr_range,
                            subnet.network.split("/")[-1] if subnet.network else "unknown",
                        ]
                    )

        total_count += len(subnet_rows)

        sections.append("### Subnets")

        if subnet_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Subnet Name", "Region", "CIDR", "VPC"],
                    subnet_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No subnets found.")

    except Exception as exc:
        sections.append(f"Subnet scan failed: {exc}")

    sections.append("")

    # ------------------------------------------------------------------
    # Firewall Rules
    # ------------------------------------------------------------------
    try:
        firewall_client = compute_v1.FirewallsClient()
        firewalls = list(firewall_client.list(project=project_id))

        firewall_rows = []

        for firewall in firewalls:
            firewall_rows.append(
                [
                    firewall.name,
                    firewall.direction,
                    firewall.network.split("/")[-1] if firewall.network else "unknown",
                    str(firewall.disabled),
                ]
            )

        total_count += len(firewall_rows)

        sections.append("### Firewall Rules")

        if firewall_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Firewall Name", "Direction", "VPC", "Disabled"],
                    firewall_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No firewall rules found.")

    except Exception as exc:
        sections.append(f"Firewall scan failed: {exc}")

    sections.append("")

    # ------------------------------------------------------------------
    # GKE Clusters
    # ------------------------------------------------------------------
    try:
        from google.cloud import container_v1

        cluster_client = container_v1.ClusterManagerClient()
        parent = f"projects/{project_id}/locations/-"

        response = cluster_client.list_clusters(parent=parent)
        clusters = response.clusters or []

        cluster_rows = []

        for cluster in clusters:
            cluster_rows.append(
                [
                    cluster.name,
                    cluster.location,
                    str(cluster.status),
                    str(cluster.current_node_count),
                ]
            )

        total_count += len(cluster_rows)

        sections.append("### GKE Clusters")

        if cluster_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Cluster Name", "Location", "Status", "Current Nodes"],
                    cluster_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No GKE clusters found.")

    except Exception as exc:
        sections.append(f"GKE cluster scan failed: {exc}")

    sections.append("")

    # ------------------------------------------------------------------
    # Artifact Registry
    # ------------------------------------------------------------------
    try:
        from google.cloud import artifactregistry_v1

        artifact_client = artifactregistry_v1.ArtifactRegistryClient()
        parent = f"projects/{project_id}/locations/{location}"

        repositories = list(artifact_client.list_repositories(parent=parent))

        artifact_rows = []

        for repo in repositories:
            artifact_rows.append(
                [
                    repo.name.split("/")[-1],
                    str(repo.format_),
                    repo.name,
                ]
            )

        total_count += len(artifact_rows)

        sections.append("### Artifact Registry Repositories")

        if artifact_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["Repository", "Format", "Full Resource Name"],
                    artifact_rows,
                )
                + "\n```"
            )
        else:
            sections.append(f"No Artifact Registry repositories found in location {location}.")

    except Exception as exc:
        sections.append(f"Artifact Registry scan failed: {exc}")

    sections.insert(
        3,
        f"- Total discovered resource entries: {total_count}",
    )

    return "\n".join(sections)


# ======================================================================
# TRUE GCP BILLING ANALYSIS USING BIGQUERY
# ======================================================================

def analyze_actual_gcp_billing(billing_table: str = None, days: int = 30) -> str:
    from google.cloud import bigquery

    table_id = billing_table or _get_context_billing_table()
    client_project = _get_bigquery_client_project()

    if not table_id:
        return "BILLING ANALYSIS FAILED: billing_table is missing. Please submit billing table from UI."

    if not client_project:
        return "BILLING ANALYSIS FAILED: project_id is missing. Please submit project details from UI first."

    try:
        days = int(days)
    except Exception:
        days = 30

    if days < 1:
        days = 30

    try:
        client = bigquery.Client(project=client_project)
    except Exception as exc:
        return f"BILLING ANALYSIS FAILED: Could not initialize BigQuery client: {exc}"

    query = f"""
        SELECT
            service.description AS service_name,
            SUM(cost) AS total_cost,
            SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS effective_cost
        FROM
            `{table_id}`
        WHERE
            usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY
            service_name
        ORDER BY
            total_cost DESC
        LIMIT 10
    """

    sections = [
        "## True GCP Billing Analysis",
        f"- Project ID: {_get_context_project_id() or 'not provided'}",
        f"- BigQuery Client Project: {client_project}",
        f"- BigQuery Billing Table: {table_id}",
        f"- Window: Last {days} days",
        "- Metric: Actual billing export cost values.",
        "",
    ]

    try:
        query_job = client.query(query)
        results = query_job.result()

        billing_rows = []
        total_spend = 0.0

        for row in results:
            service = row["service_name"]
            cost = float(row["total_cost"] or 0)
            effective = float(row["effective_cost"] or 0)

            total_spend += cost

            billing_rows.append(
                [
                    service,
                    f"${cost:.2f}",
                    f"${effective:.2f}",
                ]
            )

        sections.append(f"### Top 10 Cost Drivers — Total Spend: ~${total_spend:.2f}")

        if billing_rows:
            sections.append(
                "```text\n"
                + _build_text_table(
                    ["GCP Service", "Raw Cost", "Effective Cost With Credits"],
                    billing_rows,
                )
                + "\n```"
            )
        else:
            sections.append("No billing data found for this timeframe.")

    except Exception as exc:
        sections.append(f"BigQuery execution failed: {exc}")

    return "\n".join(sections)


# ======================================================================
# REPORT GENERATION
# ======================================================================

def generate_rca_report(content_text: str, file_path: str | Path = REPORT_PATH):
    try:
        from fpdf import FPDF
    except Exception as exc:
        return f"PDF generation failed because fpdf is not installed: {exc}"

    project_id = _get_context_project_id() or "unknown-project"

    class SREReport(FPDF):
        def header(self):
            self.set_font("Helvetica", "B", 12)
            self.set_text_color(120, 120, 120)
            self.cell(0, 10, "LUMEN CLOUDOPTIX - PROJECT-WIDE SRE OBSERVATION", 0, 1, "L")
            self.line(10, 20, 200, 20)
            self.ln(10)

        def footer(self):
            self.set_y(-15)
            self.set_font("Helvetica", "I", 8)
            timestamp = datetime.now().strftime("%H:%M")
            self.cell(
                0,
                10,
                f"Page {self.page_no()} | Generated by CloudOptix | {timestamp}",
                0,
                0,
                "C",
            )

    pdf = SREReport()
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(0, 40, 80)
    pdf.cell(0, 15, "CloudOptix Report", ln=1)

    pdf.set_fill_color(245, 245, 245)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(0)
    pdf.cell(45, 8, " Project ID:", 1, 0, "L", True)

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, f" {project_id}", 1, 1, "L")
    pdf.ln(10)

    pdf.set_font("Helvetica", size=11)

    clean_text = str(content_text).encode("latin-1", "replace").decode("latin-1")
    pdf.multi_cell(0, 8, clean_text)

    pdf.output(str(file_path))

    return f"SUCCESS: PDF generated at {file_path}"


def send_rca_email(recipient_email: str, file_path: str | Path = REPORT_PATH):
    sender_email = os.getenv("SENDER_EMAIL")

    if not sender_email:
        return "EMAIL SKIPPED: SENDER_EMAIL is not configured."

    sender_password = os.getenv("SENDER_PASSWORD")

    if not sender_password:
        return "EMAIL SKIPPED: SENDER_PASSWORD is not configured."

    report_path = Path(file_path)

    if not report_path.exists():
        return f"EMAIL SKIPPED: report file not found at {report_path}"

    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = f"CloudOptix Report for {_get_context_project_id() or 'unknown project'}"

    msg.attach(
        MIMEText(
            "CloudOptix has completed project analysis. Please find attached report.",
            "plain",
        )
    )

    try:
        with report_path.open("rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())

        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={report_path.name}")
        msg.attach(part)

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()

        return f"SUCCESS: Report emailed to {recipient_email}"

    except Exception as exc:
        return f"EMAIL FAILED: {exc}"


def create_and_send_report(analysis_summary: str, recipient_email: str | None = None):
    try:
        pdf_result = generate_rca_report(analysis_summary)

        if recipient_email:
            email_result = send_rca_email(recipient_email)
            return f"{pdf_result}\n{email_result}"

        return pdf_result

    except Exception as exc:
        return f"Report Generation Error: {exc}"


def build_fallback_report(logs: str, workloads: str, context: str = "") -> str:
    context_block = f"\nRUNBOOK CONTEXT\n{context}\n" if context else ""

    return f"""PROJECT STATUS SUMMARY

RESOURCE SNAPSHOT
{workloads}

CRITICAL LOGS DISCOVERED
{logs}

{context_block}
"""