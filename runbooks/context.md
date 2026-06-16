    # Lumen SRE Runbook Context

    ## Incident Response
    1. Check GKE workload health first.
    2. Review recent Cloud Logging errors for the impacted resource.
    3. Compare failures with the last known deployment or configuration change.
    4. Document blast radius, likely root cause, and immediate remediation steps.

    ## Reporting Guidance
    - Use a formal RCA report when the request is stakeholder-facing.
    - Include current symptoms, suspected root cause, impacted services, and next actions.
    - Email distribution should only happen after the user requests a stakeholder report.

    ## Query Patterns
    - "Audit the current project state"
    - "Summarize recent production errors"
    - "What runbook steps apply to a failing GKE service?"

    ## Architecture Context
    - Cloud Logging provides recent failure signals.
    - GKE cluster inventory provides workload health context.
    - The SRE ADK agent combines live signals with this runbook context.
    - The remote tool server exposes these capabilities for external callers.