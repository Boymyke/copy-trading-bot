import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    metaapi_token: str
    source_account_id: str
    target_account_id: str
    telegram_bot_token: str
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


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", "/data" if os.path.isdir("/data") else "."))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        metaapi_token=os.getenv("METAAPI_TOKEN", "").strip(),
        source_account_id=os.getenv("METAAPI_SOURCE_ACCOUNT_ID", "").strip(),
        target_account_id=os.getenv("METAAPI_TARGET_ACCOUNT_ID", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", "").strip(),
        data_dir=data_dir,
        copy_symbol=os.getenv("COPY_SYMBOL", "XAUUSD").strip(),
        target_symbol=os.getenv("TARGET_SYMBOL", "XAUUSD").strip(),
        strategy_name=os.getenv("COPYFACTORY_STRATEGY_NAME", "Ferrn Gold Copier").strip(),
        fixed_lot=float(os.getenv("DEFAULT_LOT", "0.01")),
        initial_sl_usd=float(os.getenv("DEFAULT_INITIAL_SL_USD", "0.60")),
        trail_trigger_usd=float(os.getenv("DEFAULT_TRAIL_TRIGGER_USD", "0.50")),
        trail_gap_usd=float(os.getenv("DEFAULT_TRAIL_GAP_USD", "0.20")),
        trail_step_usd=float(os.getenv("DEFAULT_TRAIL_STEP_USD", "0.10")),
        telegram_poll_timeout=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25")),
        controller_poll_seconds=float(os.getenv("CONTROLLER_POLL_SECONDS", "1.0")),
    )
