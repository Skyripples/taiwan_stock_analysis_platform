"""Connection pool using the existing database environment configuration."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from database.connection import settings


class DatabaseUnavailable(RuntimeError):
    pass


class DatabasePool:
    def __init__(self) -> None:
        self._pool = None

    def open(self) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        options = dict(settings())
        options["options"] = "-c statement_timeout=5000 -c idle_in_transaction_session_timeout=10000"
        self._pool = ConnectionPool(
            kwargs={**options, "row_factory": dict_row},
            min_size=1,
            max_size=10,
            timeout=3,
            max_idle=300,
            max_lifetime=1800,
            open=True,
        )
        self._pool.wait(timeout=10)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def connection(self) -> Iterator[object]:
        if self._pool is None:
            raise DatabaseUnavailable("Database pool is not available")
        try:
            with self._pool.connection(timeout=3) as connection:
                yield connection
        except Exception as exc:
            from psycopg import OperationalError
            from psycopg_pool import PoolTimeout

            if isinstance(exc, (OperationalError, PoolTimeout)):
                raise DatabaseUnavailable("Database is temporarily unavailable") from exc
            raise


pool = DatabasePool()
