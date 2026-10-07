"""Gold Copy Trader v5.

MetaApi CopyFactory copies source trades to the target account natively. This
package adopts that existing CopyFactory setup (read-only), monitors the target
account over MetaApi REST, and runs a DRY-RUN risk manager that calculates and
reports simulated stop-loss / trailing-stop actions without sending any orders.
"""

__version__ = "5.0.0"
