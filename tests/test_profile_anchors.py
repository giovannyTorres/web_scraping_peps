"""Source-backed profile import controls; fictitious directory records only."""
import copy
import hashlib
import importlib.util
import io
import json
import unittest

from peps.identity import IdentityResolver
from peps.profile_anchors import documented_profile, validate_documented_field


HTML = b'''<html><head><title>Colegio de Notarios de la Ciudad de M\xc3\xa9xico</title></head><body>
<div class="nombre_dir">Notaria Naira Cedro Olmos - Notar\xc3\xada No. 42</div>
<p>Direcci\xc3\xb3n: Vialidad 100. Alcald\xc3\xada: Miguel Hidalgo C.P. 11000 Tel\xc3\xa9fono: 55555555</p>
<div class="nombre_dir">Notario Otra Persona Diferente - Notar\xc3\xada No. 51</div>
<p>Alcald\xc3\xada: Cuauht\xc3\xa9moc C.P. 06000</p></body></html>'''
OBSERVED = "2020-06-03T12:00:00+00:00"
URL = "https://colegiodenotarios.org.mx/directorio"


def entity():
    return {"id": "p1", "name": "Naira Cedro Olmos", "kind": "person", "aliases": [],
            "context": {"role": "Notaria", "state": "Ciudad de México", "municipality": "Miguel Hidalgo", "notary_number": 42}}


def enrich(item=None, **changes):
    args = {"raw": HTML, "source_url": URL, "source_sha256": hashlib.sha256(HTML).hexdigest(),
            "observed_at": OBSERVED, "adapter": "cdmx_notary_html", **changes}
    return documented_profile(item or entity(), **args)


