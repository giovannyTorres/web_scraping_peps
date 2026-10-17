"""Functional runner guards and genuine local API integration, not a benchmark."""
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from scripts.validate_recovery_pipeline import (
    assert_new_paths, database_fingerprints, export_actual_api,
    file_hash, import_negative_host_policy, runtime_lock, source_hashes,
    successful_searched_people, validate_launch, write_new_json,
)


class RecoveryRunnerGuardsTests(unittest.TestCase):
    def test_existing_empty_bank_is_rejected_without_altering_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank = root / 'previous-bank'
            bank.mkdir()
            args = Namespace(data_dir=bank, closed_bank=root / 'closed', roster=root / 'roster',
                sources_config=root / 'sources', protocol_lock=root / 'protocol', utility_lock=None,
                roster_sanitization_lock=None, census_policy_lock=None, host_policy_registry=None,
                host_policy_importer=None, output=root / 'result', close_receipt=root / 'receipt',
                api_export=root / 'api', launch_lock=root / 'launch')
            with self.assertRaisesRegex(ValueError, 'rutas nuevas'):
                assert_new_paths(args)
            self.assertEqual(list(bank.iterdir()), [])
            self.assertFalse(args.closed_bank.exists())

    def test_live_launch_needs_explicit_switch_before_reading_or_writing_paths(self):
        with self.assertRaisesRegex(ValueError, '--root-go'):
            validate_launch(Namespace(online=True, root_go=False))

    def test_receipt_cannot_overwrite_a_failed_earlier_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'receipt.json'
            write_new_json(path, {'passed': False})
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                write_new_json(path, {'passed': True})
            self.assertEqual(path.read_bytes(), original)

    def test_success_count_requires_nominal_200_and_durable_parsed_receipt(self):
        def query(identifier, url, *, done=True, receipt=True):
            return {'entity_ids': [identifier], 'query_url': url, 'status': 'done' if done else 'queued',
                    'discovery_receipt_saved': receipt}
        good, challenged, unparsable = ['https://fixture.invalid/search?q=' + value for value in ('good', 'challenge', 'unparsed')]
        report = {'session_http_events': [
            {'url': good, 'parent_fetch_url': good, 'http_status': 200, 'robots_request': False},
            {'url': challenged, 'parent_fetch_url': challenged, 'http_status': 429, 'robots_request': False},
            {'url': 'https://fixture.invalid/robots.txt', 'parent_fetch_url': challenged, 'http_status': 200, 'robots_request': True},
            {'url': unparsable, 'parent_fetch_url': unparsable, 'http_status': 200, 'robots_request': False}],
            'query_results': [query('real-success', good), query('blocked', challenged),
                              query('not-parsed', unparsable, receipt=False)]}
        self.assertEqual(successful_searched_people(report), {'real-success'})

    def test_runtime_freeze_binds_required_versions_and_static_package_files(self):
        import importlib.metadata
        try:
            import trafilatura  # noqa: F401
        except ImportError:
            self.skipTest('Extraction dependency absent; online recovery runner will reject this environment.')
        first, second = runtime_lock(), runtime_lock()
        self.assertEqual(first, second)
        self.assertEqual(first['required_extraction_pins']['trafilatura'], first['trafilatura_version'])
        self.assertEqual(first['required_extraction_pins']['lxml'], first['lxml_version'])
        packages = first['installed_distributions']
        self.assertEqual(len(packages), len({(p['name'], p['version'], p['installation_root']) for p in packages}))
        self.assertTrue(all(len(p['static_file_manifest_sha256']) == 64 for p in packages))
        version = importlib.metadata.version
        with patch('scripts.validate_recovery_pipeline.importlib.metadata.version',
                   side_effect=lambda name: '0.0.0' if name == 'trafilatura' else version(name)):
            with self.assertRaisesRegex(ValueError, 'Versión distinta.*trafilatura'):
                runtime_lock()

    def test_source_freeze_includes_extraction_lock_build_hook_and_packaging(self):
        hashes = source_hashes()
        for path in ('pyproject.toml', 'requirements-extraction.lock.txt', 'scripts/build_windows.ps1',
                     'scripts/pyinstaller_hooks/hook-trafilatura.py'):
            with self.subTest(path=path):
                self.assertEqual(hashes[path], file_hash(Path(__file__).resolve().parents[1] / path))

    def test_changed_policy_helper_is_rejected_before_network_initialization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = Engine(root / 'bank', online=False)
            registry, helper = root / 'registry.json', root / 'helper.py'
            registry.write_text('{"schema_version":1,"hosts":[]}')
            helper.write_text('raise AssertionError("Must never run")')
            frozen = {'registry_path': str(registry), 'registry_sha256': file_hash(registry),
                      'importer_path': str(helper), 'importer_sha256': '0' * 64}
            with self.assertRaisesRegex(ValueError, 'cambiaron'):
                import_negative_host_policy(engine, {}, root / 'attempt', frozen)
            self.assertFalse((engine.data_dir / 'network.sqlite3').exists())

    def test_helper_zero_content_claim_does_not_override_actual_database_rows(self):
        # Deliberately dishonest local helper fixture; no public transport.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            engine = Engine(root / 'bank', online=False)
            NewsAcquirer(engine, merge_defaults=False)
            registry, helper = root / 'registry.json', root / 'helper.py'
            registry.write_text('{"schema_version":1,"hosts":[]}')
            helper.write_text('''import argparse,json,sqlite3
p=argparse.ArgumentParser()
for name in ('network-db','registry','registry-sha256','receipt'):p.add_argument('--'+name)
a=p.parse_args()
with sqlite3.connect(a.network_db) as c:
 c.execute("INSERT INTO request_history(occurred_at,url,request_kind,network_requests,wire_bytes,decoded_bytes,elapsed_seconds,http_status,outcome) VALUES('fixture','https://fixture.invalid/','fixture',1,0,0,0,200,'fixture')")
with open(a.receipt,'x') as f:json.dump({'registry_sha256':a.registry_sha256,'network_requests':0,'content_counts_remain_zero':True,'existing_restrictions_only_strengthened':True},f)
''')
            frozen = {'registry_path': str(registry), 'registry_sha256': file_hash(registry),
                      'importer_path': str(helper), 'importer_sha256': file_hash(helper)}
            attempt = root / 'attempt'
            attempt.mkdir()
            with self.assertRaisesRegex(ValueError, 'tablas de contenido vacías'):
                import_negative_host_policy(engine, {}, attempt, frozen)
            self.assertTrue((attempt / 'network-policy-initial-empty-receipt.json').exists())


