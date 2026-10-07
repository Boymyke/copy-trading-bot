"""Gold Copy Trader v6.

MetaApi CopyFactory mirrors the source trade lifecycle onto the target account
(source open -> target open, source close -> target close). This package adopts
that existing CopyFactory setup, monitors both accounts at low frequency, and
provides Telegram control, a dashboard and health checks. It sends no trades.
"""

__version__ = "6.0.0"
