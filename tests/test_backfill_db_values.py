import numpy as np

from db_values import normalize_db_params


def test_normalize_db_params_converts_numpy_scalars():
    row = {
        "price": np.float64(123.45),
        "volume": np.int64(1000),
        "missing": np.float64("nan"),
    }

    normalized = normalize_db_params(row)

    assert normalized["price"] == 123.45
    assert type(normalized["price"]) is float
    assert normalized["volume"] == 1000
    assert type(normalized["volume"]) is int
    assert normalized["missing"] is None


def test_normalize_db_params_converts_nested_values():
    normalized = normalize_db_params({"values": [np.float64(1.5), np.float64("inf")]})

    assert normalized == {"values": [1.5, None]}
