import asyncio
from typing import Any, Dict, Optional

from .config import Settings
from .copyfactory_service import CopyFactoryService
from .risk_manager import RiskManager
from .storage import Store


class Controller:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.copyfactory = CopyFactoryService(settings, store)
        self.risk = RiskManager(settings, store)
        self.copyfactory_ready = False
        self.copyfactory_error: Optional[str] = None
        self.last_cf_status: Dict[str, Any] = {}

        # Initialize explicit safe defaults only once.
        if self.store.get("copy_paused") is None:
            self.store.set("copy_paused", "1")
        if self.store.get("lot_size") is None:
            self.store.set("lot_size", settings.fixed_lot)
        defaults = {
            "initial_sl_usd": settings.initial_sl_usd,
            "trail_trigger_usd": settings.trail_trigger_usd,
            "trail_gap_usd": settings.trail_gap_usd,
            "trail_step_usd": settings.trail_step_usd,
        }
        for key, value in defaults.items():
            if self.store.get(key) is None:
                self.store.set(key, value)

    async def bootstrap_copyfactory(self):
        """Continuously ensure the native copy path is configured.

        This loop is configuration/monitoring only. A failure here does not sit in
        the source->target execution path once CopyFactory has been configured.
        """
        while True:
            try:
                self.last_cf_status = await self.copyfactory.ensure_ready()
                self.copyfactory_ready = True
                self.copyfactory_error = None
                self.store.event("info", "copyfactory", "Native CopyFactory path is ready")
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.copyfactory_ready = False
                self.copyfactory_error = str(exc)
                self.store.event("error", "copyfactory-bootstrap", str(exc))
                await asyncio.sleep(15)

    async def set_pause(self, paused: bool):
        if not self.copyfactory_ready:
            # Persist the requested state. It will be applied on the first successful
            # CopyFactory bootstrap instead of pretending the remote config changed.
            self.store.set("copy_paused", "1" if paused else "0")
            return
        await self.copyfactory.set_paused(paused)
        self.last_cf_status = await self.copyfactory.status()

    async def set_lot(self, lot: float):
        if lot <= 0 or lot > 100:
            raise ValueError("lot must be greater than 0 and at most 100")
        self.store.set("lot_size", lot)
        if self.copyfactory_ready:
            await self.copyfactory.set_lot(lot)
            self.last_cf_status = await self.copyfactory.status()

    def set_risk(self, key: str, value: float):
        if value < 0 or value > 100000:
            raise ValueError("value must be between 0 and 100000")
        if key not in {"initial_sl_usd", "trail_trigger_usd", "trail_gap_usd", "trail_step_usd"}:
            raise ValueError("unknown risk setting")
        self.store.set(key, value)
        self.store.event("info", "risk-setting", f"{key}={value}")

    def state(self) -> Dict[str, Any]:
        return {
            "version": "4.0.0",
            "architecture": "MetaApi CopyFactory native replication + Railway control/risk layer",
            "copyFactory": {
                "ready": self.copyfactory_ready,
                "error": self.copyfactory_error,
                "status": self.last_cf_status,
                "paused": self.store.get("copy_paused", "1") == "1",
                "lot": float(self.store.get("lot_size", str(self.settings.fixed_lot))),
                "sourceSymbol": self.settings.copy_symbol,
                "targetSymbol": self.settings.target_symbol,
            },
            "risk": self.risk.state(),
            "events": self.store.recent_events(30),
        }
