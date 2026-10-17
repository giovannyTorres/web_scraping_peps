"""Archive metadata and budget controls; fictional fixtures, no external HTTP."""
from copy import deepcopy
import json
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from peps.archive_text_search import ENDPOINT, SessionBudget, SESSION_LIMIT
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import build_search_plan
from peps.public_search import parse_public_results, public_search_url, PublicSearchParseError


SOURCE = {'id': 'archive', 'kind': 'arquivo_fulltext', 'url': ENDPOINT,
          'enabled': True, 'terms_approved': True, 'allowed_hosts': ['arquivo.pt'],
          'robots_required': True, 'discovery_only': True, 'download_articles': False,
          'min_interval_seconds': 5, 'max_records': 50}
NAME = 'Persona Pública de Ejemplo'


def item(url='https://publisher.example/story', **changes):
    return dict({'originalURL': url, 'title': 'Un hecho concreto',
                 'snippet': '<em>Persona Pública</em> de Ejemplo &amp; contexto.',
                 'tstamp': '20200102030405', 'digest': 'SHA1-archive-digest',
                 'mimeType': 'text/html', 'collection': 'fixture',
                 'linkToExtractedText': 'https://arquivo.pt/textextracted?never=request',
                 'linkToArchive': 'https://arquivo.pt/wayback/never-request'}, **changes)


def response(items):
    return {'status': 'ok', 'url': public_search_url(NAME, SOURCE),
            'body': json.dumps({'request_parameters': {'q': NAME}, 'response_items': items})}