class RecoveryRunnerActualLocalAPITests(unittest.TestCase):
    def test_batch_export_preserves_all_payloads_and_exact_http_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            engine = Engine(source, online=False)
            NewsAcquirer(engine, merge_defaults=False)
            engine.store.import_entities([{'id': f'fictional-{i}', 'name': f'Persona Sintética {i}',
                                          'kind': 'person'} for i in range(5)])
            engine.rescore()
            original = database_fingerprints(source / 'peps.sqlite3')
            individual = export_actual_api(source, root / 'individual', root / 'individual.json', workers=1)
            batch = export_actual_api(source, root / 'batch', root / 'batch.json', workers=1, batch_size=2)
            self.assertTrue(batch['passed'], batch['checks'])
            self.assertEqual(batch['responses'], individual['responses'])
            self.assertEqual(batch['actual_http_requests'], 3)
            self.assertEqual(batch['actual_explanation_payloads'], 5)
            self.assertNotIn('one_actual_get_per_scored_entity', batch['checks'])
            self.assertEqual(database_fingerprints(source / 'peps.sqlite3'), original)
            captured = []
            for event in batch['http_events']:
                self.assertEqual(event['method'], 'POST')
                self.assertEqual(event['http_status'], 200)
                raw = Path(event['capture_path']).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), event['response_sha256'])
                captured.extend(json.loads(raw)['items'])
                self.assertEqual(len(json.loads(raw)['items']), event['payload_count'])
            self.assertEqual(sorted(captured, key=lambda row: row['entity']['id']), batch['responses'])

    def test_export_preserves_real_persisted_inputs_and_captures_production_cards(self):
        # This is an isolated fictitious functional fixture, never online
        # performance, retrieval quality or part of the real 300-person run.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, closed = root / 'source', root / 'closed'
            engine = Engine(source, online=False)
            NewsAcquirer(engine, merge_defaults=False)
            engine.store.import_entities([{'id': 'fictional-runner', 'name': 'Aurora Sintética del Valle',
                                          'kind': 'person', 'aliases': [], 'context': {}}])
            engine.import_documents([{'url': 'https://fixture.invalid/fictitious', 'title': 'Ficción funcional',
                'source_kind': 'news', 'entity_ids': ['fictional-runner'],
                'text': 'Aurora Sintética del Valle fue acusada de fraude.', 'published_at': '2026-01-15'}])
            engine.rescore()
            before = database_fingerprints(source / 'peps.sqlite3')
            catalogue = (source / 'news_sources.json').read_bytes()
            result = export_actual_api(source, closed, root / 'api-private.json', workers=1, timeout=20)
            self.assertTrue(result['passed'], result['checks'])
            self.assertEqual(result['bank_sha256'], file_hash(closed / 'peps.sqlite3'))
            self.assertEqual(database_fingerprints(source / 'peps.sqlite3'), before)
            self.assertEqual((source / 'news_sources.json').read_bytes(), catalogue)
            self.assertEqual(len(result['responses']), 1)
            payload = result['responses'][0]
            self.assertEqual(payload['entity']['id'], 'fictional-runner')
            self.assertGreater(payload['document_review']['total_leads'], 0)
            self.assertEqual(payload['document_review']['identity_confirmations_created'], 0)
            self.assertEqual(result['closed_table_fingerprints']['score_runs'], before['score_runs'])
            event = result['http_events'][0]
            self.assertEqual(event['method'], 'GET')
            self.assertEqual(event['http_status'], 200)
            raw = Path(event['capture_path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), event['response_sha256'])
            self.assertEqual(json.loads(raw), payload)
            self.assertNotIn('token', json.dumps(event).lower())


if __name__ == '__main__':
    unittest.main()
