import ast
from datetime import datetime
from pathlib import Path


SOURCE = Path(__file__).parents[1].joinpath("market_breath.py").read_text()
TREE = ast.parse(SOURCE)
CONSTANT_NAMES = {
    "ACTIVE_POLL_INTERVAL",
    "FINAL_SESSION_POLL_INTERVAL",
    "IDLE_POLL_INTERVAL",
    "FINAL_SESSION_START_HOUR",
    "BROKER_POSITION_REFRESH_INTERVAL",
}
CONSTANTS = [
    node for node in TREE.body
    if isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id in CONSTANT_NAMES for target in node.targets)
]
FUNCTION = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_position_poll_interval"
)
NAMESPACE = {"datetime": datetime}
exec(compile(ast.Module(body=[*CONSTANTS, FUNCTION], type_ignores=[]), "market_breath.py", "exec"), NAMESPACE)
position_poll_interval = NAMESPACE["_position_poll_interval"]


def test_open_position_polling_is_fastest_in_final_session():
    regular = position_poll_interval(1, datetime(2026, 9, 18, 14, 30))
    final_session = position_poll_interval(1, datetime(2026, 9, 18, 15, 15))

    assert regular == 0.25
    assert final_session == 0.10
    assert final_session < regular


def test_idle_polling_avoids_unnecessary_broker_pressure():
    assert position_poll_interval(0, datetime(2026, 9, 18, 15, 15)) == 2.0


def test_broker_inventory_has_a_separate_one_second_refresh_limit():
    assert NAMESPACE["BROKER_POSITION_REFRESH_INTERVAL"] == 1.0
