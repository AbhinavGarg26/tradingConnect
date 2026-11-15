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


def test_daily_loader_can_skip_symbol_absent_from_kite_catalog():
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
        def instruments(self, exchange):
            return []

    result = load_daily_candles(
        ("MISSING",), date(2026, 11, 2), date(2026, 11, 2),
        session_factory=factory, kite_factory=Kite,
        now=datetime(2026, 11, 3, 10, tzinfo=IST),
        skip_missing_instruments=True,
    )
    assert result == {"MISSING": []}


def test_database_only_returns_available_rows_without_calling_kite():
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.execute(text("""CREATE TABLE market_snapshots (
            symbol TEXT, timeframe TEXT, captured_at TIMESTAMP, ltp NUMERIC,
            open_price NUMERIC, high_price NUMERIC, low_price NUMERIC, close_price NUMERIC,
            created_at TIMESTAMP, updated_at TIMESTAMP,
            UNIQUE(symbol, timeframe, captured_at))"""))
        db.execute(text("CREATE TABLE trading_holidays (holiday_date DATE)"))
        db.execute(text("""INSERT INTO market_snapshots
            (symbol, timeframe, captured_at, open_price, high_price, low_price, close_price)
            VALUES ('TEST', '1d', '2026-11-02 10:00:00', 100, 105, 99, 104)"""))
        db.commit()

    result = load_daily_candles(
        ("TEST",), date(2026, 11, 2), date(2026, 11, 3),
        session_factory=factory, now=datetime(2026, 11, 4, 10, tzinfo=IST),
        database_only=True,
    )
    assert [row.close for row in result["TEST"]] == [104]
