from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class XYChartUiTests(unittest.TestCase):
    def test_shared_chart_has_grid_ticks_and_axis_titles(self):
        script = (ROOT / "xy-chart.js").read_text(encoding="utf-8")
        css = (ROOT / "xy-chart.css").read_text(encoding="utf-8")
        for marker in ("xy-grid", "xy-tick", "xy-axis-title", "PlatformXYChart"):
            self.assertIn(marker, script + css)

    def test_product_charts_use_shared_renderer(self):
        for page, script_name in (("crypto.html", "crypto.js"), ("futures.html", "futures.js"), ("bonds.html", "bonds.js"), ("stock-analysis.html", "stock-analysis.js"), ("chips-analysis.html", "chips-analysis.js")):
            html = (ROOT / page).read_text(encoding="utf-8")
            script = (ROOT / script_name).read_text(encoding="utf-8")
            self.assertIn("xy-chart.css", html)
            self.assertIn("xy-chart.js", html)
            self.assertIn("PlatformXYChart", script)


if __name__ == "__main__":
    unittest.main()
