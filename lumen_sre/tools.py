import os
import re
import smtplib
import time
import subprocess  # Used for running kubectl diagnostics and patches
from collections import defaultdict
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from fpdf import FPDF

load_dotenv()

REPORT_PATH = Path(__file__).resolve().parents[1] / "Formal_RCA_Report.pdf"


def _parse_cpu_to_millicores(cpu_value: str | None) -> float:
    if not cpu_value:
        return 0.0

    raw_value = str(cpu_value).strip()
    if not raw_value:
        return 0.0

    if raw_value.endswith("m"):
        return max(0.0, float(raw_value[:-1]))

    if raw_value.endswith("n"):
        return max(0.0, float(raw_value[:-1]) / 1_000_000)

    if raw_value.endswith("u"):
        return max(0.0, float(raw_value[:-1]) / 1_000)

    return max(0.0, float(raw_value) * 1000)


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
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "Pi": 1024**5,
        "Ei": 1024**6,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
        "": 1,
    }

    multiplier = multipliers.get(unit)
    if multiplier is None:
        return 0

    return int(number * multiplier)


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


def _format_storage_bytes(value: int | float | None, default: str = "not set", zero_is_value: bool = True) -> str:
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


def _percentile(values: list[float], percentile_value: int) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round((percentile_value / 100) * (len(ordered) - 1))))
    return float(ordered[rank])


def _bytes_to_mebibytes(value: int | float) -> float:
    return float(value) / (1024 * 1024)


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


def _build_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    header_row = "| " + " | ".join(headers) + " |"
    separator_row = "| " + " | ".join("---" for _ in headers) + " |"
    body_rows = ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join([header_row, separator_row, *body_rows])


def _build_text_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def format_row(values: list[str]) -> str:
        padded = [value.ljust(widths[index]) for index, value in enumerate(values)]
        return " | ".join(padded)

    separator = "-+-".join("-" * width for width in widths)
    lines = [format_row(headers), separator]
    lines.extend(format_row(row) for row in rows)
    return "\n".join(lines)


def _format_kubectl_style_cpu(value: float | int | None, default: str = "not set") -> str:
    if value is None:
        return default

    numeric_value = float(value)
    if numeric_value < 0:
        return default

    rounded_up = 0 if numeric_value == 0 else max(1, int(numeric_value) if numeric_value.is_integer() else int(numeric_value) + 1)
    return f"{rounded_up}m"


def _format_cpu_summary(usage_millicores: float, request_millicores: float) -> str:
    if request_millicores <= 0:
        return (
            f"Current CPU usage (raw): {_format_millicores_value(usage_millicores)}\n"
            f"        -> Current CPU usage (kubectl-style): {_format_kubectl_style_cpu(usage_millicores)}\n"
            "        -> Requested CPU: not set\n"
            "        -> Utilization vs request: unavailable because no CPU request is defined"
        )

    utilization = round((usage_millicores / request_millicores) * 100, 1)
    return (
        f"Current CPU usage (raw): {_format_millicores_value(usage_millicores)}\n"
        f"        -> Current CPU usage (kubectl-style): {_format_kubectl_style_cpu(usage_millicores)}\n"
        f"        -> Requested CPU: {_format_millicores_value(request_millicores, zero_is_value=False)}\n"
        f"        -> Utilization vs request: {utilization}%"
    )


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


def _extract_monitoring_point_value(point) -> float:
    value_kind = point.value._pb.WhichOneof("value")
    if not value_kind:
        return 0.0

    return float(getattr(point.value, value_kind))


def _get_active_cluster_location() -> str | None:
    configured_location = os.getenv("GOOGLE_CLOUD_LOCATION", "").strip()
    if configured_location.count("-") >= 2:
        return configured_location

    try:
        core_v1, _ = _load_kube_clients()
        nodes = core_v1.list_node(watch=False).items
    except Exception:
        return configured_location or None

    zone_labels = set()
    for node in nodes:
        labels = getattr(node.metadata, "labels", {}) or {}
        zone_value = labels.get("topology.kubernetes.io/zone") or labels.get("failure-domain.beta.kubernetes.io/zone")
        if zone_value:
            zone_labels.add(zone_value)

    if len(zone_labels) == 1:
        return next(iter(zone_labels))

    return configured_location or None


def _query_historical_metric_series(
    metric_type: str,
    namespace: str,
    days: int,
    aligner,
) -> tuple[dict[str, list[float]], str | None]:
    from google.cloud import monitoring_v3
    import google.auth
    
    try:
        _, project_id = google.auth.default()
    except Exception as exc:
        project_id = os.getenv("GCP_PROJECT_ID")

    if not project_id:
        return {}, "GCP_PROJECT_ID could not be automatically discovered from gcloud credentials."

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
    import google.auth
    
    try:
        _, project_id = google.auth.default()
    except Exception:
        project_id = os.getenv("GCP_PROJECT_ID")

    if not project_id:
        return {}, "GCP_PROJECT_ID could not be automatically discovered from gcloud credentials."

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


def _build_historical_rightsizing_signal(
    cpu_request_millicores: int,
    cpu_p95_millicores: float,
    memory_request_bytes: int,
    memory_p95_bytes: float,
) -> str:
    signals = []

    if cpu_request_millicores > 0 and cpu_p95_millicores > 0:
        cpu_ratio = round((cpu_p95_millicores / cpu_request_millicores) * 100, 1)
        if cpu_ratio < 35:
            signals.append(f"CPU request appears higher than needed relative to historical P95 at {cpu_ratio}% of request.")
        elif cpu_ratio > 80:
            signals.append(f"CPU request is already close to historical P95 at {cpu_ratio}% of request.")
        else:
            signals.append(f"CPU request is in a moderate band relative to historical P95 at {cpu_ratio}% of request.")
    else:
        signals.append("CPU request comparison is unavailable because matching historical CPU samples were not found.")

    if memory_request_bytes > 0 and memory_p95_bytes > 0:
        memory_ratio = round((memory_p95_bytes / memory_request_bytes) * 100, 1)
        if memory_ratio < 50:
            signals.append(f"Memory request appears higher than needed relative to historical P95 at {memory_ratio}% of request.")
        elif memory_ratio > 85:
            signals.append(f"Memory request is already close to historical P95 at {memory_ratio}% of request.")
        else:
            signals.append(f"Memory request is in a moderate band relative to historical P95 at {memory_ratio}% of request.")
    else:
        signals.append("Memory request comparison is unavailable because matching historical memory samples were not found.")

    return " ".join(signals)


def _build_historical_data_diagnostic(
    current_workloads: set[str],
    historical_cpu_workloads: set[str],
    historical_memory_workloads: set[str],
) -> list[str]:
    historical_workloads = historical_cpu_workloads | historical_memory_workloads
    if not current_workloads:
        return []

    if not historical_workloads:
        return [
            "- Historical Match Status: no Cloud Monitoring series matched the current workloads in this namespace.",
            "- Diagnosis: the project and live cluster mapping are correct, but this cluster does not yet have enough matching historical Cloud Monitoring data for the current workloads.",
            "- Action: keep collecting Monitoring data for the current workloads and rerun the 7-day, 30-day, or 60-day analysis after sufficient history is available.",
        ]

    matched_workloads = sorted(current_workloads & historical_workloads)
    if matched_workloads:
        return []

    examples = sorted(historical_workloads)[:5]
    lines = [
        "- Historical Match Status: no Cloud Monitoring series matched the current workloads in this namespace.",
        "- Diagnosis: the project and live cluster mapping are correct, but this cluster does not yet have enough matching historical Cloud Monitoring data for the current workloads.",
    ]
    if examples:
        lines.append(f"- Monitoring Workload Examples: {', '.join(examples)}")
    lines.append(
        "- Action: keep collecting Monitoring data for the current workloads and rerun the 7-day, 30-day, or 60-day analysis after sufficient history is available."
    )
    return lines


