"""Create immutable, experimental B4 forward-prediction records.

This command intentionally does not build daily features.  A caller must
provide a separately captured 08:30 feature snapshot with a timezone-aware
``generated_at``.  This prevents a post-close B4 research row from being
presented as a timely prediction.
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
from research.build_futures_b4_same_day_dataset import BINARY_COLUMN, FEATURE_COLUMNS, OUTPUT_ROOT
from research.run_futures_b4_baselines import _matrix, _preprocessor, load_b4_rows
from taifex_trading_calendar import get_taifex_trading_days_in_range


VERSION = "v5.b4-forward-1.1"
TAIPEI = ZoneInfo("Asia/Taipei")
RETRAIN_SESSIONS = 21
MIN_TRAINING_ROWS = 504
DEFAULT_STATE_ROOT = PROJECT_ROOT / "data_lake" / "futures_prediction" / "b4_forward"
EXPERIMENTAL_NOTICE = "experimental; no demonstrated predictive advantage; not a trading instruction"
MODEL_NAMES = ("dummy", "logistic", "hist_gradient_boosting")


def _aware(value: Any, field: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _day(value: Any, field: str) -> date:
    parsed = value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])
    return parsed


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--snapshot", type=Path, help="JSON feature snapshot captured for this prediction")
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--mode", choices=("live", "replay"), default="live")
    parser.add_argument("--preflight", action="store_true", help="Only report current daily-feature live readiness")
    parser.add_argument("--evaluate", action="store_true", help="Append evaluations for revealed labels; never changes predictions")
    parser.add_argument("--evaluate-only", action="store_true", help="Evaluate prior timely predictions without creating a new prediction")
    return parser.parse_args()


def live_readiness() -> dict[str, Any]:
    """Document the deployment boundary rather than claiming live readiness."""
    return {
        "ready": False,
        "status": "blocked",
        "blockers": [
            "no pre-08:30 scheduler has been installed for build_futures_b4_live_snapshot.py followed by this runner",
            "a live snapshot remains unavailable whenever its selected TX staging source lacks fetched_at/source hash evidence before the snapshot completes",
        ],
        "required_before_live": [
            "schedule the independent snapshot writer early enough that generated_at is no later than prediction_at",
            "retain and audit the source fetched_at/available_at evidence recorded by each immutable snapshot",
        ],
        "current_tool_behavior": "the independent snapshot writer writes unavailable rather than substituting a post-close B4 dataset row",
        "models": list(MODEL_NAMES),
    }


def normalize_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a captured feature snapshot without imputing any field."""
    day = _day(payload.get("prediction_date"), "prediction_date")
    cutoff = _aware(payload.get("prediction_at"), "prediction_at")
    if cutoff.astimezone(TAIPEI).date() != day or cutoff.astimezone(TAIPEI).time() != time(8, 30):
        raise ValueError("snapshot prediction_at must be prediction_date 08:30 Asia/Taipei")
    generated = _aware(payload.get("generated_at"), "generated_at")
    if payload.get("status") == "unavailable":
        return {
            "prediction_date": day, "prediction_at": cutoff, "generated_at": generated, "contract": None,
            "max_feature_available_at": None, "source_fetched_at": None, "features": {},
            "missing_columns": list(FEATURE_COLUMNS), "source_hash": _hash(payload),
            "declared_unavailable": str(payload.get("unavailable_reason") or "source_unavailable"),
        }
    available = _aware(payload.get("max_feature_available_at"), "max_feature_available_at")
    source_fetched = _aware(payload.get("source_fetched_at"), "source_fetched_at")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("snapshot must retain non-empty source provenance")
    source_availability = []
    source_fetches = []
    for source in sources:
        if not isinstance(source, Mapping) or not all(key in source for key in ("path", "sha256", "source_date", "available_at", "fetched_at")):
            raise ValueError("snapshot source provenance is incomplete")
        source_availability.append(_aware(source["available_at"], "source available_at"))
        source_fetches.append(_aware(source["fetched_at"], "source fetched_at"))
    if max(source_availability) > available:
        raise ValueError("max_feature_available_at understates source availability")
    if max(source_fetches) > source_fetched:
        raise ValueError("source_fetched_at understates source acquisition")
    if source_fetched > generated:
        raise ValueError("snapshot includes a source not locally acquired at generated_at")
    feature_times = payload.get("feature_available_at")
    if not isinstance(feature_times, Mapping) or set(feature_times) != set(FEATURE_COLUMNS):
        raise ValueError("snapshot must retain availability for every TX Core feature")
    for name in FEATURE_COLUMNS:
        feature_available = _aware(feature_times[name], f"{name} availability")
        if feature_available > available or feature_available > cutoff:
            raise ValueError(f"snapshot feature {name} is not available at cutoff")
    contract = payload.get("contract") or payload.get("tx_contract") or payload.get("target_contract")
    if not isinstance(contract, str) or not contract:
        raise ValueError("snapshot contract is required")
    values: dict[str, float | None] = {}
    missing_columns = []
    for name in FEATURE_COLUMNS:
        if name not in payload:
            missing_columns.append(name)
            continue
        value = payload[name]
        if value is None:
            values[name] = None
        elif isinstance(value, bool):
            values[name] = float(value)
        elif isinstance(value, (int, float)) and np.isfinite(value):
            values[name] = float(value)
        else:
            raise ValueError(f"snapshot feature {name} is not finite numeric or null")
    return {
        "prediction_date": day, "prediction_at": cutoff, "generated_at": generated,
        "max_feature_available_at": available, "source_fetched_at": source_fetched, "contract": contract, "features": values,
        "missing_columns": missing_columns,
        "source_hash": _hash(payload),
    }


