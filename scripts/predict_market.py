"""Generate a next-session market prediction with the trained baseline model."""

from __future__ import annotations

import csv
import json
import logging
import os
import pickle
from pathlib import Path
from typing import Any

from config import PROJECT_ROOT


LOGGER = logging.getLogger("market_prediction")
MODEL_PATH = PROJECT_ROOT / "models" / "baseline_model.pkl"
MARKET_HISTORY_PATH = PROJECT_ROOT / "data" / "history" / "market_daily.csv"
OUTPUT_PATH = PROJECT_ROOT / "data" / "prediction" / "prediction.json"


def predict_market(
    model_path: Path = MODEL_PATH,
    history_path: Path = MARKET_HISTORY_PATH,
    output_path: Path = OUTPUT_PATH,
) -> bool:
    """Load the trained artifact and predict from the latest complete history row."""

    if not model_path.exists():
        LOGGER.warning("Baseline model has not been created: %s", model_path)
        return False
    artifact = _load_artifact(model_path)
    feature_names = artifact.get("feature_names")
    model = artifact.get("model")
    model_version = artifact.get("model_version")
    if not isinstance(feature_names, list) or not feature_names or model is None or not model_version:
        raise ValueError("Baseline model artifact is missing required fields")

    latest = _load_latest_history_row(history_path, feature_names)
    feature_values = [[float(latest[name]) for name in feature_names]]
    probabilities = model.predict_proba(feature_values)[0]
    class_probabilities = {int(label): float(value) for label, value in zip(model.classes_, probabilities)}
    if 0 not in class_probabilities or 1 not in class_probabilities:
        raise ValueError("Baseline model does not contain both direction classes")
    down_probability = class_probabilities[0]
    up_probability = class_probabilities[1]
    direction = "up" if up_probability > down_probability else "down"
    payload = {
        "prediction_date": latest["trade_date"],
        "up_probability": round(up_probability, 6),
        "down_probability": round(down_probability, 6),
        "direction": direction,
        "confidence": round(max(up_probability, down_probability), 6),
        "model_version": str(model_version),
    }
    _write_json_atomic(output_path, payload)
    LOGGER.info("Market prediction written: %s", output_path)
    return True


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
