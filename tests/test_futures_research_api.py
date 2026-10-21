from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from api.routes.futures import router
from api.services import futures as service


class FuturesResearchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        cls.client = TestClient(app)

    def test_summary_matches_canonical_research_dataset(self):
        response = self.client.get("/api/v1/futures/research/summary")
        self.assertEqual(response.status_code, 200)
        summary = response.json()
        self.assertEqual(summary["rows"], 1218)
        self.assertEqual(summary["target_available"], 1215)
        self.assertEqual(summary["target_missing"], 3)
        self.assertEqual(summary["class_distribution"], {"up": 499, "neutral": 269, "down": 447})
        self.assertEqual(summary["first_prediction_date"], "2021-10-14")
        self.assertEqual(summary["last_prediction_date"], "2026-10-20")
        self.assertEqual(set(summary["feature_coverage"]), {
            "tx_front_next_spread", "tx_basis", "foreign_net_oi", "investment_trust_net_oi",
            "dealer_net_oi", "volume_pcr", "oi_pcr",
        })

    def test_daily_filters_limit_public_schema_and_json_nulls(self):
        response = self.client.get(
            "/api/v1/futures/research/daily?start=2026-10-15&end=2026-10-20&limit=2"
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 2)
        rows = payload["results"]
        self.assertEqual(len({row["prediction_date"] for row in rows}), len(rows))
        self.assertTrue(all("source_date" not in row for row in rows))
        self.assertTrue(all("target_quality_flags" not in row for row in rows))
        self.assertTrue(all(set(row) == set(service.RESEARCH_PUBLIC_COLUMNS) | {"quality_flags"} for row in rows))
        self.assertTrue(all("2026-10-15" <= row["prediction_date"] <= "2026-10-20" for row in rows))

    def test_null_target_and_feature_are_serializable(self):
        response = self.client.get("/api/v1/futures/research/daily?start=2026-10-20&end=2026-10-20")
        self.assertEqual(response.status_code, 200)
        row = response.json()["results"][0]
        self.assertIsNone(row["next_trading_day_regular_return"])
        self.assertIsNone(row["direction_class"])
        missing_oi = self.client.get("/api/v1/futures/research/daily?start=2023-01-03&end=2023-01-03")
        self.assertEqual(missing_oi.status_code, 200)
        self.assertIsNone(missing_oi.json()["results"][0]["oi_pcr"])

    def test_invalid_range_is_rejected(self):
        response = self.client.get("/api/v1/futures/research/daily?start=2026-10-20&end=2026-10-15")
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
