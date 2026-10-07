"""Dashboard + health HTTP server (stdlib, background thread)."""

from __future__ import annotations

import base64
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .controller import Controller
from .logging_setup import get_logger

log = get_logger("web")


class WebServer:
    def __init__(self, controller: Controller):
        self.controller = controller
        self.dashboard = Path(__file__).resolve().parent.parent / "v4_dashboard.html"

    def start(self) -> ThreadingHTTPServer:
        controller = self.controller
        dashboard = self.dashboard

        class Handler(BaseHTTPRequestHandler):
            def _json(self, code: int, payload) -> None:
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
                    return user == "trader" and hmac.compare_digest(supplied, password)
                except Exception:
                    return False

            def _require_auth(self) -> bool:
                if self._auth():
                    return True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Gold Copy Trader (user: trader)"')
                self.end_headers()
                return False

            def do_GET(self) -> None:  # noqa: N802
                try:
                    if self.path == "/health":
                        # Always 200 while the process is alive: Railway must not restart
                        # us just because MetaApi or the broker is temporarily down.
                        mon = controller.monitor.state()
                        self._json(200, {
                            "status": "ok",
                            "copyFactoryReady": controller.copyfactory_ready,
                            "copyFactoryActive": bool((controller.last_cf_status or {}).get("active")),
                            "monitorOnline": mon["online"],
                            "inSync": mon["inSync"],
                        })
                        return
                    if not self._require_auth():
                        return
                    if self.path.startswith("/api/state"):
                        self._json(200, controller.state())
                        return
                    if self.path == "/" or self.path.startswith("/?"):
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
                except Exception as exc:
                    log.exception("http_handler_failed", path=self.path, error=str(exc))
                    try:
                        self._json(500, {"error": str(exc)})
                    except Exception:
                        pass

            def log_message(self, *_args) -> None:
                return

        port = int(os.getenv("PORT", "8080"))
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, name="web", daemon=True).start()
        log.info("web_started", port=port, dashboard_auth=bool(controller.settings.dashboard_password))
        return server
