"""Cross-process exclusion for reference-runner and guardian broker operations."""
from contextlib import contextmanager


@contextmanager
def trade_execution_lock(trade_id):
    from sqlalchemy import text
    from trading.database import engine
    with engine.connect() as connection:
        key = {'key': f'strategy-trade:{int(trade_id)}'}
        acquired = connection.execute(text(
            'SELECT pg_try_advisory_lock(hashtextextended(:key, 0))'), key).scalar()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                try:
                    connection.rollback()
                    connection.execute(text(
                        'SELECT pg_advisory_unlock(hashtextextended(:key, 0))'), key)
                    connection.commit()
                except Exception:
                    # A session lock must never leak into the connection pool.
                    connection.invalidate()
                    raise
