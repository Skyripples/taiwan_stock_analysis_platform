"""Independent append-only DAY 08:15 shadow prediction runner.

This experimental runner uses the DAY dataset only for revealed historical
labels and the immutable DAY live snapshot for inference.  It is not a live
trading signal and has no scheduler or UI integration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import numpy as np

from config import PROJECT_ROOT
from research import build_futures_day_dataset as day
from research import run_futures_b4_baselines as baselines
from research import run_futures_day_baselines as day_baselines
from research import run_futures_b4_forward_prediction as b4_forward


VERSION = "v5.day-forward-1.1"
TAIPEI = ZoneInfo("Asia/Taipei")
MODEL_NAMES = ("dummy", "logistic", "hist_gradient_boosting")
RETRAIN_SESSIONS = 21
MIN_TRAINING_ROWS = 504
DEFAULT_STATE_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "day_0815_forward"
EXPERIMENTAL_NOTICE = "experimental shadow prediction; no demonstrated predictive advantage; not a trading instruction"


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=day.OUTPUT_ROOT)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--mode", choices=("live", "replay"), default="live")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Validate snapshot/training eligibility without fitting or writing.")
    parser.add_argument("--evaluate-only", action="store_true", help="Append only newly revealed labels to existing timely predictions.")
    parser.add_argument("--evaluate", action="store_true", help="Append revealed evaluations after prediction processing.")
    return parser.parse_args()


def _aware(value: Any, field: str) -> datetime:
    return b4_forward._aware(value, field)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _day(value: Any) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def normalize_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    prediction_date = _day(payload.get("prediction_date"))
    cutoff = _aware(payload.get("prediction_at"), "prediction_at")
    if cutoff.astimezone(TAIPEI).date() != prediction_date or cutoff.astimezone(TAIPEI).time() != time(8, 15):
        raise ValueError("DAY snapshot prediction_at must be prediction_date 08:15 Asia/Taipei")
    generated = _aware(payload.get("generated_at"), "generated_at")
    if payload.get("status") == "unavailable":
        return {"prediction_date": prediction_date, "prediction_at": cutoff, "generated_at": generated,
                "contract": None, "features": {}, "missing_columns": list(day.FEATURE_COLUMNS),
                "declared_unavailable": str(payload.get("unavailable_reason") or "source_unavailable"),
                "source_hash": _hash(payload), "max_feature_available_at": None, "source_fetched_at": None}
    available = _aware(payload.get("max_feature_available_at"), "max_feature_available_at")
    fetched = _aware(payload.get("source_fetched_at"), "source_fetched_at")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("DAY snapshot must retain source provenance")
    availability = []
    acquisitions = []
    for source in sources:
        if not isinstance(source, Mapping) or not all(key in source for key in ("path", "sha256", "available_at", "fetched_at")):
            raise ValueError("DAY snapshot source provenance is incomplete")
        availability.append(_aware(source["available_at"], "source available_at"))
        acquisitions.append(_aware(source["fetched_at"], "source fetched_at"))
    if max(availability) > available or max(acquisitions) > fetched:
        raise ValueError("DAY snapshot max source timestamps understate provenance")
    if max(availability) > cutoff or max(acquisitions) > cutoff:
        raise ValueError("DAY snapshot has source evidence after 08:15 cutoff")
    if fetched > generated:
        raise ValueError("DAY snapshot includes data not locally fetched at generated_at")
    feature_times = payload.get("feature_available_at")
    if not isinstance(feature_times, Mapping) or set(feature_times) != set(day.FEATURE_COLUMNS):
        raise ValueError("DAY snapshot must preserve per-feature availability")
    features: dict[str, float | None] = {}
    missing = []
    for name in day.FEATURE_COLUMNS:
        value = payload.get(name)
        if name not in payload:
            missing.append(name)
            continue
        feature_available = _aware(feature_times[name], f"{name} availability")
        if feature_available > cutoff:
            raise ValueError(f"DAY snapshot feature {name} is after 08:15 cutoff")
        if value is None:
            features[name] = None
        elif isinstance(value, bool):
            features[name] = float(value)
        elif isinstance(value, (int, float)) and np.isfinite(value):
            features[name] = float(value)
        else:
            raise ValueError(f"DAY snapshot feature {name} must be finite or null")
    contract = payload.get("contract")
    if not isinstance(contract, str) or not contract:
        raise ValueError("DAY snapshot contract is required")
    return {"prediction_date": prediction_date, "prediction_at": cutoff, "generated_at": generated,
            "contract": contract, "features": features, "missing_columns": missing, "source_hash": _hash(payload),
            "max_feature_available_at": available, "source_fetched_at": fetched}


def snapshot_status(snapshot: Mapping[str, Any], mode: str) -> tuple[str, str | None]:
    if snapshot.get("declared_unavailable"):
        return "unavailable", str(snapshot["declared_unavailable"])
    if snapshot["missing_columns"]:
        return "unavailable", "missing_feature_columns:" + ",".join(snapshot["missing_columns"])
    if mode == "replay":
        return "replay", None
    if snapshot["generated_at"] > snapshot["prediction_at"]:
        return "late", "generated_after_day_0815_cutoff"
    if snapshot["generated_at"] < snapshot["prediction_at"]:
        return "early", None
    return "on_time", None


def eligible_training_rows(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if row["prediction_at"] < cutoff
            and row.get("feature_pit_status") == "passed_0815"
            and row.get(day.BINARY_COLUMN) in (0, 1)
            and row.get("target_available_at") is not None
            and _aware(row["target_available_at"], "target_available_at") <= cutoff]


def sessions_since(fit_at: datetime, cutoff: datetime) -> int:
    return b4_forward.taifex_sessions_since(fit_at, cutoff)


def _artifact_path(root: Path, model: str, trained_at: datetime, training_hash: str) -> Path:
    return root / "artifacts" / f"day_0815_{model}_trained-{trained_at.astimezone(TAIPEI).date().isoformat()}_{training_hash[:12]}.pkl"


def _load_reusable(root: Path, cutoff: datetime, identity: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], Path]] | None:
    groups: dict[tuple[str, str], dict[str, tuple[dict[str, Any], Path]]] = {}
    for path in (root / "artifacts").glob("day_0815_*.pkl") if (root / "artifacts").exists() else ():
        try:
            with path.open("rb") as source:
                bundle = pickle.load(source)
            fit_at = _aware(bundle.get("training_label_cutoff_at", bundle.get("fit_at")), "artifact training label cutoff")
        except (OSError, pickle.UnpicklingError, ValueError, AttributeError):
            continue
        if (bundle.get("version") != VERSION or bundle.get("data_identity") != dict(identity)
                or bundle.get("model") not in MODEL_NAMES or fit_at >= cutoff):
            continue
        groups.setdefault((fit_at.isoformat(), str(bundle.get("training_hash"))), {})[bundle["model"]] = (bundle, path)
    complete = [(key, values) for key, values in groups.items() if set(values) == set(MODEL_NAMES)]
    if not complete:
        return None
    _, models = max(complete, key=lambda item: item[0][0])
    fit_at = _aware(next(iter(models.values()))[0]["fit_at"], "artifact fit_at")
    return models if sessions_since(fit_at, cutoff) < RETRAIN_SESSIONS else None


def _fit_model(name: str, rows: list[dict[str, Any]], trained_at: datetime,
               training_label_cutoff_at: datetime, prediction_cutoff_at: datetime,
               identity: Mapping[str, Any], training_hash: str) -> dict[str, Any]:
    models = baselines._models()
    estimator = models[name]
    target = np.asarray([row[day.BINARY_COLUMN] for row in rows], dtype=int)
    raw = baselines._matrix(rows, day.FEATURE_COLUMNS)
    preprocessor = None
    if name != "dummy":
        preprocessor = baselines._preprocessor()
        raw = preprocessor.fit_transform(raw)
    estimator.fit(raw, target)
    return {"version": VERSION, "model": name,
            "trained_at": trained_at.isoformat(),
            "training_label_cutoff_at": training_label_cutoff_at.isoformat(),
            "prediction_cutoff_at": prediction_cutoff_at.isoformat(),
            # fit_at is retained only to read the first append-only prototype artifacts.
            "fit_at": training_label_cutoff_at.isoformat(), "training_rows": len(rows),
            "trained_through": rows[-1]["prediction_date"].isoformat(), "training_hash": training_hash,
            "data_identity": dict(identity), "feature_columns": list(day.FEATURE_COLUMNS),
            "preprocessor": preprocessor, "estimator": estimator}


def load_or_fit_models(rows: list[dict[str, Any]], cutoff: datetime, state_root: Path,
                       identity: Mapping[str, Any], *, trained_at: datetime | None = None,
                       dry_run: bool = False) -> tuple[dict[str, dict[str, Any]], dict[str, Path], dict[str, str], bool]:
    reusable = _load_reusable(state_root, cutoff, identity)
    if reusable:
        return ({name: bundle for name, (bundle, _) in reusable.items()}, {name: path for name, (_, path) in reusable.items()}, {}, False)
    trained_at = (trained_at or datetime.now(TAIPEI)).astimezone(TAIPEI)
    training_label_cutoff_at = min(trained_at, cutoff)
    train = eligible_training_rows(rows, training_label_cutoff_at)
    if len(train) < MIN_TRAINING_ROWS:
        raise ValueError(f"requires at least {MIN_TRAINING_ROWS} revealed non-FLAT DAY labels before cutoff")
    training_hash = _hash({"dates": [row["prediction_date"].isoformat() for row in train],
                           "labels": [row[day.BINARY_COLUMN] for row in train], "dataset": identity,
                           "features": list(day.FEATURE_COLUMNS)})
    if dry_run:
        return {}, {}, {}, False
    bundles: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    errors: dict[str, str] = {}
    fitted = False
    for name in MODEL_NAMES:
        path = _artifact_path(state_root, name, trained_at, training_hash)
        try:
            if path.exists():
                with path.open("rb") as source:
                    bundle = pickle.load(source)
                if bundle.get("training_hash") != training_hash:
                    raise ValueError("artifact training identity mismatch")
            else:
                bundle = _fit_model(name, train, trained_at, training_label_cutoff_at, cutoff, identity, training_hash)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                with temporary.open("xb") as target:
                    pickle.dump(bundle, target)
                os.replace(temporary, path)
                fitted = True
            bundles[name], paths[name] = bundle, path
        except Exception as error:
            errors[name] = f"fit_or_load_failed:{type(error).__name__}:{error}"
    return bundles, paths, errors, fitted


def prediction_records(snapshot: Mapping[str, Any], status: str, reason: str | None,
                       bundles: Mapping[str, Mapping[str, Any]], artifacts: Mapping[str, Path],
                       errors: Mapping[str, str]) -> list[dict[str, Any]]:
    base = {"prediction_date": snapshot["prediction_date"].isoformat(), "contract": snapshot["contract"],
            "prediction_cutoff_at": snapshot["prediction_at"].isoformat(),
            # cutoff remains as a compatibility alias for earlier preview tooling.
            "cutoff": snapshot["prediction_at"].isoformat(), "generated_at": snapshot["generated_at"].isoformat(),
            "snapshot_hash": snapshot["source_hash"], "feature_available_at": snapshot["max_feature_available_at"].isoformat() if snapshot["max_feature_available_at"] else None,
            "version": VERSION, "notice": EXPERIMENTAL_NOTICE}
    raw = np.asarray([[snapshot["features"].get(name, np.nan) if snapshot["features"].get(name) is not None else np.nan
                       for name in day.FEATURE_COLUMNS]]) if status != "unavailable" else None
    records = []
    for name in MODEL_NAMES:
        bundle = bundles.get(name)
        if status == "unavailable" or bundle is None:
            records.append({**base, "model": name, "status": "unavailable" if status == "unavailable" else status,
                            "unavailable_reason": reason or errors.get(name) or "model_unavailable", "probability_up": None,
                            "direction": None, "model_artifact": None, "trained_at": None,
                            "training_label_cutoff_at": None, "training_hash": None})
            continue
        try:
            matrix = raw if bundle["preprocessor"] is None else bundle["preprocessor"].transform(raw)
            probabilities = bundle["estimator"].predict_proba(matrix)[0]
            classes = list(bundle["estimator"].classes_)
            probability = float(probabilities[classes.index(1)]) if 1 in classes else 0.0
            records.append({**base, "model": name, "status": status, "unavailable_reason": reason,
                            "probability_up": probability, "direction": "UP" if probability >= .5 else "DOWN",
                            "model_artifact": str(artifacts.get(name, "")) or None,
                            "trained_at": bundle.get("trained_at", bundle.get("fit_at")),
                            "training_label_cutoff_at": bundle.get("training_label_cutoff_at", bundle.get("fit_at")),
                            "training_hash": bundle["training_hash"]})
        except Exception as error:
            records.append({**base, "model": name, "status": "unavailable",
                            "unavailable_reason": f"predict_failed:{type(error).__name__}:{error}", "probability_up": None,
                            "direction": None, "model_artifact": str(artifacts.get(name, "")) or None,
                            "trained_at": bundle.get("trained_at", bundle.get("fit_at")),
                            "training_label_cutoff_at": bundle.get("training_label_cutoff_at", bundle.get("fit_at")),
                            "training_hash": bundle.get("training_hash")})
    return records


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return b4_forward._read_jsonl(path)


def evaluate_records(predictions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {row["prediction_date"].isoformat(): row[day.BINARY_COLUMN] for row in rows}
    output = []
    for prediction in predictions:
        label = labels.get(prediction.get("prediction_date"))
        if prediction.get("status") not in {"early", "on_time", "timely"} or label not in (0, 1) or prediction.get("probability_up") is None:
            continue
        probability = float(prediction["probability_up"])
        output.append({"prediction_date": prediction["prediction_date"], "model": prediction["model"], "label": label,
                       "probability_up": probability, "correct": int((probability >= .5) == bool(label)),
                       "brier": (probability - label) ** 2,
                       "log_loss": -(label * np.log(max(probability, 1e-15)) + (1 - label) * np.log(max(1 - probability, 1e-15))),
                       "prediction_reference": _hash({key: prediction.get(key) for key in ("prediction_date", "model", "snapshot_hash", "training_hash")})})
    return output


def append_new_evaluations(state_root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    predictions = _read_jsonl(state_root / "predictions.jsonl")
    existing = {row.get("prediction_reference") for row in _read_jsonl(state_root / "evaluations.jsonl")}
    additions = [row for row in evaluate_records(predictions, rows) if row["prediction_reference"] not in existing]
    if additions:
        b4_forward.append_immutable(state_root / "evaluations.jsonl", additions, ("prediction_reference",))
    return {"new_records": len(additions), "path": str(state_root / "evaluations.jsonl")}


def preflight() -> dict[str, Any]:
    return {"ready": False, "status": "blocked", "models": list(MODEL_NAMES), "cutoff": "08:15 Asia/Taipei",
            "blockers": ["no DAY 08:15 scheduler is installed", "requires immutable snapshot source available_at and fetched_at evidence by cutoff"],
            "required": ["run DAY snapshot dry-run before cutoff", "ensure at least 504 revealed non-FLAT DAY labels", "keep state root independent from B4"]}


def run(options: argparse.Namespace) -> dict[str, Any]:
    if options.preflight:
        return {"version": VERSION, "preflight": preflight()}
    rows, identity = day_baselines.load_day_rows(options.dataset)
    if options.evaluate_only:
        if options.dry_run:
            return {"version": VERSION, "dry_run": True, "would_evaluate": len(evaluate_records(_read_jsonl(options.state_root / "predictions.jsonl"), rows))}
        return {"version": VERSION, "evaluation": append_new_evaluations(options.state_root, rows)}
    if options.snapshot is None:
        raise ValueError("--snapshot is required unless --preflight or --evaluate-only is used")
    snapshot = normalize_snapshot(json.loads(options.snapshot.read_text(encoding="utf-8")))
    status, reason = snapshot_status(snapshot, options.mode)
    eligible = eligible_training_rows(rows, snapshot["prediction_at"])
    if options.dry_run:
        return {"version": VERSION, "dry_run": True, "status": status, "reason": reason,
                "revealed_training_rows": len(eligible), "minimum_training_rows": MIN_TRAINING_ROWS,
                "would_train": status != "unavailable" and len(eligible) >= MIN_TRAINING_ROWS,
                "models": list(MODEL_NAMES)}
    bundles: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, Path] = {}
    errors: dict[str, str] = {}
    fitted = False
    if status != "unavailable":
        try:
            bundles, artifacts, errors, fitted = load_or_fit_models(
                rows, snapshot["prediction_at"], options.state_root, identity,
                trained_at=datetime.now(TAIPEI),
            )
        except ValueError as error:
            status, reason = "unavailable", f"training_unavailable:{error}"
    records = prediction_records(snapshot, status, reason, bundles, artifacts, errors)
    path = options.state_root / "predictions.jsonl"
    existing = {tuple(row.get(key) for key in ("prediction_date", "model")) for row in _read_jsonl(path)}
    additions = [record for record in records if (record["prediction_date"], record["model"]) not in existing]
    if additions:
        b4_forward.append_immutable(path, additions, ("prediction_date", "model"))
    result = {"version": VERSION, "status": status, "reason": reason, "records": len(records), "new_records": len(additions),
              "model_fitted": fitted, "model_errors": errors, "prediction_path": str(path), "notice": EXPERIMENTAL_NOTICE}
    if options.evaluate:
        result["evaluation"] = append_new_evaluations(options.state_root, rows)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({"version": VERSION, "status": "error", "error": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
