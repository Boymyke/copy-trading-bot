"""End-to-end dry-run test against a fake MetaApi REST server and fake CopyFactory API.

Proves: REST-only reads, managed vs unrelated classification, simulated SL and
trailing, never-widen, simulated SL hit, close detection with final P/L,
failure reporting/recovery, CopyFactory adoption without writes, and that no
non-GET request ever reaches the trading API.
"""

import asyncio
import copy
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
        self.positions = []
        self.bid, self.ask = 2650.00, 2650.20
        self.fail_positions = 0
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
        if p.endswith("/positions"):
            if self.fail_positions:
                self.fail_positions -= 1
                return web.json_response({"error": "TimeoutError", "message": "account not connected to broker"}, status=504)
            return web.json_response(self.positions)
        if p.endswith("/specification"):
            return web.json_response({"symbol": "XAUUSDm", "digits": 3, "tickSize": 0.001, "point": 0.001,
                                      "stopsLevel": 0, "freezeLevel": 0, "contractSize": 100})
        if p.endswith("/current-price"):
            return web.json_response({"symbol": "XAUUSDm", "bid": self.bid, "ask": self.ask,
                                      "lossTickValue": 0.1, "profitTickValue": 0.1})
        if p.endswith("/account-information"):
            return web.json_response({"broker": "Exness", "currency": "USD", "balance": 100, "equity": 100})
        if "/history-deals/position/" in p:
            return web.json_response(self.deals.get(p.rsplit("/", 1)[-1], []))
        if p in (f"/users/current/accounts/{SRC}", f"/users/current/accounts/{TGT}"):
            acc = p.rsplit("/", 1)[-1]
            return web.json_response({"_id": acc, "name": acc, "state": "DEPLOYED", "connectionStatus": "CONNECTED",
                                      "region": "london", "copyFactoryRoles": ["PROVIDER" if acc == SRC else "SUBSCRIBER"]})
        return web.json_response({"error": "not found"}, status=404)


class FakeConfiguration:
    def __init__(self):
        self.strategy = {"_id": "STRAT1", "name": "Gold Source Strategy", "accountId": SRC, "platformCommissionRate": 0,
                         "symbolFilter": {"included": ["XAUUSD.f"]}, "symbolMapping": [{"from": "XAUUSD.f", "to": "XAUUSDm"}],
                         "copyStopLoss": False, "copyTakeProfit": False, "skipPendingOrders": True, "reverse": False,
                         "tradeSizeScaling": {"mode": "fixedVolume", "tradeVolume": 0.01},
                         "timeSettings": {"lifetimeInHours": 240}}
        self.other = {"_id": "OTHER", "name": "Something else", "accountId": "unrelated"}
        self.subscriber = {"_id": TGT, "name": "My Target", "maxTradeRisk": 0.5,
                           "subscriptions": [{"strategyId": "STRAT1", "multiplier": 1.0, "customField": "keep-me"}]}
        self.writes = []

    async def get_strategies_with_infinite_scroll_pagination(self, options=None):
        return [copy.deepcopy(self.other), copy.deepcopy(self.strategy)]

    async def get_strategy(self, strategy_id):
        if strategy_id != "STRAT1":
            raise RuntimeError("not found")
        return copy.deepcopy(self.strategy)

    async def get_subscriber(self, subscriber_id):
        assert subscriber_id == TGT
        return copy.deepcopy(self.subscriber)

    async def update_subscriber(self, subscriber_id, body):
        self.writes.append(("update_subscriber", subscriber_id, copy.deepcopy(body)))
        self.subscriber = {"_id": subscriber_id, **copy.deepcopy(body)}

    async def update_strategy(self, strategy_id, body):
        self.writes.append(("update_strategy", strategy_id, copy.deepcopy(body)))

    async def generate_strategy_id(self):  # must never be called
        raise AssertionError("must never create strategies")


class FakeCopyFactory:
    def __init__(self):
        self.configuration_api = FakeConfiguration()


def drain(notifier):
    out = []
    while not notifier.queue.empty():
        out.append(notifier.queue.get_nowait())
    return out


