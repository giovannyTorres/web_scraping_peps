import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peps.cli import main
from peps.store import Store


class OfficialCLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / 'runtime'

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, extra):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['peps', '--data-dir', str(self.runtime), *extra]), contextlib.redirect_stdout(output):
            main()
        return json.loads(output.getvalue())

    def test_valid_un_import_matches_alias_with_provenance(self):
        store = Store(self.runtime / 'peps.sqlite3')
        store.import_entities([{'id': 'demo', 'name': 'ALIAS DEMO', 'kind': 'organization'}])
        path = self.root / 'un.xml'
        path.write_text('<CONSOLIDATED_LIST dateGenerated="2026-05-01T00:00:00Z"><ENTITIES><ENTITY><DATAID>1</DATAID><FIRST_NAME>ORGANIZACION DEMO</FIRST_NAME><ENTITY_ALIAS><ALIAS_NAME>ALIAS DEMO</ALIAS_NAME></ENTITY_ALIAS></ENTITY></ENTITIES></CONSOLIDATED_LIST>')
        result = self.invoke(['import-official', str(path), '--source', 'un', '--source-url',
                              'https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['documents'], 1)
        self.assertEqual(result['links'], 1)
        evidence = store.evidence('demo')
        self.assertEqual(evidence[0]['metadata']['official_record']['record_id'], '1')
        self.assertEqual(len(evidence[0]['metadata']['source_snapshot_sha256']), 64)

    def test_truncated_snapshot_publishes_no_documents(self):
        path = self.root / 'bad.xml'
        path.write_text('<sdnList><sdnEntry><uid>1</uid><lastName>DEMO</lastName></sdnEntry><sdnEntry>')
        with self.assertRaises(SystemExit) as caught:
            self.invoke(['import-official', str(path), '--source', 'ofac', '--source-url',
                         'https://sanctionslist.ofac.treas.gov/Home/SdnList'])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(Store(self.runtime / 'peps.sqlite3').stats()['documents'], 0)

    def test_non_official_provenance_is_rejected(self):
        path = self.root / 'unused.xml'
        path.write_text('<CONSOLIDATED_LIST/>')
        with self.assertRaises(SystemExit) as caught:
            self.invoke(['import-official', str(path), '--source', 'un', '--source-url',
                         'https://main.un.org.evil.example/list'])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(Store(self.runtime / 'peps.sqlite3').stats()['documents'], 0)


if __name__ == '__main__':
    unittest.main()
