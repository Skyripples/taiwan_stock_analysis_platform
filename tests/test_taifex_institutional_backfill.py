from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from providers.taifex_provider import TaifexProvider
from research import backfill_futures_institutional_positions as module


UTC = timezone.utc


def values(long_oi: int, short_oi: int) -> list[str]:
    return ["0"] * 6 + [str(long_oi), "1000", str(short_oi), "400", str(long_oi - short_oi), "600"]


def raw(day: date, fetched_at: datetime) -> dict:
    return {
        "trade_date": day.isoformat(),
        "fetched_at": fetched_at.isoformat(),
        "rows": [
            ["1", "臺股期貨", "自營商", *values(80, 50)],
            ["投信", *values(70, 20)],
            ["外資", *values(100, 40)],
        ],
    }


class Response:
    def __init__(self, text: str):
        self.text = text
        self.encoding = None

    def raise_for_status(self) -> None:
        return None


class Session:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def post(self, _url: str, **kwargs):
        self.calls.append(kwargs)
        return Response(self.text)


class TaifexInstitutionalProviderTests(unittest.TestCase):
    def test_normalizes_three_investors_from_one_response(self):
        records = TaifexProvider().normalize_investors(raw(date(2026, 10, 19), datetime.now(UTC)))
        self.assertEqual({row["metadata"]["investor_type"] for row in records}, {"外資", "投信", "自營商"})
        self.assertEqual(next(row for row in records if row["metadata"]["investor_type"] == "外資")["net_position"]["open_interest"], 60)
        self.assertTrue(all(TaifexProvider().validate([row]) for row in records))

    def test_product_and_investor_labels_tolerate_spacing_and_tai_variant(self):
        payload = raw(date(2026, 10, 19), datetime.now(UTC))
        payload["rows"][0][1] = "台 股 期 貨"
        payload["rows"][0][2] = "自 營 商"
        payload["rows"][1][0] = "投 信"
        payload["rows"][2][0] = "外 資 及 陸 資"
        records = TaifexProvider().normalize_investors(payload)
        self.assertEqual({row["metadata"]["investor_type"] for row in records}, {"外資", "投信", "自營商"})

    def test_legacy_normalize_still_returns_only_foreign_row(self):
        payload = raw(date(2026, 10, 19), datetime.now(UTC))
        payload["rows"] = [["1", "臺股期貨", "外資", *values(100, 40)]]
        records = TaifexProvider().normalize(payload)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["metadata"]["investor_type"], "外資")

    def test_fetch_for_date_posts_requested_date_and_requires_exact_response_date(self):
        day = date(2026, 10, 19)
        html = "<div>日期 2026/10/19</div><table><tr><td>1</td><td>臺股期貨</td><td>外資</td>" + "".join(
            f"<td>{value}</td>" for value in values(100, 40)
        ) + "</tr></table>"
        session = Session(html)
        result = TaifexProvider().fetch_for_date(day, session=session)
        self.assertEqual(result["trade_date"], day.isoformat())
        self.assertEqual(session.calls[0]["data"]["queryDate"], "2026/10/19")
        mismatch = Session(html.replace("2026/10/19", "2026/10/18"))
        self.assertIsNone(TaifexProvider().fetch_for_date(day, session=mismatch))

    def test_latest_fetch_keeps_lookback_behavior(self):
        provider = TaifexProvider()
        provider.lookback_days = 2
        expected = raw(date(2026, 10, 19), datetime.now(UTC))
        provider._session = Mock(return_value=object())
        provider.fetch_for_date = Mock(side_effect=[None, expected])
        self.assertIs(provider.fetch(), expected)
        self.assertEqual(provider.fetch_for_date.call_count, 2)

    def test_provider_source_is_valid_utf8_with_chinese_literals(self):
        source = (ROOT / "scripts/providers/taifex_provider.py").read_bytes().decode("utf-8")
        self.assertIn('investor_names = ("外資", "投信", "自營商")', source)


class TaifexInstitutionalBackfillTests(unittest.TestCase):
    def test_old_invalid_official_empty_checkpoint_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint.json"
            path.write_text(
                '{"version":"v4.6-1.0","dates":{"2026-10-14":{"status":"official_empty"},'
                '"2026-10-13":{"status":"success"}}}', encoding="utf-8",
            )
            checkpoint = module.load_checkpoint(path)
        self.assertNotIn("2026-10-14", checkpoint["dates"])
        self.assertEqual(checkpoint["dates"]["2026-10-13"]["status"], "success")

    def test_source_retention_is_classified_without_network(self):
        self.assertEqual(module.source_retention_start(date(2026, 10, 20)), date(2023, 10, 20))

    def test_conservative_availability_and_current_session_exclusion(self):
        day = date(2026, 10, 19)
        available = module.institutional_available_at(day)
        self.assertEqual(available, datetime(2026, 10, 19, 16, 0, tzinfo=UTC))
        provider = TaifexProvider()
        self.assertEqual(module.canonical_rows(provider, raw(day, available)), module.canonical_rows(provider, raw(day, available)))
        self.assertEqual(len(module.canonical_rows(provider, raw(day, available))), 3)
        self.assertEqual(module.canonical_rows(provider, raw(day, available.replace(hour=15, minute=59))), [])

    def test_atomic_upsert_is_idempotent(self):
        day = date(2026, 10, 19)
        rows = module.canonical_rows(TaifexProvider(), raw(day, module.institutional_available_at(day)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.write_rows(rows, root)
            module.write_rows(rows, root)
            stored = module.read_partition(module.canonical_path(root, day.year))
            self.assertEqual(len(stored), 3)
            self.assertEqual(len({(row["trade_date"], row["investor_type"]) for row in stored}), 3)

    def test_resume_skips_complete_date_without_network_request(self):
        day = date(2026, 10, 19)
        options = SimpleNamespace(start=day, end=day, resume=True, delay=0.0)
        provider = TaifexProvider()
        provider._session = Mock(return_value=object())
        provider.fetch_for_date = Mock(return_value=raw(day, module.institutional_available_at(day)))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(module, "CANONICAL_ROOT", root / "daily_positions"), \
                    patch.object(module, "CHECKPOINT", root / "checkpoint.json"), \
                    patch.object(module, "SUMMARY", root / "summary.json"), \
                    patch.object(module, "get_trading_days_in_range", return_value=([day], [])):
                first = module.run(options, provider=provider)
                second = module.run(options, provider=provider)
                self.assertEqual(first["written_rows"], 3)
                self.assertEqual(second["written_rows"], 0)
                self.assertEqual(second["network_requests"], 0)
                self.assertEqual(len(module.existing_keys()), 3)
        self.assertEqual(provider.fetch_for_date.call_count, 1)

    def test_historical_official_empty_is_checkpointed_without_rows(self):
        day = date(2025, 1, 2)
        options = SimpleNamespace(start=day, end=day, resume=True, delay=0.0)
        provider = TaifexProvider()
        provider._session = Mock(return_value=object())
        provider.fetch_for_date = Mock(return_value=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(module, "CANONICAL_ROOT", root / "daily_positions"), \
                    patch.object(module, "CHECKPOINT", root / "checkpoint.json"), \
                    patch.object(module, "SUMMARY", root / "summary.json"), \
                    patch.object(module, "get_trading_days_in_range", return_value=([day], [])):
                result = module.run(options, provider=provider)
                checkpoint = module.load_checkpoint()
        self.assertEqual(result["official_empty"], 1)
        self.assertEqual(checkpoint["dates"][day.isoformat()]["status"], "official_empty")


if __name__ == "__main__":
    unittest.main()
