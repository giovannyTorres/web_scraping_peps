"""Functional isolation/scope checks; these are not performance benchmarks."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from peps.engine import Engine
from peps.collector import SafeFetcher
from peps.news import NewsAcquirer
from peps.store import canonical, digest
from scripts.validate_live_pipeline import write_json
from scripts.validate_publisher_bridge import (
    bank_fingerprint, derived_config, hold_original_jobs, permitted_body_url,
    read_rows, validate_origin,
)
from scripts.validate_real_people import copy_bank


class PublisherBridgeGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = {'id': 'new_publisher', 'kind': 'rss', 'discovery': False,
            'automated_search': False, 'enabled': True, 'terms_approved': True,
            'robots_required': True, 'download_articles': True,
            'article_terms_approved': True, 'body_terms_approved': True,
            'url': 'https://publisher.example.org/', 'base_url': 'https://publisher.example.org/',
            'allowed_hosts': ['publisher.example.org'], 'article_allowed_hosts': ['publisher.example.org'],
            'article_path_globs': ['/news/real-article/', '/post.php'],
            'article_excluded_path_globs': ['/news/search/*'],
            'article_metadata_url_allowlist': ['https://publisher.example.org/news/real-article/',
                                               'https://publisher.example.org/post.php?id=5'],
            'min_interval_seconds': 5, 'permission_scope': 'Private technical session only',
            'review_note': 'Public original already discovered by the nominal cold search'}
        self.original = {'sources': [{'id': 'original', 'enabled': True, 'untouched': ['one']}],
                         'search': {'strategy': 'targeted', 'frozen': True}, 'user_agent': 'Existing agent'}

    def test_catalogue_is_an_exact_original_superset_without_mutating_inputs(self):
        expected = deepcopy(self.original)
        result = derived_config(self.original, {'sources': [self.source]})
        self.assertEqual(self.original, expected)
        self.assertEqual(result['sources'][:-1], expected['sources'])
        self.assertEqual({key: value for key, value in result.items() if key != 'sources'},
                         {key: value for key, value in expected.items() if key != 'sources'})
        result['sources'][0]['untouched'].append('derived-only')
        self.assertEqual(self.original, expected)

    def test_discovery_or_implicit_body_permissions_are_rejected(self):
        for change in ({'automated_search': True}, {'discovery': True}, {'terms_approved': False},
                       {'article_terms_approved': False}, {'body_terms_approved': False},
                       {'search_url': 'https://publisher.example.org/search?q={query}'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    derived_config(self.original, {'sources': [self.source | change]})

    def test_unsafe_origins_unbounded_paths_and_replaced_sources_are_rejected(self):
        for change in ({'id': 'original'}, {'allowed_hosts': ['*.example.org']},
                       {'allowed_hosts': ['127.0.0.1'], 'article_allowed_hosts': ['127.0.0.1']},
                       {'base_url': 'http://publisher.example.org/'},
                       {'url': 'https://user:password@publisher.example.org/'},
                       {'article_path_globs': ['/*']}, {'article_path_globs': ['/news/../admin']},
                       {'min_interval_seconds': 0}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    derived_config(self.original, {'sources': [self.source | change]})

    def test_exact_url_guard_distinguishes_query_articles_and_limits_redirect_paths(self):
        self.assertTrue(permitted_body_url('https://publisher.example.org/post.php?id=5', self.source))
        self.assertFalse(permitted_body_url('https://publisher.example.org/post.php?id=6', self.source))
        self.assertFalse(permitted_body_url('https://publisher.example.org/search?q=Person', self.source, exact=False))
        self.assertFalse(permitted_body_url('https://elsewhere.example.org/news/real-article/', self.source, exact=False))
        self.assertFalse(permitted_body_url('https://publisher.example.org:444/news/real-article/', self.source, exact=False))
        self.assertFalse(permitted_body_url('http://publisher.example.org/news/real-article/', self.source, exact=False))
        self.assertFalse(permitted_body_url('https://publisher.example.org/news/search/real-article/', self.source, exact=False))
        self.assertTrue(permitted_body_url('https://publisher.example.org/news/real-article/', self.source, exact=False))

    def make_bank(self):
        engine = Engine(self.directory / 'cold')
        news = NewsAcquirer(engine)
        engine.store.import_entities([{'id': 'person', 'name': 'Nombre Público Verificado', 'kind': 'person'}])
        engine.store.enqueue('news_query', {'source_id': 'old', 'url': 'https://old.example.org/search'}, key='original-query')
        engine.store.enqueue('news_article', {'source_id': 'old', 'url': 'https://old.example.org/original'}, key='original-body')
        with engine.store.connection() as con:
            con.execute("UPDATE jobs SET status='deferred',attempts=2,due=?,owner='historical-owner',error='real 429',lease_until=NULL",
                        (time.time() + 86400,))
            con.execute("UPDATE jobs SET status='queued',due=? WHERE kind='news_article'", (time.time() - 10,))
            entities = [dict(row) for row in con.execute('SELECT id,name,kind,aliases,context FROM entities ORDER BY id')]
        frozen = {'hashes': {'plan_sha256': 'unchanged-plan'}, 'config': news.config(),
                  'imported_entities_sha256': digest(canonical(entities))}
        write_json(engine.data_dir / 'frozen_inputs.json', frozen)
        report = self.directory / 'cold-result.json'
        write_json(report, {'run': {'bank_dir': str(engine.data_dir), 'completed_at': '2026-10-17T00:00:00Z'},
                            'frozen_inputs': frozen['hashes']})
        return engine, report

    def test_original_jobs_and_backoff_are_restored_even_after_failure(self):
        original, report = self.make_bank()
        before = bank_fingerprint(original.data_dir)
        target = self.directory / 'derived'
        copy_bank(original.data_dir, target)
        copied = Engine(target)
        rows = read_rows(target / 'peps.sqlite3', 'jobs')
        with self.assertRaisesRegex(RuntimeError, 'interrupted real phase'):
            with hold_original_jobs(copied.store, time.time() + 300):
                self.assertIsNone(copied.store.claim(kind_prefix='news_'))
                copied.store.enqueue('news_article', {'url': 'https://publisher.example.org/news/real-article/'}, key='new-body')
                self.assertEqual(copied.store.claim(kind_prefix='news_')['payload']['url'],
                                 'https://publisher.example.org/news/real-article/')
                raise RuntimeError('interrupted real phase')
        after = read_rows(target / 'peps.sqlite3', 'jobs')
        self.assertTrue(all(row in after for row in rows))
        self.assertEqual(bank_fingerprint(original.data_dir), before)
        self.assertTrue(copied.store.verify_audit()['ok'])
        with copied.store.connection() as con:
            actions = {row[0] for row in con.execute('SELECT action FROM audit')}
        self.assertIn('publisher_bridge_inherited_jobs_held', actions)
        self.assertIn('publisher_bridge_inherited_jobs_restored', actions)

    def test_sqlite_backup_and_raw_files_are_identical_and_source_is_unchanged(self):
        original, report = self.make_bank()
        # A persisted anti-bot wait is copied unchanged. No request is made;
        # this fixture verifies storage/isolation, not network performance.
        fetcher = SafeFetcher({'user_agent': 'WebScope functional guard fixture'}, original.data_dir, offline=True)
        fetcher.close()
        with closing(sqlite3.connect(original.data_dir / 'network.sqlite3')) as con:
            con.execute('INSERT INTO hosts(host,blocked_until,reason) VALUES(?,?,?)',
                        ('duckduckgo.com', time.time() + 86400, 'captcha'))
            con.commit()
        (original.data_dir / 'news_snapshots').mkdir()
        (original.data_dir / 'news_snapshots' / 'real-capture.bin').write_bytes(b'actual retained source bytes')
        before = bank_fingerprint(original.data_dir)
        target = self.directory / 'independent'
        copy_bank(original.data_dir, target)
        self.assertEqual(bank_fingerprint(target), before)
        self.assertEqual(bank_fingerprint(original.data_dir), before)
        self.assertEqual(read_rows(target / 'network.sqlite3', 'hosts'),
                         read_rows(original.data_dir / 'network.sqlite3', 'hosts'))
        with closing(sqlite3.connect(target / 'peps.sqlite3')) as con:
            con.execute("UPDATE entities SET name='Changed only in derived copy'")
            con.commit()
        self.assertNotEqual(bank_fingerprint(target), before)
        self.assertEqual(bank_fingerprint(original.data_dir), before)

    def test_completed_original_is_required_and_changed_names_are_rejected(self):
        engine, report = self.make_bank()
        record, result = validate_origin(engine.data_dir.resolve(), report, allow_small=True)
        with self.assertRaisesRegex(ValueError, '300'):
            validate_origin(engine.data_dir.resolve(), report)
        with engine.store.connection() as con:
            con.execute("UPDATE entities SET name='Changed name'")
        with self.assertRaisesRegex(ValueError, 'nombres'):
            validate_origin(engine.data_dir.resolve(), report, allow_small=True)

    def test_live_lease_or_unfinished_result_is_rejected_before_copy(self):
        engine, report = self.make_bank()
        value = json.loads(report.read_text(encoding='utf-8'))
        del value['run']['completed_at']
        write_json(report, value)
        with self.assertRaisesRegex(ValueError, 'informe final'):
            validate_origin(engine.data_dir.resolve(), report, allow_small=True)
        value['run']['completed_at'] = '2026-10-17T00:00:00Z'
        write_json(report, value)
        with engine.store.connection() as con:
            con.execute("UPDATE jobs SET status='running',lease_until=? WHERE kind='news_query'", (time.time() + 300,))
        with self.assertRaisesRegex(ValueError, 'trabajadores activos'):
            validate_origin(engine.data_dir.resolve(), report, allow_small=True)


if __name__ == '__main__':
    unittest.main()
