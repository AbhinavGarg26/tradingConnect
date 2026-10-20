from datetime import date

from market.trading_calendar import is_nse_trading_day


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeDb:
    def __init__(self, holiday_dates):
        self.holiday_dates = holiday_dates
        self.queries = []

    def execute(self, statement, params):
        self.queries.append((statement, params))
        return FakeResult(1 if params["day"] in self.holiday_dates else None)


def test_nse_holiday_is_not_a_trading_day():
    db = FakeDb({date(2026, 10, 20)})
    assert not is_nse_trading_day(date(2026, 10, 20), db)
    assert db.queries[0][1]["day"] == date(2026, 10, 20)


def test_normal_weekday_is_a_trading_day():
    assert is_nse_trading_day(date(2026, 10, 21), FakeDb({date(2026, 10, 20)}))


def test_weekend_skips_database():
    db = FakeDb(set())
    assert not is_nse_trading_day(date(2026, 10, 24), db)
    assert db.queries == []
