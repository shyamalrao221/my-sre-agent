import asyncio
from google.genai import types

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from .knowledge import get_developer_context
from .tools import (
    build_fallback_report,
    create_and_send_report,
    fetch_all_workload_statuses,
    fetch_broken_pod_logs,
    fetch_project_errors,
    generate_rca_report,
    send_rca_email,
    patch_backend_deployment,  # 👈 Clean tool import registered here
)

# --- 3. THE SRE ENGINE (ADK ENTRY POINT) ---

root_agent = Agent(
    name="SRE_Automation_Agent",
    model="gemini-2.5-flash",
    instruction="""
You are a Lead Google Cloud SRE with a focus on long-term reliability and cost optimization. Your primary goal is to analyze historical infrastructure data over a rolling 60-day (2-month) window.

1. Call fetch_all_workload_statuses to inspect the current GKE fleet and collect both live and historical pod-level CPU and memory usage trends spanning the last two months.
2. Analyze actual pod resource usage over the last two months versus requested and limited resources to identify sustained underutilization, seasonal idleness, or chronic over-provisioning.
3. Identify opportunities to optimize GKE pod configurations based on these 60-day utilization baselines with the goal of achieving at least 20% sustainable cost savings, while maintaining application stability during peak historical traffic.
4. Call fetch_project_errors to inspect failures. If a deployment has availability issues or is failing, identify the failing pod name and call fetch_broken_pod_logs to read its container logs and find the root cause.
5. If a deployment is verified to be failing because of an incorrect database endpoint context or connection string, use the patch_backend_deployment tool to dynamically remediate the target environment variables.
6. Call get_developer_context when runbooks, architecture context, or legacy remediation guidance would help explain long-term optimization recommendations.
7. Only call create_and_send_report when the user explicitly asks for a formal RCA PDF or a stakeholder-ready 2-month retrospective cost optimization or incident report.
8. Answer the user's exact query and cite concrete 60-day trend findings from the tools, including estimated monthly and bi-monthly cost-saving impacts where possible.
9. If tools fail to provide historical metrics stretching back 2 months, explicitly warn the user about data retention limitations instead of generalizing from live data.
""",
    tools=[
        fetch_all_workload_statuses,
        fetch_project_errors,
        get_developer_context,
        create_and_send_report,
        fetch_broken_pod_logs,
        patch_backend_deployment,  # 👈 Registered inside active agent toolbelt array
    ]
)

DEFAULT_QUERY = "Audit project and summarize current SRE risks."


async def run_sre_query(user_query: str, user_id: str = "shyamsuri955") -> str:
    session_service = InMemorySessionService()
    runner = Runner(agent=root_agent, app_name="Lumen_SRE", session_service=session_service)

    try:
        session = await session_service.create_session(app_name="Lumen_SRE", user_id=user_id)
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


async def main(
    user_query: str = DEFAULT_QUERY,
    recipient: str | None = None,
    auto_send_report: bool = False,
):
    print("--- Running SRE Agent Terminal Mode ---")

    try:
        full_response = await run_sre_query(user_query=user_query)
        if full_response:
            print(full_response)
            if auto_send_report:
                generate_rca_report(full_response)
                send_rca_email(recipient or "your-email@lumen.com")

    except Exception as e:
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            print("\n[RESILIENCE] Rate limit hit. Triggering Fallback logic...")
            fallback_txt = build_fallback_report(
                logs=fetch_project_errors(),
                workloads=fetch_all_workload_statuses(),
                context=get_developer_context("incident remediation"),
            )
            print(fallback_txt)
            if auto_send_report:
                generate_rca_report(fallback_txt)
                send_rca_email(recipient or "your-email@lumen.com")
        else:
            print(f"\n[ERROR]: {str(e)}")

if __name__ == "__main__":
    asyncio.run(main())
