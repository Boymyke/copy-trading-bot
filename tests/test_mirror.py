"""Tests against a fake MetaApi REST server and fake CopyFactory API.

Covers: open/close mirroring reports, out-of-sync alerts, 0.01-lot check,
429 back-off honouring MetaApi's retry time, CopyFactory adoption without
writes, independent-exit config warnings, minimal pause/resume writes, and
that nothing in the app can send a trade.
"""

import asyncio
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiohttp import web

from app import metaapi_rest
from app.config import load_settings
from app.controller import Controller
from app.copyfactory_service import CopyFactoryService
from app.notifier import Notifier
from app.storage import Store

SRC, TGT = "src-acc", "tgt-acc"


class FakeMetaApi:
    def __init__(self):
        self.positions = {SRC: [], TGT: []}
        self.status_override = {}
        self.requests = []
        self.deals = {}

    def app(self):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.handle)
        return app

    async def handle(self, request: web.Request):
        self.requests.append((request.method, request.path))
        if request.method != "GET":
            return web.json_response({"error": "write attempted"}, status=405)
        p = request.path
        for acc in (SRC, TGT):
            if p == f"/users/current/accounts/{acc}/positions":
                if acc in self.status_override:
                    status, body = self.status_override[acc]
                    return web.json_response(body, status=status)
                return web.json_response(self.positions[acc])
            if p == f"/users/current/accounts/{acc}":
                return web.json_response({"_id": acc, "name": acc, "state": "DEPLOYED", "connectionStatus": "CONNECTED",
                                          "copyFactoryRoles": ["PROVIDER" if acc == SRC else "SUBSCRIBER"]})
        if "/history-deals/position/" in p:
            return web.json_response(self.deals.get(p.rsplit("/", 1)[-1], []))
        return web.json_response({"error": "not found"}, status=404)


class FakeConfiguration:
    def __init__(self):
        self.strategy = {"_id": "STRAT1", "name": "Gold Source Strategy", "accountId": SRC, "platformCommissionRate": 0,
                         "symbolFilter": {"included": ["XAUUSD.f"]}, "symbolMapping": [{"from": "XAUUSD.f", "to": "XAUUSDm"}],
                         "copyStopLoss": False, "copyTakeProfit": False, "skipPendingOrders": True, "reverse": False,
                         "tradeSizeScaling": {"mode": "fixedVolume", "tradeVolume": 0.01}}
        self.subscriber = {"_id": TGT, "name": "Gold Target Subscriber",
                           "subscriptions": [{"strategyId": "STRAT1", "multiplier": 1.0, "customField": "keep-me"}]}
        self.writes = []

    async def get_strategies_with_infinite_scroll_pagination(self, options=None):
        return [{"_id": "OTHER", "name": "x", "accountId": "unrelated"}, copy.deepcopy(self.strategy)]

    async def get_strategy(self, strategy_id):
        if strategy_id != "STRAT1":
            raise RuntimeError("not found")
        return copy.deepcopy(self.strategy)

    async def get_subscriber(self, subscriber_id):
        return copy.deepcopy(self.subscriber)

    async def update_subscriber(self, subscriber_id, body):
        self.writes.append(("update_subscriber", subscriber_id, copy.deepcopy(body)))
        self.subscriber = {"_id": subscriber_id, **copy.deepcopy(body)}

    async def update_strategy(self, *a, **k):
        raise AssertionError("must never write strategies")

    async def generate_strategy_id(self):
        raise AssertionError("must never create strategies")


class FakeTrading:
    def __init__(self):
        self.user_log = []
        self.stopouts = []

    async def get_user_log(self, subscriber_id, start_time=None, limit=1000, **kw):
        return list(self.user_log)

    async def get_stopouts(self, subscriber_id):
        return list(self.stopouts)


class FakeHistory:
    async def get_subscription_transactions(self, since, till, subscriber_ids=None, limit=None):
        return [{"time": "t", "type": "DEAL_TYPE_BUY", "symbol": "XAUUSD.f", "positionId": "S1", "slavePositionId": "T1",
                 "strategy": {"name": "Gold Source Strategy"}}]


class FakeCopyFactory:
    def __init__(self):
        self.configuration_api = FakeConfiguration()
        self.trading_api = FakeTrading()
        self.history_api = FakeHistory()


def drain(notifier):
    out = []
    while not notifier.queue.empty():
        out.append(notifier.queue.get_nowait())
    return out


