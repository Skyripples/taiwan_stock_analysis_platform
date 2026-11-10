from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MlPredictionDayCopyTests(unittest.TestCase):
    def test_day_real_uses_day_0815_copy_without_changing_b4_defaults(self):
        page = (ROOT / "ml-prediction.html").read_text(encoding="utf-8")
        script = (ROOT / "ml-prediction.js").read_text(encoding="utf-8")
        self.assertIn('id="experimentSubtitle"', page)
        self.assertIn('id="modelTaskLabel"', page)
        self.assertIn("sourceMode === 'day-real'", script)
        self.assertIn("08:15 前可用", script)
        self.assertIn("08:45 開盤至正常日盤收盤", script)
        self.assertIn("./preview-data/b4-forward-live.json", script)
        self.assertIn("./mock-data/b4-ml-preview.json", script)


if __name__ == "__main__":
    unittest.main()
