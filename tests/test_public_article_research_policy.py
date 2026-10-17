"""Opt-in public research admission. Functional fixtures, no load claims."""
from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.collector import SafeFetcher
from peps.engine import Engine
from peps.news import NewsAcquirer, _BodyBudget
from peps.relevance import RelevanceIndex
from peps.store import canonical, digest, now


def profile():
    return {'id': 'research', 'kind': 'rss', 'url': 'https://policy.invalid/',
            'allowed_hosts': [], 'article_allowed_hosts': [], 'enabled': True,
            'terms_approved': True, 'download_articles': True,
            'article_terms_approved': True, 'body_terms_approved': True,
            'discovery': False, 'automated_search': False, 'robots_required': True,
            'discover_public_article_hosts': True,
            'public_research_scope': 'personal_local_research', 'min_interval_seconds': 1}


class FixtureTransport(SafeFetcher):
    """Replace only DNS/transport; production fetch controls remain active."""
    def __init__(self, root, responses=None, robots=b'User-agent: *\nAllow: /\n'):
        super().__init__({'user_agent': 'PublicResearchFixture/1', 'cache_ttl_seconds': 900}, root)
        self.responses, self.calls, self.robots = responses or {}, [], robots
        self.instant = time.time()
        self._clock = lambda: self.instant
        self._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]

    def _transport(self, url, headers, timeout, max_bytes, addresses):
        self.calls.append(url)
        self.instant += 2  # Advance fixture time; never relax the one-second policy.
        if url.endswith('/robots.txt'):
            return 200, {'content-type': 'text/plain'}, self.robots
        return self.responses[url]


class PublicArticleResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root, online=True)
        self.news = NewsAcquirer(self.engine, merge_defaults=False)
        self.store = self.engine.store
        self.store.import_entities([{'id': 'person', 'name': 'Persona Pública Ejemplo'}])
        self.origin = {'id': 'search', 'kind': 'searxng', 'url': 'https://search.example.org/search',
                       'allowed_hosts': ['search.example.org'], 'enabled': True,
                       'terms_approved': True, 'download_articles': False, 'discovery': True}
        self.policy = profile()
        self.extra = []
        self.save()
        self.index = RelevanceIndex(self.store.iter_entities())
        with self.store.connection() as con:
            self.index.bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        self.url = 'https://publisher.example.org/news/story'
        self.html = '<html><title>Persona Pública Ejemplo</title><article class="entry-content"><p>' + (
            'Persona Pública Ejemplo presenta públicamente su informe municipal. ' * 8) + '</p></article></html>'

    def save(self):
        self.news.config_path.write_text(json.dumps({'sources': [self.origin, self.policy, *self.extra],
                                                    'search': {'strategy': 'targeted'}}))

    def observe(self, url=None, *, budget=None, relevant=True, response_hash=None):
        title = 'Persona Pública Ejemplo presenta informe' if relevant else 'Información sin nombre'
        candidate = self.index.rank(title, '')
        candidate['bank_revision'] = self.index.bank_revision
        return self.news._observe({'url': url or self.url, 'title': title, 'text': ''}, self.origin,
            self.origin['url'], {'sha256': response_hash if response_hash is not None else 'a' * 64,
                                 'retrieved_at': now()}, None, candidate=candidate,
            body_budget=budget if budget is not None else _BodyBudget(4, 2))

    def job(self):
        return self.store.claim(kind_prefix='news_article')

    def execute(self, fetcher, budget=None):
        return self.news.execute(self.job(), fetcher, relevance=self.index,
                                 body_budget=budget if budget is not None else _BodyBudget(4, 2))

    def test_selector_is_read_only_exact_and_never_looks_up_dns(self):
        self.observe()
        config = self.news.config_path.read_bytes()
        with self.store.connection() as con:
            before = con.execute('SELECT count(*) FROM audit').fetchone()[0]
        with patch('socket.getaddrinfo', side_effect=AssertionError('selector attempted DNS')):
            result = self.news._source_for_article(self.url)
        self.assertEqual(result['allowed_hosts'], ['publisher.example.org'])
        self.assertEqual(result['article_path_globs'], ['/news/story'])
        self.assertNotIn('documented_api_permission', result)
        self.assertNotIn('_documented_api_scope', result)
        self.assertFalse(result['public_article_admission']['publisher_terms_individually_reviewed'])
        self.assertFalse(result['public_article_admission']['identity_verified'])
        self.assertEqual(self.news.config_path.read_bytes(), config)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM audit').fetchone()[0], before)

    def test_no_observation_other_bank_or_irrelevant_card_cannot_authorize(self):
        self.assertIsNone(self.news._source_for_article(self.url))
        self.observe(relevant=False)
        self.assertIsNone(self.news._source_for_article(self.url))
        self.observe()
        with tempfile.TemporaryDirectory() as other:
            other_engine = Engine(other, online=True)
            other_news = NewsAcquirer(other_engine, merge_defaults=False)
            other_news.config_path.write_bytes(self.news.config_path.read_bytes())
            other_engine.store.import_entities([{'id': 'person', 'name': 'Persona Pública Ejemplo'}])
            self.assertIsNone(other_news._source_for_article(self.url))

    def test_opt_in_scope_source_and_robots_are_all_required(self):
        self.observe()
        for field, value in [('discover_public_article_hosts', False), ('enabled', False),
                             ('terms_approved', False), ('robots_required', False),
                             ('public_research_scope', 'commercial'), ('kind', 'searxng'),
                             ('discovery', True), ('automated_search', True),
                             ('documented_api_permission', {'scope': 'public_json_search_api'}),
                             ('allowed_hosts', ['publisher.example.org'])]:
            old = deepcopy(self.policy)
            with self.subTest(field=field):
                self.policy[field] = value
                self.save()
                self.assertIsNone(self.news._source_for_article(self.url))
            self.policy = old
        self.save()

    def test_revoked_or_changed_discovery_and_missing_response_hash_fail_closed(self):
        for response_hash in ('', 'fixture', 'a' * 63):
            with self.subTest(response_hash=response_hash):
                self.observe('https://publisher.example.org/news/' + str(len(response_hash)), response_hash=response_hash)
                self.assertIsNone(self.news._source_for_article('https://publisher.example.org/news/' + str(len(response_hash))))
        self.observe()
        self.origin['enabled'] = False
        self.save()
        self.assertIsNone(self.news._source_for_article(self.url))
        self.origin['enabled'] = True
        self.origin['url'] = 'https://search.example.org/other-reviewed-operation'
        self.save()
        self.assertIsNone(self.news._source_for_article(self.url))

    def test_bank_import_invalidates_old_candidate_admission(self):
        self.observe()
        self.store.import_entities([{'id': 'different', 'name': 'Otra Persona Distinta'}])
        self.assertIsNone(self.news._source_for_article(self.url))

    def test_unsafe_social_media_and_non_article_paths_are_not_granted(self):
        urls = ['http://publisher.example.org/news/story', 'https://127.0.0.1/news/story',
                'https://8.8.8.8/news/story', 'https://localhost/news/story',
                'https://metadata.google.internal/news/story', 'https://u:p@publisher.example.org/news/story',
                'https://publisher.example.org:444/news/story', 'https://www.facebook.com/person/post',
                'https://publisher.example.org/author/person', 'https://publisher.example.org/tag/person',
                'https://publisher.example.org/login', 'https://publisher.example.org/api/article',
                'https://publisher.example.org/video/news', 'https://publisher.example.org/watch?v=1',
                'https://publisher.example.org/a.pdf', 'https://publisher.example.org/?q=person',
                'https://publisher.example.org/news/%2e%2e/login', 'https://publisher.example.org/news/%252e%252e/login']
        for url in urls:
            with self.subTest(url=url):
                self.observe(url)
                self.assertIsNone(self.news._source_for_article(url))

    def test_known_profile_restrictions_and_global_denies_take_precedence(self):
        fixed = {'id': 'fixed', 'kind': 'rss', 'url': 'https://publisher.example.org/feed',
                 'allowed_hosts': ['publisher.example.org'], 'enabled': True, 'terms_approved': True,
                 'download_articles': True, 'article_terms_approved': True, 'article_path_globs': ['/other/*']}
        self.extra = [fixed]
        self.save()
        self.observe()
        self.assertIsNone(self.news._source_for_article(self.url))
        fixed['article_path_globs'] = ['/news/*']
        self.save()
        self.assertEqual(self.news._source_for_article(self.url)['id'], 'fixed')
        self.policy['article_denied_hosts'] = ['example.org']
        self.save()
        self.assertIsNone(self.news._source_for_article(self.url))

    def test_disabled_default_is_distinct_from_an_explicit_operator_denial(self):
        fixed = {'id': 'fixed', 'kind': 'rss', 'url': 'https://publisher.example.org/feed',
                 'allowed_hosts': ['publisher.example.org'], 'enabled': False, 'terms_approved': False,
                 'download_articles': False, 'article_terms_approved': False}
        self.extra = [fixed]
        self.save()
        self.observe()
        self.assertIsNotNone(self.news._source_for_article(self.url))
        self.news.set_source('fixed', False, False)
        self.assertIsNone(self.news._source_for_article(self.url))

    def test_specific_redirect_cannot_reach_a_host_denied_after_queue(self):
        target = 'https://another.example.net/news/story'
        fixed = {'id': 'fixed', 'kind': 'rss', 'url': 'https://publisher.example.org/feed',
                 'allowed_hosts': ['publisher.example.org', 'another.example.net'],
                 'enabled': True, 'terms_approved': True, 'robots_required': True,
                 'download_articles': True, 'article_terms_approved': True, 'article_path_globs': ['/news/*']}
        self.extra = [fixed]
        self.save()
        self.observe()
        self.policy['article_denied_hosts'] = ['example.net']
        self.save()
        with FixtureTransport(self.root / 'network', {self.url: (302, {'location': target}, b'')}) as fetcher:
            self.execute(fetcher)
            self.assertEqual(fetcher.calls, ['https://publisher.example.org/robots.txt', self.url])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)

    def test_specific_cached_redirect_cannot_reuse_a_newly_denied_host(self):
        target = 'https://another.example.net/news/story'
        fixed = {'id': 'fixed', 'kind': 'rss', 'url': 'https://publisher.example.org/feed',
                 'allowed_hosts': ['publisher.example.org', 'another.example.net'],
                 'enabled': True, 'terms_approved': True, 'robots_required': True,
                 'download_articles': True, 'article_terms_approved': True, 'article_path_globs': ['/news/*']}
        self.extra = [fixed]
        self.save()
        self.observe()
        with FixtureTransport(self.root / 'network', {
                self.url: (302, {'location': target}, b''),
                target: (200, {'content-type': 'text/html', 'cache-control': 'max-age=900'}, self.html.encode())}) as fetcher:
            self.assertEqual(fetcher.fetch(self.url, fixed)['status'], 'ok')
            calls = list(fetcher.calls)
            self.extra.append({'id': 'deny', 'allowed_hosts': ['another.example.net'],
                               'public_article_download_denied': True})
            self.save()
            self.execute(fetcher)
            self.assertEqual(fetcher.calls, calls)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)

    def test_deny_added_after_queue_also_blocks_specific_source_without_http(self):
        fixed = {'id': 'fixed', 'kind': 'rss', 'url': 'https://publisher.example.org/feed',
                 'allowed_hosts': ['publisher.example.org'], 'enabled': True, 'terms_approved': True,
                 'download_articles': True, 'article_terms_approved': True, 'article_path_globs': ['/news/*']}
        for mode in ('global_domain_list', 'specific_host_denial'):
            with self.subTest(mode=mode):
                self.policy['article_denied_hosts'] = []
                fixed.pop('public_article_download_denied', None)
                self.extra = [fixed]
                self.save()
                self.observe(self.url + '-' + mode)
                job = self.job()
                self.assertEqual(job['payload']['source_id'], 'fixed')
                if mode == 'global_domain_list':
                    self.policy['article_denied_hosts'] = ['example.org']
                else:
                    fixed['public_article_download_denied'] = True
                self.save()
                with FixtureTransport(self.root / ('network-' + mode)) as fetcher:
                    self.news.execute(job, fetcher, relevance=self.index, body_budget=_BodyBudget(2, 2))
                    self.assertEqual(fetcher.calls, [])
                with self.store.connection() as con:
                    row = con.execute('SELECT status,error FROM jobs WHERE id=?', (job['id'],)).fetchone()
                    self.assertEqual(row['status'], 'blocked')
                    self.assertIn('explicitly denies', row['error'])
                    self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)

    def test_body_budget_is_required_at_queue_and_worker(self):
        result = self.observe(budget=_BodyBudget(0, 0))
        self.assertEqual(result['status'], 'body_budget_limited')
        self.assertIsNone(self.job())
        self.observe()
        with FixtureTransport(self.root / 'network') as fetcher:
            self.execute(fetcher, _BodyBudget(0, 0))
            self.assertEqual(fetcher.calls, [])

    def test_worker_revalidates_revocation_before_cached_or_network_read(self):
        self.observe()
        self.origin['enabled'] = False
        self.save()
        with FixtureTransport(self.root / 'network') as fetcher:
            self.execute(fetcher)
            self.assertEqual(fetcher.calls, [])

    def test_worker_preserves_snapshot_admission_and_frozen_catalogue(self):
        self.observe()
        config = self.news.config_path.read_bytes()
        with FixtureTransport(self.root / 'network', {self.url: (200, {'content-type': 'text/html'}, self.html.encode())}) as fetcher:
            result = self.execute(fetcher)
            self.assertEqual(result['body_result'], 'upgraded')
            self.assertEqual(fetcher.calls, ['https://publisher.example.org/robots.txt', self.url])
        self.assertEqual(self.news.config_path.read_bytes(), config)
        with self.store.connection() as con:
            metadata = json.loads(con.execute('SELECT metadata FROM documents').fetchone()[0])
            self.assertIn('public_article_admission', metadata)
            self.assertEqual(metadata['source_policy']['allowed_hosts'], ['publisher.example.org'])
            self.assertEqual(metadata['public_article_admission']['policy_source_hash'], digest(canonical(self.policy)))
            self.assertTrue((self.root / metadata['raw_snapshot_path']).is_file())
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='news_public_article_admission'").fetchone()[0], 1)

    def test_private_dns_and_robots_denial_still_stop_the_transport(self):
        self.observe()
        with FixtureTransport(self.root / 'network') as fetcher:
            fetcher._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
            self.execute(fetcher)
            self.assertEqual(fetcher.calls, [])
        # A new observation/body job is not needed to test the policy itself.
        effective = self.news._source_for_article(self.url)
        effective['_article_path_scope'] = True
        with FixtureTransport(self.root / 'robots', robots=b'User-agent: *\nDisallow: /\n') as fetcher:
            result = fetcher.fetch(self.url, effective)
            self.assertEqual(result['status'], 'robots_denied')
            self.assertEqual(fetcher.calls, ['https://publisher.example.org/robots.txt'])

    def test_cross_host_and_same_host_login_redirects_cannot_expand_the_grant(self):
        self.observe()
        effective = self.news._source_for_article(self.url)
        effective['_article_path_scope'] = True
        for target in ('https://www.facebook.com/person/post', 'https://other.example.org/news/story',
                       'https://publisher.example.org/login', self.url + '?q=search',
                       self.url + '?id=another-article'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                with FixtureTransport(root, {self.url: (302, {'location': target}, b'')}) as fetcher:
                    result = fetcher.fetch(self.url, effective)
                    self.assertIn(result['status'], ('blocked', 'error'))
                    self.assertEqual(fetcher.calls, ['https://publisher.example.org/robots.txt', self.url])

    def test_cached_redirect_to_another_query_cannot_reuse_a_broader_grant(self):
        self.observe()
        effective = self.news._source_for_article(self.url)
        effective['_article_path_scope'] = True
        target = self.url + '?id=other'
        with FixtureTransport(self.root / 'network', {
                self.url: (302, {'location': target}, b''),
                target: (200, {'content-type': 'text/html', 'cache-control': 'max-age=900'}, self.html.encode())}) as fetcher:
            broad = dict(effective)
            broad.pop('_public_article_exact_url')
            self.assertEqual(fetcher.fetch(self.url, broad)['status'], 'ok')
            calls = list(fetcher.calls)
            denied = fetcher.fetch(self.url, effective)
            self.assertEqual(denied['status'], 'blocked')
            self.assertEqual(fetcher.calls, calls)

    def test_paywall_and_json_body_are_terminal_without_conservation(self):
        for mime, body in [('text/html', '<script type="application/ld+json">{"isAccessibleForFree":false}</script>' + self.html),
                           ('application/json', '{"article":"public but not HTML"}')]:
            with self.subTest(mime=mime), tempfile.TemporaryDirectory() as root:
                self.observe('https://publisher.example.org/news/' + mime.split('/')[1])
                job = self.job()
                with FixtureTransport(root, {job['payload']['url']: (200, {'content-type': mime}, body.encode())}) as fetcher:
                    self.news.execute(job, fetcher, relevance=self.index, body_budget=_BodyBudget(4, 2))
                with self.store.connection() as con:
                    self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)

    def test_specific_host_approval_does_not_authorize_a_paywall_preview(self):
        fixed = {'id': 'fixed', 'kind': 'rss', 'url': 'https://publisher.example.org/feed',
                 'allowed_hosts': ['publisher.example.org'], 'enabled': True, 'terms_approved': True,
                 'download_articles': True, 'article_terms_approved': True, 'robots_required': True,
                 'min_interval_seconds': 1}
        self.extra = [fixed]
        self.save()
        self.observe()
        body = '<script type="application/ld+json">{"isAccessibleForFree":false}</script>' + self.html
        with FixtureTransport(self.root / 'network', {
                self.url: (200, {'content-type': 'text/html'}, body.encode())}) as fetcher:
            self.execute(fetcher)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
            job = con.execute("SELECT status,error FROM jobs WHERE kind='news_article'").fetchone()
            self.assertEqual(job['status'], 'blocked')
            self.assertIn('Subscription restriction', job['error'])


if __name__ == '__main__':
    unittest.main()
