from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from research import build_futures_daily_pit_alignment as module


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")


def tx_row(
    day: date, contract: str = "TX202601", *, expiry_week: bool = False, rank: str = "front",
    open_value: float = 100.0, high: float = 110.0, low: float = 95.0, close: float = 105.0,
    volume: int = 1000, open_interest: int = 2000,
) -> dict:
    return {
        "trade_date": day.isoformat(), "product": "TX", "contract": contract,
        "contract_rank": rank, "session": "regular", "open": open_value,
        "high": high, "low": low, "close": close, "volume": volume,
        "open_interest": open_interest, "days_to_expiry": 5 if expiry_week else 20,
        "is_expiry_week": expiry_week, "available_at": f"{day.isoformat()}T15:30:00+08:00",
    }


def factors(prior_day: date, prior_available: datetime, future_day: date | None = None, future_available: datetime | None = None):
    output = {}
    for factor in (*module.MARKET_FACTORS, *module.MACRO_FACTORS):
        rows = [{"value": 100.0, "source_date": prior_day, "available_at": prior_available}]
        if future_day and future_available:
            rows.append({"value": 999.0, "source_date": future_day, "available_at": future_available})
        output[factor] = rows
    return output


def institutional_row(day: date, available_at: datetime, base: int) -> dict:
    return {
        "source_date": day, "available_at": available_at,
        "long_open_interest": base + 100, "short_open_interest": base + 40,
        "net_open_interest": base + 60,
    }


def pcr_row(day: date, available_at: datetime, *, oi_available: bool = True, base: int = 0) -> dict:
    put_volume = 300 + base
    call_volume = 400 + base
    put_oi = 50 + base if oi_available else None
    call_oi = 75 + base if oi_available else None
    return {
        "source_date": day,
        "available_at": available_at,
        "put_volume": put_volume,
        "call_volume": call_volume,
        "volume_pcr": put_volume / call_volume,
        "put_oi": put_oi,
        "call_oi": call_oi,
        "oi_pcr": put_oi / call_oi if oi_available else None,
        "oi_available": oi_available,
    }


