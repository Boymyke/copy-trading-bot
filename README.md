# MT5 Gold Copy Trading Bot

This project watches one MetaTrader 5 account (the **source**) and mirrors eligible live positions to a second MT5 account (the **target**).

You **do not need the source EA's code**. The source monitor reads the positions that already exist in the MT5 terminal/account.

## Current behavior

- Watches live `XAUUSD` / `GOLD` positions on the source account.
- Copies BUY and SELL entries to the target account.
- Applies the target bot's **own lot size, stop loss and take profit**.
- Supports fixed lot, source-volume multiplier, or percentage-risk sizing.
- If the target trade reaches its own SL/TP first, it is **not reopened** while that same source ticket remains open.
- If the source trade closes while the copied trade is still open, the copier automatically closes the copied trade.
- Persists source-ticket → target-ticket state across restarts.
- Refuses to trade when source state is stale.
- Defaults to `DRY_RUN=true`.

## Architecture

The MetaTrader5 Python package controls a locally installed MT5 terminal. To avoid one process switching back and forth between two terminals/accounts, this project uses two processes:

- `source_monitor.py` → source MT5 terminal → `runtime/source_positions.json`
- `target_copier.py` → target MT5 terminal → copied trades

Both scripts should initially run on the **same Windows PC or Windows VPS/VM** with two MT5 terminal installations.

## Install

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your source and target terminal/account details.

Start the source side:

```bash
python source_monitor.py
```

Then start the target side in a second terminal:

```bash
python target_copier.py
```

Keep this at first:

```env
DRY_RUN=true
```

Once detection and intended orders look correct on demo accounts, change it to:

```env
DRY_RUN=false
```

## Connecting the two MT5 accounts

You need two MT5 terminals on the same Windows machine or VPS:

1. **Source terminal** — logged into the account where your existing gold EA trades.
2. **Target terminal** — logged into the account that should copy those trades.

Fill these values in `.env`:

```env
SOURCE_MT5_PATH=C:\\Program Files\\MetaTrader 5 Source\\terminal64.exe
SOURCE_LOGIN=12345678
SOURCE_PASSWORD=your_source_trading_password
SOURCE_SERVER=YourBroker-Server

TARGET_MT5_PATH=C:\\Program Files\\MetaTrader 5 Target\\terminal64.exe
TARGET_LOGIN=87654321
TARGET_PASSWORD=your_target_trading_password
TARGET_SERVER=YourBroker-Server
```

Use the **trading password**, not the investor/read-only password, for the target account because it must place and close trades.

The source account can technically be read-only if the terminal exposes positions, but using the normal trading login is simplest.

## Gold symbol mapping

The source and target brokers may use different names such as `XAUUSD`, `XAUUSDm`, `GOLD`, or `GOLD.a`.

Example:

```env
TARGET_SYMBOL_MAP=XAUUSD:XAUUSDm,GOLD:GOLD.a
```

## Lot modes

### Fixed

```env
LOT_MODE=fixed
FIXED_LOT=0.01
```

### Source multiplier

```env
LOT_MODE=source_multiplier
LOT_MULTIPLIER=0.5
```

A source position of 0.10 lots becomes 0.05 lots on the target.

### Equity risk percentage

```env
LOT_MODE=risk_percent
RISK_PERCENT=1.0
SL_POINTS=500
```

The copier calculates target volume from target equity, target broker tick value/tick size, and the configured SL distance.

## SL and TP

`SL_POINTS` and `TP_POINTS` are MT5 **points for the target symbol**, not dollar values.

```env
SL_POINTS=500
TP_POINTS=1000
```

Set either one to `0` to disable it.

## Not yet included

The first version does not yet mirror:

- partial source closes,
- changes to the source SL/TP,
- pending orders,
- one source trade across multiple target accounts,
- remote source/target machines.

Those can be added after the basic copier is verified on demo.

## Safety

Automated trading can lose money quickly. Verify symbol mapping, point size, contract size, minimum stop distances, fill mode and lot sizing on a demo account before enabling live execution. Never commit `.env` or MT5 credentials to GitHub.
