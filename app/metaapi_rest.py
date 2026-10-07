"""Read-only MetaApi REST client.

This deliberately exposes GET requests only. There is no method in this module
that can open, close or modify a trade, which is what guarantees the risk
manager stays in dry-run mode.

Endpoints used (all ``GET``, header ``auth-token``):

* Trading terminal (region host, e.g. ``https://mt-client-api-v1.london.agiliumtrade.ai``)
    - ``/users/current/accounts/{id}/positions``
    - ``/users/current/accounts/{id}/symbols/{symbol}/specification``
    - ``/users/current/accounts/{id}/symbols/{symbol}/current-price``
    - ``/users/current/accounts/{id}/account-information``
    - ``/users/current/accounts/{id}/history-deals/position/{positionId}``
* Provisioning (global host ``https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai``)
    - ``/users/current/accounts/{id}`` (deployment state, broker connection status, CopyFactory roles)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from .logging_setup import get_logger

log = get_logger("rest")

PROVISIONING_BASE_URL = "https://mt-provisioning-api-v1.agiliumtrade.agiliumtrade.ai"


class MetaApiRestError(RuntimeError):
    """A failed REST call with enough context to diagnose it from logs."""

    def __init__(self, endpoint: str, status: Optional[int], message: str, retry_after: Optional[float] = None):
        self.endpoint = endpoint
        self.status = status
        self.retry_after = retry_after
        prefix = f"HTTP {status}" if status is not None else "network"
        super().__init__(f"{prefix} on {endpoint}: {message}")


class MetaApiRestClient:
    def __init__(self, token: str, base_url: str, timeout_seconds: float = 15.0):
        if not token:
            raise ValueError("METAAPI_TOKEN is required")
        self._token = token
        self.base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_ok_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.request_count = 0
        self.error_count = 0
        # Per-account trading-API host, learned from the account's provisioning
        # region (e.g. london -> https://mt-client-api-v1.london.agiliumtrade.ai).
        self.account_hosts: dict[str, str] = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"auth-token": self._token, "Accept": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _get(self, url: str, endpoint: str, params: Optional[dict] = None) -> Any:
        session = await self._get_session()
        started = time.monotonic()
        self.request_count += 1
        try:
            async with session.get(url, params=params) as response:
                elapsed_ms = round((time.monotonic() - started) * 1000)
                text = await response.text()
                if response.status >= 400:
                    retry_after = _retry_after(response.headers.get("Retry-After"), text)
                    message = _error_message(text)
                    self.error_count += 1
                    self.last_error = f"HTTP {response.status} {endpoint}: {message}"
                    log.warning(
                        "rest_error",
                        endpoint=endpoint,
                        status=response.status,
                        elapsed_ms=elapsed_ms,
                        message=message,
                        retry_after=retry_after,
                    )
                    raise MetaApiRestError(endpoint, response.status, message, retry_after)
                self.last_ok_at = time.time()
                self.last_error = None
                log.debug("rest_ok", endpoint=endpoint, status=response.status, elapsed_ms=elapsed_ms)
                if not text:
                    return None
                return await asyncio.to_thread(_parse_json, text)
        except MetaApiRestError:
            raise
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            elapsed_ms = round((time.monotonic() - started) * 1000)
            message = f"{type(exc).__name__}: {exc}".strip(": ")
            self.error_count += 1
            self.last_error = f"{endpoint}: {message}"
            log.warning("rest_network_error", endpoint=endpoint, elapsed_ms=elapsed_ms, message=message)
            raise MetaApiRestError(endpoint, None, message) from exc

    # -- trading terminal ---------------------------------------------------

    def set_account_region(self, account_id: str, region: Optional[str]) -> None:
        if region:
            self.account_hosts[account_id] = f"https://mt-client-api-v1.{region}.agiliumtrade.ai"

    def _account_url(self, account_id: str, suffix: str) -> str:
        host = self.account_hosts.get(account_id, self.base_url)
        return f"{host}/users/current/accounts/{quote(account_id, safe='')}{suffix}"

    async def get_positions(self, account_id: str) -> list[dict]:
        data = await self._get(self._account_url(account_id, "/positions"), "positions")
        return list(data or [])

    async def get_symbol_specification(self, account_id: str, symbol: str) -> dict:
        url = self._account_url(account_id, f"/symbols/{quote(symbol, safe='')}/specification")
        return dict(await self._get(url, f"specification:{symbol}") or {})

    async def get_symbol_price(self, account_id: str, symbol: str) -> dict:
        url = self._account_url(account_id, f"/symbols/{quote(symbol, safe='')}/current-price")
        # keepSubscription keeps MetaApi streaming the quote server-side so the
        # next REST read is fast. It does not open a websocket in this process.
        return dict(await self._get(url, f"price:{symbol}", params={"keepSubscription": "true"}) or {})

    async def get_account_information(self, account_id: str) -> dict:
        return dict(await self._get(self._account_url(account_id, "/account-information"), "account-information") or {})

    async def get_deals_by_position(self, account_id: str, position_id: str) -> list[dict]:
        url = self._account_url(account_id, f"/history-deals/position/{quote(str(position_id), safe='')}")
        return list(await self._get(url, "history-deals") or [])

    # -- provisioning -------------------------------------------------------

    async def get_account(self, account_id: str) -> dict:
        url = f"{PROVISIONING_BASE_URL}/users/current/accounts/{quote(account_id, safe='')}"
        return dict(await self._get(url, "provisioning-account") or {})


def _parse_json(text: str) -> Any:
    import json

    return json.loads(text)


def _error_message(text: str) -> str:
    try:
        data = _parse_json(text)
        if isinstance(data, dict):
            parts = [str(data.get(k)) for k in ("error", "message") if data.get(k)]
            return " - ".join(parts)[:500] or text[:500]
    except Exception:
        pass
    return (text or "").strip()[:500]


def _retry_after(header: Optional[str], text: str) -> Optional[float]:
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:
        data = _parse_json(text)
        recommended = (data.get("metadata") or {}).get("recommendedRetryTime") if isinstance(data, dict) else None
        if recommended:
            from datetime import datetime, timezone

            when = datetime.fromisoformat(str(recommended).replace("Z", "+00:00"))
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        pass
    return None
