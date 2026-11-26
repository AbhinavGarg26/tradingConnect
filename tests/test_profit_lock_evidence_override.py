import json
from unittest.mock import MagicMock

from one_direction_profit_lock_readiness import evidence_override


ACK = "I accept live trading without the historical option backtest and verified reversal exit"


def factory_for(value):
    db = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = value
    context = MagicMock()
    context.__enter__.return_value = db
    return lambda: context


def test_explicit_evidence_approval():
    assert evidence_override(factory_for(json.dumps({"approved": True, "acknowledgement": ACK})), 7)


def test_missing_or_invalid_approval_fails_closed():
    for value in (None, "not json", json.dumps({"approved": True}),
                  json.dumps({"approved": False, "acknowledgement": ACK})):
        assert not evidence_override(factory_for(value), 7)
