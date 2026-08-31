from __future__ import annotations

import json
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import patch

import requests

from scripts.collectors.framework import (
    AllSourcesUnavailable,
    CircuitBreakerOpen,
    CollectorPolicy,
    OfficialHttpClient,
    StructuredLog,
    atomic_json,
    missing_ranges,
)


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload or {}
        self.encoding = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"status={self.status_code}")
            error.response = self
            raise error

    def json(self) -> dict:
        return self._payload


class FakeSession(requests.Session):
    def __init__(self, responses: list[FakeResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class CollectorFrameworkTests(unittest.TestCase):
    def test_forward_and_backward_gap_planning(self) -> None:
        covered = [(date(2024, 1, 3), date(2024, 1, 8))]
        self.assertEqual(
            missing_ranges(date(2024, 1, 1), date(2024, 1, 10), covered),
            [(date(2024, 1, 1), date(2024, 1, 2)), (date(2024, 1, 9), date(2024, 1, 10))],
        )
        self.assertEqual(
            missing_ranges(date(2024, 1, 1), date(2024, 1, 10), covered, refresh=True),
            [(date(2024, 1, 1), date(2024, 1, 10))],
        )

    def test_official_primary_to_mirror_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession([FakeResponse(500), FakeResponse(200, {"stat": "OK"})])
            client = OfficialHttpClient(
                CollectorPolicy(retries=0, request_interval_seconds=0),
                "fixture", StructuredLog(Path(directory) / "log.jsonl"), lambda: session,
            )
            self.assertEqual(client.request_json("GET", ["https://official-primary", "https://official-mirror"]), {"stat": "OK"})
            self.assertEqual(session.calls, 2)

    def test_429_opens_bounded_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession([FakeResponse(429)])
            client = OfficialHttpClient(
                CollectorPolicy(retries=2, request_interval_seconds=0, throttle_circuit_threshold=1, circuit_cooldown_seconds=60),
                "fixture", StructuredLog(Path(directory) / "log.jsonl"), lambda: session,
            )
            with self.assertRaises(AllSourcesUnavailable):
                client.request_json("GET", "https://official")
            self.assertEqual(session.calls, 1)

    def test_primary_and_mirror_failure_stops_following_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession([FakeResponse(500), FakeResponse(500)])
            client = OfficialHttpClient(
                CollectorPolicy(retries=0, request_interval_seconds=0, source_failure_circuit_threshold=1),
                "fixture", StructuredLog(Path(directory) / "log.jsonl"), lambda: session,
            )
            with self.assertRaises(AllSourcesUnavailable):
                client.request_json("GET", ["https://primary", "https://mirror"])
            with self.assertRaises(AllSourcesUnavailable):
                client.request_json("GET", ["https://primary", "https://mirror"])
            self.assertEqual(session.calls, 2, "open source circuits must prevent per-date retries")

    def test_pit_writes_partial_summary_after_source_stop(self) -> None:
        from scripts.research import backfill_taiwan_pit_foundation as pit

        class StoppedClient:
            requests = 2

            def json(self, *args, **kwargs):
                raise AllSourcesUnavailable(["primary", "mirror"], ["fixture disconnect"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text(json.dumps({
                "history_years": 5,
                "sources": {"twse_issued_shares": "primary", "twse_corporate_actions": "primary", "tpex_issued_shares": "primary", "tpex_corporate_actions": "primary"},
                "source_fallbacks": {"twse_issued_shares": ["mirror"]},
            }), encoding="utf-8")
            args = Namespace(years=5, start="2024-01-02", end="2024-01-04", market=["TWSE"], dataset="shares", refresh=False, dry_run=False, workers=1, batch_size=1, resume=True)
            with (
                patch.object(pit, "ROOT", root), patch.object(pit, "CONFIG", config),
                patch.object(pit, "COVERAGE", root / "coverage.json"), patch.object(pit, "CHECKPOINT", root / "checkpoint.json"),
                patch.object(pit, "MANIFEST", root / "manifest.json"), patch.object(pit, "SUMMARY", root / "summary.json"),
                patch.object(pit, "Client", StoppedClient),
                patch.object(pit, "trading_dates", return_value=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)]),
                patch.object(pit, "universe", return_value={"TWSE": {"2330"}, "TPEX": set()}),
                patch.object(pit, "build_market_cap", return_value=0), patch.object(pit, "build_sessions", return_value=[]),
            ):
                result = pit.run(args)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["stopped_reason"], "all_official_sources_unavailable")
            self.assertEqual(result["remaining_gap"]["share_requests"], 3)
            self.assertEqual(json.loads((root / "checkpoint.json").read_text())["status"], "partial")

    def test_atomic_summary_and_structured_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / "summary.json", {"status": "ok", "rows": 3})
            StructuredLog(root / "run.jsonl").emit("batch_committed", rows=3)
            self.assertEqual(json.loads((root / "summary.json").read_text())["rows"], 3)
            self.assertEqual(json.loads((root / "run.jsonl").read_text())["event"], "batch_committed")


if __name__ == "__main__":
    unittest.main()
