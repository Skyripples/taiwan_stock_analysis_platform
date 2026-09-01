"""Validate production prediction inputs without creating a prediction."""

from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import MARKET_DATA_DIR, PROJECT_ROOT
from update_history import MARKET_FIELDS, build_history_rows, load_sources


MODEL_PATH = PROJECT_ROOT / "models" / "baseline_model.pkl"
MODEL_INFO_PATH = PROJECT_ROOT / "models" / "model_info.json"
HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"
STATUS_PATH = PROJECT_ROOT / "data" / "status" / "prediction_pipeline_status.json"
FORMAL_FEATURES = (
    "taiex_close",
    "taiex_change_percent",
    "tpex_close",
    "turnover",
    "advancing",
    "declining",
    "foreign_cash_flow",
    "foreign_futures_position",
    "night_futures_change",
    "tsm_adr_change_percent",
    "sox_change_percent",
    "sp500_change_percent",
    "nasdaq_change_percent",
    "vix_change_percent",
    "kospi_change_percent",
)


def _latest_history_row(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise ValueError("market_daily.csv has no data rows")
    return rows[-1]


def _same_value(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-9)
    except (TypeError, ValueError):
        return str(left) == str(right)


def check_readiness(
    history_path: Path = HISTORY_PATH,
    market_data_dir: Path = MARKET_DATA_DIR,
    upstream_market_data_ok: bool = True,
) -> dict[str, Any]:
    """Return a readiness result; malformed or stale inputs are never ready."""

    if not upstream_market_data_ok:
        return {
            "status": "skipped",
            "ready": False,
            "reason": "required_market_provider_failed",
            "feature_count": len(FORMAL_FEATURES),
        }

    try:
        if not MODEL_PATH.exists():
            raise ValueError("production baseline model artifact is missing")
        with MODEL_INFO_PATH.open("r", encoding="utf-8") as model_info_file:
            model_info = json.load(model_info_file)
        model_features = model_info.get("feature_names") if isinstance(model_info, dict) else None
        if model_features != list(FORMAL_FEATURES):
            raise ValueError("production model feature_names do not match the formal 15-feature contract")
        history_row = _latest_history_row(history_path)
        expected_row, _ = build_history_rows(load_sources(market_data_dir))
        missing = [name for name in FORMAL_FEATURES if history_row.get(name) in (None, "")]
        invalid = []
        for name in FORMAL_FEATURES:
            if name in missing:
                continue
            try:
                value = float(history_row[name])
                if not math.isfinite(value):
                    invalid.append(name)
            except (TypeError, ValueError):
                invalid.append(name)
        if missing or invalid:
            return {
                "status": "skipped",
                "ready": False,
                "reason": "formal_features_incomplete",
                "missing_features": missing,
                "invalid_features": invalid,
                "feature_count": len(FORMAL_FEATURES),
            }

        stale_fields = [
            field
            for field in MARKET_FIELDS
            if field in expected_row and not _same_value(history_row.get(field), expected_row[field])
        ]
        if stale_fields:
            return {
                "status": "stale",
                "ready": False,
                "reason": "history_does_not_match_latest_required_sources",
                "feature_date": history_row.get("trade_date"),
                "latest_source_date": expected_row.get("trade_date"),
                "stale_fields": stale_fields,
                "feature_count": len(FORMAL_FEATURES),
            }
        return {
            "status": "ready",
            "ready": True,
            "reason": None,
            "feature_date": history_row.get("trade_date"),
            "feature_count": len(FORMAL_FEATURES),
        }
    except Exception as exc:
        return {
            "status": "skipped",
            "ready": False,
            "reason": "prediction_input_validation_failed",
            "detail": str(exc),
            "feature_count": len(FORMAL_FEATURES),
        }


def _write_status(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": datetime.now(timezone.utc).isoformat(), **result}
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    upstream_ok = os.getenv("MARKET_DATA_OUTCOME", "success") == "success"
    result = check_readiness(upstream_market_data_ok=upstream_ok)
    _write_status(STATUS_PATH, result)
    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output:
            output.write(f"ready={'true' if result['ready'] else 'false'}\n")
    print(
        "Prediction readiness: "
        f"status={result['status']} ready={str(result['ready']).lower()} "
        f"reason={result.get('reason') or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
