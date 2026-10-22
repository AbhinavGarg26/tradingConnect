"""Experimental V3B: only reference exits receive a five-point buffer."""
from decimal import Decimal
from strategies.nifty_30m_reference import Nifty30mReference

STRATEGY_NAME = 'nifty_30m_reference_buffered'
STRATEGY_VERSION = 'v3b'


class Nifty30mReferenceBuffered(Nifty30mReference):
    def __init__(self, *, reference_exit_buffer=5, **kwargs):
        self.reference_exit_buffer = Decimal(str(reference_exit_buffer))
        if not self.reference_exit_buffer.is_finite() or self.reference_exit_buffer < 0:
            raise ValueError('Reference exit buffer must be finite and nonnegative')
        super().__init__(**kwargs)

    def _reference_exit_level(self, direction, reference):
        # CE exits below reference-buffer; PE above reference+buffer.
        return reference - direction * self.reference_exit_buffer
