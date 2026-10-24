from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_events


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class DispositionEventTests(unittest.TestCase):
    def test_parses_roc_disposition_period(self):
        self.assertEqual(
            fetch_events.parse_disposition_period("115/10/21～115/10/28"),
            ("2026-10-21", "2026-10-28"),
        )
        self.assertIsNone(fetch_events.parse_disposition_period("not a period"))

    def test_twse_keeps_only_current_four_digit_company_stocks(self):
        payload = {"data": [
            [1, "115/10/20", "2424", "隴華", 1, "連續三次", "115/10/21～115/10/28"],
            [2, "115/10/19", "053040", "可寧衛富邦5C購01", 1, "連續三次", "115/10/20～115/10/27"],
            [3, "115/10/10", "1111", "已出關", 1, "連續三次", "115/10/11～115/10/17"],
        ]}
        with patch.object(fetch_events.SESSION, "get", return_value=FakeResponse(payload)):
            events = fetch_events.fetch_twse_disposition_events(date(2026, 10, 25))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].stock_id, "2424")
        self.assertEqual(events[0].disposition_end_date, "2026-10-28")
        self.assertIn("預計出關 2026-10-28", events[0].title)

    def test_tpex_strips_detail_link_and_keeps_current_row(self):
        payload = {"tables": [{"data": [
            [1, "115/10/21", "3287", "廣寰科(../../detail.html?code=3287)", 1, "115/10/22~115/10/29"],
            [2, "115/10/01", "8358", "金居(../../detail.html?code=8358)", 1, "115/10/08~115/10/15"],
        ]}]}
        with patch.object(fetch_events.SESSION, "get", return_value=FakeResponse(payload)):
            events = fetch_events.fetch_tpex_disposition_events(date(2026, 10, 25))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].stock_name, "廣寰科")
        self.assertEqual(events[0].disposition_start_date, "2026-10-22")

    def test_non_disposition_schema_stays_unchanged(self):
        data = fetch_events.build([
            fetch_events.EventItem("2330", "台積電", "earnings_call", "法說會", "2026-10-30", "Yahoo", "https://example.test"),
            fetch_events.EventItem(
                "2424", "隴華", "disposition", "處置中", "2026-10-21", "TWSE", "https://example.test",
                disposition_start_date="2026-10-21", disposition_end_date="2026-10-28",
            ),
        ], 2026)
        by_type = {event["type"]: event for event in data["events"]}
        ordinary = by_type["earnings_call"]
        disposition = by_type["disposition"]
        self.assertNotIn("disposition_end_date", ordinary)
        self.assertEqual(disposition["disposition_end_date"], "2026-10-28")

    def test_home_renders_current_dispositions_and_exit_date(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "home.js").read_text(encoding="utf-8")
        css = (ROOT / "platform.css").read_text(encoding="utf-8")
        for element_id in ("dispositionReminderState", "dispositionReminderList", "dispositionReminderCount"):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('event.type === "disposition"', script)
        self.assertIn("disposition_end_date", script)
        self.assertIn("預計出關", script)
        self.assertIn(".disposition-reminder-list", css)


if __name__ == "__main__":
    unittest.main()
