"""Structured (one JSON object per line) logging to stdout for Railway.

Usage::

    log = get_logger("risk")
    log.info("position_detected", position_id="123", side="BUY")

Every line carries ``ts``, ``level``, ``logger`` and ``event`` plus any keyword
fields, so Railway's log search can filter on e.g. ``"event":"rest_error"``.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

_SECRET_KEYS = {"token", "auth-token", "password", "metaapi_token", "telegram_bot_token"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            for key, value in fields.items():
                payload[key] = "***" if key.lower() in _SECRET_KEYS else value
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
        return json.dumps(payload, default=str, ensure_ascii=False)


class StructuredLogger:
    """Thin wrapper so call sites can pass fields as keyword arguments."""

    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _log(self, level: int, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._logger.log(level, event, extra={"fields": fields}, exc_info=exc_info)

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, exc_info: bool = False, **fields: Any) -> None:
        self._log(logging.ERROR, event, exc_info=exc_info, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, exc_info=True, **fields)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level, logging.INFO))
    # The MetaApi SDKs are chatty; we keep their warnings but not their info noise.
    for noisy in ("metaapi_cloud_sdk", "metaapi_cloud_copyfactory_sdk", "aiohttp.access", "socketio", "engineio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> StructuredLogger:
    return StructuredLogger(logging.getLogger(f"app.{name}"))
