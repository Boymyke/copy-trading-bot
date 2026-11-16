"""GoldBot: rules, practice mode against a reference simulator, and the live order flow."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from goldbot import strategy as S
from goldbot.bot import COMMENT, Bot, State
from goldbot.metaapi import TradeRejected

SPREAD = 0.30


# ----------------------------------------------------------------------------- rules

def test_breakout_signal():
    highs, lows = [10.0] * 72, [5.0] * 72
    assert S.breakout_signal(highs, lows, 10.01) == 1
    assert S.breakout_signal(highs, lows, 4.99) == -1
    assert S.breakout_signal(highs, lows, 10.0) == 0
    assert S.breakout_signal(highs[:71], lows[:71], 99.0) == 0          # not enough history
    assert S.breakout_signal([50.0] + highs, [1.0] + lows, 10.01) == 1  # only the last 72 bars count


def test_session_is_12_to_17_utc():
    day = 1_794_787_200  # 2026-11-16 00:00 UTC
    assert not S.in_session(day + 11 * 3600 + 55 * 60)
    assert S.in_session(day + 12 * 3600)
    assert S.in_session(day + 16 * 3600 + 55 * 60)
    assert not S.in_session(day + 17 * 3600)


def test_trailing_stop_only_moves_forward():
    entry, stop, best = 4000.0, 3994.0, 0.0
    best, stop = S.trail(1, entry, stop, best, 4001.9)
    assert stop == 3994.0                       # not $2 ahead yet
    best, stop = S.trail(1, entry, stop, best, 4002.0)
    assert stop == pytest.approx(4000.0)        # break-even once $2 ahead
    best, stop = S.trail(1, entry, stop, best, 4005.0)
    assert stop == pytest.approx(4003.0)
    best, stop = S.trail(1, entry, stop, best, 4003.5)
    assert stop == pytest.approx(4003.0)        # never moves back
    best, stop = S.trail(-1, entry, 4006.0, 0.0, 3996.5)
    assert stop == pytest.approx(3998.5)
    assert S.stop_hit(1, 4003.0, 4003.0) and not S.stop_hit(1, 4003.0, 4003.01)
    assert S.stop_hit(-1, 3998.5, 3998.5) and not S.stop_hit(-1, 3998.5, 3998.49)


def test_best_is_recovered_from_a_trailed_stop():
    assert S.best_from_stop(1, 4000.0, 3994.0) == 0.0
    assert S.best_from_stop(1, 4000.0, 3994.1) == 0.0           # slippage on the first stop is not a trail
    assert S.best_from_stop(1, 4000.0, 4003.0) == pytest.approx(5.0)
    assert S.best_from_stop(-1, 4000.0, 3998.5) == pytest.approx(3.5)


def test_lot_size():
    assert S.lot_size(12) == 0.01
    assert S.lot_size(79.99) == 0.01
    assert S.lot_size(80) == 0.02
    assert S.lot_size(200) == 0.05
    assert S.lot_size(10_000) == 0.50
    assert S.lot_size(12, fixed=0.03) == 0.03
    assert S.money(6.0, 0.01) == pytest.approx(6.0)


# ----------------------------------------------------------------------------- market replay

def make_market(days: int = 60, seed: int = 3):
    """Random one-minute bars, Monday-Friday, 01:00-21:00 UTC."""
    rng = random.Random(seed)
    t0 = 1_794_787_200  # Monday 2026-11-16 00:00 UTC
    T, O, H, L, C = [], [], [], [], []
    price = 4000.0
    for d in range(days):
        if d % 7 >= 5:
            continue
        drift = rng.choice([-0.012, 0.0, 0.012])
        for m in range(60, 21 * 60):
            o = price
            c = o + rng.gauss(drift, 0.55)
            T.append(t0 + d * 86400 + m * 60)
            O.append(o); C.append(c)
            H.append(max(o, c) + abs(rng.gauss(0, 0.25))); L.append(min(o, c) - abs(rng.gauss(0, 0.25)))
            price = c
    return T, O, H, L, C


def five_minute(T, O, H, L, C):
    bars, nxt = [], []
    i = 0
    while i < len(T):
        k = T[i] // 300
        j = i
        while j < len(T) and T[j] // 300 == k:
            j += 1
        bars.append({"t": k * 300, "o": O[i], "h": max(H[i:j]), "l": min(L[i:j]), "c": C[j - 1]})
        nxt.append(j)
        i = j
    return bars, nxt


def reference_trades(T, O, H, L, C):
    """The back-test simulator the rules were chosen with (6h breakout, 12-17 UTC, stop 6, trail 2 after +2)."""
    bars, nxt = five_minute(T, O, H, L, C)
    out, free, day, cnt, n = [], 0, -1, 0, len(T)
    for i, bar in enumerate(bars):
        if i < 72 or not S.in_session(bar["t"]):
            continue
        hi, lo = max(b["h"] for b in bars[i - 72:i]), min(b["l"] for b in bars[i - 72:i])
        dr = 1 if bar["c"] > hi else -1 if bar["c"] < lo else 0
        k = nxt[i]
        if not dr or k >= n - 2 or k < free or T[k] != bar["t"] + 300:
            continue
        d = T[k] // 86400
        if d != day:
            day, cnt = d, 0
        if cnt >= 2:
            continue
        entry = O[k] + SPREAD if dr > 0 else O[k]
        stop, best, pnl, j = entry - dr * 6, 0.0, None, k
        while j < n and T[j] - T[k] <= 240 * 60:
            if dr > 0:
                if L[j] <= stop:
                    pnl = stop - entry
                    break
                best = max(best, H[j] - entry)
                if best >= 2:
                    stop = max(stop, entry + best - 2)
            else:
                if H[j] + SPREAD >= stop:
                    pnl = entry - stop
                    break
                best = max(best, entry - (L[j] + SPREAD))
                if best >= 2:
                    stop = min(stop, entry - best + 2)
            j += 1
        if pnl is None:
            j = min(j, n - 1)
            pnl = (C[j] - entry) if dr > 0 else (entry - C[j] - SPREAD)
        out.append((T[k], dr, round(pnl, 6)))
        cnt += 1
        free = min(j, n - 1) + 1
    return out


class FakeBroker:
    """Stands in for MetaApi: serves bars and quotes from the replay clock, and keeps one position."""

    account_id = "acc"

    def __init__(self, T, O, H, L, C):
        self.T, self.O, self.H, self.L, self.C = T, O, H, L, C
        self.bars, self.nxt = five_minute(T, O, H, L, C)
        self.now = 0.0
        self.quote = {"bid": 0.0, "ask": 0.0}
        self.balance = 12.0
        self.position = None
        self.deals: dict[str, list] = {}
        self.orders: list[dict] = []
        self.reject_modify = False

    def clock(self) -> float:
        return self.now

    async def candles(self, symbol, timeframe="5m", limit=80):
        done = [b for b in self.bars if b["t"] + 300 <= self.now]
        return done[-limit:]

    async def price(self, symbol):
        return dict(self.quote)

    async def info(self):
        return {"balance": self.balance, "equity": self.balance}

    async def positions(self):
        return [dict(self.position)] if self.position else []

    async def market_order(self, symbol, side, volume, stop_loss, comment):
        price = self.quote["ask"] if side > 0 else self.quote["bid"]
        self.position = {
            "id": "777", "symbol": symbol, "type": "POSITION_TYPE_BUY" if side > 0 else "POSITION_TYPE_SELL",
            "openPrice": price + 0.07 * side, "currentPrice": price, "stopLoss": stop_loss, "volume": volume,
            "comment": comment, "time": "2026-11-16T12:05:00.000Z",
        }
        self.orders.append({"side": side, "volume": volume, "stopLoss": stop_loss})
        return {"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE", "orderId": "777", "positionId": "777"}

    async def modify_stop(self, position_id, stop_loss):
        if self.reject_modify:
            raise TradeRejected(10016, "TRADE_RETCODE_INVALID_STOPS")
        self.position["stopLoss"] = stop_loss
        return {"numericCode": 10009}

    async def close_position(self, position_id):
        self.broker_close(self.position["currentPrice"])
        return {"numericCode": 10009}

    def broker_close(self, price):
        p = self.position
        side = 1 if p["type"].endswith("BUY") else -1
        profit = (price - p["openPrice"]) * side * p["volume"] * 100
        self.deals[p["id"]] = [
            {"entryType": "DEAL_ENTRY_IN", "profit": 0, "commission": 0, "swap": 0, "price": p["openPrice"]},
            {"entryType": "DEAL_ENTRY_OUT", "profit": profit, "commission": 0, "swap": 0, "price": price},
        ]
        self.balance += profit
        self.position = None

    async def deals_for_position(self, position_id):
        return self.deals.get(str(position_id), [])

    async def account(self):
        return {"state": "DEPLOYED", "connectionStatus": "CONNECTED", "region": "london"}


def make_bot(broker, tmp_path: Path, live: bool = False):
    messages: list[str] = []
    state = State(tmp_path / "state.json")
    state["live"] = live
    bot = Bot(broker, state, messages.append, broker.clock)
    bot.wait, bot.ready = 0.0, True
    return bot, messages


async def test_practice_mode_reproduces_the_backtest(tmp_path):
    T, O, H, L, C = make_market()
    expected = reference_trades(T, O, H, L, C)
    assert len(expected) >= 15, "the synthetic market should produce a meaningful number of trades"
    broker = FakeBroker(T, O, H, L, C)
    bot, _ = make_bot(broker, tmp_path)
    index = {t: i for i, t in enumerate(T)}
    free = 0                                             # first minute bar after the last exit
    for bar in broker.bars:
        k = index.get(bar["t"] + 300)
        if k is None or k >= len(T) - 2 or k < free:
            continue
        broker.now = bar["t"] + 300 + 4
        broker.quote = {"bid": O[k], "ask": O[k] + SPREAD}
        await bot.tick()
        if not bot.state["pos"]:
            continue
        side, opened, j = bot.state["pos"]["side"], T[k], k
        while bot.state["pos"] and j < len(T):
            broker.now = T[j]
            if T[j] - opened > 240 * 60:
                ticks = [C[j]]
            else:
                ticks = [L[j], H[j]] if side > 0 else [H[j], L[j]]
            for bid in ticks:
                if not bot.state["pos"]:
                    break
                broker.quote = {"bid": bid, "ask": bid + SPREAD}
                await bot.manage()
            j += 1
        free = j
    got = [(t["side"], round(t["pnl"], 2)) for t in bot.state["trades"]]
    want = [(dr, round(pnl, 2)) for _, dr, pnl in expected]      # 0.01 lot: $1 per $1 move
    assert got == want


# ----------------------------------------------------------------------------- live order flow

def signal_market():
    """Six quiet hours, then one 5-minute bar that closes above everything at 12:05 UTC."""
    t0 = 1_794_787_200 + 6 * 3600
    T = [t0 + m * 60 for m in range(6 * 60 + 5)]
    O = [4000.0] * len(T); H = [4000.5] * len(T); L = [3999.5] * len(T); C = [4000.0] * len(T)
    for i in range(len(T) - 5, len(T)):
        H[i], C[i] = 4003.2, 4003.0
    return T, O, H, L, C


async def open_live_trade(tmp_path):
    broker = FakeBroker(*signal_market())
    bot, messages = make_bot(broker, tmp_path, live=True)
    broker.now = broker.T[-1] + 60 + 4
    broker.quote = {"bid": 4003.0, "ask": 4003.3}
    await bot.tick()
    return broker, bot, messages


async def test_live_trade_is_opened_with_a_broker_stop(tmp_path):
    broker, bot, messages = await open_live_trade(tmp_path)
    pos = bot.state["pos"]
    assert broker.orders == [{"side": 1, "volume": 0.01, "stopLoss": pytest.approx(3997.3)}]
    assert pos["live"] and pos["id"] == "777" and pos["entry"] == pytest.approx(4003.37)
    assert broker.position["stopLoss"] == pytest.approx(3997.37)      # re-centred on the real fill
    assert broker.position["comment"] == COMMENT
    assert bot.state["count"] == 1 and "BUY 0.01" in messages[-1] and "LIVE" in messages[-1]


async def test_live_stop_is_trailed_and_broker_close_is_reported(tmp_path):
    broker, bot, messages = await open_live_trade(tmp_path)
    broker.now += 30
    broker.position["currentPrice"] = 4004.5            # +1.13: stop stays
    await bot.manage()
    assert broker.position["stopLoss"] == pytest.approx(3997.37)
    broker.position["currentPrice"] = 4007.37           # +4: stop moves to entry + 2
    await bot.manage()
    assert broker.position["stopLoss"] == pytest.approx(4005.37)
    broker.broker_close(4005.37)                        # stop hit at the broker
    await bot.manage()
    assert bot.state["pos"] is not None                 # one missing read is not enough
    await bot.manage()
    assert bot.state["pos"] is None
    assert bot.state["trades"][-1]["pnl"] == pytest.approx(2.0)
    assert "+$2.00" in messages[-1] and "Balance: $14.00" in messages[-1]


async def test_live_trade_closed_after_four_hours(tmp_path):
    broker, bot, messages = await open_live_trade(tmp_path)
    broker.now += 240 * 60 + 31
    broker.position["currentPrice"] = 4004.0
    await bot.manage()
    assert broker.position is None and bot.state["pos"] is None
    assert "time limit" in messages[-1]


async def test_no_live_order_when_one_stop_out_costs_more_than_the_account(tmp_path):
    broker = FakeBroker(*signal_market())
    broker.balance = 5.0
    bot, messages = make_bot(broker, tmp_path, live=True)
    broker.now = broker.T[-1] + 60 + 4
    broker.quote = {"bid": 4003.0, "ask": 4003.3}
    await bot.tick()
    assert broker.orders == [] and bot.state["pos"] is None and "not taken" in messages[-1]


async def test_practice_mode_sends_no_orders(tmp_path):
    broker = FakeBroker(*signal_market())
    bot, messages = make_bot(broker, tmp_path, live=False)
    broker.now = broker.T[-1] + 60 + 4
    broker.quote = {"bid": 4003.0, "ask": 4003.3}
    await bot.tick()
    assert broker.orders == [] and bot.state["pos"]["live"] is False and "PRACTICE" in messages[-1]


async def test_restart_picks_the_open_trade_back_up(tmp_path):
    broker, bot, _ = await open_live_trade(tmp_path)
    broker.position["stopLoss"] = 4006.37               # already trailed: best was +5
    (tmp_path / "state.json").unlink()                  # no volume: state lost
    bot2, messages = make_bot(broker, tmp_path, live=True)
    await bot2.recover()
    pos = bot2.state["pos"]
    assert pos["id"] == "777" and pos["best"] == pytest.approx(5.0) and pos["stop"] == pytest.approx(4006.37)
    assert "picked" in messages[-1]


async def test_commands(tmp_path):
    broker = FakeBroker(*signal_market())
    bot, _ = make_bot(broker, tmp_path)
    assert "LIVE trading is ON" in await bot.command("/live on") and bot.state["live"] is True
    assert "OFF" in await bot.command("/live off") and bot.state["live"] is False
    await bot.command("/lot 0.02")
    assert bot.state["fixed_lot"] == 0.02
    await bot.command("/lot auto")
    assert bot.state["fixed_lot"] == 0.0
    assert "Not changed" in await bot.command("/per 5")
    await bot.command("/goal 200")
    assert bot.state["goal"] == 200.0
    assert "Mode: PRACTICE" in await bot.command("/status")
    assert State(tmp_path / "state.json")["goal"] == 200.0      # saved to disk


# ----------------------------------------------------------------------------- MetaApi client on the wire

async def test_metaapi_client_requests(aiohttp_server, monkeypatch):
    from aiohttp import web

    from goldbot import metaapi

    seen: list[tuple] = []

    async def handler(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        seen.append((request.method, request.path, dict(request.query), body, request.headers.get("auth-token")))
        path = request.path
        if path.endswith("/candles"):
            return web.json_response([
                {"time": "2026-11-16T12:05:00.000Z", "open": 2, "high": 3, "low": 1, "close": 2.5},
                {"time": "2026-11-16T12:00:00.000Z", "open": 1, "high": 2, "low": 0.5, "close": 2},
            ])
        if path.endswith("/current-price"):
            return web.json_response({"symbol": "XAUUSDm", "bid": 4000.1, "ask": 4000.4})
        if path.endswith("/trade"):
            if body.get("actionType") == "POSITION_MODIFY":
                return web.json_response({"numericCode": 10016, "stringCode": "TRADE_RETCODE_INVALID_STOPS", "message": "Invalid stops"})
            return web.json_response({"numericCode": 10009, "stringCode": "TRADE_RETCODE_DONE", "orderId": "5", "positionId": "5"})
        if path.endswith("/positions"):
            return web.json_response({"error": "TooManyRequestsError", "message": "slow down", "metadata": {"recommendedRetryTime": "2099-01-01T00:00:00.000Z"}}, status=429)
        if path == "/users/current/accounts":
            return web.json_response([{"_id": "abc", "login": "224085839", "state": "DEPLOYED", "connectionStatus": "CONNECTED", "region": "london"}])
        return web.json_response({"_id": "abc", "state": "DEPLOYED", "connectionStatus": "CONNECTED", "region": "new-york"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    base = f"http://{server.host}:{server.port}"
    monkeypatch.setattr(metaapi, "PROVISIONING", base)
    api = metaapi.MetaApi("secret-token", "abc")
    monkeypatch.setattr(api, "_trade_host", lambda: base)
    monkeypatch.setattr(api, "_data_host", lambda: base)
    try:
        assert (await api.accounts())[0]["login"] == "224085839"
        await api.account()
        assert api.region == "new-york"
        candles = await api.candles("XAUUSDm", "5m", limit=80)
        assert [c["t"] for c in candles] == [1794830400, 1794830700] and candles[-1]["c"] == 2.5      # oldest first, UTC
        assert await api.price("XAUUSDm") == {"bid": 4000.1, "ask": 4000.4}
        result = await api.market_order("XAUUSDm", -1, 0.01, 4006.4, "GB6H")
        assert result["positionId"] == "5"
        with pytest.raises(metaapi.TradeRejected):
            await api.modify_stop("5", 4001.0)
        with pytest.raises(metaapi.ApiError) as err:
            await api.positions()
        assert err.value.status == 429 and api.blocked_until > 0
        with pytest.raises(metaapi.ApiError):      # no request is sent while the limit resets
            await api.price("XAUUSDm")
    finally:
        await api.close()
    order = next(s for s in seen if s[1].endswith("/trade"))
    assert order[0] == "POST" and order[1] == "/users/current/accounts/abc/trade" and order[4] == "secret-token"
    assert order[3] == {"actionType": "ORDER_TYPE_SELL", "symbol": "XAUUSDm", "volume": 0.01, "stopLoss": 4006.4, "comment": "GB6H"}
    candle_call = next(s for s in seen if s[1].endswith("/candles"))
    assert candle_call[1] == "/users/current/accounts/abc/historical-market-data/symbols/XAUUSDm/timeframes/5m/candles"
    assert candle_call[2] == {"limit": "80"}
    assert sum(1 for s in seen if s[1].endswith("/current-price")) == 1
