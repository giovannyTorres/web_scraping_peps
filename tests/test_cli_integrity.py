import contextlib
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from peps.cli import export_jsonl, import_legacy, main
from peps.engine import Engine


class CLIIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root / 'runtime')
        self.engine.store.import_entities([
            {'id': 'first', 'name': 'Persona Ficticia Gemela', 'context': {'city': 'León'}},
            {'id': 'second', 'name': 'Persona Ficticia Gemela', 'context': {'city': 'Toluca'}},
            {'id': 'unique', 'name': 'Persona Ficticia Única'},
        ])

    def legacy(self, rows):
        output = io.StringIO(newline='')
        writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(key for row in rows for key in row)))
        writer.writeheader()
        writer.writerows(rows)
        output.seek(0)
        return import_legacy(self.engine, output)

    def test_legacy_uses_seed_id_and_preserves_original_evidence(self):
        row = {'seed_id': 'second', 'nombre_persona_input': 'Persona Ficticia Gemela',
               'evidence_snippet': 'Fragmento histórico de una persona ficticia.',
               'evidence_summary': 'Resumen alternativo.', 'evidence_date': '2026-01-03',
               'retrieved_at': '2026-01-04T10:00:00+00:00', 'raw_file_sha256': 'a' * 64,
               'source_url': 'https://example.org/copia', 'source': 'fixture'}
        result = self.legacy([row])
        self.assertEqual(result['documents'], 1)
        self.assertEqual(self.engine.store.evidence('first'), [])
        evidence = self.engine.store.evidence('second')[0]
        self.assertEqual(evidence['text'], row['evidence_snippet'])
        self.assertEqual(evidence['url'], row['source_url'])
        self.assertEqual(evidence['published_at'], row['evidence_date'])
        self.assertEqual(evidence['retrieved_at'], row['retrieved_at'])
        self.assertEqual(evidence['metadata']['legacy_record'], row)
        self.assertTrue(evidence['metadata']['requires_source_revalidation'])
        self.assertEqual(evidence['source_kind'], 'legacy')
        self.engine.rescore('second')
        self.assertEqual(self.engine.store.get_score('second')['score'], 0)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_legacy_never_assigns_an_ambiguous_or_conflicting_name(self):
        result = self.legacy([
            {'nombre_persona_input': 'Persona Ficticia Gemela', 'evidence_summary': 'Sin ID.'},
            {'seed_id': 'first', 'nombre_persona_input': 'Persona Ficticia Única', 'evidence_summary': 'ID en conflicto.'},
            {'seed_id': 'old-id', 'nombre_persona_input': 'Persona Ficticia Única', 'evidence_summary': 'Único.'},
            {'nombre_persona_input': 'Ausente', 'evidence_summary': 'No existe.'},
            {'seed_id': 'first', 'evidence_summary': '   '},
        ])
        self.assertEqual(result['documents'], 1)
        self.assertEqual(result['legacy_skipped'], {
            'ambiguous_name': 1, 'identity_conflict': 1, 'unknown_entity': 1, 'empty_text': 1})
        self.assertEqual(self.engine.store.evidence('first'), [])
        self.assertEqual(self.engine.store.evidence('second'), [])
        self.assertEqual(len(self.engine.store.evidence('unique')), 1)

    def test_legacy_cannot_inherit_an_existing_official_document(self):
        text = 'Persona Ficticia Gemela fue acusada de fraude.'
        url = 'https://example.org/compartido'
        self.engine.import_documents([{'url': url, 'text': text, 'source_kind': 'official',
                                      'entity_ids': ['first']}])
        self.legacy([{'seed_id': 'second', 'nombre_persona_input': 'Persona Ficticia Gemela',
                      'evidence_url': url, 'evidence_snippet': text}])
        self.assertEqual(self.engine.store.evidence('first')[0]['source_kind'], 'official')
        evidence = self.engine.store.evidence('second')[0]
        self.assertEqual(evidence['source_kind'], 'legacy')
        self.assertTrue(evidence['metadata']['requires_source_revalidation'])
        self.engine.rescore('second')
        self.assertEqual(self.engine.store.get_score('second')['score'], 0)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_legacy_rows_with_same_url_and_text_keep_distinct_provenance(self):
        rows = [{'seed_id': entity_id, 'nombre_persona_input': 'Persona Ficticia Gemela',
                 'evidence_url': 'https://example.org/compartido',
                 'evidence_snippet': 'La misma nota histórica.', 'raw_file_sha256': marker * 64}
                for entity_id, marker in [('first', 'a'), ('second', 'b')]]
        self.legacy(rows)
        for row in rows:
            evidence = self.engine.store.evidence(row['seed_id'])[0]
            self.assertEqual(evidence['url'], row['evidence_url'])
            self.assertEqual(evidence['metadata']['legacy_record'], row)
        self.assertEqual(self.engine.store.stats()['documents'], 2)
        self.legacy(rows)
        self.assertEqual(self.engine.store.stats()['documents'], 2)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_export_never_overwrites_database_or_live_auxiliary_files(self):
        database = self.engine.store.path
        original = database.read_bytes()
        for target in [database, *(Path(str(database) + suffix) for suffix in ('-wal', '-shm', '-journal'))]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                export_jsonl(self.engine.store, target)
        self.assertEqual(database.read_bytes(), original)
        self.assertEqual(self.engine.store.stats()['entities'], 3)

    def test_export_rejects_hardlink_to_database(self):
        alias = self.root / 'database-link'
        os.link(self.engine.store.path, alias)
        with self.assertRaises(ValueError):
            export_jsonl(self.engine.store, alias)
        self.assertEqual(self.engine.store.stats()['entities'], 3)

    def test_failed_export_preserves_previous_file_and_removes_staging(self):
        target = self.root / 'bank.jsonl'
        target.write_text('previous complete export\n', encoding='utf-8')

        def interrupted():
            yield {'record_type': 'entity', 'id': 'partial'}
            raise OSError('Simulated disk failure')

        with patch.object(self.engine.store, 'export', interrupted), self.assertRaises(OSError):
            export_jsonl(self.engine.store, target)
        self.assertEqual(target.read_text(encoding='utf-8'), 'previous complete export\n')
        self.assertEqual(list(self.root.glob('.bank.jsonl-*.tmp')), [])
        result = export_jsonl(self.engine.store, target)
        self.assertEqual(result['exported'], str(target.resolve()))
        self.assertEqual(len([json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]), 3)

    def test_audit_failure_has_nonzero_exit_for_automation(self):
        with self.engine.store.connection() as con:
            con.execute("UPDATE audit SET payload='{}' WHERE seq=(SELECT min(seq) FROM audit)")
        output = io.StringIO()
        with patch.object(sys, 'argv', ['peps', '--data-dir', str(self.engine.data_dir), 'audit']), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 2)
        self.assertFalse(json.loads(output.getvalue())['ok'])


if __name__ == '__main__':
    unittest.main()
