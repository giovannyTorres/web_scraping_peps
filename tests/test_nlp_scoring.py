"""Synthetic fixtures: no allegations about real people are manufactured here."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from peps.nlp import MODEL_VERSION as NLP_VERSION, analyze
from peps.scoring import MODEL_VERSION, PARAMETERS_HASH, score_entity

ENTITY = {"id": "fixture-1", "name": "José Peña Villaluz", "kind": "person", "aliases": [], "context": {}}
AS_OF = "2026-10-15"


def evidence(text="José Peña Villaluz fue detenido por fraude.", **kwargs):
    row = {"id": "ev-1", "source_kind": "news", "source_url": "https://fixture.invalid/1",
           "title": "Documento de prueba sintético", "text": text,
           "published_at": "2026-10-10", "retrieved_at": AS_OF,
           "content_hash": hashlib.sha256(text.encode()).hexdigest()}
    row.update(kwargs)
    return row


def confirmed(**kwargs):
    return {"status": "confirmed", "actor": "analista-de-prueba", "reason": "Identidad, texto y fuente revisados manualmente.", **kwargs}


class SpanishNlpTests(unittest.TestCase):
    def test_labeled_synthetic_cases_and_original_offsets(self):
        path = Path(__file__).parent / "data" / "scoring_cases.jsonl"
        self.assertGreaterEqual(len(path.read_text(encoding="utf-8").splitlines()), 70)
        for line in path.read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            with self.subTest(case=case["id"]):
                entity = case.get("entity", ENTITY)
                text = case["text"]
                result = analyze(text, entity)
                self.assertEqual(len(result["mentions"]), case.get("mentions", 1))
                for mention in result["mentions"]:
                    self.assertEqual(text[mention["start"]:mention["end"]], mention["text"])
                for finding in result["findings"]:
                    self.assertEqual(text[finding["start"]:finding["end"]], finding["quote"])
                if "stage" in case:
                    matching = [f for f in result["findings"] if f["stage"] == case["stage"]]
                    self.assertTrue(matching, result)
                    for key in ("category", "relationship", "negated", "uncertain"):
                        if key in case:
                            self.assertTrue(any(f[key] == case[key] for f in matching), (key, result))
                score = score_entity(entity, [evidence(text)], AS_OF)
                self.assertEqual(score["score"] > 0, case["scoreable"], score)
                self.assertEqual(score["validated_score"], 0)

    def test_unicode_offsets_300_transformed_examples(self):
        names = ["José Peña Villaluz", "María Treviño Albor", "Ana Muñoz Niebla", "Óscar Cañón Brisal", "Íñigo Méndez Valtierra"]
        prefixes = ["", "📰 ", "\n\n", "Texto previo. ", "México: ", "  "]
        suffixes = [" fue detenido por fraude.", " fue acusado de corrupción."]
        for name in names:
            for prefix in prefixes:
                for suffix in suffixes:
                    for case in (str, str.upper, str.lower):
                        text = prefix + case(name) + suffix
                        entity = {**ENTITY, "name": name}
                        with self.subTest(name=name, prefix=prefix, suffix=suffix, case=case):
                            result = analyze(text, entity)
                            self.assertEqual(len(result["mentions"]), 1)
                            mention = result["mentions"][0]
                            self.assertEqual(mention["start"], len(prefix))
                            self.assertEqual(text[mention["start"]:mention["end"]], case(name))

    def test_alias_does_not_duplicate_overlapping_name(self):
        entity = {**ENTITY, "aliases": ["Peña Villaluz", ENTITY["name"]]}
        self.assertEqual(len(analyze("José Peña Villaluz fue detenido.", entity)["mentions"]), 1)

    def test_context_improves_but_never_certifies_identity(self):
        entity = {**ENTITY, "context": {"state": "Oaxaca", "position": "alcalde"}}
        plain = analyze("José Peña Villaluz fue acusado de fraude.", entity)
        contextual = analyze("José Peña Villaluz, alcalde de Oaxaca, fue acusado de fraude.", entity)
        self.assertGreater(contextual["identity_confidence"], plain["identity_confidence"])
        self.assertLess(contextual["identity_confidence"], 1)
        self.assertTrue(contextual["review_required"])

    def test_no_query_based_inference(self):
        result = score_entity(ENTITY, [evidence("", title="José Peña Villaluz fraude detenido", query="José Peña Villaluz fraude")], AS_OF)
        self.assertEqual(result["score"], 0)
        self.assertIn("missing_source_body", result["evidence_audit"][0]["exclusions"])

    def test_dates_spanish_iso_and_invalid_calendar_dates(self):
        text = "José Peña Villaluz fue detenido el 12 de febrero de 2018 por fraude. Actualizado 2026-10-10; 31/02/2020."
        result = analyze(text, ENTITY)
        self.assertEqual([d["iso_date"] for d in result["dates"]], ["2018-02-12", "2026-10-10"])
        self.assertEqual(result["findings"][0]["event_date"], "2018-02-12")
        for found in result["dates"]:
            self.assertEqual(text[found["start"]:found["end"]], found["text"])

    def test_multiple_dates_are_ambiguous_not_invented_event_date(self):
        result = analyze("José Peña Villaluz fue detenido entre el 12 de febrero de 2018 y el 15 de marzo de 2019 por fraude.", ENTITY)
        self.assertNotIn("event_date", result["findings"][0])
        self.assertTrue(result["findings"][0]["event_date_ambiguous"])

    def test_common_active_news_headlines(self):
        for verb, stage in (("Detienen", "arrest"), ("Denuncian", "allegation"), ("Condenan", "conviction"), ("Sentencian", "conviction"), ("Imputan", "allegation")):
            with self.subTest(verb=verb):
                result = analyze(f"{verb} a José Peña Villaluz por fraude.", ENTITY)
                self.assertTrue(any(f["stage"] == stage for f in result["findings"]))

    def test_offense_and_resolution_of_other_subject_never_transfer(self):
        for other in ("fue absuelto de corrupción", "fue detenido por narcotráfico"):
            result = analyze(f"José Peña Villaluz fue acusado de fraude, mientras Pedro Robles Tormenta {other}.", ENTITY)
            self.assertTrue(result["findings"])
            self.assertEqual({(f["category"], f["stage"]) for f in result["findings"]}, {("fraud", "allegation")})

    def test_acquittal_for_one_charge_does_not_clear_another_charge(self):
        text = "José Peña Villaluz fue absuelto de fraude, pero sigue acusado de lavado de dinero."
        result = score_entity(ENTITY, [evidence(text)], AS_OF)
        self.assertGreater(result["score"], 0)
        stages = {(f["category"], f["stage"]) for f in result["evidence_audit"][0]["findings"]}
        self.assertEqual(stages, {("fraud", "acquittal"), ("money_laundering", "allegation")})

    def test_future_or_requested_actions_are_not_completed_actions(self):
        for text in ("José Peña Villaluz podría ser detenido por fraude.",
                     "José Peña Villaluz será condenado por fraude.",
                     "La fiscalía pide que sea detenido José Peña Villaluz por fraude."):
            with self.subTest(text=text):
                result = score_entity(ENTITY, [evidence(text)], AS_OF)
                self.assertEqual(result["score"], 0)


class AuditableScoringTests(unittest.TestCase):
    def test_pending_is_never_validated(self):
        result = score_entity(ENTITY, [evidence()], AS_OF)
        self.assertGreater(result["score"], 0)
        self.assertEqual(result["validated_score"], 0)
        self.assertEqual(result["status"], "provisional")
        self.assertTrue(result["review_required"])
        self.assertEqual(result["meaning"], "document_review_priority")
        self.assertFalse(result["confidence_is_probability"])

    def test_named_human_review_required(self):
        for review in ({"status": "confirmed"}, {"status": "confirmed", "actor": "test"}, {"status": "confirmed", "reason": "test"}):
            with self.subTest(review=review):
                result = score_entity(ENTITY, [evidence(review=review)], AS_OF)
                self.assertEqual(result["validated_score"], 0)
                self.assertEqual(result["status"], "provisional")
        result = score_entity(ENTITY, [evidence(review=confirmed())], AS_OF)
        self.assertGreater(result["validated_score"], 0)
        self.assertEqual(result["status"], "validated")

    def test_duplicate_documents_do_not_raise_score(self):
        one = score_entity(ENTITY, [evidence()], AS_OF)
        duplicated = [evidence(id=f"copy-{i}", source_url=f"https://fixture.invalid/{i}") for i in range(100)]
        many = score_entity(ENTITY, duplicated, AS_OF)
        self.assertEqual(one["score"], many["score"])
        self.assertEqual(len(many["components"]), 1)

    def test_paraphrased_unresolved_same_event_not_inflated(self):
        rows = [evidence("José Peña Villaluz fue detenido por fraude."),
                evidence("Detuvieron por fraude a José Peña Villaluz.", id="ev-2")]
        self.assertEqual(score_entity(ENTITY, rows, AS_OF)["score"], score_entity(ENTITY, rows[:1], AS_OF)["score"])

    def test_explicit_case_id_clusters_cross_category(self):
        rows = [evidence("José Peña Villaluz fue detenido por fraude y corrupción.", event_id="case-1")]
        result = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(len(result["components"]), 1)
        strongest = max(f["points_before_dedup"] for f in result["evidence_audit"][0]["findings"])
        self.assertEqual(result["score"], round(strongest))

    def test_distinct_reviewed_events_can_increase_priority(self):
        rows = [evidence(event_id="one"), evidence("La fiscalía detuvo a José Peña Villaluz por fraude.", id="ev-2", event_id="two")]
        self.assertGreater(score_entity(ENTITY, rows, AS_OF)["score"], score_entity(ENTITY, rows[:1], AS_OF)["score"])

    def test_republished_identical_content_does_not_inflate_or_refresh(self):
        rows = [evidence(published_at="2018-01-01"), evidence(id="republished", published_at="2026-10-10")]
        old_only = score_entity(ENTITY, rows[:1], AS_OF)
        combined = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(combined["score"], old_only["score"])
        self.assertEqual(len(combined["components"]), 1)
        self.assertIn("duplicate_of", combined["evidence_audit"][1])

    def test_changed_entity_does_not_reuse_old_analysis(self):
        text = "José Peña Villaluz fue detenido por fraude. Ana Niebla Caracol inauguró una escuela."
        stale = analyze(text, ENTITY)
        changed = {**ENTITY, "name": "Ana Niebla Caracol"}
        result = score_entity(changed, [evidence(text, analysis=stale)], AS_OF)
        self.assertEqual(result["score"], 0)
        self.assertTrue(result["evidence_audit"][0]["analysis_recomputed"])
        self.assertNotEqual(stale["entity_signature"], result["evidence_audit"][0]["entity_signature"])

    def test_changed_rules_or_text_does_not_reuse_analysis(self):
        row = evidence()
        stale = analyze(row["text"], ENTITY)
        stale["rules_hash"] = "old-rule-set"
        result = score_entity(ENTITY, [evidence(analysis=stale)], AS_OF)
        self.assertTrue(result["evidence_audit"][0]["analysis_recomputed"])

    def test_human_rejected_is_excluded(self):
        result = score_entity(ENTITY, [evidence(review={"status": "rejected", "actor": "test", "reason": "Homónimo"})], AS_OF)
        self.assertEqual(result["score"], 0)
        self.assertIn("human_rejected", result["evidence_audit"][0]["exclusions"])

    def test_later_exculpatory_update_suspends_provisional_event(self):
        rows = [evidence(event_id="case-1"), evidence("José Peña Villaluz fue absuelto de fraude.", id="resolution", event_id="case-1", published_at="2026-10-14")]
        result = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["components"][0]["resolution"], "acquittal")
        self.assertTrue(result["review_required"])

    def test_unreviewed_acquittal_does_not_erase_validated_record(self):
        rows = [evidence(event_id="case-1", review=confirmed()), evidence("José Peña Villaluz fue absuelto de fraude.", id="resolution", event_id="case-1", published_at="2026-10-14")]
        result = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(result["provisional_score"], 0)
        self.assertGreater(result["validated_score"], 0)
        self.assertTrue(result["components"][0]["resolution_review_required"])
        rows[1]["review"] = confirmed()
        resolved = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(resolved["validated_score"], 0)

    def test_old_acquittal_does_not_clear_new_different_action(self):
        rows = [evidence(event_id="case-1"), evidence("José Peña Villaluz fue absuelto de fraude.", id="old", event_id="case-1", published_at="2026-09-01")]
        self.assertGreater(score_entity(ENTITY, rows, AS_OF)["score"], 0)

    def test_stage_order_is_documented_not_probability(self):
        scores = []
        for stage in ("acusado", "investigado", "detenido", "condenado"):
            scores.append(score_entity(ENTITY, [evidence(f"José Peña Villaluz fue {stage} por fraude.")], AS_OF)["score"])
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(len(set(scores)), 4)

    def test_source_and_age_weights(self):
        recent = score_entity(ENTITY, [evidence()], AS_OF)
        old = score_entity(ENTITY, [evidence(published_at="2010-01-01")], AS_OF)
        undated = score_entity(ENTITY, [evidence(published_at=None)], AS_OF)
        official = score_entity(ENTITY, [evidence(source_kind="government")], AS_OF)
        comment = score_entity(ENTITY, [evidence(source_kind="public_comment")], AS_OF)
        self.assertGreater(recent["score"], old["score"])
        self.assertGreater(recent["score"], undated["score"])
        self.assertGreater(official["score"], recent["score"])
        self.assertLess(comment["score"], recent["score"])

    def test_old_event_in_new_article_is_not_fresh(self):
        recent = score_entity(ENTITY, [evidence()], AS_OF)
        historic = score_entity(ENTITY, [evidence("José Peña Villaluz fue detenido el 12 de febrero de 2018 por fraude.")], AS_OF)
        self.assertLess(historic["score"], recent["score"])
        finding = historic["evidence_audit"][0]["findings"][0]
        self.assertEqual(finding["event_date"], "2018-02-12")
        self.assertEqual(finding["effective_date"], "2018-02-12")

    def test_uncertain_report_lower_weight_than_same_asserted_stage(self):
        certain = score_entity(ENTITY, [evidence("José Peña Villaluz fue detenido por fraude.")], AS_OF)
        uncertain = score_entity(ENTITY, [evidence("José Peña Villaluz habría sido detenido por presunto fraude.")], AS_OF)
        self.assertLess(uncertain["score"], certain["score"])

    def test_future_event_date_excluded(self):
        result = score_entity(ENTITY, [evidence(event_date="2030-01-01")], AS_OF)
        self.assertEqual(result["score"], 0)

    def test_hash_mismatch_future_and_missing_url_excluded(self):
        for kwargs, code in (({"content_hash": "bad"}, "content_hash_mismatch"), ({"published_at": "2030-01-01"}, "future_publication_date"), ({"source_url": None}, "missing_source_url")):
            with self.subTest(kwargs=kwargs):
                result = score_entity(ENTITY, [evidence(**kwargs)], AS_OF)
                self.assertEqual(result["score"], 0)
                self.assertIn(code, result["evidence_audit"][0]["exclusions"])

    def test_discovery_and_unverified_legacy_never_become_article_evidence(self):
        for kwargs in ({"source_kind": "discovery"}, {"metadata": {"discovery_only": True}},
                       {"source_kind": "legacy"}, {"metadata": {"requires_source_revalidation": True}}):
            with self.subTest(kwargs=kwargs):
                result = score_entity(ENTITY, [evidence(**kwargs)], AS_OF)
                self.assertEqual(result["score"], 0)
                self.assertFalse(result["components"])

    def test_invalid_identity_and_tampered_quotes_excluded(self):
        analysis = analyze(evidence()["text"], ENTITY)
        for value in (0, float("nan"), "invalid"):
            modified = copy.deepcopy(analysis)
            modified["identity_confidence"] = value
            self.assertEqual(score_entity(ENTITY, [evidence(analysis=modified)], AS_OF)["score"], 0)
        modified = copy.deepcopy(analysis)
        modified["findings"][0]["quote"] = "Cita inventada"
        result = score_entity(ENTITY, [evidence(analysis=modified)], AS_OF)
        self.assertEqual(result["score"], 0)

    def test_version_parameters_and_audit_are_reproducible(self):
        first = score_entity(ENTITY, [evidence()], AS_OF)
        second = score_entity(ENTITY, [evidence()], AS_OF)
        self.assertEqual(first, second)
        self.assertEqual(first["model_version"], MODEL_VERSION)
        self.assertEqual(first["parameters_hash"], PARAMETERS_HASH)
        self.assertEqual(len(PARAMETERS_HASH), 64)
        self.assertTrue(first["evidence_audit"][0]["source_url"])
        self.assertEqual(analyze("", ENTITY)["model_version"], NLP_VERSION)

    def test_empty_no_evidence_and_invalid_as_of(self):
        result = score_entity(ENTITY, [], AS_OF)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["status"], "no_evidence")
        with self.assertRaises(ValueError):
            score_entity(ENTITY, [], "invalid")


if __name__ == "__main__":
    unittest.main()
