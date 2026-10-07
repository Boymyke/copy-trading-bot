import asyncio
import json
import urllib.parse
import urllib.request

from .controller import Controller
from .storage import Store
from .config import Settings


class TelegramBot:
    def __init__(self, settings: Settings, store: Store, controller: Controller):
        self.settings = settings
        self.store = store
        self.controller = controller
        self.offset = 0

    def _request_sync(self, method: str, data=None):
        if not self.settings.telegram_bot_token:
            return {"ok": False, "description": "TELEGRAM_BOT_TOKEN missing"}
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"
        payload = urllib.parse.urlencode(data or {}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=40) as response:
            return json.loads(response.read().decode("utf-8"))

    async def _request(self, method: str, data=None):
        return await asyncio.to_thread(self._request_sync, method, data)

    async def send(self, text: str):
        chat_id = self.store.get("telegram_chat_id")
        if not chat_id:
            return
        try:
            await self._request("sendMessage", {"chat_id": chat_id, "text": text})
        except Exception as exc:
            self.store.event("error", "telegram", str(exc))

    def _status(self) -> str:
        s = self.controller.state()
        cf = s["copyFactory"]
        risk = s["risk"]
        if cf["ready"]:
            copy_line = "⏸ PAUSED" if cf["paused"] else "⚡ ACTIVE — CopyFactory native"
            cf_line = "🟢 READY"
        else:
            copy_line = "⏸ PAUSED/NOT READY" if cf["paused"] else "🟠 ARMED — setup incomplete"
            cf_line = "🔴 NOT READY"
        return (
            "🤖 Gold Copy Trader v4\n\n"
            f"Native CopyFactory: {cf_line}\n"
            f"Copying: {copy_line}\n"
            f"Fixed lot: {cf['lot']:.2f}\n"
            f"Symbol: {cf['sourceSymbol']} → {cf['targetSymbol']}\n"
            f"Risk manager: {'🟢 connected' if risk['connected'] else '🔴 reconnecting'}\n"
            f"Initial SL: ${risk['initialSlUsd']:.2f} per 0.01\n"
            f"Trail trigger: ${risk['trailTriggerUsd']:.2f} per 0.01\n"
            f"Trail gap: ${risk['trailGapUsd']:.2f} per 0.01\n"
            f"Trail step: ${risk['trailStepUsd']:.2f} per 0.01\n"
            f"Managed open trades: {len(risk['managedPositions'])}"
            + (f"\n\nSetup error: {cf['error']}" if cf['error'] else "")
        )

    @staticmethod
    def _help() -> str:
        return (
            "Gold Copy Trader v4 commands\n\n"
            "/status — native copier and risk status\n"
            "/lot 0.01 — fixed target lot for NEW CopyFactory trades\n"
            "/pause — stop NEW copied entries, continue close management\n"
            "/resume — allow new native CopyFactory entries\n"
            "/setsl 0.60 — initial target SL dollars per 0.01\n"
            "/settrigger 0.50 — trailing starts at this profit per 0.01\n"
            "/setgap 0.20 — trailing gap dollars per 0.01\n"
            "/setstep 0.10 — minimum locked-profit improvement per 0.01\n"
            "/positions — currently managed target trades\n"
            "/help — show commands"
        )

    async def _handle(self, message: dict):
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = str(message.get("text") or "").strip()
        if not chat_id or not text:
            return

        paired = self.store.get("telegram_chat_id")
        if not paired:
            parts = text.split(maxsplit=1)
            if len(parts) == 2 and parts[0].lower() == "/pair":
                expected = __import__("os").getenv("TELEGRAM_PAIRING_CODE", "").strip() or self.settings.dashboard_password
                if expected and parts[1].strip() == expected:
                    self.store.set("telegram_chat_id", chat_id)
                    await self._request("sendMessage", {"chat_id": chat_id, "text": "✅ Telegram paired to v4.\n\n" + self._help()})
                else:
                    await self._request("sendMessage", {"chat_id": chat_id, "text": "❌ Invalid pairing code."})
            return
        if chat_id != paired:
            return

        cmd, *rest = text.split(maxsplit=1)
        cmd = cmd.split("@", 1)[0].lower()
        arg = rest[0].strip() if rest else ""
        try:
            if cmd in ("/start", "/help"):
                reply = self._help()
            elif cmd == "/status":
                reply = self._status()
            elif cmd == "/pause":
                await self.controller.set_pause(True)
                reply = "⏸ Native CopyFactory entries paused. Existing copied positions remain eligible for source-close management."
            elif cmd == "/resume":
                if not self.controller.copyfactory_ready:
                    reply = "⚠️ CopyFactory is not ready yet. I saved the requested state, but do not consider copying live until /status says Native CopyFactory: READY."
                    await self.controller.set_pause(False)
                else:
                    await self.controller.set_pause(False)
                    reply = "▶️ Native CopyFactory copying resumed. New eligible Gold trades can now copy directly through MetaApi."
            elif cmd == "/lot":
                value = float(arg)
                await self.controller.set_lot(value)
                reply = f"✅ Fixed CopyFactory target lot set to {value:g}. New copied trades will use this value."
            elif cmd in ("/setsl", "/settrigger", "/setgap", "/setstep"):
                value = float(arg)
                key = {
                    "/setsl": "initial_sl_usd",
                    "/settrigger": "trail_trigger_usd",
                    "/setgap": "trail_gap_usd",
                    "/setstep": "trail_step_usd",
                }[cmd]
                self.controller.set_risk(key, value)
                label = {
                    "/setsl": "Initial SL",
                    "/settrigger": "Trail trigger",
                    "/setgap": "Trailing gap",
                    "/setstep": "Trailing step",
                }[cmd]
                reply = f"✅ {label} set to ${value:g} per 0.01 lot."
            elif cmd == "/positions":
                rows = self.controller.risk.state()["managedPositions"]
                if not rows:
                    reply = "No managed target positions are currently open."
                else:
                    lines = ["📈 Managed target positions"]
                    for r in rows[:20]:
                        lines.append(
                            f"\n{r['side']} {r['symbol']} {float(r['volume']):g} lot\n"
                            f"Position #{r['position_id']}\nSL: {float(r['current_sl'] or 0):.2f} | P/L: ${float(r['last_profit'] or 0):.2f}"
                        )
                    reply = "\n".join(lines)
            else:
                reply = "Unknown command.\n\n" + self._help()
        except Exception as exc:
            reply = f"❌ {exc}"
        await self._request("sendMessage", {"chat_id": chat_id, "text": reply})

    async def run(self):
        if not self.settings.telegram_bot_token:
            return
        while True:
            try:
                response = await self._request(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": self.settings.telegram_poll_timeout,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                if not response.get("ok"):
                    raise RuntimeError(response.get("description") or "Telegram API error")
                for update in response.get("result", []):
                    self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                    if update.get("message"):
                        await self._handle(update["message"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.event("error", "telegram", str(exc))
                await asyncio.sleep(5)
