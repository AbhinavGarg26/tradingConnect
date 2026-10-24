"""Independent monthly-call profile using completed NIFTY 15m EMA 9/21 crosses."""
KEY = 'nifty_bullish_15m_9_21'
VERSION = 'bullish_15m_9_21_v1'
FAST = 9
SLOW = 21


def decision(state, *, owned_call=False):
    if not state['warmed_up']:
        return 'warmup'
    if owned_call:
        return 'exit_call' if state['cross_down'] else 'hold_call'
    return 'enter_call' if state['cross_up'] else 'wait'
