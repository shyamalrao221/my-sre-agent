import os
import re
import smtplib
import time
import subprocess  # Used for running kubectl diagnostics and patches
from collections import defaultdict
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from fpdf import FPDF

load_dotenv()

REPORT_PATH = Path(__file__).resolve().parents[1] / "Formal_RCA_Report.pdf"


def _parse_cpu_to_millicores(cpu_value: str | None) -> int:
    if not cpu_value:
        return 0

    raw_value = str(cpu_value).strip()
    if not raw_value:
        return 0

    if raw_value.endswith("m"):
        return int(float(raw_value[:-1]))

    if raw_value.endswith("n"):
        return int(float(raw_value[:-1]) / 1_000_000)

    if raw_value.endswith("u"):
        return int(float(raw_value[:-1]) / 1_000)

    return int(float(raw_value) * 1000)


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


def _percentile(values: list[float], percentile_value: int) -> float:
    if not values:
        return 0.0

    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round((percentile_value / 100) * (len(ordered) - 1))))
    return float(ordered[rank])


def _format_cpu_summary(usage_millicores: int, request_millicores: int) -> str:
    if request_millicores <= 0:
        return (
            f"Current CPU usage: {usage_millicores}m\n"
            "        -> Requested CPU: not set\n"
            "        -> Utilization vs request: unavailable because no CPU request is defined"
        )

    utilization = round((usage_millicores / request_millicores) * 100, 1)
    return (
        f"Current CPU usage: {usage_millicores}m\n"
        f"        -> Requested CPU: {request_millicores}m\n"
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
        core_v1, _ = _load_kube_clients()
    except Exception as exc:
        return {}, f"Kubernetes client configuration failed: {exc}"

    try:
        pods = core_v1.list_namespaced_pod(namespace, watch=False).items
    except Exception as exc:
        return {}, f"Could not list pods in namespace {namespace}: {exc}"

    grouped = defaultdict(
        lambda: {
            "pods": 0,
            "cpu_request_millicores": 0,
            "cpu_limit_millicores": 0,
            "memory_request_bytes": 0,
            "memory_limit_bytes": 0,
        }
    )

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


def _query_historical_metric_series(
    metric_type: str,
    namespace: str,
    days: int,
    aligner,
) -> tuple[dict[str, list[float]], str | None]:
    from google.cloud import monitoring_v3

    project_id = os.getenv("GCP_PROJECT_ID")
    if not project_id:
        return {}, "GCP_PROJECT_ID is not set."

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
    metric_filter = (
        f'metric.type = "{metric_type}" '
        f'AND resource.labels.namespace_name = "{namespace}"'
    )

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
            signals.append(f"CPU request looks high versus historical P95 at {cpu_ratio}% of request.")
        elif cpu_ratio > 80:
            signals.append(f"CPU request is already close to historical P95 at {cpu_ratio}% of request.")
        else:
            signals.append(f"CPU request is in a moderate band with historical P95 at {cpu_ratio}% of request.")
    else:
        signals.append("CPU request comparison is unavailable.")

    if memory_request_bytes > 0 and memory_p95_bytes > 0:
        memory_ratio = round((memory_p95_bytes / memory_request_bytes) * 100, 1)
        if memory_ratio < 50:
            signals.append(f"Memory request looks high versus historical P95 at {memory_ratio}% of request.")
        elif memory_ratio > 85:
            signals.append(f"Memory request is already close to historical P95 at {memory_ratio}% of request.")
        else:
            signals.append(f"Memory request is in a moderate band with historical P95 at {memory_ratio}% of request.")
    else:
        signals.append("Memory request comparison is unavailable.")

    return " ".join(signals)


def fetch_historical_resource_analysis(namespace: str = "default", days: int = 30):
    """Return 30-60 day CPU and memory history for application workloads when Cloud Monitoring data exists."""
    from google.cloud import monitoring_v3

    try:
        window_days = int(days)
    except (TypeError, ValueError):
        return "HISTORICAL RESOURCE ANALYSIS: invalid days value. Provide an integer between 1 and 60."

    if window_days < 1:
        return "HISTORICAL RESOURCE ANALYSIS: invalid days value. Provide an integer between 1 and 60."

    if window_days > 60:
        window_days = 60

    current_resources, current_resource_error = _group_current_workload_resources(namespace)
    cpu_series, cpu_error = _query_historical_metric_series(
        metric_type="kubernetes.io/container/cpu/core_usage_time",
        namespace=namespace,
        days=window_days,
        aligner=monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
    )
    memory_series, memory_error = _query_historical_metric_series(
        metric_type="kubernetes.io/container/memory/used_bytes",
        namespace=namespace,
        days=window_days,
        aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
    )

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
        f"HISTORICAL RESOURCE ANALYSIS: namespace {namespace} | window {window_days}d",
        "  DATA SOURCE: Google Cloud Monitoring aligned CPU and memory series plus current pod resource settings.",
        "  INTERPRETATION: this is the right input for organization-level rightsizing discussion, but it is still advisory until you add approvals and rollback flow.",
    ]

    if current_resource_error:
        sections.append(f"  CURRENT RESOURCE CONFIG: unavailable. {current_resource_error}")
    if cpu_error:
        sections.append(f"  CPU HISTORY: unavailable. {cpu_error}")
    if memory_error:
        sections.append(f"  MEMORY HISTORY: unavailable. {memory_error}")
    if historical_only_workloads and current_resources:
        sections.append(
            f"  HISTORICAL-ONLY WORKLOADS OMITTED: {', '.join(historical_only_workloads[:5])}"
            + (f" and {len(historical_only_workloads) - 5} more" if len(historical_only_workloads) > 5 else "")
        )

    sections.append("  WORKLOADS:")

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

        sections.append(f"    - Workload: {workload_name}")
        sections.append(
            "      CPU: "
            f"p50 {_format_millicores_value(cpu_p50, default='not set', zero_is_value=bool(cpu_values_millicores))} | "
            f"p95 {_format_millicores_value(cpu_p95, default='not set', zero_is_value=bool(cpu_values_millicores))} | "
            f"peak {_format_millicores_value(cpu_peak, default='not set', zero_is_value=bool(cpu_values_millicores))} | "
            f"current request {_format_millicores_value(resource_state['cpu_request_millicores'] or None, zero_is_value=False)} | "
            f"current limit {_format_millicores_value(resource_state['cpu_limit_millicores'] or None, zero_is_value=False)}"
        )
        sections.append(
            "      Memory: "
            f"p50 {_format_mebibytes_value(memory_p50_mib, default='not set', zero_is_value=bool(memory_values_bytes))} | "
            f"p95 {_format_mebibytes_value(memory_p95_mib, default='not set', zero_is_value=bool(memory_values_bytes))} | "
            f"peak {_format_mebibytes_value(memory_peak_mib, default='not set', zero_is_value=bool(memory_values_bytes))} | "
            f"current request {_format_mebibytes_value((resource_state['memory_request_bytes'] / (1024 * 1024)) if resource_state['memory_request_bytes'] else None, zero_is_value=False)} | "
            f"current limit {_format_mebibytes_value((resource_state['memory_limit_bytes'] / (1024 * 1024)) if resource_state['memory_limit_bytes'] else None, zero_is_value=False)}"
        )
        sections.append(
            f"      Sample coverage: CPU {len(cpu_values_millicores)} aligned points | Memory {len(memory_values_bytes)} aligned points | Current pods {resource_state['pods']}"
        )
        sections.append(f"      Historical signal: {signal}")

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
        else:
            pod_items = core_v1.list_pod_for_all_namespaces(watch=False).items
    except Exception as exc:
        return [], f"Could not list pod specs to fetch CPU requests: {exc}"

    pod_specs = {
        (pod.metadata.namespace, pod.metadata.name): pod
        for pod in pod_items
    }
    samples = []

    for item in metric_items:
        metadata = item.get("metadata", {})
        pod_namespace = metadata.get("namespace", namespace or "default")
        pod_name = metadata.get("name", "unknown-pod")

        containers = item.get("containers", [])
        usage_millicores = sum(
            _parse_cpu_to_millicores(container.get("usage", {}).get("cpu"))
            for container in containers
        )

        pod_spec = pod_specs.get((pod_namespace, pod_name))
        request_millicores = 0
        limit_millicores = 0
        status = "Unknown"
        ready_text = "0/0"
        workload_name = pod_name

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
            ready_count = sum(1 for status_item in (pod_spec.status.container_statuses or []) if status_item.ready)
            total_count = len(pod_spec.status.container_statuses or [])
            ready_text = f"{ready_count}/{total_count}" if total_count else "0/0"
            status = _get_pod_status(pod_spec)
            workload_name = _derive_workload_name(pod_spec)

        samples.append(
            {
                "namespace": pod_namespace,
                "pod_name": pod_name,
                "workload_name": workload_name,
                "usage_millicores": usage_millicores,
                "request_millicores": request_millicores,
                "limit_millicores": limit_millicores,
                "status": status,
                "ready": ready_text,
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
        "request_millicores": 0,
        "limit_millicores": 0,
        "statuses": [],
    })

    for sample in app_samples:
        workload = grouped[sample["workload_name"]]
        workload["pods"] += 1
        workload["usage_millicores"] += sample["usage_millicores"]
        workload["request_millicores"] += sample["request_millicores"]
        workload["limit_millicores"] += sample["limit_millicores"]
        workload["statuses"].append(sample["status"])

    sections = [
        f"COST OPTIMIZATION SNAPSHOT: namespace {namespace}",
        "  SCOPE: live Kubernetes metrics and pod specs only.",
        "  HISTORICAL CONFIDENCE: unavailable for 30- or 60-day rightsizing because this implementation does not yet persist long-range usage history.",
        "  RECOMMENDATION RULE: use this output for current over-provisioning signals only, not for committed savings estimates.",
        "  WORKLOADS:",
    ]

    for workload_name in sorted(grouped):
        workload = grouped[workload_name]
        request_millicores = workload["request_millicores"]
        usage_millicores = workload["usage_millicores"]
        limit_millicores = workload["limit_millicores"]
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

        sections.append(f"    - Workload: {workload_name}")
        sections.append(f"      Pods: {workload['pods']} | Usage: {usage_millicores}m | Request: {request_millicores or 'not set'}m | Limit: {limit_millicores or 'not set'}m")
        sections.append(f"      Utilization vs request: {utilization_text}")
        sections.append(f"      Health gate: {'blocked by unhealthy pods' if unhealthy else 'clear'}")
        sections.append(f"      Recommendation: {recommendation}")

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
    try:
        core_v1, custom_api = _load_kube_clients()
    except ImportError:
        return "CURRENT POD USAGE: unavailable. Install the `kubernetes` package to fetch live pod CPU usage."
    except Exception as exc:
        return f"CURRENT POD USAGE: unavailable. Kubernetes client configuration failed: {exc}"

    try:
        metrics_response = custom_api.list_cluster_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            plural="pods",
        )
    except Exception as exc:
        return (
            "CURRENT POD USAGE: unavailable. Could not query metrics.k8s.io for pod usage. "
            f"{exc}"
        )

    metric_items = metrics_response.get("items", [])
    if not metric_items:
        return "CURRENT POD USAGE: unavailable. The Kubernetes metrics API returned no pod metrics."

    try:
        pod_list = core_v1.list_pod_for_all_namespaces(watch=False).items
    except Exception as exc:
        return f"CURRENT POD USAGE: unavailable. Could not list pod specs to fetch CPU requests: {exc}"

    pod_specs = {
        (pod.metadata.namespace, pod.metadata.name): pod
        for pod in pod_list
    }

    summaries = []
    unhealthy = []
    total_usage = 0
    total_request = 0

    for item in metric_items:
        metadata = item.get("metadata", {})
        namespace = metadata.get("namespace", "default")
        pod_name = metadata.get("name", "unknown-pod")

        containers = item.get("containers", [])
        usage_millicores = sum(
            _parse_cpu_to_millicores(container.get("usage", {}).get("cpu"))
            for container in containers
        )

        pod_spec = pod_specs.get((namespace, pod_name))
        request_millicores = 0
        status = "Unknown"
        ready_text = "?/?"
        if pod_spec is not None:
            request_millicores = sum(
                _parse_cpu_to_millicores(container.resources.requests.get("cpu"))
                for container in pod_spec.spec.containers
                if container.resources and container.resources.requests
            )
            ready_count = sum(1 for status_item in (pod_spec.status.container_statuses or []) if status_item.ready)
            total_count = len(pod_spec.status.container_statuses or [])
            ready_text = f"{ready_count}/{total_count}" if total_count else "0/0"
            status = pod_spec.status.phase or "Unknown"
            if any((status_item.state.waiting and status_item.state.waiting.reason) for status_item in (pod_spec.status.container_statuses or [])):
                waiting_reasons = [
                    status_item.state.waiting.reason
                    for status_item in pod_spec.status.container_statuses
                    if status_item.state and status_item.state.waiting and status_item.state.waiting.reason
                ]
                if waiting_reasons:
                    status = waiting_reasons[0]

        summary = (
            f"  Pod: {namespace}/{pod_name}\n"
            f"        -> Ready: {ready_text}\n"
            f"        -> Status: {status}\n"
            f"        -> {_format_cpu_summary(usage_millicores, request_millicores)}"
        )
        summaries.append((status, summary))
        total_usage += usage_millicores
        total_request += request_millicores

        if status not in {"Running", "Completed", "Succeeded"}:
            unhealthy.append(f"{namespace}/{pod_name} | Status: {status} | CPU: {usage_millicores}m")

    summaries.sort(key=lambda item: (item[0] in {"Running", "Completed", "Succeeded"}, item[0], item[1]))

    sections = ["CURRENT POD USAGE:"]
    if unhealthy:
        sections.append("  INCIDENT PRIORITY PODS:")
        sections.extend(f"    - {entry}" for entry in unhealthy[:10])

    if total_request > 0:
        overall_utilization = round((total_usage / total_request) * 100, 1)
        sections.append(
            f"  CLUSTER SAMPLE TOTALS: usage {total_usage}m CPU | requested {total_request}m CPU | utilization {overall_utilization}%"
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


def fetch_all_workload_statuses():
    """Return live pod health, pod CPU usage, and any real monitoring metrics available."""
    try:
        from google.cloud import container_v1
        from google.cloud import monitoring_v3

        project_id = os.getenv("GCP_PROJECT_ID")
        if not project_id:
            return "GKE Discovery Error: GCP_PROJECT_ID is not set."

        client = container_v1.ClusterManagerClient()
        response = client.list_clusters(parent=f"projects/{project_id}/locations/-")

        inventory = []
        inventory.append(_fetch_live_pod_health())
        inventory.append(_fetch_kubernetes_usage_summary())
        inventory.append(
            "METRIC WINDOW NOTICE: This implementation currently queries roughly the last 1 hour of Cloud Monitoring data, not 60 days."
        )
        inventory.append(
            "If historical metrics are missing, treat cost optimization output as unavailable instead of estimating savings from placeholders."
        )

        # Connect directly to the Cloud Monitoring API endpoint
        metrics_client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{project_id}"
        
        now = time.time()
        interval = monitoring_v3.TimeInterval({
            "end_time": {"seconds": int(now)},
            "start_time": {"seconds": int(now) - 3600}, 
        })

        if not response.clusters:
            return "PROJECT-WIDE GKE SNAPSHOT:\n" + "\n".join(inventory) + "\nNo GKE clusters found."

        for cluster in response.clusters:
            inventory.append(f"Cluster: {cluster.name} | Status: {cluster.status} | Location: {cluster.location}")
            inventory.append("    LIVE POD RESOURCE ANALYSIS (FROM GOOGLE CLOUD METRICS):")
            
            # PURE PYTHON PIPELINE: Bypasses terminal 'gcloud' subprocess execution to prevent Windows env errors
            try:
                cpu_filter = (
                    f'metric.type = "kubernetes.io/container/cpu/core_usage_time" '
                    f'AND resource.labels.cluster_name = "{cluster.name}"'
                )
                results = metrics_client.list_time_series(
                    name=project_name, filter=cpu_filter, interval=interval,
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL
                )

                found_metrics = False
                for ts in results:
                    pod = ts.resource.labels.get("pod_name", "unknown-pod")
                    ns = ts.resource.labels.get("namespace_name", "default")
                    container_name = ts.resource.labels.get("container_name", "app")
                    
                    if ns in ["kube-system", "gke-gmp-system"]:
                        continue  # Clear out platform cluster noise
                        
                    if ts.points:
                        found_metrics = True
                        actual_cpu_cores = ts.points[0].value.double_value
                        actual_milli_cores = round(actual_cpu_cores * 1000, 1)
                        
                        inventory.append(
                            f"      Pod: {pod} [Namespace: {ns}]\n"
                            f"        -> Container: {container_name}\n"
                            f"        -> ACTUAL RESOURCE CONSUMPTION: {actual_milli_cores}m CPU\n"
                            f"        -> NOTE: Requested and limited CPU were not fetched by this tool, so do not claim over-provisioning from this data alone."
                        )

                if not found_metrics:
                    inventory.append(
                        "      No real CPU time-series were returned for this cluster in the current 1-hour query window.\n"
                        "      COST ANALYSIS STATUS: unavailable. Do not estimate 60-day savings or invent pod-level optimization values."
                    )
            except Exception as metric_err:
                inventory.append(
                    "      COST ANALYSIS STATUS: unavailable due to metrics query failure.\n"
                    f"      Metrics query trace exception: {metric_err}"
                )

        return "PROJECT-WIDE GKE SNAPSHOT:\n" + "\n".join(inventory) if inventory else "No GKE clusters found."
    except Exception as e:
        return f"GKE Discovery Error: {str(e)}"


def send_rca_email(recipient_email: str, file_path: str | Path = REPORT_PATH):
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_PASSWORD")
    if not sender_email or not sender_password:
        print("[NOTIFIER] Skipping email: Credentials missing in .env")
        return

    report_path = Path(file_path)
    msg = MIMEMultipart()
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = f"PROJECT AUDIT: Critical SRE Report for {os.getenv('GCP_PROJECT_ID')}"
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
    pdf.cell(0, 8, f" {os.getenv('GCP_PROJECT_ID')}", 1, 1, "L")
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