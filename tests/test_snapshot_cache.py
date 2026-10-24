import unittest
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, text, event
from backtest.snapshot_cache import load_snapshot_candles
from strategies.nifty_30m_reference import IST
from backtest.session_calendar import REGULAR_SESSION_HOLIDAYS, regular_session_closed

DAY = date(2026, 10, 23)
OPEN = datetime(2026, 10, 23, 9, 15, tzinfo=IST)


class SnapshotCacheTest(unittest.TestCase):
    def test_database_only_missing_never_fetches_or_writes(self):
        with self.assertRaisesRegex(ValueError, 'Database-only'):
            self.load(database_only=True)
        self.factory.assert_not_called()
        count = self.connection.execute(text('SELECT COUNT(*) FROM market_snapshots')).scalar()
        self.assertEqual(0, count)

    def test_database_only_complete_cache(self):
        expected = self.load()
        self.factory.reset_mock()
        self.assertEqual(expected, self.load(database_only=True))
        self.factory.assert_not_called()

    def test_database_only_explicit_skip(self):
        excluded = []
        self.assertEqual([], self.load(database_only=True, skip_incomplete_days=True,
                                       excluded_days=excluded))
        self.assertEqual([DAY], excluded)
        self.factory.assert_not_called()

    def test_historical_calendars_and_amended_2023_holiday(self):
        expected_counts = {2020: 12, 2021: 14, 2022: 13, 2023: 15, 2024: 17, 2025: 14, 2026: 16}
        for year, count in expected_counts.items():
            dates = [d for d in REGULAR_SESSION_HOLIDAYS if d.year == year]
            self.assertEqual(count, len(dates), year)
            self.assertTrue(all(regular_session_closed(d) for d in dates))
        self.assertFalse(regular_session_closed(date(2023, 6, 28)))
        self.assertTrue(regular_session_closed(date(2023, 6, 29)))
        # Settlement/bank-only holidays must remain trading days.
        self.assertFalse(regular_session_closed(date(2022, 4, 1)))
        self.assertFalse(regular_session_closed(date(2021, 8, 20)))

    def test_old_holidays_skip_without_fetching(self):
        for day in (date(2020, 2, 21), date(2021, 7, 21), date(2022, 8, 9), date(2023, 6, 29)):
            rows = load_snapshot_candles(day, day, now=OPEN,
                session_factory=self.session, trading_day=lambda day, db: True,
                kite_factory=self.factory)
            self.assertEqual([], rows)
        self.factory.assert_not_called()

    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.connection = self.engine.connect()
        self.connection.execute(text('''CREATE TABLE market_snapshots (
            symbol TEXT, timeframe TEXT, captured_at TIMESTAMP, ltp NUMERIC,
            open_price NUMERIC, high_price NUMERIC, low_price NUMERIC,
            close_price NUMERIC, created_at TIMESTAMP, updated_at TIMESTAMP,
            UNIQUE(symbol, timeframe, captured_at))'''))
        self.kite = Mock()
        self.kite.instruments.return_value = [{'tradingsymbol': 'NIFTY 50', 'instrument_token': 123}]
        self.kite.historical_data.side_effect = self.history
        self.factory = Mock(return_value=self.kite)

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    @contextmanager
    def session(self):
        yield self.connection

    def history(self, token, start, end, interval):
        return [dict(date=start + timedelta(minutes=i), open=100, high=102, low=99, close=101)
                for i in range(int((end-start).total_seconds()/60)+1)]

    def load(self, **kwargs):
        return load_snapshot_candles(DAY, DAY, now=kwargs.pop('now', OPEN + timedelta(hours=7)),
                                     session_factory=self.session, trading_day=lambda day, db: True,
                                     kite_factory=self.factory, **kwargs)

    def test_empty_cache_saved_and_second_run_needs_no_kite(self):
        rows = self.load()
        self.assertEqual(375, len(rows))
        self.assertEqual(OPEN.isoformat(), rows[0]['date'])
        stamp = self.connection.execute(text('SELECT MIN(captured_at) FROM market_snapshots')).scalar()
        self.assertEqual('2026-10-23 03:45:00', str(stamp))
        self.kite.historical_data.assert_called_once()
        self.factory.reset_mock()
        self.kite.reset_mock()
        self.assertEqual(rows, self.load())
        self.factory.assert_not_called()
        self.kite.historical_data.assert_not_called()

    def test_only_gap_and_invalid_candle_refetched(self):
        self.load()
        self.connection.execute(text("DELETE FROM market_snapshots WHERE captured_at = '2026-10-23 04:00:00'"))
        self.connection.execute(text("UPDATE market_snapshots SET high_price = 1 WHERE captured_at = '2026-10-23 04:01:00'"))
        self.kite.historical_data.reset_mock()
        self.assertEqual(375, len(self.load()))
        args = self.kite.historical_data.call_args.args
        self.assertEqual(OPEN + timedelta(minutes=15), args[1])
        self.assertEqual(OPEN + timedelta(minutes=16), args[2])
        self.kite.historical_data.assert_called_once()

    def test_incomplete_candle_not_cached(self):
        rows = self.load(now=OPEN + timedelta(minutes=2, seconds=1))
        self.assertEqual(1, len(rows))
        self.assertEqual(OPEN, self.kite.historical_data.call_args.args[2])

    def test_missing_broker_data_fails_instead_of_silent_partial_backtest(self):
        self.kite.historical_data.side_effect = lambda *args: []
        with self.assertRaisesRegex(ValueError, '375 missing/invalid'):
            self.load()

    def test_holiday_does_not_fetch(self):
        rows = load_snapshot_candles(DAY, DAY, now=OPEN + timedelta(hours=7),
            session_factory=self.session, trading_day=lambda day, db: False, kite_factory=self.factory)
        self.assertEqual([], rows)
        self.factory.assert_not_called()

    def test_future_date_rejected(self):
        with self.assertRaisesRegex(ValueError, 'future'):
            self.load(now=OPEN - timedelta(days=1))

    def test_published_holiday_missing_from_database_does_not_fetch(self):
        for day in (date(2025, 10, 2), date(2025, 10, 21), date(2025, 10, 22),
                    date(2026, 1, 15), date(2026, 10, 2), date(2026, 10, 20)):
            with self.subTest(day=day):
                rows = load_snapshot_candles(day, day, now=OPEN + timedelta(hours=7),
                    session_factory=self.session, trading_day=lambda day, db: True,
                    kite_factory=self.factory)
                self.assertEqual([], rows)
        self.factory.assert_not_called()

    def test_range_skips_holiday_but_keeps_adjacent_trading_day(self):
        rows = load_snapshot_candles(date(2025, 10, 2), date(2025, 10, 3),
            now=OPEN + timedelta(hours=7), session_factory=self.session,
            trading_day=lambda day, db: True, kite_factory=self.factory)
        self.assertEqual(375, len(rows))
        self.assertTrue(all(row['date'].startswith('2025-10-03') for row in rows))
        self.kite.historical_data.assert_called_once()

    def test_weekend_missing_from_database_does_not_fetch(self):
        rows = load_snapshot_candles(date(2025, 10, 4), date(2025, 10, 5),
            now=OPEN + timedelta(hours=7), session_factory=self.session,
            trading_day=lambda day, db: True, kite_factory=self.factory)
        self.assertEqual([], rows)
        self.factory.assert_not_called()

    def test_election_holiday_is_excluded_between_normal_sessions(self):
        rows = load_snapshot_candles(date(2026, 1, 14), date(2026, 1, 16),
            now=OPEN, session_factory=self.session,
            trading_day=lambda day, db: True, kite_factory=self.factory)
        self.assertEqual(750, len(rows))
        self.assertEqual({'2026-01-14', '2026-01-16'}, {r['date'][:10] for r in rows})
        self.kite.historical_data.assert_called_once()

    def test_2024_holidays_do_not_require_database_calendar_entries(self):
        for value in ('2024-01-22', '2024-01-26', '2024-03-08', '2024-03-25',
                      '2024-03-29', '2024-04-11', '2024-04-17', '2024-05-01',
                      '2024-05-20', '2024-06-17', '2024-07-17', '2024-08-15',
                      '2024-10-02', '2024-11-01', '2024-11-15', '2024-11-20',
                      '2024-12-25'):
            with self.subTest(day=value):
                day = date.fromisoformat(value)
                rows = load_snapshot_candles(day, day, now=OPEN,
                    session_factory=self.session, trading_day=lambda day, db: True,
                    kite_factory=self.factory)
                self.assertEqual([], rows)
        self.factory.assert_not_called()

    def test_january_2024_closure_keeps_next_trading_day(self):
        rows = load_snapshot_candles(date(2024, 1, 22), date(2024, 1, 23),
            now=OPEN, session_factory=self.session,
            trading_day=lambda day, db: True, kite_factory=self.factory)
        self.assertEqual(375, len(rows))
        self.assertTrue(all(row['date'].startswith('2024-01-23') for row in rows))

    def test_multiple_days_use_one_fetch_and_bulk_inserts(self):
        statements = []
        event.listen(self.engine, 'before_cursor_execute',
                     lambda conn, cursor, statement, params, context, many: statements.append(statement))
        rows = load_snapshot_candles(date(2025, 10, 3), date(2025, 10, 7),
            now=OPEN, session_factory=self.session,
            trading_day=lambda day, db: True, kite_factory=self.factory)
        self.assertEqual(3 * 375, len(rows))
        self.kite.historical_data.assert_called_once()
        # 1,125 candles become three true multi-row INSERTs, not 1,125 executions.
        inserts = [s for s in statements if 'INSERT INTO market_snapshots' in s]
        self.assertEqual(3, len(inserts))
        self.assertGreater(inserts[0].count("'NIFTY 50'"), 1)

    def test_separate_gaps_use_one_request_and_preserve_cached_values(self):
        original = self.load()
        self.connection.execute(text("DELETE FROM market_snapshots WHERE captured_at IN ('2026-10-23 04:00:00', '2026-10-23 05:00:00')"))
        self.kite.historical_data.reset_mock()
        def changed_history(*args):
            return [dict(row, open=200, high=202, low=199, close=201)
                    for row in self.history(*args)]
        self.kite.historical_data.side_effect = changed_history
        rows = self.load()
        self.kite.historical_data.assert_called_once()
        self.assertEqual(201, rows[15]['close'])
        self.assertEqual(201, rows[75]['close'])
        self.assertEqual(original[16], rows[16])

    def test_fetch_windows_bounded_and_restart_uses_cache(self):
        kwargs = dict(now=OPEN, session_factory=self.session,
                      trading_day=lambda day, db: True, kite_factory=self.factory)
        rows = load_snapshot_candles(date(2025, 9, 1), date(2025, 10, 10), **kwargs)
        self.assertEqual(2, self.kite.historical_data.call_count)
        for call in self.kite.historical_data.call_args_list:
            self.assertLess(call.args[2]-call.args[1], timedelta(days=30))
        self.factory.reset_mock()
        self.assertEqual(rows, load_snapshot_candles(date(2025, 9, 1), date(2025, 10, 10), **kwargs))
        self.factory.assert_not_called()

    def test_database_holidays_read_once_for_entire_range(self):
        self.connection.execute(text('CREATE TABLE trading_holidays (holiday_date DATE)'))
        self.connection.execute(text("INSERT INTO trading_holidays VALUES ('2025-10-06')"))
        rows = load_snapshot_candles(date(2025, 10, 3), date(2025, 10, 7),
            now=OPEN, session_factory=self.session, kite_factory=self.factory)
        self.assertEqual(750, len(rows))
        self.assertFalse(any(row['date'].startswith('2025-10-06') for row in rows))

    @patch('backtest.snapshot_cache.retry_clock.sleep')
    def test_targeted_retry_repairs_six_candles_omitted_by_bulk_fetch(self, sleep):
        gaps = {OPEN + timedelta(minutes=i) for i in range(97, 103)}
        def history(token, first, last, interval):
            rows = self.history(token, first, last, interval)
            if self.kite.historical_data.call_count == 1:
                return [row for row in rows if row['date'] not in gaps]
            return rows
        self.kite.historical_data.side_effect = history
        self.assertEqual(375, len(self.load()))
        self.assertEqual(2, self.kite.historical_data.call_count)
        self.assertEqual(min(gaps), self.kite.historical_data.call_args.args[1])
        self.assertEqual(max(gaps), self.kite.historical_data.call_args.args[2])

    @patch('backtest.snapshot_cache.retry_clock.sleep')
    def test_persistent_gap_reports_times_and_keeps_other_candles(self, sleep):
        gap = OPEN + timedelta(minutes=97)
        self.kite.historical_data.side_effect = lambda *args: [
            row for row in self.history(*args) if row['date'] != gap]
        with self.assertRaisesRegex(ValueError, 'times \\(IST\\): 10:52'):
            self.load()
        self.assertEqual(3, self.kite.historical_data.call_count)
        self.assertEqual(374, self.connection.execute(text('SELECT COUNT(*) FROM market_snapshots')).scalar())

    @patch('backtest.snapshot_cache.retry_clock.sleep')
    def test_invalid_broker_ohlc_is_retried_and_not_saved(self, sleep):
        def history(*args):
            rows = self.history(*args)
            if self.kite.historical_data.call_count == 1:
                rows[0]['high'] = 1
            return rows
        self.kite.historical_data.side_effect = history
        self.assertEqual(375, len(self.load()))
        self.assertEqual(2, self.kite.historical_data.call_count)

    @patch('backtest.snapshot_cache.retry_clock.sleep')
    def test_explicit_skip_omits_whole_day_and_reports_it(self, sleep):
        bad_day = date(2024, 4, 23)
        excluded = []
        def history(*args):
            return [row for row in self.history(*args)
                    if not (row['date'].date() == bad_day
                            and row['date'].hour == 10 and 52 <= row['date'].minute <= 57)]
        self.kite.historical_data.side_effect = history
        rows = load_snapshot_candles(bad_day, date(2024, 4, 24), now=OPEN,
            session_factory=self.session, trading_day=lambda day, db: True,
            kite_factory=self.factory, skip_incomplete_days=True, excluded_days=excluded)
        self.assertEqual([bad_day], excluded)
        self.assertEqual(375, len(rows))
        self.assertTrue(all(row['date'].startswith('2024-04-24') for row in rows))
        # Exclusion does not delete the otherwise valid cached candles.
        self.assertEqual(744, self.connection.execute(text('SELECT COUNT(*) FROM market_snapshots')).scalar())


if __name__ == '__main__':
    unittest.main()
