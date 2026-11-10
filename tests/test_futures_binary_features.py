from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from research.futures_binary import features as module


class FuturesBinaryFeaturesTests(unittest.TestCase):
    def test_default_is_only_thirteen_tx_core_columns(self):
        self.assertEqual(module.feature_names(), module.FEATURE_GROUPS["tx_core"])
        self.assertEqual(len(module.feature_names()), 13)

    def test_all_candidates_match_actual_v46_schema_and_are_numeric(self):
        import pyarrow as pa
        from research.build_futures_research_dataset import output_schema

        schema = output_schema()
        names = module.feature_names(module.FEATURE_GROUPS)
        self.assertEqual(len(names), 40)
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            kind = schema.field(name).type
            self.assertTrue(pa.types.is_integer(kind) or pa.types.is_floating(kind)
                            or pa.types.is_boolean(kind), name)

    def test_group_and_column_order_are_explicit(self):
        expected = module.FEATURE_GROUPS["global"] + module.FEATURE_GROUPS["tx_core"]
        self.assertEqual(module.feature_names(["global", "tx_core"]), expected)
        columns = ("tx_volume", "tx_days_to_expiry")
        projected = module.select_feature_rows([dict.fromkeys(columns, 1)], columns)
        self.assertEqual(tuple(projected[0]), columns)

    def test_targets_and_metadata_cannot_enter_x(self):
        from research.build_futures_research_dataset import output_schema

        for name in output_schema().names + ["binary_direction_target", "target_future_column"]:
            if name not in module.FEATURE_ALLOWLIST:
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Excluded feature"):
                    module.select_feature_rows([], [name])

    def test_dgs10_is_quarantined_even_when_explicitly_requested(self):
        self.assertNotIn("dgs10_value", module.FEATURE_ALLOWLIST)
        self.assertIn("vintage", module.exclusion_reason("dgs10_value"))
        with self.assertRaisesRegex(ValueError, "Excluded feature"):
            module.select_feature_rows([{"dgs10_value": 4.1}], ["dgs10_value"])

    def test_unknown_numeric_column_excluded_by_default(self):
        row = dict.fromkeys(module.feature_names(), 1)
        row.update({"future_return": 100.0, "next_trading_day_regular_return": 0.1})
        self.assertEqual(set(module.select_feature_rows([row])[0]), set(module.feature_names()))
        with self.assertRaisesRegex(ValueError, "allowlist"):
            module.validate_feature_names(["future_return"])

    def test_optional_missing_and_missing_labels_do_not_drop_rows(self):
        rows = [{"oi_pcr": None, "binary_direction_target": None},
                {"oi_pcr": 1.2, "binary_direction_target": 1}]
        before = copy.deepcopy(rows)
        result = module.select_feature_rows(rows, ["oi_pcr"])
        self.assertEqual(result, [{"oi_pcr": None}, {"oi_pcr": 1.2}])
        self.assertEqual(rows, before)
        result[0]["oi_pcr"] = 10
        self.assertIsNone(rows[0]["oi_pcr"])

    def test_entire_missing_column_retained(self):
        self.assertEqual(module.select_feature_rows([{"volume_pcr": None}] * 3, ["volume_pcr"]),
                         [{"volume_pcr": None}] * 3)

    def test_absent_column_is_schema_error(self):
        with self.assertRaisesRegex(ValueError, "Missing feature column"):
            module.select_feature_rows([{}], ["oi_pcr"])

    def test_bad_feature_values_fail(self):
        for value in [float("nan"), float("inf"), "100", object()]:
            with self.assertRaisesRegex(ValueError, "finite numeric"):
                module.select_feature_rows([{"tx_volume": value}], ["tx_volume"])

    def test_boolean_regime_fields_preserved(self):
        self.assertEqual(module.select_feature_rows([{"tx_roll_regime": True}], ["tx_roll_regime"]),
                         [{"tx_roll_regime": True}])

    def test_invalid_group_requests_fail(self):
        for groups in [[], "tx_core", ["unknown"], ["tx_core", "tx_core"], [None]]:
            with self.assertRaises(ValueError):
                module.feature_names(groups)

    def test_invalid_column_requests_fail(self):
        for names in [[], "tx_volume", ["tx_volume", "tx_volume"], [None]]:
            with self.assertRaises(ValueError):
                module.validate_feature_names(names)

    def test_group_contract_cannot_be_mutated(self):
        with self.assertRaises(TypeError):
            module.FEATURE_GROUPS["tx_core"] = ("target_available_at",)


if __name__ == "__main__":
    unittest.main()
