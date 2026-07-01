import json
import asyncio
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .project_context import set_project_context, get_project_context

from .agent import manager
from .knowledge import get_developer_context
from .tools import (
    create_and_send_report,
    fetch_all_workload_statuses,
    fetch_cost_optimization_snapshot,
    fetch_historical_resource_analysis,
    fetch_pod_usage_since_start,
)


UI_PATH = Path(__file__).resolve().parent / "ui" / "index.html"


class RemoteToolHandler(BaseHTTPRequestHandler):

    def _write_json(self, payload: dict, status_code: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, body: str, status_code: int = 200):
        payload = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"

        if not raw_body:
            return {}

        try:
            return json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc

    def log_message(self, format: str, *args):
        return

    # ✅ GET ENDPOINTS (NO CHANGE)
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route in {"/", "/index.html"}:
            self._write_html(UI_PATH.read_text(encoding="utf-8"))
            return

        if route == "/health":
            self._write_json({"status": "ok"})
            return

        if route == "/tools/workloads":
            self._write_json({"result": fetch_all_workload_statuses()})
            return

        if route == "/tools/cost-analysis":
            namespace = params.get("namespace", ["default"])[0] or "default"
            self._write_json({
                "result": fetch_cost_optimization_snapshot(namespace=namespace)
            })
            return

        if route == "/tools/historical-cost-analysis":
            namespace = params.get("namespace", ["default"])[0] or "default"
            days_param = params.get("days", ["30"])[0] or "30"

            try:
                days = int(days_param)
            except ValueError:
                self._write_json({"error": "days must be an integer."}, status_code=400)
                return

            self._write_json({
                "result": fetch_historical_resource_analysis(namespace=namespace, days=days)
            })
            return

        if route == "/tools/pod-usage-since-start":
            namespace = params.get("namespace", ["default"])[0] or "default"
            self._write_json({
                "result": fetch_pod_usage_since_start(namespace=namespace)
            })
            return

        if route == "/tools/context":
            topic = params.get("topic", [""])[0]
            self._write_json({"result": get_developer_context(topic)})
            return

        if route == "/tools/snapshot":
            self._write_json({
                "workloads": fetch_all_workload_statuses(),
                "context": get_developer_context("cost optimization"),
            })
            return

        self._write_json({"error": "Unknown route."}, status_code=404)

    # ✅ POST ENDPOINTS (UPDATED ✅✅✅)
    def do_POST(self):
        parsed = urlparse(self.path)

        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._write_json({"error": str(exc)}, status_code=400)
            return

        # ✅ 1. SAVE PROJECT CONTEXT (NEW)
        if parsed.path == "/projects":
            set_project_context(payload)
            self._write_json({
                "message": "Project context saved successfully",
                "context": payload
            })
            return

        # ✅ 2. RUN QUERY WITH CONTEXT (UPDATED)
        if parsed.path == "/tools/query":
            query = str(payload.get("query", "")).strip()
            user_id = str(payload.get("user_id", "developer")).strip() or "developer"

            if not query:
                self._write_json({"error": "query is required."}, status_code=400)
                return

            # ✅ GET PROJECT CONTEXT
            context = get_project_context()

            if not context:
                self._write_json({"error": "Project context not set"}, status_code=400)
                return

            # ✅ INJECT CONTEXT INTO QUERY
            query_with_context = f"""
Project Context:
{context}

User Query:
{query}
"""

            result = asyncio.run(
                manager.handle_query(
                    user_query=query_with_context,
                    user_id=user_id
                )
            )

            self._write_json({"result": result})
            return

        # ✅ 3. REPORT GENERATION (optional existing feature)
        if parsed.path == "/tools/report":
            analysis_summary = payload.get("analysis_summary", "")
            recipient_email = payload.get("recipient_email")

            if not analysis_summary:
                self._write_json({"error": "analysis_summary is required."}, status_code=400)
                return

            result = create_and_send_report(analysis_summary, recipient_email=recipient_email)
            self._write_json({"result": result})
            return

        # ✅ UNKNOWN ROUTE
        self._write_json({"error": "Unknown route."}, status_code=404)


# ✅ SERVER START
def main(host: str = "127.0.0.1", port: int = 8080, open_browser_on_start: bool = True):
    server = HTTPServer((host, port), RemoteToolHandler)
    app_url = f"http://{host}:{port}"

    print(f"Remote CloudOptix server running at {app_url}")

    if open_browser_on_start:
        threading.Timer(0.4, lambda: webbrowser.open(app_url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()