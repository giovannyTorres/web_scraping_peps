"""Independent, synthetic adversarial holdout; not a real-world accuracy claim.

Cases were authored separately from tests/data/identity_cases.jsonl. Labels
prioritize refusing attribution when a verified bank anchor belongs to a
different subject, a negation, an anniversary, or a title-only occurrence.
Run --audit PATH to export every expected label and actual resolution.
"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps import identity
from peps.identity import IdentityResolver
from peps.engine import Engine
from peps.news import NewsAcquirer, article_text
from peps.store import digest, now


def verified(fields):
    context = dict(fields)
    context["verified_fields"] = {key: {"verified": True, "source": "synthetic:independent-bank-review"}
                                  for key in fields}
    return context


def person(entity_id="h1", context=None):
    return dict(id=entity_id, name="Naira Cedro Olmos", kind="person", aliases=[], context=context or {})


def corporation(entity_id="hc", context=None):
    return dict(id=entity_id, name="Operadora Cúpula Cedro, S.A. de C.V.", kind="company", aliases=[], context=context or {})


def holdout_cases():
    p1 = person(context=verified(dict(role="contralora", state="Durango")))
    p2 = person("h2", verified(dict(role="veterinaria", state="Colima")))
    rfc_person = person(context=verified(dict(rfc="CEON800101AB1")))
    aged = person(context=verified(dict(role="contralora", age="50")))
    born = person(context=verified(dict(role="contralora", birth_date="1980-01-01")))
    c1 = corporation(context={**verified(dict(rfc="OCC010101AB1")),
        "verified_aliases": [{"name": "CedroMX", "verified": True, "source": "synthetic:trademark-register"},
                             {"name": "OCC", "verified": True, "source": "synthetic:acronym-register"}]})
    buyer = dict(id="buyer", name="Nexo Puerto, S.A. de C.V.", kind="company", aliases=[],
                 context=verified(dict(rfc="NPU020202AB2")))
    nonmatch = ["candidate", "ambiguous", "rejected"]
    return [
        dict(id="independent_direct_verified_positive", entities=[p1],
             text="Naira Cedro Olmos, contralora de Durango, presentó su informe técnico.", expected={"h1": ["matched"]}, supported_counts={"h1": 1}),
        dict(id="independent_two_homonyms_one_paragraph", entities=[p1, p2],
             text="Naira Cedro Olmos, contralora de Durango, presentó cuentas y Naira Cedro Olmos, veterinaria de Colima, atendió a un paciente.",
             expected={"h1": ["matched"], "h2": ["matched"]}, supported_counts={"h1": 1, "h2": 1}),
        dict(id="independent_homonym_later_adverse_does_not_inherit_anchor", entities=[p1, p2],
             text="Naira Cedro Olmos, contralora de Durango, presentó un informe. Naira Cedro Olmos fue detenida por fraude; la nota no precisa su profesión ni lugar de origen.",
             expected={"h1": ["matched"], "h2": nonmatch}, supported_counts={"h1": 1, "h2": 0}),
        dict(id="independent_accompanied_another_official", entities=[p1],
             text="Naira Cedro Olmos acompañó a la contralora de Durango durante el recorrido.", expected={"h1": nonmatch}),
        dict(id="independent_listened_to_another_official", entities=[p1],
             text="Naira Cedro Olmos escuchó a la contralora de Durango y entregó sus preguntas.", expected={"h1": nonmatch}),
        dict(id="independent_negated_role_and_jurisdiction", entities=[p1],
             text="Naira Cedro Olmos no es contralora de Durango; se trata de una persona homónima.", expected={"h1": nonmatch}),
        dict(id="independent_employer_and_role_denial", entities=[person(context=verified(dict(role="contralora", organization="Instituto del Cedro")))],
             text="Naira Cedro Olmos no trabaja en el Instituto del Cedro ni ejerce como contralora.", expected={"h1": nonmatch}),
        dict(id="independent_unverified_wrong_rfc_not_rejection", entities=[person(context={"rfc": "CEON800101AB1"})],
             text="Naira Cedro Olmos, RFC CEON800101ZZ9, acudió a la audiencia.", expected={"h1": ["candidate"]}),
        dict(id="independent_rfc_of_audited_supplier", entities=[rfc_person],
             text="Naira Cedro Olmos auditó al proveedor con RFC CEON800101AB1 durante su investigación.", expected={"h1": nonmatch}),
        dict(id="independent_rfc_of_compared_company", entities=[rfc_person],
             text="Naira Cedro Olmos comparó a una sociedad identificada con RFC CEON800101AB1 con la licitante ganadora.", expected={"h1": nonmatch}),
        dict(id="independent_age_anniversary_of_institution", entities=[aged],
             text="Naira Cedro Olmos, contralora, celebró los 50 años del Instituto del Cedro.", expected={"h1": nonmatch}),
        dict(id="independent_age_direct_positive", entities=[aged],
             text="Naira Cedro Olmos, contralora de 50 años, presentó un informe.", expected={"h1": ["matched"]}),
        dict(id="independent_birth_date_of_grandfather", entities=[born],
             text="Naira Cedro Olmos, contralora, recordó que su abuelo nació el 1980-01-01 en una comunidad rural.", expected={"h1": nonmatch}),
        dict(id="independent_buyer_is_not_acquired_brand", entities=[c1, buyer],
             text="Nexo Puerto, S.A. de C.V., RFC NPU020202AB2, anunció la adquisición de la marca CedroMX.",
             expected={"buyer": ["matched"], "hc": nonmatch}),
        dict(id="independent_company_other_suppliers_rfc", entities=[c1],
             text="Operadora Cúpula Cedro, S.A. de C.V., contrató a un proveedor con RFC OCC010101AB1.", expected={"hc": nonmatch}),
        dict(id="independent_brand_and_owned_rfc_positive", entities=[c1],
             text="CedroMX, RFC OCC010101AB1, presentó sus estados financieros.", expected={"hc": ["matched"]}),
        dict(id="independent_acronym_anchor_only_in_title", entities=[c1],
             title="OCC, RFC OCC010101AB1: información institucional", text="La página presenta información general sin identificar a una empresa en el artículo.",
             expected={"hc": nonmatch}),
        dict(id="independent_body_name_title_only_role_anchors", entities=[p1],
             title="Contralora de Durango presenta un balance", text="Naira Cedro Olmos asistió a la sesión junto a varias personas.", expected={"h1": nonmatch}),
        dict(id="independent_rfc_person_company_format_mismatch", entities=[person(context=verified(dict(rfc="OCC010101AB1")))],
             text="Naira Cedro Olmos, RFC OCC010101AB1, leyó una declaración.", expected={"h1": nonmatch}),
        dict(id="independent_attribution_break_before_second_named_subject", entities=[p1,
             dict(id="witness", name="Dalia Pino Lumbre", kind="person", aliases=[], context={})],
             text="Naira Cedro Olmos saludó a Dalia Pino Lumbre, contralora de Durango, al terminar el evento.",
             expected={"h1": nonmatch, "witness": nonmatch}),
    ]


def evaluate_holdout():
    records = []
    for case in holdout_cases():
        resolver = IdentityResolver(copy.deepcopy(case["entities"]))
        body = case["text"]
        rows = resolver.resolve(body, case.get("title", ""), case.get("metadata"))
        by_id = {row["entity_id"]: row for row in rows}
        errors = []
        for entity_id, allowed in case["expected"].items():
            actual = by_id.get(entity_id, {}).get("status", "absent")
            if actual not in allowed:
                errors.append(dict(entity_id=entity_id, expected=allowed, actual=actual, issue="status"))
        for entity_id, expected_count in case.get("supported_counts", {}).items():
            actual_count = sum(m.get("identity_supported", False) for m in by_id.get(entity_id, {}).get("mentions", []))
            if actual_count != expected_count:
                errors.append(dict(entity_id=entity_id, expected=expected_count, actual=actual_count, issue="supported_body_mentions"))
        for row in rows:
            for mention in row["mentions"]:
                if body[mention["start"]:mention["end"]] != mention["text"]:
                    errors.append(dict(entity_id=row["entity_id"], issue="invalid_body_span"))
        records.append(dict(case_id=case["id"], synthetic=True, input=case,
                            passed=not errors, errors=errors, resolutions=rows))
    return dict(generated_at=datetime.now(timezone.utc).isoformat(),
                scope="independently_authored_synthetic_adversarial_holdout_not_real_world_accuracy",
                model_version=identity.MODEL_VERSION, rules_hash=identity.RULES_HASH,
                module_sha256=hashlib.sha256(Path(identity.__file__).read_bytes()).hexdigest(),
                cases=len(records), passed=sum(r["passed"] for r in records),
                failed=sum(not r["passed"] for r in records),
                false_positive_matched=sum(e["issue"] == "status" and e["actual"] == "matched"
                                          for r in records for e in r["errors"]),
                results=records,
                limitation="Synthetic labels test specific refusal boundaries; they do not estimate recall, precision, probability or coverage on Mexican news. Human source-backed identity review remains required.")


class IndependentIdentityHoldout(unittest.TestCase):
    def test_independent_adversarial_holdout(self):
        for record in evaluate_holdout()["results"]:
            with self.subTest(case=record["case_id"]):
                self.assertTrue(record["passed"], json.dumps(record["errors"], ensure_ascii=False))


class IndependentNewsScoringIntegration(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(Path(self.directory.name), online=False)
        self.news = NewsAcquirer(self.engine)

    def seed(self, entities, body, metadata_only=False):
        self.engine.store.import_entities(entities)
        url = "https://synthetic.example/independent-holdout"
        article_id = digest(url)
        doc_id = None
        if not metadata_only:
            doc_id = self.engine.store.add_document(dict(url=url, title="Prueba sintética independiente", text=body,
                source_id="independent", source_kind="news", published_at="2026-09-01",
                metadata={"news_module": True, "scope": "full_article", "publisher_id": "synthetic.example"}), [], lambda *_: {})
        with self.engine.store.connection() as con:
            con.execute("INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,published_at,discovered_at,document_id,scope,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (article_id, url, "synthetic.example", "independent", "Prueba sintética independiente", body if metadata_only else "",
                         "2026-09-01", now(), doc_id, "feed_metadata_only" if metadata_only else "full_article", "discovered" if metadata_only else "downloaded"))
        self.news.attribute()

    def score(self, entity_id):
        self.engine.rescore(entity_id)
        with self.engine.store.connection() as con:
            return json.loads(con.execute("SELECT result FROM scores WHERE entity_id=?", (entity_id,)).fetchone()[0])

    def choose(self, entity_id, index):
        evidence = self.engine.store.evidence(entity_id)[0]
        span = evidence["identity_resolution"]["mentions"][index]
        self.engine.store.review(evidence["id"], "confirmed", "synthetic-independent-reviewer",
                                 "Synthetic source-backed selection of this specific occurrence; no real person assessed.", identity_spans=[span])

    def test_ambiguous_homonyms_do_not_score_without_review(self):
        self.seed([person(), person("h2")], "Naira Cedro Olmos fue detenida por fraude.")
        for entity_id in ("h1", "h2"):
            self.assertEqual(self.score(entity_id)["score"], 0)
            self.assertEqual(self.engine.store.evidence(entity_id)[0]["identity_resolution"]["status"], "ambiguous")

    def test_second_homonym_adverse_does_not_contaminate_supported_first(self):
        case = holdout_cases()[2]
        self.seed(case["entities"], case["text"])
        score = self.score("h1")
        self.assertEqual(score["score"], 0)
        self.assertEqual(self.engine.store.evidence("h1")[0]["identity_resolution"]["status"], "matched")
        self.assertTrue(any("finding_outside_corroborated_identity_span" in finding["exclusions"]
                            for audit in score["evidence_audit"] for finding in audit["findings"]))

    def test_human_can_select_adverse_second_homonym_only(self):
        case = holdout_cases()[2]
        self.seed(case["entities"], case["text"])
        self.choose("h2", 1)
        self.assertGreater(self.score("h2")["validated_score"], 0)
        self.assertEqual(self.score("h1")["score"], 0)

    def test_unselected_ambiguous_multiple_mentions_stay_unscored(self):
        case = holdout_cases()[2]
        self.seed(case["entities"], case["text"])
        evidence = self.engine.store.evidence("h2")[0]
        self.engine.store.review(evidence["id"], "confirmed", "synthetic-reviewer", "No occurrence selected; review is insufficient.")
        self.assertEqual(self.score("h2")["score"], 0)

    def test_bank_revision_invalidates_old_resolution(self):
        case = holdout_cases()[2]
        self.seed(case["entities"], case["text"])
        self.choose("h2", 1)
        self.assertGreater(self.score("h2")["validated_score"], 0)
        self.engine.store.import_entities([dict(id="new-peer", name="Naira Cedro Olmos", kind="person", aliases=[], context={})])
        score = self.score("h2")
        self.assertEqual(score["score"], 0)
        self.assertIn("stale_or_invalid_identity_resolution", score["evidence_audit"][0]["exclusions"])

    def test_article_extraction_excludes_acronym_in_navigation(self):
        html = '<html><body><nav>CedroMX, RFC OCC010101AB1, fue condenada por fraude.</nav><article><h1>Vida comunitaria</h1><p>Se celebró una sesión pública para revisar los servicios locales.</p></article></body></html>'
        body = article_text(html)
        self.assertNotIn("CedroMX", body)
        self.assertNotIn("OCC010101AB1", body)
        self.assertEqual(IdentityResolver(holdout_cases()[15]["entities"]).resolve(body), [])

    def test_feed_metadata_never_scores_even_after_human_selection(self):
        entity = person(context=verified(dict(role="contralora", state="Durango")))
        self.seed([entity], "Naira Cedro Olmos, contralora de Durango, fue detenida por fraude.", metadata_only=True)
        self.choose("h1", 0)
        score = self.score("h1")
        self.assertEqual(score["score"], 0)
        self.assertIn("discovery_index_is_not_article_evidence", score["evidence_audit"][0]["exclusions"])

    def test_verified_match_scores_provisionally_then_human_validation(self):
        entity = person(context=verified(dict(role="contralora", state="Durango")))
        self.seed([entity], "Naira Cedro Olmos, contralora de Durango, fue condenada por fraude.")
        score = self.score("h1")
        self.assertGreater(score["provisional_score"], 0)
        self.assertEqual(score["validated_score"], 0)
        self.choose("h1", 0)
        self.assertGreater(self.score("h1")["validated_score"], 0)


def evaluate_integration():
    records = []
    for name in unittest.TestLoader().getTestCaseNames(IndependentNewsScoringIntegration):
        result = unittest.TestResult()
        IndependentNewsScoringIntegration(name).run(result)
        records.append(dict(test=name, passed=result.wasSuccessful(),
                            errors=[message for _, message in result.errors + result.failures]))
    return dict(scope="offline_news_attribution_to_scoring_synthetic_integration", tests=len(records),
                passed=sum(r["passed"] for r in records), failed=sum(not r["passed"] for r in records), results=records)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--audit":
        report = evaluate_holdout()
        report["scoring_integration"] = evaluate_integration()
        baseline = Path(__file__).resolve().parents[1] / "work/identity_holdout_baseline.json"
        if baseline.exists():
            previous = json.loads(baseline.read_text(encoding="utf-8"))
            report["initial_holdout_audit"] = {key: previous[key] for key in
                ("generated_at", "module_sha256", "cases", "passed", "failed", "false_positive_matched")}
            report["initial_holdout_audit"]["failed_cases"] = [r["case_id"] for r in previous["results"] if not r["passed"]]
        destination = Path(sys.argv[2])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: report[key] for key in ("cases", "passed", "failed", "false_positive_matched", "model_version", "scoring_integration")}, indent=2))
        raise SystemExit(bool(report["failed"] or report["scoring_integration"]["failed"]))
    unittest.main()
