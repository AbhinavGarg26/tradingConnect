"""Read-through cache of completed NIFTY minute candles in market_snapshots.

Snapshot timestamps follow the replay convention: UTC in the database,
converted to Indian exchange time for the strategy.
"""
import logging
import time as retry_clock
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from strategies.nifty_30m_reference import IST
from backtest.session_calendar import regular_session_closed

log = logging.getLogger(__name__)


def exchange_time(value, *, database=False):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc if database else IST)
    return value.astimezone(IST)


def valid_ohlc(row):
    try:
        o, h, l, c = (Decimal(str(row[k])) for k in ('open', 'high', 'low', 'close'))
        return all(v.is_finite() and v > 0 for v in (o, h, l, c)) and l <= min(o, c) <= max(o, c) <= h
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False


def save_candles(db, rows):
    """True multi-row upserts, avoiding a database round trip per candle."""
    from sqlalchemy import text
    for offset in range(0, len(rows), 500):
        batch = rows[offset:offset + 500]
        values, params = [], {}
        for i, row in enumerate(batch):
            values.append(f"('NIFTY 50', '1m', :stamp{i}, :close{i}, :open{i}, :high{i}, "
                          f":low{i}, :close{i}, :now{i}, :now{i})")
            params.update({f'{key}{i}': value for key, value in row.items()})
        db.execute(text("""
            INSERT INTO market_snapshots
              (symbol, timeframe, captured_at, ltp, open_price, high_price,
               low_price, close_price, created_at, updated_at)
            VALUES """ + ','.join(values) + """
            ON CONFLICT (symbol, timeframe, captured_at) DO UPDATE SET
              ltp = EXCLUDED.ltp, open_price = EXCLUDED.open_price,
              high_price = EXCLUDED.high_price, low_price = EXCLUDED.low_price,
              close_price = EXCLUDED.close_price, updated_at = EXCLUDED.updated_at
        """), params)
    db.commit()  # Keep each fetched window if a later request fails.


