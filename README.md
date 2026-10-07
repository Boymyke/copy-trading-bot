# Gold Copy Trader — MetaApi CopyFactory mirror

```
Gold Source (XAUUSD.f) ──MetaApi CopyFactory──▶ Gold Target (XAUUSDm, fixed 0.01)
     source open  ─────────────────────────────▶ target open
     source close ─────────────────────────────▶ target close
                       ▲
                       │ read-only monitoring (1 poll / 60 s), /pause /resume
            Railway: python -m app.main  (Telegram · dashboard · /health)
```

* **Execution is 100% MetaApi CopyFactory**, using the existing
  `Gold Source Strategy` and `Gold Target Subscriber`
  (`XAUUSD.f → XAUUSDm`, fixed volume 0.01, source SL/TP not copied, pending
  orders skipped, reverse off). The target mirrors the source lifecycle and has
  no SL, TP, trailing stop or exit logic of its own.
* **This service sends no trades.** It never creates or overwrites
  CopyFactory strategies or subscribers. The only writes are `/pause` and
  `/resume`, which toggle `closeOnly` on the existing subscription and leave
  every other field untouched.
* It does not depend on any laptop, local MT5 terminal or local process.

## What Railway runs

| Task | Frequency | API |
|---|---|---|
| CopyFactory config + account check | every 60 s | CopyFactory / provisioning REST |
| Source + target open positions | every 60 s (2 calls) | trading REST |
| CopyFactory user log (copy errors) | every 60 s | CopyFactory REST |
| Telegram commands | long-poll | Telegram |

On HTTP 429 the monitor waits for MetaApi's `recommendedRetryTime` (up to
30 min) instead of retrying. Every loop is supervised and restarts forever;
`/health` stays 200 while the process is alive, and Railway's restart policy is
`ALWAYS`.

## Alerts (Telegram)

Source opened / closed · target opened / closed (with final P/L) · source and
target out of sync for 2 polls · target lot not 0.01 · CopyFactory user-log
warnings/errors · CopyFactory settings that could skip or close trades on their
own (lifetime, risk limits, max stop loss, stop-outs, paused) · accounts
disconnected · monitoring failures and recovery.

Commands: `/status`, `/positions`, `/pause`, `/resume`, `/help`.
Pair once with `/pair <TELEGRAM_PAIRING_CODE or DASHBOARD_PASSWORD>`.

## Dashboard

`https://<railway-domain>/` — user `trader`, password `DASHBOARD_PASSWORD`.

## Configuration

See `.env.example`. Required: `METAAPI_TOKEN` (API access token),
`METAAPI_SOURCE_ACCOUNT_ID`, `METAAPI_TARGET_ACCOUNT_ID`. State lives in
`DATA_DIR` (`/data` volume on Railway). Logs are one JSON object per line.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

`legacy/` holds earlier copiers that send live orders; they are not run.
