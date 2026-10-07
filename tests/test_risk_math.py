import pytest

from app import risk_math as rm

# Typical Exness XAUUSDm: 3 digits, tick 0.001, contract 100 oz -> $0.1 per tick per 1.0 lot.
GOLD = rm.SymbolRules.from_spec({"symbol": "XAUUSDm", "digits": 3, "tickSize": 0.001, "point": 0.001, "stopsLevel": 0, "freezeLevel": 0})
TV = 0.1


def test_spec_parsing_defaults():
    rules = rm.SymbolRules.from_spec({"symbol": "X", "digits": 2})
    assert rules.point == pytest.approx(0.01)
    assert rules.tick_size == pytest.approx(0.01)
    with pytest.raises(ValueError):
        rm.SymbolRules.from_spec({"symbol": "X", "digits": 2, "tickSize": -1, "point": -1})


def test_money_distance_round_trip():
    d = rm.money_to_distance(0.60, 0.01, TV, 0.001)
    assert d == pytest.approx(0.60)
    assert rm.distance_to_money(d, 0.01, TV, 0.001) == pytest.approx(0.60)


@pytest.mark.parametrize("side,entry,expected", [("BUY", 2650.000, 2649.400), ("SELL", 2650.000, 2650.600)])
def test_initial_sl_is_60_cents_per_001(side, entry, expected):
    plan = rm.plan_initial_sl(side=side, open_price=entry, volume=0.01, risk_per_base_lot=0.60, rules=GOLD,
                              tick_value_loss=TV, bid=entry - 0.1, ask=entry + 0.1)
    assert plan.stop_loss == pytest.approx(expected)
    assert plan.money_at_sl == pytest.approx(-0.60)
    assert plan.notes == []


def test_initial_sl_scales_with_volume_but_same_distance():
    plan = rm.plan_initial_sl(side="BUY", open_price=2650.0, volume=0.02, risk_per_base_lot=0.60, rules=GOLD,
                              tick_value_loss=TV, bid=2650.0, ask=2650.2)
    assert plan.stop_loss == pytest.approx(2649.400)
    assert plan.money_at_sl == pytest.approx(-1.20)


def test_initial_sl_rounds_toward_entry_never_exceeding_risk():
    coarse = rm.SymbolRules.from_spec({"symbol": "X", "digits": 2, "tickSize": 0.05, "point": 0.01})
    plan = rm.plan_initial_sl(side="BUY", open_price=100.00, volume=0.01, risk_per_base_lot=0.62, rules=coarse,
                              tick_value_loss=5.0, bid=100.0, ask=100.1)
    # raw SL 99.38 -> rounded up to 99.40 (closer), risk 0.60 <= 0.62
    assert plan.stop_loss == pytest.approx(99.40)
    assert abs(plan.money_at_sl) <= 0.62 + 1e-9


def test_initial_sl_respects_stops_level():
    rules = rm.SymbolRules.from_spec({"symbol": "X", "digits": 3, "tickSize": 0.001, "point": 0.001, "stopsLevel": 1000})
    # min distance 1.000 from bid; price fell to 2649.9 so 2649.400 is too close
    plan = rm.plan_initial_sl(side="BUY", open_price=2650.0, volume=0.01, risk_per_base_lot=0.60, rules=rules,
                              tick_value_loss=TV, bid=2649.9, ask=2650.1)
    assert plan.stop_loss == pytest.approx(2648.900)
    assert any("stops_level" in n for n in plan.notes)


def _trail(side, sl, bid, ask, rules=GOLD):
    return rm.plan_trailing(side=side, open_price=2650.0, volume=0.01, current_sl=sl, rules=rules,
                            tick_value_profit=TV, bid=bid, ask=ask, trigger_per_base_lot=0.50,
                            gap_per_base_lot=0.20, step_per_base_lot=0.10)


def test_no_trail_below_trigger():
    plan = _trail("BUY", 2649.4, bid=2650.49, ask=2650.69)
    assert not plan.move and plan.reason == "below_trigger"


def test_buy_trail_starts_at_trigger_with_gap():
    plan = _trail("BUY", 2649.4, bid=2650.50, ask=2650.70)
    assert plan.move
    assert plan.stop_loss == pytest.approx(2650.300)
    assert plan.locked_money == pytest.approx(0.30)


def test_sell_trail_mirror():
    plan = _trail("SELL", 2650.6, bid=2649.30, ask=2649.50)
    assert plan.move
    assert plan.stop_loss == pytest.approx(2649.700)
    assert plan.locked_money == pytest.approx(0.30)


def test_step_required_before_next_move():
    # SL already locks 0.30; profit 0.55 -> wants lock 0.35, only +0.05 < step 0.10
    plan = _trail("BUY", 2650.300, bid=2650.55, ask=2650.75)
    assert not plan.move and plan.reason == "below_step"
    plan = _trail("BUY", 2650.300, bid=2650.60, ask=2650.80)
    assert plan.move and plan.stop_loss == pytest.approx(2650.400)


def test_never_widens():
    # Price retraced: candidate lock is lower than existing SL -> no move
    plan = _trail("BUY", 2650.800, bid=2650.70, ask=2650.90)
    assert not plan.move and plan.reason == "would_not_tighten"
    assert rm.tighter("BUY", 2650.8, 2650.5) == 2650.8
    assert rm.tighter("SELL", 2649.2, 2649.5) == 2649.2


def test_freeze_level_blocks_modification():
    rules = rm.SymbolRules.from_spec({"symbol": "X", "digits": 3, "tickSize": 0.001, "point": 0.001, "freezeLevel": 500})
    plan = _trail("BUY", 2650.400, bid=2650.70, ask=2650.90, rules=rules)
    assert not plan.move and plan.reason == "inside_freeze_level"


def test_sl_breach_detection():
    assert rm.sl_breached("BUY", 2649.4, bid=2649.4, ask=2649.6)
    assert not rm.sl_breached("BUY", 2649.4, bid=2649.5, ask=2649.7)
    assert rm.sl_breached("SELL", 2650.6, bid=2650.4, ask=2650.6)
    assert not rm.sl_breached("SELL", None, bid=1, ask=1)
