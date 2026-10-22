"""Experimental V3D: +10 => breakeven, +20 => lock10, continuing by tens."""
from decimal import Decimal
from strategies.nifty_30m_reference import Nifty30mReference

STRATEGY_NAME = 'nifty_30m_reference_step_trail'
STRATEGY_VERSION = 'v3d'


class Nifty30mReferenceStepTrail(Nifty30mReference):
    def __init__(self, *, trail_step_points=10, **kwargs):
        self.trail_step_points=Decimal(str(trail_step_points))
        if not self.trail_step_points.is_finite() or self.trail_step_points<=0:
            raise ValueError('Trail step must be positive and finite')
        self.entry_price=None
        self.trailing_active=False
        super().__init__(**kwargs)

    def _manage_stop(self,bar):
        direction=1 if self.position=='CE' else -1
        favorable=direction*(bar.close-self.entry_price)
        if favorable < self.trail_step_points:
            return
        steps=favorable//self.trail_step_points
        candidate=self.entry_price+direction*(steps-1)*self.trail_step_points
        improves=candidate>self.stop if direction==1 else candidate<self.stop
        if improves:
            self.stop=candidate
            self.trailing_active=True

    def _stop_exit_reason(self):
        return 'step_trail_stop' if self.trailing_active else 'fixed_stop'

    def confirm_fill(self, *, index_price=None):
        entering=self.pending is not None and self.pending.action.startswith('BUY_')
        super().confirm_fill(index_price=index_price)
        self.entry_price=Decimal(str(index_price)) if entering else None
        self.trailing_active=False