def snapshot_status(snapshot: Mapping[str, Any], mode: str) -> tuple[str, str | None]:
    if snapshot.get("declared_unavailable"):
        return "unavailable", str(snapshot["declared_unavailable"])
    if snapshot["missing_columns"]:
        return "unavailable", "missing_feature_columns:" + ",".join(snapshot["missing_columns"])
    if snapshot["max_feature_available_at"] > snapshot["prediction_at"]:
        return "unavailable", "feature_available_after_cutoff"
    if mode == "replay":
        return "replay", None
    if snapshot["generated_at"] > snapshot["prediction_at"]:
        return "late", "generated_after_prediction_cutoff"
    return "timely", None


def eligible_training_rows(rows: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    return [row for row in rows if row["prediction_at"] < cutoff and row.get("feature_available")
            and row.get(BINARY_COLUMN) in (0, 1) and row.get("target_available_at") is not None
            and _aware(row["target_available_at"], "target_available_at") <= cutoff]


def _artifact_path(root: Path, cutoff: datetime, training_hash: str) -> Path:
    return root / "models" / f"b4_core_fit-{cutoff.astimezone(TAIPEI).date().isoformat()}_{training_hash[:12]}.pkl"


def _fit_bundle(train_rows: list[dict[str, Any]], cutoff: datetime, data_identity: Mapping[str, Any]) -> dict[str, Any]:
    from sklearn.dummy import DummyClassifier
    from sklearn.linear_model import LogisticRegression

    target = np.asarray([row[BINARY_COLUMN] for row in train_rows], dtype=int)
    matrix = _matrix(train_rows, FEATURE_COLUMNS)
    preprocessor = _preprocessor()
    transformed = preprocessor.fit_transform(matrix)
    logistic = LogisticRegression(C=1.0, max_iter=1000, solver="lbfgs", random_state=0).fit(transformed, target)
    dummy = DummyClassifier(strategy="prior", random_state=0).fit(matrix, target)
    training_identity = {
        "prediction_dates": [row["prediction_date"].isoformat() for row in train_rows],
        "labels": target.tolist(), "dataset": data_identity, "feature_columns": list(FEATURE_COLUMNS),
    }
    return {
        "version": VERSION, "feature_columns": list(FEATURE_COLUMNS), "fit_at": cutoff.isoformat(),
        "trained_through": train_rows[-1]["prediction_date"].isoformat(), "training_rows": len(train_rows),
        "training_hash": _hash(training_identity), "data_identity": dict(data_identity),
        "preprocessor": preprocessor, "logistic": logistic, "dummy": dummy,
    }


def _latest_bundle(root: Path, cutoff: datetime) -> tuple[dict[str, Any], Path] | None:
    latest: tuple[datetime, dict[str, Any], Path] | None = None
    for path in (root / "models").glob("*.pkl") if (root / "models").exists() else ():
        try:
            with path.open("rb") as source:
                bundle = pickle.load(source)
            fit_at = _aware(bundle.get("fit_at"), "saved model fit_at")
        except (OSError, pickle.UnpicklingError, ValueError, AttributeError):
            continue
        if fit_at >= cutoff or bundle.get("version") != VERSION:
            continue
        if latest is None or fit_at > latest[0]:
            latest = (fit_at, bundle, path)
    return (latest[1], latest[2]) if latest else None


def taifex_sessions_since(fit_at: datetime, cutoff: datetime) -> int:
    sessions, _ = get_taifex_trading_days_in_range(fit_at.astimezone(TAIPEI).date(), cutoff.astimezone(TAIPEI).date())
    return sum(day > fit_at.astimezone(TAIPEI).date() for day in sessions)


def load_or_fit_bundle(rows: list[dict[str, Any]], cutoff: datetime, state_root: Path,
                       data_identity: Mapping[str, Any]) -> tuple[dict[str, Any], Path, bool]:
    existing = _latest_bundle(state_root, cutoff)
    if (existing and existing[0].get("data_identity") == dict(data_identity)
            and taifex_sessions_since(_aware(existing[0]["fit_at"], "saved model fit_at"), cutoff) < RETRAIN_SESSIONS):
        return existing[0], existing[1], False
    train_rows = eligible_training_rows(rows, cutoff)
    if len(train_rows) < MIN_TRAINING_ROWS:
        raise ValueError(f"requires at least {MIN_TRAINING_ROWS} revealed eligible labels before cutoff")
    identity = {"prediction_dates": [row["prediction_date"].isoformat() for row in train_rows],
                "labels": [row[BINARY_COLUMN] for row in train_rows], "dataset": data_identity,
                "feature_columns": list(FEATURE_COLUMNS)}
    training_hash = _hash(identity)
    path = _artifact_path(state_root, cutoff, training_hash)
    if path.exists():
        with path.open("rb") as source:
            bundle = pickle.load(source)
        if bundle.get("training_hash") != training_hash:
            raise ValueError("saved model artifact training identity mismatch")
        return bundle, path, False
    bundle = _fit_bundle(train_rows, cutoff, data_identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("xb") as target:
        pickle.dump(bundle, target)
    os.replace(temporary, path)
    return bundle, path, True


def _probability_up(model: Any, matrix: np.ndarray) -> float:
    probabilities = model.predict_proba(matrix)[0]
    classes = list(model.classes_)
    return float(probabilities[classes.index(1)]) if 1 in classes else 0.0


def _model_artifact_path(root: Path, model: str, cutoff: datetime, training_hash: str) -> Path:
    return root / "models" / f"b4_{model}_fit-{cutoff.astimezone(TAIPEI).date().isoformat()}_{training_hash[:12]}.pkl"


def _fit_single_model(model_name: str, train_rows: list[dict[str, Any]], cutoff: datetime,
                      data_identity: Mapping[str, Any], training_hash: str) -> dict[str, Any]:
    from sklearn.dummy import DummyClassifier
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    target = np.asarray([row[BINARY_COLUMN] for row in train_rows], dtype=int)
    raw = _matrix(train_rows, FEATURE_COLUMNS)
    preprocessor = None
    if model_name == "dummy":
        estimator = DummyClassifier(strategy="prior", random_state=0).fit(raw, target)
    elif model_name == "logistic":
        preprocessor = _preprocessor()
        estimator = LogisticRegression(C=1.0, max_iter=1000, solver="lbfgs", random_state=0).fit(preprocessor.fit_transform(raw), target)
    elif model_name == "hist_gradient_boosting":
        preprocessor = _preprocessor()
        estimator = HistGradientBoostingClassifier(
            learning_rate=0.05, max_iter=100, max_leaf_nodes=15,
            l2_regularization=1.0, early_stopping=False, random_state=0,
        ).fit(preprocessor.fit_transform(raw), target)
    else:
        raise ValueError(f"Unknown B4 forward model: {model_name}")
    return {
        "version": VERSION, "model": model_name, "feature_columns": list(FEATURE_COLUMNS),
        "fit_at": cutoff.isoformat(), "trained_through": train_rows[-1]["prediction_date"].isoformat(),
        "training_rows": len(train_rows), "training_hash": training_hash, "data_identity": dict(data_identity),
        "preprocessor": preprocessor, "estimator": estimator,
    }


def _shared_saved_models(root: Path, cutoff: datetime, data_identity: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], Path]] | None:
    grouped: dict[tuple[str, str], dict[str, tuple[dict[str, Any], Path]]] = {}
    for path in (root / "models").glob("b4_*.pkl") if (root / "models").exists() else ():
        try:
            with path.open("rb") as source:
                bundle = pickle.load(source)
            model = bundle.get("model")
            fit_at = _aware(bundle.get("fit_at"), "saved model fit_at")
        except (OSError, pickle.UnpicklingError, ValueError, AttributeError):
            continue
        if model not in MODEL_NAMES or bundle.get("version") != VERSION or bundle.get("data_identity") != dict(data_identity) or fit_at >= cutoff:
            continue
        grouped.setdefault((fit_at.isoformat(), str(bundle.get("training_hash"))), {})[model] = (bundle, path)
    complete = [(key, models) for key, models in grouped.items() if set(models) == set(MODEL_NAMES)]
    if not complete:
        return None
    key, models = max(complete, key=lambda item: item[0][0])
    fit_at = _aware(next(iter(models.values()))[0]["fit_at"], "saved model fit_at")
    return models if taifex_sessions_since(fit_at, cutoff) < RETRAIN_SESSIONS else None


