from datetime import datetime, timezone

from fastapi import APIRouter

from api.db import pool


router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    with pool.connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 AS healthy")
        database_ok = cursor.fetchone()["healthy"] == 1
    return {
        "status": "ok" if database_ok else "degraded",
        "database": {"status": "ok" if database_ok else "unavailable"},
        "api_version": "3.15.0",
        "server_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
