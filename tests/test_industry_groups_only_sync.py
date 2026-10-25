from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_stock_database as sync


COMPLETE = {"complete_snapshot": True, "registry": [{"key": "pcb", "name": "PCB"}], "by_symbol": {}, "updated_at": "2026-10-25T00:00:00+00:00"}


class Transaction:
    def __enter__(self): return self
    def __exit__(self, *_): return False


class Connection:
    def transaction(self): return Transaction()
    def close(self): pass


class IndustryGroupsOnlyTests(unittest.TestCase):
    def test_cli_mode_is_mutually_exclusive_and_can_pair_with_schema_init(self):
        with patch.object(sys, "argv", ["sync", "--init-schema", "--industry-groups-only"]):
            options = sync.arguments()
        self.assertTrue(options.init_schema)
        self.assertEqual(sync.selected_mode(options), "industry-groups-only")

    def test_required_snapshot_missing_or_incomplete_fails(self):
        for payload in (None, {"complete_snapshot": False}):
            with self.subTest(payload=payload), patch.object(sync, "read_json", return_value=payload):
                with self.assertRaisesRegex(RuntimeError, "Complete canonical"):
                    sync.sync_industry_groups(MagicMock(), MagicMock(), required=True)

    @patch.object(sync, "StockRepository")
    @patch.object(sync, "sync_industry_groups")
    def test_only_mode_calls_existing_group_reconcile(self, reconcile, repository):
        reconcile.return_value = 3
        self.assertEqual(sync.sync_industry_groups_only(MagicMock()), 3)
        reconcile.assert_called_once()
        self.assertTrue(reconcile.call_args.kwargs["required"])
        repository.assert_called_once()

    def test_main_only_mode_skips_batches_and_runs_schema_then_groups(self):
        options = argparse.Namespace(init_schema=True, industry_groups_only=True, full=False, daily=False, monthly=False,
                                     quarterly=False, symbol=None, dry_run=False)
        connection = Connection()
        with patch.object(sync, "arguments", return_value=options), \
             patch.object(sync, "read_json", return_value=COMPLETE), \
             patch.object(sync, "build_batches", side_effect=AssertionError("must not build batches")), \
             patch.object(sync, "connect", return_value=connection), \
             patch.object(sync, "apply_migrations") as migrations, \
             patch.object(sync, "sync_industry_groups_only", return_value=3) as only:
            self.assertEqual(sync.main(), 0)
        migrations.assert_called_once_with(connection)
        only.assert_called_once_with(connection)

    def test_full_mode_still_builds_and_calls_normal_sync(self):
        options = argparse.Namespace(init_schema=False, industry_groups_only=False, full=True, daily=False, monthly=False,
                                     quarterly=False, symbol=None, dry_run=True)
        with patch.object(sync, "arguments", return_value=options), patch.object(sync, "build_batches", return_value=({"stocks": []}, None)) as batches:
            self.assertEqual(sync.main(), 0)
        batches.assert_called_once()

    def test_source_keeps_official_industry_untouched_and_only_mode_has_no_fact_upserts(self):
        source = Path(sync.__file__).read_text(encoding="utf-8")
        section = source[source.index("def sync_industry_groups_only"):source.index("def sync(")]
        self.assertNotIn('upsert_many("stocks"', section)
        self.assertIn("required=True", section)


if __name__ == "__main__":
    unittest.main()
