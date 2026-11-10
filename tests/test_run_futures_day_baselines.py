from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research import build_futures_day_dataset as day
from research import run_futures_day_baselines as runner

TAIPEI = ZoneInfo("Asia/Taipei")


def row(index: int, *, direction: str = "up", feature_ok: bool = True) -> dict:
    value = date(2024, 1, 2) + timedelta(days=index)
    cutoff = datetime.combine(value, time(8, 15), TAIPEI)
    output = {
        "prediction_date": value, "prediction_at": cutoff, "feature_pit_status": "passed_0815" if feature_ok else "unavailable_0815",
        "feature_missing_reason": None if feature_ok else "fixture_feature_unavailable",
        "direction_class": direction,
        day.BINARY_COLUMN: 1 if direction == "up" else 0 if direction == "down" else None,
        "target_available_at": cutoff + timedelta(hours=7) if direction != "missing" else None,
        "contract_selection_evidence_status": "unverified_default_rule_availability",
        "live_readiness": "not_assessed_no_0815_live_snapshot",
    }
    for number, name in enumerate(day.FEATURE_COLUMNS):
        output[name] = None if index % 11 == 0 and number == 0 else float(index + number + 1)
    return output


class DayBaselineTests(unittest.TestCase):
    @staticmethod
    def global_pit_rows(rows: list[dict]) -> dict:
        """Small PIT fixture with distinct real source observations per date."""
        output = {}
        for index, item in enumerate(rows):
            cutoff = item["prediction_at"]
            pit = {"prediction_date": item["prediction_date"], "prediction_at": cutoff}
            for prefix in runner.baseline.GLOBAL_LEVEL_COLUMNS:
                pit[f"{prefix}_value"] = 100.0 + index
                pit[f"{prefix}_source_date"] = item["prediction_date"]
                pit[f"{prefix}_available_at"] = cutoff - timedelta(minutes=1)
            output[item["prediction_date"]] = pit
        return output

    @staticmethod
    def positioning_pit_rows(rows: list[dict]) -> dict:
        output = {}
        for item in rows:
            cutoff = item["prediction_at"]
            pit = {"prediction_date": item["prediction_date"]}
            for column in runner.baseline.POSITIONING_COLUMNS:
                pit[column] = 1.0
            for field in ("foreign_available_at", "investment_trust_available_at", "dealer_available_at", "pcr_available_at"):
                pit[field] = cutoff - timedelta(minutes=1)
            output[item["prediction_date"]] = pit
        return output

    def test_three_models_share_test_dates_and_flat_missing_are_unscored(self):
        rows = [row(index, direction=("flat" if index == 506 else "missing" if index == 507 else "up" if index % 2 else "down"))
                for index in range(510)]
        report = runner.build_report(rows, initial_train_size=504, test_size=21, data_identity={"fixture": True})
        models = report["models"]
        dates = [item["prediction_date"] for item in models["dummy"]["oos_predictions"]]
        self.assertEqual(dates, [item["prediction_date"] for item in models["logistic"]["oos_predictions"]])
        self.assertEqual(dates, [item["prediction_date"] for item in models["hist_gradient_boosting"]["oos_predictions"]])
        flat = next(item for item in models["dummy"]["oos_predictions"] if item["direction_class"] == "flat")
        self.assertEqual(flat["score_exclusion_reason"], "flat_label_excluded_from_binary")
        self.assertEqual(report["row_counts"]["test_sessions"], 6)
        self.assertIn("logistic", report["relative_to_dummy"])
        relative = report["relative_to_dummy"]["logistic"]
        self.assertEqual(relative["common_scorable_count"], relative["model_metrics"]["support"])
        self.assertEqual(relative["common_scorable_count"], relative["dummy_metrics"]["support"])
        self.assertNotIn(flat["prediction_date"], relative["common_scorable_dates"])

    def test_relative_to_dummy_excludes_missing_label_and_missing_prediction(self):
        def record(day: str, *, target=1, prediction=1):
            return {"prediction_date": day, "target": target, "prediction": prediction, "probability_up": 0.7}

        report = {"models": {
            "dummy": {"oos_predictions": [record("2024-01-02"), record("2024-01-03", target=None), record("2024-01-04")]},
            "logistic": {"oos_predictions": [record("2024-01-02"), record("2024-01-03", target=None), record("2024-01-04", prediction=None)]},
            "hist_gradient_boosting": {"oos_predictions": [record("2024-01-02"), record("2024-01-03", target=None), record("2024-01-04")]},
        }}
        relative = runner._relative_to_dummy(report)
        self.assertEqual(relative["logistic"]["common_scorable_dates"], ["2024-01-02"])
        self.assertEqual(relative["logistic"]["common_scorable_count"], 1)
        self.assertEqual(relative["logistic"]["model_metrics"]["support"], 1)

    def test_repair_report_recomputes_only_derived_statistics(self):
        source = {"version": "v5.day-baselines-1.0", "experiment": "day_tx_core", "models": {
            name: {"oos_predictions": [{"prediction_date": "2024-01-02", "target": 1, "prediction": 1, "probability_up": 0.8}]}
            for name in ("dummy", "logistic", "hist_gradient_boosting")
        }}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.json"
            path.write_text(__import__("json").dumps(source), encoding="utf-8")
            repaired = runner.repair_report(path)
        self.assertTrue(repaired["derived_statistics_repaired"]["training_or_predictions_recomputed"] is False)
        self.assertEqual(repaired["relative_to_dummy"]["logistic"]["common_scorable_count"], 1)

    def test_existing_output_is_rejected_before_dataset_load_or_training(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "already-exists.json"
            output.write_text("{}", encoding="utf-8")
            options = SimpleNamespace(
                output=output, repair_report=None, dataset=Path(directory), initial_train_size=504, test_size=21,
            )
            with patch.object(runner, "load_day_rows", side_effect=AssertionError("must not load dataset")):
                with self.assertRaises(FileExistsError):
                    runner.run(options)

    def test_feature_unavailable_row_is_reported_as_coverage_not_silently_modelled(self):
        rows = [row(index, direction="up" if index % 2 else "down", feature_ok=index != 505) for index in range(510)]
        report = runner.build_report(rows, initial_train_size=504, test_size=21)
        self.assertEqual(report["row_counts"]["excluded_feature_unavailable"], 1)
        self.assertEqual(report["excluded_rows"][0]["reason"], "fixture_feature_unavailable")

    def test_day_global_comparison_reuses_pit_safe_returns_and_shared_scoring_denominator(self):
        rows = [row(index, direction="up" if index % 2 else "down") for index in range(10)]
        report = runner.build_global_comparison_report(
            rows,
            pit_rows=self.global_pit_rows(rows),
            pit_identity={"fixture": "daily-pit"},
            initial_train_size=4,
            test_size=3,
            data_identity={"fixture": "day"},
        )
        self.assertEqual(report["global_feature_policy"]["dgs10"], "excluded/quarantined")
        self.assertEqual(
            report["global_feature_policy"]["included"], list(runner.baseline.GLOBAL_RETURN_COLUMNS),
        )
        global_rows = report["tx_core_plus_global"]["models"]["dummy"]["oos_predictions"]
        core_rows = report["tx_core"]["models"]["dummy"]["oos_predictions"]
        self.assertEqual(
            [item["prediction_date"] for item in core_rows],
            [item["prediction_date"] for item in global_rows],
        )
        for comparison in report["common_scoring"].values():
            self.assertEqual(comparison["common_scorable_count"], comparison["tx_core_metrics"]["support"])
            self.assertEqual(comparison["common_scorable_count"], comparison["tx_core_plus_global_metrics"]["support"])
        self.assertEqual(
            report["comparison"]["models"]["logistic"]["common_scorable_count"],
            report["common_scoring"]["logistic"]["common_scorable_count"],
        )

    def test_day_global_return_rejects_value_available_after_0815_cutoff(self):
        rows = [row(index, direction="up" if index % 2 else "down") for index in range(2)]
        pit_rows = self.global_pit_rows(rows)
        for prefix in runner.baseline.GLOBAL_LEVEL_COLUMNS:
            pit_rows[rows[1]["prediction_date"]][f"{prefix}_available_at"] = rows[1]["prediction_at"] + timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "exceeds prediction_at"):
            runner.baseline.attach_global_returns(runner.prepare_rows(rows), pit_rows)

    def test_day_positioning_comparison_keeps_oos_dates_and_falls_back_to_core_when_train_coverage_fails(self):
        rows = [row(index, direction="up" if index % 2 else "down") for index in range(10)]
        report = runner.build_positioning_comparison_report(
            rows,
            pit_rows=self.positioning_pit_rows(rows),
            pit_identity={"fixture": "daily-pit"},
            initial_train_size=4,
            test_size=3,
            data_identity={"fixture": "day"},
        )
        self.assertEqual(set(report["experiments"]), {
            "tx_core", "tx_core_plus_institutional", "tx_core_plus_pcr", "tx_core_plus_institutional_pcr",
        })
        core_dates = [item["prediction_date"] for item in report["experiments"]["tx_core"]["models"]["dummy"]["oos_predictions"]]
        for name, experiment in report["experiments"].items():
            self.assertEqual(core_dates, [item["prediction_date"] for item in experiment["models"]["dummy"]["oos_predictions"]])
            self.assertIn("relative_to_dummy", experiment)
        guard = report["experiments"]["tx_core_plus_institutional"]["folds"][0]["coverage_guard"]
        self.assertTrue(guard["fallback_to_tx_core"])
        self.assertEqual(guard["retained_columns"], list(day.FEATURE_COLUMNS))
        shared = report["common_scoring"]["tx_core_plus_pcr"]["logistic"]
        self.assertEqual(shared["common_scorable_count"], shared["tx_core_metrics"]["support"])
        self.assertEqual(shared["common_scorable_count"], shared["tx_core_plus_pcr_metrics"]["support"])

    def test_day_positioning_rejects_source_later_than_0815(self):
        rows = [row(index, direction="up" if index % 2 else "down") for index in range(2)]
        pit_rows = self.positioning_pit_rows(rows)
        pit_rows[rows[1]["prediction_date"]]["foreign_available_at"] = rows[1]["prediction_at"] + timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "exceeds prediction_at"):
            runner.baseline.attach_positioning_features(runner.prepare_rows(rows), pit_rows)
