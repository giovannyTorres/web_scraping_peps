"""Optional model tests use invented entities; no actual accusations or networking."""
from __future__ import annotations

import hashlib
import importlib.util
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from peps import advanced_nlp
from scripts import setup_local_nlp

ENTITY = {"id": "fixture-aurora", "name": "Empresa Aurora Ficticia", "kind": "organization", "aliases": ["Aurora Inventada"], "context": {}}
HAS_MODEL = bool(importlib.util.find_spec("spacy") and importlib.util.find_spec("es_core_news_sm"))


class LocalNLPSetupTests(unittest.TestCase):
    def test_tampered_wheel_rejected_even_if_size_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / setup_local_nlp.FILENAME
            path.write_bytes(b"bad")
            with patch.object(setup_local_nlp, "SIZE_BYTES", 3), patch.object(setup_local_nlp, "SHA256", hashlib.sha256(b"yes").hexdigest()):
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    setup_local_nlp.verify(path)

    def test_wrong_wheel_size_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / setup_local_nlp.FILENAME
            path.write_bytes(b"bad")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("must reject size first")):
                with self.assertRaisesRegex(ValueError, "Tamaño"):
                    setup_local_nlp.verify(path)

    def test_local_wheel_install_uses_no_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / setup_local_nlp.FILENAME
            path.write_bytes(b"yes")
            with patch.object(setup_local_nlp, "SIZE_BYTES", 3), patch.object(setup_local_nlp, "SHA256", hashlib.sha256(b"yes").hexdigest()), patch("sys.argv", ["setup_local_nlp.py", "--wheel", str(path), "--install"]), patch.object(setup_local_nlp.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")), patch.object(setup_local_nlp.subprocess, "run") as install:
                self.assertEqual(setup_local_nlp.main(), 0)
            command = install.call_args.args[0]
            self.assertIn("--no-index", command)
            self.assertIn("--no-deps", command)
            self.assertEqual(command[-1], str(path))

    def test_oversize_download_stops_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(setup_local_nlp, "SIZE_BYTES", 3), patch("sys.argv", ["setup_local_nlp.py", "--download", "--directory", directory]), patch.object(setup_local_nlp.urllib.request, "urlopen", return_value=io.BytesIO(b"four")), patch.object(setup_local_nlp.subprocess, "run") as install:
                self.assertEqual(setup_local_nlp.main(), 1)
                install.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])


class AdvancedNLPOptionalTests(unittest.TestCase):
    def test_missing_model_abstains_with_explicit_warning(self):
        with patch.object(advanced_nlp, "_pipeline", return_value=(None, {"available": False, "error": "not installed"})):
            result = advanced_nlp.analyze("Empresa Aurora Ficticia fue acusada de fraude.", ENTITY)
        self.assertFalse(result["available"])
        self.assertEqual(result["role"], "insufficient")
        self.assertTrue(result["abstained"])
        self.assertIn("not installed", result["warnings"])
        self.assertEqual(result["score_effect"], "auxiliary_only")

    def test_scan_budget_precedes_alias_matching(self):
        text = "x " * advanced_nlp.CONFIG["max_chars"] + ENTITY["name"]
        with patch.object(advanced_nlp, "_pipeline", return_value=(None, {"available": False, "error": "not installed"})):
            result = advanced_nlp.analyze(text, ENTITY)
        self.assertEqual(result["mentions"], [])
        self.assertEqual(result["content_hash"], hashlib.sha256(text.encode()).hexdigest())


