from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DayShadowPublicContractTests(unittest.TestCase):
    def test_day_real_uses_only_https_readonly_endpoint(self):
        script = (ROOT / "ml-prediction.js").read_text(encoding="utf-8")
        self.assertIn("https://172-238-20-217.ip.linodeusercontent.com/api/v1/futures/day-shadow.json", script)
        self.assertIn("sourceMode === 'day-real'", script)
        self.assertIn("./preview-data/b4-forward-live.json", script)

    def test_nginx_endpoint_is_exact_file_only_and_pages_cors_only(self):
        config = (ROOT / "deploy" / "nginx-day-shadow-readonly.conf").read_text(encoding="utf-8")
        self.assertIn("location = /api/v1/futures/day-shadow.json", config)
        self.assertIn("alias /opt/taiwan-stock-day-experiment/readonly-api/day-shadow.json", config)
        self.assertIn('Access-Control-Allow-Origin "https://skyripples.github.io"', config)
        self.assertIn("limit_except GET { deny all; }", config)
        self.assertNotIn("state/", config)
        self.assertNotIn("artifacts", config)

    def test_exported_preview_contains_no_server_or_artifact_paths(self):
        payload_path = ROOT / "preview-data" / "day-shadow.json"
        if not payload_path.exists():
            self.skipTest("local readonly preview has not been exported")
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("/opt/", serialized)
        self.assertNotIn("model_artifact", serialized)
        self.assertNotIn("training_hash", serialized)

    def test_exporter_keeps_only_the_derived_json_group_readable(self):
        exporter = (ROOT / "scripts" / "operations" / "export_futures_day_shadow_json.py").read_text(encoding="utf-8")
        self.assertIn("os.chmod(destination, 0o640)", exporter)