def load_or_fit_models(rows: list[dict[str, Any]], cutoff: datetime, state_root: Path,
                       data_identity: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Path], dict[str, str], bool]:
    """Use one shared TAIFEX-session retrain context, with isolated models."""
    reusable = _shared_saved_models(state_root, cutoff, data_identity)
    if reusable:
        return ({name: bundle for name, (bundle, _) in reusable.items()},
                {name: path for name, (_, path) in reusable.items()}, {}, False)
    train_rows = eligible_training_rows(rows, cutoff)
    if len(train_rows) < MIN_TRAINING_ROWS:
        raise ValueError(f"requires at least {MIN_TRAINING_ROWS} revealed eligible labels before cutoff")
    identity = {"prediction_dates": [row["prediction_date"].isoformat() for row in train_rows],
                "labels": [row[BINARY_COLUMN] for row in train_rows], "dataset": data_identity,
                "feature_columns": list(FEATURE_COLUMNS)}
    training_hash = _hash(identity)
    bundles: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    errors: dict[str, str] = {}
    fitted = False
    for name in MODEL_NAMES:
        path = _model_artifact_path(state_root, name, cutoff, training_hash)
        try:
            if path.exists():
                with path.open("rb") as source:
                    bundle = pickle.load(source)
                if bundle.get("training_hash") != training_hash or bundle.get("model") != name:
                    raise ValueError("saved model artifact training identity mismatch")
            else:
                bundle = _fit_single_model(name, train_rows, cutoff, data_identity, training_hash)
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".tmp")
                with temporary.open("xb") as target:
                    pickle.dump(bundle, target)
                os.replace(temporary, path)
                fitted = True
            bundles[name], paths[name] = bundle, path
        except Exception as error:  # An isolated model failure must not borrow another model's output.
            errors[name] = f"fit_or_load_failed:{type(error).__name__}:{error}"
    return bundles, paths, errors, fitted


