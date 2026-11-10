from __future__ import annotations

import sys
import unittest
import tempfile
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import build_futures_day_dataset as day
from research import build_futures_day_live_snapshot as snapshot_builder
from research import audit_futures_day_forward_records as audit
from research import run_futures_day_forward_prediction as forward

TAIPEI = ZoneInfo("Asia/Taipei")


class PickleableEstimator:
    classes_ = [0, 1]

    def fit(self, _matrix, _target):
        return self


def raw_snapshot(*, generated_at: datetime | None = None) -> dict:
    cutoff = datetime(2026, 10, 20, 8, 15, tzinfo=TAIPEI)
    result = {
        "status": "ready", "prediction_date": "2026-10-20", "prediction_at": cutoff.isoformat(),
        "generated_at": (generated_at or cutoff).isoformat(), "contract": "TX202610",
        "max_feature_available_at": cutoff.isoformat(), "source_fetched_at": cutoff.isoformat(),
        "sources": [{"path": "fixture.jsonl.gz", "sha256": "fixture", "available_at": cutoff.isoformat(), "fetched_at": cutoff.isoformat()}],
        "feature_available_at": {name: cutoff.isoformat() for name in day.FEATURE_COLUMNS},
    }
    result.update({name: float(index + 1) for index, name in enumerate(day.FEATURE_COLUMNS)})
    return result


def dataset_row(value: date, *, label: int | None = 1, revealed: bool = True) -> dict:
    prediction = datetime.combine(value, time(8, 15), TAIPEI)
    row = {
        "prediction_date": value, "prediction_at": prediction, "feature_pit_status": "passed_0815",
        day.BINARY_COLUMN: label, "target_available_at": prediction + timedelta(hours=7) if revealed else None,
    }
    row.update({name: 1.0 for name in day.FEATURE_COLUMNS})
    return row


