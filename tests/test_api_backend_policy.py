"""Durable documented-API backend policy with synthetic, offline transport."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from peps.collector import SafeFetcher
from peps.public_search import public_search_url
from tests.test_documented_api_permissions import SOURCE


def source(engines='mojeek', **changes):
    return dict(deepcopy(SOURCE), search_url='https://search.example/search?engines=' + engines,
                **changes)


def record(engine):
    return {'url': 'https://publisher.example.org/synthetic-story', 'title': 'Synthetic report',
            'engine': engine, 'engines': [engine]}


def response(results=(), errors=()):
    return {'results': list(results), 'unresponsive_engines': list(errors)}


class ApiBackendPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.now = [1800000000.]
        self.calls = []
        self.payload = response()

    def fetcher(self, root=None):
        fetcher = SafeFetcher({'trust_env_proxy': False}, root or self.root)
        self.addCleanup(fetcher.close)
        fetcher._clock = lambda: self.now[0]
        fetcher._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        def transport(url, *_args):
            self.calls.append(url)
            if url.endswith('/robots.txt'):
                return 200, {'content-type': 'text/plain'}, b'User-agent: *\nAllow: /\n'
            return 200, {'content-type': 'application/json', 'etag': 'synthetic-etag'}, self.raw()
        fetcher._transport = transport
        return fetcher

    def raw(self):
        return json.dumps(self.payload, ensure_ascii=False, indent=2).encode('utf-8')

    def fetch(self, fetcher, config=None, query='Synthetic fictional person'):
        config = config or source()
        url = public_search_url(query, config)
        result = fetcher.fetch(url, config)
        if result.get('error_code') == 'host_interval':
            self.now[0] += 2
            result = fetcher.fetch(url, config)
        return result

    def rows(self, fetcher, table):
        with fetcher._db() as con:
            return [dict(row) for row in con.execute('SELECT * FROM ' + table)]

    def no_network(self, fetcher):
        fetcher._dns = lambda *_a, **_k: self.fail('Policy restriction attempted DNS')
        fetcher._transport = lambda *_a, **_k: self.fail('Policy restriction attempted HTTP')

    def test_backend_denial_survives_new_query_source_and_fetcher_without_host_ban(self):
        fetcher = self.fetcher()
        self.payload = response(errors=[['mojeek', 'Suspended: verify you are human']])
        first = self.fetch(fetcher)
        self.assertEqual(first['error_code'], 'discovery_backend_restricted')
        self.assertEqual(first['status'], 'blocked')
        self.assertIsNone(first['retry_at'])
        self.assertEqual(first['body'].encode('utf-8'), self.raw())
        self.assertEqual(self.rows(fetcher, 'hosts')[0]['blocked_until'], 0)
        self.assertEqual(self.rows(fetcher, 'http_cache'), [])
        fetcher.close()
        restarted = self.fetcher()
        self.no_network(restarted)
        again = self.fetch(restarted, source(id='new-profile-id'), 'Another fictional person')
        self.assertEqual(again['error_code'], 'discovery_backend_restricted')
        self.assertEqual(again['network_requests'], 0)
        restriction = self.rows(restarted, 'api_backend_restrictions')[0]
        self.assertEqual(restriction['pending_review'], 1)
        self.assertEqual(restriction['engine'], 'mojeek')
        self.assertEqual(restriction['evidence_sha256'], hashlib.sha256(self.raw()).hexdigest())

    def test_fallback_blocks_route_and_persists_all_unexpected_denials(self):
        fetcher = self.fetcher()
        self.payload = response([record('yandex'), record('mwmbl')],
                                [['duckduckgo', 'Suspended: CAPTCHA'], ['seznam', 'Access denied'],
                                 ['qwant', 'Timeout']])
        result = self.fetch(fetcher)
        self.assertEqual(result['error_code'], 'discovery_backend_provenance_mismatch')
        self.assertEqual(result['backend_provenance'], 'invalid')
        self.assertEqual(result['reported_engines'], ['duckduckgo', 'mwmbl', 'qwant', 'seznam', 'yandex'])
        self.assertEqual({row['engine'] for row in self.rows(fetcher, 'api_backend_restrictions')},
                         {'duckduckgo', 'seznam'})
        observation = self.rows(fetcher, 'api_backend_observations')[0]
        self.assertEqual(observation['body'], self.raw())
        self.assertEqual(len(json.loads(observation['diagnostics'])), 3)
        self.assertEqual(self.rows(fetcher, 'api_route_restrictions')[0]['requested_engines'], '["mojeek"]')
        self.no_network(fetcher)
        for name in ('mojeek', 'MOJEEK', 'mojeek,mojeek', '%6dojeek'):
            with self.subTest(selection=name):
                result = self.fetch(fetcher, source(name, id='different-profile'), 'Different query')
                self.assertEqual(result['error_code'], 'discovery_backend_provenance_mismatch')
                self.assertEqual(result['network_requests'], 0)

    def test_route_key_canonicalizes_case_order_duplicates_and_encoding(self):
        fetcher = self.fetcher()
        self.payload = response([record('unexpected')])
        self.assertEqual(self.fetch(fetcher, source('mojeek,yandex'))['status'], 'blocked')
        self.no_network(fetcher)
        for name in ('YANDEX,MOJEEK', '%79andex,mojeek', 'mojeek,yandex,mojeek'):
            with self.subTest(selection=name):
                result = self.fetch(fetcher, source(name))
                self.assertEqual(result['error_code'], 'discovery_backend_provenance_mismatch')

    def test_bad_mojeek_route_does_not_ban_explicit_yandex_or_api_host(self):
        fetcher = self.fetcher()
        self.payload = response([record('yandex')], [['seznam', 'Suspended: access denied']])
        self.assertEqual(self.fetch(fetcher)['status'], 'blocked')
        self.payload = response([record('yandex')])
        result = self.fetch(fetcher, source('yandex'))
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['backend_provenance'], 'reported')
        self.assertEqual(self.rows(fetcher, 'hosts')[0]['blocked_until'], 0)
        self.assertEqual(len(self.rows(fetcher, 'api_route_restrictions')), 1)
        self.assertEqual({row['engine'] for row in self.rows(fetcher, 'api_backend_restrictions')}, {'seznam'})

    def test_denied_backend_cannot_be_hidden_in_multi_engine_selection(self):
        fetcher = self.fetcher()
        self.payload = response(errors=[['mojeek', 'Suspended: timeout']])
        self.fetch(fetcher)
        self.no_network(fetcher)
        result = self.fetch(fetcher, source('yandex,MOJEEK'))
        self.assertEqual(result['error_code'], 'discovery_backend_restricted')
        self.assertEqual(result['restricted_engines'], ['mojeek'])

    def test_endpoint_case_trailing_dot_and_default_port_cannot_reset_backend_policy(self):
        fetcher = self.fetcher()
        self.payload = response(errors=[['mojeek', 'Suspended: CAPTCHA']])
        self.fetch(fetcher)
        self.no_network(fetcher)
        for endpoint in ('https://SEARCH.EXAMPLE/search', 'https://search.example./search',
                         'https://search.example:443/search'):
            with self.subTest(endpoint=endpoint):
                config = source()
                config['search_url'] = endpoint + '?engines=mojeek'
                config['documented_api_permission']['endpoint'] = endpoint
                config['documented_api_permission']['operator_documentation_url'] = endpoint.replace('/search', '/info/en/about')
                result = self.fetch(fetcher, config)
                self.assertEqual(result['status'], 'blocked')
                self.assertEqual(result['network_requests'], 0)

    def test_dropping_grant_or_changing_kind_cannot_erase_existing_negative_policy(self):
        fetcher = self.fetcher()
        self.payload = response(errors=[['mojeek', 'Suspended: CAPTCHA']])
        self.fetch(fetcher)
        self.no_network(fetcher)
        for kind in ('searxng', 'rss', 'publisher_search'):
            for suffix in ('engines=mojeek', '', 'categories=general', 'engines=mojeek&categories=general'):
                with self.subTest(kind=kind, suffix=suffix):
                    config = source(kind=kind, id='new-id', robots_required=False)
                    config.pop('documented_api_permission')
                    url = 'https://search.example/search?q=fiction&format=json&' + suffix
                    result = fetcher.fetch(url, config)
                    self.assertEqual(result['status'], 'blocked')
                    self.assertEqual(result['error_code'], 'discovery_backend_restricted'
                                     if suffix == 'engines=mojeek' else 'documented_api_backend_scope')
                    self.assertEqual(result['network_requests'], 0)

    def test_generic_search_without_prior_negative_policy_does_not_gain_json_exception(self):
        fetcher = self.fetcher()
        config = source(robots_required=False)
        config.pop('documented_api_permission')
        self.payload = response(errors=[['mojeek', 'Suspended: verify you are human']])
        result = self.fetch(fetcher, config)
        self.assertEqual(result['error_code'], 'access_challenge')
        self.assertEqual(self.rows(fetcher, 'api_backend_restrictions'), [])

    def test_missing_or_ambiguous_provenance_stops_route_without_backend_ban(self):
        invalid = [{}, {'engine': None}, {'engine': 'mojeek', 'engines': ['yandex']},
                   {'engines': []}, {'engines': 'mojeek'}, {'engines': [None]},
                   {'engine': 'mojeek,yandex'}, {'engine': 'mojeek\x00'}]
        for index, bad in enumerate(invalid):
            with self.subTest(index=index):
                fetcher = self.fetcher(self.root / str(index))
                self.payload = response([dict(record('mojeek'), **bad)] if bad else [{'url': 'https://publisher.example.org/story'}])
                if 'engines' in bad and 'engine' not in bad:
                    self.payload['results'][0].pop('engine', None)
                result = self.fetch(fetcher)
                self.assertEqual(result['error_code'], 'discovery_backend_provenance_mismatch')
                self.assertEqual(self.rows(fetcher, 'api_backend_restrictions'), [])
                self.assertEqual(len(self.rows(fetcher, 'api_route_restrictions')), 1)

    def test_valid_result_provenance_formats_and_unobserved_empty_response(self):
        cases = [response(), response([{'engine': 'mojeek'}]), response([{'engines': ['MOJEEK']}]),
                 response([{'engine': 'mojeek', 'engines': ['mojeek', 'MOJEEK']}])]
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                fetcher = self.fetcher(self.root / str(index))
                self.payload = payload
                result = self.fetch(fetcher)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['backend_provenance'], 'unobserved' if index == 0 else 'reported')
                again = self.fetch(fetcher)
                self.assertEqual(again['backend_provenance'], result['backend_provenance'])
                self.assertEqual(again['network_requests'], 0)

    def test_transient_backend_diagnostics_are_observed_without_permanent_ban(self):
        fetcher = self.fetcher()
        self.payload = response(errors=[['mojeek', 'Timeout'], ['mojeek', 'OSError: network failure']])
        result = self.fetch(fetcher)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['backend_diagnostics_count'], 2)
        self.assertEqual(self.rows(fetcher, 'api_backend_restrictions'), [])
        self.assertEqual(self.rows(fetcher, 'api_route_restrictions'), [])
        self.assertEqual(len(json.loads(self.rows(fetcher, 'api_backend_observations')[0]['diagnostics'])), 2)

    def test_reviewed_backend_restriction_marker_matrix_blocks_next_query(self):
        messages = ['Suspended: verify you are human', 'Verify you are a human', 'Human verification required',
                    'HTTP 401', 'HTTP 403', 'HTTP 407', 'HTTP 429', 'HTTP 451', 'Access denied',
                    'Permission denied', 'Too many requests', 'Rate limit exceeded', 'ratelimited',
                    'CAPTCHA required', 'Challenge page', 'Unusual traffic from your computer', 'Forbidden',
                    'Temporarily blocked', 'Blocking requests', 'IP banned', 'Bot detection', 'Bot protection',
                    'Automated queries restricted', 'cf-chl-response', 'anomaly-modal', '/anomaly.js',
                    'Just a moment', 'Are you a robot', 'robot check', 'Suspended: timeout']
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                fetcher = self.fetcher(self.root / str(index))
                self.payload = response(errors=[['mojeek', message]])
                first = self.fetch(fetcher)
                self.assertEqual(first['error_code'], 'discovery_backend_restricted')
                self.assertEqual(first['status'], 'blocked')
                self.assertEqual(self.rows(fetcher, 'hosts')[0]['blocked_until'], 0)
                self.no_network(fetcher)
                again = self.fetch(fetcher, query='Another fictional person')
                self.assertEqual(again['error_code'], 'discovery_backend_restricted')
                self.assertEqual(again['network_requests'], 0)

    def test_ordinary_backend_failure_matrix_never_creates_permanent_restriction(self):
        for index, message in enumerate(['Timeout', 'OSError: network failure', 'HTTP 500', 'HTTP 503',
                                         'No results', 'Engine not found', 'Parse error']):
            with self.subTest(message=message):
                fetcher = self.fetcher(self.root / str(index))
                self.payload = response(errors=[['mojeek', message]])
                self.assertEqual(self.fetch(fetcher)['status'], 'ok')
                self.assertEqual(self.rows(fetcher, 'api_backend_restrictions'), [])
                self.assertEqual(self.rows(fetcher, 'api_route_restrictions'), [])

    def test_malformed_results_preserve_valid_backend_denial_facts_and_stop_route(self):
        for index, results in enumerate([{}, None, 'unexpected', 0, False]):
            with self.subTest(results=results):
                fetcher = self.fetcher(self.root / str(index))
                self.payload = {'results': results, 'unresponsive_engines':
                                [['mojeek', 'Permission denied'], ['qwant', 'Suspended: timeout']]}
                first = self.fetch(fetcher)
                self.assertEqual(first['error_code'], 'discovery_backend_provenance_mismatch')
                self.assertEqual({row['engine'] for row in self.rows(fetcher, 'api_backend_restrictions')},
                                 {'mojeek', 'qwant'})
                captured = self.rows(fetcher, 'api_backend_observations')[0]
                self.assertEqual(captured['body'], self.raw())
                self.assertEqual(len(json.loads(captured['diagnostics'])), 2)
                self.no_network(fetcher)
                self.assertEqual(self.fetch(fetcher, query='Another fictional person')['network_requests'], 0)

    def test_valid_denials_are_captured_even_with_other_malformed_diagnostics(self):
        fetcher = self.fetcher()
        self.payload = response(errors=[{'unknown': 'shape'}, ['qwant', 'Suspended: CAPTCHA'],
                                       ['seznam', 'Access denied'], ['mojeek', 'Timeout']])
        result = self.fetch(fetcher)
        self.assertEqual(result['error_code'], 'discovery_backend_provenance_mismatch')
        self.assertEqual({row['engine'] for row in self.rows(fetcher, 'api_backend_restrictions')}, {'qwant', 'seznam'})
        self.assertEqual(result['backend_diagnostics_count'], 3)

    def test_known_backend_denial_prevents_fresh_local_cache_reuse(self):
        fetcher = self.fetcher()
        self.payload = response([record('mojeek')])
        self.assertEqual(self.fetch(fetcher)['status'], 'ok')
        cached = self.rows(fetcher, 'http_cache')[0]
        self.payload = response(errors=[['mojeek', 'Suspended: CAPTCHA']])
        self.now[0] += 2
        self.assertEqual(self.fetch(fetcher, source(_cache_mode='revalidate'))['status'], 'blocked')
        self.no_network(fetcher)
        for mode in ('local', 'reuse', 'revalidate'):
            with self.subTest(mode=mode):
                result = self.fetch(fetcher, source(_cache_mode=mode))
                self.assertEqual(result['error_code'], 'discovery_backend_restricted')
        self.assertEqual(self.rows(fetcher, 'http_cache')[0], cached)

    def test_post_dns_recheck_stops_new_restriction_before_transport(self):
        fetcher = self.fetcher()
        self.payload = response()
        self.assertEqual(self.fetch(fetcher)['status'], 'ok')
        self.now[0] += 2
        original = fetcher._dns
        def dns(*args, **kwargs):
            deny = response(errors=[['mojeek', 'Suspended: CAPTCHA']])
            raw = json.dumps(deny).encode('utf-8')
            url = public_search_url('Concurrent fictional person', source())
            fetcher._api_response_policy(raw.decode(), raw, source(), url, url,
                                         'application/json', 'application/json')
            return original(*args, **kwargs)
        fetcher._dns = dns
        before = len(self.calls)
        result = self.fetch(fetcher, query='Another fictional person')
        self.assertEqual(result['error_code'], 'discovery_backend_restricted')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(result['network_requests'], 0)

    def test_inflight_success_cannot_clear_restrictions_or_original_evidence(self):
        fetcher = self.fetcher()
        config = source()
        url = public_search_url('Synthetic fictional person', config)
        first = response(errors=[['mojeek', 'Suspended: CAPTCHA']])
        raw = json.dumps(first).encode()
        fetcher._api_response_policy(raw.decode(), raw, config, url, url, 'application/json', 'application/json')
        before = self.rows(fetcher, 'api_backend_restrictions')[0]
        self.now[0] += 120
        later = json.dumps(response([record('mojeek')])).encode()
        _metadata, failure = fetcher._api_response_policy(later.decode(), later, config, url, url,
                                                          'application/json', 'application/json')
        self.assertEqual(failure['error_code'], 'discovery_backend_restricted')
        self.assertEqual(self.rows(fetcher, 'api_backend_restrictions')[0], before)
        self.assertEqual(self.rows(fetcher, 'api_backend_observations')[0]['body'], raw)

    def test_restriction_committed_during_sqlite_wait_prevents_transport(self):
        fetcher = self.fetcher()
        self.assertEqual(self.fetch(fetcher)['status'], 'ok')
        self.now[0] += 2
        with fetcher._db() as con:
            database = con.execute('PRAGMA database_list').fetchone()[2]
        writer = sqlite3.connect(database, timeout=5)
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute('INSERT INTO api_backend_restrictions VALUES(?,?,?,?,?,1,?)',
                           ('https://search.example/search', 'mojeek', 'Suspended: CAPTCHA',
                            self.now[0], self.now[0], 'a' * 64))
            entered = threading.Event()
            original = fetcher._reserve
            def reserve(*args, **kwargs):
                entered.set()
                return original(*args, **kwargs)
            fetcher._reserve = reserve
            before = len(self.calls)
            outcomes = []
            def work():
                try:
                    outcomes.append(self.fetch(fetcher, source(robots_required=False),
                                               query='Concurrent fictional person'))
                except Exception as exc:
                    outcomes.append(exc)
            worker = threading.Thread(target=work)
            worker.start()
            try:
                self.assertTrue(entered.wait(5), 'Fetch never reached its atomic admission')
            finally:
                writer.commit()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outcomes), 1)
            if isinstance(outcomes[0], Exception):
                raise outcomes[0]
            self.assertEqual(outcomes[0]['error_code'], 'discovery_backend_restricted')
            self.assertEqual(len(self.calls), before)
        finally:
            writer.close()

    def test_restriction_arriving_after_body_validation_prevents_cache_promotion(self):
        fetcher = self.fetcher()
        self.payload = response([record('mojeek')])
        original = fetcher._content_success
        def success(host):
            original(host)
            denied = json.dumps(response(errors=[['mojeek', 'Suspended: CAPTCHA']])).encode()
            url = public_search_url('Concurrent fictional person', source())
            fetcher._api_response_policy(denied.decode(), denied, source(), url, url,
                                         'application/json', 'application/json')
        fetcher._content_success = success
        result = self.fetch(fetcher)
        self.assertEqual(result['error_code'], 'discovery_backend_restricted')
        self.assertEqual(self.rows(fetcher, 'http_cache'), [])

    def test_default_or_categories_selection_fails_before_dns_and_http(self):
        fetcher = self.fetcher()
        self.no_network(fetcher)
        for suffix in ('', '?categories=general', '?engines=', '?engines=mojeek&categories=general'):
            with self.subTest(suffix=suffix):
                config = source()
                config['search_url'] = 'https://search.example/search' + suffix
                result = self.fetch(fetcher, config)
                self.assertEqual(result['error_code'], 'documented_api_backend_scope')
                self.assertEqual(result['network_requests'], 0)

    def test_redirect_cannot_change_query_or_engine_before_following(self):
        for change in ('q', 'engines'):
            with self.subTest(parameter=change):
                fetcher = self.fetcher(self.root / change)
                original = fetcher._transport
                def transport(url, *args):
                    if url.endswith('/robots.txt'):
                        return original(url, *args)
                    self.calls.append(url)
                    parts = urlsplit(url)
                    query = parse_qs(parts.query)
                    query[change] = ['Different query' if change == 'q' else 'yandex']
                    target = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
                    return 302, {'location': target}, b''
                fetcher._transport = transport
                before = len(self.calls)
                self.assertEqual(self.fetch(fetcher)['error_code'], 'documented_api_backend_scope')
                self.assertEqual(len(self.calls) - before, 2)

    def test_legacy_cache_requires_full_revalidation_and_cannot_use_304(self):
        fetcher = self.fetcher()
        self.assertEqual(self.fetch(fetcher)['status'], 'ok')
        with fetcher._db() as con:
            con.execute("UPDATE http_cache SET api_backend_metadata=''")
        self.no_network(fetcher)
        self.assertEqual(self.fetch(fetcher, source(_cache_mode='local'))['status'], 'blocked')
        fetcher._dns = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        def not_modified(_url, headers, *_args):
            self.assertNotIn('If-None-Match', headers)
            return 304, {}, b''
        fetcher._transport = not_modified
        self.now[0] += 2
        result = self.fetch(fetcher)
        self.assertEqual(result['error_code'], 'documented_api_backend_scope')
        self.assertEqual(result['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