def multi_model_prediction_records(snapshot: Mapping[str, Any], status: str, reason: str | None,
                                   bundles: Mapping[str, Mapping[str, Any]], artifacts: Mapping[str, Path],
                                   errors: Mapping[str, str]) -> list[dict[str, Any]]:
    base = {
        "prediction_date": snapshot["prediction_date"].isoformat(), "contract": snapshot["contract"],
        "cutoff": snapshot["prediction_at"].isoformat(), "generated_at": snapshot["generated_at"].isoformat(),
        "feature_available_at": snapshot["max_feature_available_at"].isoformat() if snapshot["max_feature_available_at"] else None,
        "snapshot_hash": snapshot["source_hash"], "version": VERSION, "notice": EXPERIMENTAL_NOTICE,
    }
    raw = (np.asarray([[snapshot["features"][name] if snapshot["features"].get(name) is not None else np.nan for name in FEATURE_COLUMNS]])
           if status != "unavailable" else None)
    records = []
    for name in MODEL_NAMES:
        bundle = bundles.get(name)
        if status == "unavailable" or bundle is None:
            records.append({**base, "model": name, "status": "unavailable", "unavailable_reason": reason or errors.get(name) or "model_unavailable",
                            "probability_up": None, "direction": None, "model_artifact": None, "model_fit_at": None, "training_hash": None})
            continue
        try:
            matrix = raw if bundle.get("preprocessor") is None else bundle["preprocessor"].transform(raw)
            probability = _probability_up(bundle["estimator"], matrix)
            records.append({**base, "model": name, "status": status, "unavailable_reason": reason,
                            "probability_up": probability, "direction": "UP" if probability >= .5 else "DOWN",
                            "model_artifact": str(artifacts.get(name, "")) or None,
                            "model_fit_at": bundle["fit_at"], "training_hash": bundle["training_hash"]})
        except Exception as error:
            records.append({**base, "model": name, "status": "unavailable", "unavailable_reason": f"predict_failed:{type(error).__name__}:{error}",
                            "probability_up": None, "direction": None, "model_artifact": str(artifacts.get(name, "")) or None,
                            "model_fit_at": bundle.get("fit_at"), "training_hash": bundle.get("training_hash")})
    return records


