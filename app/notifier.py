"""Non-blocking notification queue.

Producers (risk manager, CopyFactory monitor) call ``notify`` and never wait on
Telegram. The Telegram task drains the queue. If Telegram is not configured or
not paired, messages are still written to the structured logs.
"""

from __future__ import annotations

import asyncio

from .logging_setup import get_logger

log = get_logger("notify")


class Notifier:
    def __init__(self, maxsize: int = 500):
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def notify(self, text: str, kind: str = "info") -> None:
        log.info("notification", kind=kind, text=text)
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning("notification_dropped", dropped_total=self.dropped)
