import asyncio
import os
import re
from dataclasses import dataclass

import google.auth
from google.genai import types

# ----------------------------------------------------------------------
# ENFORCE VERTEX AI BACKEND
# ----------------------------------------------------------------------
# This ensures google.genai uses Vertex AI ADC instead of Gemini API key.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"

if not os.getenv("GOOGLE_CLOUD_LOCATION"):
    os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"

try:
    _, _discovered_project = google.auth.default()

    if _discovered_project:
        os.environ["GOOGLE_CLOUD_PROJECT"] = _discovered_project
except Exception:
    pass


from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from .knowledge import get_developer_context
from .tools import (
    build_fallback_report,
    create_and_send_report,
    fetch_all_workload_statuses,
    fetch_cost_optimization_snapshot,
    fetch_historical_resource_analysis,
    fetch_pod_usage_since_start,
    evaluate_cluster_infrastructure,
    analyze_billing_vs_utilization,
    manage_kubernetes_secret,
    scan_gcp_resources,
    scan_full_gcp_resources,
    analyze_actual_gcp_billing,
    fetch_actual_gcp_billing,
    analyze_billing_efficiency,
    audit_iam_permissions,
    rotate_service_account_keys,
    enable_required_apis,
    list_cloud_resources_by_label,
    forecast_monthly_cost,
    predict_resource_growth,
    detect_cost_anomalies,
)


# ----------------------------------------------------------------------
# APP CONSTANTS
# ----------------------------------------------------------------------

DEFAULT_QUERY = "Show a 60-day historical resource analysis for the default namespace."
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID", "local_user")
APP_NAME = "CloudOptix"


# ----------------------------------------------------------------------
# PROJECT CONTEXT HELPERS
# ----------------------------------------------------------------------

def _get_project_context() -> dict:
    """
    Reads project details submitted from UI.

    Expected values:
    - project_id
    - billing_table
    - namespace
    - region/location/zone
    - billing_project_id optional
    """
    try:
        from .project_context import get_project_context
        return get_project_context() or {}
    except Exception:
        return {}


def _get_context_namespace(default: str = "default") -> str:
    context = _get_project_context()
    return context.get("namespace") or default


def _build_context_summary() -> str:
    context = _get_project_context()

    if not context:
        return "Project context is not set. User must submit project details from UI."

    return f"""
Current CloudOptix Project Context:
- project_id: {context.get("project_id", "not provided")}
- billing_table: {context.get("billing_table", "not provided")}
- billing_project_id: {context.get("billing_project_id", "not provided")}
- namespace: {context.get("namespace", "default")}
- region/location/zone: {context.get("region") or context.get("location") or context.get("zone") or "not provided"}
"""


def _update_context_from_query(user_query: str):
    try:
        from .project_context import set_project_context
    except Exception:
        return

    existing = _get_project_context().copy()
    updated = False

    project_match = None

    # Only accept explicit project_id forms to avoid false matches such as
    # "Project Context" being parsed as project ID "Context".
    for pattern in (
        r"\bproject_id\s*[:=]\s*['\"]?([a-z][a-z0-9-]{4,})['\"]?\b",
        r"\bproject\s+id\s*[:=]\s*['\"]?([a-z][a-z0-9-]{4,})['\"]?\b",
        r"['\"]project_id['\"]\s*:\s*['\"]([a-z][a-z0-9-]{4,})['\"]",
    ):
        project_match = re.search(pattern, user_query, re.IGNORECASE)

        if project_match:
            break

    if project_match:
        project_id = project_match.group(1)

        if existing.get("project_id") != project_id:
            existing["project_id"] = project_id
            updated = True

    namespace_match = None

    for pattern in (
        r"\bnamespace\s*[:=]\s*['\"]?([a-z0-9-]+)['\"]?\b",
        r"['\"]namespace['\"]\s*:\s*['\"]([a-z0-9-]+)['\"]",
    ):
        namespace_match = re.search(pattern, user_query, re.IGNORECASE)

        if namespace_match:
            break

    if namespace_match:
        namespace = namespace_match.group(1)

        if existing.get("namespace") != namespace:
            existing["namespace"] = namespace
            updated = True

    if updated:
        set_project_context(existing)