class FuturesDailyPitAlignmentTests(unittest.TestCase):
    def test_pcr_asof_join_uses_equality_and_never_uses_future_observation(self):
        anchor = date(2026, 10, 16)
        prediction = date(2026, 10, 19)
        cutoff = module.prediction_timestamp(prediction).astimezone(UTC)
        pcr = [
            pcr_row(anchor, cutoff),
            pcr_row(prediction, datetime(2026, 10, 19, 16, 0, tzinfo=UTC), base=999),
        ]

        row = module.align_rows(
            [anchor, prediction], prediction, prediction, [tx_row(anchor)],
            factors(anchor, datetime(2026, 10, 16, 0, 0, tzinfo=UTC)), pcr_observations=pcr,
        )[0]
        flags = json.loads(row["quality_flags"])

        self.assertEqual(row["put_volume"], 300)
        self.assertEqual(row["call_volume"], 400)
        self.assertEqual(row["pcr_source_date"], anchor)
        self.assertEqual(row["pcr_available_at"], cutoff)
        self.assertFalse(flags["missing_pcr_volume"])
        self.assertFalse(flags["missing_pcr_oi"])
        self.assertLessEqual(row["max_feature_available_at"], row["prediction_at"])

    def test_pcr_unavailable_oi_remains_null_without_forward_fill_or_zero(self):
        anchor = date(2023, 10, 19)
        prediction = date(2023, 10, 20)
        row = module.align_rows(
            [anchor, prediction], prediction, prediction, [tx_row(anchor)],
            factors(anchor, datetime(2023, 10, 19, 0, 0, tzinfo=UTC)),
            pcr_observations=[pcr_row(anchor, datetime(2023, 10, 19, 16, 0, tzinfo=UTC), oi_available=False)],
        )[0]
        flags = json.loads(row["quality_flags"])

        self.assertEqual(row["put_volume"], 300)
        self.assertEqual(row["call_volume"], 400)
        self.assertIsNotNone(row["volume_pcr"])
        self.assertIsNone(row["put_oi"])
        self.assertIsNone(row["call_oi"])
        self.assertIsNone(row["oi_pcr"])
        self.assertFalse(flags["missing_pcr_volume"])
        self.assertTrue(flags["missing_pcr_oi"])

    def test_institutional_asof_join_uses_latest_eligible_rows_and_prior_change(self):
        first = date(2023, 10, 20)
        second = date(2023, 10, 23)
        prediction = date(2023, 10, 24)
        prior_available = datetime(2023, 10, 20, 16, 0, tzinfo=UTC)
        available = datetime(2023, 10, 23, 16, 0, tzinfo=UTC)
        future_available = datetime(2023, 10, 24, 16, 0, tzinfo=UTC)
        institutions = {
            investor: [
                institutional_row(first, prior_available, 0),
                institutional_row(second, available, 10),
                institutional_row(prediction, future_available, 999),
            ] for investor in module.INSTITUTIONAL_INVESTORS
        }
        row = module.align_rows(
            [first, second, prediction], prediction, prediction,
            [tx_row(first), tx_row(second)], factors(second, datetime(2023, 10, 23, 0, 0, tzinfo=UTC)),
            institutional_positions=institutions,
        )[0]
        self.assertEqual(row["foreign_long_oi"], 110)
        self.assertEqual(row["foreign_short_oi"], 50)
        self.assertEqual(row["foreign_net_oi"], 70)
        self.assertEqual(row["foreign_net_oi_change"], 10)
        self.assertEqual(row["investment_trust_net_oi"], 70)
        self.assertEqual(row["dealer_net_oi"], 70)
        self.assertEqual(row["foreign_source_date"], second)
        self.assertEqual(row["foreign_available_at"], available)
        self.assertLessEqual(row["max_feature_available_at"], row["prediction_at"])

    def test_missing_institutional_history_is_null_and_flagged(self):
        anchor = date(2023, 10, 19)
        prediction = date(2023, 10, 20)
        row = module.align_rows(
            [anchor, prediction], prediction, prediction, [tx_row(anchor)],
            factors(anchor, datetime(2023, 10, 19, 0, 0, tzinfo=UTC)), institutional_positions={},
        )[0]
        flags = json.loads(row["quality_flags"])
        self.assertIsNone(row["foreign_net_oi"])
        self.assertIsNone(row["investment_trust_net_oi_change"])
        self.assertEqual(set(flags["missing_institutional_factors"]), {"外資", "投信", "自營商"})

    def test_engineered_features_use_only_completed_same_contract_sessions(self):
        sessions = [date(2026, 1, day) for day in (5, 6, 7, 8, 9, 12, 13)]
        tx_rows = [
            tx_row(day, close=100.0 + index, open_value=99.0 + index, high=102.0 + index,
                   low=98.0 + index, volume=1000 + index * 100, open_interest=2000 + index * 10)
            for index, day in enumerate(sessions[:-1])
        ]
        row = module.align_rows(
            sessions, sessions[-1], sessions[-1], tx_rows,
            factors(sessions[-2], datetime(2026, 1, 12, 0, 0, tzinfo=UTC)),
        )[0]
        self.assertAlmostEqual(row["tx_previous_day_return"], 105.0 / 104.0 - 1)
        self.assertAlmostEqual(row["tx_open_to_close_return"], 105.0 / 104.0 - 1)
        self.assertAlmostEqual(row["tx_intraday_range_pct"], 4.0 / 104.0)
        self.assertAlmostEqual(row["tx_volume_change_1d"], 1500.0 / 1400.0 - 1)
        self.assertEqual(row["tx_open_interest_change_1d"], 10.0)
        self.assertIsNotNone(row["tx_return_volatility_5d"])

    def test_pit_safe_spread_basis_and_foreign_position(self):
        anchor = date(2026, 1, 9)
        prediction = date(2026, 1, 12)
        available = datetime(2026, 1, 9, 8, 0, tzinfo=UTC)
        local = {
            "TAIEX_SPOT": [{"value": 100.0, "source_date": anchor, "available_at": available}],
            "FOREIGN_FUTURES_NET_OI": [{"value": -500.0, "source_date": anchor, "available_at": available}],
        }
        row = module.align_rows(
            [anchor, prediction], prediction, prediction,
            [tx_row(anchor, close=105.0), tx_row(anchor, "TX202602", rank="next", close=107.0)],
            factors(anchor, available), local,
        )[0]
        self.assertEqual(row["tx_front_next_spread"], 2.0)
        self.assertEqual(row["tx_basis"], 5.0)
        self.assertEqual(row["foreign_futures_net_oi"], -500.0)
        self.assertLessEqual(row["max_feature_available_at"], row["prediction_at"])

    def test_missing_optional_local_factors_remain_null_and_flagged(self):
        anchor = date(2026, 1, 9)
        prediction = date(2026, 1, 12)
        row = module.align_rows(
            [anchor, prediction], prediction, prediction, [tx_row(anchor)],
            factors(anchor, datetime(2026, 1, 9, 8, 0, tzinfo=UTC)),
        )[0]
        flags = json.loads(row["quality_flags"])
        self.assertIsNone(row["tx_basis"])
        self.assertIsNone(row["foreign_futures_net_oi"])
        self.assertEqual(set(flags["missing_local_factors"]), {"TAIEX_SPOT", "FOREIGN_FUTURES_NET_OI"})

    def test_prediction_anchor_and_previous_official_session(self):
        friday = date(2026, 1, 9)
        monday = date(2026, 1, 12)
        rows = module.align_rows(
            [friday, monday], monday, monday, [tx_row(friday)],
            factors(friday, datetime(2026, 1, 9, 21, 30, tzinfo=UTC)),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["prediction_date"], monday)
        self.assertEqual(rows[0]["prediction_at"], datetime(2026, 1, 12, 8, 30, tzinfo=TAIPEI))
        self.assertEqual(rows[0]["tx_source_trading_date"], friday)
        self.assertEqual(rows[0]["tx_contract"], "TX202601")

    def test_long_holiday_uses_previous_calendar_session(self):
        before_holiday = date(2026, 2, 11)
        after_holiday = date(2026, 2, 23)
        row = module.align_rows(
            [before_holiday, after_holiday], after_holiday, after_holiday,
            [tx_row(before_holiday)],
            factors(before_holiday, datetime(2026, 2, 11, 21, 30, tzinfo=UTC)),
        )[0]
        self.assertEqual(row["tx_source_trading_date"], before_holiday)

    def test_external_asof_join_rejects_same_day_us_close_in_est(self):
        prior = date(2026, 1, 9)
        prediction = date(2026, 1, 12)
        row = module.align_rows(
            [prior, prediction], prediction, prediction, [tx_row(prior)],
            factors(prior, datetime(2026, 1, 9, 21, 30, tzinfo=UTC),
                    prediction, datetime(2026, 1, 12, 21, 30, tzinfo=UTC)),
        )[0]
        self.assertEqual(row["sox_value"], 100.0)
        self.assertEqual(row["sox_source_date"], prior)
        self.assertLessEqual(row["max_feature_available_at"], row["prediction_at"])

    def test_external_asof_join_is_dst_safe(self):
        prior = date(2026, 7, 10)
        prediction = date(2026, 7, 13)
        row = module.align_rows(
            [prior, prediction], prediction, prediction, [tx_row(prior)],
            factors(prior, datetime(2026, 7, 10, 20, 30, tzinfo=UTC),
                    prediction, datetime(2026, 7, 13, 20, 30, tzinfo=UTC)),
        )[0]
        self.assertEqual(row["nasdaq_source_date"], prior)
        self.assertEqual(row["nasdaq_available_at"], datetime(2026, 7, 10, 20, 30, tzinfo=UTC))
        self.assertLessEqual(row["max_feature_available_at"], row["prediction_at"].astimezone(UTC))

    def test_missing_external_is_null_and_flagged_without_future_fill(self):
        prior = date(2026, 1, 9)
        prediction = date(2026, 1, 12)
        source = factors(prior, datetime(2026, 1, 12, 1, 0, tzinfo=UTC))
        source["SOX"] = [{"value": 999.0, "source_date": prediction,
                          "available_at": datetime(2026, 1, 12, 21, 30, tzinfo=UTC)}]
        row = module.align_rows([prior, prediction], prediction, prediction, [tx_row(prior)], source)[0]
        self.assertIsNone(row["sox_value"])
        self.assertIn("SOX", json.loads(row["quality_flags"])["missing_external_factors"])

    def test_roll_metadata_is_preserved_without_continuous_return(self):
        first = date(2026, 1, 20)
        anchor = date(2026, 1, 21)
        prediction = date(2026, 1, 22)
        row = module.align_rows(
            [first, anchor, prediction], prediction, prediction,
            [tx_row(first, "TX202601"), tx_row(anchor, "TX202602", expiry_week=True)],
            factors(anchor, datetime(2026, 1, 21, 21, 30, tzinfo=UTC)),
        )[0]
        flags = json.loads(row["quality_flags"])
        self.assertTrue(flags["tx_roll_regime"])
        self.assertEqual(set(flags["tx_roll_reasons"]), {"expiry_week", "front_contract_changed"})
        self.assertEqual(row["tx_contract"], "TX202602")
        self.assertEqual(row["tx_days_to_expiry"], 5)

    def test_prediction_dates_unique_and_parquet_rerun_idempotent(self):
        prior = date(2026, 1, 9)
        prediction = date(2026, 1, 12)
        rows = module.align_rows(
            [prior, prediction], prediction, prediction, [tx_row(prior)],
            factors(prior, datetime(2026, 1, 9, 21, 30, tzinfo=UTC)),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module.write_rows(rows, root)
            module.write_rows(rows, root)
            import pyarrow.parquet as pq
            path = root / "partition_year=2026/part-00000.parquet"
            stored = pq.ParquetFile(path).read().to_pylist()
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["prediction_date"], prediction)
            self.assertEqual(stored[0]["prediction_at"].hour, 8)
            self.assertEqual(stored[0]["prediction_at"].minute, 30)

    def test_global_canonical_root_is_read_only_and_output_is_separate(self):
        self.assertNotEqual(module.GLOBAL_ROOT, module.OUTPUT_ROOT)
        self.assertFalse(module.OUTPUT_ROOT.is_relative_to(module.GLOBAL_ROOT))


if __name__ == "__main__":
    unittest.main()
