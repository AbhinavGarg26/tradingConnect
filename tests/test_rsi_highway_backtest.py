from datetime import date, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backtest.rsi_highway import IST, aggregate, breadth_by_day, load_daily_candles
from strategies.rsi_highway import Candle


def candle(day, close):
    return Candle(day, close - 1, close + 1, close - 2, close)


def test_period_aggregation_preserves_ohlc():
    start = date(2026, 1, 1)
    rows = [candle(start + timedelta(days=i), 100 + i) for i in range(3)]
    result = aggregate(rows, "month")
    assert len(result) == 1
    assert (result[0].open, result[0].high, result[0].low, result[0].close) == (99, 103, 98, 102)


def test_breadth_uses_only_trailing_history_through_each_day():
    start = date(2026, 1, 1)
    data = {
        "A": [candle(start + timedelta(days=i), 100 + i) for i in range(3)],
        "B": [candle(start + timedelta(days=i), 100 - i) for i in range(3)],
    }
    result = breadth_by_day(data, minimum_history=2)
    assert result[start + timedelta(days=1)] == .5
    assert result[start + timedelta(days=2)] == .5


def test_daily_loader_saves_missing_candles_and_reuses_database():
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.execute(text("""CREATE TABLE market_snapshots (
            symbol TEXT, timeframe TEXT, captured_at TIMESTAMP, ltp NUMERIC,
            open_price NUMERIC, high_price NUMERIC, low_price NUMERIC, close_price NUMERIC,
            created_at TIMESTAMP, updated_at TIMESTAMP,
            UNIQUE(symbol, timeframe, captured_at))"""))
        db.execute(text("CREATE TABLE trading_holidays (holiday_date DATE)"))
        db.commit()

    class Kite:
        calls = 0

        def instruments(self, exchange):
            return [{"tradingsymbol": "TEST", "instrument_token": 1}]

        def historical_data(self, token, start, end, interval):
            self.calls += 1
            return [{"date": datetime(2026, 11, 2, 15, 30, tzinfo=IST),
                     "open": 100, "high": 105, "low": 99, "close": 104}]

    kite = Kite()
    kwargs = dict(session_factory=factory, kite_factory=lambda: kite,
                  now=datetime(2026, 11, 3, 10, tzinfo=IST))
    first = load_daily_candles(("TEST",), date(2026, 11, 2), date(2026, 11, 2), **kwargs)
    second = load_daily_candles(("TEST",), date(2026, 11, 2), date(2026, 11, 2), **kwargs)
    assert first == second
    assert first["TEST"][0].close == 104
    assert kite.calls == 1
    with factory() as db:
        assert db.execute(text("SELECT COUNT(*) FROM market_snapshots")).scalar() == 1
