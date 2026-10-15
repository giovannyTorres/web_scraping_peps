"""Offline demo safety and historical replay, using only invented data."""
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from scripts.demo_explainability import RUNTIME, _empty_demo_directory, run_demo


class ExplainabilityDemoTests(unittest.TestCase):
    def test_refuses_paths_outside_runtime_without_creating_them(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "do-not-create"
            with self.assertRaisesRegex(ValueError, "runtime"):
                _empty_demo_directory(target)
            self.assertFalse(target.exists())

    def test_refuses_nonempty_directory_without_overwriting(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME, prefix="demo-test-") as directory:
            marker = Path(directory) / "existing-user-data.txt"
            marker.write_text("preserve this bank", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "vacío"):
                run_demo(Path(directory))
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve this bank")
            self.assertEqual(list(Path(directory).iterdir()), [marker])

    def test_offline_demo_preserves_and_replays_historical_evaluations(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=RUNTIME, prefix="demo-test-") as directory:
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")), patch.object(urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
                summary = run_demo(Path(directory))
            current = {row["entity_id"]: row for row in summary["current"]}
            self.assertEqual(len(current), 6)
            self.assertTrue(all(row["replay_matched"] for row in current.values()))
            self.assertTrue(summary["historical_before_resolution"]["replay_matched"])
            self.assertEqual(summary["historical_before_resolution"]["evidence_count"], 1)
            self.assertEqual(current["demo-resolucion"]["evidence_count"], 2)
            self.assertNotEqual(summary["historical_before_resolution"]["run_id"], current["demo-resolucion"]["run_id"])
            self.assertEqual(current["demo-resolucion"]["score"], 0)
            self.assertEqual(current["demo-victima"]["score"], 0)
            self.assertEqual(current["demo-denunciante"]["score"], 0)
            self.assertEqual(current["demo-homonimo"]["score"], 0)
            self.assertIn("identity_ambiguous_or_requires_selected_spans", [item["code"] for item in current["demo-homonimo"]["exclusions"]])
            self.assertEqual(len(list((Path(directory) / "exports" / "notes").glob("*.json"))), 11)
            self.assertEqual(len(list((Path(directory) / "exports" / "runs").glob("*.json"))), 7)
            # Deliberately do not compare run IDs, wall time or decaying scores
            # with fixture constants. Every run replays its own evaluation date.


if __name__ == "__main__":
    unittest.main()
