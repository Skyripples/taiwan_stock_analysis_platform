from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from io_utils import atomic_write_json


class AtomicJsonTests(unittest.TestCase):
    def test_writes_utf8_json_and_replaces_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "payload.json"
            output.write_text('{"old": true}\n', encoding="utf-8")
            payload = {"status": "可使用", "value": None}
            atomic_write_json(output, payload)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(output.parent.glob("*.tmp")), [])

    def test_rejects_non_finite_json_without_overwriting_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "payload.json"
            original = b'{"valid": true}\n'
            output.write_bytes(original)
            with self.assertRaises(ValueError):
                atomic_write_json(output, {"invalid": float("nan")})
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
