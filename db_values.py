"""Helpers for passing pandas/NumPy-generated values to database drivers."""

import math
from collections.abc import Mapping


def normalize_db_value(value):
    """Convert pandas/NumPy values into types understood by psycopg2."""
    if value is None:
        return None

    # NumPy scalars expose item(); native Python scalars generally do not.
    item = getattr(value, "item", None)
    if callable(item):
        try:
            value = item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {key: normalize_db_value(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_db_value(val) for val in value]
    return value


def normalize_db_params(row: dict) -> dict:
    """Return a copy of a SQL parameter row containing native Python values."""
    return {key: normalize_db_value(value) for key, value in row.items()}
