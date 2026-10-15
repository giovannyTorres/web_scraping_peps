"""Offline experiment safeguards; these tests require no model or ML libraries."""
import hashlib
import importlib.util
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "poc_laya.py"
SPEC = importlib.util.spec_from_file_location("poc_laya_test_module", MODULE_PATH)
poc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(poc)


class LayaExperimentSafeguards(unittest.TestCase):
    def test_network_guard_denies_python_access_and_restores_functions(self):
        original = socket.getaddrinfo
        with poc.blocked_python_network() as attempts:
            with self.assertRaisesRegex(RuntimeError, "Network disabled"):
                socket.getaddrinfo("example.invalid", 80)
            self.assertEqual(len(attempts), 1)
        self.assertIs(socket.getaddrinfo, original)

    def test_oversized_download_never_installs_an_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            artifacts = {"model.onnx": (3, hashlib.sha256(b"abc").hexdigest())}
            with patch.object(poc, "FILES", artifacts), patch("urllib.request.urlopen", return_value=io.BytesIO(b"oversized")):
                with self.assertRaisesRegex(ValueError, "exceeds expected size"):
                    poc.prepare(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_wrong_hash_never_installs_an_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            artifacts = {"model.onnx": (3, hashlib.sha256(b"abc").hexdigest())}
            with patch.object(poc, "FILES", artifacts), patch("urllib.request.urlopen", return_value=io.BytesIO(b"bad")):
                with self.assertRaisesRegex(ValueError, "integrity verification"):
                    poc.prepare(target)
            self.assertEqual(list(target.iterdir()), [])

    def test_successful_prepare_preserves_license_and_downloads_only_github(self):
        calls = []
        def response(url, **kwargs):
            calls.append(url)
            return io.BytesIO(b"abc" if url.endswith("model.onnx") else b"license fixture")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            artifacts = {"model.onnx": (3, hashlib.sha256(b"abc").hexdigest())}
            with patch.object(poc, "FILES", artifacts), patch("urllib.request.urlopen", side_effect=response):
                poc.prepare(target)
            self.assertEqual((target / "model.onnx").read_bytes(), b"abc")
            self.assertTrue((target / "LICENSE").exists())
            self.assertTrue((target / "NOTICE").exists())
            self.assertTrue(all(url.startswith(("https://github.com/", "https://raw.githubusercontent.com/")) for url in calls))


if __name__ == "__main__":
    unittest.main()
