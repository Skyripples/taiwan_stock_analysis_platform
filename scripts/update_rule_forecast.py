"""Generate and record the production statistical next-day rule forecast."""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from config import MARKET_DATA_DIR, PROJECT_ROOT
from prediction_temporal import prediction_target_date, source_date_is_valid

CONFIG_PATH = PROJECT_ROOT / "config" / "rule_forecast_config.json"
HISTORICAL_PATH = PROJECT_ROOT / "data" / "history" / "historical_prediction_dataset.csv"
DAILY_DATASET_PATH = PROJECT_ROOT / "data" / "history" / "prediction_dataset.csv"
MARKET_HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"
OUTPUT_PATH = MARKET_DATA_DIR / "next_day_rule_forecast.json"
HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "rule_forecast_history.csv"
HISTORY_FIELDS = ("feature_date", "target_date", "score", "direction", "confidence_status", "weights", "contributions", "generated_at", "feature_close", "target_close", "actual_return", "actual_direction", "hit", "validated_at")
TAIPEI = ZoneInfo("Asia/Taipei")


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def direction(value: float | None, band: float = 0) -> int:
    return 0 if value is None or abs(value) <= band else (1 if value > 0 else -1)


def _record(filename: str) -> dict[str, Any]:
    payload = json.loads((MARKET_DATA_DIR / filename).read_text(encoding="utf-8"))
    records = payload.get("data", {}).get("records", [])
    if not records or not isinstance(records[0], dict):
        raise ValueError(f"No current record in {filename}")
    return records[0]


def current_rule_inputs() -> tuple[str, str, dict[str, int], dict[str, dict[str, Any]]]:
    market = _record("taiwan_market_overview.json")
    feature_date = str(market.get("trade_date", ""))
    target_date = prediction_target_date(feature_date)
    if not feature_date or not target_date:
        raise ValueError("Official next trading day is unavailable")
    records = {
        "market": market,
        "night": _record("night_futures.json"), "sp500": _record("sp500_index.json"),
        "nasdaq": _record("nasdaq_index.json"), "sox": _record("sox_index.json"),
        "tsm": _record("tsm_adr.json"), "vix": _record("vix_index.json"),
    }
    temporal_sources = {
        "market": "taiwan_market", "night": "night_futures", "sp500": "sp500",
        "nasdaq": "nasdaq", "sox": "sox", "tsm": "tsm_adr", "vix": "vix",
    }
    for key, record in records.items():
        source_date = str(record.get("trade_date", ""))
        if not source_date or not source_date_is_valid(
            temporal_sources[key], source_date, feature_date, target_date
        ):
            raise ValueError(
                f"Illegal source date for {key}: {source_date} "
                f"(feature={feature_date}, target={target_date})"
            )

    def resonance(left: float | None, right: float | None, left_band: float, right_band: float) -> int:
        values = (direction(left, left_band), direction(right, right_band))
        return values[0] if values[0] and values[0] == values[1] else 0

    signals = {
        "night_futures_direction": direction(number(records["night"].get("change"))),
        "us_market_direction": resonance(number(records["sp500"].get("change_percent")), number(records["nasdaq"].get("change_percent")), 0.15, 0.2),
        "sox_tsm_adr_resonance": resonance(number(records["sox"].get("change_percent")), number(records["tsm"].get("change_percent")), 0.3, 0.3),
        "vix_risk_off": -direction(number(records["vix"].get("change_percent")), 2.0),
    }
    return feature_date, target_date, signals, records


def ensure_forecast_timeliness(target_date: str, now: datetime | None = None) -> None:
    current = now.astimezone(TAIPEI) if now else datetime.now(TAIPEI)
    cutoff = datetime.combine(date.fromisoformat(target_date), time(9, 0), tzinfo=TAIPEI)
    if current >= cutoff:
        raise ValueError(f"Forecast target cutoff has passed: {target_date} 09:00 Asia/Taipei")


def _load_training_rows(feature_date: str) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for path in (HISTORICAL_PATH, DAILY_DATASET_PATH):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                if row.get("feature_date") and row.get("target_date") and row["target_date"] <= feature_date and number(row.get("next_taiex_return")) is not None:
                    merged[row["feature_date"]] = row
    return [merged[key] for key in sorted(merged)]


def _historical_signal(row: dict[str, str], key: str) -> int:
    if key == "night_futures_direction": return direction(number(row.get("night_futures_change")))
    if key == "vix_risk_off": return -direction(number(row.get("vix_change_percent")), 2.0)
    fields, bands = (("sp500_change_percent", "nasdaq_change_percent"), (0.15, 0.2)) if key == "us_market_direction" else (("sox_change_percent", "tsm_adr_change_percent"), (0.3, 0.3))
    values = [direction(number(row.get(field)), band) for field, band in zip(fields, bands)]
    return values[0] if values[0] and values[0] == values[1] else 0