@unittest.skipUnless(HAS_MODEL, "Optional local spaCy model not installed")
class AdvancedNLPModelTests(unittest.TestCase):
    def test_negated_acquittal_and_negated_denial_abstain_without_new_adverse_fact(self):
        # Independent regressions found by code inspection, not challenge examples.
        texts = [
            "Empresa Aurora Ficticia no fue absuelta del fraude.",
            "Empresa Aurora Ficticia nunca fue exonerada de la acusación de fraude.",
            "Empresa Aurora Ficticia no negó las acusaciones de fraude.",
            "Empresa Aurora Ficticia no rechazó las acusaciones de fraude.",
            "Empresa Aurora Ficticia no desmintió su participación en fraude.",
            "No se descartó que Empresa Aurora Ficticia cometiera fraude.",
            "La Fiscalía no exoneró a Empresa Aurora Ficticia del fraude.",
            "No se desmintió que Empresa Aurora Ficticia cometiera fraude.",
        ]
        for text in texts:
            with self.subTest(text=text):
                result = advanced_nlp.analyze(text, ENTITY)
                self.assertEqual(result["role"], "insufficient")
                self.assertTrue(result["abstained"])
                self.assertTrue(result["events"])
                self.assertTrue(all(relation["role"] == "insufficient" for relation in result["relations"]))
                self.assertIn("nuevo hecho adverso", " ".join(result["warnings"]))
                self.assertEqual(result["score_effect"], "auxiliary_only")

    def test_contrastive_context_not_word_cooccurrence(self):
        examples = [
            ("Empresa Aurora Ficticia fue acusada de fraude por la Fiscalía.", "accused"),
            ("Empresa Aurora Ficticia denunció un fraude ante la Fiscalía.", "complainant"),
            ("Empresa Aurora Ficticia fue víctima de fraude.", "victim"),
            ("Empresa Aurora Ficticia no fue acusada de fraude.", "negated"),
            ("Empresa Aurora Ficticia patrocinó el concierto donde se discutió un fraude ajeno.", "incidental"),
            ("El hermano de Empresa Aurora Ficticia fue acusado de fraude.", "indirect"),
            ("Empresa Aurora Ficticia fue absuelta del fraude.", "resolved"),
            ("La Fiscalía acusó a Empresa Aurora Ficticia de fraude.", "accused"),
            ("Si Empresa Aurora Ficticia cometiera fraude, sería investigada.", "insufficient"),
        ]
        for text, expected in examples:
            with self.subTest(expected=expected, text=text):
                result = advanced_nlp.analyze(text, ENTITY)
                self.assertEqual(result["role"], expected)
                self.assertEqual(result["score_effect"], "auxiliary_only")

    def test_runtime_no_network_and_evidence_exact_codepoint_offsets(self):
        text = "🧭 Informe inventado. Aurora Inventada no fue acusada de corrupción en México."
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            result = advanced_nlp.analyze(text, ENTITY)
        self.assertEqual(result["role"], "negated")
        for finding in result["findings"]:
            self.assertEqual(text[finding["start"]:finding["end"]], finding["quote"])
        for span in result["entities"] + result["events"] + result["mentions"]:
            self.assertEqual(text[span["start"]:span["end"]], span["text"])
        for relation in result["relations"]:
            for span in relation["dependency_path"]:
                self.assertEqual(text[span["start"]:span["end"]], span["text"])

    def test_absent_target_does_not_inherit_another_entity_allegation(self):
        result = advanced_nlp.analyze("Empresa Aurora Distinta fue acusada de fraude.", ENTITY)
        self.assertEqual(result["role"], "insufficient")
        self.assertEqual(result["relations"], [])

    def test_conflicting_reported_roles_abstain_without_resolving_chronology(self):
        text = "Empresa Aurora Ficticia fue acusada de fraude. Empresa Aurora Ficticia fue absuelta del fraude."
        result = advanced_nlp.analyze(text, ENTITY)
        self.assertEqual(result["role"], "insufficient")
        self.assertTrue(result["abstained"])
        self.assertEqual(result["competing_roles"], ["accused", "resolved"])

    def test_ner_is_statistical_and_not_alias_linking_output(self):
        result = advanced_nlp.analyze("María Pérez visitó Madrid con Empresa Aurora Ficticia.", ENTITY)
        self.assertTrue(result["entities"])
        self.assertTrue(all(e["detector"] == "statistical_ner" for e in result["entities"]))
        self.assertTrue(any(e["label"] == "LOC" for e in result["entities"]))
        self.assertIn("homónimo", " ".join(result["warnings"]))

    def test_model_config_is_defensive_copy(self):
        info = advanced_nlp.get_model_info()
        info["config"]["max_chars"] = 1
        info["vectors"]["width"] = 999
        fresh = advanced_nlp.get_model_info()
        self.assertNotEqual(fresh["config"]["max_chars"], 1)
        self.assertNotEqual(fresh["vectors"]["width"], 999)


if __name__ == "__main__":
    unittest.main()
