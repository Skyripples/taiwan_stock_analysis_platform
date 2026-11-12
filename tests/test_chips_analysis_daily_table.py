from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ChipsAnalysisDailyTableTests(unittest.TestCase):
    def test_summary_renders_five_unaggregated_daily_rows(self):
        html = (ROOT / "chips-analysis.html").read_text(encoding="utf-8")
        script = (ROOT / "chips-analysis.js").read_text(encoding="utf-8")

        self.assertIn('id="dailyChipsRows"', html)
        self.assertIn("最近 5 個交易日的單日數據", html)
        self.assertIn("return rows.slice(-5);", script)
        self.assertIn("renderDailyTable(dailyRows())", script)
        self.assertNotIn("foreign_20d_sum", script)
        self.assertNotIn("investment_trust_20d_sum", script)
        self.assertNotIn("net_change_20d", script)
        self.assertNotIn('data-range="20"', html)


if __name__ == "__main__":
    unittest.main()
