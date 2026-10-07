from typing import Any, Dict, Optional

from metaapi_cloud_sdk import CopyFactory, MetaApi

from .config import Settings
from .storage import Store


class CopyFactoryService:
    """Owns CopyFactory configuration.

    Trade replication itself runs inside MetaApi CopyFactory. Railway only writes
    configuration and observes status; it is not in the source->target execution path.
    """

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.metaapi = MetaApi(token=settings.metaapi_token)
        self.copyfactory = CopyFactory(token=settings.metaapi_token)
        self.configuration = self.copyfactory.configuration_api
        self.trading = self.copyfactory.trading_api
        self.source_account = None
        self.target_account = None
        self.strategy_id: Optional[str] = store.get("copyfactory_strategy_id")

    async def inspect_accounts(self) -> Dict[str, Any]:
        self.source_account = await self.metaapi.metatrader_account_api.get_account(self.settings.source_account_id)
        self.target_account = await self.metaapi.metatrader_account_api.get_account(self.settings.target_account_id)
        source_roles = list(self.source_account.copy_factory_roles or [])
        target_roles = list(self.target_account.copy_factory_roles or [])
        return {
            "sourceState": self.source_account.state,
            "targetState": self.target_account.state,
            "sourceConnection": self.source_account.connection_status,
            "targetConnection": self.target_account.connection_status,
            "sourceRoles": source_roles,
            "targetRoles": target_roles,
            "sourceReady": "PROVIDER" in source_roles,
            "targetReady": "SUBSCRIBER" in target_roles,
        }

    async def ensure_ready(self) -> Dict[str, Any]:
        status = await self.inspect_accounts()
        if not status["sourceReady"] or not status["targetReady"]:
            missing = []
            if not status["sourceReady"]:
                missing.append("Gold Source must have CopyFactory PROVIDER role")
            if not status["targetReady"]:
                missing.append("Gold Target must have CopyFactory SUBSCRIBER role")
            raise RuntimeError("; ".join(missing))

        if self.source_account.state != "DEPLOYED":
            await self.source_account.deploy()
        if self.target_account.state != "DEPLOYED":
            await self.target_account.deploy()
        await self.source_account.wait_connected()
        await self.target_account.wait_connected()

        await self._ensure_strategy()
        await self.apply_subscription()
        return await self.status()

    async def _strategy_exists(self, strategy_id: str) -> bool:
        try:
            await self.configuration.get_strategy(strategy_id)
            return True
        except Exception:
            return False

    async def _find_existing_strategy(self) -> Optional[str]:
        """Adopt the strategy already created in the MetaApi dashboard.

        This prevents the controller from generating a second strategy after the
        user has configured the provider manually.
        """
        strategies = await self.configuration.get_strategies_with_infinite_scroll_pagination()
        matches = [s for s in strategies if str(s.get("accountId")) == self.settings.source_account_id]
        if not matches:
            return None
        named = next((s for s in matches if str(s.get("name")) == self.settings.strategy_name), None)
        chosen = named or matches[0]
        return str(chosen.get("_id") or chosen.get("id") or "") or None

    async def _ensure_strategy(self) -> None:
        if self.strategy_id and not await self._strategy_exists(self.strategy_id):
            self.strategy_id = None

        if not self.strategy_id:
            self.strategy_id = await self._find_existing_strategy()
            if self.strategy_id:
                self.store.set("copyfactory_strategy_id", self.strategy_id)
                self.store.event("info", "copyfactory", f"Adopted existing CopyFactory strategy {self.strategy_id}")

        if not self.strategy_id:
            generated = await self.configuration.generate_strategy_id()
            self.strategy_id = str(generated["id"])
            self.store.set("copyfactory_strategy_id", self.strategy_id)

        strategy: Dict[str, Any] = {
            "name": self.settings.strategy_name,
            "description": "Copies Gold trades from the TM Financials source account to the target account.",
            "accountId": self.settings.source_account_id,
            "skipPendingOrders": True,
            "symbolFilter": {"included": [self.settings.copy_symbol]},
            "copyStopLoss": False,
            "copyTakeProfit": False,
            "reverse": False,
            "tradeSizeScaling": {
                "mode": "fixedVolume",
                "forceTinyTrades": False,
                "tradeVolume": float(self.store.get("lot_size", str(self.settings.fixed_lot))),
            },
            "timeSettings": {
                "lifetimeInHours": 1,
                "openingIntervalInMinutes": 1,
            },
        }
        if self.settings.copy_symbol != self.settings.target_symbol:
            strategy["symbolMapping"] = [
                {"from": self.settings.copy_symbol, "to": self.settings.target_symbol}
            ]

        await self.configuration.update_strategy(id=self.strategy_id, strategy=strategy)

    def _subscription(self) -> Dict[str, Any]:
        if not self.strategy_id:
            raise RuntimeError("CopyFactory strategy is not configured")
        paused = self.store.get("copy_paused", "1") == "1"
        lot = float(self.store.get("lot_size", str(self.settings.fixed_lot)))
        sub: Dict[str, Any] = {
            "strategyId": self.strategy_id,
            "multiplier": 1,
            "skipPendingOrders": True,
            "tradeSizeScaling": {
                "mode": "fixedVolume",
                "forceTinyTrades": False,
                "tradeVolume": lot,
            },
            "copyStopLoss": False,
            "copyTakeProfit": False,
            "reverse": False,
        }
        # Keep mapping at strategy level. Do not duplicate it in the subscriber chain.
        if paused:
            # Existing positions continue to receive close signals, but new positions
            # are not opened while the user has paused copying.
            sub["closeOnly"] = "by-position"
        return sub

    async def apply_subscription(self) -> None:
        await self.configuration.update_subscriber(
            self.settings.target_account_id,
            {
                "name": "Gold Target Subscriber",
                "copyStopLoss": False,
                "copyTakeProfit": False,
                "subscriptions": [self._subscription()],
            },
        )
        self.store.event("info", "copyfactory", "CopyFactory subscription configuration applied")

    async def set_paused(self, paused: bool) -> None:
        self.store.set("copy_paused", "1" if paused else "0")
        await self.apply_subscription()
        self.store.event("info", "copy-control", "Copying paused" if paused else "Copying resumed")

    async def set_lot(self, lot: float) -> None:
        if lot <= 0 or lot > 100:
            raise ValueError("lot must be greater than 0 and at most 100")
        self.store.set("lot_size", lot)
        await self._ensure_strategy()
        await self.apply_subscription()
        self.store.event("info", "lot", f"Fixed CopyFactory lot changed to {lot:g}")

    async def status(self) -> Dict[str, Any]:
        account_status = await self.inspect_accounts()
        return {
            **account_status,
            "strategyId": self.strategy_id,
            "paused": self.store.get("copy_paused", "1") == "1",
            "lot": float(self.store.get("lot_size", str(self.settings.fixed_lot))),
            "symbol": self.settings.copy_symbol,
            "targetSymbol": self.settings.target_symbol,
            "copyStopLoss": False,
            "copyTakeProfit": False,
        }

    async def get_signals(self):
        if not self.strategy_id:
            return []
        client = await self.trading.get_subscriber_signal_client(self.settings.target_account_id)
        return await client.get_trading_signals()