def dynamic_weights(rows: list[dict[str, str]], rule_keys: list[str]) -> tuple[dict[str, float], dict[str, Any]]:
    quality = {}
    for key in rule_keys:
        hits, yearly = [], defaultdict(list)
        for row in rows:
            forecast, actual = _historical_signal(row, key), direction(number(row.get("next_taiex_return")))
            if not forecast or not actual: continue
            hit = forecast == actual
            hits.append(hit); yearly[row["target_date"][:4]].append(hit)
        accuracy = statistics.fmean(hits) if hits else 0.5
        yearly_accuracy = [statistics.fmean(values) for values in yearly.values() if len(values) >= 10]
        stability = max(0, 1 - 2 * statistics.pstdev(yearly_accuracy)) if len(yearly_accuracy) >= 2 else 0.5
        quality[key] = {"sample_size": len(hits), "accuracy": accuracy, "stability": stability, "raw_weight": max(0, accuracy - 0.5) * stability}
    total = sum(item["raw_weight"] for item in quality.values())
    if total <= 0: raise ValueError("Historical rule performance cannot produce positive weights")
    return {key: quality[key]["raw_weight"] / total * 100 for key in rule_keys}, quality


def generate_forecast() -> dict[str, Any]:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    feature_date, target_date, signals, records = current_rule_inputs()
    ensure_forecast_timeliness(target_date)
    training = _load_training_rows(feature_date)
    if len(training) < int(config["initial_training_samples"]):
        raise ValueError(f"Insufficient completed training samples: {len(training)}")
    keys = list(config["rules"])
    weights, quality = dynamic_weights(training, keys)
    active_weight = sum(weights[key] for key, value in signals.items() if value)
    vote = sum(weights[key] * value for key, value in signals.items() if value) / active_weight if active_weight else 0
    score = max(0.0, min(100.0, 50 + 50 * vote))
    if score <= 20: status, forecast_direction = "extreme", "Strong Bearish"
    elif score >= 80: status, forecast_direction = "extreme", "Strong Bullish"
    else: status, forecast_direction = "low_confidence", "Neutral"
    contributions = {key: {"signal": signals[key], "weight": weights[key], "weighted_vote": weights[key] * signals[key], "display_name": config["rules"][key]["display_name"]} for key in keys}
    return {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "version": config["version"],
        "forecast_type": "statistical_next_day_rule_forecast", "feature_date": feature_date, "target_date": target_date,
        "feature_close": number(records["market"].get("taiex", {}).get("close")),
        "score": score, "max_score": 100, "direction": forecast_direction, "confidence_status": status,
        "training": {"sample_size": len(training), "latest_completed_target_date": training[-1]["target_date"], "weight_method": "expanding history only; accuracy edge adjusted by yearly stability", "rule_quality": quality},
        "weights": weights, "contributions": contributions, "source_dates": {key: value["trade_date"] for key, value in records.items()},
        "historical_validation": config["validation"], "disclaimer": config["validation"]["disclaimer"],
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def update_history(payload: dict[str, Any]) -> tuple[int, int]:
    market_rows = {}
    with MARKET_HISTORY_PATH.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream): market_rows[row["trade_date"]] = row
    rows = {}
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if tuple(reader.fieldnames or ()) != HISTORY_FIELDS: raise ValueError("Unexpected rule forecast history header")
            rows = {row["feature_date"]: row for row in reader}
    validated = 0
    for row in rows.values():
        if row["hit"] or row["target_date"] not in market_rows: continue
        feature_close, target_close = number(market_rows[row["feature_date"]]["taiex_close"]), number(market_rows[row["target_date"]]["taiex_close"])
        if not feature_close or not target_close: continue
        actual_return = (target_close / feature_close - 1) * 100
        actual_direction = "Strong Bullish" if actual_return > 0 else "Strong Bearish"
        row.update(target_close=target_close, actual_return=actual_return, actual_direction=actual_direction, hit=str(row["direction"] == actual_direction).lower() if row["direction"] != "Neutral" else "", validated_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")); validated += 1
    feature_close = number(market_rows.get(payload["feature_date"], {}).get("taiex_close")) or number(payload.get("feature_close"))
    if not feature_close: raise ValueError("Forecast feature close is absent from market history")
    rows[payload["feature_date"]] = {"feature_date": payload["feature_date"], "target_date": payload["target_date"], "score": payload["score"], "direction": payload["direction"], "confidence_status": payload["confidence_status"], "weights": json.dumps(payload["weights"], ensure_ascii=False, separators=(",", ":")), "contributions": json.dumps(payload["contributions"], ensure_ascii=False, separators=(",", ":")), "generated_at": payload["updated_at"], "feature_close": feature_close, "target_close": "", "actual_return": "", "actual_direction": "", "hit": "", "validated_at": ""}
    temporary = HISTORY_PATH.with_suffix(".csv.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HISTORY_FIELDS); writer.writeheader(); writer.writerows(rows[key] for key in sorted(rows))
    os.replace(temporary, HISTORY_PATH)
    return len(rows), validated


def main() -> int:
    try:
        payload = generate_forecast(); rows, validated = update_history(payload); write_json_atomic(OUTPUT_PATH, payload)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Rule forecast skipped safely: {exc}"); return 0
    print(f"Rule forecast updated | feature={payload['feature_date']} | target={payload['target_date']} | score={payload['score']:.2f} | history={rows} | validated={validated}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