def load_snapshot_candles(start, end, *, now=None, session_factory=None,
                          trading_day=None, kite_factory=None,
                          skip_incomplete_days=False, excluded_days=None,
                          database_only=False):
    from sqlalchemy import text
    if end < start:
        raise ValueError('End precedes start')
    if session_factory is None:
        from nifty_15m_env import load_nifty_environment
        load_nifty_environment()
        from trading.database import get_db
        session_factory = get_db
    now = exchange_time(now or datetime.now(IST))
    if end > now.date():
        raise ValueError('Backtest end date cannot be in the future')
    kite = token = None
    output = []
    with session_factory() as db:
        # One holiday query for the entire requested range, not one per day.
        holidays = set()
        if trading_day is None:
            holidays = {date.fromisoformat(row[0]) if isinstance(row[0], str) else row[0]
                        for row in db.execute(text(
                'SELECT holiday_date FROM trading_holidays WHERE holiday_date BETWEEN :start AND :end'
            ), {'start': start, 'end': end})}
        window = start
        while window <= end:
            window_end = min(window + timedelta(days=29), end)
            expected = set()
            day = window
            while day <= window_end:
                if (not regular_session_closed(day) and day not in holidays
                        and (trading_day is None or trading_day(day, db))):
                    opening = datetime.combine(day, time(9, 15), IST)
                    expected.update(opening + timedelta(minutes=i) for i in range(375)
                                    if opening + timedelta(minutes=i+1, seconds=2) <= now)
                day += timedelta(days=1)
            if not expected:
                window = window_end + timedelta(days=1)
                continue

            def read_window():
                records = db.execute(text("""
                    SELECT captured_at, open_price, high_price, low_price, close_price
                    FROM market_snapshots
                    WHERE symbol = 'NIFTY 50' AND timeframe = '1m'
                      AND captured_at >= :start AND captured_at <= :end
                    ORDER BY captured_at
                """), {'start': min(expected).astimezone(timezone.utc).replace(tzinfo=None),
                           'end': max(expected).astimezone(timezone.utc).replace(tzinfo=None)})
                result = {}
                for stamp, o, h, l, c in records:
                    stamp = exchange_time(stamp, database=True)
                    row = dict(date=stamp.isoformat(), open=o, high=h, low=l, close=c)
                    if stamp in expected and valid_ohlc(row):
                        result[stamp] = row
                return result

            saved = read_window()
            missing = expected - saved.keys()
            if missing and database_only:
                affected = sorted({stamp.date() for stamp in missing})
                if not skip_incomplete_days:
                    raise ValueError(f'{affected[0]}: missing/invalid NIFTY 1m candles in '
                                     'market_snapshots. Database-only mode does not fetch or write data. '
                                     'Use --skip-incomplete-days to explicitly exclude affected days.')
                for day in affected:
                    log.warning('EXCLUDED %s: incomplete database minute candles', day)
                    if excluded_days is not None:
                        excluded_days.append(day)
                expected = {stamp for stamp in expected if stamp.date() not in affected}
                output.extend(saved[stamp] for stamp in sorted(expected))
                window = window_end + timedelta(days=1)
                continue
            if not missing:
                log.info('%s to %s: all %s minute candles cached', window, window_end, len(saved))
            else:
                if kite is None:
                    if kite_factory is None:
                        from trading.user_token import fetch_user_token
                        kite, _ = fetch_user_token(log)
                    else:
                        kite = kite_factory()
                    token = next((i['instrument_token'] for i in kite.instruments('NSE')
                                  if i['tradingsymbol'] == 'NIFTY 50'), None)
                    if token is None:
                        raise ValueError('NIFTY 50 not found in Kite instruments')
                log.info('%s to %s: %s cached, fetching %s missing/invalid minutes in one request',
                         window, window_end, len(saved), len(missing))
                # One request per <=30-calendar-day window, even for many gaps.
                # Cached candles between the endpoints are ignored, never overwritten.
                def fetch_and_save(first, last, wanted):
                    records = kite.historical_data(token, first, last, 'minute')
                    rows = {}
                    invalid = 0
                    for row in records:
                        stamp = exchange_time(row['date'])
                        if stamp not in wanted:
                            continue
                        if not valid_ohlc(row):
                            invalid += 1
                            continue
                        rows[stamp] = dict(stamp=stamp.astimezone(timezone.utc).replace(tzinfo=None),
                                           open=row['open'], high=row['high'], low=row['low'], close=row['close'],
                                           now=now.astimezone(timezone.utc).replace(tzinfo=None))
                    if rows:
                        save_candles(db, list(rows.values()))
                        log.info('Saved %s candles in batches of up to 500', len(rows))
                    if invalid:
                        log.warning('Kite returned %s invalid OHLC candles in requested gaps', invalid)

                fetch_and_save(min(missing), max(missing), missing)
                saved = read_window()  # Backtest persisted values, including DB rounding.
                remaining = expected - saved.keys()
                # A bulk response can omit candles. Retry short day-scoped ranges
                # twice, keeping successful repairs without replacing valid rows.
                for attempt in range(1, 3):
                    if not remaining:
                        break
                    for day in sorted({stamp.date() for stamp in remaining}):
                        wanted = {stamp for stamp in remaining if stamp.date() == day}
                        log.warning('%s: retry %s/2 for %s missing/invalid minutes (%s to %s)',
                                    day, attempt, len(wanted), min(wanted).time(), max(wanted).time())
                        retry_clock.sleep(0.4)
                        fetch_and_save(min(wanted), max(wanted), wanted)
                    saved = read_window()
                    remaining = expected - saved.keys()
                if remaining and skip_incomplete_days:
                    excluded = sorted({stamp.date() for stamp in remaining})
                    for day in excluded:
                        gaps = sorted(stamp for stamp in remaining if stamp.date() == day)
                        log.warning('EXCLUDED %s: entire trading day omitted; %s missing/invalid minutes: %s',
                                    day, len(gaps), ', '.join(stamp.strftime('%H:%M') for stamp in gaps))
                        if excluded_days is not None:
                            excluded_days.append(day)
                    expected = {stamp for stamp in expected if stamp.date() not in excluded}
                elif remaining:
                    first = min(remaining)
                    day_gaps = sorted(stamp for stamp in remaining if stamp.date() == first.date())
                    sample = ', '.join(stamp.strftime('%H:%M') for stamp in day_gaps[:12])
                    raise ValueError(f'{first.date()}: {len(day_gaps)} missing/invalid NIFTY 1m candles '
                                     f'after bulk fetch and 2 targeted retries; times (IST): {sample}. '
                                     'Kite did not supply valid OHLC for these minutes. '
                                     'Valid fetched candles are saved; reruns reuse them. '
                                     'Backtest stopped to avoid fabricating prices or silently omitting a trading day. '
                                     'Use --skip-incomplete-days to explicitly exclude entire affected days.')
            output.extend(saved[stamp] for stamp in sorted(expected))
            window = window_end + timedelta(days=1)
    return output