def pos(pid, symbol, side="BUY", volume=0.01, seconds_ago=0, profit=0.0):
    t = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    return {"id": pid, "type": f"POSITION_TYPE_{side}", "symbol": symbol, "volume": volume, "openPrice": 4100.0,
            "profit": profit, "time": t}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {
        "METAAPI_TOKEN": "x", "METAAPI_SOURCE_ACCOUNT_ID": SRC, "METAAPI_TARGET_ACCOUNT_ID": TGT,
        "DATA_DIR": str(tmp_path), "COPY_SYMBOL": "XAUUSD.f", "TARGET_SYMBOL": "XAUUSDm", "TELEGRAM_BOT_TOKEN": "",
        "CONTROLLER_POLL_SECONDS": "0.5",
    }.items():
        monkeypatch.setenv(k, v)
    return tmp_path


async def _setup(monkeypatch, aiohttp_server):
    fake = FakeMetaApi()
    server = await aiohttp_server(fake.app())
    base = str(server.make_url("")).rstrip("/")
    monkeypatch.setenv("METAAPI_REST_BASE_URL", base)
    monkeypatch.setattr(metaapi_rest, "PROVISIONING_BASE_URL", base)
    settings = load_settings()
    store = Store(settings.data_dir)
    notifier = Notifier()
    cf_api = FakeCopyFactory()
    rest = metaapi_rest.MetaApiRestClient(settings.metaapi_token, settings.metaapi_rest_base_url, 5)
    cf = CopyFactoryService(settings, store, rest, copyfactory=cf_api)
    controller = Controller(settings, store, notifier, rest=rest, copyfactory=cf)
    return fake, cf_api, controller, notifier, store


def test_old_fast_poll_setting_is_ignored(env):
    assert load_settings().monitor_poll_seconds == 60.0


async def test_mirror_lifecycle_reporting(env, monkeypatch, aiohttp_server):
    fake, _, controller, notifier, _ = await _setup(monkeypatch, aiohttp_server)
    mon = controller.monitor

    await mon.cycle()  # first poll adopts current state silently
    assert drain(notifier) == []

    # source opens, CopyFactory has already copied it 1.2s later; unrelated target trade ignored
    fake.positions[SRC] = [pos("S1", "XAUUSD.f", volume=0.5, seconds_ago=3)]
    fake.positions[TGT] = [pos("T1", "XAUUSDm", seconds_ago=1.8), pos("X9", "EURUSDm", volume=1.0)]
    await mon.cycle()
    msgs = drain(notifier)
    assert any("Source opened BUY XAUUSD.f 0.5" in m for m in msgs)
    assert any("Target trade opened by CopyFactory" in m and "Copied ~1.2s after source" in m for m in msgs)
    assert mon.state()["inSync"] and mon.state()["otherTargetPositions"] == 1

    # both close; final P/L comes from deal history
    fake.positions = {SRC: [], TGT: [pos("X9", "EURUSDm", volume=1.0)]}
    fake.deals["T1"] = [{"entryType": "DEAL_ENTRY_IN", "profit": 0, "commission": -0.03},
                        {"entryType": "DEAL_ENTRY_OUT", "profit": 1.25}]
    await mon.cycle()
    await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name().startswith("target-close-")])
    msgs = drain(notifier)
    assert any("Source closed" in m for m in msgs)
    assert any("Target trade closed by CopyFactory" in m and "+$1.22" in m for m in msgs)
    assert {m for m, _ in fake.requests} == {"GET"}


async def test_out_of_sync_alert_after_two_polls_and_recovery(env, monkeypatch, aiohttp_server):
    fake, _, controller, notifier, _ = await _setup(monkeypatch, aiohttp_server)
    mon = controller.monitor
    await mon.cycle()
    fake.positions[SRC] = [pos("S1", "XAUUSD.f")]
    await mon.cycle()  # 1st poll: copy may still be in flight -> no alert
    assert not any("out of sync" in m for m in drain(notifier))
    await mon.cycle()
    assert any("out of sync" in m for m in drain(notifier))
    fake.positions[TGT] = [pos("T1", "XAUUSDm", volume=0.02)]
    await mon.cycle()
    msgs = drain(notifier)
    assert any("back in sync" in m for m in msgs)
    assert any("0.02 lot (expected 0.01)" in m for m in msgs)


