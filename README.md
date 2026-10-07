# Gold Copy Trader v5 — CopyFactory + REST dry-run risk monitor

```
Gold Source (XAUUSD.f) ──MetaApi CopyFactory──▶ Gold Target (XAUUSDm, fixed 0.01)
                                   ▲                          │
                read-only adopt    │                          │ REST GET only
                pause/resume/lot   │                          ▼
                         ┌──────── Railway: python -m app.main ────────┐
                         │ CopyFactory monitor · DRY-RUN risk manager  │
                         │ Telegram bot · dashboard · SQLite in /data  │
                         └──────────────────────────────────────────────┘
```

* **Trade copying** happens inside MetaApi CopyFactory using the strategy you
  already configured (`Gold Source Strategy`: `XAUUSD.f → XAUUSDm`, fixed 0.01,
  SL/TP copy off, pending orders skipped, reverse off).
* **This service never creates or overwrites CopyFactory config.** It adopts
  the existing strategy and subscriber, validates them, and reports differences
  as warnings. The only writes are the ones you trigger: `/pause`, `/resume`,
  `/lot` — each is read-modify-write of a single field.
* **The risk manager is DRY RUN.** It polls the target account over MetaApi
  REST (no RPC/WebSocket), calculates the SL and trailing SL it *would* set, and
  reports them. There is no code that sends orders or `POSITION_MODIFY`.

## Simulated risk rules (per 0.01 lot, scaled by volume)

| Rule | Default | Env / Telegram |
|---|---|---|
| Initial SL | $0.60 risk | `DEFAULT_INITIAL_SL_USD` · `/setsl` |
| Trailing starts | +$0.50 floating | `DEFAULT_TRAIL_TRIGGER_USD` · `/settrigger` |
| Trailing gap | $0.20 behind price | `DEFAULT_TRAIL_GAP_USD` · `/setgap` |
| Trailing step | SL moves only when it locks ≥ $0.10 more | `DEFAULT_TRAIL_STEP_USD` · `/setstep` |

Broker rules applied from the `XAUUSDm` specification and quote: tick size,
digits, point, `stopsLevel` (minimum stop distance), `freezeLevel`, and tick
value (`lossTickValue` / `profitTickValue` from the price). Prices are rounded
conservatively, and a calculated SL is never widened.

A target position is **managed** only if it is `TARGET_SYMBOL`, at the
CopyFactory lot, and not opened manually (`MANAGED_EXCLUDE_REASONS`). All other
positions are listed as ignored and never touched.

## Telegram

Pair once with `/pair <TELEGRAM_PAIRING_CODE or DASHBOARD_PASSWORD>`.
Commands: `/status`, `/positions`, `/pause`, `/resume`, `/lot`, `/setsl`,
`/settrigger`, `/setgap`, `/setstep`, `/help`.

Automatic messages: trade detected (entry, side, lot, simulated SL), simulated
trailing moves, "simulated SL would have been hit", floating P/L every
`TELEGRAM_PNL_UPDATE_SECONDS`, trade closed with final P/L from deal history,
REST/API failures (after 3 in a row) and recovery, CopyFactory config warnings.

## Dashboard

`https://<railway-domain>/` — user `trader`, password `DASHBOARD_PASSWORD`.
`/health` is unauthenticated and always 200 while the process runs.

## Logs

Every line on stdout is JSON (`ts`, `level`, `logger`, `event`, fields).
Useful Railway log filters: `"event":"dry_run_intended_action"`,
`"event":"rest_error"`, `"kind":"trade-detected"`, `"event":"risk_cycle_failed"`,
`"event":"copyfactory_refresh_failed"`.

## Configuration

See `.env.example`. Required: `METAAPI_TOKEN` (an **API access token**, not an
account access token — CopyFactory configuration calls need it),
`METAAPI_SOURCE_ACCOUNT_ID`, `METAAPI_TARGET_ACCOUNT_ID`,
`METAAPI_REST_BASE_URL` (the target account's region host).
Persistent state lives in `DATA_DIR` (`/data` volume on Railway).

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

Tests cover the SL/trailing maths and a full lifecycle against a fake MetaApi
REST server, including a check that only `GET` requests reach the trading API.

`legacy/` holds earlier copiers that **do** send live orders; they are not run.
