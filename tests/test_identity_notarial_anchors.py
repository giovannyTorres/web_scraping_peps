"""Office corroboration regressions, with fictitious names and sources.

These are functional controls, not acquisition/performance benchmark data.
"""
import copy
import hashlib
import unittest

from peps.identity import IdentityResolver


def person(identity="p1", number="42", verified=True, **context):
    values = {"role": "Notario", "state": "Querétaro", "notary_number": number, **context}
    if verified:
        values["verified_fields"] = {key: {"verified": True, "source": "fixture:professional-directory-row-5"}
                                     for key in ("role", "state", "notary_number")}
    return {"id": identity, "name": "Naira Cedro Olmos", "kind": "person", "aliases": [], "context": values}


class NotarialAnchorTests(unittest.TestCase):
    def test_public_notary_nominal_title_links_the_existing_verified_role(self):
        text = "La notaria pública Naira Cedro Olmos, de Querétaro, presentó su informe."
        row = IdentityResolver([person()]).resolve(text)[0]
        self.assertEqual(row["status"], "matched")
        self.assertTrue(row["mentions"][0]["identity_supported"])
        self.assertEqual(row["features"]["supports"][0]["verified_groups"], ["geography", "role"])

    def test_notary_number_is_preserved_as_a_literal_bounded_attribute(self):
        text = "El notario público número 42 de Querétaro, Naira Cedro Olmos, presentó su informe."
        row = IdentityResolver([person()]).resolve(text)[0]
        support = row["features"]["supports"][0]
        self.assertEqual(row["status"], "matched")
        self.assertEqual(support["matched_notary_numbers"], ["42"])
        registry = [a for a in support["attributes"] if a["group"] == "professional_registry"]
        self.assertEqual(len(registry), 1)
        self.assertEqual(text[registry[0]["start"]:registry[0]["end"]], "42")
        self.assertEqual(registry[0]["provenance"], "fixture:professional-directory-row-5")
        self.assertNotIn("professional_registry", support["verified_groups"])

    def test_office_number_discriminates_verified_same_name_same_state_homonyms(self):
        bank = [person("right", "42"), person("other", "51")]
        text = "El notario público número 42 de Querétaro, Naira Cedro Olmos, presentó su informe."
        rows = {r["entity_id"]: r for r in IdentityResolver(bank).resolve(text)}
        self.assertEqual(rows["right"]["status"], "matched")
        self.assertEqual(rows["other"]["status"], "rejected")
        self.assertEqual(rows["other"]["features"]["supports"][0]["conflicting_notary_numbers"], ["42"])

    def test_same_office_and_plaza_do_not_resolve_duplicate_holders(self):
        rows = IdentityResolver([person("one"), person("two")]).resolve(
            "El notario 42 de Querétaro, Naira Cedro Olmos, presentó su informe.")
        self.assertEqual({r["status"] for r in rows}, {"ambiguous"})
        self.assertFalse(any(m["identity_supported"] for r in rows for m in r["mentions"]))

    def test_number_and_place_hints_are_never_promoted_to_verified_context(self):
        row = IdentityResolver([person(verified=False)]).resolve(
            "El notario público número 42 de Querétaro, Naira Cedro Olmos, presentó su informe.")[0]
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["features"]["supports"][0]["verified_groups"], [])
        self.assertEqual(row["features"]["supports"][0]["matched_notary_numbers"], ["42"])

    def test_verified_number_never_replaces_the_second_identity_context_group(self):
        for trusted in [("role", "notary_number"), ("notary_number",), ("state", "notary_number")]:
            with self.subTest(trusted=trusted):
                entity = person(verified=False)
                entity["context"]["verified_fields"] = {
                    key: {"verified": True, "source": "fixture:directory-row-5"} for key in trusted}
                row = IdentityResolver([entity]).resolve(
                    "El notario 42 de Querétaro, Naira Cedro Olmos, presentó su informe.")[0]
                self.assertEqual(row["status"], "candidate")

    def test_verified_legacy_number_plaza_field_retains_its_actual_location(self):
        entity = person(number="42 Guadalajara", state="Jalisco")
        row = IdentityResolver([entity]).resolve(
            "El notario público número 42 del municipio de Guadalajara, Naira Cedro Olmos, presentó su informe.")[0]
        self.assertEqual(row["status"], "matched")
        places = [a for a in row["features"]["supports"][0]["attributes"] if a["key"] == "notary_number.jurisdiction"]
        self.assertEqual(places[0]["text"], "Guadalajara")
        self.assertTrue(places[0]["verified"])

    def test_same_number_in_another_plaza_does_not_corroborate_a_homonym(self):
        bank = [person("one", "42 Guadalajara", state="Jalisco"),
                person("two", "42 Zapopan", state="Jalisco")]
        rows = {r["entity_id"]: r for r in IdentityResolver(bank).resolve(
            "La notaria 42 de Guadalajara, Naira Cedro Olmos, presentó su informe.")}
        self.assertEqual(rows["one"]["status"], "matched")
        self.assertNotEqual(rows["two"]["status"], "matched")

    def test_predicates_other_recipients_and_negations_cannot_donate_office_anchors(self):
        resolver = IdentityResolver([person()])
        for text in (
            "Naira Cedro Olmos no es notaria pública número 42 de Querétaro.",
            "Naira Cedro Olmos nunca fue titular de la notaría 42 de Querétaro.",
            "Naira Cedro Olmos informó que la notaria pública número 42 de Querétaro es Otra Persona.",
            "Naira Cedro Olmos denunció al notario público número 42 de Querétaro.",
            "Naira Cedro Olmos visitó la notaría 42 de Querétaro.",
            "Naira Cedro Olmos con la notaria pública número 42 de Querétaro presentó su informe.",
            "No se trata de Naira Cedro Olmos, notaria pública número 42 de Querétaro.",
        ):
            with self.subTest(text=text):
                self.assertEqual(resolver.resolve(text)[0]["status"], "candidate")

    def test_bare_numbers_and_other_professional_numbers_do_not_create_notarial_attributes(self):
        resolver = IdentityResolver([person()])
        for text in ("Naira Cedro Olmos tiene 42 años y vive en Querétaro.",
                     "Naira Cedro Olmos tiene cédula profesional 42 en Querétaro.",
                     "Naira Cedro Olmos presentó el expediente 42 en Querétaro."):
            with self.subTest(text=text):
                row = resolver.resolve(text)[0]
                self.assertEqual(row["status"], "candidate")
                self.assertEqual(row["features"]["supports"][0]["matched_notary_numbers"], [])

    def test_unverified_contradiction_is_reported_without_rejecting_the_person(self):
        entity = person()
        entity["context"]["verified_fields"].pop("notary_number")
        row = IdentityResolver([entity]).resolve(
            "Naira Cedro Olmos, notaria pública 51 de Querétaro, presentó su informe.")[0]
        self.assertEqual(row["status"], "candidate")
        self.assertTrue(row["features"]["supports"][0]["conflicting_notary_numbers"])
        self.assertFalse(row["features"]["supports"][0]["verified_notary_conflict"])

    def test_dated_appointments_do_not_make_a_successor_match_historical_news(self):
        bank = []
        for identity, begin, end in (("past", "2010-01-01", "2015-12-31"),
                                     ("later", "2020-01-01", "2025-12-31")):
            entity = person(identity)
            for key in ("role", "notary_number"):
                entity["context"][key] = {"value": entity["context"][key], "verified": True,
                    "source": "fixture:dated-appointment", "valid_from": begin, "valid_to": end}
            bank.append(entity)
        text = "La notaria 42 de Querétaro, Naira Cedro Olmos, presentó su informe."
        resolver = IdentityResolver(bank)
        rows = {r["entity_id"]: r for r in resolver.resolve(text, metadata={"published_at": "2012-06-01"})}
        self.assertEqual(rows["past"]["status"], "matched")
        self.assertNotEqual(rows["later"]["status"], "matched")
        self.assertNotEqual(resolver.resolve(text)[0]["status"], "matched")

    def test_one_mention_cannot_donate_its_number_to_another_mention(self):
        row = IdentityResolver([person()]).resolve(
            "Naira Cedro Olmos, notaria pública 42 de Querétaro, presentó su informe. "
            "Naira Cedro Olmos, notaria pública 51 de Querétaro, fue entrevistada.")[0]
        self.assertEqual(row["status"], "matched")
        self.assertEqual([m["identity_supported"] for m in row["mentions"]], [True, False])

    def test_resolver_keeps_source_offsets_hash_and_inputs_unchanged(self):
        entity = person(number={"number": "0042", "jurisdiction": "Guadalajara", "verified": True,
                                "source": "fixture:directory", "valid_from": "2020-01-01"})
        before = copy.deepcopy(entity)
        text = "📰 La notaria pública 0042 de Guadalajara, Naira Cedro Olmos, presentó su informe."
        row = IdentityResolver([entity]).resolve(text, metadata={"published_at": "2024-01-01"})[0]
        self.assertEqual(row["status"], "matched")
        self.assertEqual(row["content_hash"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(entity, before)
        for a in row["features"]["supports"][0]["attributes"]:
            self.assertEqual(text[a["start"]:a["end"]], a["text"])
        self.assertEqual(row["features"]["supports"][0]["matched_notary_numbers"], ["42"])


if __name__ == "__main__":
    unittest.main()