class ArchiveDiscoveryTests(unittest.TestCase):
    def test_query_uses_only_documented_metadata_parameters_without_publication_dates(self):
        query = parse_qs(urlsplit(public_search_url(NAME, SOURCE)).query)
        self.assertEqual(query['q'], [NAME])
        self.assertEqual(set(query), {'q', 'maxItems', 'fields'})
        self.assertNotIn('linkToArchive', query['fields'][0])
        for endpoint in ('https://arquivo.pt/textextracted', 'https://other.example/textsearch',
                         ENDPOINT + '?versionHistory=x', ENDPOINT + '#fragment'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                public_search_url(NAME, dict(SOURCE, url=endpoint))

    def test_captures_are_deduplicated_and_retained_without_becoming_publication_or_body(self):
        fetch = response([item(), item(tstamp='20210102030405', digest='second-digest')])
        result = parse_public_results(fetch, SOURCE)
        self.assertEqual(len(result), 1)
        row = result[0]
        self.assertEqual(row['text'], NAME + ' & contexto.')
        self.assertIsNone(row['published_at'])
        self.assertEqual(row['source_kind'], 'discovery')
        self.assertEqual(row['metadata']['score_effect'], 'none')
        self.assertTrue(row['metadata']['archive_metadata_only'])
        self.assertEqual(len(row['metadata']['archive_captures']), 2)
        self.assertNotIn('linkToArchive', json.dumps(row))
        self.assertNotIn('linkToExtractedText', json.dumps(row))
        self.assertEqual(fetch['parse_metadata']['raw_archive_captures'], 2)
        self.assertEqual(fetch['parse_metadata']['duplicate_archive_captures'], 1)

    def test_unsafe_originals_are_not_emitted_and_cannot_grant_article_permissions(self):
        fetch = response([item('https://127.0.0.1/private'), item('http://publisher.example/old'),
                          item('https://user:secret@publisher.example/story'), item()])
        rows = parse_public_results(fetch, SOURCE)
        self.assertEqual([r['url'] for r in rows], ['https://publisher.example/story'])
        self.assertEqual(fetch['parse_metadata']['excluded_unsafe_or_non_https_originals'], 3)
        with tempfile.TemporaryDirectory() as directory:
            news = NewsAcquirer(Engine(directory, online=True), merge_defaults=False)
            self.assertIsNone(news._source_for_article(rows[0]['url'], SOURCE, [SOURCE]))

    def test_bad_json_schema_timestamp_and_query_are_not_successful_empty_searches(self):
        bad = [None, {}, {'response_items': []},
               {'request_parameters': {'q': 'Another query'}, 'response_items': []},
               {'request_parameters': {'q': NAME}, 'response_items': [item(tstamp='20200230000000')]},
               {'request_parameters': {'q': NAME}, 'response_items': [item(snippet=None)]}]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(PublicSearchParseError):
                parse_public_results(dict(response([]), body=json.dumps(payload)), SOURCE)
        with self.assertRaises(PublicSearchParseError):
            parse_public_results(dict(response([]), body='{"response_items":[],"response_items":[]}'), SOURCE)
        self.assertEqual(parse_public_results(response([]), SOURCE), [])

    def test_archive_does_not_replace_any_primary_provider_query(self):
        entities = [{'id': str(i), 'name': NAME + ' ' + str(i)} for i in range(6)]
        other = dict(SOURCE, id='other', kind='mwmbl', url='https://mwmbl.org/api/v2/search/',
                     allowed_hosts=['mwmbl.org'])
        plan = build_search_plan(entities, [SOURCE, other], {'strategy': 'public', 'preset': 'identity',
            'include_aliases': False, 'use_context': False, 'max_queries': 12})
        self.assertEqual([q['source_id'] for q in plan['queries'][:6]], ['other'] * 6)
        self.assertEqual([q['source_id'] for q in plan['queries'][6:]], ['archive'] * 6)
        source = next(s for s in plan['sources'] if s['id'] == 'archive')
        self.assertTrue(source['archive_capture_is_not_publication'])
        self.assertEqual(source['session_budget_fraction'], .1)

    def test_one_budget_is_shared_across_calls_and_clipped_to_the_session(self):
        clock = [100.]
        budget = SessionBudget(600, 650, lambda: clock[0])
        self.assertEqual(budget.deadline(), 160)
        clock[0] = 170
        self.assertEqual(budget.deadline(), 160)
        self.assertEqual(SessionBudget(600, 200, lambda: 190).deadline(), 200)

    def test_real_scheduler_preserves_budget_limited_jobs_without_http_and_resumes_next_session(self):
        real_monotonic = time.monotonic
        offset = [0.]
        calls = []

        class Fetcher:
            def __init__(self, *_args, **_kwargs): pass
            def close(self): pass
            def fetch(self, url, source):
                mode = source['_cache_mode']
                calls.append((mode, source['_deadline_monotonic']))
                if mode == 'local':
                    return {'status': 'blocked', 'body': '', 'error_code': 'local_cache_miss',
                            'request_attempted': False, 'network_requests': 0}
                # Advance beyond the 10-second archive window in a 100-second
                # session. All later calls must be local, with no business HTTP.
                self_outer.assertLessEqual(source['timeout_seconds'], 10.01)
                offset[0] += 11
                q = parse_qs(urlsplit(url).query)['q'][0]
                return {'status': 'ok', 'url': url, 'body': json.dumps({
                    'request_parameters': {'q': q}, 'response_items': []}), 'network_requests': 1}

        self_outer = self
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, online=True)
            engine.store.import_entities([{'id': str(i), 'name': NAME + ' ' + str(i)} for i in range(3)])
            news = NewsAcquirer(engine, merge_defaults=False)
            news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {
                'strategy': 'public', 'preset': 'identity', 'include_aliases': False, 'use_context': False}}), encoding='utf8')
            with patch('peps.collector.SafeFetcher', Fetcher), \
                 patch('peps.news.time.monotonic', side_effect=lambda: real_monotonic() + offset[0]):
                news.acquire(workers=1, seconds=100)
                with engine.store.connection() as con:
                    rows = con.execute("SELECT status,attempts,error FROM jobs WHERE kind='news_query'").fetchall()
                self.assertEqual(sum(r['status'] == 'done' for r in rows), 1)
                deferred = [r for r in rows if r['status'] == 'deferred']
                self.assertEqual(len(deferred), 2)
                self.assertTrue(all(r['attempts'] == 0 and r['error'] == SESSION_LIMIT for r in deferred))
                self.assertEqual([mode for mode, _ in calls], ['reuse', 'local', 'local'])
                calls.clear()
                news.acquire(workers=1, seconds=100, queue=False)
                self.assertEqual(calls[0][0], 'reuse')
                self.assertEqual(len([c for c in calls if c[0] == 'reuse']), 1)
