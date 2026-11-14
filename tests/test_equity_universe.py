from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backtest.equity_universe import download_constituents, load_equity_universe, sync_equity_instruments


def write_index(root: Path, name: str, symbols):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.csv").write_text("Company Name,Industry,Symbol,Series,ISIN Code\n" +
                                      "\n".join(f"Company {s},Test,{s},EQ,INE{s}" for s in symbols))


def test_universe_is_cached_and_deduplicated(tmp_path):
    write_index(tmp_path, "nifty500", ["AAA", "BBB"])
    write_index(tmp_path, "midcap150", ["BBB", "CCC"])
    assert download_constituents("nifty500", cache_dir=tmp_path) == ["AAA", "BBB"]
    assert load_equity_universe(("nifty500", "midcap150"), cache_dir=tmp_path) == ("AAA", "BBB", "CCC")


def test_instrument_sync_is_idempotent():
    engine = create_engine("sqlite://")
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.execute(text("""CREATE TABLE instruments (
          id INTEGER PRIMARY KEY, symbol TEXT, exchange TEXT, segment TEXT,
          instrument_type TEXT, instrument_token INTEGER UNIQUE, lot_size INTEGER,
          tick_size NUMERIC, expiry_date DATE, strike_price NUMERIC, is_active BOOLEAN,
          created_at TIMESTAMP, updated_at TIMESTAMP)"""))
        db.commit()

    class Kite:
        def instruments(self, exchange):
            return [{"tradingsymbol": "AAA", "instrument_type": "EQ", "segment": "NSE",
                     "instrument_token": 123, "lot_size": 1, "tick_size": .05}]

    with factory() as db:
        first = sync_equity_instruments(db, Kite(), ("AAA", "MISSING"))
        second = sync_equity_instruments(db, Kite(), ("AAA",))
        assert first["missing"] == ["MISSING"]
        assert second["upserted"] == 1
        assert db.execute(text("SELECT count(*) FROM instruments")).scalar() == 1
