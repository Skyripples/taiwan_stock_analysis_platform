from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ChipsAnalysisDailyTableTests(unittest.TestCase):
    def test_chips_sections_render_ten_unaggregated_daily_rows(self):
        html = (ROOT / "chips-analysis.html").read_text(encoding="utf-8")
        script = (ROOT / "chips-analysis.js").read_text(encoding="utf-8")

        for table_body in ("institutionalRows", "futuresRows", "marginRows"):
            self.assertIn(f'id="{table_body}"', html)
        self.assertNotIn('id="dailyChipsRows"', html)
        self.assertNotIn("籌碼摘要", html)
        self.assertIn("return rows.slice(-10);", script)
        self.assertIn("renderInstitutionalTable", script)
        self.assertIn("renderFuturesTable", script)
        self.assertIn("renderMarginTable", script)
        self.assertNotIn("foreign_20d_sum", script)
        self.assertNotIn("investment_trust_20d_sum", script)
        self.assertNotIn("net_change_20d", script)
        self.assertNotIn('data-range="20"', html)
        for removed in ("trendTitle", "institutionalChart", "futuresChart", "marginChart", "PlatformXYChart"):
            self.assertNotIn(removed, html + script)


if __name__ == "__main__":
    unittest.main()