class DayForwardTests(unittest.TestCase):
    def test_snapshot_normalization_requires_0815_and_source_fetched_by_cutoff(self):
        current = forward.normalize_snapshot(raw_snapshot())
        self.assertEqual(forward.snapshot_status(current, "live"), ("on_time", None))
        payload = raw_snapshot()
        late = (datetime(2026, 10, 20, 8, 15, 1, tzinfo=TAIPEI)).isoformat()
        payload["sources"][0]["fetched_at"] = late
        payload["source_fetched_at"] = late
        with self.assertRaisesRegex(ValueError, "after 08:15"):
            forward.normalize_snapshot(payload)

    def test_training_rows_require_revealed_non_flat_labels_before_cutoff(self):
        cutoff = datetime(2026, 10, 20, 8, 15, tzinfo=TAIPEI)
        eligible = dataset_row(date(2026, 10, 19), label=1)
        flat = dataset_row(date(2026, 10, 18), label=None)
        pending = dataset_row(date(2026, 10, 17), label=0, revealed=False)
        future = dataset_row(date(2026, 10, 21), label=0)
        self.assertEqual(forward.eligible_training_rows([eligible, flat, pending, future], cutoff), [eligible])

    def test_late_snapshot_is_never_timely_and_prediction_records_are_per_model(self):
        class Estimator:
            classes_ = [0, 1]
            def predict_proba(self, _matrix): return [[.4, .6]]

        payload = raw_snapshot(generated_at=datetime(2026, 10, 20, 8, 16, tzinfo=TAIPEI))
        snapshot = forward.normalize_snapshot(payload)
        status, reason = forward.snapshot_status(snapshot, "live")
        self.assertEqual(status, "late")
        bundles = {
            name: {"preprocessor": None, "estimator": Estimator(), "fit_at": "2026-10-19T08:15:00+08:00", "training_hash": name}
            for name in forward.MODEL_NAMES
        }
        records = forward.prediction_records(snapshot, status, reason, bundles, {}, {})
        self.assertEqual({record["model"] for record in records}, set(forward.MODEL_NAMES))
        self.assertTrue(all(record["status"] == "late" for record in records))

    def test_early_legacy_record_is_audited_without_overwrite(self):
        record = {"prediction_date": "2026-11-09", "model": "dummy", "status": "timely",
                  "generated_at": "2026-11-07T07:07:56+08:00", "cutoff": "2026-11-09T08:15:00+08:00"}
        report = audit.audit([record])
        finding = report["findings"][0]
        self.assertTrue(report["read_only"])
        self.assertEqual(finding["derived_temporal_status"], "early")
        self.assertTrue(finding["legacy_status_mismatch"])

    def test_artifact_keeps_distinct_training_and_prediction_timestamps(self):
        cutoff = datetime(2026, 10, 20, 8, 15, tzinfo=TAIPEI)
        trained = datetime(2026, 10, 20, 8, 10, tzinfo=TAIPEI)
        rows = [dataset_row(date(2026, 10, 18), label=0), dataset_row(date(2026, 10, 19), label=1)]
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(forward, "MIN_TRAINING_ROWS", 2), \
             patch.object(forward.baselines, "_models", return_value={name: PickleableEstimator() for name in forward.MODEL_NAMES}):
            bundles, _, errors, _ = forward.load_or_fit_models(rows, cutoff, Path(temporary), {}, trained_at=trained)
        self.assertFalse(errors)
        self.assertEqual(set(bundles), set(forward.MODEL_NAMES))
        self.assertTrue(all(bundle["trained_at"] == trained.isoformat() for bundle in bundles.values()))
        self.assertTrue(all(bundle["training_label_cutoff_at"] == trained.isoformat() for bundle in bundles.values()))
        self.assertTrue(all(bundle["prediction_cutoff_at"] == cutoff.isoformat() for bundle in bundles.values()))

    def test_day_snapshot_builder_rejects_source_fetched_one_second_late(self):
        prediction_date = date(2026, 10, 20)
        cutoff = day.prediction_timestamp(prediction_date)
        base = raw_snapshot()
        base["prediction_date"] = prediction_date.isoformat()
        base["prediction_at"] = datetime(2026, 10, 20, 8, 30, tzinfo=TAIPEI).isoformat()
        base["sources"][0]["available_at"] = cutoff.isoformat()
        base["sources"][0]["fetched_at"] = cutoff.isoformat()
        with patch.object(snapshot_builder.b4_snapshot, "build_snapshot", return_value=base):
            ready = snapshot_builder.build_snapshot(prediction_date, [], generated_at=cutoff, mode="live")
        self.assertEqual(ready["status"], "ready")
        base["sources"][0]["fetched_at"] = (cutoff + timedelta(seconds=1)).isoformat()
        with patch.object(snapshot_builder.b4_snapshot, "build_snapshot", return_value=base):
            unavailable = snapshot_builder.build_snapshot(prediction_date, [], generated_at=cutoff, mode="live")
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["unavailable_reason"], "source_fetched_after_day_0815_cutoff")

    def test_existing_early_snapshot_is_reused_without_reading_sources_or_overwrite(self):
        prediction_date = date(2026, 10, 20)
        generated = datetime(2026, 10, 19, 8, 0, tzinfo=TAIPEI)
        payload = raw_snapshot(generated_at=generated)
        source_time = datetime(2026, 10, 19, 7, 0, tzinfo=TAIPEI).isoformat()
        payload["max_feature_available_at"] = source_time
        payload["source_fetched_at"] = source_time
        payload["sources"][0]["available_at"] = source_time
        payload["sources"][0]["fetched_at"] = source_time
        payload["feature_available_at"] = {name: source_time for name in day.FEATURE_COLUMNS}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "2026-10-20.json"
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            path.write_bytes(encoded)
            options = type("Options", (), {"output_root": root, "prediction_date": prediction_date,
                                              "mode": "live", "dry_run": False, "tx_root": Path("must-not-read")})()
            with patch.object(snapshot_builder.b4_snapshot, "read_tx_rows", side_effect=AssertionError("must not read sources")):
                result = snapshot_builder.run(options, now=datetime(2026, 10, 20, 8, 5, tzinfo=TAIPEI))
            self.assertEqual(result["action"], "reused_existing")
            self.assertEqual(result["status"], "early")
            self.assertTrue(result["read_only"])
            self.assertEqual(path.read_bytes(), encoded)

    def test_new_snapshot_is_created_for_another_trading_day(self):
        prediction_date = date(2026, 10, 20)
        cutoff = day.prediction_timestamp(prediction_date)
        payload = raw_snapshot(generated_at=cutoff)
        payload["prediction_date"] = prediction_date.isoformat()
        payload["prediction_at"] = datetime(2026, 10, 20, 8, 30, tzinfo=TAIPEI).isoformat()
        payload["sources"][0]["available_at"] = cutoff.isoformat()
        payload["sources"][0]["fetched_at"] = cutoff.isoformat()
        with tempfile.TemporaryDirectory() as temporary, patch.object(snapshot_builder.b4_snapshot, "read_tx_rows", return_value=[]), \
             patch.object(snapshot_builder.b4_snapshot, "build_snapshot", return_value=payload):
            options = type("Options", (), {"output_root": Path(temporary), "prediction_date": prediction_date,
                                              "mode": "live", "dry_run": False, "tx_root": Path("unused")})()
            result = snapshot_builder.run(options, now=cutoff)
            self.assertEqual(result["action"], "created")
            self.assertTrue((Path(temporary) / "2026-10-20.json").exists())


if __name__ == "__main__":
    unittest.main()
