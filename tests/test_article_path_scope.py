"""Collection-boundary regressions, without external requests or load claims."""
import json
import socket
import tempfile
import unittest

from peps.article_paths import (ARTICLE_PATH_SCOPE_FLAG, article_path_allowed,
                                validated_article_globs)
from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now


def source(**overrides):
    value = {'id': 'fixture', 'kind': 'rss', 'url': 'https://example.org/feed',
             'allowed_hosts': ['example.org'], 'enabled': True, 'terms_approved': True,
             'download_articles': True, 'article_terms_approved': True,
             'robots_required': True, 'min_interval_seconds': 0,
             'article_path_globs': ['/news/*'],
             'article_excluded_path_globs': ['/news/author/*', '/author/*']}
    value.update(overrides)
    return value


class PathRuleTests(unittest.TestCase):
    def test_allowed_path_and_exclusion_precedence(self):
        self.assertTrue(article_path_allowed('https://example.org/news/2026/item', source()))
        self.assertFalse(article_path_allowed('https://example.org/news/author/person', source()))
        self.assertFalse(article_path_allowed('https://example.org/author/person', source()))

    def test_query_is_not_used_for_matching(self):
        cfg = source(article_path_globs=['/post.php'], article_excluded_path_globs=[])
        self.assertTrue(article_path_allowed('https://example.org/post.php?id_nota=123', cfg))
        self.assertTrue(article_path_allowed('https://example.org/post.php?id_nota=456', cfg))
        self.assertFalse(article_path_allowed('https://example.org/author/person?path=/post.php', cfg))

    def test_missing_or_none_rules_keep_previous_behavior(self):
        for cfg in ({}, {'article_path_globs': None, 'article_excluded_path_globs': None}):
            self.assertTrue(article_path_allowed('https://example.org/anything', cfg))

    def test_bounded_patterns_fail_closed(self):
        for bad in ('/news/*', ['news/*'], ['/' + 'x'*240], ['/' + '*'*7],
                    ['/x']*33, ['//host/path'], ['/news/\\*'], ['/news/\x00*'], [None]):
            with self.subTest(value=bad):
                cfg = source(article_path_globs=bad)
                with self.assertRaises(ValueError):
                    validated_article_globs(cfg)
                self.assertFalse(article_path_allowed('https://example.org/news/item', cfg))

    def test_encoded_exclusions_and_traversal_do_not_bypass_scope(self):
        cfg = source(article_path_globs=['/*'])
        for path in ('/%61uthor/person', '/news/%61uthor/person', '/news/../author/person',
                     '/news/%2e%2e/author/person', '/news/%252e%252e/author/person', '/news/%5cauthor'):
            with self.subTest(path=path):
                self.assertFalse(article_path_allowed('https://example.org'+path, cfg))


class CapturedTransport(SafeFetcher):
    def __init__(self, root, responses):
        super().__init__({'user_agent': 'Regression fixture', 'cache_ttl_seconds': 900}, root)
        self.responses, self.calls = responses, []
        self._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]

    def _transport(self, url, headers, timeout, max_bytes, addresses):
        self.calls.append(url)
        if url.endswith('/robots.txt'):
            return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
        return self.responses[url]


class FetchBoundaryTests(unittest.TestCase):
    def test_disallowed_article_is_blocked_before_dns_or_transport(self):
        with tempfile.TemporaryDirectory() as root:
            with SafeFetcher({}, root) as fetcher:
                fetcher._dns = lambda *_a, **_k: self.fail('Disallowed path attempted DNS')
                cfg = source(**{ARTICLE_PATH_SCOPE_FLAG: True})
                result = fetcher.fetch('https://example.org/author/person', cfg)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['error_code'], 'article_path_scope')

    def test_redirect_cannot_escape_article_scope_and_robots_still_work(self):
        with tempfile.TemporaryDirectory() as root:
            url = 'https://example.org/news/item'
            with CapturedTransport(root, {url: (302, {'location': '/author/person'}, b'')}) as fetcher:
                result = fetcher.fetch(url, source(**{ARTICLE_PATH_SCOPE_FLAG: True}))
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['error_code'], 'article_path_scope')
                self.assertEqual(fetcher.calls, ['https://example.org/robots.txt', url])

    def test_old_cached_redirect_is_rechecked_against_current_path_scope(self):
        with tempfile.TemporaryDirectory() as root:
            url, target = 'https://example.org/news/item', 'https://example.org/author/person'
            responses = {url: (302, {'location': target}, b''),
                         target: (200, {'content-type': 'text/html', 'cache-control': 'max-age=900'},
                                  b'<article><p>Archived content.</p></article>')}
            with CapturedTransport(root, responses) as fetcher:
                broad = source(article_path_globs=['/*'], article_excluded_path_globs=[],
                               **{ARTICLE_PATH_SCOPE_FLAG: True})
                self.assertEqual(fetcher.fetch(url, broad)['status'], 'ok')
                calls = list(fetcher.calls)
                result = fetcher.fetch(url, source(**{ARTICLE_PATH_SCOPE_FLAG: True}))
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(fetcher.calls, calls)

    def test_discovery_fetch_is_not_subject_to_article_path_rules(self):
        with tempfile.TemporaryDirectory() as root:
            url = 'https://example.org/feed'
            with CapturedTransport(root, {url: (200, {'content-type': 'application/rss+xml'}, b'<rss/>')}) as fetcher:
                self.assertEqual(fetcher.fetch(url, source())['status'], 'ok')


class NewsBoundaryTests(unittest.TestCase):
    def test_source_selection_obeys_optional_paths(self):
        with tempfile.TemporaryDirectory() as root:
            news = NewsAcquirer(Engine(root))
            cfg = source()
            self.assertIsNotNone(news._source_for_article('https://example.org/news/item', cfg, [cfg]))
            self.assertIsNone(news._source_for_article('https://example.org/author/person', cfg, [cfg]))
            cfg.pop('article_path_globs')
            cfg.pop('article_excluded_path_globs')
            self.assertIsNotNone(news._source_for_article('https://example.org/anything', cfg, [cfg]))

    def test_stale_queued_article_outside_paths_finishes_blocked_without_fetch(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(root)
            news = NewsAcquirer(engine)
            cfg, url = source(), 'https://example.org/author/person'
            news.config_path.write_text(json.dumps({'sources': [cfg]}), encoding='utf-8')
            news._observe({'url': url, 'title': 'Reference', 'text': 'Metadata only.'},
                          cfg, cfg['url'], {'retrieved_at': now()}, None, False)
            identifier = engine.store.enqueue('news_article',
                        {'article_id': digest(url), 'source_id': cfg['id'], 'url': url}, key='old-outside-scope')
            job = engine.store.claim(kind_prefix='news_article')
            class NoFetch:
                def fetch(self, *_args):
                    raise AssertionError('Out-of-scope task attempted a fetch')
            news.execute(job, NoFetch())
            with engine.store.connection() as con:
                row = con.execute('SELECT status,error FROM jobs WHERE id=?', (identifier,)).fetchone()
                self.assertEqual(row['status'], 'blocked')
                self.assertIn('source scope', row['error'])
                self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
