"""Closed reference corpus boundaries, entirely offline with synthetic names."""
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import collect_notary_reference_corpus as corpus


def approved_source(**extra):
    return {'id': 'test-public', 'kind': 'news', 'allowed_hosts': ['example.org'],
            'enabled': True, 'terms_approved': True, 'download_articles': True,
            'body_terms_approved': True, 'min_interval_seconds': 1, **extra}


class ClosedNotaryCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.text = 'Synthetic Public Notary attended an ordinary public legal conference. ' * 8
        self.html = ('<html><head><title>Public report</title>'
                     '<meta property="article:published_time" content="2020-02-03T12:00:00Z">'
                     '</head><body><article><p>' + self.text + '</p></article></body></html>')
        self.result = {'status': 'ok', 'url': 'https://example.org/story',
                       'content_type': 'text/html; charset=utf-8', 'body': self.html}

    def inputs(self, references=None):
        cohort = [{'id': 'synthetic-a', 'name': 'Synthetic Public Notary'},
                  {'id': 'synthetic-b', 'name': 'Synthetic Control Notary'}]
        references = references or [{'entity_id': 'synthetic-a', 'url': 'https://example.org/story',
                                     'title': 'Frozen reference report', 'published_at': '2020-02-03'}]
        cohort_path, references_path = self.directory / 'cohort.json', self.directory / 'references.json'
        cohort_path.write_text(json.dumps({'entities': cohort}), encoding='utf-8')
        references_path.write_text(json.dumps({'reference_articles': references}), encoding='utf-8')
        return references_path, cohort_path, hashlib.sha256(references_path.read_bytes()).hexdigest(), hashlib.sha256(cohort_path.read_bytes()).hexdigest()

    def test_frozen_input_hashes_are_checked_before_any_output_or_http(self):
        reference, cohort, _, cohort_hash = self.inputs()
        output = self.directory / 'must-not-exist'
        args = Namespace(references=reference, cohort=cohort, references_sha256='0' * 64,
                         cohort_sha256=cohort_hash, output_dir=output)
        with patch.object(corpus.BoundedFetcher, '__init__', side_effect=AssertionError('no fetcher before hash validation')):
            with self.assertRaisesRegex(ValueError, 'references SHA256'):
                corpus.collect(args)
        self.assertFalse(output.exists())

    def test_duplicate_urls_preserve_all_entity_reference_links(self):
        paths = self.inputs([
            {'entity_id': 'synthetic-a', 'url': 'https://example.org/story?utm_source=x#top'},
            {'entity_id': 'synthetic-b', 'url': 'https://EXAMPLE.org/story'}])
        cohort, references, jobs = corpus.load_inputs(*paths)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['entity_ids'], ['synthetic-a', 'synthetic-b'])
        self.assertEqual(len(jobs[0]['references']), 2)
        self.assertEqual(len(references), 2)

    def test_unknown_reference_entities_and_http_urls_fail_validation(self):
        for reference in ({'entity_id': 'missing', 'url': 'https://example.org/story'},
                          {'entity_id': 'synthetic-a', 'url': 'http://example.org/story'}):
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    corpus.load_inputs(*self.inputs([reference]))

    def test_only_explicit_article_permissions_enable_source_and_courtesy_is_clamped(self):
        source, reason = corpus.select_source('https://example.org/story', [approved_source()])
        self.assertIsNone(reason)
        self.assertEqual(source['min_interval_seconds'], 5)
        self.assertTrue(source['robots_required'])
        for changes in ({'enabled': False}, {'terms_approved': False}, {'download_articles': False},
                        {'body_terms_approved': False}, {'discovery_only': True}):
            with self.subTest(changes=changes):
                selected, reason = corpus.select_source('https://example.org/story', [approved_source(**changes)])
                self.assertIsNone(selected)
                self.assertEqual(reason, 'no_explicitly_approved_article_source')

    def test_metadata_only_domains_are_denied_initially_and_removed_from_redirect_scope(self):
        source = approved_source(article_allowed_hosts=['example.org', 'periodicocentral.mx',
                                                        'www.periodicocentral.mx', 'www.alcaldesdemexico.com',
                                                        'periodicocentral.mx.', 'periodicocentral\u3002mx'])
        for host in ('periodicocentral.mx', 'www.periodicocentral.mx', 'www.alcaldesdemexico.com'):
            selected, reason = corpus.select_source('https://' + host + '/story', [source])
            self.assertIsNone(selected)
            self.assertEqual(reason, 'metadata_only_source_policy')
        selected, _ = corpus.select_source('https://example.org/story', [source])
        self.assertEqual(selected['allowed_hosts'], ['example.org'])

    def test_transport_budget_is_reserved_atomically_before_underlying_network(self):
        with corpus.BoundedFetcher({}, self.directory / 'network', deadline=time.monotonic() + 60, max_requests=3) as fetcher:
            with patch.object(corpus.SafeFetcher, '_transport', return_value=(200, {}, b'ok')) as transport:
                def request(_):
                    try:
                        fetcher._transport('https://example.org/story', {}, 12, 2000, [])
                        return 'accepted'
                    except corpus.BudgetStop:
                        return 'budget'
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(request, range(20)))
                self.assertEqual(results.count('accepted'), 3)
                self.assertEqual(transport.call_count, 3)
                self.assertEqual(len(fetcher.events), 3)

    def test_expired_deadline_never_invokes_transport(self):
        with corpus.BoundedFetcher({}, self.directory / 'network', deadline=time.monotonic() - 1, max_requests=300) as fetcher:
            with patch.object(corpus.SafeFetcher, '_transport', side_effect=AssertionError('must not network')):
                with self.assertRaises(corpus.BudgetStop):
                    fetcher._transport('https://example.org/story', {}, 12, 2000, [])
            self.assertEqual(fetcher.events, [])

    def test_private_content_addressed_writes_are_atomic_immutable_and_deduplicated(self):
        data = b'private article body\n' * 10000
        path = self.directory / 'private' / 'body.txt'
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: corpus._write_private(path, data), range(30)))
        self.assertEqual(path.read_bytes(), data)
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(path.parent.glob('.corpus-*')))
        with self.assertRaisesRegex(ValueError, 'collision'):
            corpus._write_private(path, b'different body')
        self.assertEqual(path.read_bytes(), data)

    def test_production_extractor_and_metadata_are_hashed_without_gold_substitution(self):
        references = [{'published_at': '2020-02-03', 'title': 'Different frozen reference title'}]
        row = corpus.extract_article(self.result, approved_source(), references, self.directory)
        self.assertEqual(row['status'], 'collected')
        self.assertEqual(row['title'], 'Public report')
        self.assertEqual(row['published_at'], '2020-02-03T12:00:00+00:00')
        self.assertEqual(row['publication_date_quality'], 'matches_reference_day')
        text = Path(row['text_path']).read_bytes()
        self.assertEqual(hashlib.sha256(text).hexdigest(), row['body_sha256'])
        snapshot = gzip.decompress(Path(row['raw_snapshot_path']).read_bytes())
        self.assertEqual(snapshot, self.html.encode())
        self.assertEqual(hashlib.sha256(snapshot).hexdigest(), row['raw_snapshot_sha256'])
        self.assertNotIn(self.text, json.dumps(row))

    def test_paywall_short_body_and_non_html_are_quality_failures_without_body_files(self):
        for html, content_type, reason in (
                ('<script>{"isAccessibleForFree":false}</script>' + self.html, 'text/html', 'explicit_paywall_metadata'),
                ('<meta itemprop="isAccessibleForFree" content="false">' + self.html, 'text/html', 'explicit_paywall_metadata'),
                ('<span itemprop="isAccessibleForFree">false</span>' + self.html, 'text/html', 'explicit_paywall_metadata'),
                ('<article>Too short</article>', 'text/html', 'insufficient_editorial_body'),
                ('{"excerpt":"not an article"}', 'application/json', 'non_html_original')):
            with self.subTest(reason=reason):
                row = corpus.extract_article(dict(self.result, body=html, content_type=content_type),
                                             approved_source(), [], self.directory)
                self.assertEqual(row['status'], 'rejected_article_quality')
                self.assertEqual(row['quality_reason'], reason)
                self.assertIsNone(row['text_path'])
                self.assertIsNone(row['body_sha256'])
        self.assertFalse((self.directory / 'texts').exists())

    def test_missing_and_conflicting_observed_dates_remain_explicit(self):
        for html, status in ((self.html.replace('article:published_time', 'unrelated'), 'unknown_observed_date'),
                             (self.html, 'conflicts_reference_day')):
            row = corpus.extract_article(dict(self.result, body=html), approved_source(),
                                         [{'published_at': '2010-01-01'}], self.directory)
            self.assertEqual(row['status'], 'collected')
            self.assertEqual(row['publication_date_quality'], status)
        missing = corpus.extract_article(dict(self.result, body=self.html.replace('article:published_time', 'unrelated')),
                                         approved_source(), [{'published_at': '2010-01-01'}], self.directory)
        self.assertIsNone(missing['published_at'])

    def test_equivalent_date_offsets_and_invalid_reference_dates_are_distinguished(self):
        self.assertEqual(corpus._publication_quality('2020-01-02T05:00:00+00:00',
            [{'published_at': '2020-01-01T23:00:00-06:00'}]), 'matches_reference_day')
        self.assertEqual(corpus._publication_quality('2020-01-02T05:00:00+00:00',
            [{'published_at': 'definitely-not-a-date'}]), 'invalid_reference_date')

    def test_scheduler_retains_policy_denied_and_transport_failures_in_all_reference_totals(self):
        references = [{'entity_id': 'synthetic-a', 'url': 'https://example.org/story'},
                      {'entity_id': 'synthetic-b', 'url': 'https://www.periodicocentral.mx/story'}]
        reference, cohort, reference_hash, cohort_hash = self.inputs(references)
        source_path = self.directory / 'sources.json'
        source_path.write_text(json.dumps({'sources': [approved_source()]}))
        with tempfile.TemporaryDirectory(prefix='closed-corpus-test-', dir=corpus.ROOT / 'runtime') as temp:
            output = Path(temp) / 'isolated'
            args = Namespace(references=reference, cohort=cohort, references_sha256=reference_hash,
                             cohort_sha256=cohort_hash, sources=source_path, output_dir=output,
                             seconds=1, max_requests=10, workers=2, timeout=1)
            class RobotsDeniedFetcher:
                def __init__(self, *args, **kwargs): self.events = []
                def fetch(self, url, source):
                    self_test.assertEqual(url, 'https://example.org/story')
                    self_test.assertTrue(source['robots_required'])
                    return {'status': 'robots_denied', 'error': 'robots denied'}
                def metrics_snapshot(self): return {'network_requests': 0}
                def close(self): pass
            self_test = self
            report = corpus.collect(args, fetcher_class=RobotsDeniedFetcher)
            self.assertEqual(report['total_reference_rows'], 2)
            self.assertEqual(report['unique_reference_urls'], 2)
            self.assertEqual(report['status_counts'], {'not_collected_transport': 1, 'skipped_source_policy': 1})
            self.assertFalse(report['discovery_evaluated'])
            self.assertFalse(report['search_recall_evaluated'])
            self.assertEqual(report['transport_attempts'], 0)
            with self.assertRaises(FileExistsError):
                corpus.collect(args, fetcher_class=RobotsDeniedFetcher)


if __name__ == '__main__':
    unittest.main()
