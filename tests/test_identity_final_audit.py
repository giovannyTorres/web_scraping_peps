"""Final independent identity lifecycle regressions; synthetic offline sources."""
import json
import unittest
from unittest.mock import patch

from peps import identity
from peps.scoring import score_entity
import tests.test_identity_holdout as holdout


class FinalIdentityLifecycleAudit(unittest.TestCase):
    def setUp(self):
        self.fixture = holdout.IndependentNewsScoringIntegration("test_ambiguous_homonyms_do_not_score_without_review")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.engine, self.news = self.fixture.engine, self.fixture.news

    def test_review_does_not_revive_after_bank_change_and_reattribution(self):
        self.fixture.seed([holdout.person()], "Naira Cedro Olmos fue detenida por fraude.")
        self.fixture.choose("h1", 0)
        self.assertGreater(self.fixture.score("h1")["validated_score"], 0)
        self.engine.store.import_entities([holdout.person("h2")])
        self.assertEqual(self.fixture.score("h1")["validated_score"], 0)
        self.news.attribute()
        after = self.fixture.score("h1")
        self.assertEqual(after["validated_score"], 0)
        self.assertEqual(self.engine.store.evidence("h1")[0]["identity_resolution"]["status"], "ambiguous")
        self.assertIn("human_identity_review_stale_or_incomplete", after["evidence_audit"][0]["exclusions"])
        self.fixture.choose("h1", 0)
        self.assertGreater(self.fixture.score("h1")["validated_score"], 0)

    def test_identical_import_is_not_a_bank_revision_change(self):
        entity = holdout.person()
        self.fixture.seed([entity], "Naira Cedro Olmos fue detenida por fraude.")
        self.fixture.choose("h1", 0)
        evidence = self.engine.store.evidence("h1")[0]
        self.engine.store.import_entities([entity])
        self.assertEqual(self.engine.store.evidence("h1")[0]["identity_bank_revision"], evidence["identity_bank_revision"])
        self.assertGreater(self.fixture.score("h1")["validated_score"], 0)

    def test_retired_link_keeps_review_history_and_restore_requires_new_review(self):
        entity = holdout.person()
        self.fixture.seed([entity], "Naira Cedro Olmos fue detenida por fraude.")
        self.fixture.choose("h1", 0)
        self.assertGreater(self.fixture.score("h1")["validated_score"], 0)
        self.engine.store.import_entities([dict(entity, name="Persona Distinta Sintética")])
        self.news.attribute()
        self.assertEqual(self.engine.store.evidence("h1"), [])
        self.assertEqual(self.fixture.score("h1")["score"], 0)
        with self.engine.store.connection() as con:
            history = con.execute("SELECT * FROM news_identity_history WHERE entity_id='h1'").fetchall()
        self.assertEqual(len(history), 1)
        retired_review = json.loads(history[0]["review"])
        self.assertEqual(retired_review["status"], "confirmed")
        self.assertTrue(retired_review["identity_spans"])
        self.assertTrue(retired_review["actor"])
        self.engine.store.import_entities([entity])
        self.news.attribute()
        self.assertEqual(self.fixture.score("h1")["validated_score"], 0)
        self.assertNotEqual(self.engine.store.evidence("h1")[0]["review"].get("status"), "confirmed")
        self.assertTrue(self.engine.store.verify_audit()["ok"])

    def test_offline_news_import_cannot_score_both_unresolved_homonyms(self):
        self.engine.store.import_entities([holdout.person(), holdout.person("h2")])
        self.engine.import_documents([{"url": "https://synthetic.example/imported", "source_kind": "news",
            "text": "Naira Cedro Olmos fue detenida por fraude.", "published_at": "2026-09-01"}])
        for entity_id in ("h1", "h2"):
            evidence = self.engine.store.evidence(entity_id)
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0]["identity_resolution"]["status"], "ambiguous")
            self.assertEqual(self.fixture.score(entity_id)["score"], 0)

    def test_optional_top_level_fields_are_preserved_as_unverified_hints(self):
        self.engine.store.import_entities([{"id": "h1", "name": "Naira Cedro Olmos", "cargo": "contralora",
            "estado": "Durango", "municipio": "Durango", "RFC": "CEON800101AB1", "edad": "50",
            "fecha_nacimiento": "1980-01-01"}])
        entity = self.engine.store.get_entity("h1")
        context = entity["context"]
        self.assertEqual(context["role"], "contralora")
        self.assertEqual(context["state"], "Durango")
        self.assertEqual(context["municipality"], "Durango")
        self.assertEqual(context["rfc"], "CEON800101AB1")
        self.assertEqual(str(context["age"]), "50")
        self.assertEqual(context["birth_date"], "1980-01-01")
        row = identity.IdentityResolver([entity]).resolve("Naira Cedro Olmos, contralora de Durango, RFC CEON800101AB1.")[0]
        self.assertEqual(row["status"], "candidate")

    def test_revoked_commercial_alias_and_old_review_do_not_score(self):
        entity = holdout.holdout_cases()[15]["entities"][0]
        body = "CedroMX, RFC OCC010101AB1, fue condenada por fraude."
        self.fixture.seed([entity], body)
        self.fixture.choose("hc", 0)
        self.assertGreater(self.fixture.score("hc")["validated_score"], 0)
        changed = json.loads(json.dumps(entity))
        changed["context"]["verified_aliases"][0]["status"] = "revoked"
        self.engine.store.import_entities([changed])
        self.news.attribute()
        self.assertEqual(self.fixture.score("hc")["score"], 0)
        self.assertEqual(self.engine.store.evidence("hc")[0]["identity_resolution"]["status"], "candidate")

    def test_index_cannot_be_tagged_with_revision_imported_during_construction(self):
        self.fixture.seed([holdout.person()], "Naira Cedro Olmos fue detenida por fraude.")
        original_revision = self.engine.store.evidence("h1")[0]["identity_bank_revision"]
        real_resolver = identity.IdentityResolver
        def build_after_import(entities):
            self.engine.store.import_entities([holdout.person("h2")])
            return real_resolver(entities)
        with patch("peps.identity.IdentityResolver", side_effect=build_after_import):
            self.news.attribute(force=True)
        evidence = self.engine.store.evidence("h1")[0]
        self.assertEqual(evidence["identity_resolution"]["bank_revision"], original_revision)
        self.assertGreater(evidence["identity_bank_revision"], original_revision)
        self.assertEqual(self.fixture.score("h1")["score"], 0)
        self.news.attribute()
        self.assertEqual(self.engine.store.evidence("h1")[0]["identity_resolution"]["status"], "ambiguous")

    def test_identity_rules_hash_is_required_for_scoring(self):
        entity = holdout.holdout_cases()[0]["entities"][0]
        self.fixture.seed([entity], "Naira Cedro Olmos, contralora de Durango, fue detenida por fraude.")
        self.assertGreater(self.fixture.score("h1")["provisional_score"], 0)
        evidence = self.engine.store.evidence("h1")
        evidence[0]["identity_resolution"]["features"]["rules_hash"] = "outdated-rules"
        score = score_entity(self.engine.store.get_entity("h1"), evidence)
        self.assertEqual(score["score"], 0)
        self.assertIn("stale_or_invalid_identity_resolution", score["evidence_audit"][0]["exclusions"])

    def test_identity_rules_change_requeues_attribution_without_force(self):
        self.fixture.seed([holdout.person()], "Naira Cedro Olmos fue detenida por fraude.")
        self.assertEqual(self.news.attribute()["articles_processed"], 0)
        with patch("peps.identity.RULES_HASH", "synthetic-upgraded-rules"):
            result = self.news.attribute()
            self.assertEqual(result["articles_processed"], 1)
            self.assertEqual(self.engine.store.evidence("h1")[0]["identity_resolution"]["features"]["rules_hash"], "synthetic-upgraded-rules")


if __name__ == "__main__":
    unittest.main()
