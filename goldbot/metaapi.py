"""Small MetaApi REST client: market data, positions and orders for one account.

Hosts and paths are the ones in MetaApi's REST documentation
(https://metaapi.cloud/docs/client/). Every request carries the ``auth-token``
header; the token is never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

log = logging.getLogger("goldbot.metaapi")

PROVISIONING = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"
OK_CODES = {0, 10008, 10009, 10010, 10025}


class ApiError(RuntimeError):
    def __init__(self, what: str, status: Optional[int], message: str, retry_after: Optional[float] = None):
        self.what, self.status, self.message, self.retry_after = what, status, message, retry_after
        super().__init__(f"{what}: {'HTTP ' + str(status) if status else 'network'} {message}")


class TradeRejected(RuntimeError):
    def __init__(self, code: Any, text: str):
        self.code, self.text = code, text
        super().__init__(f"order rejected: {code} {text}")


def parse_time(value: str) -> int:
    """ISO-8601 (``2026-11-16T12:05:00.000Z``) -> epoch seconds."""
    return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


class MetaApi:
    def __init__(self, token: str, account_id: str = "", timeout: float = 25.0):
        self.token = token
        self.account_id = account_id
        self.region = "london"
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None
        self.blocked_until = 0.0

    # -- transport ------------------------------------------------------------

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"auth-token": self.token, "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _call(self, method: str, url: str, what: str, params: Optional[dict] = None, body: Optional[dict] = None) -> Any:
        if time.time() < self.blocked_until:
            raise ApiError(what, 429, "waiting for the MetaApi rate limit to reset", self.blocked_until - time.time())
        session = await self._http()
        try:
            async with session.request(method, url, params=params, json=body) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    retry = _retry_after(resp.headers.get("Retry-After"), text)
                    if resp.status == 429:
                        self.blocked_until = time.time() + min(retry or 60.0, 1800.0)
                    raise ApiError(what, resp.status, _message(text), retry)
                return json.loads(text) if text.strip() else None
        except ApiError:
            raise
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise ApiError(what, None, f"{type(exc).__name__} {exc}".strip()) from exc

    def _trade_host(self) -> str:
        return f"https://mt-client-api-v1.{self.region}.agiliumtrade.ai"

    def _data_host(self) -> str:
        return f"https://mt-market-data-client-api-v1.{self.region}.agiliumtrade.ai"

    def _acc(self, suffix: str) -> str:
        return f"{self._trade_host()}/users/current/accounts/{quote(self.account_id, safe='')}{suffix}"

    # -- account management ------------------------------------------------------

    async def accounts(self) -> list[dict]:
        data = await self._call("GET", f"{PROVISIONING}/users/current/accounts", "accounts")
        if isinstance(data, dict):
            data = data.get("items") or []
        return list(data or [])

    async def account(self) -> dict:
        url = f"{PROVISIONING}/users/current/accounts/{quote(self.account_id, safe='')}"
        data = dict(await self._call("GET", url, "account") or {})
        if data.get("region"):
            self.region = data["region"]
        return data

    async def deploy(self) -> None:
        url = f"{PROVISIONING}/users/current/accounts/{quote(self.account_id, safe='')}/deploy"
        await self._call("POST", url, "deploy")

    # -- market data ------------------------------------------------------------------

    async def candles(self, symbol: str, timeframe: str = "5m", limit: int = 80) -> list[dict]:
        """Latest candles, oldest first, each with ``t`` (bar start, epoch seconds UTC)."""
        url = (
            f"{self._data_host()}/users/current/accounts/{quote(self.account_id, safe='')}"
            f"/historical-market-data/symbols/{quote(symbol, safe='')}/timeframes/{timeframe}/candles"
        )
        data = await self._call("GET", url, "candles", params={"limit": str(limit)})
        out = []
        for c in data or []:
            try:
                out.append({"t": parse_time(c["time"]), "o": float(c["open"]), "h": float(c["high"]), "l": float(c["low"]), "c": float(c["close"])})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c["t"])
        return out

    async def price(self, symbol: str) -> dict:
        url = self._acc(f"/symbols/{quote(symbol, safe='')}/current-price")
        data = dict(await self._call("GET", url, "price", params={"keepSubscription": "true"}) or {})
        return {"bid": float(data["bid"]), "ask": float(data["ask"])}

    # -- account state ------------------------------------------------------------------

    async def info(self) -> dict:
        return dict(await self._call("GET", self._acc("/account-information"), "account-information") or {})

    async def positions(self) -> list[dict]:
        return list(await self._call("GET", self._acc("/positions"), "positions") or [])

    async def deals_for_position(self, position_id: str) -> list[dict]:
        url = self._acc(f"/history-deals/position/{quote(str(position_id), safe='')}")
        return list(await self._call("GET", url, "history-deals") or [])

    async def deals_between(self, start_iso: str, end_iso: str) -> list[dict]:
        url = self._acc(f"/history-deals/time/{quote(start_iso, safe='')}/{quote(end_iso, safe='')}")
        return list(await self._call("GET", url, "history-deals-time") or [])

    # -- orders -----------------------------------------------------------------------------

    async def _trade(self, body: dict) -> dict:
        data = dict(await self._call("POST", self._acc("/trade"), "trade", body=body) or {})
        code = data.get("numericCode")
        if code not in OK_CODES and data.get("stringCode") not in ("TRADE_RETCODE_DONE", "ERR_NO_ERROR"):
            raise TradeRejected(code, f"{data.get('stringCode')} {data.get('message')}")
        return data

    async def market_order(self, symbol: str, side: int, volume: float, stop_loss: float, comment: str) -> dict:
        return await self._trade(
            {
                "actionType": "ORDER_TYPE_BUY" if side > 0 else "ORDER_TYPE_SELL",
                "symbol": symbol,
                "volume": volume,
                "stopLoss": round(stop_loss, 3),
                "comment": comment,
            }
        )

    async def modify_stop(self, position_id: str, stop_loss: float) -> dict:
        return await self._trade({"actionType": "POSITION_MODIFY", "positionId": str(position_id), "stopLoss": round(stop_loss, 3)})

    async def close_position(self, position_id: str) -> dict:
        return await self._trade({"actionType": "POSITION_CLOSE_ID", "positionId": str(position_id)})


def _message(text: str) -> str:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            parts = [str(data[k]) for k in ("error", "message") if data.get(k)]
            if parts:
                return " - ".join(parts)[:300]
    except ValueError:
        pass
    return (text or "").strip()[:300]


def _retry_after(header: Optional[str], text: str) -> Optional[float]:
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:
        data = json.loads(text)
        when = (data.get("metadata") or {}).get("recommendedRetryTime") if isinstance(data, dict) else None
        if when:
            return max(0.0, parse_time(when) - time.time())
    except (ValueError, TypeError):
        pass
    return None
