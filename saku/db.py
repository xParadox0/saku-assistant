"""Database connection and schema setup."""
import os
from importlib.resources import files

import psycopg


def connect(url: str | None = None) -> psycopg.Connection:
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return psycopg.connect(url, autocommit=True)


def init_schema(conn: psycopg.Connection) -> None:
    """Apply saku/schema.sql. Safe to run repeatedly (CREATE ... IF NOT EXISTS)."""
    sql = files("saku").joinpath("schema.sql").read_text(encoding="utf-8")
    with conn.transaction():
        conn.execute(sql)
