"""A cold run imports only the exact verified negative-policy bytes."""
from contextlib import closing, redirect_stdout
import hashlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('host_policy_import_integrity',
    ROOT / 'scripts/network_validation_bridge/import_negative_host_policy.py')
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class HostPolicyImportIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        fetcher = SafeFetcher({}, self.root / 'bank')
        self.db = fetcher.db_path
        fetcher.close()
        self.registry = self.root / 'negative-policy.json'
        self.receipt = self.root / 'receipt.json'
        self.original = self.payload('original.example')
        self.registry.write_bytes(self.original)

    def payload(self, host):
        return json.dumps({'schema_version': 1, 'hosts': [{'host': host,
            'blocked_until': time.time() + 3600, 'next_at': time.time() + 3600,
            'reason': 'Synthetic negative policy fixture'}]}).encode('utf8')

    def run_import(self):
        argv = ['import-policy', '--network-db', str(self.db), '--registry', str(self.registry),
                '--registry-sha256', hashlib.sha256(self.original).hexdigest(),
                '--receipt', str(self.receipt)]
        with patch.object(sys, 'argv', argv), redirect_stdout(StringIO()):
            HELPER.main()

    def hosts(self):
        with closing(sqlite3.connect(self.db)) as con:
            return list(con.execute('SELECT host FROM hosts ORDER BY host'))

    def test_file_replacement_after_read_cannot_substitute_unverified_policy(self):
        real_read_bytes = Path.read_bytes
        reads = []
        def replace_after_read(path):
            raw = real_read_bytes(path)
            if path == self.registry:
                reads.append(path)
                path.write_bytes(self.payload('unverified.example'))
            return raw
        with patch.object(Path, 'read_bytes', replace_after_read):
            self.run_import()
        self.assertEqual(reads, [self.registry])
        self.assertEqual(self.hosts(), [('original.example',)])
        self.assertNotEqual(self.registry.read_bytes(), self.original)
        self.assertEqual(json.loads(self.receipt.read_text(encoding='utf8'))['registry_sha256'],
                         hashlib.sha256(self.original).hexdigest())

    def test_initial_hash_mismatch_imports_nothing(self):
        self.registry.write_bytes(self.payload('unverified.example'))
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.hosts(), [])
        self.assertFalse(self.receipt.exists())

    def test_backend_observations_are_not_a_cold_bank(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('INSERT INTO api_backend_observations VALUES(?,?,?,?,?,?,?,?)',
                ('https://search.example/search', '["fixture"]', 'a' * 64, time.time(),
                 'application/json', b'{}', '[]', '[]'))
            con.commit()
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.hosts(), [])
        self.assertFalse(self.receipt.exists())

    def test_error_response_bodies_are_not_a_cold_bank(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.execute('INSERT INTO http_error_diagnostics VALUES(?,?,?,?,?,?,?,?,?,?)',
                (1, '2026-01-01T00:00:00+00:00', 'https://example.org/failure', None,
                 'document', 503, 'b' * 64, 0, None, '{}'))
            con.commit()
        with self.assertRaises(ValueError):
            self.run_import()
        self.assertEqual(self.hosts(), [])
        self.assertFalse(self.receipt.exists())
