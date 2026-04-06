import json
import tempfile
import unittest
from pathlib import Path

from orthanc_tools.state import atomic_write_json, atomic_write_text, load_json, now_iso


class StateTests(unittest.TestCase):
    def test_atomic_write_text_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.txt"
            atomic_write_text(path, "hello\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")

    def test_atomic_write_json_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            payload = {"updated_at": now_iso(), "value": 1}
            atomic_write_json(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["value"], 1)
            self.assertEqual(load_json(path)["value"], 1)
