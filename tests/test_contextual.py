"""Offline orchestration/identity regressions; no large optional model required."""
# SPDX-License-Identifier: MIT
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from peps import contextual
from peps.identity import MODEL_VERSION as IDENTITY_VERSION, RULES_HASH as IDENTITY_RULES_HASH, entity_signature

ENTITY = {"id": "synthetic-entity", "name": "José Peña Villaluz", "kind": "person", "aliases": [], "context": {}}
ROOT = Path(__file__).resolve().parents[1]


def evidence(text, **kwargs):
    return {"id": "synthetic-note", "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "source_kind": "news", "source_url": "https://synthetic.example/note", "metadata": {}, "review": {}, **kwargs}


def identity_evidence(text, status="matched", supported=(0,)):
    item = evidence(text, metadata={"news_module": True}, identity_bank_revision=5)
    mentions, start = [], 0
    while (found := text.find(ENTITY["name"], start)) >= 0:
        mentions.append({"start": found, "end": found+len(ENTITY["name"]), "text": ENTITY["name"],
            "identity_supported": len(mentions) in supported})
        start = found+len(ENTITY["name"])
    item["identity_resolution"] = {"status": status, "entity_id": ENTITY["id"],
        "content_hash": item["content_hash"], "entity_signature": entity_signature(ENTITY),
        "model_version": IDENTITY_VERSION, "features": {"rules_hash": IDENTITY_RULES_HASH},
        "bank_revision": 5, "mentions": mentions}
    return item


def confirm(item, selected):
    result = deepcopy(item)
    result["review"] = {"status": "confirmed", "actor": "analista-sintetico", "reason": "Identidad revisada en la fuente sintética.",
        "content_hash": result["content_hash"], "entity_signature": entity_signature(ENTITY),
        "identity_rules_hash": IDENTITY_RULES_HASH, "bank_revision": 5,
        "identity_spans": [{key: result["identity_resolution"]["mentions"][index][key] for key in ("start", "end", "text")} for index in selected]}
    return result


def fake_advanced(role="victim", available=True, invalid_quote=False):
    def analyze(text, entity, source_kind):
        name = entity["name"]
        start = text.index(name)
        mention = {"start": start, "end": start+len(name), "text": name}
        event_text = "fraude"
        event_start = text.find(event_text)
        event = {"start": event_start, "end": event_start+len(event_text), "text": event_text, "method": "fake_dependency_fixture"} if event_start >= 0 else None
        entity_span = {**mention, "label": "PER"}
        relation = {"mention": mention, "event": event, "role": role} if event else None
        return {"available": available, "method": "spacy", "model_version": "fixture-advanced-1",
            "model_hash": "fixture-hash", "config_hash": "fixture-config", "role": role if available else "insufficient",
            "predicted_role": role if available else "insufficient", "abstained": not available,
            "relevance": contextual.context_model.ROLE_RELEVANCE[role if available else "insufficient"],
            "findings": [{"start": 0, "end": len(text), "quote": "invented" if invalid_quote else text,
                "role": role, "mentions": [mention], "entities": [entity_span], "events": [event] if event else [], "relations": [relation] if relation else []}] if available else [],
            "mentions": [mention] if available else [], "entities": [entity_span] if available else [],
            "events": [event] if event and available else [], "relations": [relation] if relation and available else [],
            "warnings": [], "limitations": ["Synthetic optional component fixture; not real NLP."]}
    def get_model_info():
        return {"available": available, "model_version": "fixture-advanced-1", "model_hash": "fixture-hash", "config_hash": "fixture-config", "config": {"fixture": True}}
    return SimpleNamespace(analyze=analyze, get_model_info=get_model_info)


class ContextualTests(unittest.TestCase):
    def test_default_is_local_discriminative_and_has_matching_metadata_fingerprint(self):
        text = "José Peña Villaluz denunció a los responsables de fraude ante las autoridades."
        with patch("peps.contextual._advanced_module", side_effect=AssertionError("Default must not load optional NLP")):
            result = contextual.analyze_evidence(ENTITY, evidence(text))
            info = contextual.get_method_info()
        self.assertEqual(result["evidence_id"], "synthetic-note")
        analysis = result["analysis"]
        self.assertEqual(analysis["role"], "complainant")
        self.assertEqual(analysis["method"], "discriminative")
        self.assertEqual(analysis["score_effect"], "auxiliary_only")
        self.assertEqual(analysis["config_hash"], info["config_hash"])
        self.assertEqual(analysis["model_hash"], info["model_hash"])
        self.assertTrue(analysis["inference_attempted"])

    def test_default_imports_no_optional_scientific_dependency_and_no_network(self):
        code = '''
import builtins,socket
original=builtins.__import__
def checked(name,*args,**kwargs):
    if name.split('.')[0] in {'spacy','numpy','scipy','torch','sklearn','transformers'}:
        raise AssertionError(name)
    return original(name,*args,**kwargs)
builtins.__import__=checked
def deny(*args,**kwargs): raise AssertionError('Network must not be used')
socket.socket=deny
socket.create_connection=deny
from peps.contextual import analyze_evidence,get_method_info
r=analyze_evidence({'name':'Persona Ficticia'}, {'id':'x','text':'Persona Ficticia fue acusado de fraude por la fiscalía.','source_kind':'news'})
assert r['analysis']['role']=='accused', r
assert get_method_info()['available']
'''
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True, capture_output=True, text=True)

    def test_discovery_legacy_rejected_missing_hash_and_budget_skip_all_inference(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        fixtures = [evidence(text, source_kind="discovery"), evidence(text, metadata={"discovery_only": True}),
            evidence(text, source_kind="legacy"), evidence(text, metadata={"requires_source_revalidation": True}),
            evidence(text, review={"status": "rejected"}), evidence(""), evidence(text, content_hash="different"),
            evidence(text*2000)]
        with patch("peps.contextual._run_method", side_effect=AssertionError("Ineligible notes must not infer")):
            for item in fixtures:
                with self.subTest(item=item["source_kind"], length=len(item["text"])):
                    analysis = contextual.analyze_evidence(ENTITY, item, "enriched")["analysis"]
                    self.assertTrue(analysis["abstained"])
                    self.assertEqual(analysis["role"], "insufficient")
                    self.assertFalse(analysis["inference_attempted"])
                    self.assertEqual(analysis["content_hash"], hashlib.sha256(item["text"].encode()).hexdigest())

    def test_unresolved_identity_skips_before_inference(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        fixtures = [identity_evidence(text, status="ambiguous", supported=()),
            evidence(text, metadata={"news_module": True})]
        with patch("peps.contextual._run_method", side_effect=AssertionError("Unresolved identity must not infer")):
            for item in fixtures:
                result = contextual.analyze_evidence(ENTITY, item)["analysis"]
                self.assertTrue(result["abstained"])
                self.assertEqual(result["role"], "insufficient")
                self.assertEqual(result["identity_status"], "unresolved")
                self.assertFalse(result["inference_attempted"])

    def test_auto_supported_spans_do_not_borrow_another_homonym_role(self):
        text = ("José Peña Villaluz denunció a los responsables de fraude ante las autoridades. "
            "José Peña Villaluz fue acusado de fraude por la fiscalía.")
        item = identity_evidence(text, supported=(0,))
        result = contextual.analyze_evidence(ENTITY, item)["analysis"]
        self.assertEqual(result["role"], "complainant", result)
        self.assertFalse(result["abstained"])
        self.assertEqual(result["identity_status"], "matched_supported")
        self.assertEqual(len(result["findings"]), 1)
        self.assertTrue(all(finding["end"] <= text.index(". ")+1 for finding in result["findings"]))

    def test_human_selected_span_overrides_ambiguous_identity_and_lifts_offsets(self):
        text = ("José Peña Villaluz fue acusado de fraude por la fiscalía. "
            "José Peña Villaluz denunció a los responsables de fraude ante las autoridades.")
        item = confirm(identity_evidence(text, status="ambiguous", supported=()), [1])
        with patch("peps.contextual._advanced_module", return_value=fake_advanced(role="complainant")):
            result = contextual.analyze_evidence(ENTITY, item, "enriched")["analysis"]
        self.assertEqual(result["role"], "complainant", result)
        self.assertEqual(result["identity_status"], "human_selected")
        self.assertEqual(result["selected_identity_spans"][0]["start"], text.rindex(ENTITY["name"]))
        for finding in result["findings"]:
            self.assertEqual(text[finding["start"]:finding["end"]], finding["quote"])
        for span in [*result["entities"], *result["events"]]:
            self.assertEqual(text[span["start"]:span["end"]], span["text"])
            self.assertGreater(span["start"], 30)
        for relation in result["relations"]:
            for key in ("mention", "event"):
                span = relation[key]
                self.assertEqual(text[span["start"]:span["end"]], span["text"])

    def test_selected_and_unselected_homonyms_in_same_clause_are_excluded(self):
        text = "José Peña Villaluz denunció por fraude a José Peña Villaluz."
        item = confirm(identity_evidence(text, status="ambiguous", supported=()), [0])
        with patch("peps.contextual._run_method", side_effect=AssertionError("Mixed identity clause must not infer")):
            result = contextual.analyze_evidence(ENTITY, item)["analysis"]
        self.assertTrue(result["abstained"])
        self.assertFalse(result["inference_attempted"])
        self.assertEqual(result["excluded_mixed_identity_clauses"], 1)
        self.assertEqual(result["findings"], [])

    def test_empty_human_selection_does_not_restore_a_supported_single_mention(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        item = confirm(identity_evidence(text), [])
        with patch("peps.contextual._run_method", side_effect=AssertionError("An empty selection must not infer")):
            result = contextual.analyze_evidence(ENTITY, item)["analysis"]
        self.assertTrue(result["abstained"])
        self.assertFalse(result["inference_attempted"])
        self.assertEqual(result["identity_status"], "unresolved")

    def test_stale_identity_and_review_cannot_restore_attribution(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        for field, value in (("content_hash", "stale"), ("entity_signature", "stale"), ("model_version", "stale"), ("bank_revision", 4)):
            item = identity_evidence(text)
            item["identity_resolution"][field] = value
            with patch("peps.contextual._run_method", side_effect=AssertionError("Stale identity must not infer")):
                result = contextual.analyze_evidence(ENTITY, item)["analysis"]
            self.assertTrue(result["abstained"])
            self.assertEqual(result["identity_status"], "stale")
        item = confirm(identity_evidence(text, status="ambiguous", supported=()), [0])
        item["review"]["bank_revision"] = 4
        self.assertEqual(contextual.analyze_evidence(ENTITY, item)["analysis"]["identity_status"], "unresolved")

    def test_changed_identity_rules_invalidate_resolution_and_method_fingerprint(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        item = identity_evidence(text)
        before = contextual.get_method_info()
        with patch("peps.identity.RULES_HASH", "synthetic-new-identity-policy"):
            after = contextual.get_method_info()
            result = contextual.analyze_evidence(ENTITY, item)["analysis"]
        self.assertNotEqual(before["config_hash"], after["config_hash"])
        self.assertTrue(result["abstained"])
        self.assertFalse(result["inference_attempted"])
        self.assertEqual(result["identity_status"], "stale")

    def test_enriched_keeps_discriminative_role_and_reports_disagreement(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        with patch("peps.contextual._advanced_module", return_value=fake_advanced(role="victim")):
            analysis = contextual.analyze_evidence(ENTITY, evidence(text), "enriched")["analysis"]
            info = contextual.get_method_info("enriched")
        self.assertEqual(analysis["role"], "accused")
        self.assertFalse(analysis["abstained"])
        self.assertEqual(analysis["component_results"]["advanced"]["role"], "victim")
        self.assertTrue(analysis["entities"])
        self.assertTrue(any("Desacuerdo" in warning for warning in analysis["warnings"]))
        self.assertEqual(analysis["config_hash"], info["config_hash"])

    def test_optional_unavailable_enriched_preserves_default_hybrid_abstains(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        with patch("peps.contextual._advanced_module", return_value=fake_advanced(available=False)):
            enriched = contextual.analyze_evidence(ENTITY, evidence(text), "enriched")["analysis"]
            hybrid = contextual.analyze_evidence(ENTITY, evidence(text), "hybrid")["analysis"]
            advanced = contextual.analyze_evidence(ENTITY, evidence(text), "advanced")["analysis"]
        self.assertEqual(enriched["role"], "accused")
        self.assertTrue(enriched["available"])
        self.assertEqual(enriched["entities"], [])
        self.assertTrue(any("no disponible" in warning for warning in enriched["warnings"]))
        self.assertTrue(hybrid["abstained"])
        self.assertEqual(hybrid["role"], "insufficient")
        self.assertTrue(advanced["abstained"])

    def test_hybrid_requires_nonabstaining_agreement(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        for role in ("accused", "victim"):
            with patch("peps.contextual._advanced_module", return_value=fake_advanced(role=role)):
                result = contextual.analyze_evidence(ENTITY, evidence(text), "hybrid")["analysis"]
            self.assertEqual(result["role"], "accused" if role == "accused" else "insufficient")
            self.assertEqual(result["abstained"], role != "accused")

    def test_component_failure_and_invalid_output_fail_gracefully(self):
        text = "José Peña Villaluz fue acusado de fraude por la fiscalía."
        with patch("peps.contextual.context_model.analyze", side_effect=RuntimeError("PRIVATE_INTERNAL_DETAILS")):
            result = contextual.analyze_evidence(ENTITY, evidence(text))["analysis"]
        self.assertTrue(result["abstained"])
        self.assertEqual(result["component_results"]["discriminative"]["available"], False)
        self.assertNotIn("PRIVATE_INTERNAL_DETAILS", json.dumps(result))
        with patch("peps.contextual._advanced_module", return_value=fake_advanced(invalid_quote=True)):
            result = contextual.analyze_evidence(ENTITY, evidence(text), "advanced")["analysis"]
        self.assertTrue(result["abstained"])
        self.assertEqual(result["error_type"], "ValueError")
        self.assertEqual(result["findings"], [])

    def test_malformed_unicode_and_invalid_inputs_return_auxiliary_failure(self):
        # Invalid UTF-8 must not escape the orchestrator into the score path.
        result = contextual.analyze_evidence(ENTITY, {"id": "malformed", "text": "invalid\ud800"})["analysis"]
        self.assertTrue(result["abstained"])
        self.assertFalse(result["inference_attempted"])
        self.assertEqual(result["error_type"], "UnicodeEncodeError")
        for entity, item in ((None, {}), (ENTITY, None), ({}, [])):
            result = contextual.analyze_evidence(entity, item)["analysis"]
            self.assertTrue(result["abstained"])
            self.assertEqual(result["score_effect"], "auxiliary_only")

    def test_method_metadata_is_copy_and_unknown_mode_fails_closed(self):
        info = contextual.get_method_info()
        info["config"]["max_input_chars"] = 1
        info["components"]["discriminative"]["config"]["minimum_margin"] = 99
        after = contextual.get_method_info()
        self.assertEqual(after["config"]["max_input_chars"], 60000)
        self.assertEqual(after["components"]["discriminative"]["config"]["minimum_margin"], .7)
        self.assertFalse(contextual.get_method_info("unknown")["available"])
        self.assertTrue(contextual.analyze_evidence(ENTITY, evidence("texto"), "unknown")["analysis"]["abstained"])

    def test_caller_inputs_and_score_are_untouched(self):
        item = identity_evidence("José Peña Villaluz fue acusado de fraude por la fiscalía.")
        before_entity, before_item = deepcopy(ENTITY), deepcopy(item)
        with patch("peps.scoring.score_entity", side_effect=AssertionError("Context does not score")):
            result = contextual.analyze_evidence(ENTITY, item)
        self.assertEqual(ENTITY, before_entity)
        self.assertEqual(item, before_item)
        self.assertEqual(result["analysis"]["score_effect"], "auxiliary_only")

if __name__ == "__main__":
    unittest.main()
