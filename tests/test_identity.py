"""Synthetic identity labels; no real person's reputation is evaluated here."""
import concurrent.futures
import copy
import hashlib
import json
import os
import platform
import sys
import time
import unicodedata
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from peps import identity as identity_module
from peps.identity import (CONFIDENCE_KIND, MODEL_VERSION, IdentityResolver,
                           entity_for_analysis, entity_signature, normalize_name)


def verified(context, *fields):
    context = copy.deepcopy(context)
    context["verified_fields"] = {field: {"status": "verified", "source": "bank-review:synthetic-1"}
                                  for field in fields}
    return context


def person(entity_id="p1", name="Juan Pérez", context=None, aliases=None):
    return {"id": entity_id, "name": name, "kind": "person", "aliases": aliases or [], "context": context or {}}


def company(entity_id="c1", name="Luz de Nogal, S.A. de C.V.", context=None, aliases=None):
    return {"id": entity_id, "name": name, "kind": "company", "aliases": aliases or [], "context": context or {}}


P1 = {"state": "Oaxaca", "role": "alcalde", "organization": "Instituto de Servicios Locales"}
P2 = {"state": "Chiapas", "role": "médico", "organization": "Clínica Las Palmas"}
C1 = {"rfc": "LNO010101AB1", "state": "Sonora", "organization": "Cámara del Nogal"}
C2 = {"rfc": "LNO010101AB2", "state": "Sinaloa", "organization": "Cámara del Pino"}


def banks():
    return {
        "one": [person()],
        "hints": [person(context=P1)],
        "homonyms": [person(context=P1), person("p2", context=P2)],
        "verified": [person(context=verified(P1, "state", "role", "organization")),
                     person("p2", context=verified(P2, "state", "role", "organization"))],
        "long": [person(name="José Peña Villaluz", context=verified(P1, "state", "role"))],
        "company": [company(context={**C1, "trade_names": ["LuzNogal", "LN"]})],
        "companies": [company(context=C1), company("c2", "Luz de Nogal SAB de CV", context=C2)],
        "companies_verified": [company(context=verified(C1, "rfc", "state", "organization")),
                               company("c2", "Luz de Nogal SAB de CV", context=verified(C2, "rfc", "state", "organization"))],
        "verified_trade": [company(context={**verified(C1, "rfc", "state", "organization"),
            "verified_aliases": [{"name": "LuzNogal", "source": "bank-review:trade", "status": "verified"},
                                 {"name": "LN", "source": "bank-review:acronym", "status": "verified"}]})],
        "partial": [person(name="Juan Pérez Montes", aliases=["Juan Pérez", "JPM"])],
        "unvalidated": [company(context=verified(C1, "rfc"), aliases=[{
            "name": "LuzNogal", "validated": False, "source": "bank-review:rejected"}])],
        "rfc_hint": [person(context={"rfc": "PEJJ800101AB1"})],
        "rfc_verified": [person(context=verified({"rfc": "PEJJ800101AB1"}, "rfc"))],
    }