# ----------------------------------------------------------------------
# CLOUDOPTIX ADK AGENT
# ----------------------------------------------------------------------

root_agent = Agent(
    name="CloudOptix_Insight_to_Optimize",
    model="gemini-2.5-flash",
    instruction="""
You are CloudOptix - Insight to Optimize, an AI-powered Google Cloud optimization agent.

Your purpose is to help Leadership, Cloud Administrators, DevOps Engineers, and Developers gain full visibility into GCP resources, understand usage trends, compare billing with utilization, and identify safe cost optimization opportunities.

IMPORTANT PROJECT CONTEXT RULES:
- Always use the GCP project context submitted from the UI.
- Do not ask users to edit .env files for project-specific details.
- Project-specific values such as project_id, billing_table, namespace, region, cluster, and billing_project_id must come from UI-submitted context.
- If project_id is missing, tell the user to submit project details from the UI first.
- If billing_table is missing and the user asks for actual billing, tell the user to submit the BigQuery billing table from the UI.

CORE WORKFLOW:
1. For full cloud inventory, use scan_full_gcp_resources.
2. For basic VM and bucket discovery, use scan_gcp_resources.
3. Review current workload usage using fetch_cost_optimization_snapshot.
4. For historical trends, use fetch_historical_resource_analysis.
5. For actual billing, use analyze_actual_gcp_billing.
6. For estimated billing waste vs utilization, use analyze_billing_vs_utilization.
7. For disks, PVCs, nodes, or infrastructure checks, use evaluate_cluster_infrastructure.
8. For pod-level usage since start, use fetch_pod_usage_since_start.
9. For developer documentation or runbook context, use get_developer_context.
10. For exportable reports, use create_and_send_report only when explicitly requested.

TOOL SELECTION:
- If user asks "scan full GCP resources", "full GCP resources", "scan all resources", "list all GCP resources", "VPC", "subnets", "firewall", "GKE clusters", or "Artifact Registry", call scan_full_gcp_resources.
- If user asks only "scan GCP resources", "discover resources", "list VMs", or "list buckets", call scan_gcp_resources.
- If user asks "actual billing", "true billing", "billing export", "BigQuery billing", or "cost drivers", call analyze_actual_gcp_billing.
- If user asks "billing vs utilization", "effective billing", "waste", or "over-provisioned cost", call analyze_billing_vs_utilization.
- If user asks "live cost", "current usage", "cost optimization snapshot", or "current snapshot", call fetch_cost_optimization_snapshot.
- If user asks "historical", "30 day", "60 day", "usage trend", or "rightsizing", call fetch_historical_resource_analysis.
- If user asks about disks, PVCs, node pools, unused disks, empty nodes, or infrastructure, call evaluate_cluster_infrastructure.
- If user asks about pod usage since start, call fetch_pod_usage_since_start.
- If user asks to create/delete/manage Kubernetes secrets, use manage_kubernetes_secret.

SAFETY:
- Treat create, delete, patch, resize, restart, and secret changes as write operations.
- Do not perform destructive or write actions unless approval is clearly provided.
- Never expose secret values in the response.
- For production-impacting changes, recommend approval and rollback validation.

RESPONSE FORMAT:
- Answer the exact user question.
- Use clear markdown.
- Keep fixed-width tables exactly as returned by tools.
- For leadership, summarize business impact and savings.
- For DevOps, include technical detail and recommended next actions.
- For developers, keep utility instructions simple and safe.
""",
    tools=[
        fetch_cost_optimization_snapshot,
        fetch_historical_resource_analysis,
        fetch_pod_usage_since_start,
        fetch_all_workload_statuses,
        get_developer_context,
        create_and_send_report,
        evaluate_cluster_infrastructure,
        analyze_billing_vs_utilization,
        manage_kubernetes_secret,
        scan_gcp_resources,
        scan_full_gcp_resources,
        analyze_actual_gcp_billing,
        fetch_actual_gcp_billing,
        analyze_billing_efficiency,
        audit_iam_permissions,
        rotate_service_account_keys,
        enable_required_apis,
        list_cloud_resources_by_label,
        forecast_monthly_cost,
        predict_resource_growth,
        detect_cost_anomalies,
    ],
)


