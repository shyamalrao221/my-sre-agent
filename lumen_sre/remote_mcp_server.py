import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .knowledge import get_developer_context
from .tools import create_and_send_report, fetch_all_workload_statuses, fetch_project_errors


class RemoteToolHandler(BaseHTTPRequestHandler):
    def _write_json(self, payload: dict, status_code: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route == "/health":
            self._write_json({"status": "ok"})
            return

        if route == "/tools/workloads":
            self._write_json({"result": fetch_all_workload_statuses()})
            return

        if route == "/tools/errors":
            self._write_json({"result": fetch_project_errors()})
            return

        if route == "/tools/context":
            topic = params.get("topic", [""])[0]
            self._write_json({"result": get_developer_context(topic)})
            return

        if route == "/tools/snapshot":
            self._write_json(
                {
                    "workloads": fetch_all_workload_statuses(),
                    "errors": fetch_project_errors(),
                    "context": get_developer_context("incident remediation"),
                }
            )
            return

        self._write_json({"error": "Unknown route."}, status_code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/tools/report":
            self._write_json({"error": "Unknown route."}, status_code=404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        payload = json.loads(raw_body.decode("utf-8"))
        analysis_summary = payload.get("analysis_summary", "")
        recipient_email = payload.get("recipient_email")

        if not analysis_summary:
            self._write_json({"error": "analysis_summary is required."}, status_code=400)
            return

        result = create_and_send_report(analysis_summary, recipient_email=recipient_email)
        self._write_json({"result": result})


def main(host: str = "127.0.0.1", port: int = 8080):
    server = HTTPServer((host, port), RemoteToolHandler)
    print(f"Remote SRE tool server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()