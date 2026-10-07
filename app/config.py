"""Runtime configuration, loaded once from environment variables.

Secrets (tokens, passwords) are only ever read from the environment and are
never logged. Use ``Settings.redacted()`` when settings need to be printed.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


@dataclass(frozen=True)
class Settings:
    metaapi_token: str
    metaapi_rest_base_url: str
    source_account_id: str
    target_account_id: str
    telegram_bot_token: str
    telegram_pairing_code: str
    dashboard_password: str
    data_dir: Path
    copy_symbol: str
    target_symbol: str
    strategy_name: str
    fixed_lot: float
    telegram_poll_timeout: int
    monitor_poll_seconds: float
    copyfactory_refresh_seconds: float
    rest_timeout_seconds: float
    log_level: str

    def redacted(self) -> dict:
        data = asdict(self)
        for key in ("metaapi_token", "telegram_bot_token", "telegram_pairing_code", "dashboard_password"):
            data[key] = "set" if data[key] else "missing"
        data["data_dir"] = str(self.data_dir)
        return data


def load_settings() -> Settings:
    data_dir = Path(_env_str("DATA_DIR") or ("/data" if os.path.isdir("/data") else "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        metaapi_token=_env_str("METAAPI_TOKEN"),
        metaapi_rest_base_url=(
            _env_str("METAAPI_REST_BASE_URL") or "https://mt-client-api-v1.london.agiliumtrade.ai"
        ).rstrip("/"),
        source_account_id=_env_str("METAAPI_SOURCE_ACCOUNT_ID"),
        target_account_id=_env_str("METAAPI_TARGET_ACCOUNT_ID"),
        telegram_bot_token=_env_str("TELEGRAM_BOT_TOKEN"),
        telegram_pairing_code=_env_str("TELEGRAM_PAIRING_CODE"),
        dashboard_password=_env_str("DASHBOARD_PASSWORD"),
        data_dir=data_dir,
        copy_symbol=_env_str("COPY_SYMBOL", "XAUUSD.f"),
        target_symbol=_env_str("TARGET_SYMBOL", "XAUUSDm"),
        strategy_name=_env_str("COPYFACTORY_STRATEGY_NAME", "Gold Source Strategy"),
        fixed_lot=_env_float("DEFAULT_LOT", 0.01),
        telegram_poll_timeout=_env_int("TELEGRAM_POLL_TIMEOUT", 25),
        # Monitoring only: CopyFactory copies trades natively, so position reads can be
        # infrequent. Floor of 15 s keeps the trading-API credit budget far from 429.
        # (CONTROLLER_POLL_SECONDS from older builds is intentionally ignored.)
        monitor_poll_seconds=max(15.0, _env_float("MONITOR_POLL_SECONDS", 60.0)),
        copyfactory_refresh_seconds=max(30.0, _env_float("COPYFACTORY_REFRESH_SECONDS", 60.0)),
        rest_timeout_seconds=max(5.0, _env_float("METAAPI_REST_TIMEOUT_SECONDS", 20.0)),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )
