"""Research entry selection only: first signal PE trades; first signal CE skips day."""
from strategies.nifty_30m_reference import Nifty30mReference


class Nifty30mReferenceFirstPE(Nifty30mReference):
    def __init__(self, *, max_entries=1, **kwargs):
        if max_entries != 1:
            raise ValueError('First-signal PE experiment requires max_entries=1')
        self.decision_session = self.first_signal_side = None
        super().__init__(max_entries=max_entries, **kwargs)

    def _entry_allowed(self, side, bar):
        if self.decision_session != self.session:
            self.decision_session = self.session
            self.first_signal_side = None
        if self.first_signal_side is None:
            self.first_signal_side = side
        return self.first_signal_side == 'PE' and side == 'PE'