def _round_up_to_step(value: float, step: float) -> float:
    if value <= 0:
        return 0.0

    return float(((value + step - 1) // step) * step)


def _build_recommendation_confidence(window_days: int, cpu_points: int, memory_points: int) -> str:
    if window_days >= 60 and cpu_points >= 100 and memory_points >= 100:
        return "high"
    if window_days >= 30 and cpu_points >= 100 and memory_points >= 100:
        return "high"
    if window_days >= 14 and cpu_points >= 24 and memory_points >= 24:
        return "medium"
    if cpu_points > 0 or memory_points > 0:
        return "low"
    return "unavailable"


def _build_historical_window_candidates(requested_days: int) -> list[int]:
    primary_window = min(requested_days, 60)
    candidates = [primary_window]

    if primary_window > 30:
        candidates.append(30)
    if primary_window > 7:
        candidates.append(7)

    return candidates


def _load_historical_series_with_fallback(namespace: str, requested_days: int, current_workloads: set[str]) -> dict:
    from google.cloud import monitoring_v3

    attempts = []
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
        matched_workloads = sorted((set(cpu_series) | set(memory_series)) & current_workloads)
        cpu_points = sum(len(cpu_series.get(workload_name, [])) for workload_name in matched_workloads)
        memory_points = sum(len(memory_series.get(workload_name, [])) for workload_name in matched_workloads)
        attempt = {
            "window_days": candidate_days,
            "cpu_series": cpu_series,
            "memory_series": memory_series,
            "cpu_error": cpu_error,
            "memory_error": memory_error,
            "matched_workloads": matched_workloads,
            "cpu_points": cpu_points,
            "memory_points": memory_points,
        }
        attempts.append(attempt)

        if matched_workloads and (cpu_points > 0 or memory_points > 0):
            return {
                "selected": attempt,
                "attempts": attempts,
                "used_fallback": candidate_days != min(requested_days, 60),
            }

    return {
        "selected": attempts[0] if attempts else None,
        "attempts": attempts,
        "used_fallback": False,
    }


def _build_historical_rightsizing_recommendation(
    resource_state: dict,
    cpu_p50_millicores: float,
    cpu_p95_millicores: float,
    cpu_peak_millicores: float,
    memory_p50_mib: float,
    memory_p95_mib: float,
    memory_peak_mib: float,
    cpu_points: int,
    memory_points: int,
    window_days: int,
) -> dict[str, str | float | int | None]:
    current_pods = max(resource_state.get("pods", 0), 1)
    current_replicas = resource_state.get("desired_replicas") or resource_state.get("pods") or 1
    per_pod_cpu_request = resource_state.get("cpu_request_millicores", 0) / current_pods
    per_pod_memory_request_bytes = resource_state.get("memory_request_bytes", 0) / current_pods
    per_pod_memory_request_mib = _bytes_to_mebibytes(per_pod_memory_request_bytes) if per_pod_memory_request_bytes > 0 else 0.0

    cpu_ratio = (cpu_p95_millicores / per_pod_cpu_request) if per_pod_cpu_request > 0 and cpu_p95_millicores > 0 else None
    memory_ratio = (memory_p95_mib / per_pod_memory_request_mib) if per_pod_memory_request_mib > 0 and memory_p95_mib > 0 else None
    confidence = _build_recommendation_confidence(window_days, cpu_points, memory_points)

    recommended_cpu_per_pod = None
    if per_pod_cpu_request > 0 and cpu_p95_millicores > 0:
        baseline_cpu = max(cpu_p95_millicores * 1.25, cpu_p50_millicores * 1.4, 25.0)
        candidate_cpu = _round_up_to_step(baseline_cpu, 25.0)
        if candidate_cpu < per_pod_cpu_request * 0.9:
            recommended_cpu_per_pod = candidate_cpu

    recommended_memory_per_pod_mib = None
    if per_pod_memory_request_mib > 0 and memory_p95_mib > 0:
        baseline_memory_mib = max(memory_p95_mib * 1.2, memory_p50_mib * 1.35, 64.0)
        candidate_memory_mib = _round_up_to_step(baseline_memory_mib, 16.0)
        if candidate_memory_mib < per_pod_memory_request_mib * 0.9:
            recommended_memory_per_pod_mib = candidate_memory_mib

    target_replicas = int(current_replicas)
    replica_recommendation = "Keep current replicas until traffic, SLOs, and autoscaling policy are reviewed."
    if (
        current_replicas > 1
        and window_days >= 30
        and cpu_ratio is not None
        and memory_ratio is not None
        and cpu_ratio <= 0.25
        and memory_ratio <= 0.40
        and cpu_peak_millicores <= per_pod_cpu_request * 0.45
        and memory_peak_mib <= per_pod_memory_request_mib * 0.65
        and confidence in {"high", "medium"}
    ):
        target_replicas = max(1, int(current_replicas) - 1)
        replica_recommendation = (
            f"Consider reducing replicas from {int(current_replicas)} to {target_replicas} after validating peak traffic and rollback readiness."
        )

    if cpu_ratio is not None and memory_ratio is not None and cpu_ratio <= 0.35 and memory_ratio <= 0.50:
        underuse_text = "Consistently underused versus current requests."
    elif cpu_ratio is not None and cpu_ratio <= 0.50:
        underuse_text = "CPU appears underused, but memory is not clearly underused."
    elif memory_ratio is not None and memory_ratio <= 0.60:
        underuse_text = "Memory appears underused, but CPU is not clearly underused."
    else:
        underuse_text = "No clear underuse signal strong enough for direct downsizing."

    current_cpu_capacity = float(resource_state.get("cpu_request_millicores", 0))
    current_memory_capacity_mib = _bytes_to_mebibytes(resource_state.get("memory_request_bytes", 0))
    target_cpu_capacity = current_cpu_capacity
    target_memory_capacity_mib = current_memory_capacity_mib

    if recommended_cpu_per_pod is not None:
        target_cpu_capacity = recommended_cpu_per_pod * target_replicas
    if recommended_memory_per_pod_mib is not None:
        target_memory_capacity_mib = recommended_memory_per_pod_mib * target_replicas

    cpu_savings_pct = (
        round(((current_cpu_capacity - target_cpu_capacity) / current_cpu_capacity) * 100, 1)
        if current_cpu_capacity > 0 and target_cpu_capacity < current_cpu_capacity
        else 0.0
    )
    memory_savings_pct = (
        round(((current_memory_capacity_mib - target_memory_capacity_mib) / current_memory_capacity_mib) * 100, 1)
        if current_memory_capacity_mib > 0 and target_memory_capacity_mib < current_memory_capacity_mib
        else 0.0
    )

    return {
        "underuse_text": underuse_text,
        "recommended_cpu_per_pod": recommended_cpu_per_pod,
        "recommended_memory_per_pod_mib": recommended_memory_per_pod_mib,
        "target_replicas": target_replicas,
        "replica_recommendation": replica_recommendation,
        "cpu_savings_pct": cpu_savings_pct,
        "memory_savings_pct": memory_savings_pct,
        "confidence": confidence,
    }


def fetch_historical_resource_analysis(namespace: str = "default", days: int = 60):
    """Return 30-60 day CPU and memory history for application workloads when Cloud Monitoring data exists."""
    try:
        requested_days = int(days)
    except (TypeError, ValueError):
        return "HISTORICAL RESOURCE ANALYSIS: invalid days value. Provide an integer between 1 and 60."

    if requested_days < 1:
        return "HISTORICAL RESOURCE ANALYSIS: invalid days value. Provide an integer between 1 and 60."

    if requested_days > 60:
        requested_days = 60

    current_resources, current_resource_error = _group_current_workload_resources(namespace)
    historical_result = _load_historical_series_with_fallback(
        namespace=namespace,
        requested_days=requested_days,
        current_workloads=set(current_resources),
    )
    selected_attempt = historical_result["selected"]
    if not selected_attempt:
        return f"HISTORICAL RESOURCE ANALYSIS: no workloads or monitoring series were found for namespace {namespace}."

    window_days = selected_attempt["window_days"]
    cpu_series = selected_attempt["cpu_series"]
    memory_series = selected_attempt["memory_series"]
    cpu_error = selected_attempt["cpu_error"]
    memory_error = selected_attempt["memory_error"]

    if cpu_error and memory_error:
        return (
            "HISTORICAL RESOURCE ANALYSIS: unavailable. "
            f"CPU query failed: {cpu_error} | Memory query failed: {memory_error}"
        )

    historical_only_workloads = sorted((set(cpu_series) | set(memory_series)) - set(current_resources))
    workload_names = sorted(current_resources) if current_resources else sorted(set(cpu_series) | set(memory_series))
    if not workload_names:
        return f"HISTORICAL RESOURCE ANALYSIS: no workloads or monitoring series were found for namespace {namespace}."

    sections = [
        "## Historical Resource Analysis",
        f"- Namespace: {namespace}",
        f"- Requested Window: {requested_days}d",
        f"- Analysis Window: {window_days}d",
        "- Data Source: Google Cloud Monitoring aligned CPU and memory series plus current pod resource settings.",
        "- Interpretation: this is the right input for organization-level rightsizing discussion, but it is still advisory until you add approvals and rollback flow.",
    ]

    fallback_attempts = [str(attempt["window_days"]) for attempt in historical_result["attempts"]]
    if historical_result["used_fallback"]:
        sections.append(
            "- Window Fallback: "
            f"requested {requested_days}d, but used {window_days}d because that was the longest window with matching historical samples for the current workloads."
        )
    elif len(fallback_attempts) > 1 and not selected_attempt["matched_workloads"]:
        sections.append(
            "- Window Fallback: "
            f"checked {', '.join(f'{value}d' for value in fallback_attempts)} and found no matching historical samples for the current workloads."
        )

    if current_resource_error:
        sections.append(f"- Current Resource Config: unavailable. {current_resource_error}")
    if cpu_error:
        sections.append(f"- CPU History: unavailable. {cpu_error}")
    if memory_error:
        sections.append(f"- Memory History: unavailable. {memory_error}")
    if historical_only_workloads and current_resources:
        sections.append(
            f"- Historical-Only Workloads Omitted: {', '.join(historical_only_workloads[:5])}"
            + (f" and {len(historical_only_workloads) - 5} more" if len(historical_only_workloads) > 5 else "")
        )
    sections.extend(
        _build_historical_data_diagnostic(
            current_workloads=set(current_resources),
            historical_cpu_workloads=set(cpu_series),
            historical_memory_workloads=set(memory_series),
        )
    )

    sections.append("")

    history_rows = []
    recommendation_rows = []

    for workload_name in workload_names:
        resource_state = current_resources.get(
            workload_name,
            {
                "pods": 0,
                "cpu_request_millicores": 0,
                "cpu_limit_millicores": 0,
                "memory_request_bytes": 0,
                "memory_limit_bytes": 0,
            },
        )
        cpu_values_millicores = [value * 1000 for value in cpu_series.get(workload_name, [])]
        memory_values_bytes = memory_series.get(workload_name, [])

        cpu_p50 = _percentile(cpu_values_millicores, 50)
        cpu_p95 = _percentile(cpu_values_millicores, 95)
        cpu_peak = max(cpu_values_millicores, default=0.0)
        memory_p50_mib = _percentile([value / (1024 * 1024) for value in memory_values_bytes], 50)
        memory_p95_mib = _percentile([value / (1024 * 1024) for value in memory_values_bytes], 95)
        memory_peak_mib = max((value / (1024 * 1024) for value in memory_values_bytes), default=0.0)

        signal = _build_historical_rightsizing_signal(
            cpu_request_millicores=resource_state["cpu_request_millicores"],
            cpu_p95_millicores=cpu_p95,
            memory_request_bytes=resource_state["memory_request_bytes"],
            memory_p95_bytes=memory_p95_mib * 1024 * 1024,
        )
        recommendation = _build_historical_rightsizing_recommendation(
            resource_state=resource_state,
            cpu_p50_millicores=cpu_p50,
            cpu_p95_millicores=cpu_p95,
            cpu_peak_millicores=cpu_peak,
            memory_p50_mib=memory_p50_mib,
            memory_p95_mib=memory_p95_mib,
            memory_peak_mib=memory_peak_mib,
            cpu_points=len(cpu_values_millicores),
            memory_points=len(memory_values_bytes),
            window_days=window_days,
        )
        current_replicas = resource_state.get("desired_replicas") or resource_state["pods"]
        suggested_replicas = recommendation["target_replicas"]

        history_rows.append(
            [
                workload_name,
                resource_state.get('workload_kind', 'Workload'),
                str(resource_state['pods']),
                _format_millicores_value(resource_state['cpu_request_millicores'] or None, zero_is_value=False),
                _format_millicores_value(resource_state['cpu_limit_millicores'] or None, zero_is_value=False),
                _format_millicores_value(cpu_p50, default='not set', zero_is_value=bool(cpu_values_millicores)),
                _format_millicores_value(cpu_p95, default='not set', zero_is_value=bool(cpu_values_millicores)),
                _format_millicores_value(cpu_peak, default='not set', zero_is_value=bool(cpu_values_millicores)),
                _format_mebibytes_value((resource_state['memory_request_bytes'] / (1024 * 1024)) if resource_state['memory_request_bytes'] else None, zero_is_value=False),
                _format_mebibytes_value((resource_state['memory_limit_bytes'] / (1024 * 1024)) if resource_state['memory_limit_bytes'] else None, zero_is_value=False),
                _format_mebibytes_value(memory_p50_mib, default='not set', zero_is_value=bool(memory_values_bytes)),
                _format_mebibytes_value(memory_p95_mib, default='not set', zero_is_value=bool(memory_values_bytes)),
                _format_mebibytes_value(memory_peak_mib, default='not set', zero_is_value=bool(memory_values_bytes)),
                f"CPU {len(cpu_values_millicores)}, Mem {len(memory_values_bytes)}",
            ]
        )

        recommendation_rows.append(
            [
                workload_name,
                str(suggested_replicas) if suggested_replicas != current_replicas else "keep current",
                recommendation['underuse_text'],
                _format_millicores_value(recommendation['recommended_cpu_per_pod'], default='keep current', zero_is_value=False),
                _format_mebibytes_value(recommendation['recommended_memory_per_pod_mib'], default='keep current', zero_is_value=False),
                f"{recommendation['cpu_savings_pct']}%",
                f"{recommendation['memory_savings_pct']}%",
                recommendation['confidence'],
            ]
        )

    sections.append("### Historical Resource Profile")
    if history_rows:
        sections.append(
            "```text\n"
            + _build_text_table(
                headers=[
                    "Workload", "Type", "Pods",
                    "CPU Req", "CPU Limit", "CPU p50", "CPU p95", "CPU Peak",
                    "Mem Req", "Mem Limit", "Mem p50", "Mem p95", "Mem Peak",
                    "Coverage Points"
                ],
                rows=history_rows,
            )
            + "\n```\n"
        )
    else:
        sections.append("No history data available.\n")

    sections.append("### Rightsizing Recommendations")
    if recommendation_rows:
        sections.append(
            "```text\n"
            + _build_text_table(
                headers=[
                    "Workload", "Target Replicas", "Underuse Assessment",
                    "Suggested CPU Req", "Suggested Mem Req",
                    "CPU Req Savings", "Mem Req Savings", "Confidence"
                ],
                rows=recommendation_rows,
            )
            + "\n```"
        )
    else:
        sections.append("No recommendations available.")

    return "\n".join(sections)


def fetch_pod_usage_since_start(namespace: str = "default"):
    """Return current pod-instance usage from pod start time until now for current pods in a namespace."""
    from google.cloud import monitoring_v3

    try:
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return f"POD USAGE SINCE START: unavailable. Kubernetes client configuration failed: {exc}"

    try:
        pods = core_v1.list_namespaced_pod(namespace, watch=False).items
    except Exception as exc:
        return f"POD USAGE SINCE START: unavailable. Could not list pods in namespace {namespace}: {exc}"

    if not pods:
        return f"POD USAGE SINCE START: no pods found in namespace {namespace}."

    current_pods = []
    for pod in pods:
        start_time = getattr(pod.status, "start_time", None) or getattr(pod.metadata, "creation_timestamp", None)
        if not start_time:
            continue
        current_pods.append(pod)

    if not current_pods:
        return f"POD USAGE SINCE START: no current pods with start timestamps were found in namespace {namespace}."

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
    memory_mean_series, memory_mean_error = _query_pod_metric_series_since_timestamp(
        metric_type="kubernetes.io/container/memory/used_bytes",
        namespace=namespace,
        start_timestamp_seconds=earliest_start_seconds,
        aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
        alignment_seconds=alignment_seconds,
        pod_names=pod_names,
    )
    memory_peak_series, memory_peak_error = _query_pod_metric_series_since_timestamp(
        metric_type="kubernetes.io/container/memory/used_bytes",
        namespace=namespace,
        start_timestamp_seconds=earliest_start_seconds,
        aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MAX,
        alignment_seconds=alignment_seconds,
        pod_names=pod_names,
    )

    live_samples, live_error = _collect_pod_resource_samples(namespace=namespace)
    live_samples_by_pod = {sample["pod_name"]: sample for sample in live_samples}

    sections = [
        "## Pod Usage Since Start",
        f"- Namespace: {namespace}",
        "- Definition: values below describe the current pod instances from each pod start time until now, not from deployment creation day.",
        f"- Alignment Window: {alignment_seconds}s",
        "",
    ]

    if cpu_error:
        sections.append(f"- CPU HISTORY ERROR: {cpu_error}")
    if memory_mean_error:
        sections.append(f"- MEMORY AVERAGE ERROR: {memory_mean_error}")
    if memory_peak_error:
        sections.append(f"- MEMORY PEAK ERROR: {memory_peak_error}")
    if live_error:
        sections.append(f"- LIVE SNAPSHOT ERROR: {live_error}")

    now = datetime.now(timezone.utc)
    pod_rows = []
    for pod in sorted(current_pods, key=lambda item: item.metadata.name):
        pod_name = pod.metadata.name
        start_time = pod.status.start_time or pod.metadata.creation_timestamp
        age_seconds = max(1.0, (now - start_time).total_seconds())
        memory_mean_values = memory_mean_series.get(pod_name, [])
        memory_peak_values = memory_peak_series.get(pod_name, [])
        cpu_values = cpu_series.get(pod_name, [])
        live_sample = live_samples_by_pod.get(pod_name, {})

        current_cpu = live_sample.get("usage_millicores")
        current_memory_bytes = live_sample.get("memory_usage_bytes")
        current_memory_mib = _bytes_to_mebibytes(current_memory_bytes) if current_memory_bytes is not None else None
        cpu_core_seconds = sum(cpu_values)
        average_cpu_millicores = (cpu_core_seconds / age_seconds) * 1000 if cpu_values else None
        average_memory_mib = _bytes_to_mebibytes(sum(memory_mean_values) / len(memory_mean_values)) if memory_mean_values else None
        peak_memory_mib = _bytes_to_mebibytes(max(memory_peak_values)) if memory_peak_values else None

        if cpu_values:
            cpu_consumed_text = f"{round(cpu_core_seconds, 3)} CPU-seconds ({round(cpu_core_seconds / 3600, 6)} CPU-hours)"
        else:
            cpu_consumed_text = "unavailable from Cloud Monitoring for this current pod instance"

        if average_cpu_millicores is None:
            average_cpu_text = "unavailable"
        else:
            average_cpu_text = _format_millicores_value(average_cpu_millicores)

        pod_rows.append(
            [
                pod_name,
                _format_duration(age_seconds),
                _format_kubectl_style_cpu(current_cpu),
                _format_mebibytes_value(average_memory_mib),
                _format_mebibytes_value(peak_memory_mib),
            ]
        )

    sections.append("### Pods")
    sections.append(
        "```text\n"
        + _build_text_table(
            headers=[
                "Pod",
                "Pod Age",
                "Current CPU",
                "Avg Memory Since Start",
                "Peak Memory Since Start",
            ],
            rows=pod_rows,
        )
        + "\n```"
    )

    return "\n".join(sections)


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
        return [], f"Could not list pod specs to fetch CPU requests: {exc}"

    pod_specs = {
        (pod.metadata.namespace, pod.metadata.name): pod
        for pod in pod_items
    }
    pvc_capacities = {
        (claim.metadata.namespace, claim.metadata.name): _parse_memory_to_bytes((claim.status.capacity or {}).get("storage"))
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
        ephemeral_request_bytes = 0
        ephemeral_limit_bytes = 0
        status = "Unknown"
        ready_text = "0/0"
        workload_name = pod_name
        pod_ip = "unknown"
        node_name = "unknown"
        restart_count = 0
        pvc_claim_names = []
        pvc_capacity_bytes = 0

        if pod_spec is not None:
            request_millicores = sum(
                _parse_cpu_to_millicores(container.resources.requests.get("cpu"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.requests
            )
            limit_millicores = sum(
                _parse_cpu_to_millicores(container.resources.limits.get("cpu"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.limits
            )
            memory_request_bytes = sum(
                _parse_memory_to_bytes(container.resources.requests.get("memory"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.requests
            )
            memory_limit_bytes = sum(
                _parse_memory_to_bytes(container.resources.limits.get("memory"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.limits
            )
            ephemeral_request_bytes = sum(
                _parse_memory_to_bytes(container.resources.requests.get("ephemeral-storage"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.requests
            )
            ephemeral_limit_bytes = sum(
                _parse_memory_to_bytes(container.resources.limits.get("ephemeral-storage"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.limits
            )
            ready_count = sum(1 for status_item in (pod_spec.status.container_statuses or []) if status_item.ready)
            total_count = len(pod_spec.status.container_statuses or [])
            ready_text = f"{ready_count}/{total_count}" if total_count else "0/0"
            status = _get_pod_status(pod_spec)
            workload_name = _derive_workload_name(pod_spec)
            pod_ip = getattr(pod_spec.status, "pod_ip", None) or "unknown"
            node_name = getattr(pod_spec.spec, "node_name", None) or "unknown"
            restart_count = sum(status_item.restart_count for status_item in (pod_spec.status.container_statuses or []))
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
                "ephemeral_request_bytes": ephemeral_request_bytes,
                "ephemeral_limit_bytes": ephemeral_limit_bytes,
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


def fetch_cost_optimization_snapshot(namespace: str = "default"):
    """Return a live, namespace-scoped cost snapshot without claiming long-term savings."""
    samples, error = _collect_pod_resource_samples(namespace=namespace)
    if error:
        return f"COST OPTIMIZATION SNAPSHOT: unavailable. {error}"

    app_samples = [sample for sample in samples if sample["namespace"] == namespace]
    if not app_samples:
        return f"COST OPTIMIZATION SNAPSHOT: no pod metrics were returned for namespace {namespace}."

    grouped = defaultdict(lambda: {
        "pods": 0,
        "usage_millicores": 0,
        "memory_usage_bytes": 0,
        "request_millicores": 0,
        "limit_millicores": 0,
        "memory_request_bytes": 0,
        "memory_limit_bytes": 0,
        "ephemeral_request_bytes": 0,
        "ephemeral_limit_bytes": 0,
        "pvc_capacity_bytes": 0,
        "statuses": [],
    })

    for sample in app_samples:
        workload = grouped[sample["workload_name"]]
        workload["pods"] += 1
        workload["usage_millicores"] += sample["usage_millicores"]
        workload["memory_usage_bytes"] += sample["memory_usage_bytes"]
        workload["request_millicores"] += sample["request_millicores"]
        workload["limit_millicores"] += sample["limit_millicores"]
        workload["memory_request_bytes"] += sample["memory_request_bytes"]
        workload["memory_limit_bytes"] += sample["memory_limit_bytes"]
        workload["ephemeral_request_bytes"] += sample["ephemeral_request_bytes"]
        workload["ephemeral_limit_bytes"] += sample["ephemeral_limit_bytes"]
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
        f"- Namespace: {namespace}",
        "- Scope: live Kubernetes metrics and pod specs only.",
        f"- Sample Time: {sample_timestamp}",
        f"- Sample Window: {sample_window}",
        "- Historical Confidence: unavailable for 30- or 60-day rightsizing because this implementation does not yet persist long-range usage history.",
        "- Recommendation Rule: use this output for current over-provisioning signals only, not for committed savings estimates.",
        "- Storage Note: live disk usage is not exposed by metrics.k8s.io here. The tables below show configured ephemeral-storage requests and limits plus attached PVC capacity when present.",
        "",
    ]

    workload_rows = []

    for workload_name in sorted(grouped):
        workload = grouped[workload_name]
        request_millicores = workload["request_millicores"]
        usage_millicores = workload["usage_millicores"]
        memory_usage_mib = _bytes_to_mebibytes(workload["memory_usage_bytes"])
        limit_millicores = workload["limit_millicores"]
        memory_request_bytes = workload["memory_request_bytes"]
        memory_limit_bytes = workload["memory_limit_bytes"]
        ephemeral_request_bytes = workload["ephemeral_request_bytes"]
        ephemeral_limit_bytes = workload["ephemeral_limit_bytes"]
        pvc_capacity_bytes = workload["pvc_capacity_bytes"]
        unhealthy = [status for status in workload["statuses"] if status not in {"Running", "Completed", "Succeeded"}]

        if request_millicores <= 0:
            utilization_text = "unavailable"
            recommendation = "Define CPU requests before making any rightsizing recommendation."
        else:
            utilization = round((usage_millicores / request_millicores) * 100, 1)
            utilization_text = f"{utilization}%"
            if unhealthy:
                recommendation = "Do not optimize this workload yet; stabilize unhealthy pods first."
            elif utilization <= 20:
                recommendation = "Likely over-provisioned in the current snapshot. Reduce requests cautiously after collecting history."
            elif utilization <= 60:
                recommendation = "Near a normal operating band in this snapshot. Observe longer before changing requests."
            else:
                recommendation = "Current usage is meaningful relative to requests. Preserve headroom until historical data confirms otherwise."

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
            headers=[
                "Workload",
                "Pods",
                "Current CPU",
                "Current Memory",
                "CPU Request",
                "Memory Request",
                "Attached PVC Capacity",
                "CPU Utilization",
                "Status",
                "Optimization Recommendation",
            ],
            rows=workload_rows,
        )
        + "\n```"
    )
    sections.append("")

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

    sections.append("### Exact Pod Resource Details")
    sections.append(
        "```text\n"
        + _build_text_table(
            headers=[
                "Pod",
                "Current CPU",
                "Current Memory",
                "CPU Request",
                "Memory Request",
                "Ready",
                "Status",
                "Restarts",
            ],
            rows=pod_resource_rows,
        )
        + "\n```"
    )
    sections.append("")

    sections.append("### Exact Pod Placement And Storage Details")
    sections.append(
        "```text\n"
        + _build_text_table(
            headers=["Pod", "Node", "Pod IP", "PVC Claims", "PVC Capacity"],
            rows=pod_infra_rows,
        )
        + "\n```"
    )

    return "\n".join(sections)


def _load_kube_clients():
    from kubernetes import client

    _load_kube_configuration()

    return client.CoreV1Api(), client.CustomObjectsApi()


def _load_kube_configuration():
    from kubernetes import config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


def _load_all_kube_clients():
    from kubernetes import client

    _load_kube_configuration()
    return client.CoreV1Api(), client.CustomObjectsApi(), client.AppsV1Api()


def _choose_probe_pod(core_v1, namespace: str, excluded_pod_name: str | None = None) -> str | None:
    try:
        pods = core_v1.list_namespaced_pod(namespace).items
    except Exception:
        return None

    running_pods = []
    for pod in pods:
        if pod.metadata.name == excluded_pod_name:
            continue
        if _get_pod_status(pod) != "Running":
            continue
        if not any(status.ready for status in (pod.status.container_statuses or [])):
            continue
        running_pods.append(pod.metadata.name)

    return sorted(running_pods)[0] if running_pods else None


def _extract_host_port_from_mongo_uri(uri_value: str) -> tuple[str, int] | None:
    match = re.match(r"^mongodb(?:\+srv)?://(?:[^@/]+@)?(?P<host>[A-Za-z0-9.-]+)(?::(?P<port>\d+))?", uri_value)
    if not match:
        return None

    host = match.group("host")
    port = int(match.group("port") or 27017)
    return host, port


def _exec_in_pod(core_v1, pod_name: str, namespace: str, command: list[str]) -> str:
    from kubernetes.stream import stream

    return stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        namespace,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _get_pod_status(pod) -> str:
    for container_status in pod.status.container_statuses or []:
        waiting = getattr(container_status.state, "waiting", None)
        if waiting and waiting.reason:
            return waiting.reason

    return pod.status.phase or "Unknown"


def _read_namespaced_pod_logs(core_v1, pod_name: str, namespace: str, tail_lines: int = 100) -> str:
    try:
        return core_v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            timestamps=True,
        )
    except Exception as exc:
        return f"Unable to read pod logs: {exc}"


def _read_namespaced_pod_events(core_v1, pod_name: str, namespace: str) -> str:
    try:
        events = core_v1.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={pod_name}",
        ).items
    except Exception as exc:
        return f"Unable to read pod events: {exc}"

    if not events:
        return "No recent pod events found."

    def event_timestamp(event):
        return (
            event.last_timestamp
            or event.event_time
            or event.first_timestamp
            or event.metadata.creation_timestamp
        )

    rendered_events = []
    for event in sorted(events, key=event_timestamp):
        timestamp = event_timestamp(event)
        rendered_events.append(
            f"[{timestamp}] {event.type or 'Normal'} {event.reason or 'Event'}: {event.message or 'No message'}"
        )

    return "\n".join(rendered_events)


def _resolve_deployment_name_for_pod(pod, apps_v1) -> str | None:
    owner_references = getattr(pod.metadata, "owner_references", None) or []
    if not owner_references:
        return None

    owner = owner_references[0]
    if owner.kind == "Deployment":
        return owner.name

    if owner.kind != "ReplicaSet":
        return owner.name

    namespace = pod.metadata.namespace
    try:
        replica_set = apps_v1.read_namespaced_replica_set(owner.name, namespace)
    except Exception:
        return owner.name.rsplit("-", 1)[0] if "-" in owner.name else owner.name

    replica_owners = getattr(replica_set.metadata, "owner_references", None) or []
    if replica_owners:
        replica_owner = replica_owners[0]
        if replica_owner.kind == "Deployment":
            return replica_owner.name

    return owner.name.rsplit("-", 1)[0] if "-" in owner.name else owner.name


def _looks_like_placeholder_uri(uri_value: str) -> bool:
    lowered = uri_value.strip().lower()
    placeholders = ("changeme", "example", "placeholder", "localhost", "127.0.0.1", "<", ">")
    return not lowered or any(token in lowered for token in placeholders)


def _inspect_connection_env(core_v1, apps_v1, namespace: str, deployment_name: str) -> dict:
    deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
    container = deployment.spec.template.spec.containers[0]
    env_candidates = ["MONGO_URI", "MONGO_URL", "MONGODB_URI", "DATABASE_URL"]

    selected_env = None
    for env_var in container.env or []:
        if env_var.name in env_candidates:
            selected_env = env_var
            break

    result = {
        "deployment_name": deployment_name,
        "container_name": container.name,
        "env_name": selected_env.name if selected_env else "MONGO_URI",
        "state": "missing",
        "summary": "Connection env var is missing from the deployment.",
    }

    if not selected_env:
        return result

    if selected_env.value:
        result["state"] = "placeholder_literal" if _looks_like_placeholder_uri(selected_env.value) else "literal_value"
        result["summary"] = f"Connection env var {selected_env.name} is set directly in the deployment."
        return result

    value_from = selected_env.value_from
    if value_from and value_from.secret_key_ref:
        secret_name = value_from.secret_key_ref.name
        secret_key = value_from.secret_key_ref.key
        result.update({"source_type": "secret", "source_name": secret_name, "source_key": secret_key})
        try:
            secret = core_v1.read_namespaced_secret(secret_name, namespace)
            if secret_key not in (secret.data or {}):
                result["state"] = "broken_secret_ref"
                result["summary"] = f"Connection env var {selected_env.name} points to secret {secret_name}, but key {secret_key} is missing."
            else:
                result["state"] = "secret_ref"
                result["summary"] = f"Connection env var {selected_env.name} is sourced from secret {secret_name}."
        except Exception as exc:
            result["state"] = "broken_secret_ref"
            result["summary"] = f"Connection env var {selected_env.name} points to missing secret {secret_name}: {exc}"
        return result

    if value_from and value_from.config_map_key_ref:
        config_map_name = value_from.config_map_key_ref.name
        config_map_key = value_from.config_map_key_ref.key
        result.update({"source_type": "configmap", "source_name": config_map_name, "source_key": config_map_key})
        try:
            config_map = core_v1.read_namespaced_config_map(config_map_name, namespace)
            if config_map_key not in (config_map.data or {}):
                result["state"] = "broken_configmap_ref"
                result["summary"] = f"Connection env var {selected_env.name} points to config map {config_map_name}, but key {config_map_key} is missing."
            else:
                result["state"] = "configmap_ref"
                result["summary"] = f"Connection env var {selected_env.name} is sourced from config map {config_map_name}."
        except Exception as exc:
            result["state"] = "broken_configmap_ref"
            result["summary"] = f"Connection env var {selected_env.name} points to missing config map {config_map_name}: {exc}"
        return result

    result["state"] = "unsupported_source"
    result["summary"] = f"Connection env var {selected_env.name} uses an unsupported valueFrom source."
    return result


def _find_connection_source_candidates(core_v1, namespace: str) -> list[dict]:
    keys = ("MONGO_URI", "MONGODB_URI", "DATABASE_URL")
    candidates = []

    for secret in core_v1.list_namespaced_secret(namespace).items:
        for key in keys:
            if key in (secret.data or {}):
                candidates.append({"type": "secret", "name": secret.metadata.name, "key": key})

    for config_map in core_v1.list_namespaced_config_map(namespace).items:
        for key in keys:
            if key in (config_map.data or {}):
                candidates.append({"type": "configmap", "name": config_map.metadata.name, "key": key})

    return candidates


def _select_single_safe_candidate(candidates: list[dict], preferred_key: str) -> dict | None:
    matching_key = [candidate for candidate in candidates if candidate["key"] == preferred_key]
    if len(matching_key) == 1:
        return matching_key[0]

    if len(candidates) == 1:
        return candidates[0]

    mongo_named = [candidate for candidate in candidates if "mongo" in candidate["name"].lower()]
    if len(mongo_named) == 1:
        return mongo_named[0]

    return None


def _patch_env_reference(
    apps_v1,
    namespace: str,
    deployment_name: str,
    container_name: str,
    env_name: str,
    candidate: dict,
) -> str:
    if candidate["type"] == "secret":
        env_patch = {
            "name": env_name,
            "valueFrom": {
                "secretKeyRef": {
                    "name": candidate["name"],
                    "key": candidate["key"],
                }
            },
        }
    else:
        env_patch = {
            "name": env_name,
            "valueFrom": {
                "configMapKeyRef": {
                    "name": candidate["name"],
                    "key": candidate["key"],
                }
            },
        }

    patch_body = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": container_name,
                            "env": [env_patch],
                        }
                    ]
                }
            }
        }
    }

    apps_v1.patch_namespaced_deployment(deployment_name, namespace, patch_body)
    return (
        f"AUTO-REMEDIATION APPLIED: patched {deployment_name} to source {env_name} from "
        f"{candidate['type']} {candidate['name']} key {candidate['key']}."
    )


def _classify_failure(logs: str, events: str, env_state: dict) -> dict:
    combined = f"{logs}\n{events}".lower()
    connection_error = any(
        marker in combined
        for marker in (
            "mongonetworktimeouterror",
            "ecconnrefused",
            "connection timed out",
            "failed to connect",
            "mongodb",
        )
    )

    if connection_error and env_state["state"] in {"missing", "broken_secret_ref", "broken_configmap_ref", "placeholder_literal"}:
        return {
            "summary": "The workload is failing on database connectivity and the connection env var is missing or broken.",
            "auto_fix": True,
        }

    if connection_error:
        return {
            "summary": "The workload is failing on database connectivity, but the connection env var already points to an existing source. This likely needs network, DNS, firewall, or database-side remediation.",
            "auto_fix": False,
        }

    return {
        "summary": "The failure pattern is not one of the currently supported autonomous remediation scenarios.",
        "auto_fix": False,
    }


def _fetch_kubernetes_usage_summary() -> str:
    """Return per-pod CPU usage and request data from the Kubernetes metrics API."""
    samples, error = _collect_pod_resource_samples()
    if error:
        return f"CURRENT POD USAGE: unavailable. {error}"

    summaries = []
    unhealthy = []
    total_usage = 0.0
    total_request = 0.0
    sample_timestamp = next(
        (sample["sample_timestamp"] for sample in samples if sample.get("sample_timestamp")),
        "unknown",
    )
    sample_window = next(
        (sample["sample_window"] for sample in samples if sample.get("sample_window")),
        "unknown",
    )

    for sample in samples:
        namespace = sample["namespace"]
        pod_name = sample["pod_name"]
        usage_millicores = sample["usage_millicores"]
        memory_usage_mib = _bytes_to_mebibytes(sample["memory_usage_bytes"])
        request_millicores = sample["request_millicores"]
        status = sample["status"]
        ready_text = sample["ready"]

        summary = (
            f"  Pod: {namespace}/{pod_name}\n"
            f"        -> Ready: {ready_text}\n"
            f"        -> Status: {status}\n"
            f"        -> {_format_cpu_summary(usage_millicores, request_millicores)}\n"
            f"        -> Current memory usage: {_format_mebibytes_value(memory_usage_mib)}"
        )
        summaries.append((status, summary))
        total_usage += usage_millicores
        total_request += request_millicores

        if status not in {"Running", "Completed", "Succeeded"}:
            unhealthy.append(
                f"{namespace}/{pod_name} | Status: {status} | CPU: {_format_millicores_value(usage_millicores)}"
            )

    summaries.sort(key=lambda item: (item[0] in {"Running", "Completed", "Succeeded"}, item[0], item[1]))

    sections = ["CURRENT POD USAGE:", f"  SAMPLE TIME: {sample_timestamp} | SAMPLE WINDOW: {sample_window}"]
    if unhealthy:
        sections.append("  INCIDENT PRIORITY PODS:")
        sections.extend(f"    - {entry}" for entry in unhealthy[:10])

    if total_request > 0:
        overall_utilization = round((total_usage / total_request) * 100, 1)
        sections.append(
            "  CLUSTER SAMPLE TOTALS: "
            f"usage {_format_millicores_value(total_usage)} CPU | "
            f"requested {_format_millicores_value(total_request, zero_is_value=False)} CPU | "
            f"utilization {overall_utilization}%"
        )
    else:
        sections.append(f"  CLUSTER SAMPLE TOTALS: usage {total_usage}m CPU | requested CPU unavailable")

    sections.append("  POD DETAILS:")
    sections.extend(summary for _, summary in summaries[:25])
    if len(summaries) > 25:
        sections.append(f"  ... {len(summaries) - 25} more pods omitted")

    return "\n".join(sections)


def _fetch_live_pod_health() -> str:
    """Return the current pod health summary from kubectl when available."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "-A", "--no-headers"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        return "CURRENT POD HEALTH: kubectl is not installed or not on PATH."
    except subprocess.CalledProcessError as exc:
        error_output = (exc.stderr or exc.stdout or "kubectl get pods failed.").strip()
        return f"CURRENT POD HEALTH: unavailable. {error_output}"

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return "CURRENT POD HEALTH: no pods were returned by kubectl."

    unhealthy = []
    healthy = []
    for line in lines:
        parts = line.split()
        if len(parts) < 5:
            continue

        namespace, pod_name, ready, status = parts[0], parts[1], parts[2], parts[3]
        summary = f"{namespace}/{pod_name} | Ready: {ready} | Status: {status}"
        if status not in {"Running", "Completed"}:
            unhealthy.append(summary)
        else:
            healthy.append(summary)

    sections = ["CURRENT POD HEALTH:"]
    if unhealthy:
        sections.append("  UNHEALTHY PODS DETECTED:")
        sections.extend(f"    - {entry}" for entry in unhealthy)
        sections.append("  INCIDENT PRIORITY: Diagnose unhealthy pods before making cost recommendations.")
    else:
        sections.append("  No unhealthy pods detected in the current kubectl snapshot.")

    if healthy:
        preview = healthy[:5]
        sections.append("  HEALTHY POD SAMPLE:")
        sections.extend(f"    - {entry}" for entry in preview)
        if len(healthy) > len(preview):
            sections.append(f"    - ... {len(healthy) - len(preview)} more healthy pods omitted")

    return "\n".join(sections)


def fetch_all_workload_statuses(namespace: str = "default"):
    """
    Return live pod health, pod CPU usage, and current cost optimization constraints.
    Redirects back to the robust snapshot formatter to ensure table safety.
    """
    return fetch_cost_optimization_snapshot(namespace=namespace)


def send_rca_email(recipient_email: str, file_path: str | Path = REPORT_PATH):
    import google.auth
    from google.cloud import secretmanager

    sender_email = os.getenv("SENDER_EMAIL", "shyampadagala221@gmail.com")
    
    try:
        _, project_id = google.auth.default()
        client = secretmanager.SecretManagerServiceClient()
        secret_path = client.secret_version_path(project_id or "matchify-backend-shyamal-2026", "sre-agent-email-password", "latest")
        response = client.access_secret_version(request={"name": secret_path})
        sender_password = response.payload.data.decode("UTF-8")
    except Exception as exc:
        print(f"[NOTIFIER] Skipping email: Failed to fetch password from Secret Manager. {exc}")
        return

    report_path = Path(file_path)
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = f"PROJECT AUDIT: Critical SRE Report for {project_id or 'unknown'}"
    msg.attach(
        MIMEText(
            "The SRE Agent has completed its project-wide observation. See attached PDF.",
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
        print(f"[NOTIFIER] Success: Report sent to {recipient_email}")
    except Exception as e:
        print(f"[NOTIFIER] SMTP Error: {str(e)}")


class SREReport(FPDF):
    def header(self):
        self.set_font("Helvetica", "B", 12)
        self.set_text_color(120, 120, 120)
        self.cell(0, 10, "LUMEN DEMO - PROJECT-WIDE SRE OBSERVATION", 0, 1, "L")
        self.line(10, 20, 200, 20)
        self.ln(10)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        timestamp = datetime.now().strftime("%H:%M")
        self.cell(0, 10, f"Page {self.page_no()} | Generated by AI SRE Agent | {timestamp}", 0, 0, "C")


def generate_rca_report(content_text: str, file_path: str | Path = REPORT_PATH):
    import google.auth
    
    try:
        _, project_id = google.auth.default()
    except Exception:
        project_id = "unknown-project"

    pdf = SREReport()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(0, 40, 80)
    
    pdf.cell(0, 15, "Root Cause Analysis", ln=1) 
    
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
    print("[STAKEHOLDERS] Success: Professional PDF Generated.")


def create_and_send_report(analysis_summary: str, recipient_email: str | None = None):
    """Generate a professional PDF and email it to stakeholders."""
    try:
        print("\n[REPORT-TOOL] Starting report generation pipeline...")
        
        # 1. Generate the PDF file
        print(f"[REPORT-TOOL] Step 1: Compiling text analysis into PDF format...")
        generate_rca_report(analysis_summary)
        
        # 2. Determine who gets the email
        target_email = recipient_email or os.getenv("DEFAULT_RECIPIENT_EMAIL", "your-email@lumen.com")
        print(f"[REPORT-TOOL] Step 2: Routing destination set to: {target_email}")
        
        # 3. Trigger the email sender function
        print(f"[REPORT-TOOL] Step 3: Handoff to SMTP network manager to deliver attachment...")
        send_rca_email(target_email)
        
        print("[REPORT-TOOL] Status: Pipeline completed successfully!\n")
        return "SUCCESS: Professional RCA PDF generated and emailed."
        
    except Exception as e:
        print(f"[REPORT-TOOL] CRITICAL EXCEPTION: Pipeline failed on step execution: {str(e)}")
        return f"Report Generation Error: {str(e)}"


def build_fallback_report(logs: str, workloads: str, context: str = "") -> str:
    """Fallback report generator required by the agent initialization pipeline."""
    context_block = f"\nRUNBOOK CONTEXT\n{context}\n" if context else ""
    return f"""PROJECT STATUS SUMMARY (FALLBACK MODE)
Due to high API demand, this report was generated using deterministic logic.

RESOURCE SNAPSHOT
{workloads}

CRITICAL LOGS DISCOVERED
{logs}
{context_block}
"""

# ======================================================================
# CLOUDOPTIX ADDITIONS (Infrastructure, Secrets, and Billing Utilities)
# ======================================================================

def evaluate_cluster_infrastructure(namespace: str = "default") -> str:
    """
    Evaluates cluster infra: Unused Persistent Volumes / Disks, Node capacities.
    Addresses CloudOptix Use Case 04: AI-Powered Optimization (deleting unused disks, evaluating nodes).
    """
    try:
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return f"Kubernetes client configuration failed: {exc}"

    sections = ["## Infrastructure Evaluation Snapshot", f"- Namespace: {namespace}", ""]
    
    # Check Nodes
    try:
        nodes = core_v1.list_node().items
        node_stats = []
        for n in nodes:
            name = n.metadata.name
            cpu = n.status.allocatable.get('cpu', 'unknown')
            mem = n.status.allocatable.get('memory', 'unknown')
            node_stats.append(f"Node {name}: {cpu} cores, {mem} memory allocatable.")
        sections.append("### Node Pools")
        sections.append("\n".join(node_stats))
    except Exception as e:
        sections.append(f"Could not list nodes: {e}")

    # Check Unused PVCs (Disks)
    sections.append("\n### Persistent Volume Claims (Disks)")
    try:
        pvcs = core_v1.list_namespaced_persistent_volume_claim(namespace).items
        if not pvcs:
            sections.append("No PersistentVolumeClaims found in this namespace.")
        else:
            orphaned = []
            for pvc in pvcs:
                status = pvc.status.phase
                if status != "Bound":
                    orphaned.append(f"Unused/Unbound PVC: {pvc.metadata.name} | Request: {pvc.spec.resources.requests.get('storage')} | Status: {status}")
            if orphaned:
                sections.extend(orphaned)
            else:
                sections.append(f"Found {len(pvcs)} PVCs. All are currently Bound and attached.")
    except Exception as e:
        sections.append(f"Could not list PVCs: {e}")

    sections.append("")
    return "\n".join(sections)


def analyze_billing_vs_utilization(namespace: str = "default") -> str:
    """
    Calculates estimated effective billing vs actual utilization based on snapshot data.
    Addresses CloudOptix Use Case 03: Billing vs Utilization Analysis.
    Assuming approx $25/core/month and $3/GiB/month.
    """
    live_samples, error = _collect_pod_resource_samples(namespace)
    if error:
        return f"Billing analysis failed: {error}"

    total_wasted_cpu_millicores = 0.0
    total_wasted_memory_bytes = 0

    for sample in live_samples:
        # Calculate waste by comparing requested capacity vs actual usage
        cpu_request = sample.get('request_millicores', 0)
        cpu_usage = sample.get('usage_millicores', 0)
        mem_request = sample.get('memory_request_bytes', 0)
        mem_usage = sample.get('memory_usage_bytes', 0)

        cpu_waste = max(0, cpu_request - cpu_usage)
        mem_waste = max(0, mem_request - mem_usage)

        total_wasted_cpu_millicores += cpu_waste
        total_wasted_memory_bytes += mem_waste

    wasted_cores = total_wasted_cpu_millicores / 1000.0
    wasted_gb = total_wasted_memory_bytes / (1024**3)

    # Standard GCP general purpose pricing approximations (monthly)
    cost_per_core = 25.0
    cost_per_gb = 3.0

    wasted_cpu_cost_monthly = wasted_cores * cost_per_core
    wasted_mem_cost_monthly = wasted_gb * cost_per_gb
    total_wasted_monthly = wasted_cpu_cost_monthly + wasted_mem_cost_monthly

    return f"""## Billing vs. Utilization Analysis
- Namespace: {namespace}
- Methodology: Estimated effective billing based on active pod overprovisioning vs. GCP standard compute rates.

### Monthly Financial Waste Estimate
- **Wasted CPU Capacity:** {wasted_cores:.2f} cores (Estimated Waste: ${wasted_cpu_cost_monthly:.2f}/mo)
- **Wasted Memory Capacity:** {wasted_gb:.2f} GiB (Estimated Waste: ${wasted_mem_cost_monthly:.2f}/mo)
- **Total Estimated Inefficiency:** **${total_wasted_monthly:.2f}/mo**

*Note: True billing requires GCP Cloud Billing export to BigQuery. This is a heuristic based on live kubernetes resource waste.*
"""

def manage_kubernetes_secret(action: str, namespace: str, secret_name: str, key_values: str = None) -> str:
    """
    Utility tool to automate routine day-to-day operations for Developers.
    Addresses CloudOptix Use Case 05: Utility Functions for Secrets.
    action: 'create' or 'delete'.
    key_values: Format 'key1=value1,key2=value2' for creation.
    """
    import base64
    from kubernetes import client

    try:
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return f"Kubernetes client configuration failed: {exc}"

    if action == "delete":
        try:
            core_v1.delete_namespaced_secret(secret_name, namespace)
            return f"SUCCESS: Secret '{secret_name}' deleted from namespace '{namespace}'."
        except Exception as e:
            return f"Failed to delete secret: {e}"

    if action == "create":
        if not key_values:
            return "ERROR: must provide key_values to create a secret."
        
        try:
            data_dict = {}
            for pair in key_values.split(','):
                k, v = pair.split('=', 1)
                data_dict[k.strip()] = base64.b64encode(v.strip().encode('utf-8')).decode('utf-8')
            
            secret = client.V1Secret(
                api_version="v1",
                kind="Secret",
                metadata=client.V1ObjectMeta(name=secret_name),
                data=data_dict
            )
            core_v1.create_namespaced_secret(namespace=namespace, body=secret)
            return f"SUCCESS: Secret '{secret_name}' successfully created in namespace '{namespace}'."
        except Exception as e:
            return f"Failed to create secret: {e}"

    return "ERROR: Invalid action. Must be 'create' or 'delete'."