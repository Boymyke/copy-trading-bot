"""Ferrn Gold Copy Trader v4.

The v4 architecture deliberately separates mission-critical trade copying from
Railway. MetaApi CopyFactory copies source trades to the target account; this
package provides configuration, Telegram controls, monitoring and target-side
risk management.
"""

__version__ = "4.0.0"
