"""Load the same local database settings as the sibling Rails development app."""

import os
from pathlib import Path


def load_nifty_environment():
    from dotenv import dotenv_values, load_dotenv
    from sqlalchemy.engine.url import URL, make_url

    load_dotenv()
    local_file = Path(__file__).resolve().parent.parent / "portfolio-tracker/config/.env.local"
    if not local_file.is_file():
        return
    rails = dotenv_values(local_file)
    current = os.getenv("DATABASE_URL")
    if not current:
        return
    parsed = make_url(current)
    # Never replace a remote or differently named database connection.
    if (parsed.host not in {"127.0.0.1", "localhost"} or parsed.port != 5433
            or parsed.database != rails.get("POSTGRES_DATABASE")):
        return
    password = rails.get("POSTGRES_PASSWORD")
    username = rails.get("POSTGRES_USERNAME")
    if password and username and (password != parsed.password or username != parsed.username):
        os.environ["DATABASE_URL"] = URL.create(
            drivername=parsed.drivername, username=username, password=password,
            host=parsed.host, port=parsed.port, database=parsed.database,
        ).render_as_string(hide_password=False)
