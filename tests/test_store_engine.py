import concurrent.futures
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from peps.engine import Engine, EntityIndex, capacity
from peps.nlp import analyze
from peps.store import Store, read_entities


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name)
        self.store = self.engine.store
        self.entity = {'id': 'e1', 'name': 'Arturo Pérez Montes', 'kind': 'person', 'context': {}}
        self.store.import_entities([self.entity])

    def test_import_legacy_and_preserve_homonyms(self):
        rows = read_entities('nombre_persona_input,estado_input,alias_input\nJosé Pérez,Sonora,Pepe\nJosé Pérez,Sinaloa,JP')
        self.assertEqual(2, self.store.import_entities(rows))
        self.assertEqual(3, self.store.stats()['entities'])

    def test_seed_id_preserves_entity_identity_when_normalized_context_changes(self):
        self.store.import_entities([dict(seed_id='stable-normalized-id', name='Persona Sintética', context={'state': 'Oaxaca'})])
        self.store.import_entities([dict(seed_id='stable-normalized-id', name='Persona Sintética', context={'state': 'Sonora'})])
        self.assertEqual(2, self.store.stats()['entities'])
        self.assertEqual(self.store.get_entity('stable-normalized-id')['context']['state'], 'Sonora')

    def test_entity_import_rollback_on_invalid_row(self):
        with self.assertRaises(ValueError):
            self.store.import_entities([{'name': 'Persona válida'}, {'name': ''}])
        self.assertEqual(1, self.store.stats()['entities'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_invalid_context_and_aliases_rejected_before_write(self):
        for row in [{'name': 'Prueba', 'context': '[1]'}, {'name': 'Prueba', 'aliases': [7]}]:
            with self.assertRaises(ValueError):
                self.store.import_entities([row])
        self.assertEqual(1, self.store.stats()['entities'])

    def test_document_offsets_hash_dedup_and_review_rescore(self):
        doc = {'url': 'https://example.org/test', 'title': 'Título distinto del cuerpo',
               'text': 'Arturo Pérez Montes fue condenado por corrupción.', 'source_kind': 'official',
               'published_at': '2026-10-01', 'entity_ids': ['e1']}
        self.engine.import_documents([doc, doc])
        self.assertEqual(1, self.store.stats()['documents'])
        self.assertEqual(1, self.store.stats()['evidence'])
        self.engine.rescore()
        result = self.store.list_entities()[0]['score']
        self.assertGreater(result['score'], 0)
        self.assertEqual(0, result['validated_score'])
        evidence = self.store.evidence('e1')[0]
        self.store.review(evidence['id'], 'rejected', 'Auditor', 'Es un homónimo verificado')
        self.engine.rescore('e1')
        self.assertEqual(0, self.store.list_entities()[0]['score']['score'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_query_keyword_cannot_create_findings(self):
        self.engine.import_documents([{'url': 'https://example.org/neutral', 'title': 'corrupción fraude arresto',
            'text': 'Arturo Pérez Montes inauguró una biblioteca.', 'entity_ids': ['e1'],
            'metadata': {'query': 'Arturo Pérez Montes corrupción fraude arresto'}}])
        self.engine.rescore()
        self.assertEqual(0, self.store.list_entities()[0]['score']['score'])

    def test_parallel_claim_no_double_delivery(self):
        for i in range(200):
            self.store.enqueue('test', {'n': i})
        def drain(_):
            claimed = []
            while True:
                job = self.store.claim()
                if not job:
                    break
                claimed.append(job['id'])
                self.assertTrue(self.store.finish(job))
            return claimed
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            ids = sum(pool.map(drain, range(8)), [])
        self.assertEqual(200, len(ids))
        self.assertEqual(200, len(set(ids)))
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_expired_lease_fencing_and_deadletter(self):
        self.store.enqueue('test', {})
        first = self.store.claim(lease_seconds=-1)
        second = self.store.claim()
        self.assertNotEqual(first['owner'], second['owner'])
        self.assertFalse(self.store.finish(first))
        self.assertTrue(self.store.finish(second))

    def test_deferred_only_ready_after_due(self):
        self.store.enqueue('test', {})
        job = self.store.claim()
        self.store.finish(job, 'deferred', '429', time.time() + 3600)
        self.assertIsNone(self.store.claim())
        self.assertEqual(1, self.store.stats()['jobs']['deferred'])

    def test_host_politeness_does_not_exhaust_retries(self):
        self.store.enqueue('test', {})
        for _ in range(20):
            job = self.store.claim()
            self.assertIsNotNone(job)
            self.assertEqual(1, job['attempts'])
            self.store.finish(job, 'deferred', 'Pausa del dominio', time.time() - 1, consume_attempt=False)
        job = self.store.claim()
        self.store.finish(job)
        self.assertEqual(1, self.store.stats()['jobs']['done'])

    def test_fetch_failures_only_preserve_attempts_for_courtesy_deferrals(self):
        cases = [
            ({'status': 'error', 'request_attempted': False,
              'error': 'Host is not in the source allowlist'}, 1),
            ({'status': 'deferred', 'request_attempted': False,
              'error_code': 'host_interval'}, 0),
            ({'status': 'deferred', 'request_attempted': False,
              'error_code': 'robots_pending'}, 0),
        ]
        for index, (result, expected) in enumerate(cases):
            with self.subTest(result=result):
                job_id = self.store.enqueue('test', {}, key='failure-test:' + str(index))
                job = self.store.claim()
                self.assertEqual(job['id'], job_id)
                self.assertTrue(self.engine._handle_fetch_failure(result, job))
                with self.store.connection() as con:
                    attempts = con.execute('SELECT attempts FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
                self.assertEqual(attempts, expected)

    def test_audit_tamper_detected(self):
        with self.store.connection() as con:
            con.execute("UPDATE audit SET payload='{}' WHERE seq=1")
        self.assertFalse(self.store.verify_audit()['ok'])

    def test_document_tampering_detected(self):
        self.engine.import_documents([{'url': 'https://example.org/1', 'text': 'Arturo Pérez Montes habló.'}])
        with self.store.connection() as con:
            con.execute("UPDATE documents SET text='Texto adulterado'")
        self.assertFalse(self.store.verify_audit()['ok'])

    def test_rename_recomputes_identity(self):
        self.engine.import_documents([{'url': 'https://example.org/1', 'text': 'Arturo Pérez Montes fue condenado por corrupción.'}])
        self.store.import_entities([dict(self.entity, name='Otra Persona Mexicana')])
        self.engine.rescore()
        self.assertEqual(0, self.store.list_entities()[0]['score']['score'])

    def test_discovery_index_does_not_become_adverse_after_reanalysis(self):
        self.engine.import_documents([{'url': 'https://example.org/index', 'text': 'Arturo Pérez Montes fue condenado por corrupción.',
                                      'source_kind': 'discovery', 'metadata': {'discovery_only': True}}])
        self.engine.reanalyze()
        self.engine.rescore()
        self.assertEqual(0, self.store.list_entities()[0]['score']['score'])

    def test_backup_has_consistent_data(self):
        target = Path(self.temp.name) / 'backup' / 'copy.db'
        self.store.backup(target)
        backup = Store(target)
        self.assertEqual(backup.stats()['entities'], self.store.stats()['entities'])
        self.assertEqual(backup.verify_audit(), self.store.verify_audit())

    def test_backup_rejects_database_aliases_and_sqlite_auxiliary_files(self):
        alias = Path(self.temp.name) / 'hardlinked-bank.db'
        os.link(self.store.path, alias)
        targets = [self.store.path, alias, *(Path(str(self.store.path) + suffix) for suffix in ('-wal', '-shm', '-journal'))]
        for target in targets:
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, 'ruta distinta'):
                self.store.backup(target)
        self.assertEqual(self.store.stats()['entities'], 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_legacy_document_keeps_its_own_provenance_when_url_and_body_collide(self):
        doc = {'url': 'https://example.org/shared', 'text': 'Arturo Pérez Montes fue condenado por corrupción.',
               'source_kind': 'official'}
        official_id = self.store.add_document(doc, ['e1'], analyze)
        legacy = dict(doc, source_kind='legacy', metadata={'requires_source_revalidation': True,
                      'legacy_record': {'seed_id': 'e1', 'old_row': 'one'}})
        first_id = self.store.add_document(legacy, ['e1'], analyze)
        second_id = self.store.add_document(dict(legacy, metadata={'requires_source_revalidation': True,
            'legacy_record': {'seed_id': 'e1', 'old_row': 'two'}}), ['e1'], analyze)
        self.assertEqual(len({official_id, first_id, second_id}), 3)
        self.assertEqual(self.store.add_document(legacy, ['e1'], analyze), first_id)
        self.engine.rescore('e1')
        self.assertTrue(self.store.verify_audit()['ok'])
        sources = {item['document_id']: item for item in self.store.evidence('e1')}
        self.assertEqual(sources[official_id]['source_kind'], 'official')
        self.assertEqual(sources[first_id]['source_kind'], 'legacy')
        self.assertEqual(sources[second_id]['metadata']['legacy_record']['old_row'], 'two')

    def test_offline_never_creates_network_jobs(self):
        self.assertEqual({'status': 'offline', 'processed': 0}, self.engine.run())
        self.assertEqual(0, self.engine.queue_discovery())

    def test_discovery_idempotent_spaced_and_cached_response_accepted(self):
        self.store.import_entities([{'id': 'e2', 'name': 'Otra Persona Nacional'}])
        self.engine.set_source('gdelt', True, True)
        self.engine.queue_discovery('2026-10-15')
        self.engine.queue_discovery('2026-10-15')
        with self.store.connection() as con:
            jobs = con.execute('SELECT due FROM jobs ORDER BY due').fetchall()
        self.assertEqual(2, len(jobs))
        self.assertGreaterEqual(jobs[1][0] - jobs[0][0], 5)
        self.assertFalse(self.engine._handle_fetch_failure({'status': 'not_modified', 'body': '{}'}, {}))

    def test_index_full_alias_and_unicode_boundaries(self):
        index = EntityIndex([self.entity, {'id': 'org', 'name': 'Instituto Público Nacional', 'aliases': ['IPN']}])
        self.assertEqual(['e1', 'org'], index.match('ARTURO PEREZ MONTES visitó el IPN.'))
        self.assertEqual([], index.match('Arturo Pérez montesinos visitó XIPNX'))

    def test_capacity_labels_estimate(self):
        model = capacity()
        self.assertEqual(80000, model['discovery_requests'])
        self.assertIn('no SLA', model['qualification'])


if __name__ == '__main__':
    unittest.main()
