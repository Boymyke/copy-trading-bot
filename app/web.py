import base64
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import Controller


class WebServer:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.dashboard = Path(__file__).resolve().parent.parent / "v4_dashboard.html"

    def start(self):
        controller = self.controller
        dashboard = self.dashboard

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code: int, payload):
                body = json.dumps(payload, default=str).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self) -> bool:
                password = controller.settings.dashboard_password
                if not password:
                    return False
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Basic "):
                    return False
                try:
                    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                    user, supplied = decoded.split(":", 1)
                    return user == "trader" and supplied == password
                except Exception:
                    return False

            def _require_auth(self) -> bool:
                if self._auth():
                    return True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Gold Copy Trader v4"')
                self.end_headers()
                return False

            def do_GET(self):
                state = controller.state()
                if self.path == "/health":
                    # Railway should keep the controller alive even while CopyFactory is
                    # waiting for paid-role setup or a third-party API recovers.
                    self._json(200, {
                        "status": "ok",
                        "version": state["version"],
                        "copyFactoryReady": state["copyFactory"]["ready"],
                        "copyPaused": state["copyFactory"]["paused"],
                        "riskConnected": state["risk"]["connected"],
                    })
                    return
                if not self._require_auth():
                    return
                if self.path.startswith("/api/state"):
                    self._json(200, state)
                    return
                if self.path == "/" or self.path.startswith("/?"):
                    if not dashboard.exists():
                        self.send_response(404)
                        self.end_headers()
                        return
                    body = dashboard.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *_args):
                return

        port = int(os.getenv("PORT", "8080"))
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