def reseal(proof):
    proof["assertion_sha256"] = hashlib.sha256(json.dumps(
        {k: v for k, v in proof.items() if k != "assertion_sha256"}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ProfileAnchorTests(unittest.TestCase):
    def test_literal_directory_fields_have_binding_and_no_human_confirmation(self):
        original = entity()
        output = enrich(original)
        self.assertEqual(original, entity())
        self.assertNotIn("verified_fields", output["context"])
        proofs = output["context"]["documented_fields"]
        self.assertEqual(set(proofs), {"role", "state", "municipality", "notary_number"})
        for key, proof in proofs.items():
            self.assertFalse(proof["human_review"])
            self.assertNotIn("review", proof)
            self.assertIsNotNone(validate_documented_field(output, key, output["context"][key], proof))
            self.assertEqual(proof["temporal_scope"], {"kind": "source_listing_at_observation_only", "observed_date": "2020-06-03"})
            self.assertNotIn("valid_from", proof)
            self.assertNotIn("valid_to", proof)
            self.assertNotIn("55555555", proof["record"]["text"])
            self.assertNotIn("Vialidad", proof["record"]["text"])

    def test_observation_day_can_corroborate_a_literal_professional_passage(self):
        result = enrich()
        text = "La notaria pública 42 de Ciudad de México, Naira Cedro Olmos, presentó su informe."
        row = IdentityResolver([result]).resolve(text, metadata={"published_at": "2020-06-03"})[0]
        self.assertEqual(row["status"], "matched")
        self.assertTrue(row["features"]["scoring_requires_human_review"])
        self.assertTrue(all(not a["provenance"]["human_review"] for a in row["features"]["supports"][0]["attributes"]))

    def test_observation_is_never_extended_to_past_future_or_unknown_publication(self):
        resolver = IdentityResolver([enrich()])
        text = "La notaria 42 de Ciudad de México, Naira Cedro Olmos, presentó su informe."
        for day in (None, "2018-01-01", "2020-06-02", "2020-06-04", "2025-01-01"):
            with self.subTest(day=day):
                row = resolver.resolve(text, metadata={"published_at": day})[0]
                self.assertEqual(row["status"], "candidate")
                self.assertTrue(any(a["date_requires_review"] for a in row["features"]["supports"][0]["attributes"]))
                self.assertEqual(row["features"]["supports"][0]["verified_groups"], [])

    def test_url_and_directory_labels_alone_do_not_create_an_anchor(self):
        item = entity()
        item["context"].update(source_url=URL, source_sha256=hashlib.sha256(HTML).hexdigest(), observed_at=OBSERVED)
        row = IdentityResolver([item]).resolve(
            "La notaria 42 de Ciudad de México, Naira Cedro Olmos, presentó su informe.", metadata={"published_at": "2020-06-03"})[0]
        self.assertEqual(row["status"], "candidate")

    def test_wrong_or_altered_capture_fails_before_any_assertion_is_created(self):
        for raw in (HTML.replace(b"42", b"43"), b"<html>unrelated</html>"):
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(ValueError):
                    enrich(raw=raw)
        with self.assertRaises(ValueError):
            enrich(source_sha256="0" * 64)

    def test_other_person_and_a_shared_name_without_unique_source_row_are_rejected(self):
        wrong = entity()
        wrong["name"] = "Naira Cedro Olmos Rivera"
        with self.assertRaises(ValueError):
            enrich(wrong)
        duplicate = HTML.replace(b"Otra Persona Diferente", b"Naira Cedro Olmos")
        with self.assertRaises(ValueError):
            enrich(raw=duplicate, source_sha256=hashlib.sha256(duplicate).hexdigest())

    def test_unrelated_hosts_paths_userinfo_and_http_are_rejected(self):
        for url in ("http://colegiodenotarios.org.mx/directorio", "https://example.com/directorio",
                    "https://colegiodenotarios.org.mx.example.com/directorio", "https://user@colegiodenotarios.org.mx/directorio",
                    "https://colegiodenotarios.org.mx/nota", URL + "#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                enrich(source_url=url)

    def test_future_and_undated_observations_are_rejected(self):
        for observed in ("2999-01-01T00:00:00+00:00", "2020-06-03", "not-a-date"):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                enrich(observed_at=observed)

    def test_changed_bank_name_id_and_field_value_invalidate_retained_assertions(self):
        output = enrich()
        proof = output["context"]["documented_fields"]["role"]
        self.assertIsNone(validate_documented_field(dict(output, id="other"), "role", "Notaria", proof))
        self.assertIsNone(validate_documented_field(dict(output, name="Otra Persona Diferente"), "role", "Notaria", proof))
        self.assertIsNone(validate_documented_field(output, "role", "Abogada", proof))

    def test_edited_spans_and_relabelled_source_record_fail_even_with_a_new_digest(self):
        output = enrich()
        original = output["context"]["documented_fields"]["role"]
        for mutate in (lambda p: p["record"]["field_span"].update(start=2),
                       lambda p: p.update(field="rfc", value="TEST000000XXX"),
                       lambda p: p["source"].update(adapter_version="future"),
                       lambda p: p["record"].update(text=p["record"]["text"].replace("No. 42", "No. 51")),
                       lambda p: p["source"]["locator"].update(raw_start=-1),
                       lambda p: p.update(human_review=True)):
            proof = copy.deepcopy(original)
            mutate(proof)
            reseal(proof)
            self.assertIsNone(validate_documented_field(output, "role", "Notaria", proof))

    def test_an_invented_appointment_interval_cannot_expand_an_observation(self):
        output = enrich()
        proof = output["context"]["documented_fields"]["role"]
        proof["temporal_scope"].update(valid_from="2010-01-01", valid_to="2030-01-01")
        reseal(proof)
        self.assertIsNone(validate_documented_field(output, "role", "Notaria", proof))
        self.assertEqual(IdentityResolver([output]).resolve(
            "Naira Cedro Olmos, notaria de Ciudad de México, participó.", metadata={"published_at": "2020-06-03"})[0]["status"], "candidate")

    def test_misaligned_hint_values_are_not_promoted(self):
        wrong = entity()
        wrong["context"].update(notary_number=51, municipality="Cuauhtémoc")
        proofs = enrich(wrong)["context"]["documented_fields"]
        self.assertNotIn("notary_number", proofs)
        self.assertNotIn("municipality", proofs)

    def test_repeated_import_is_deterministic_and_does_not_modify_the_input(self):
        item = entity()
        before = copy.deepcopy(item)
        first = enrich(item)
        self.assertEqual(enrich(item), first)
        self.assertEqual(item, before)
        self.assertEqual(enrich(first), first)

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf is an optional PDF import dependency")
    def test_pdf_responsible_office_record_does_not_infer_a_municipal_president_role(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"), NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 10 Tf 50 750 Td (Directorio de Alcald\\355as en Nuevo Le\\363n) Tj "
                         b"0 -20 Td (Presidencia Municipal de Abasolo) Tj 0 -20 Td (Responsable: C.P. Naira Cedro Olmos) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(content)
        output = io.BytesIO()
        writer.write(output)
        raw = output.getvalue()
        target = entity()
        target["context"] = {"role": "presidente municipal", "state": "Nuevo León", "municipality": "Abasolo", "office": "Presidencia municipal"}
        actual = documented_profile(target, raw=raw, source_url="https://www.nl.gob.mx/files/20261001_directorio_alcaldias.pdf",
            source_sha256=hashlib.sha256(raw).hexdigest(), observed_at=OBSERVED, adapter="nuevo_leon_municipal_pdf")
        self.assertEqual(set(actual["context"]["documented_fields"]), {"state", "municipality", "office"})
        self.assertNotIn("role", actual["context"]["documented_fields"])
        for field, proof in actual["context"]["documented_fields"].items():
            self.assertIsNotNone(validate_documented_field(actual, field, actual["context"][field], proof))


if __name__ == "__main__":
    unittest.main()
