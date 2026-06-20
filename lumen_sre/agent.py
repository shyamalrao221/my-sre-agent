import asyncio
import os
import re
from dataclasses import dataclass
from google.genai import types
import google.auth

# --- ENFORCE VERTEX AI BACKEND VIA DYNAMIC DISCOVERY ---
# This ensures google.genai uses Vertex AI (ADC) instead of complaining about missing Gemini API keys.
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
)

# --- 3. THE SRE ENGINE (ADK ENTRY POINT) ---

root_agent = Agent(
    name="CloudOptix_SRE_Agent",
    model="gemini-2.5-flash",
    instruction="""
You are a Lead Google Cloud SRE functioning as the core "CloudOptix - Insight to Optimize" agent. 
Your goal is to provide full workload visibility, AI-driven optimization recommendations, financial billing analysis, and day-to-day developer utilities.

1. **Optimization vs Billing:** If the user asks for financial or billing analysis vs utilization, call analyze_billing_vs_utilization first.
2. **Infrastructure:** If the user asks about disks, PVCs, empty node pools, or cluster infrastructure, call evaluate_cluster_infrastructure.
3. **Utility Secrets Check/Manage:** If the user wants to create or delete a Kubernetes secret (acting as a developer utility), use the manage_kubernetes_secret tool.
4. For long-term cost questions, call fetch_historical_resource_analysis first so the answer is based on 30- to 60-day CPU and memory history.
5. For live cost questions, call fetch_cost_optimization_snapshot. 
6. If the user asks for pod-instance usage from pod start until now, call fetch_pod_usage_since_start.
7. Call get_developer_context when documentation or architecture context helps.
8. Only call create_and_send_report when asked for a formal RCA PDF or exportable report.
9. Answer the user's exact query closely. Use strict markdown. If a fixed-width table is provided by the tool, MUST maintain the table code blocks exactly as given.
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
    ]
)

import os

DEFAULT_QUERY = "Show a 60-day historical resource analysis for the default namespace."
DEFAULT_USER_ID = os.getenv("DEFAULT_USER_ID", "local_user")
APP_NAME = "Lumen_SRE"


@dataclass(slots=True)
class SREAgentManager:
    """Coordinates agent execution, fallback handling, and optional reporting."""

    agent: Agent
    app_name: str = APP_NAME

    def _try_direct_tool_response(self, user_query: str) -> str | None:
        normalized = user_query.strip().lower()
        namespace = self._extract_namespace(user_query)

        if "pod usage since start" in normalized or "since start" in normalized:
            return fetch_pod_usage_since_start(namespace=namespace)

        if "cost optimization snapshot" in normalized or "current snapshot" in normalized:
            return fetch_cost_optimization_snapshot(namespace=namespace)

        if "billing vs utilization" in normalized or "effective billing" in normalized:
            return analyze_billing_vs_utilization(namespace=namespace)

        if "infrastructure evaluation" in normalized or "evaluate infrastructure" in normalized or "unused disks" in normalized:
            return evaluate_cluster_infrastructure(namespace=namespace)

        if "historical resource analysis" in normalized or ("historical" in normalized and "analysis" in normalized):
            return fetch_historical_resource_analysis(namespace=namespace, days=self._extract_days(user_query))

        if "all workload statuses" in normalized or "broader operational view" in normalized:
            return fetch_all_workload_statuses(namespace=namespace)

        return None

    @staticmethod
    def _extract_namespace(user_query: str) -> str:
        match = re.search(r"\bnamespace\s+([a-z0-9-]+)", user_query, re.IGNORECASE)
        if match:
            return match.group(1)
        return "default"

    @staticmethod
    def _extract_days(user_query: str) -> int:
        match = re.search(r"\b(7|30|60)\s*[- ]?day\b", user_query, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 60

    async def run_query(self, user_query: str, user_id: str = DEFAULT_USER_ID) -> str:
        session_service = InMemorySessionService()
        runner = Runner(agent=self.agent, app_name=self.app_name, session_service=session_service)

        try:
            session = await session_service.create_session(app_name=self.app_name, user_id=user_id)
            full_response = ""
            async for event in runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=types.Content(role="user", parts=[types.Part(text=user_query)]),
            ):
                if event.content and event.content.parts:
                    full_response += "".join(part.text or "" for part in event.content.parts)

            return full_response.strip()
        finally:
            await runner.close()

    async def handle_query(
        self,
        user_query: str,
        user_id: str = DEFAULT_USER_ID,
        recipient: str | None = None,
        auto_send_report: bool = False,
    ) -> str:
        direct_response = self._try_direct_tool_response(user_query)
        if direct_response is not None:
            if auto_send_report and direct_response:
                create_and_send_report(direct_response, recipient_email=recipient)
            return direct_response

        try:
            response = await self.run_query(user_query=user_query, user_id=user_id)
        except Exception as exc:
            if not self._is_rate_limit_error(exc):
                raise

            print("\n[RESILIENCE] Rate limit hit. Triggering fallback logic...")
            response = self._build_fallback_response()

        if auto_send_report and response:
            create_and_send_report(response, recipient_email=recipient)

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


manager = SREAgentManager(agent=root_agent)


async def run_sre_query(user_query: str, user_id: str = DEFAULT_USER_ID) -> str:
    return await manager.run_query(user_query=user_query, user_id=user_id)


async def main(
    user_query: str = DEFAULT_QUERY,
    user_id: str = DEFAULT_USER_ID,
    recipient: str | None = None,
    auto_send_report: bool = False,
):
    print("--- Running SRE Agent Terminal Mode ---")

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