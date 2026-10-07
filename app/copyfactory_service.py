"""CopyFactory adoption and monitoring.

Rules this service follows:

* It **never creates** a strategy or a subscriber. It adopts the strategy that
  already exists for the source account (preferring the configured name,
  ``Gold Source Strategy``) and the subscriber that already exists for the
  target account.
* Its periodic refresh is **read-only**. It validates the remote configuration
  and reports mismatches as warnings; it never "corrects" them.
* Writes happen only when the operator explicitly asks (``/pause``, ``/resume``),
  and they are read-modify-write: the current remote object is fetched, exactly
  one field (``closeOnly``) is changed, and everything else is sent back as-is.
* It never deploys/undeploys accounts and opens no websocket connections
  (CopyFactory configuration and MetaApi provisioning are plain REST).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Optional

from metaapi_cloud_copyfactory_sdk import CopyFactory

from .config import Settings
from .logging_setup import get_logger
from .metaapi_rest import MetaApiRestClient, MetaApiRestError
from .storage import Store

log = get_logger("copyfactory")

PAUSED_CLOSE_ONLY = {"by-position", "by-symbol", "immediately"}


class CopyFactoryConfigError(RuntimeError):
    """The remote CopyFactory configuration is missing something we must not create ourselves."""


def _sid(obj: dict) -> str:
    return str(obj.get("_id") or obj.get("id") or "")


class CopyFactoryService:
    def __init__(self, settings: Settings, store: Store, rest: MetaApiRestClient, copyfactory: Optional[Any] = None):
        self.settings = settings
        self.store = store
        self.rest = rest
        self.copyfactory = copyfactory or CopyFactory(token=settings.metaapi_token)
        self.configuration = self.copyfactory.configuration_api
        self.strategy_id: Optional[str] = store.get("copyfactory_strategy_id")
        self.strategy: Optional[dict] = None
        self.subscriber: Optional[dict] = None
        self.subscription: Optional[dict] = None
        self.accounts: dict[str, dict] = {}
        self.warnings: list[str] = []
        self.stopouts: list[dict] = []
        self._config_fingerprint: Optional[str] = None

    # -- accounts ---------------------------------------------------------------------

    async def inspect_accounts(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for role, account_id in (("source", self.settings.source_account_id), ("target", self.settings.target_account_id)):
            previous = self.accounts.get(role, {})
            try:
                acc = await self.rest.get_account(account_id)
                info = {
                    "id": account_id,
                    "name": acc.get("name"),
                    "state": acc.get("state"),
                    "connectionStatus": acc.get("connectionStatus"),
                    "region": acc.get("region"),
                    "reliability": acc.get("reliability"),
                    "copyFactoryRoles": list(acc.get("copyFactoryRoles") or []),
                    "connected": acc.get("state") == "DEPLOYED" and acc.get("connectionStatus") == "CONNECTED",
                    "error": None,
                    "readFailures": 0,
                }
                self.rest.set_account_region(account_id, acc.get("region"))
            except MetaApiRestError as exc:
                failures = int(previous.get("readFailures") or 0) + 1
                if previous and not previous.get("error") and failures < 3:
                    # A single slow provisioning read is not a disconnect: keep last known state.
                    info = {**previous, "readFailures": failures}
                    log.warning("account_read_failed", role=role, error=str(exc), failures=failures)
                else:
                    info = {"id": account_id, "connected": False, "error": str(exc), "readFailures": failures}
            if (previous.get("state"), previous.get("connectionStatus"), previous.get("error")) != (
                info.get("state"),
                info.get("connectionStatus"),
                info.get("error"),
            ):
                level = "info" if info["connected"] else "warning"
                self.store.event(
                    level,
                    f"{role}-account",
                    f"{role.title()} account {info.get('name') or account_id}: state={info.get('state')} "
                    f"connection={info.get('connectionStatus')} region={info.get('region')}"
                    + (f" reliability={info.get('reliability')}" if info.get("reliability") else "")
                    + (f" error={info['error']}" if info.get("error") else ""),
                    account_id=account_id,
                    roles=info.get("copyFactoryRoles"),
                )
            result[role] = info
        self.accounts = result
        return result

    async def get_user_log(self, since) -> list:
        return list(
            await self.copyfactory.trading_api.get_user_log(
                self.settings.target_account_id, start_time=since, limit=200
            )
            or []
        )

    async def recent_copy_history(self, days: int = 7) -> dict:
        """Read-only proof of copying: CopyFactory's own deal history for this subscriber."""
        from datetime import datetime, timedelta, timezone

        till = datetime.now(timezone.utc)
        since = till - timedelta(days=days)
        txs = await self.copyfactory.history_api.get_subscription_transactions(
            since, till, subscriber_ids=[self.settings.target_account_id], limit=1000
        )
        rows = [
            {
                "time": str(t.get("time")),
                "type": t.get("type"),
                "symbol": t.get("symbol"),
                "sourcePositionId": t.get("positionId"),
                "targetPositionId": t.get("slavePositionId"),
                "quantity": t.get("quantity"),
                "profit": t.get("profit"),
                "strategy": (t.get("strategy") or {}).get("name"),
            }
            for t in (txs or [])
        ]
        result = {"days": days, "count": len(rows), "latest": rows[-20:] if rows else []}

        # What the source actually traded in the same window (one trading-API call).
        try:
            fmt = "%Y-%m-%dT%H:%M:%S.000Z"
            deals = await self.rest.get_deals_by_time(self.settings.source_account_id, since.strftime(fmt), till.strftime(fmt))
            gold = [d for d in deals if d.get("symbol") == self.settings.copy_symbol]
            result["sourceDeals"] = len(gold)
            result["sourceSymbolsTraded"] = sorted({str(d.get("symbol")) for d in deals if d.get("symbol")})
            result["sourceLatest"] = [
                {k: d.get(k) for k in ("time", "type", "entryType", "symbol", "volume", "positionId", "price")}
                for d in gold[-10:]
            ]
        except Exception as exc:
            result["sourceDealsError"] = f"{type(exc).__name__}: {exc}"

        # Signals the strategy itself received/logged (CopyFactory provider side).
        try:
            logs = await self.copyfactory.trading_api.get_strategy_log(
                self.strategy_id, start_time=since, limit=50
            ) if self.strategy_id else []
            result["strategyLog"] = [
                {k: r.get(k) for k in ("time", "level", "message", "symbol", "positionId")} for r in (logs or [])[:20]
            ]
        except Exception as exc:
            result["strategyLogError"] = f"{type(exc).__name__}: {exc}"
        return result

    async def get_stopouts(self) -> list:
        return [dict(s) for s in (await self.copyfactory.trading_api.get_stopouts(self.settings.target_account_id) or [])]

    # -- adoption ---------------------------------------------------------------------

    async def _adopt_strategy(self) -> dict:
        source_id = self.settings.source_account_id
        if self.strategy_id:
            try:
                strategy = dict(await self.configuration.get_strategy(self.strategy_id))
                if str(strategy.get("accountId")) == source_id and not strategy.get("removed"):
                    return strategy
                log.warning("stored_strategy_mismatch", strategy_id=self.strategy_id, account_id=strategy.get("accountId"))
            except Exception as exc:
                log.warning("stored_strategy_unavailable", strategy_id=self.strategy_id, error=str(exc))

        strategies = await self.configuration.get_strategies_with_infinite_scroll_pagination()
        candidates = [dict(s) for s in strategies if str(s.get("accountId")) == source_id and not s.get("removed")]
        named = [s for s in candidates if str(s.get("name")) == self.settings.strategy_name]
        if len(named) == 1:
            chosen = named[0]
        elif len(named) > 1:
            raise CopyFactoryConfigError(
                f"{len(named)} strategies named '{self.settings.strategy_name}' exist for the source account; "
                "refusing to guess. Rename or remove the duplicates in the MetaApi dashboard."
            )
        elif len(candidates) == 1:
            chosen = candidates[0]
            self.warnings.append(
                f"Strategy name is '{chosen.get('name')}', expected '{self.settings.strategy_name}' (adopted anyway: only strategy for source)"
            )
        elif not candidates:
            raise CopyFactoryConfigError(
                "No CopyFactory strategy exists for the source account. This service never creates one; "
                f"create '{self.settings.strategy_name}' in the MetaApi dashboard."
            )
        else:
            raise CopyFactoryConfigError(
                f"Source account has {len(candidates)} strategies and none is named '{self.settings.strategy_name}'. "
                "Set COPYFACTORY_STRATEGY_NAME to the exact strategy name."
            )
        new_id = _sid(chosen)
        if new_id != self.strategy_id:
            self.strategy_id = new_id
            self.store.set("copyfactory_strategy_id", new_id)
            self.store.event("info", "copyfactory", f"Adopted existing CopyFactory strategy '{chosen.get('name')}' ({new_id})")
        return chosen

    async def _read_subscription(self) -> tuple[dict, dict]:
        target_id = self.settings.target_account_id
        try:
            subscriber = dict(await self.configuration.get_subscriber(target_id))
        except Exception as exc:
            raise CopyFactoryConfigError(
                f"Could not read the CopyFactory subscriber for the target account ({exc}). "
                "This service never creates subscribers; check it exists in the MetaApi dashboard."
            ) from exc
        subs = [dict(s) for s in subscriber.get("subscriptions") or []]
        match = next((s for s in subs if str(s.get("strategyId")) == self.strategy_id and not s.get("removed")), None)
        if match is None:
            raise CopyFactoryConfigError(
                f"Target subscriber is not subscribed to strategy {self.strategy_id}. "
                "This service will not add the subscription; add it in the MetaApi dashboard."
            )
        return subscriber, match

    # -- validation ------------------------------------------------------------------

    def effective_trade_size(self) -> dict:
        sub = self.subscription or {}
        strat = self.strategy or {}
        scaling = sub.get("tradeSizeScaling") or strat.get("tradeSizeScaling") or {}
        level = "subscription" if sub.get("tradeSizeScaling") else "strategy"
        multiplier = float(sub.get("multiplier") if sub.get("multiplier") is not None else 1.0)
        lot = None
        if scaling.get("mode") == "fixedVolume" and scaling.get("tradeVolume") is not None:
            lot = round(float(scaling["tradeVolume"]) * multiplier, 8)
        return {"mode": scaling.get("mode"), "tradeVolume": scaling.get("tradeVolume"), "multiplier": multiplier, "lot": lot, "level": level}

    def _validate(self) -> list[str]:
        s = self.settings
        strat = self.strategy or {}
        sub = self.subscription or {}
        warnings: list[str] = []

        def effective(key: str) -> Any:
            return sub[key] if key in sub and sub[key] is not None else strat.get(key)

        mappings = list(strat.get("symbolMapping") or []) + list(sub.get("symbolMapping") or [])
        if s.copy_symbol != s.target_symbol and not any(
            m.get("from") == s.copy_symbol and m.get("to") == s.target_symbol for m in mappings
        ):
            warnings.append(f"No symbol mapping {s.copy_symbol} -> {s.target_symbol} found")
        included = list(((effective("symbolFilter") or {}).get("included")) or [])
        if included and s.copy_symbol not in included:
            warnings.append(f"Symbol filter {included} does not include {s.copy_symbol}")
        if effective("copyStopLoss") is not False:
            warnings.append(f"copyStopLoss is {effective('copyStopLoss')!r} (expected False)")
        if effective("copyTakeProfit") is not False:
            warnings.append(f"copyTakeProfit is {effective('copyTakeProfit')!r} (expected False)")
        if effective("skipPendingOrders") is not True:
            warnings.append(f"skipPendingOrders is {effective('skipPendingOrders')!r} (expected True)")
        if effective("reverse"):
            warnings.append("reverse is enabled (expected off)")
        size = self.effective_trade_size()
        if size["mode"] != "fixedVolume":
            warnings.append(f"Trade size mode is {size['mode']!r} (expected fixedVolume)")
        elif size["lot"] is not None and abs(size["lot"] - s.fixed_lot) > 1e-8:
            warnings.append(f"Effective CopyFactory lot is {size['lot']:g}, DEFAULT_LOT is {s.fixed_lot:g}")

        # Anything that lets CopyFactory skip, alter or close the target trade on its
        # own (instead of mirroring the source lifecycle) is reported here.
        subscriber = self.subscriber or {}
        lifetime = (strat.get("timeSettings") or {}).get("lifetimeInHours")
        if lifetime:
            warnings.append(f"Strategy closes copied trades after lifetimeInHours={lifetime} (target may close before source)")
        for label, obj in (("strategy", strat), ("subscription", sub), ("subscriber", subscriber)):
            if obj.get("riskLimits"):
                warnings.append(f"{label} riskLimits set: {obj.get('riskLimits')} (can stop copying / close trades)")
            if obj.get("maxStopLoss"):
                warnings.append(f"{label} maxStopLoss set: {obj.get('maxStopLoss')} (adds an SL to copied trades)")
            if obj.get("maxTradeRisk") not in (None, 0):
                warnings.append(f"{label} maxTradeRisk={obj.get('maxTradeRisk')} (can reduce/skip trades)")
            if obj.get("newsFilter"):
                warnings.append(f"{label} newsFilter set (can skip trades around news)")
            if obj.get("signalDelay"):
                warnings.append(f"{label} signalDelay set: {obj.get('signalDelay')}")
            sides = obj.get("allowedSides")
            if sides and set(sides) != {"buy", "sell"} and "all" not in sides:
                warnings.append(f"{label} allowedSides={sides} (some trades will not copy)")
        if str(sub.get("closeOnly") or "") in PAUSED_CLOSE_ONLY:
            warnings.append(f"Copying is PAUSED (closeOnly={sub.get('closeOnly')}): new source trades will NOT open on target")
        for stopout in self.stopouts:
            warnings.append(
                f"CopyFactory stop-out: {stopout.get('reason')} — {stopout.get('reasonDescription')} "
                f"(until {stopout.get('stoppedTill')})"
            )
        for role, needed in (("source", "PROVIDER"), ("target", "SUBSCRIBER")):
            acc = self.accounts.get(role) or {}
            if acc.get("copyFactoryRoles") is not None and needed not in (acc.get("copyFactoryRoles") or []) and not acc.get("error"):
                warnings.append(f"{role} account is missing CopyFactory role {needed}")
            if acc and not acc.get("connected"):
                warnings.append(f"{role} account not connected to broker ({acc.get('connectionStatus') or acc.get('error')})")
        return warnings

    # -- public API ------------------------------------------------------------------

    async def refresh(self) -> dict[str, Any]:
        """Read-only refresh of accounts, strategy and subscription."""
        self.warnings = []
        await self.inspect_accounts()
        self.strategy = await self._adopt_strategy()
        self.subscriber, self.subscription = await self._read_subscription()
        try:
            self.stopouts = await self.get_stopouts()
        except Exception as exc:  # informational
            log.warning("stopouts_read_failed", error=f"{type(exc).__name__}: {exc}")
        self._log_config_if_changed()
        self.warnings.extend(self._validate())

        paused = str(self.subscription.get("closeOnly") or "") in PAUSED_CLOSE_ONLY
        previous_paused = self.store.get("copy_paused")
        self.store.set("copy_paused", "1" if paused else "0")
        if previous_paused not in (None, "") and previous_paused !=("1" if paused else "0"):
            self.store.event("info", "copy-control", f"CopyFactory copying is now {'PAUSED' if paused else 'ACTIVE'} (read from MetaApi)")
        size = self.effective_trade_size()
        if size["lot"] is not None:
            self.store.set("lot_size", size["lot"])
        return self.status()

    def _log_config_if_changed(self) -> None:
        """Log the full live CopyFactory config (no secrets in it) whenever it changes."""
        snapshot = {"strategy": self.strategy, "subscriber": self.subscriber, "stopouts": self.stopouts}
        fingerprint = json.dumps(snapshot, sort_keys=True, default=str)
        if fingerprint != self._config_fingerprint:
            self._config_fingerprint = fingerprint
            log.info("copyfactory_live_config", config=json.loads(fingerprint))

    @property
    def active(self) -> bool:
        """Copying is live: subscribed, not paused, no stop-out, both accounts connected."""
        return bool(
            self.subscription
            and str(self.subscription.get("closeOnly") or "") not in PAUSED_CLOSE_ONLY
            and not self.stopouts
            and all((self.accounts.get(r) or {}).get("connected") for r in ("source", "target"))
        )

    def status(self) -> dict[str, Any]:
        strat = self.strategy or {}
        sub = self.subscription or {}
        return {
            "strategyId": self.strategy_id,
            "strategyName": strat.get("name"),
            "subscriberName": (self.subscriber or {}).get("name"),
            "subscribed": bool(self.subscription),
            "closeOnly": sub.get("closeOnly"),
            "paused": str(sub.get("closeOnly") or "") in PAUSED_CLOSE_ONLY,
            "tradeSize": self.effective_trade_size(),
            "symbolMapping": list(strat.get("symbolMapping") or []) + list(sub.get("symbolMapping") or []),
            "symbolFilter": sub.get("symbolFilter") or strat.get("symbolFilter"),
            "copyStopLoss": sub.get("copyStopLoss", strat.get("copyStopLoss")),
            "copyTakeProfit": sub.get("copyTakeProfit", strat.get("copyTakeProfit")),
            "skipPendingOrders": sub.get("skipPendingOrders", strat.get("skipPendingOrders")),
            "reverse": sub.get("reverse", strat.get("reverse")),
            "warnings": list(self.warnings),
            "active": self.active,
            "stopouts": self.stopouts,
            "accounts": self.accounts,
        }

    async def set_paused(self, paused: bool) -> None:
        """Toggle closeOnly on our one subscription; every other field is preserved."""
        await self._adopt_strategy()
        subscriber, _ = await self._read_subscription()
        body = copy.deepcopy(subscriber)
        body.pop("_id", None)
        changed = False
        for sub in body.get("subscriptions") or []:
            if str(sub.get("strategyId")) == self.strategy_id and not sub.get("removed"):
                before = sub.get("closeOnly")
                if paused:
                    sub["closeOnly"] = "by-position"
                else:
                    sub.pop("closeOnly", None)
                changed = before != sub.get("closeOnly")
                log.info("subscriber_update", field="closeOnly", before=before, after=sub.get("closeOnly"))
        if changed:
            await self.configuration.update_subscriber(self.settings.target_account_id, body)
        self.store.set("copy_paused", "1" if paused else "0")
        self.store.event("info", "copy-control", ("Copying paused (closeOnly=by-position)" if paused else "Copying resumed") + ("" if changed else " — already in that state"))
        await self.refresh()
