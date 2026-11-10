from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research.build_futures_b4_same_day_dataset import FEATURE_COLUMNS
from research.run_futures_b4_baselines import (
    DOWN, UP, GLOBAL_RETURN_COLUMNS, attach_global_returns, comparison_report,
    INSTITUTIONAL_COLUMNS, PCR_COLUMNS, POSITIONING_COLUMNS, attach_positioning_features,
    derive_global_returns, positioning_comparison_report, run_rows, run_rows_with_coverage_guard,
    select_coverage_guard_features, write_report,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def row(index: int, *, target: int | None = None, feature_available: bool = True) -> dict:
    day = date(2025, 1, 2) + timedelta(days=index)
    prediction = datetime.combine(day, time(8, 30), TAIPEI)
    output = {
        "prediction_date": day, "prediction_at": prediction,
        "target_available_at": prediction + timedelta(hours=7) if target is not None else None,
        "binary_direction_target": target,
        "feature_available": feature_available,
        "feature_missing_reason": None if feature_available else "missing_pit_feature_row",
    }
    for position, name in enumerate(FEATURE_COLUMNS):
        # tx_roll_regime is intentionally all missing: handling must be
        # determined separately inside every training fold.
        output[name] = None if name == "tx_roll_regime" else float(index + position + 1)
    return output


class B4BaselineRunnerTests(unittest.TestCase):
    def test_three_models_share_oos_dates_and_use_train_only_folds(self):
        rows = [row(index, target=index % 2) for index in range(12)]
        rows[-1] = row(11, target=None)
        rows[3] = row(3, target=UP, feature_available=False)
        report = run_rows(rows, initial_train_size=4, test_size=3, data_identity={"fixture": True})
        self.assertEqual(report["task"]["labels"], {"DOWN": DOWN, "UP": UP})
        self.assertEqual(report["row_counts"]["excluded_feature_unavailable"], 1)
        self.assertTrue(report["folds"])
        expected = None
        for name in ("dummy", "logistic", "hist_gradient_boosting"):
            dates = [item["prediction_date"] for item in report["models"][name]["oos_predictions"]]
            self.assertEqual(dates, expected or dates)
            expected = dates
        for fold in report["folds"]:
            self.assertLess(fold["train_count"], 12)
            for name in ("logistic", "hist_gradient_boosting"):
                self.assertIn("tx_roll_regime", fold["models"][name]["diagnostics"].get("all_missing_features", []))
        self.assertIn("overall_metrics", report["models"]["dummy"])
        self.assertIn("year_metrics", report["models"]["dummy"])
        self.assertIn("confusion_matrix", report["models"]["dummy"]["overall_metrics"]["values"])

    def test_one_class_fold_records_unavailable_model_predictions_without_dropping_dates(self):
        rows = [row(index, target=DOWN) for index in range(8)]
        report = run_rows(rows, initial_train_size=3, test_size=2)
        for name in ("logistic", "hist_gradient_boosting"):
            predictions = report["models"][name]["oos_predictions"]
            self.assertTrue(predictions)
            self.assertTrue(all(item["prediction"] is None for item in predictions))
            self.assertIn("all", report["models"][name]["overall_metrics"]["unavailable"])
        dummy = report["models"]["dummy"]["oos_predictions"]
        self.assertTrue(all(item["probability_up"] == 0.0 and item["prediction"] == DOWN for item in dummy))

    def test_report_never_overwrites_existing_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            write_report({"fixture": True}, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["fixture"], True)
            with self.assertRaises(FileExistsError):
                write_report({"replacement": True}, path)

    def test_global_returns_use_source_observations_not_forward_filled_prediction_rows(self):
        first = date(2025, 1, 2)
        second = date(2025, 1, 3)
        third = date(2025, 1, 4)
        def pit(day, source_day, value):
            output = {"prediction_date": day, "prediction_at": datetime.combine(day, time(8, 30), TAIPEI)}
            for prefix in ("sox", "tsm_adr", "nasdaq", "sp500", "usdtwd"):
                output[f"{prefix}_source_date"] = source_day
                output[f"{prefix}_value"] = value
                output[f"{prefix}_available_at"] = datetime.combine(day, time(1, 0), TAIPEI)
            return output
        pit_rows = {
            first: pit(first, date(2025, 1, 1), 100.0),
            second: pit(second, date(2025, 1, 2), 110.0),
            third: pit(third, date(2025, 1, 2), 110.0),
        }
        for prefix in ("sox", "tsm_adr", "nasdaq", "sp500", "usdtwd"):
            pit_rows[third][f"{prefix}_available_at"] = pit_rows[second][f"{prefix}_available_at"]
        derived = derive_global_returns(pit_rows.values())
        self.assertAlmostEqual(derived["sox"][date(2025, 1, 2)]["value"], .1)
        b4_rows = [row(0, target=DOWN), row(1, target=UP), row(2, target=DOWN)]
        for item, day in zip(b4_rows, (first, second, third)):
            item["prediction_date"] = day
            item["prediction_at"] = datetime.combine(day, time(8, 30), TAIPEI)
            item["target_available_at"] = item["prediction_at"] + timedelta(hours=7)
        enriched, metadata = attach_global_returns(b4_rows, pit_rows)
        self.assertIsNone(enriched[0]["sox_return"])
        self.assertAlmostEqual(enriched[1]["sox_return"], .1)
        self.assertAlmostEqual(enriched[2]["sox_return"], .1)
        self.assertEqual(metadata["dgs10"], "excluded/quarantined")

    def test_global_return_rejects_availability_after_prediction_cutoff(self):
        day = date(2025, 1, 2)
        pit = {"prediction_date": day, "prediction_at": datetime.combine(day, time(8, 30), TAIPEI)}
        for prefix in ("sox", "tsm_adr", "nasdaq", "sp500", "usdtwd"):
            pit[f"{prefix}_source_date"] = date(2025, 1, 1)
            pit[f"{prefix}_value"] = 100.0
            pit[f"{prefix}_available_at"] = datetime.combine(day, time(9, 0), TAIPEI)
        with self.assertRaisesRegex(ValueError, "exceeds prediction_at"):
            # Add an earlier observation so the later one has a return to join.
            prior = dict(pit)
            prior["prediction_date"] = date(2025, 1, 1)
            prior["prediction_at"] = datetime.combine(date(2025, 1, 1), time(8, 30), TAIPEI)
            for prefix in ("sox", "tsm_adr", "nasdaq", "sp500", "usdtwd"):
                prior[f"{prefix}_source_date"] = date(2024, 12, 31)
                prior[f"{prefix}_available_at"] = datetime.combine(date(2025, 1, 1), time(1, 0), TAIPEI)
            attach_global_returns([row(0, target=DOWN)], {prior["prediction_date"]: prior, day: pit})

    def test_comparison_requires_identical_oos_dates_and_reports_metric_deltas(self):
        rows = [row(index, target=index % 2) for index in range(10)]
        global_rows = []
        for source in rows:
            enriched = dict(source)
            for column in GLOBAL_RETURN_COLUMNS:
                enriched[column] = None if source["prediction_date"] == rows[0]["prediction_date"] else 0.01
            global_rows.append(enriched)
        core = run_rows(rows, initial_train_size=4, test_size=3)
        global_ = run_rows(global_rows, initial_train_size=4, test_size=3,
                           feature_columns=(*FEATURE_COLUMNS, *GLOBAL_RETURN_COLUMNS), experiment_name="tx_core_plus_global")
        report = comparison_report(core, global_, {"fixture": True}, augmented_name="tx_core_plus_global")
        for model in ("dummy", "logistic", "hist_gradient_boosting"):
            self.assertTrue(report["comparison"]["models"][model]["oos_prediction_dates"])
            self.assertIn("accuracy", report["comparison"]["models"][model]["overall_delta"])

    def test_positioning_attach_is_pit_safe_and_train_coverage_is_recorded(self):
        base = row(0, target=DOWN)
        cutoff = base["prediction_at"]
        pit = {"prediction_date": base["prediction_date"]}
        for column in POSITIONING_COLUMNS:
            pit[column] = 1.0
        for name in ("foreign_available_at", "investment_trust_available_at", "dealer_available_at", "pcr_available_at"):
            pit[name] = cutoff
        enriched, metadata = attach_positioning_features([base], {base["prediction_date"]: pit})
        self.assertTrue(all(enriched[0][column] == 1.0 for column in POSITIONING_COLUMNS))
        self.assertEqual(metadata["coverage"][INSTITUTIONAL_COLUMNS[0]], 1)
        pit["foreign_available_at"] = cutoff + timedelta(microseconds=1)
        with self.assertRaisesRegex(ValueError, "exceeds prediction_at"):
            attach_positioning_features([base], {base["prediction_date"]: pit})
        rows = [row(index, target=index % 2) for index in range(9)]
        for item in rows:
            for column in INSTITUTIONAL_COLUMNS:
                item[column] = None
        report = run_rows(rows, initial_train_size=4, test_size=2,
                          feature_columns=(*FEATURE_COLUMNS, *INSTITUTIONAL_COLUMNS))
        diagnostics = report["folds"][0]["training_feature_diagnostics"]
        self.assertIn(INSTITUTIONAL_COLUMNS[0], diagnostics["all_missing_features"])
        self.assertEqual(diagnostics["coverage"][INSTITUTIONAL_COLUMNS[0]]["coverage"], 0.0)

    def test_positioning_comparison_preserves_core_oos_dates(self):
        rows = [row(index, target=index % 2) for index in range(9)]
        positioned = []
        for source in rows:
            item = dict(source)
            for column in POSITIONING_COLUMNS:
                item[column] = None
            positioned.append(item)
        core = run_rows(rows, initial_train_size=4, test_size=2)
        institutional = run_rows(positioned, initial_train_size=4, test_size=2,
                                 feature_columns=(*FEATURE_COLUMNS, *INSTITUTIONAL_COLUMNS))
        pcr = run_rows(positioned, initial_train_size=4, test_size=2,
                       feature_columns=(*FEATURE_COLUMNS, *PCR_COLUMNS))
        both = run_rows(positioned, initial_train_size=4, test_size=2,
                        feature_columns=(*FEATURE_COLUMNS, *INSTITUTIONAL_COLUMNS, *PCR_COLUMNS))
        report = positioning_comparison_report({
            "tx_core": core, "tx_core_plus_institutional": institutional,
            "tx_core_plus_pcr": pcr, "tx_core_plus_institutional_pcr": both,
        }, {"fixture": True})
        self.assertIn("exploratory", report["exploratory_notice"])
        self.assertIn("tx_core_plus_pcr", report["relative_to_tx_core"])

    def test_coverage_guard_threshold_boundary_and_core_fallback_are_train_only(self):
        train = [{"core": 1.0, "optional_ok": 1.0 if index < 252 else None,
                  "optional_low": 1.0 if index < 251 else None} for index in range(504)]
        selection = select_coverage_guard_features(train, core_columns=("core",),
                                                   additional_columns=("optional_ok", "optional_low"))
        self.assertEqual(selection["retained_columns"], ["core", "optional_ok"])
        rejected = selection["excluded_columns"][0]
        self.assertEqual(rejected["column"], "optional_low")
        self.assertIn("non_null<252", rejected["reasons"])
        self.assertIn("coverage<0.5", rejected["reasons"])
        # The selector accepts only train rows: hypothetical test values cannot
        # alter this result.
        self.assertEqual(selection, select_coverage_guard_features(train, core_columns=("core",),
                                                                    additional_columns=("optional_ok", "optional_low")))
        rows = [row(index, target=index % 2) for index in range(8)]
        for item in rows:
            item["foreign_long_oi"] = None
        guarded = run_rows_with_coverage_guard(rows, additional_columns=("foreign_long_oi",),
                                               experiment_name="fixture", initial_train_size=4, test_size=2)
        guard = guarded["folds"][0]["coverage_guard"]
        self.assertTrue(guard["fallback_to_tx_core"])
        self.assertEqual(guard["retained_columns"], list(FEATURE_COLUMNS))


if __name__ == "__main__":
    unittest.main()
