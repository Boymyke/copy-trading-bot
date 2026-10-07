"""Glues the CopyFactory monitor, the mirror monitor and persisted settings.

Execution (open on source open, close on source close) is done entirely by
MetaApi CopyFactory. Nothing in this service sends trades.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from . import __version__
from .config import Settings
from .copyfactory_service import CopyFactoryConfigError, CopyFactoryService
from .logging_setup import get_logger
from .metaapi_rest import MetaApiRestClient
from .mirror_monitor import MirrorMonitor
from .notifier import Notifier
from .storage import Store

log = get_logger("controller")


class Controller:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        notifier: Notifier,
        rest: Optional[MetaApiRestClient] = None,
        copyfactory: Optional[CopyFactoryService] = None,
    ):
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.rest = rest or MetaApiRestClient(
            settings.metaapi_token, settings.metaapi_rest_base_url, settings.rest_timeout_seconds
        )
        self.copyfactory = copyfactory or CopyFactoryService(settings, store, self.rest)
        self.monitor = MirrorMonitor(settings, store, self.rest, notifier, self.copyfactory)
        self.copyfactory_ready = False
        self.copyfactory_error: Optional[str] = None
        self.copyfactory_checked_at: Optional[float] = None
        self.last_cf_status: Dict[str, Any] = {}
        self.started_at = time.time()
        # The pause state lives in MetaApi (subscription.closeOnly); forget any local copy until read.
        self.store.set("copy_paused", "")

    def lot(self) -> float:
        return self.store.get_float("lot_size", self.settings.fixed_lot)

    async def monitor_copyfactory(self) -> None:
        """Read-only CopyFactory monitor. Never creates or overwrites remote config."""
        reported_error: Optional[str] = None
        reported_warnings: tuple[str, ...] = ()
        failures = 0
        while True:
            try:
                status = await self.copyfactory.refresh()
                self.last_cf_status = status
                if not self.copyfactory_ready:
                    self.store.event(
                        "info",
                        "copyfactory",
                        f"CopyFactory configuration loaded: strategy '{status.get('strategyName')}' ({status.get('strategyId')}), "
                        f"subscriber '{status.get('subscriberName')}', lot {status['tradeSize'].get('lot')}, "
                        f"{'ACTIVE' if status['active'] else 'NOT ACTIVE'}",
                        **{k: status.get(k) for k in ("strategyId", "strategyName", "subscriberName", "closeOnly", "active")},
                        trade_size=status.get("tradeSize"),
                        symbol_mapping=status.get("symbolMapping"),
                    )
                    if reported_error:
                        self.notifier.notify("🟢 CopyFactory configuration readable again.", kind="copyfactory")
                self.copyfactory_ready = True
                self.copyfactory_error = None
                reported_error = None
                failures = 0
                warnings = tuple(status.get("warnings") or ())
                if warnings != reported_warnings:
                    reported_warnings = warnings
                    for warning in warnings:
                        self.store.event("warning", "copyfactory-config", warning)
                    if warnings:
                        self.notifier.notify(
                            "⚠️ CopyFactory needs attention (not changed by bot):\n• " + "\n• ".join(warnings),
                            kind="copyfactory-warning",
                        )
                    else:
                        log.info("copyfactory_config_ok", active=status["active"])
                self.copyfactory_checked_at = time.time()
                await asyncio.sleep(self.settings.copyfactory_refresh_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                message = f"{type(exc).__name__}: {exc}"
                self.copyfactory_checked_at = time.time()
                if isinstance(exc, CopyFactoryConfigError) or failures >= 3:
                    # Transient API hiccups (a single timeout) do not flip the state.
                    self.copyfactory_ready = False
                    self.copyfactory_error = message
                log.warning("copyfactory_refresh_failed", error=message, failures=failures)
                if (isinstance(exc, CopyFactoryConfigError) or failures == 3) and message != reported_error:
                    reported_error = message
                    self.store.event("error", "copyfactory", message)
                    self.notifier.notify(f"🔴 CopyFactory check failing:\n{message}\nRetrying automatically.", kind="copyfactory-error")
                await asyncio.sleep(min(120.0, 15.0 * failures))

    async def set_pause(self, paused: bool) -> None:
        if not self.copyfactory_ready:
            raise RuntimeError(
                "CopyFactory configuration is not readable right now, so pause/resume was NOT applied. "
                f"Last error: {self.copyfactory_error}"
            )
        await self.copyfactory.set_paused(paused)
        self.last_cf_status = self.copyfactory.status()

    def state(self) -> Dict[str, Any]:
        cf = self.last_cf_status or {}
        return {
            "version": __version__,
            "uptimeSeconds": round(time.time() - self.started_at),
            "architecture": "MetaApi CopyFactory executes; Railway monitors (Telegram, dashboard, health)",
            "copyFactory": {
                "ready": self.copyfactory_ready,
                "active": bool(cf.get("active")) and self.copyfactory_ready,
                "error": self.copyfactory_error,
                "checkedAt": self.copyfactory_checked_at,
                "status": cf,
                "paused": {"1": True, "0": False}.get(self.store.get("copy_paused") or ""),
                "lot": self.lot(),
                "sourceSymbol": self.settings.copy_symbol,
                "targetSymbol": self.settings.target_symbol,
            },
            "accounts": cf.get("accounts") or self.copyfactory.accounts,
            "monitor": self.monitor.state(),
            "events": self.store.recent_events(40),
        }
