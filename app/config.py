"""Runtime configuration, loaded once from environment variables.

Secrets (tokens, passwords) are only ever read from the environment and are
never logged. Use ``Settings.redacted()`` when settings need to be printed.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

# This build only contains a monitoring / simulation risk manager. There is no
# code path that sends orders or POSITION_MODIFY requests to a broker, so dry-run
# is a fact of the build rather than a toggle that could be flipped by mistake.
DRY_RUN = True


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


def _env_list(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


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
    initial_sl_usd: float
    trail_trigger_usd: float
    trail_gap_usd: float
    trail_step_usd: float
    telegram_poll_timeout: int
    controller_poll_seconds: float
    copyfactory_refresh_seconds: float
    rest_timeout_seconds: float
    spec_cache_seconds: float
    pnl_update_seconds: float
    close_confirm_polls: int
    managed_exclude_reasons: tuple[str, ...]
    log_level: str

    @property
    def dry_run(self) -> bool:
        return DRY_RUN

    def redacted(self) -> dict:
        data = asdict(self)
        for key in ("metaapi_token", "telegram_bot_token", "telegram_pairing_code", "dashboard_password"):
            data[key] = "set" if data[key] else "missing"
        data["data_dir"] = str(self.data_dir)
        data["dry_run"] = self.dry_run
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
        initial_sl_usd=_env_float("DEFAULT_INITIAL_SL_USD", 0.60),
        trail_trigger_usd=_env_float("DEFAULT_TRAIL_TRIGGER_USD", 0.50),
        trail_gap_usd=_env_float("DEFAULT_TRAIL_GAP_USD", 0.20),
        trail_step_usd=_env_float("DEFAULT_TRAIL_STEP_USD", 0.10),
        telegram_poll_timeout=_env_int("TELEGRAM_POLL_TIMEOUT", 25),
        controller_poll_seconds=max(0.25, _env_float("CONTROLLER_POLL_SECONDS", 0.5)),
        copyfactory_refresh_seconds=max(10.0, _env_float("COPYFACTORY_REFRESH_SECONDS", 30.0)),
        rest_timeout_seconds=max(2.0, _env_float("METAAPI_REST_TIMEOUT_SECONDS", 15.0)),
        spec_cache_seconds=max(30.0, _env_float("SPEC_CACHE_SECONDS", 600.0)),
        pnl_update_seconds=max(0.0, _env_float("TELEGRAM_PNL_UPDATE_SECONDS", 60.0)),
        close_confirm_polls=max(1, _env_int("CLOSE_CONFIRM_POLLS", 2)),
        managed_exclude_reasons=_env_list(
            "MANAGED_EXCLUDE_REASONS",
            "POSITION_REASON_CLIENT,POSITION_REASON_MOBILE,POSITION_REASON_WEB",
        ),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )
