"""Glues CopyFactory monitoring, the dry-run risk manager and persisted settings."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from . import __version__
from .config import Settings
from .copyfactory_service import CopyFactoryConfigError, CopyFactoryService
from .logging_setup import get_logger
from .metaapi_rest import MetaApiRestClient
from .notifier import Notifier
from .risk_manager import RiskManager
from .storage import Store

log = get_logger("controller")

RISK_KEYS = {"initial_sl_usd", "trail_trigger_usd", "trail_gap_usd", "trail_step_usd"}


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
        self.risk = RiskManager(settings, store, self.rest, notifier, expected_lot=self.lot)
        self.copyfactory_ready = False
        self.copyfactory_error: Optional[str] = None
        self.copyfactory_checked_at: Optional[float] = None
        self.last_cf_status: Dict[str, Any] = {}
        self.started_at = time.time()

        # The pause state lives in MetaApi (subscription.closeOnly). Older builds stored a
        # local default here that was never applied remotely, so forget it until we read it.
        self.store.set("copy_paused", "")

        # Persisted risk defaults: env values seed /data once, then Telegram edits win.
        if self.store.get("lot_size") is None:
            self.store.set("lot_size", settings.fixed_lot)
        for key in RISK_KEYS:
            if self.store.get(key) is None:
                self.store.set(key, getattr(settings, key))

    def lot(self) -> float:
        return self.store.get_float("lot_size", self.settings.fixed_lot)

    async def monitor_copyfactory(self) -> None:
        """Read-only CopyFactory monitor. Never creates or overwrites remote config."""
        reported_error: Optional[str] = None
        reported_warnings: tuple[str, ...] = ()
        while True:
            try:
                status = await self.copyfactory.refresh()
                self.last_cf_status = status
                if not self.copyfactory_ready:
                    self.store.event(
                        "info",
                        "copyfactory",
                        f"CopyFactory configuration loaded: strategy '{status.get('strategyName')}' ({status.get('strategyId')}), "
                        f"lot {status['tradeSize'].get('lot')}, {'PAUSED' if status['paused'] else 'ACTIVE'}",
                        **{k: status.get(k) for k in ("strategyId", "strategyName", "subscriberName", "closeOnly")},
                        trade_size=status.get("tradeSize"),
                        symbol_mapping=status.get("symbolMapping"),
                    )
                    if reported_error:
                        self.notifier.notify("🟢 CopyFactory configuration readable again.", kind="copyfactory")
                self.copyfactory_ready = True
                self.copyfactory_error = None
                reported_error = None
                warnings = tuple(status.get("warnings") or ())
                if warnings != reported_warnings:
                    reported_warnings = warnings
                    for warning in warnings:
                        self.store.event("warning", "copyfactory-config", warning)
                    if warnings:
                        self.notifier.notify(
                            "⚠️ CopyFactory configuration differs from expected (not changed by bot):\n• " + "\n• ".join(warnings),
                            kind="copyfactory-warning",
                        )
                self.copyfactory_checked_at = time.time()
                await asyncio.sleep(self.settings.copyfactory_refresh_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.copyfactory_ready = False
                self.copyfactory_error = str(exc)
                self.copyfactory_checked_at = time.time()
                if isinstance(exc, CopyFactoryConfigError) or str(exc) == reported_error:
                    log.error("copyfactory_refresh_failed", error=str(exc), error_type=type(exc).__name__)
                else:  # full traceback only the first time a new error appears
                    log.exception("copyfactory_refresh_failed", error=str(exc))
                if str(exc) != reported_error:
                    reported_error = str(exc)
                    self.store.event("error", "copyfactory", str(exc))
                    self.notifier.notify(f"🔴 CopyFactory check failed:\n{exc}", kind="copyfactory-error")
                await asyncio.sleep(15)

    async def set_pause(self, paused: bool) -> None:
        if not self.copyfactory_ready:
            raise RuntimeError(
                "CopyFactory configuration is not readable right now, so pause/resume was NOT applied. "
                f"Last error: {self.copyfactory_error}"
            )
        await self.copyfactory.set_paused(paused)
        self.last_cf_status = self.copyfactory.status()

    async def set_lot(self, lot: float) -> str:
        if not self.copyfactory_ready:
            raise RuntimeError("CopyFactory configuration is not readable right now, so the lot was NOT changed.")
        where = await self.copyfactory.set_lot(lot)
        self.last_cf_status = self.copyfactory.status()
        return where

    def set_risk(self, key: str, value: float) -> None:
        if key not in RISK_KEYS:
            raise ValueError("unknown risk setting")
        if value < 0 or value > 100000:
            raise ValueError("value must be between 0 and 100000")
        self.store.set(key, value)
        self.store.event("info", "risk-setting", f"{key}={value}")

    def state(self) -> Dict[str, Any]:
        cf = self.last_cf_status or {}
        accounts = cf.get("accounts") or self.copyfactory.accounts
        return {
            "version": __version__,
            "dryRun": self.settings.dry_run,
            "uptimeSeconds": round(time.time() - self.started_at),
            "architecture": "MetaApi CopyFactory native replication + Railway REST dry-run risk monitor",
            "copyFactory": {
                "ready": self.copyfactory_ready,
                "error": self.copyfactory_error,
                "checkedAt": self.copyfactory_checked_at,
                "status": cf,
                # None until read from MetaApi at least once; afterwards the last read value.
                "paused": {"1": True, "0": False}.get(self.store.get("copy_paused") or ""),
                "pausedIsLive": self.copyfactory_ready,
                "lot": self.lot(),
                "sourceSymbol": self.settings.copy_symbol,
                "targetSymbol": self.settings.target_symbol,
            },
            "accounts": accounts,
            "risk": self.risk.state(),
            "events": self.store.recent_events(40),
        }
