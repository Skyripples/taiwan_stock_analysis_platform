"""PostgreSQL connection and migration helpers.

Credentials are intentionally read only from the process environment.  This
module never reads a repository file containing secrets.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


REQUIRED = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class DatabaseConfigurationError(RuntimeError):
    """Raised when database credentials are incomplete."""


def settings() -> dict[str, object]:
    missing = [name for name in REQUIRED if not os.environ.get(name)]
    if missing:
        raise DatabaseConfigurationError(
            "Missing database environment variables: " + ", ".join(missing)
        )
    return {
        "host": os.environ["DB_HOST"],
        "port": int(os.environ.get("DB_PORT") or "5432"),
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "sslmode": os.environ.get("DB_SSLMODE") or "require",
        "connect_timeout": 15,
        "application_name": "taiwan_stock_analysis_platform",
    }


def configured() -> bool:
    return all(os.environ.get(name) for name in REQUIRED)


def connect():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on deployment env
        raise RuntimeError("psycopg is required; run pip install -r requirements.txt") from exc
    return psycopg.connect(**settings(), row_factory=dict_row)


@contextmanager
def transaction() -> Iterator[object]:
    connection = connect()
    try:
        with connection.transaction():
            yield connection
    finally:
        connection.close()


def apply_migrations(connection) -> list[str]:
    """Apply every numbered SQL migration once, in lexical order."""
    applied: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version text PRIMARY KEY,
                   applied_at timestamptz NOT NULL DEFAULT now()
               )"""
        )
        cursor.execute("SELECT version FROM schema_migrations")
        existing = {row["version"] for row in cursor.fetchall()}
        for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            if path.name in existing:
                continue
            cursor.execute(path.read_text(encoding="utf-8"))
            cursor.execute(
                "INSERT INTO schema_migrations(version) VALUES (%s)", (path.name,)
            )
            applied.append(path.name)
    return applied
