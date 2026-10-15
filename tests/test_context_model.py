"""Auxiliary discriminative model contract; synthetic names and allegations only."""
# SPDX-License-Identifier: MIT
import hashlib
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

from peps.context_model import MODEL_PATH, MODEL_VERSION, analyze, get_model_info, _validate_model, _load_model

ROOT = Path(__file__).resolve().parents[1]
ENTITY = {"id": "synthetic-model", "name": "José Peña Villaluz", "kind": "person", "aliases": []}


class ContextModelTests(unittest.TestCase):
    def test_role_contract_distinguishes_actor_and_grammatical_context(self):
        fixtures = [
            ("fue acusado de fraude por la fiscalía.", "accused"),
            ("fue víctima de fraude y sufrió pérdidas económicas.", "victim"),
            ("denunció a los responsables de fraude ante las autoridades.", "complainant"),
            ("asistió a un foro sobre prevención de fraude.", "incidental"),
            ("no fue acusado de fraude; la nota aclara el error.", "negated"),
            ("fue absuelto de los cargos de fraude por el tribunal.", "resolved"),
            ("aparece sin contexto; el cuerpo de la nota no está disponible.", "insufficient"),
            ("es familiar de una persona imputada por fraude.", "indirect"),
        ]
        for suffix, role in fixtures:
            with self.subTest(role=role):
                result = analyze(ENTITY["name"] + " " + suffix, ENTITY)
                self.assertEqual(result["role"], role)
                self.assertEqual(result["score_effect"], "auxiliary_only")
                self.assertTrue(result["warnings"])
                self.assertNotIn("confidence", result)

    def test_exact_source_offsets_unicode_and_aliases(self):
        for form in ("NFC", "NFD"):
            text = unicodedata.normalize(form, "📰 José Peña Villaluz fue acusado de fraude por la fiscalía.")
            result = analyze(text, ENTITY)
            self.assertTrue(result["findings"])
            for finding in result["findings"]:
                self.assertEqual(text[finding["start"]:finding["end"]], finding["quote"])
                self.assertEqual(finding["evidence_status"], "exact_input_quote_not_fact_verification")
                for mention in finding["mentions"]:
                    self.assertEqual(text[mention["start"]:mention["end"]], mention["text"])
        alias_entity = {**ENTITY, "aliases": ["Firma Lumbre Inventada"]}
        alias_text = "Firma Lumbre Inventada fue víctima de fraude y sufrió pérdidas económicas."
        result = analyze(alias_text, alias_entity)
        self.assertEqual(result["role"], "victim")
        self.assertEqual(result["mentions"][0]["matched_alias"], "Firma Lumbre Inventada")

    def test_absent_or_partial_target_never_attributes_a_role(self):
        for text in ("Otra Persona fue acusada de fraude.", "José Peña Villaluzano fue acusado de fraude.", ""):
            result = analyze(text, ENTITY)
            self.assertTrue(result["abstained"])
            self.assertEqual(result["role"], "insufficient")
            self.assertEqual(result["findings"], [])

    def test_competing_roles_are_visible_and_cause_abstention(self):
        text = ("José Peña Villaluz fue acusado de fraude por la fiscalía. "
                "José Peña Villaluz fue absuelto de los cargos de fraude por el tribunal.")
        result = analyze(text, ENTITY)
        self.assertTrue(result["abstained"])
        self.assertEqual(result["role"], "insufficient")
        self.assertEqual(result["competing_roles"], ["accused", "resolved"])
        self.assertEqual(len(result["findings"]), 2)

    def test_processing_budget_is_explicit_and_abstains(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía " + "contexto irrelevante " * 200
        result = analyze(text, ENTITY)
        self.assertTrue(result["abstained"])
        self.assertTrue(any("presupuesto" in warning for warning in result["warnings"]))
        self.assertLessEqual(len(result["findings"][0]["quote"]), 1800)
        self.assertEqual(text[result["findings"][0]["start"]:result["findings"][0]["end"]], result["findings"][0]["quote"])

    def test_configuration_artifact_and_corpus_are_identified(self):
        info = get_model_info()
        self.assertEqual(info["model_version"], MODEL_VERSION)
        self.assertEqual(info["license"]["weights"], "CC0-1.0")
        self.assertEqual(info["artifact_sha256"], hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest())
        self.assertEqual(info["corpus_sha256"], hashlib.sha256((ROOT / "tests/data/context_train.jsonl").read_bytes()).hexdigest())
        self.assertLess(MODEL_PATH.stat().st_size, 3_000_000)
        canonical = json.dumps(info["config"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(info["config_hash"], hashlib.sha256(canonical).hexdigest())

    def test_no_train_holdout_family_or_masked_text_leakage(self):
        train = [json.loads(line) for line in (ROOT / "tests/data/context_train.jsonl").read_text().splitlines()]
        holdout = [json.loads(line) for line in (ROOT / "tests/data/context_holdout.jsonl").read_text().splitlines()]
        self.assertFalse({row["family"] for row in train} & {row["family"] for row in holdout})
        self.assertFalse({row["masked_text"] for row in train} & {row["masked_text"] for row in holdout})
        self.assertFalse({row["template"] for row in train} & {row["template"] for row in holdout})
        self.assertTrue(all(row["license"] == "CC0-1.0" for row in train + holdout))

    def test_inference_runs_without_network_or_scientific_packages(self):
        script = '''
import builtins, socket
original_import = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'sklearn','numpy','scipy','torch','spacy','transformers'}:
        raise AssertionError('Unexpected optional inference dependency: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guard
def deny(*args, **kwargs):
    raise AssertionError('Unexpected network operation')
socket.socket = deny
socket.create_connection = deny
from peps.context_model import analyze
r = analyze('Ana Brisal fue acusado de fraude por la fiscalía.', {'name':'Ana Brisal'})
assert r['role'] == 'accused', r
assert r['score_effect'] == 'auxiliary_only'
'''
        subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True, capture_output=True, text=True)

    def test_input_limit_applies_before_mention_matching_and_preserves_original_hash(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía. " * 40000
        with patch("peps.context_model._mentions", side_effect=AssertionError("Mention budget must guard matching")):
            result = analyze(text, ENTITY)
        self.assertTrue(result["abstained"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["role"], "insufficient")
        self.assertEqual(result["content_hash"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result["total_chars"], len(text))
        self.assertLessEqual(result["processed_chars"], 60000)
        self.assertTrue(any("menciones" in warning for warning in result["warnings"]))
        # A single target in a long note still keeps exact prefix offsets.
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía. " + "relleno " * 16000
        result = analyze(text, ENTITY)
        self.assertTrue(result["abstained"])
        self.assertEqual(result["content_hash"], hashlib.sha256(text.encode()).hexdigest())
        for finding in result["findings"]:
            self.assertEqual(text[finding["start"]:finding["end"]], finding["quote"])
            self.assertLessEqual(finding["end"], 60000)

    def test_metadata_does_not_expose_mutable_model_cache(self):
        before = analyze("José Peña Villaluz fue acusado de fraude por la fiscalía.", ENTITY)
        info = get_model_info()
        info["config"]["minimum_margin"] = 99
        info["classes"].append("fabricated")
        info["training"]["examples"] = 0
        info["runtime_limits"]["max_input_chars"] = 1
        after = analyze("José Peña Villaluz fue acusado de fraude por la fiscalía.", ENTITY)
        self.assertEqual(before, after)
        self.assertEqual(get_model_info()["config"]["minimum_margin"], .7)

    def test_artifact_integrity_fails_closed(self):
        # A metadata-only mutation must also break the release-pinned digest.
        tampered = json.loads(MODEL_PATH.read_text())
        tampered["training"]["examples"] += 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tampered.json"
            path.write_text(json.dumps(tampered))
            _load_model.cache_clear()
            try:
                with patch("peps.context_model.MODEL_PATH", path):
                    with self.assertRaisesRegex(ValueError, "integrity"):
                        _load_model()
            finally:
                _load_model.cache_clear()
        _load_model()

    def test_artifact_schema_rejects_dimension_mismatch_and_nonfinite_numbers(self):
        original = json.loads(MODEL_PATH.read_text())
        malformed = deepcopy(original)
        malformed["branches"][0]["coef"][0].pop()
        with self.assertRaisesRegex(ValueError, "coefficient"):
            _validate_model(malformed)
        malformed = deepcopy(original)
        malformed["intercept"][0] = float("nan")
        with self.assertRaisesRegex(ValueError, "intercept"):
            _validate_model(malformed)
        malformed = deepcopy(original)
        malformed["branches"][0]["idf"][0] = float("inf")
        with self.assertRaisesRegex(ValueError, "IDF"):
            _validate_model(malformed)

    def test_score_is_not_called_and_feature_effects_are_distinct(self):
        with patch("peps.scoring.score_entity", side_effect=AssertionError("Auxiliary model must not score")):
            result = analyze("José Peña Villaluz fue acusado de fraude por la fiscalía.", ENTITY)
        self.assertEqual(result["score_effect"], "auxiliary_only")
        features = result["findings"][0]["feature_evidence"]
        self.assertTrue(features)
        self.assertTrue(all(feature["scope"] == "auxiliary_classifier_only" for feature in features))
        self.assertTrue(all(feature["margin_contribution"] > 0 for feature in features))

if __name__ == "__main__":
    unittest.main()
