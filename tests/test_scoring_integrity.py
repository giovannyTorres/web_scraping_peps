"""Offline synthetic regressions for score cache integrity and copy ordering."""
import copy
import hashlib
from itertools import permutations
import unittest

from peps.nlp import analyze
from peps.scoring import score_entity


ENTITY = {"id": "integrity-fixture", "name": "José Peña Villaluz", "kind": "person",
          "aliases": [], "context": {}}
AS_OF = "2026-10-15"


def evidence(text="José Peña Villaluz fue detenido por fraude.", **changes):
    row = {"id": "e1", "text": text, "source_kind": "news",
           "source_url": "https://fixture.invalid/integrity", "published_at": "2026-10-10",
           "content_hash": hashlib.sha256(text.encode()).hexdigest()}
    row.update(changes)
    return row


class ScoringIntegrityTests(unittest.TestCase):
    def test_current_metadata_cannot_authorize_invented_decisions(self):
        text = "José Peña Villaluz inauguró una biblioteca."
        cached = analyze(text, ENTITY)
        cached["findings"] = [{"category": "violence", "stage": "conviction",
                               "relationship": "direct", "quote": text,
                               "start": 0, "end": len(text), "negated": False}]
        result = score_entity(ENTITY, [evidence(text, analysis=cached)], AS_OF)
        self.assertEqual(result["score"], 0)
        self.assertFalse(result["components"])
        self.assertIn("cached_analysis_content_mismatch", result["evidence_audit"][0]["exclusions"])

    def test_current_cache_cannot_change_scoring_stage_or_identity(self):
        row = evidence()
        original = analyze(row["text"], ENTITY)
        variants = []
        changed = copy.deepcopy(original)
        changed["findings"][0]["stage"] = "conviction"
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["identity_confidence"] = 1.0
        variants.append(changed)
        changed = copy.deepcopy(original)
        changed["mentions"][0]["entity_id"] = "another-entity"
        variants.append(changed)
        for cached in variants:
            with self.subTest(cached=cached):
                result = score_entity(ENTITY, [dict(row, analysis=cached)], AS_OF)
                self.assertEqual(result["score"], 0)
                self.assertIn("cached_analysis_content_mismatch", result["evidence_audit"][0]["exclusions"])

    def test_obsolete_metadata_recomputes_and_intact_cache_preserves_score(self):
        row = evidence()
        baseline = score_entity(ENTITY, [row], AS_OF)
        cached = analyze(row["text"], ENTITY)
        valid = score_entity(ENTITY, [dict(row, analysis=cached)], AS_OF)
        self.assertEqual(valid["score"], baseline["score"])
        self.assertFalse(valid["evidence_audit"][0]["analysis_recomputed"])
        cached["rules_hash"] = "obsolete-rules"
        cached["findings"][0]["stage"] = "conviction"
        stale = score_entity(ENTITY, [dict(row, analysis=cached)], AS_OF)
        self.assertEqual(stale["score"], baseline["score"])
        self.assertTrue(stale["evidence_audit"][0]["analysis_recomputed"])

    def test_republished_copy_cannot_choose_group_by_input_order(self):
        rows = [evidence(id="old", published_at="2018-01-01"),
                evidence(id="new", published_at="2026-10-10"),
                evidence("Detuvieron por fraude a José Peña Villaluz.", id="paraphrase")]
        baseline = score_entity(ENTITY, rows, AS_OF)
        self.assertEqual(baseline["score"], 17)
        self.assertEqual(len(baseline["components"]), 2)
        for ordering in permutations(rows):
            with self.subTest(order=[r["id"] for r in ordering]):
                result = score_entity(ENTITY, list(ordering), AS_OF)
                self.assertEqual(result["score"], baseline["score"])
                self.assertEqual(result["components"], baseline["components"])
                groups = {a["evidence_id"]: [f.get("event_group") for f in a["findings"]]
                          for a in result["evidence_audit"]}
                self.assertEqual(groups["old"], groups["new"])
                self.assertIn("2018-Q1", groups["old"][0])

    def test_rejected_older_copy_does_not_select_group_or_reduce_freshness(self):
        rows = [evidence(id="old", published_at="2018-01-01", review={"status": "rejected"}),
                evidence(id="new"), evidence("Detuvieron por fraude a José Peña Villaluz.", id="paraphrase")]
        baseline = score_entity(ENTITY, rows[1:], AS_OF)
        for ordering in permutations(rows):
            result = score_entity(ENTITY, list(ordering), AS_OF)
            self.assertEqual(result["score"], baseline["score"])
            self.assertEqual(result["components"], baseline["components"])
            self.assertTrue(all("2026-Q4" in c["event_group"] for c in result["components"]))


if __name__ == "__main__":
    unittest.main()
