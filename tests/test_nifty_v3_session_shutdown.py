from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from nifty_15m_env import load_nifty_environment
load_nifty_environment()
import nifty_15m_live as runner


@pytest.mark.parametrize('trade_id', [None, 12])
def test_after_close_stops_flat_and_refuses_unresolved_trade(trade_id):
    recorder = Mock(trade_id=trade_id)
    kite = Mock()
    now = datetime(2026, 11, 17, 15, 31, tzinfo=runner.IST)
    with patch.object(runner, 'datetime') as clock, patch.object(runner, 'next_week_expiry'), \
            patch.object(runner.time, 'sleep') as sleep:
        clock.now.return_value = now
        if trade_id is None:
            runner._run_session(kite, recorder=recorder)
            recorder.finish.assert_called_once_with('completed')
        else:
            with pytest.raises(RuntimeError, match='unresolved strategy trade'):
                runner._run_session(kite, recorder=recorder)
            recorder.finish.assert_not_called()
        sleep.assert_not_called()
        kite.place_order.assert_not_called()
