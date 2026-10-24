"""Daily 20/50 monthly-call planning. Pure functions; no broker order capability."""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from backtest.nifty_daily_ema import validate_daily

KEY = 'nifty_daily_ema_monthly'
VERSION = 'ema20_50_calls_monthly_v2'


@dataclass(frozen=True)
class Settings:
    max_premium: Decimal  # Explicit total rupee premium cap for one lot.
    roll_sessions: int = 2

    def __post_init__(self):
        if not self.max_premium.is_finite() or not 0 < self.max_premium <= Decimal('30000'):
            raise ValueError('Premium cap must be positive and no greater than the approved 30000 per lot')
        if type(self.roll_sessions) is not int or self.roll_sessions < 1:
            raise ValueError('roll_sessions must be a positive integer')


def trend(rows):
    bars = validate_daily(rows)
    if len(bars) < 51:
        raise ValueError('At least 51 completed daily candles required')
    fast = slow = None
    prior = None
    for bar in bars:
        prior = (fast, slow)
        fast = bar['close'] if fast is None else fast+2/21*(bar['close']-fast)
        slow = bar['close'] if slow is None else slow+2/51*(bar['close']-slow)
    return dict(date=bars[-1]['date'], close=bars[-1]['close'], fast=fast, slow=slow,
                bullish=fast > slow, cross_up=prior[0] <= prior[1] and fast > slow,
                cross_down=prior[0] >= prior[1] and fast < slow)


def sessions_to_expiry(today, expiry, is_trading_day):
    """Remaining trading dates after today, through expiry inclusive."""
    if expiry < today:
        return -1
    day = today+timedelta(days=1)
    total = 0
    while day <= expiry:
        total += bool(is_trading_day(day))
        day += timedelta(days=1)
    return total


def select_monthly_option(instruments, today, spot, settings, is_trading_day, side):
    spot = Decimal(str(spot))
    if not spot.is_finite() or spot <= 0:
        raise ValueError('Positive finite spot required')
    if side != 'CE':
        raise ValueError('This strategy selects calls only; put entries are disabled')
    # Monthly NIFTY futures expiry identifies monthly options without guessing
    # the last calendar weekday or mistaking a weekly contract for monthly.
    expiries = sorted({i['expiry'] for i in instruments if i.get('name') == 'NIFTY'
                       and i.get('instrument_type') == 'FUT' and isinstance(i.get('expiry'), date)
                       and i['expiry'] >= today
                       and sessions_to_expiry(today, i['expiry'], is_trading_day) > settings.roll_sessions})
    if not expiries:
        raise ValueError('No monthly expiry outside the configured rollover window')
    for expiry in expiries:
        choices = [i for i in instruments if i.get('name') == 'NIFTY'
                   and i.get('instrument_type') == side and i.get('expiry') == expiry]
        if choices:
            chosen = min(choices, key=lambda i: (abs(Decimal(str(i['strike']))-spot), Decimal(str(i['strike']))))
            if int(chosen.get('lot_size') or 0) <= 0:
                raise ValueError('Invalid exchange-provided lot size')
            return chosen
    raise ValueError('Monthly futures expiries have no matching listed Nifty options')


def action(state, position, today, settings, is_trading_day):
    if state['date'] >= today:
        raise ValueError('Only prior completed daily candles may drive the opening decision')
    previous = today-timedelta(days=1)
    while not is_trading_day(previous):
        previous -= timedelta(days=1)
    if state['date'] != previous:
        raise ValueError('Latest completed daily candle is stale or missing')
    if position:
        if position['expiry'] < today:
            raise ValueError('Recorded option expired; reconcile settlement before continuing')
        if position['side'] != 'CE' or not state['bullish']:
            return 'exit'
        if sessions_to_expiry(today, position['expiry'], is_trading_day) <= settings.roll_sessions:
            return 'roll'
        return 'hold'
    return 'enter' if state['cross_up'] else 'wait'


def quote_fill(quote, *, buying, quantity, now, settings):
    stamp = quote.get('timestamp')
    if stamp is None or stamp.tzinfo is None or not 0 <= (now-stamp).total_seconds() <= 60:
        raise ValueError('Fresh timezone-aware option quote required')
    side = quote.get('depth', {}).get('sell' if buying else 'buy', [])
    if not side or int(side[0].get('quantity') or 0) < quantity:
        raise ValueError('Insufficient quoted top-of-book quantity')
    price = Decimal(str(side[0].get('price') or 0))
    if not price.is_finite() or price <= 0:
        raise ValueError('Positive executable quote required')
    if buying and price*quantity > settings.max_premium:
        raise ValueError('One-lot premium exceeds explicit cap; skip without changing strike or size')
    return price