async def test_429_waits_for_recommended_retry_time(env, monkeypatch, aiohttp_server):
    fake, _, controller, notifier, _ = await _setup(monkeypatch, aiohttp_server)
    mon = controller.monitor
    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=900)).isoformat()
    fake.status_override[SRC] = (429, {"error": "TooManyRequestsError", "message": "credits", "metadata": {"recommendedRetryTime": retry_at}})
    with pytest.raises(metaapi_rest.MetaApiRestError) as err:
        await mon.cycle()
    delay = mon._on_failure(err.value)
    assert 880 <= delay <= 900
    assert mon.state()["rateLimitedUntil"] > time.time() + 800
    calls_before = len(fake.requests)
    assert calls_before == 1  # gave up immediately, no retry storm


async def test_copyfactory_active_and_adopted_without_writes(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, _, _ = await _setup(monkeypatch, aiohttp_server)
    status = await controller.copyfactory.refresh()
    assert status["active"] and status["strategyId"] == "STRAT1" and status["warnings"] == []
    assert cf_api.configuration_api.writes == []


async def test_independent_exit_settings_are_flagged(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, _, _ = await _setup(monkeypatch, aiohttp_server)
    cf_api.configuration_api.strategy["timeSettings"] = {"lifetimeInHours": 1}
    cf_api.configuration_api.strategy["maxStopLoss"] = {"value": 1, "units": "relative-price"}
    cf_api.configuration_api.subscriber["subscriptions"][0]["closeOnly"] = "by-position"
    cf_api.trading_api.stopouts = [{"reason": "day-balance-difference", "reasonDescription": "limit", "stoppedTill": "x"}]
    status = await controller.copyfactory.refresh()
    text = " | ".join(status["warnings"])
    assert "lifetimeInHours=1" in text and "maxStopLoss" in text and "PAUSED" in text and "stop-out" in text
    assert not status["active"]


async def test_pause_resume_minimal_write(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, _, store = await _setup(monkeypatch, aiohttp_server)
    await controller.copyfactory.refresh()
    controller.copyfactory_ready = True
    await controller.set_pause(True)
    (_, sub_id, body), = cf_api.configuration_api.writes
    assert sub_id == TGT and "_id" not in body and body["name"] == "Gold Target Subscriber"
    assert body["subscriptions"] == [{"strategyId": "STRAT1", "multiplier": 1.0, "customField": "keep-me", "closeOnly": "by-position"}]
    await controller.set_pause(False)
    assert "closeOnly" not in cf_api.configuration_api.writes[-1][2]["subscriptions"][0]
    assert store.get("copy_paused") == "0"


async def test_copyfactory_errors_from_user_log_are_reported(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, notifier, _ = await _setup(monkeypatch, aiohttp_server)
    cf_api.trading_api.user_log = [
        {"time": "t2", "level": "ERROR", "message": "Symbol XAUUSDm not found", "positionId": "1"},
        {"time": "t1", "level": "INFO", "message": "Opened position", "positionId": "1"},
    ]
    await controller.monitor.cycle()  # first read is a silent 24h backfill
    assert not [m for m in drain(notifier) if "CopyFactory ERROR" in m]
    cf_api.trading_api.user_log.insert(0, {"time": "t3", "level": "ERROR", "message": "Not enough money", "positionId": "2"})
    await controller.monitor.cycle()
    await controller.monitor.cycle()  # same records again -> not re-reported
    msgs = drain(notifier)
    assert [m for m in msgs if "CopyFactory ERROR" in m] == ["🔴 CopyFactory ERROR: Not enough money"]


async def test_copy_history_is_read(env, monkeypatch, aiohttp_server):
    _, _, controller, _, _ = await _setup(monkeypatch, aiohttp_server)
    history = await controller.copyfactory.recent_copy_history()
    assert history["count"] == 1 and history["latest"][0]["targetPositionId"] == "T1"


def test_no_trade_code_exists():
    src = "\n".join(p.read_text() for p in Path("app").glob("*.py"))
    for forbidden in ("modify_position", "create_market", "close_position", "/trade", "session.post(url, json",
                      "session.put", "session.delete", "update_strategy(", "generate_strategy_id", "remove_subscri"):
        assert forbidden not in src, forbidden
    assert "from metaapi_cloud_sdk" not in src and "get_rpc_connection" not in src and "get_streaming_connection" not in src
