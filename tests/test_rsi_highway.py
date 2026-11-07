from datetime import date, timedelta

from strategies.rsi_highway import Action, Candle, State, bullish_pattern, evaluate, rsi


def candles(closes, *, start=date(2025, 1, 1)):
    return [Candle(start + timedelta(days=i), value - 0.4, value + 1, value - 1, value) for i, value in enumerate(closes)]


def test_wilder_rsi_strength_and_flat_series():
    assert rsi(range(1, 17))[-1] == 100
    assert rsi([10] * 16)[-1] == 50


def test_price_action_patterns_are_explicit():
    previous = Candle(date(2026, 1, 1), 102, 103, 98, 99)
    engulfing = Candle(date(2026, 1, 2), 98, 104, 97, 103)
    hammer = Candle(date(2026, 1, 3), 100, 102, 94, 102)
    breakout = Candle(date(2026, 1, 4), 102, 106, 101, 105)
    assert bullish_pattern(previous, engulfing) == "bullish_engulfing"
    assert bullish_pattern(engulfing, hammer) == "hammer"
    assert bullish_pattern(hammer, breakout) == "breakout"


def test_entry_requires_top_down_strength_breadth_rsi_trigger_and_pattern(monkeypatch):
    monthly = candles(range(100, 116))
    weekly = candles(range(200, 216))
    daily = candles([100 + i for i in range(14)] + [113, 112, 114])
    def values(closes, period=14):
        ending = [49, 53] if len(closes) == len(daily) else [70, 72]
        return [None] * (len(closes) - 2) + ending
    monkeypatch.setattr("strategies.rsi_highway.rsi", values)

    blocked, _ = evaluate(monthly, weekly, daily, breadth_healthy=False)
    decision, state = evaluate(monthly, weekly, daily, breadth_healthy=True)

    assert blocked.reason == "market_breadth_unhealthy"
    assert decision.action == Action.ENTER
    assert decision.reason == "rsi_50_cross"
    assert decision.pattern == "breakout"
    assert state == State(True, 1, state.stop, daily[-1].day)


def test_position_exits_on_daily_rsi_close_below_50(monkeypatch):
    history = candles(range(100, 117))
    monkeypatch.setattr("strategies.rsi_highway.rsi", lambda values, period=14: [None] * (len(values) - 2) + [52, 49])
    decision, state = evaluate(history, history, history, breadth_healthy=True,
                               state=State(True, 2, 90, history[-2].day))
    assert decision.action == Action.EXIT
    assert decision.reason == "daily_rsi_below_50"
    assert state == State()


def test_pyramiding_is_capped_at_three(monkeypatch):
    trend = candles(range(100, 116))
    daily = candles([100 + i for i in range(14)] + [113, 112, 114])
    def values(closes, period=14):
        ending = [49, 53] if len(closes) == len(daily) else [70, 72]
        return [None] * (len(closes) - 2) + ending
    monkeypatch.setattr("strategies.rsi_highway.rsi", values)
    existing = State(True, 3, 90, daily[-2].day)
    decision, state = evaluate(trend, trend, daily, breadth_healthy=True, state=existing)
    assert decision.action == Action.HOLD
    assert state.entries == 3


def test_entry_blocks_when_completed_weekly_rsi_is_falling(monkeypatch):
    monthly = candles(range(100, 116))
    weekly = candles(range(200, 216))
    daily = candles([100 + i for i in range(14)] + [113, 112, 114])

    def values(closes, period=14):
        if len(closes) == len(daily):
            return [None] * (len(closes) - 2) + [49, 53]
        if closes[0] == weekly[0].close:
            return [None] * (len(closes) - 2) + [72, 70]
        return [None] * (len(closes) - 2) + [70, 72]

    monkeypatch.setattr("strategies.rsi_highway.rsi", values)
    decision, state = evaluate(monthly, weekly, daily, breadth_healthy=True)
    assert decision.action == Action.NONE
    assert decision.reason == "weekly_rsi_not_rising"
    assert state == State()
