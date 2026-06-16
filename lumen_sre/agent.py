import asyncio
from dataclasses import dataclass
from google.genai import types

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
)

# --- 3. THE SRE ENGINE (ADK ENTRY POINT) ---

root_agent = Agent(
    name="SRE_Automation_Agent",
    model="gemini-2.5-flash",
    instruction="""
You are a Lead Google Cloud SRE focused on safe, evidence-based cost optimization for Kubernetes workloads.

1. For long-term cost questions, call fetch_historical_resource_analysis first so the answer is based on 30- to 60-day CPU and memory history when available.
2. For live cost questions, call fetch_cost_optimization_snapshot. Treat it as a current snapshot of application workloads, not a long-term savings model.
3. If the user asks for a broader operational view, call fetch_all_workload_statuses to inspect health and current pod usage.
4. Call get_developer_context when runbooks, architecture context, or legacy remediation guidance would help explain recommendations or remediation steps.
5. Never invent pod names, resource requests, limits, or savings percentages.
6. If the tools do not provide real historical metrics, explicitly say that 30- or 60-day cost analysis is unavailable and keep the recommendation scoped to current signals.
7. Only call create_and_send_report when the user explicitly asks for a formal RCA PDF or a stakeholder-ready report.
8. Answer the user's exact query and clearly separate confirmed facts, live snapshot signals, and unavailable historical data.
""",
    tools=[
        fetch_cost_optimization_snapshot,
        fetch_historical_resource_analysis,
        fetch_all_workload_statuses,
        get_developer_context,
        create_and_send_report,
    ]
)

DEFAULT_QUERY = "Show a 30-day historical resource analysis for the default namespace."
DEFAULT_USER_ID = "shyamsuri955"
APP_NAME = "Lumen_SRE"


@dataclass(slots=True)
class SREAgentManager:
    """Coordinates agent execution, fallback handling, and optional reporting."""

    agent: Agent
    app_name: str = APP_NAME

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