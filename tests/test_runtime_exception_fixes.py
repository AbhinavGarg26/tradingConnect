import ast
from pathlib import Path

import pandas as pd

from analytics.kite_sync_orders import calculate_profit_factor


SOURCE = Path(__file__).parents[1].joinpath("kite_market_fetcher.py").read_text()
TREE = ast.parse(SOURCE)
LATEST_INDICATOR = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_latest_indicator"
)
SAFE = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_safe"
)
NAMESPACE = {"Optional": __import__("typing").Optional}
exec(compile(ast.Module(body=[SAFE, LATEST_INDICATOR], type_ignores=[]), "kite_market_fetcher.py", "exec"), NAMESPACE)
latest_indicator = NAMESPACE["_latest_indicator"]


def test_missing_warmup_indicator_returns_none_instead_of_crashing():
    assert latest_indicator(None) is None


def test_latest_indicator_accepts_pandas_and_array_results():
    assert latest_indicator(pd.Series([10.0, 12.5])) == 12.5
    assert latest_indicator([10.0, 12.5]) == 12.5


def test_profit_factor_is_null_when_there_are_no_losses():
    assert calculate_profit_factor(14_706.0, 0.0) is None


def test_profit_factor_remains_a_ratio_when_losses_exist():
    assert calculate_profit_factor(1_000.0, 250.0) == 4.0