class IdentityFixtureTests(unittest.TestCase):
    def test_adversarial_labeled_cases(self):
        cases = [json.loads(line) for line in (Path(__file__).parent / "data" / "identity_cases.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertGreaterEqual(len(cases), 60)
        fixtures = banks()
        for case in cases:
            with self.subTest(case=case["id"]):
                resolver = IdentityResolver(case.get("entities", fixtures.get(case.get("bank"))))
                body, title = case.get("text", ""), case.get("title", "")
                rows = resolver.resolve(body, title, case.get("metadata"))
                self.assertEqual({row["entity_id"]: row["status"] for row in rows}, case["expected"], rows)
                for row in rows:
                    self.assertTrue(0 <= row["confidence"] <= 1)
                    self.assertEqual(row["features"]["confidence_kind"], CONFIDENCE_KIND)
                    self.assertEqual(row["content_hash"], hashlib.sha256(body.encode()).hexdigest())
                    self.assertEqual(row["model_version"], MODEL_VERSION)
                    for mention in row["mentions"]:
                        self.assertEqual(body[mention["start"]:mention["end"]], mention["text"])
                    for mention in row["features"]["title_mentions"]:
                        self.assertEqual(title[mention["start"]:mention["end"]], mention["text"])
                    for support in row["features"]["supports"]:
                        for attribute in support["attributes"] + support["expired_attributes"]:
                            source = body if attribute["part"] == "body" else title
                            self.assertEqual(source[attribute["start"]:attribute["end"]], attribute["text"])
                    if row["status"] == "matched":
                        self.assertTrue(any(mention["identity_supported"] for mention in row["mentions"]))
                        self.assertFalse(row["features"]["review_required"])

    def test_unicode_offsets_and_enye_do_not_collapse(self):
        for name in ("José Peña Villaluz", "Ana Muñoz Trébol", "Íñigo Cañón Albor"):
            for conversion in (str, str.upper, str.lower, lambda s: unicodedata.normalize("NFD", s)):
                body = "📰 México — " + conversion(name) + " participa en la sesión."
                row = IdentityResolver([person(name=name)]).resolve(body)[0]
                self.assertEqual(len(row["mentions"]), 1)
                self.assertEqual(row["mentions"][0]["start"], len("📰 México — "))
                self.assertEqual(body[row["mentions"][0]["start"]:row["mentions"][0]["end"]], conversion(name))
        self.assertEqual(IdentityResolver([person(name="José Peña Villaluz")]).resolve("José Pena Villaluz"), [])

    def test_identity_is_deterministic_and_bank_is_not_mutated(self):
        bank = banks()["verified"]
        before = copy.deepcopy(bank)
        resolver = IdentityResolver(bank)
        body = "Juan Pérez, alcalde de Oaxaca, participa en una sesión."
        expected = resolver.resolve(body)
        bank[0]["name"] = "Cambio posterior"
        self.assertEqual(expected, resolver.resolve(body))
        self.assertEqual(before[1], bank[1])
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertTrue(all(r == expected for r in pool.map(lambda _: resolver.resolve(body), range(50))))

    def test_same_names_preserve_distinct_ids_and_signatures(self):
        bank = [person("one"), person("two")]
        rows = IdentityResolver(bank).resolve("Juan Pérez")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["status"] for row in rows}, {"ambiguous"})
        self.assertNotEqual(rows[0]["entity_signature"], rows[1]["entity_signature"])
        for row in rows:
            self.assertEqual(row["competing_entity_ids"], ["two" if row["entity_id"] == "one" else "one"])

    def test_many_homonyms_do_not_construct_quadratic_competitor_lists(self):
        resolver = IdentityResolver(person(str(i)) for i in range(2000))
        rows = resolver.resolve("Juan Pérez participa.")
        self.assertEqual(len(rows), 2000)
        for row in rows:
            self.assertEqual(row["status"], "ambiguous")
            self.assertEqual(len(row["competing_entity_ids"]), 100)
            self.assertTrue(row["features"]["competing_entity_ids_truncated"])
            self.assertEqual(row["features"]["competing_entity_count_lower_bound"], 1999)

    def test_same_verified_homonyms_remain_ambiguous(self):
        bank = [person(str(i), context=verified(P1, "state", "role")) for i in range(200)]
        rows = IdentityResolver(bank).resolve("Juan Pérez, alcalde de Oaxaca, participa.")
        self.assertEqual({row["status"] for row in rows}, {"ambiguous"})

    def test_unrelated_rfc_cannot_anchor_the_named_reporter(self):
        resolver = IdentityResolver(banks()["rfc_verified"])
        for text in ("Juan Pérez denunció a otra empresa con RFC PEJJ800101AB1.",
                     "Juan Pérez consultó el RFC PEJJ800101AB1.",
                     "Juan Pérez informó que el RFC PEJJ800101AB1 corresponde a otra persona."):
            self.assertEqual(resolver.resolve(text)[0]["status"], "candidate")

    def test_negation_homonym_and_other_official_context_are_not_anchors(self):
        resolver = IdentityResolver([person(context=verified(P1, "role", "state"))])
        cases = ("Juan Pérez no es alcalde de Oaxaca.",
                 "Juan Pérez habló con el alcalde de Oaxaca.",
                 "Juan Pérez, homónimo del alcalde de Oaxaca, participó.",
                 "Juan Pérez, sin relación con el alcalde de Oaxaca, participó.",
                 "No se trata de Juan Pérez, alcalde de Oaxaca.")
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(resolver.resolve(text)[0]["status"], "candidate")

    def test_public_normalizer_preserves_enye_and_has_no_identity_inference(self):
        self.assertEqual(normalize_name("  JOSÉ PEÑA, S.A. de C.V. "), "jose peña sa de cv")
        self.assertNotEqual(normalize_name("Peña"), normalize_name("Pena"))

    def test_only_explicit_verification_with_provenance_is_an_anchor(self):
        variants = [P1, {**P1, "verified_fields": ["state", "role"]},
                    {**P1, "verified_fields": {"state": True, "role": True}},
                    {**P1, "verified_fields": {"state": {"verified": True}, "role": {"verified": True}}}]
        body = "Juan Pérez, alcalde de Oaxaca, participa."
        for context in variants:
            self.assertEqual(IdentityResolver([person(context=context)]).resolve(body)[0]["status"], "candidate")
        context = {**P1, "verified_fields": ["state", "role"],
                   "provenance": {"state": "review:state", "role": "review:role"}}
        self.assertEqual(IdentityResolver([person(context=context)]).resolve(body)[0]["status"], "matched")

    def test_hints_rank_candidates_without_establishing_identity(self):
        rows = IdentityResolver(banks()["homonyms"]).resolve("Juan Pérez, alcalde de Oaxaca.")
        by_id = {row["entity_id"]: row for row in rows}
        self.assertGreater(by_id["p1"]["confidence"], by_id["p2"]["confidence"])
        self.assertEqual({row["status"] for row in rows}, {"ambiguous"})
        rows = IdentityResolver(banks()["companies"]).resolve("Luz de Nogal, RFC LNO010101AB1.")
        by_id = {row["entity_id"]: row for row in rows}
        self.assertGreater(by_id["c1"]["confidence"], by_id["c2"]["confidence"])
        self.assertEqual({row["status"] for row in rows}, {"ambiguous"})

    def test_observed_alias_helper_requires_match_current_signature_and_body(self):
        entity = banks()["verified_trade"][0]
        resolution = IdentityResolver([entity]).resolve("LuzNogal, RFC LNO010101AB1, presentó su informe.")[0]
        self.assertEqual(resolution["status"], "matched")
        observed = entity_for_analysis(entity, resolution)
        self.assertIn("LuzNogal", observed["aliases"])
        self.assertEqual(entity_signature(entity), resolution["entity_signature"])
        self.assertEqual(entity["aliases"], [])
        stale = {**entity, "context": {**entity["context"], "rfc": "LNO010101AB2"}}
        self.assertEqual(entity_for_analysis(stale, resolution), stale)
        candidate = {**resolution, "status": "candidate"}
        self.assertEqual(entity_for_analysis(entity, candidate), entity)

    def test_role_dates_are_checked_only_against_publication_date(self):
        context = {"roles": [{"name": "alcalde", "jurisdiction": "Oaxaca", "valid_from": "2018-01-01",
                              "valid_to": "2020-12-31", "verified": True, "source": "review:role"}]}
        resolver = IdentityResolver([person(context=context)])
        text = "Juan Pérez, alcalde de Oaxaca, participa."
        self.assertEqual(resolver.resolve(text, metadata={"published_at": "2019-01-01"})[0]["status"], "matched")
        self.assertEqual(resolver.resolve(text, metadata={"published_at": "2026-01-01"})[0]["status"], "candidate")
        self.assertEqual(resolver.resolve(text, metadata={"query_date": "2026-01-01"})[0]["status"], "candidate")

    def test_verified_alias_expiry_is_respected(self):
        bank = banks()["verified_trade"]
        bank[0]["context"]["verified_aliases"][0]["valid_to"] = "2020-01-01"
        row = IdentityResolver(bank).resolve("LuzNogal, RFC LNO010101AB1.", metadata={"published_at": "2026-01-01"})[0]
        self.assertEqual(row["status"], "candidate")
        self.assertIn("fuera", " ".join(row["reasons"]))

    def test_generic_rfcs_are_never_identity_anchors(self):
        for rfc in ("XAXX010101000", "XEXX010101000"):
            entity = person(context=verified({"rfc": rfc}, "rfc"))
            self.assertEqual(IdentityResolver([entity]).resolve(f"Juan Pérez, RFC {rfc}.")[0]["status"], "candidate")

    def test_identifier_entries_preserve_verification_and_type_compatibility(self):
        context = {"identifiers": [{"type": "RFC", "value": "PEJJ800101AB1", "verified": True,
                                   "source": "review:identifier"}]}
        entity = person(context=context)
        self.assertEqual(IdentityResolver([entity]).resolve("Juan Pérez, RFC PEJJ800101AB1.")[0]["status"], "matched")
        context = verified({"rfc": "LNO010101AB1"}, "rfc")
        # A company-format RFC cannot corroborate a person record, even when
        # an input entry claims verification. It remains an input hint.
        self.assertEqual(IdentityResolver([person(context=context)]).resolve("Juan Pérez, RFC LNO010101AB1.")[0]["status"], "candidate")

    def test_overlapping_partial_names_compete_with_a_full_name(self):
        bank = [person("p1", "José Peña Villaluz"), person("p2", "Peña Villaluz")]
        rows = IdentityResolver(bank).resolve("José Peña Villaluz participa.")
        self.assertEqual({row["status"] for row in rows}, {"ambiguous"})

    def test_company_initials_and_generated_core_preserve_original_spans(self):
        bank = banks()["verified_trade"]
        for text in ("L.N., RFC LNO010101AB1.", "L. N., RFC LNO010101AB1."):
            row = IdentityResolver(bank).resolve(text)[0]
            self.assertEqual(row["status"], "matched")
            observed = row["mentions"][0]
            self.assertEqual(text[observed["start"]:observed["end"]], observed["text"])

    def test_id_validation_and_very_long_alias_limit(self):
        for bank in ([person(), person()], [{"name": "Juan Pérez"}], [person(name="")], [person(None)]):
            with self.assertRaises(ValueError):
                IdentityResolver(bank)
        entity = person(aliases=[" ".join("nombre" for _ in range(40))])
        resolver = IdentityResolver([entity])
        self.assertEqual(resolver.stats["skipped_long_aliases"], 1)

    def test_indexed_40000_bank_is_token_bounded(self):
        bank = (person(str(i), f"Persona Sintética {i:05d}") for i in range(40000))
        start = time.perf_counter()
        resolver = IdentityResolver(bank)
        indexed_seconds = time.perf_counter() - start
        self.assertEqual(resolver.stats["entity_count"], 40000)
        for index in (0, 99, 31000, 39999):
            rows = resolver.resolve(f"Hoy Persona Sintética {index:05d} presentó un informe.")
            self.assertEqual([row["entity_id"] for row in rows], [str(index)])
            metrics = rows[0]["features"]["lookup"]
            self.assertLessEqual(metrics["trie_steps"], metrics["tokens"] * 4)
            self.assertEqual(metrics["candidate_entities"], 1)
        self.assertGreater(indexed_seconds, 0)


