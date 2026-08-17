"""Generate a next-session market prediction with the trained baseline model."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import pickle
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT
from trading_calendar import get_next_trading_day


LOGGER = logging.getLogger("market_prediction")
MODEL_PATH = PROJECT_ROOT / "models" / "baseline_model.pkl"
CALIBRATION_PATH = PROJECT_ROOT / "models" / "platt_calibrator.pkl"
MARKET_HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "prediction" / "prediction.json"


def predict_market(
    model_path: Path = MODEL_PATH,
    history_path: Path = MARKET_HISTORY_PATH,
    output_path: Path = OUTPUT_PATH,
    calibration_path: Path = CALIBRATION_PATH,
) -> bool:
    """Load the trained artifact and predict from the latest complete history row."""

    if not model_path.exists():
        LOGGER.warning("Baseline model has not been created: %s", model_path)
        return False
    artifact = _load_artifact(model_path)
    feature_names = artifact.get("feature_names")
    model = artifact.get("model")
    model_version = artifact.get("model_version")
    if (
        not isinstance(feature_names, list)
        or len(feature_names) != 15
        or len(set(feature_names)) != 15
        or any(not isinstance(name, str) or not name for name in feature_names)
        or model is None
        or not model_version
    ):
        raise ValueError("Baseline model artifact is missing required fields")

    latest = _load_latest_history_row(history_path, feature_names)
    feature_values = [[float(latest[name]) for name in feature_names]]
    probabilities = model.predict_proba(feature_values)[0]
    class_probabilities = {int(label): float(value) for label, value in zip(model.classes_, probabilities)}
    if 0 not in class_probabilities or 1 not in class_probabilities:
        raise ValueError("Baseline model does not contain both direction classes")
    raw_down_probability = class_probabilities[0]
    raw_up_probability = class_probabilities[1]
    if abs(raw_up_probability + raw_down_probability - 1.0) > 1e-9:
        raise ValueError("Baseline model probabilities do not sum to one")
    calibrated_up_probability, calibration_status = _calibrate_probability(
        raw_up_probability, calibration_path, str(model_version)
    )
    if calibrated_up_probability is None:
        effective_up_probability = raw_up_probability
        calibrated_down_probability = None
        calibration_method = "none"
    else:
        effective_up_probability = calibrated_up_probability
        calibrated_down_probability = 1 - calibrated_up_probability
        calibration_method = "platt"
    effective_down_probability = 1 - effective_up_probability
    direction = "up" if effective_up_probability > effective_down_probability else "down"
    payload = {
        "feature_date": latest["trade_date"],
        "target_date": get_next_trading_day(latest["trade_date"]),
        "raw_up_probability": round(raw_up_probability, 6),
        "calibrated_up_probability": round(calibrated_up_probability, 6) if calibrated_up_probability is not None else None,
        "calibrated_down_probability": round(calibrated_down_probability, 6) if calibrated_down_probability is not None else None,
        "up_probability": round(effective_up_probability, 6),
        "down_probability": round(effective_down_probability, 6),
        "direction": direction,
        "confidence": round(max(effective_up_probability, effective_down_probability), 6),
        "calibration_method": calibration_method,
        "calibration_status": calibration_status,
        "model_version": str(model_version),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    _write_json_atomic(output_path, payload)
    LOGGER.info("Market prediction written: %s", output_path)
    return True


def _calibrate_probability(
    raw_probability: float,
    path: Path,
    model_version: str,
) -> tuple[float | None, str]:
    """Return a validated Platt probability or an explicit raw fallback."""

    if not path.exists():
        LOGGER.warning("Platt calibration artifact is not available; using raw probability")
        return None, "uncalibrated_fallback_missing_artifact"
    try:
        artifact = _load_artifact(path)
        if (
            artifact.get("calibration_method") != "platt"
            or artifact.get("model_version") != model_version
            or artifact.get("input") != "raw_up_probability_logit"
            or artifact.get("calibrator") is None
        ):
            raise ValueError("Platt calibration artifact metadata is invalid")
        calibrator = artifact["calibrator"]
        probabilities = calibrator.predict_proba([[_probability_logit(raw_probability)]])[0]
        indexes = {int(label): index for index, label in enumerate(calibrator.classes_)}
        calibrated = float(probabilities[indexes[1]])
        if not math.isfinite(calibrated) or not 0 <= calibrated <= 1:
            raise ValueError("Platt calibration output is invalid")
        return calibrated, "calibrated"
    except (AttributeError, EOFError, KeyError, OSError, pickle.UnpicklingError, RuntimeError, ValueError) as exc:
        LOGGER.error("Platt calibration failed; using raw probability: %s", exc)
        return None, "uncalibrated_fallback_invalid_artifact"


def _probability_logit(probability: float) -> float:
    clipped = min(1 - 1e-15, max(1e-15, probability))
    return math.log(clipped / (1 - clipped))


def _load_artifact(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            artifact = pickle.load(source)
    except ModuleNotFoundError as exc:
        raise RuntimeError("Model dependency is missing; run: pip install -r requirements.txt") from exc
    if not isinstance(artifact, dict):
        raise ValueError("Invalid baseline model artifact")
    return artifact


def _load_latest_history_row(path: Path, feature_names: list[str]) -> dict[str, str]:
    try:
        source = path.open("r", encoding="utf-8", newline="")
    except FileNotFoundError as exc:
        raise ValueError(f"Market history not found: {path}") from exc
    with source:
        reader = csv.DictReader(source)
        required = {"trade_date", *feature_names}
        missing = sorted(required.difference(reader.fieldnames or ()))
        if missing:
            raise ValueError(f"Market history is missing columns: {', '.join(missing)}")
        rows = list(reader)
    if not rows:
        raise ValueError("Market history does not contain any data rows")
    latest = rows[-1]
    if not str(latest.get("trade_date", "")).strip():
        raise ValueError("Latest market history row is missing trade_date")
    for name in feature_names:
        value = str(latest.get(name, "")).strip()
        if not value:
            raise ValueError(f"Latest market history row is missing {name}")
        try:
            float(value)
        except ValueError as exc:
            raise ValueError(f"Latest market history value is invalid: {name}") from exc
    return latest


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    LOGGER.info("Market prediction started")
    try:
        predicted = predict_market()
    except (OSError, RuntimeError, ValueError) as exc:
        LOGGER.error("Market prediction failed: %s", exc)
        return 1
    if not predicted:
        LOGGER.info("Market prediction finished without creating fake output")
        return 0
    LOGGER.info("Market prediction finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
