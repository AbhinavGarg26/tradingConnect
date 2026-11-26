"""Incremental frozen Groww ORB signals; this module never sends orders."""
from datetime import datetime, time, timedelta
from backtest.snapshot_cache import exchange_time, valid_ohlc
from backtest.groww_orb import SETUPS


class GrowwOrbIntraday:
    """Consume consecutive completed opening-time candles in exchange time.

    Each setup emits at most one entry. Callers own fills, exits and recovery.
    """
    def __init__(self):
        self.bars = []
        self.states = {name: dict(first=None, first_time=None, inside=False, done=False)
                       for name in SETUPS}

    def on_bar(self, row):
        stamp = exchange_time(row['date'])
        if not valid_ohlc(row):
            raise ValueError('Invalid minute candle')
        if not self.bars:
            if stamp.time() != time(9, 15):
                raise ValueError('Session must begin at 09:15')
        elif stamp != self.bars[-1][0] + timedelta(minutes=1):
            raise ValueError('Minute candles must be consecutive')
        if stamp.time() > time(15, 29):
            raise ValueError('Candle outside regular session')
        bar = {key: float(row[key]) for key in ('open', 'high', 'low', 'close')}
        self.bars.append((stamp, bar))
        close_time = stamp + timedelta(minutes=1)
        signals = []
        for name, (minutes, width_filter) in SETUPS.items():
            state = self.states[name]
            if state['done'] or len(self.bars) <= minutes or close_time.time() >= time(15, 29):
                continue
            opening = [value for _, value in self.bars[:minutes]]
            high, low = max(x['high'] for x in opening), min(x['low'] for x in opening)
            width = (high - low) / opening[0]['open']
            if (width_filter == 'wide' and width <= .005) or (width_filter == 'narrow' and width > .005):
                state['done'] = True
                continue
            side = 1 if bar['close'] > high else -1 if bar['close'] < low else 0
            if state['first'] is None:
                if not side:
                    continue
                state['first'], state['first_time'] = side, close_time
                if name == 'breakout':
                    state['done'] = True
                    if side == 1 and close_time.time() < time(11):
                        signals.append(dict(setup=name, side='CE', signal_time=close_time,
                                            stop=low, high=high, low=low))
            elif name != 'breakout':
                if side == 0:
                    state['inside'] = True
                elif name == 'retest' and close_time - state['first_time'] > timedelta(minutes=60):
                    state['done'] = True
                elif state['inside'] and ((name == 'retest' and side == state['first']) or
                        (name == 'reversal' and side == -1 and side == -state['first']
                         and close_time.time() < time(12))):
                    state['done'] = True
                    signals.append(dict(setup=name, side='CE' if side == 1 else 'PE',
                                        signal_time=close_time, stop=low if side == 1 else high,
                                        high=high, low=low))
        return signals