def position(pid, *, symbol="XAUUSDm", volume=0.01, reason="POSITION_REASON_EXPERT", profit=0.0):
    return {"id": pid, "type": "POSITION_TYPE_BUY", "symbol": symbol, "volume": volume, "openPrice": 2650.0,
            "profit": profit, "currentTickValue": 0.1, "reason": reason, "magic": 0, "time": "2026-10-07T12:00:00Z"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in {
        "METAAPI_TOKEN": "x", "METAAPI_SOURCE_ACCOUNT_ID": SRC, "METAAPI_TARGET_ACCOUNT_ID": TGT,
        "DATA_DIR": str(tmp_path), "COPY_SYMBOL": "XAUUSD.f", "TARGET_SYMBOL": "XAUUSDm",
        "TELEGRAM_PNL_UPDATE_SECONDS": "0", "CLOSE_CONFIRM_POLLS": "2", "TELEGRAM_BOT_TOKEN": "",
    }.items():
        monkeypatch.setenv(k, v)
    return tmp_path


async def _setup(env, monkeypatch, aiohttp_server):
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


async def test_full_dry_run_lifecycle(env, monkeypatch, aiohttp_server):
    fake, cf_api, controller, notifier, store = await _setup(env, monkeypatch, aiohttp_server)
    risk = controller.risk
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep(asyncio.sleep))

    # CopyFactory: adopt existing strategy + subscriber, read-only
    status = await controller.copyfactory.refresh()
    assert status["strategyId"] == "STRAT1" and status["paused"] is False
    assert status["warnings"] == []
    assert cf_api.configuration_api.writes == []

    # Flat account: spec + price readable
    await risk.cycle(); risk._on_success()
    assert risk.rules.digits == 3 and risk.price["bid"] == 2650.0
    assert risk.state()["online"]

    # A copied trade, a manual trade and another symbol appear
    fake.positions = [position("111"), position("222", reason="POSITION_REASON_MOBILE"), position("333", symbol="EURUSDm")]
    await risk.cycle()
    msgs = drain(notifier)
    assert len([m for m in msgs if "target trade detected" in m]) == 1
    assert "Simulated initial SL: 2649.400" in msgs[0] and "Entry: 2650.0" in msgs[0]
    assert [r["position_id"] for r in store.open_tracked()] == ["111"]
    assert {i["id"] for i in risk.state()["ignoredPositions"]} == {"222", "333"}

    # Price reaches +$0.50 -> trailing locks $0.30
    fake.bid, fake.ask = 2650.50, 2650.70
    await risk.cycle()
    assert "2649.400 → 2650.300" in drain(notifier)[0]
    # +$0.55: below step, no move. +$0.65: lock 0.45
    fake.bid = 2650.55; await risk.cycle(); assert drain(notifier) == []
    fake.bid = 2650.65; await risk.cycle(); assert "2650.300 → 2650.450" in drain(notifier)[0]
    # Retrace: never widens, and the simulated SL would have been hit
    fake.bid = 2650.40; await risk.cycle()
    msgs = drain(notifier)
    assert len(msgs) == 1 and "simulated SL would have been hit" in msgs[0]
    assert store.get_tracked("111")["simulated_sl"] == pytest.approx(2650.45)

    # Position closes: needs 2 missing polls, then final P/L from deal history
    fake.positions = [p for p in fake.positions if p["id"] != "111"]
    fake.deals["111"] = [{"entryType": "DEAL_ENTRY_IN", "profit": 0, "commission": -0.02, "swap": 0},
                         {"entryType": "DEAL_ENTRY_OUT", "profit": 0.40, "commission": 0, "swap": 0}]
    await risk.cycle(); assert store.get_tracked("111")["status"] == "open"
    await risk.cycle(); await asyncio.gather(*[t for t in asyncio.all_tasks() if t.get_name().startswith("finalize-")])
    closed = store.get_tracked("111")
    assert closed["status"] == "closed" and closed["final_profit"] == pytest.approx(0.38)
    assert "Final P/L: +$0.38" in drain(notifier)[-1]

    # The trading API only ever saw GETs
    assert {m for m, _ in fake.requests} == {"GET"}


async def test_rest_failures_are_reported_and_recover(env, monkeypatch, aiohttp_server):
    fake, _, controller, notifier, _ = await _setup(env, monkeypatch, aiohttp_server)
    risk = controller.risk
    fake.fail_positions = 3
    for _ in range(3):
        with pytest.raises(metaapi_rest.MetaApiRestError) as err:
            await risk.cycle()
        risk._on_failure(err.value)
    msgs = drain(notifier)
    assert len(msgs) == 1 and "Target REST/API failure" in msgs[0] and "504" in msgs[0]
    assert not risk.state()["online"]
    await risk.cycle(); risk._on_success()
    assert "recovered" in drain(notifier)[0]
    assert risk.state()["online"]


async def test_pause_resume_is_minimal_read_modify_write(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, _, store = await _setup(env, monkeypatch, aiohttp_server)
    await controller.copyfactory.refresh()
    controller.copyfactory_ready = True
    await controller.set_pause(True)
    writes = cf_api.configuration_api.writes
    assert len(writes) == 1
    _, sub_id, body = writes[0]
    assert sub_id == TGT and "_id" not in body
    assert body["name"] == "My Target" and body["maxTradeRisk"] == 0.5
    assert body["subscriptions"] == [{"strategyId": "STRAT1", "multiplier": 1.0, "customField": "keep-me", "closeOnly": "by-position"}]
    assert store.get("copy_paused") == "1"
    await controller.set_pause(False)
    assert cf_api.configuration_api.writes[-1][2]["subscriptions"][0].get("closeOnly") is None
    assert store.get("copy_paused") == "0"


async def test_set_lot_changes_only_trade_volume_on_strategy(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, _, _ = await _setup(env, monkeypatch, aiohttp_server)
    await controller.copyfactory.refresh()
    controller.copyfactory_ready = True
    where = await controller.set_lot(0.02)
    assert where == "strategy"
    (_, sid, body), = cf_api.configuration_api.writes
    expected = copy.deepcopy(cf_api.configuration_api.strategy)
    for k in ("_id", "platformCommissionRate"):
        expected.pop(k)
    expected["tradeSizeScaling"]["tradeVolume"] = 0.02
    assert sid == "STRAT1" and body == expected


async def test_missing_strategy_is_never_created(env, monkeypatch, aiohttp_server):
    _, cf_api, controller, _, _ = await _setup(env, monkeypatch, aiohttp_server)
    cf_api.configuration_api.strategy["accountId"] = "someone-else"
    with pytest.raises(Exception, match="never creates"):
        await controller.copyfactory.refresh()
    assert cf_api.configuration_api.writes == []


def test_no_trade_write_code_exists():
    """Static guard: nothing in app/ can send a trade or position modification."""
    src = "\n".join(p.read_text() for p in Path("app").glob("*.py"))
    for forbidden in ("modify_position", "create_market", "close_position", "/trade", "session.post(url, json", "session.put", "session.delete"):
        assert forbidden not in src, forbidden
    # no RPC/WebSocket SDK import at all
    assert "from metaapi_cloud_sdk" not in src and "import metaapi_cloud_sdk" not in src
    assert "get_rpc_connection" not in src and "get_streaming_connection" not in src


def _fast_sleep(real_sleep):
    async def sleep(delay, *a, **kw):
        return await real_sleep(0)
    return sleep
