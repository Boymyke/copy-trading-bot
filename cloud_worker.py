import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from metaapi_cloud_sdk import MetaApi


STATE: Dict[str, Any] = {
    "status": "starting",
    "sourceConnected": False,
    "brokerConnected": False,
    "positionCount": 0,
    "positions": [],
    "lastUpdate": None,
    "lastError": None,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_position(position: Dict[str, Any]) -> Dict[str, Any]:
    """Return only non-secret position fields for logs/health output."""
    keys = (
        "id",
        "symbol",
        "type",
        "volume",
        "openPrice",
        "currentPrice",
        "stopLoss",
        "takeProfit",
        "profit",
        "magic",
        "comment",
    )
    return {key: position.get(key) for key in keys if key in position}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health"):
            self.send_response(404)
            self.end_headers()
            return

        payload = json.dumps(STATE, default=str).encode("utf-8")
        self.send_response(200 if STATE["status"] != "fatal" else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[health] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


def position_signature(position: Dict[str, Any]) -> tuple:
    return (
        position.get("symbol"),
        position.get("type"),
        position.get("volume"),
        position.get("openPrice"),
        position.get("stopLoss"),
        position.get("takeProfit"),
    )


async def monitor_source() -> None:
    token = os.getenv("METAAPI_TOKEN", "").strip()
    account_id = os.getenv("METAAPI_SOURCE_ACCOUNT_ID", "").strip()

    if not token:
        raise RuntimeError("METAAPI_TOKEN is not configured")
    if not account_id:
        raise RuntimeError("METAAPI_SOURCE_ACCOUNT_ID is not configured")

    api = MetaApi(token)
    retry_seconds = 5

    while True:
        connection = None
        try:
            STATE.update({
                "status": "connecting",
                "sourceConnected": False,
                "brokerConnected": False,
                "lastError": None,
                "lastUpdate": now_iso(),
            })

            print("[source] loading MetaApi account...", flush=True)
            account = await api.metatrader_account_api.get_account(account_id)

            if account.state != "DEPLOYED":
                print(f"[source] account state is {account.state}; deploying...", flush=True)
                await account.deploy()

            print("[source] waiting for broker connection...", flush=True)
            await account.wait_connected()

            connection = account.get_streaming_connection()
            await connection.connect()

            print("[source] synchronizing terminal state...", flush=True)
            await connection.wait_synchronized({"timeoutInSeconds": 600})

            terminal_state = connection.terminal_state
            STATE.update({
                "status": "connected",
                "sourceConnected": bool(terminal_state.connected),
                "brokerConnected": bool(terminal_state.connected_to_broker),
                "lastUpdate": now_iso(),
            })
            print("[source] CONNECTED — cloud source monitor is live", flush=True)

            previous: Dict[str, Dict[str, Any]] = {}

            while True:
                positions = terminal_state.positions or []
                current = {
                    str(position.get("id")): position
                    for position in positions
                    if position.get("id") is not None
                }

                for ticket in current.keys() - previous.keys():
                    print(
                        "[trade] OPEN " + json.dumps(safe_position(current[ticket]), default=str),
                        flush=True,
                    )

                for ticket in previous.keys() - current.keys():
                    print(
                        "[trade] CLOSED " + json.dumps(safe_position(previous[ticket]), default=str),
                        flush=True,
                    )

                for ticket in current.keys() & previous.keys():
                    if position_signature(current[ticket]) != position_signature(previous[ticket]):
                        print(
                            "[trade] UPDATED " + json.dumps(safe_position(current[ticket]), default=str),
                            flush=True,
                        )

                STATE.update({
                    "status": "connected" if terminal_state.connected_to_broker else "broker-disconnected",
                    "sourceConnected": bool(terminal_state.connected),
                    "brokerConnected": bool(terminal_state.connected_to_broker),
                    "positionCount": len(current),
                    "positions": [safe_position(position) for position in current.values()],
                    "lastUpdate": now_iso(),
                    "lastError": None,
                })

                previous = current
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATE.update({
                "status": "reconnecting",
                "sourceConnected": False,
                "brokerConnected": False,
                "lastError": str(exc),
                "lastUpdate": now_iso(),
            })
            print(f"[source] error: {exc}", flush=True)
            print(f"[source] retrying in {retry_seconds}s...", flush=True)
            await asyncio.sleep(retry_seconds)
        finally:
            if connection is not None:
                try:
                    await connection.close()
                except Exception:
                    pass


async def main() -> None:
    threading.Thread(target=start_health_server, daemon=True).start()
    try:
        await monitor_source()
    except Exception as exc:
        STATE.update({
            "status": "fatal",
            "lastError": str(exc),
            "lastUpdate": now_iso(),
        })
        print(f"[fatal] {exc}", flush=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
