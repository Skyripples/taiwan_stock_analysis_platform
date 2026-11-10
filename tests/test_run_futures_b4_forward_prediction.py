from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research.build_futures_b4_same_day_dataset import BINARY_COLUMN, FEATURE_COLUMNS
from research.run_futures_b4_forward_prediction import (
    append_immutable,
    append_new_evaluations,
    eligible_training_rows,
    load_or_fit_bundle,
    multi_model_prediction_records,
    evaluate_records,
    live_readiness,
    normalize_snapshot,
    prediction_records,
    snapshot_status,
    taifex_sessions_since,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def snapshot(*, generated_at: datetime | None = None, omit: str | None = None) -> dict:
    cutoff = datetime(2026, 10, 20, 8, 30, tzinfo=TAIPEI)
    result = {
        "prediction_date": "2026-10-20", "prediction_at": cutoff.isoformat(),
        "generated_at": (generated_at or cutoff).isoformat(),
        "max_feature_available_at": (cutoff - timedelta(minutes=1)).isoformat(), "contract": "TX202610",
        "source_fetched_at": (cutoff - timedelta(minutes=2)).isoformat(),
        "sources": [{
            "path": "fixture.jsonl.gz", "sha256": "fixture", "source_date": "2026-10-19",
            "available_at": (cutoff - timedelta(minutes=1)).isoformat(),
            "fetched_at": (cutoff - timedelta(minutes=2)).isoformat(),
        }],
    }
    result.update({name: float(index + 1) for index, name in enumerate(FEATURE_COLUMNS)})
    result["feature_available_at"] = {name: (cutoff - timedelta(minutes=1)).isoformat() for name in FEATURE_COLUMNS}
    if omit:
        del result[omit]
    return result


def row(day: date, *, revealed: bool) -> dict:
    prediction = datetime(day.year, day.month, day.day, 8, 30, tzinfo=TAIPEI)
    result = {
        "prediction_date": day, "prediction_at": prediction, "feature_available": True,
        BINARY_COLUMN: 1, "target_available_at": prediction + timedelta(hours=7) if revealed else None,
    }
    result.update({name: 1.0 for name in FEATURE_COLUMNS})
    return result


class B4ForwardPredictionTests(unittest.TestCase):
    def test_snapshot_distinguishes_unavailable_late_and_replay(self):
        current = normalize_snapshot(snapshot())
        self.assertEqual(snapshot_status(current, "live"), ("timely", None))
        late = normalize_snapshot(snapshot(generated_at=datetime(2026, 10, 20, 8, 31, tzinfo=TAIPEI)))
        self.assertEqual(snapshot_status(late, "live")[0], "late")
        self.assertEqual(snapshot_status(current, "replay"), ("replay", None))
        missing = normalize_snapshot(snapshot(omit=FEATURE_COLUMNS[0]))
        self.assertEqual(snapshot_status(missing, "live")[0], "unavailable")

    def test_training_selection_requires_revealed_label_before_cutoff(self):
        cutoff = datetime(2026, 10, 20, 8, 30, tzinfo=TAIPEI)
        prior = row(date(2026, 10, 19), revealed=True)
        pending = row(date(2026, 10, 18), revealed=False)
        future = row(date(2026, 10, 21), revealed=True)
        self.assertEqual(eligible_training_rows([prior, pending, future], cutoff), [prior])

    def test_prediction_records_never_substitute_a_missing_row(self):
        current = normalize_snapshot(snapshot(omit=FEATURE_COLUMNS[0]))
        status, reason = snapshot_status(current, "live")
        records = prediction_records(current, status, reason, None, None)
        self.assertEqual({item["model"] for item in records}, {"dummy", "logistic"})
        self.assertTrue(all(item["probability_up"] is None for item in records))
        self.assertTrue(all(item["status"] == "unavailable" for item in records))

    def test_immutable_predictions_and_timely_only_evaluation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "predictions.jsonl"
            record = {"prediction_date": "2026-10-20", "model": "logistic", "status": "timely"}
            append_immutable(path, [record], ("prediction_date", "model"))
            with self.assertRaises(FileExistsError):
                append_immutable(path, [record], ("prediction_date", "model"))
        timely = {"prediction_date": "2026-10-20", "model": "logistic", "status": "timely", "probability_up": .8,
                  "snapshot_hash": "a", "training_hash": "b"}
        late = {**timely, "model": "dummy", "status": "late"}
        labels = [row(date(2026, 10, 20), revealed=True)]
        evaluated = evaluate_records([timely, late], labels)
        self.assertEqual(len(evaluated), 1)
        self.assertEqual(evaluated[0]["correct"], 1)

    def test_evaluation_append_is_idempotent_and_keeps_late_out(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prediction = {"prediction_date": "2026-10-20", "model": "logistic", "status": "timely",
                          "probability_up": .8, "snapshot_hash": "a", "training_hash": "b"}
            late = {**prediction, "model": "dummy", "status": "late"}
            append_immutable(root / "predictions.jsonl", [prediction, late], ("prediction_date", "model"))
            rows = [row(date(2026, 10, 20), revealed=True)]
            first = append_new_evaluations(root, rows)
            second = append_new_evaluations(root, rows)
            self.assertEqual(first["new_records"], 1)
            self.assertEqual(second["new_records"], 0)
            self.assertEqual(second["summary"]["models"]["logistic"]["count"], 1)
            self.assertEqual(second["summary"]["models"]["dummy"]["count"], 0)

    def test_current_pipeline_preflight_refuses_to_certify_live_snapshot(self):
        readiness = live_readiness()
        self.assertFalse(readiness["ready"])
        self.assertIn("scheduler", " ".join(readiness["blockers"]))

    def test_synthetic_bundle_is_saved_and_reused_on_the_21_session_cycle(self):
        cutoff = datetime(2026, 10, 20, 8, 30, tzinfo=TAIPEI)
        rows = []
        for offset in range(4):
            item = row(date(2026, 10, 10 + offset), revealed=True)
            item[BINARY_COLUMN] = offset % 2
            for feature_index, name in enumerate(FEATURE_COLUMNS):
                item[name] = float(offset + feature_index + 1)
            rows.append(item)
        with tempfile.TemporaryDirectory() as directory:
            with patch("research.run_futures_b4_forward_prediction.MIN_TRAINING_ROWS", 4):
                bundle, path, fitted = load_or_fit_bundle(rows, cutoff, Path(directory), {"fixture": True})
                self.assertTrue(fitted)
                self.assertTrue(path.exists())
                reused, same_path, fitted_again = load_or_fit_bundle(rows, cutoff, Path(directory), {"fixture": True})
                self.assertFalse(fitted_again)
                self.assertEqual(path, same_path)
                snapshot_row = normalize_snapshot(snapshot())
                records = prediction_records(snapshot_row, "timely", None, reused, same_path)
                self.assertTrue(all(item["probability_up"] is not None for item in records))

    def test_retrain_interval_counts_taifex_prediction_sessions_not_new_labels(self):
        fit = datetime(2026, 1, 2, 8, 30, tzinfo=TAIPEI)
        cutoff = datetime(2026, 2, 2, 8, 30, tzinfo=TAIPEI)
        sessions = [date(2026, 1, 2) + timedelta(days=index) for index in range(22)]
        with patch("research.run_futures_b4_forward_prediction.get_taifex_trading_days_in_range", return_value=(sessions, [])):
            self.assertEqual(taifex_sessions_since(fit, cutoff), 21)

    def test_hgb_has_own_record_and_model_failure_does_not_borrow_logistic(self):
        class Model:
            classes_ = [0, 1]
            def predict_proba(self, _matrix): return [[.4, .6]]
        current = normalize_snapshot(snapshot())
        bundles = {
            "dummy": {"estimator": Model(), "preprocessor": None, "fit_at": "2026-10-19T08:30:00+08:00", "training_hash": "d"},
            "logistic": {"estimator": Model(), "preprocessor": None, "fit_at": "2026-10-19T08:30:00+08:00", "training_hash": "l"},
        }
        records = multi_model_prediction_records(current, "timely", None, bundles, {}, {"hist_gradient_boosting": "fit_failed"})
        by_model = {item["model"]: item for item in records}
        self.assertEqual(set(by_model), {"dummy", "logistic", "hist_gradient_boosting"})
        self.assertEqual(by_model["hist_gradient_boosting"]["status"], "unavailable")
        self.assertIsNone(by_model["hist_gradient_boosting"]["probability_up"])
        self.assertEqual(by_model["logistic"]["probability_up"], .6)

    def test_evaluation_reports_common_timely_dates_and_coverage(self):
        records = [
            {"prediction_date": "2026-10-20", "model": model, "status": "timely", "probability_up": .7,
             "snapshot_hash": model, "training_hash": "x"}
            for model in ("dummy", "logistic", "hist_gradient_boosting")
        ]
        labels = [row(date(2026, 10, 20), revealed=True)]
        from research.run_futures_b4_forward_prediction import evaluation_summary
        summary = evaluation_summary(evaluate_records(records, labels), records)
        self.assertEqual(summary["common_timely_comparison"]["count"], 1)
        self.assertEqual(summary["prediction_coverage"]["hist_gradient_boosting"]["timely_dates"], 1)
