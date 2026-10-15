import tempfile
import unittest
from pathlib import Path

from peps.official import (OfficialImportError, iter_ofac_xml, iter_un_xml,
                           iter_sat69b_csv, load_official_file, official_record_document)


class OfficialAdaptersTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def file(self, text, name="fixture.xml", encoding="utf-8"):
        path = self.root / name
        path.write_text(text, encoding=encoding)
        return path

    def test_ofac_namespace_alias_and_identifiers(self):
        path = self.file('''<sdnList xmlns="urn:ofac"><publshInformation><Publish_Date>05/01/2026</Publish_Date></publshInformation>
        <sdnEntry><uid>7</uid><firstName>PERSONA</firstName><lastName>DE PRUEBA</lastName><sdnType>Individual</sdnType>
        <programList><program>TEST</program></programList><akaList><aka><firstName>P.</firstName><lastName>PRUEBA</lastName></aka></akaList>
        <idList><id><idType>RFC</idType><idNumber>DEMO001</idNumber><idCountry>Mexico</idCountry></id></idList></sdnEntry></sdnList>''')
        record = next(iter_ofac_xml(path, "non_sdn"))
        self.assertEqual(record["name"], "PERSONA DE PRUEBA")
        self.assertEqual(record["snapshot_date"], "2026-05-01")
        self.assertEqual(record["aliases"], ["P. PRUEBA"])
        self.assertEqual(record["identifiers"][0]["type"], "RFC")
        self.assertFalse(record["is_criminal_conviction"])
        self.assertFalse(record["identity_verified"])
        document = official_record_document(record, "https://sanctionslist.ofac.treas.gov/Home/ConsolidatedList", "a" * 64)
        self.assertEqual(document["official_record"]["list_name"], "non_sdn")

    def test_un_individual_and_entity(self):
        path = self.file('''<CONSOLIDATED_LIST dateGenerated="2026-05-06T23:00:00Z"><INDIVIDUALS>
        <INDIVIDUAL><DATAID>1</DATAID><FIRST_NAME>PERSONA</FIRST_NAME><SECOND_NAME>PRUEBA</SECOND_NAME><UN_LIST_TYPE>TEST</UN_LIST_TYPE><REFERENCE_NUMBER>XXi.001</REFERENCE_NUMBER><LISTED_ON>2020-01-01</LISTED_ON><INDIVIDUAL_ALIAS><ALIAS_NAME>DEMO</ALIAS_NAME></INDIVIDUAL_ALIAS></INDIVIDUAL></INDIVIDUALS>
        <ENTITIES><ENTITY><DATAID>2</DATAID><FIRST_NAME>ORGANIZACIÓN DEMO</FIRST_NAME><UN_LIST_TYPE>TEST</UN_LIST_TYPE></ENTITY></ENTITIES></CONSOLIDATED_LIST>''')
        records = list(iter_un_xml(path))
        self.assertEqual([row["kind"] for row in records], ["person", "organization"])
        self.assertEqual(records[0]["reference_number"], "XXi.001")
        self.assertEqual(records[0]["aliases"], ["DEMO"])
        self.assertTrue(load_official_file(path, "un")["complete"])

    def test_sat_preamble_semicolon_encoding_and_statuses(self):
        path = self.file('Listado público de prueba\nRFC;Nombre del Contribuyente;Situación del Contribuyente\n'
                         'DEM010101AAA;COMPAÑÍA DEMO;Presuntos\n'
                         'DEM010101AAB;DEMO DOS;Definitivos\n'
                         'DEM010101AAC;DEMO TRES;Desvirtuados\n'
                         'DEM010101AAD;DEMO CUATRO;Sentencia favorable\n'
                         'DEM010101AAE;DEMO CINCO;No documentado\n', "sat.csv", "cp1252")
        records = list(iter_sat69b_csv(path))
        self.assertEqual([r["status"] for r in records], ["presumed", "definitive", "rebutted", "favorable_judgment", "unknown"])
        self.assertEqual(records[0]["name"], "COMPAÑÍA DEMO")
        doc = official_record_document(records[2], "https://omawww.sat.gob.mx/lista.csv")
        self.assertNotIn("69-B", doc["text"])
        self.assertEqual(doc["official_record"]["status"], "rebutted")
        self.assertFalse(records[1]["is_criminal_conviction"])

    def test_sat_missing_status_is_explicit_invalid(self):
        path = self.file("RFC,Nombre\nDEM010101AAA,DEMO\n", "missing.csv")
        result = load_official_file(path, "sat69b")
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["complete"])

    def test_sat_quoted_company_commas_do_not_determine_delimiter(self):
        path = self.file('RFC;Nombre del Contribuyente;Situación del Contribuyente\n'
                         'DEM010101AAA;"DEMO, UNO, DOS, TRES, CUATRO, SOCIEDAD";Presunto\n', 'commas.csv')
        records = list(iter_sat69b_csv(path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'presumed')

    def test_xml_entities_and_unsupported_schemas_are_rejected(self):
        path = self.file('<!DOCTYPE x [<!ENTITY item "bad">]><CONSOLIDATED_LIST/>')
        self.assertEqual(load_official_file(path, "un")["status"], "invalid")
        path = self.file("<Sanctions><DistinctParties/></Sanctions>")
        self.assertEqual(load_official_file(path, "ofac")["status"], "invalid")
        self.assertEqual(load_official_file(self.root / "absent.xml", "ofac")["status"], "missing")

    def test_truncated_input_reports_partial_not_complete_refresh(self):
        path = self.file('<sdnList><sdnEntry><uid>1</uid><lastName>DEMO</lastName><sdnType>Entity</sdnType></sdnEntry><sdnEntry>')
        seen = []
        result = load_official_file(path, "ofac", seen.append)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["records"], 1)
        self.assertFalse(result["complete"])

    def test_source_url_must_be_official(self):
        record = {"source": "un", "name": "DEMO", "record_id": "1"}
        with self.assertRaises(OfficialImportError):
            official_record_document(record, "https://main.un.org.evil.example/list.xml")

    def test_large_number_of_records_streams_through_callback(self):
        path = self.file("<sdnList>" + "".join(f"<sdnEntry><uid>{i}</uid><lastName>DEMO {i}</lastName><sdnType>Entity</sdnType></sdnEntry>" for i in range(5000)) + "</sdnList>")
        result = load_official_file(path, "ofac")
        self.assertEqual(result["records"], 5000)
        self.assertTrue(result["complete"])


if __name__ == "__main__":
    unittest.main()