# ----------------------------------------------------------------------
# AGENT MANAGER
# ----------------------------------------------------------------------

@dataclass(slots=True)
class SREAgentManager:
    """
    Coordinates CloudOptix agent execution, direct tool routing,
    fallback handling, and optional report generation.
    """

    agent: Agent
    app_name: str = APP_NAME

    def _try_direct_tool_response(self, user_query: str) -> str | None:
        """
        Direct deterministic routing for common CloudOptix operations.

        This avoids unnecessary LLM calls for clear tool requests.
        """
        normalized = user_query.strip().lower()
        namespace = self._extract_namespace(user_query)

        # --------------------------------------------------------------
        # FULL GCP RESOURCE DISCOVERY
        # --------------------------------------------------------------
        # IMPORTANT:
        # This block must come BEFORE the basic scan block.
        # Otherwise "Scan full GCP resources" will match "scan gcp"
        # and call the old basic scan.
        if (
            "scan full gcp resources" in normalized
            or "full gcp resources" in normalized
            or "full resource discovery" in normalized
            or "scan all resources" in normalized
            or "list all gcp resources" in normalized
            or "vpc" in normalized
            or "subnet" in normalized
            or "subnets" in normalized
            or "firewall" in normalized
            or "gke clusters" in normalized
            or "artifact registry" in normalized
            or "artifact repositories" in normalized
        ):
            return scan_full_gcp_resources()

        # --------------------------------------------------------------
        # BASIC GCP RESOURCE DISCOVERY
        # --------------------------------------------------------------
        if (
            "scan gcp" in normalized
            or "discover resources" in normalized
            or "resource discovery" in normalized
            or "list vms" in normalized
            or "list buckets" in normalized
            or "gcp resources" in normalized
        ):
            return scan_gcp_resources()

        # --------------------------------------------------------------
        # TRUE BILLING FROM BIGQUERY
        # --------------------------------------------------------------
        if (
            "actual billing" in normalized
            or "true billing" in normalized
            or "billing export" in normalized
            or "bigquery billing" in normalized
            or "cost drivers" in normalized
            or "top cost" in normalized
        ):
            return analyze_actual_gcp_billing(
                days=self._extract_days(user_query, default_days=30)
            )

        # --------------------------------------------------------------
        # BILLING VS UTILIZATION
        # --------------------------------------------------------------
        if (
            "billing vs utilization" in normalized
            or "effective billing" in normalized
            or "billing utilization" in normalized
            or "estimated waste" in normalized
            or "waste" in normalized
            or "over-provisioned cost" in normalized
        ):
            return analyze_billing_vs_utilization(namespace=namespace)

        # --------------------------------------------------------------
        # INFRASTRUCTURE EVALUATION
        # --------------------------------------------------------------
        if (
            "infrastructure evaluation" in normalized
            or "evaluate infrastructure" in normalized
            or "unused disks" in normalized
            or "pvc" in normalized
            or "persistent volume" in normalized
            or "node pools" in normalized
            or "nodes" in normalized
        ):
            return evaluate_cluster_infrastructure(namespace=namespace)

        # --------------------------------------------------------------
        # HISTORICAL RESOURCE ANALYSIS
        # --------------------------------------------------------------
        if (
            "historical resource analysis" in normalized
            or "historical" in normalized
            or "30 day" in normalized
            or "60 day" in normalized
            or "usage trend" in normalized
            or "rightsizing" in normalized
        ):
            return fetch_historical_resource_analysis(
                namespace=namespace,
                days=self._extract_days(user_query, default_days=60),
            )

        # --------------------------------------------------------------
        # POD USAGE SINCE START
        # --------------------------------------------------------------
        if (
            "pod usage since start" in normalized
            or "since start" in normalized
            or "pod start" in normalized
        ):
            return fetch_pod_usage_since_start(namespace=namespace)

        # --------------------------------------------------------------
        # LIVE COST SNAPSHOT
        # --------------------------------------------------------------
        if (
            "cost optimization snapshot" in normalized
            or "current snapshot" in normalized
            or "live cost" in normalized
            or "current usage" in normalized
            or "current utilization" in normalized
        ):
            return fetch_cost_optimization_snapshot(namespace=namespace)

        # --------------------------------------------------------------
        # ALL WORKLOAD STATUSES
        # --------------------------------------------------------------
        if (
            "all workload statuses" in normalized
            or "workload status" in normalized
            or "broader operational view" in normalized
        ):
            return fetch_all_workload_statuses(namespace=namespace)

        # ============================================================
        # PHASE 1: BILLING ANALYSIS - USE CASE 03
        # ============================================================
        if (
            "actual billing" in normalized
            or "real billing" in normalized
            or "bigquery billing" in normalized
            or "true cost" in normalized
        ):
            return fetch_actual_gcp_billing(
                days=self._extract_days(user_query, default_days=30)
            )

        if (
            "billing efficiency" in normalized
            or "cost vs utilization" in normalized
            or "billing vs utilization" in normalized
            or "efficiency analysis" in normalized
        ):
            return analyze_billing_efficiency(
                namespace=namespace,
                days=self._extract_days(user_query, default_days=30)
            )

        # ============================================================
        # PHASE 2: GCP UTILITY FUNCTIONS - USE CASE 05
        # ============================================================
        if (
            "audit iam" in normalized
            or "iam permissions" in normalized
            or "check permissions" in normalized
            or "service account access" in normalized
        ):
            return audit_iam_permissions(project_id=_get_context_project_id())

        if (
            "rotate key" in normalized
            or "rotate service account" in normalized
            or "key rotation" in normalized
            or "token rotation" in normalized
        ):
            return rotate_service_account_keys(project_id=_get_context_project_id())

        if (
            "enable api" in normalized
            or "required api" in normalized
            or "api status" in normalized
            or "gcp api" in normalized
        ):
            return enable_required_apis(project_id=_get_context_project_id())

        if (
            "resources by label" in normalized
            or "label query" in normalized
            or "find resources" in normalized
            or "resource discovery" in normalized
        ):
            return list_cloud_resources_by_label(
                project_id=_get_context_project_id()
            )

        # ============================================================
        # PHASE 3: AI OPTIMIZATION - USE CASE 04
        # ============================================================
        if (
            "forecast cost" in normalized
            or "cost forecast" in normalized
            or "projected cost" in normalized
            or "monthly forecast" in normalized
        ):
            return forecast_monthly_cost(
                project_id=_get_context_project_id(),
                days=self._extract_days(user_query, default_days=30)
            )

        if (
            "resource growth" in normalized
            or "growth prediction" in normalized
            or "predict capacity" in normalized
            or "capacity planning" in normalized
        ):
            return predict_resource_growth(
                namespace=namespace,
                days=self._extract_days(user_query, default_days=30)
            )

        if (
            "cost anomal" in normalized
            or "detect anomal" in normalized
            or "cost spike" in normalized
            or "unusual cost" in normalized
        ):
            return detect_cost_anomalies(project_id=_get_context_project_id())

        return None

    @staticmethod
    def _extract_namespace(user_query: str) -> str:
        """
        Extract namespace from query if mentioned.
        Otherwise use UI-submitted namespace.
        """
        match = re.search(r"\bnamespace\s+([a-z0-9-]+)", user_query, re.IGNORECASE)

        if match:
            return match.group(1)

        return _get_context_namespace("default")

    @staticmethod
    def _extract_days(user_query: str, default_days: int = 60) -> int:
        """
        Extract 7/30/60/90 day window from user query.
        """
        match = re.search(r"\b(7|30|60|90)\s*[- ]?day\b", user_query, re.IGNORECASE)

        if match:
            return int(match.group(1))

        return default_days

    async def run_query(self, user_query: str, user_id: str = DEFAULT_USER_ID) -> str:
        """
        Execute query through ADK runner.
        """
        session_service = InMemorySessionService()

        runner = Runner(
            agent=self.agent,
            app_name=self.app_name,
            session_service=session_service,
        )

        try:
            session = await session_service.create_session(
                app_name=self.app_name,
                user_id=user_id,
            )

            full_response = ""

            async for event in runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=types.Content(
                    role="user",
                    parts=[types.Part(text=user_query)],
                ),
            ):
                if event.content and event.content.parts:
                    full_response += "".join(
                        part.text or ""
                        for part in event.content.parts
                    )

            return full_response.strip()

        finally:
            try:
                await runner.close()
            except Exception:
                pass

    async def handle_query(
        self,
        user_query: str,
        user_id: str = DEFAULT_USER_ID,
        recipient: str | None = None,
        auto_send_report: bool = False,
    ) -> str:
        """
        Main query handler used by remote_mcp_server.py.
        """
        _update_context_from_query(user_query)

        context_summary = _build_context_summary()

        enhanced_query = f"""
{context_summary}

User Query:
{user_query}
"""

        direct_response = self._try_direct_tool_response(user_query)

        if direct_response is not None:
            if auto_send_report and direct_response:
                create_and_send_report(
                    direct_response,
                    recipient_email=recipient,
                )

            return direct_response

        try:
            response = await self.run_query(
                user_query=enhanced_query,
                user_id=user_id,
            )

        except Exception as exc:
            if not self._is_rate_limit_error(exc):
                raise

            print("\n[RESILIENCE] Rate limit hit. Triggering fallback logic...")
            response = self._build_fallback_response()

        if auto_send_report and response:
            create_and_send_report(
                response,
                recipient_email=recipient,
            )

        return response

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        error_text = str(exc)
        return "429" in error_text or "RESOURCE_EXHAUSTED" in error_text

    @staticmethod
    def _build_fallback_response() -> str:
        return build_fallback_report(
            logs="Cost-only mode: incident log collection is not enabled in this agent configuration.",
            workloads=fetch_all_workload_statuses(),
            context=get_developer_context("cost optimization"),
        )


# ----------------------------------------------------------------------
# GLOBAL MANAGER
# ----------------------------------------------------------------------

manager = SREAgentManager(agent=root_agent)


# ----------------------------------------------------------------------
# PUBLIC HELPER
# ----------------------------------------------------------------------

async def run_sre_query(user_query: str, user_id: str = DEFAULT_USER_ID) -> str:
    """
    Public helper used by CLI or imports.
    """
    return await manager.handle_query(
        user_query=user_query,
        user_id=user_id,
    )


# ----------------------------------------------------------------------
# TERMINAL MODE
# ----------------------------------------------------------------------

async def main(
    user_query: str = DEFAULT_QUERY,
    user_id: str = DEFAULT_USER_ID,
    recipient: str | None = None,
    auto_send_report: bool = False,
):
    print("--- Running CloudOptix Agent Terminal Mode ---")

    try:
        full_response = await manager.handle_query(
            user_query=user_query,
            user_id=user_id,
            recipient=recipient,
            auto_send_report=auto_send_report,
        )

        if full_response:
            print(full_response)

    except Exception as exc:
        print(f"\n[ERROR]: {str(exc)}")


if __name__ == "__main__":
    asyncio.run(main())