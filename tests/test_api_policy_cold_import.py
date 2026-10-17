"""Offline guard/monotonicity checks for the policy-only cold importer."""
from __future__ import annotations
from copy import deepcopy
from contextlib import closing, contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher

HERE = Path(__file__).resolve().parents[1] / 'scripts/network_validation_bridge'
spec = importlib.util.spec_from_file_location('negative_api_policy_import', HERE / 'import_negative_api_policy.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class NegativeApiPolicyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fetcher = SafeFetcher({}, self.root / 'network')
        self.addCleanup(self.fetcher.close)
        self.db = self.root / 'network/network.sqlite3'
        self.facts = {'schema_version': 1, 'method': 'GET',
            'api_backend_restrictions': [{'endpoint': 'https://search.example/search', 'engine': 'qwant',
                'reason': 'Observed backend suspension; pending review', 'first_seen': 100., 'last_seen': 150.,
                'pending_review': 1, 'evidence_sha256': 'a' * 64}],
            'api_route_restrictions': [{'endpoint': 'https://search.example/search', 'requested_engines': '["mojeek"]',
                'reason': 'Observed engine mismatch; pending review', 'first_seen': 101., 'last_seen': 151.,
                'evidence_sha256': 'b' * 64}]}
        self.counter = 0

    def run_import(self, data=None, expected=None):
        self.counter += 1
        path = self.root / f'facts-{self.counter}.json'
        path.write_text(json.dumps(self.facts if data is None else data))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return helper.import_policy(self.db, path, expected or digest)

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.db)) as con:
            with con:
                yield con

    def rows(self, table):
        with self.connection() as con:
            return list(con.execute('SELECT * FROM ' + table))

    def test_real_empty_schema_import_enforced_without_dns_or_grant(self):
        result = self.run_import()
        self.assertEqual(result['input_backend_rows'], 1)
        self.assertEqual(result['input_route_rows'], 1)
        self.assertTrue(all(v == 0 for v in result['content_counts_after'].values()))
        self.fetcher._dns = lambda *_a, **_k: self.fail('Imported denial reached DNS')
        self.fetcher._transport = lambda *_a, **_k: self.fail('Imported denial reached HTTP')
        config = {'id': 'new-id', 'kind': 'searxng', 'url': 'https://search.example/search', 'robots_required': False,
                  'enabled': True, 'terms_approved': True, 'allowed_hosts': ['search.example']}
        for selected, expected in [('qwant', 'discovery_backend_restricted'), ('mojeek', 'discovery_backend_provenance_mismatch')]:
            answer = self.fetcher.fetch('https://search.example/search?q=Fictional&engines=' + selected, config)
            self.assertEqual(answer['error_code'], expected)
            self.assertEqual(answer['network_requests'], 0)

    def test_existing_host_and_negative_floors_only_strengthened(self):
        with self.connection() as con:
            con.execute('INSERT INTO hosts(host,next_at,blocked_until,reason) VALUES(?,?,?,?)',
                        ('example.org', 2000000000., 2000000010., 'Observed denial'))
            con.execute('INSERT INTO api_backend_restrictions VALUES(?,?,?,?,?,1,?)',
                        ('https://search.example/search', 'qwant', 'First evidence retained', 90., 180., 'c' * 64))
            con.execute('INSERT INTO api_route_restrictions VALUES(?,?,?,?,?,?)',
                        ('https://search.example/search', '["mojeek"]', 'First route retained', 91., 181., 'd' * 64))
        host = self.rows('hosts')
        self.run_import()
        backend = self.rows('api_backend_restrictions')[0]
        route = self.rows('api_route_restrictions')[0]
        self.assertEqual(backend[2:], ('First evidence retained', 90., 180., 1, 'c' * 64))
        self.assertEqual(route[2:], ('First route retained', 91., 181., 'd' * 64))
        self.assertEqual(self.rows('hosts'), host)

    def test_empty_import_cannot_clear_existing_restrictions(self):
        self.run_import()
        data = dict(self.facts, api_backend_restrictions=[], api_route_restrictions=[])
        self.run_import(data)
        self.assertEqual(len(self.rows('api_backend_restrictions')), 1)
        self.assertEqual(len(self.rows('api_route_restrictions')), 1)

    def test_any_prior_response_observation_refuses_cold_import(self):
        with self.connection() as con:
            con.execute('INSERT INTO api_backend_observations VALUES(?,?,?,?,?,?,?,?)',
                        ('https://search.example/search', '["qwant"]', 'e' * 64, 100., 'application/json',
                         b'{"results":[]}', '[]', '[]'))
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.rows('api_backend_restrictions'), [])
        self.assertEqual(self.rows('api_route_restrictions'), [])

    def test_extra_seed_or_permission_fields_rejected(self):
        for key, value in [('query', 'Fictional name'), ('body', 'Raw text'), ('permission', True), ('source_id', 'rotated')]:
            with self.subTest(key=key):
                data = deepcopy(self.facts)
                data['api_backend_restrictions'][0][key] = value
                with self.assertRaises(ValueError):
                    self.run_import(data)
        self.assertEqual(self.rows('api_backend_restrictions'), [])

    def test_malformed_or_relaxing_backend_facts_rejected(self):
        cases = [('pending_review', 0), ('pending_review', True), ('engine', 'Qwant'), ('engine', 'qwant,google'),
                 ('first_seen', float('nan')), ('last_seen', 0), ('evidence_sha256', 'not-hash'),
                 ('endpoint', 'https://search.example/search?q=secret'), ('endpoint', 'https://search.example:443/search'),
                 ('endpoint', 'http://search.example/search')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                data = deepcopy(self.facts)
                data['api_backend_restrictions'][0][field] = value
                with self.assertRaises(ValueError):
                    self.run_import(data)
        self.assertEqual(self.rows('api_backend_restrictions'), [])

    def test_ambiguous_route_selection_rejected(self):
        for selected in ['[]', '["Qwant"]', '["qwant", "bing"]', '["qwant","qwant"]', 'null', '[NaN]', '["bing","qwant"] ']:
            with self.subTest(selected=selected):
                data = deepcopy(self.facts)
                data['api_route_restrictions'][0]['requested_engines'] = selected
                with self.assertRaises(ValueError):
                    self.run_import(data)
        self.assertEqual(self.rows('api_route_restrictions'), [])

    def test_duplicate_facts_rejected_before_any_write(self):
        data = deepcopy(self.facts)
        data['api_backend_restrictions'].append(deepcopy(data['api_backend_restrictions'][0]))
        with self.assertRaises(ValueError):
            self.run_import(data)
        self.assertEqual(self.rows('api_backend_restrictions'), [])

    def test_wrong_input_hash_refused_before_database_mutation(self):
        with self.assertRaises(ValueError):
            self.run_import(expected='f' * 64)
        self.assertEqual(self.rows('api_backend_restrictions'), [])

    def test_path_mutation_after_single_verified_read_cannot_substitute_facts(self):
        path = self.root / 'mutable-facts.json'
        raw = json.dumps(self.facts).encode()
        path.write_bytes(raw)
        changed = deepcopy(self.facts)
        changed['api_backend_restrictions'][0]['engine'] = 'unauthenticated-substitute'
        original_read = Path.read_bytes
        reads = []
        def read_then_mutate(selected):
            value = original_read(selected)
            if selected == path:
                reads.append(selected)
                path.write_text(json.dumps(changed))
            return value
        with patch.object(Path, 'read_bytes', read_then_mutate):
            helper.import_policy(self.db, path, hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(reads), 1)
        self.assertEqual(self.rows('api_backend_restrictions')[0][1], 'qwant')


if __name__ == '__main__':
    unittest.main()