def run_benchmark(destination):
    """Reproduce the CPU/index benchmark separately from network download."""
    module_digest = hashlib.sha256(Path(identity_module.__file__).read_bytes()).hexdigest()
    bank = []
    for i in range(40000):
        if i % 2:
            bank.append(company(str(i), f"Comercial Sintetica {i:05d}, S.A. de C.V.",
                {"trade_names": [f"Marca{i:05d}", f"MS{i:05d}"], "state": "Sonora"}))
        else:
            bank.append(person(str(i), f"Persona Sintetica {i:05d}", {"state": "Oaxaca", "role": "alcalde"}))
    start = time.perf_counter()
    resolver = IdentityResolver(bank)
    indexed = time.perf_counter() - start
    def body(i, long=False):
        name = f"Marca{i:05d}" if i % 2 else f"Persona Sintetica {i:05d}"
        text = f"{name} presento un informe de actividades en una sesion publica. El documento describe servicios municipales y actividades de seguimiento."
        if long:
            text += " Los participantes revisaron los procesos administrativos y el calendario de servicios. " * 20
        return text
    errors, samples, latencies = [], [], []
    start = time.perf_counter()
    for i in range(40000):
        before = time.perf_counter()
        rows = resolver.resolve(body(i))
        latencies.append(time.perf_counter() - before)
        if len(rows) != 1 or rows[0]["entity_id"] != str(i) or rows[0]["status"] != "candidate":
            errors.append(i)
        if i < 3:
            samples.append({"entity_id": rows[0]["entity_id"], "status": rows[0]["status"],
                            "lookup": rows[0]["features"]["lookup"]})
    short_seconds = time.perf_counter() - start
    long_lats = []
    for i in range(1000):
        before = time.perf_counter()
        rows = resolver.resolve(body(i, True))
        long_lats.append(time.perf_counter() - before)
        if len(rows) != 1 or rows[0]["entity_id"] != str(i):
            errors.append("long:" + str(i))
    homonyms = IdentityResolver(person(str(i), "Juan Perez") for i in range(5000))
    start = time.perf_counter()
    same = homonyms.resolve("Juan Perez participa en una sesion.")
    homonym_seconds = time.perf_counter() - start
    homonym_errors = sum(row["status"] != "ambiguous" or
        row["features"]["competing_entity_count_lower_bound"] != 4999 for row in same)
    def percentile(values, fraction):
        return sorted(values)[min(len(values) - 1, int(len(values) * fraction))] * 1000
    report = {"schema_version": 1, "model_version": MODEL_VERSION,
        "module_sha256": module_digest,
        "module_changed_during_run": module_digest != hashlib.sha256(Path(identity_module.__file__).read_bytes()).hexdigest(),
        "rules_hash": identity_module.RULES_HASH,
        "reproduction": "python tests/test_identity.py --benchmark OUTPUT_JSON",
        "environment": {"python": sys.version, "platform": platform.platform(), "cpu_logical_count": os.cpu_count()},
        "kind": "local_synthetic_identity_benchmark_no_network", "bank": resolver.stats,
        "index_seconds": indexed,
        "short_documents": {"count": 40000, "body_character_count_min": len(body(1)),
            "body_character_count_max": len(body(0)), "elapsed_seconds": short_seconds,
            "documents_per_second": 40000 / short_seconds, "p50_ms": percentile(latencies, .50),
            "p95_ms": percentile(latencies, .95), "p99_ms": percentile(latencies, .99),
            "errors": len(errors), "samples": samples},
        "long_documents": {"count": 1000, "body_character_count_min": len(body(1, True)),
            "elapsed_seconds": sum(long_lats), "p50_ms": percentile(long_lats, .50),
            "p95_ms": percentile(long_lats, .95), "p99_ms": percentile(long_lats, .99),
            "projected_40000_seconds_excludes_index": sum(long_lats) * 40},
        "homonym_stress": {"bank_count": 5000, "returned_rows": len(same),
            "elapsed_seconds": homonym_seconds, "errors": homonym_errors,
            "competitors_per_row_max": max(len(row["competing_entity_ids"]) for row in same)},
        "memory": None, "memory_qualification": "Memory usage was not measured.",
        "qualification": "Synthetic CPU/index benchmark only; no network, source coverage, download SLA or real-world identity accuracy. All optional bank fields are unverified hints."}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("index_seconds", "short_documents", "long_documents", "homonym_stress")}, indent=2))
    if errors or homonym_errors or report["module_changed_during_run"]:
        raise AssertionError("Identity benchmark discrepancies: " + str(errors[:20]))
    return report


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--benchmark":
        run_benchmark(sys.argv[2])
    else:
        unittest.main()