def prediction_records(snapshot: Mapping[str, Any], status: str, reason: str | None,
                       bundle: Mapping[str, Any] | None, artifact: Path | None) -> list[dict[str, Any]]:
    base = {
        "prediction_date": snapshot["prediction_date"].isoformat(), "contract": snapshot["contract"],
        "cutoff": snapshot["prediction_at"].isoformat(), "generated_at": snapshot["generated_at"].isoformat(),
        "feature_available_at": snapshot["max_feature_available_at"].isoformat(), "snapshot_hash": snapshot["source_hash"],
        "status": status, "unavailable_reason": reason, "version": VERSION, "notice": EXPERIMENTAL_NOTICE,
        "model_artifact": str(artifact) if artifact else None,
        "model_fit_at": bundle.get("fit_at") if bundle else None,
        "training_hash": bundle.get("training_hash") if bundle else None,
    }
    if bundle is None:
        return [{**base, "model": name, "probability_up": None, "direction": None} for name in ("dummy", "logistic")]
    raw = np.asarray([[snapshot["features"][name] if snapshot["features"][name] is not None else np.nan for name in FEATURE_COLUMNS]])
    transformed = bundle["preprocessor"].transform(raw)
    logistic_probability = _probability_up(bundle["logistic"], transformed)
    dummy_probability = _probability_up(bundle["dummy"], raw)
    return [
        {**base, "model": "dummy", "probability_up": dummy_probability, "direction": "UP" if dummy_probability >= .5 else "DOWN"},
        {**base, "model": "logistic", "probability_up": logistic_probability, "direction": "UP" if logistic_probability >= .5 else "DOWN"},
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_immutable(path: Path, records: list[dict[str, Any]], key_fields: tuple[str, ...]) -> None:
    existing = {tuple(row.get(field) for field in key_fields) for row in _read_jsonl(path)}
    keys = [tuple(row.get(field) for field in key_fields) for row in records]
    if len(keys) != len(set(keys)) or any(key in existing for key in keys):
        raise FileExistsError(f"immutable record already exists in {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as target:
        for row in records:
            target.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def evaluate_records(predictions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = {row["prediction_date"].isoformat(): row[BINARY_COLUMN] for row in rows}
    output = []
    for prediction in predictions:
        label = labels.get(prediction["prediction_date"])
        if prediction["status"] != "timely" or label not in (0, 1) or prediction["probability_up"] is None:
            continue
        probability = float(prediction["probability_up"])
        output.append({
            "prediction_date": prediction["prediction_date"], "model": prediction["model"], "prediction_status": prediction["status"],
            "label": label, "probability_up": probability, "correct": int((probability >= .5) == bool(label)),
            "brier": (probability - label) ** 2,
            "log_loss": -(label * np.log(max(probability, 1e-15)) + (1 - label) * np.log(max(1 - probability, 1e-15))),
            "prediction_reference": _hash({key: prediction.get(key) for key in ("prediction_date", "model", "snapshot_hash", "training_hash")}),
        })
    return output


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "accuracy": float(np.mean([row["correct"] for row in rows])) if rows else None,
        "brier": float(np.mean([row["brier"] for row in rows])) if rows else None,
        "log_loss": float(np.mean([row["log_loss"] for row in rows])) if rows else None,
    }


def evaluation_summary(records: list[dict[str, Any]], predictions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"timely_only": True, "models": {}, "common_timely_comparison": {}}
    for model in MODEL_NAMES:
        rows = [row for row in records if row["model"] == model]
        result["models"][model] = _metric_summary(rows)
    if predictions is None:
        return result
    dates_by_model = {
        model: {row["prediction_date"] for row in predictions if row.get("model") == model
                and row.get("status") == "timely" and row.get("probability_up") is not None}
        for model in MODEL_NAMES
    }
    all_dates = set().union(*(dates_by_model.values()))
    common = set.intersection(*(dates_by_model.values())) if all(dates_by_model.values()) else set()
    result["prediction_coverage"] = {
        model: {"timely_dates": len(dates), "all_model_prediction_dates": len({row["prediction_date"] for row in predictions if row.get("model") == model}),
                "coverage_of_any_timely_date": len(dates) / len(all_dates) if all_dates else None}
        for model, dates in dates_by_model.items()
    }
    result["common_timely_comparison"] = {
        "dates": sorted(common), "count": len(common),
        "models": {model: _metric_summary([row for row in records if row["model"] == model and row["prediction_date"] in common])
                   for model in MODEL_NAMES},
    }
    return result


def append_new_evaluations(state_root: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate revealed labels append-only; late/replay records stay excluded."""
    prediction_path = state_root / "predictions.jsonl"
    evaluation_path = state_root / "evaluations.jsonl"
    evaluations = evaluate_records(_read_jsonl(prediction_path), rows)
    existing = {row.get("prediction_reference") for row in _read_jsonl(evaluation_path)}
    additions = [row for row in evaluations if row["prediction_reference"] not in existing]
    if additions:
        append_immutable(evaluation_path, additions, ("prediction_reference",))
    return {"new_records": len(additions), "path": str(evaluation_path),
            "summary": evaluation_summary(_read_jsonl(evaluation_path), _read_jsonl(prediction_path))}


def run(options: argparse.Namespace) -> dict[str, Any]:
    if options.preflight:
        return {"version": VERSION, "live_readiness": live_readiness()}
    if options.evaluate_only:
        rows, _ = load_b4_rows(options.dataset)
        return {"version": VERSION, "evaluation": append_new_evaluations(options.state_root, rows),
                "notice": EXPERIMENTAL_NOTICE}
    if options.snapshot is None:
        raise ValueError("--snapshot is required unless --preflight is used")
    payload = json.loads(options.snapshot.read_text(encoding="utf-8"))
    snapshot = normalize_snapshot(payload)
    status, reason = snapshot_status(snapshot, options.mode)
    rows, data_identity = load_b4_rows(options.dataset)
    bundles: dict[str, dict[str, Any]] = {}
    artifacts: dict[str, Path] = {}
    errors: dict[str, str] = {}
    if status != "unavailable":
        bundles, artifacts, errors, fitted = load_or_fit_models(rows, snapshot["prediction_at"], options.state_root, data_identity)
    else:
        fitted = False
    records = multi_model_prediction_records(snapshot, status, reason, bundles, artifacts, errors)
    prediction_path = options.state_root / "predictions.jsonl"
    existing = {tuple(row.get(field) for field in ("prediction_date", "model")) for row in _read_jsonl(prediction_path)}
    additions = [record for record in records if (record["prediction_date"], record["model"]) not in existing]
    if additions:
        append_immutable(prediction_path, additions, ("prediction_date", "model"))
    result: dict[str, Any] = {"version": VERSION, "status": status, "reason": reason,
                              "records": len(records), "new_records": len(additions), "model_fitted": fitted,
                              "model_errors": errors, "prediction_path": str(prediction_path),
                              "notice": EXPERIMENTAL_NOTICE}
    if options.evaluate:
        result["evaluation"] = append_new_evaluations(options.state_root, rows)
    return result


if __name__ == "__main__":
    try:
        print(json.dumps(run(arguments()), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({"version": VERSION, "status": "error", "error": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
