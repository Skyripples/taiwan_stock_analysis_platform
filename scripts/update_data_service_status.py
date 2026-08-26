"""Atomically maintain the operational API/database manifest."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


STATUS_PATH = PROJECT_ROOT / "data" / "status" / "data_service_status.json"
INDEX_PATH = PROJECT_ROOT / "data" / "stocks" / "index.json"


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-sync-status", choices=("success", "failed", "not_configured"), required=True)
    parser.add_argument("--api-version", default="3.11.0")
    parser.add_argument("--output", type=Path, default=STATUS_PATH)
    parser.add_argument("--index", type=Path, default=INDEX_PATH)
    options = parser.parse_args()

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    previous = read_json(options.output)
    index = read_json(options.index)
    payload = {
        "updated_at": now,
        "last_database_sync": now if options.database_sync_status == "success" else previous.get("last_database_sync"),
        "database_sync_status": options.database_sync_status,
        "universe_count": index.get("active_count", len(index.get("stocks", []))),
        "api_version": options.api_version,
    }
    atomic_json(options.output, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
