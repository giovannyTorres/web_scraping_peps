"""Public-search adapters are metadata only; all strings are neutral fixtures."""
import json
from pathlib import Path
from collections import Counter
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_search import SearchPlanningStopped, build_search_plan, normalize_search_config, query_url
from peps.public_search import public_queries, public_search_links, public_search_url, safe_result_url
from peps.sources import build_search_url, parse_results


ENTITY = {'id': 'example', 'name': 'Persona Pública Ejemplo', 'kind': 'person'}
SOURCE = {'id': 'public', 'kind': 'mwmbl', 'url': 'https://mwmbl.org/api/v2/search/',
          'enabled': True, 'terms_approved': True, 'allowed_hosts': ['mwmbl.org'],
          'max_records': 50, 'download_articles': False, 'article_terms_approved': False}


def response(body, kind='mwmbl'):
    return {'status': 'ok', 'url': public_search_url(ENTITY, dict(SOURCE, kind=kind, url=None)),
            'body': json.dumps(body) if not isinstance(body, str) else body}


class PublicAdaptersTests(unittest.TestCase):
    def test_query_encoding_preserves_the_nominal_query_without_implicit_country_or_dates(self):
        for kind in ('mwmbl', 'duckduckgo_html', 'duckduckgo_lite'):
            source = {'kind': kind}
            url = public_search_url('Peña & Ejemplo', source)
            self.assertEqual(parse_qs(urlsplit(url).query), {'q': ['Peña & Ejemplo']})
            self.assertEqual(build_search_url('Peña & Ejemplo', source), url)

    def test_searxng_requires_an_explicit_https_endpoint_and_json(self):
        with self.assertRaises(ValueError):
            public_search_url(ENTITY, {'kind': 'searxng'})
        url = public_search_url(ENTITY, {'kind': 'searxng', 'url': 'https://search.example.org/search'})
        self.assertEqual(parse_qs(urlsplit(url).query)['format'], ['json'])

    def test_safe_result_url_excludes_private_credentials_ports_and_http_without_rewriting(self):
        for value in ('http://public.example/story', 'https://localhost/story', 'https://127.0.0.1/story',
                      'https://10.0.0.1/story', 'https://[::1]/story', 'https://[::ffff:8.8.8.8]/story',
                      'https://user:secret@example.org/', 'https://example.org:444/',
                      'https://metadata.google.internal/', 'https://foo.local/', 'https://example.org/\nfoo',
                      'https://2130706433/', 'https://127.1/', 'https://bad..example/', 'https://example.org\\foo'):
            with self.subTest(value=value):
                self.assertIsNone(safe_result_url(value))
        url = 'https://example.org/a%2Fb?second=2&first=1#part'
        self.assertEqual(safe_result_url(url), url)
        self.assertIsNotNone(safe_result_url('https://8.8.8.8/'))

    def test_mwmbl_keeps_unknown_publication_dates_and_metadata_only(self):
        payload = {'query': 'Ejemplo', 'number_of_results': 1, 'results': [
            {'url': 'https://editor.example/story?b=2&a=1', 'title': 'Persona Pública Ejemplo',
             'content': 'Resumen ficticio', 'score': .9, 'engine': 'mwmbl'}]}
        fetch = response(payload)
        record = parse_results(fetch, SOURCE)[0]
        self.assertEqual(record['url'], payload['results'][0]['url'])
        self.assertEqual(record['source_kind'], 'discovery')
        self.assertIsNone(record['published_at'])
        self.assertEqual(record['metadata']['score_effect'], 'none')
        self.assertTrue(record['metadata']['discovery_only'])
        self.assertFalse(fetch['parse_metadata']['complete_archive'])

    def test_mwmbl_bounds_results_counts_unsafe_urls_and_deduplicates(self):
        records = [{'url': 'https://example.org/' + str(i), 'title': 'Ejemplo', 'content': ''} for i in range(4)]
        records.extend([records[0], {'url': 'http://example.org/', 'title': 'Ejemplo'}])
        fetch = response({'query': 'Ejemplo', 'number_of_results': len(records), 'results': records})
        rows = parse_results(fetch, dict(SOURCE, max_records=2))
        self.assertEqual(len(rows), 2)
        self.assertEqual(fetch['parse_metadata']['observed_entries'], 6)
        self.assertTrue(fetch['parse_metadata']['truncated'])
        self.assertEqual(fetch['parse_metadata']['excluded_unsafe_or_non_https_urls'], 1)

    def test_error_envelopes_malformed_schema_and_generic_html_are_not_empty_success(self):
        for payload in ('<html><title>Access denied</title></html>', '', '{}', '[]',
                        {'results': []}, {'query': 'x', 'number_of_results': True, 'results': []},
                        {'query': 'x', 'number_of_results': 1, 'results': []},
                        {'query': 'x', 'number_of_results': 0, 'results': [], 'error': 'busy'},
                        {'query': 'x', 'number_of_results': 1, 'results': [None]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_results(response(payload), SOURCE)
        self.assertEqual(parse_results(response({'query': 'x', 'number_of_results': 0, 'results': []}), SOURCE), [])

    def test_duckduckgo_html_extracts_organic_cards_and_unwraps_without_following_redirects(self):
        target = 'https://editor.example/a%2Fb?two=2&one=1'
        href = '//duckduckgo.com/l/?' + urlencode({'uddg': target, 'rut': 'opaque'})
        body = ('<html><div id="links"><div class="result"><a class="result__a" href="' + href + '">'
                'Persona <b>Pública</b> Ejemplo</a><a class="result__snippet">Resumen <b>ficticio</b>.</a></div>'
                '<div class="result result--ad"><a class="result__a" href="https://ads.example/">Anuncio</a></div>'
                '<script><a class="result__a" href="https://script.example/">Oculto</a></script></div></html>')
        fetch = response(body, 'duckduckgo_html')
        rows = parse_results(fetch, {'id': 'ddg', 'kind': 'duckduckgo_html'})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['url'], rows[0]['title'], rows[0]['text']),
                         (target, 'Persona Pública Ejemplo', 'Resumen ficticio.'))

    def test_duckduckgo_lite_table_snippet_and_direct_url(self):
        body = ('<table><tr><td><a class="result-link" href="https://editor.example/story">Persona Pública Ejemplo</a>'
                '</td></tr><tr><td class="result-snippet">Resumen ficticio.</td></tr></table>')
        rows = parse_results(response(body, 'duckduckgo_lite'), {'id': 'ddg', 'kind': 'duckduckgo_lite'})
        self.assertEqual(rows[0]['text'], 'Resumen ficticio.')

    def test_duckduckgo_challenge_or_unrecognized_html_is_not_empty_success(self):
        bodies = ('<form action="/anomaly.js"><div id="anomaly-modal">Verify</div></form>',
                  '<html><title>DuckDuckGo</title><p>Unavailable</p></html>',
                  '<script>result__a</script>')
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_results(response(body, 'duckduckgo_html'), {'kind': 'duckduckgo_html'})
        empty = '<div id="links"><div class="no-results">No results found</div></div>'
        self.assertEqual(parse_results(response(empty, 'duckduckgo_html'), {'kind': 'duckduckgo_html'}), [])

    def test_duckduckgo_unsupported_redirect_links_and_unsafe_destinations_are_excluded(self):
        links = ('//duckduckgo.com/l/?uddg=https%3A%2F%2F127.0.0.1%2F',
                 '//duckduckgo.com/l/?uddg=http%3A%2F%2Fexample.org%2F',
                 '//duckduckgo.com/l/?url=https%3A%2F%2Fexample.org%2F',
                 '//duckduckgo.com/?q=x')
        body = '<div id="links">' + ''.join('<a class="result__a" href="' + link + '">Ejemplo</a>' for link in links) + '</div>'
        fetch = response(body, 'duckduckgo_html')
        self.assertEqual(parse_results(fetch, {'kind': 'duckduckgo_html'}), [])
        self.assertEqual(fetch['parse_metadata']['excluded_unsafe_or_non_https_urls'], 4)

    def test_searxng_unresponsive_empty_engines_are_not_success(self):
        source = {'kind': 'searxng'}
        with self.assertRaises(ValueError):
            parse_results({'status': 'ok', 'body': json.dumps({'query': 'x', 'results': [],
                          'unresponsive_engines': [['engine', 'timeout']]})}, source)

    def test_manual_search_links_are_https_only_and_do_not_fetch_or_assert_risk(self):
        with patch('peps.collector.SafeFetcher', side_effect=AssertionError('No network from links')):
            result = public_search_links(ENTITY)
        self.assertGreaterEqual(len(result['links']), 4)
        self.assertIn('No se descarga ni puntúa', result['note'])
        for row in result['links']:
            self.assertTrue(safe_result_url(row['url']))
            self.assertIn(row['channel'], ('web', 'social'))


class PublicPlanningTests(unittest.TestCase):
    def test_public_plan_does_not_multiply_years_or_override_disabled_permissions(self):
        settings = {'strategy': 'public', 'start': '2014-01-01', 'end': '2022-12-31',
                    'include_aliases': False, 'use_context': False}
        plan = build_search_plan([ENTITY], [SOURCE], settings)
        self.assertEqual(plan['query_count'], 1)
        self.assertEqual(plan['sources'][0]['window_unit'], 'unfiltered_search')
        self.assertFalse(plan['sources'][0]['historical'])
        self.assertFalse(plan['sources'][0]['date_filter_supported'])
        self.assertEqual(query_url(SOURCE, plan['queries'][0]), plan['queries'][0]['url'])
        self.assertEqual(build_search_plan([ENTITY], [dict(SOURCE, enabled=False)], settings)['query_count'], 0)
        self.assertEqual(build_search_plan([ENTITY], [dict(SOURCE, terms_approved=False)], settings)['query_count'], 0)

    def test_public_plan_is_bounded_for_300_entities(self):
        entities = [dict(ENTITY, id=str(i), name='Persona Pública Ejemplo ' + str(i)) for i in range(300)]
        plan = build_search_plan(entities, [SOURCE], {'strategy': 'public', 'max_queries': 50,
            'include_aliases': False, 'use_context': False, 'start': '2014-01-01'})
        self.assertEqual(plan['query_count'], 50)
        self.assertEqual(plan['queries_required'], 300)
        self.assertTrue(plan['truncated'])

    def test_300_names_are_assigned_across_providers_before_second_nominal_sweep(self):
        entities = [dict(ENTITY, id=str(i), name=f'Persona Pública Ejemplo {i}',
                         aliases=[f'Alias Público Ejemplo {i}']) for i in range(300)]
        second = dict(SOURCE, id='second', kind='duckduckgo_html',
                      url='https://html.duckduckgo.com/html/')
        for mode in ('quick', 'extended'):
            settings = {'strategy': 'public', 'max_queries': 300, 'execution_mode': mode,
                        'preset': 'adverse', 'use_context': False}
            plan = build_search_plan(entities, [SOURCE, second], settings)
            queries = plan['queries']
            self.assertEqual(plan['entities_planned'], 300)
            self.assertEqual(len(queries), 300)
            self.assertEqual(Counter(query['source_id'] for query in queries),
                             {'public': 150, 'second': 150})
            self.assertEqual([query['entity_ids'] for query in queries], [[str(i)] for i in range(300)])
            self.assertTrue(all(query['query_type'] == 'identity' for query in queries))
            self.assertTrue(plan['truncated'])
            expanded = build_search_plan(entities, [SOURCE, second], dict(settings, max_queries=600))
            self.assertEqual(Counter(query['source_id'] for query in expanded['queries']),
                             {'public': 300, 'second': 300})
            self.assertEqual(len({(query['source_id'], query['query']) for query in expanded['queries']}), 600)
            self.assertTrue(all(query['query_type'] == 'identity' for query in expanded['queries']))
            self.assertEqual(plan['queries'], expanded['queries'][:300])

    def test_primary_provider_assignment_keeps_homonyms_and_cooperative_stop(self):
        entities = [dict(ENTITY, id=str(i), name=f'Persona Pública Ejemplo {i // 2}') for i in range(300)]
        second = dict(SOURCE, id='second', kind='duckduckgo_html',
                      url='https://html.duckduckgo.com/html/')
        plan = build_search_plan(entities, [SOURCE, second], {'strategy': 'public', 'max_queries': 150,
                                                          'use_context': False})
        self.assertEqual(plan['entities_planned'], 300)
        self.assertEqual(len(plan['queries']), 150)
        self.assertEqual(Counter(query['source_id'] for query in plan['queries']),
                         {'public': 75, 'second': 75})
        checks = 0
        def stop():
            nonlocal checks
            checks += 1
            return checks > 3000
        with self.assertRaises(SearchPlanningStopped):
            build_search_plan(entities, [SOURCE, second], {'strategy': 'public', 'max_queries': 1000},
                              should_stop=stop)

    def test_focus_presets_follow_nominal_sweep_before_aliases_without_boolean_or_year_expansion(self):
        entities = [dict(ENTITY, id='one', aliases=['Persona Pública Alternativa']),
                    dict(ENTITY, id='two', name='Otra Persona Pública Ejemplo')]
        for preset, terms in (('adverse', ('corrupción', 'sanción')),
                              ('public_role', ('contrato', 'nombramiento'))):
            for kind in ('mwmbl', 'duckduckgo_html', 'duckduckgo_lite', 'lemmy_search'):
                with self.subTest(preset=preset, kind=kind):
                    source = dict(SOURCE, kind=kind)
                    if kind == 'lemmy_search':
                        source.update(url='https://lemmy.example/api/v3/search', allowed_hosts=['lemmy.example'])
                    plan = build_search_plan(entities, [source], {'strategy': 'public', 'preset': preset,
                        'start': '2014-01-01', 'end': '2022-12-31', 'use_context': False})
                    queries = plan['queries']
                    self.assertEqual([q['query_type'] for q in queries[:2]], ['identity', 'identity'])
                    self.assertEqual([q['query'] for q in queries[2:4]],
                                     [entity['name'] + ' ' + terms[0] for entity in entities])
                    self.assertEqual([q['query'] for q in queries[4:6]],
                                     [entity['name'] + ' ' + terms[1] for entity in entities])
                    self.assertEqual(queries[-1]['query_type'], 'alias')
                    self.assertEqual(plan['query_count'], 7)
                    for query in queries:
                        args = parse_qs(urlsplit(query['url']).query)
                        self.assertEqual(args['q'], [query['query']])
                        self.assertNotIn(' OR ', query['query'])
                        self.assertNotIn('after:', query['query'])
                    self.assertTrue(any('no acreditan hallazgos ni generan puntos' in warning for warning in plan['warnings']))

    def test_identity_adds_no_focus_queries_and_focus_honors_global_query_budget(self):
        settings = {'strategy': 'public', 'include_aliases': False, 'use_context': False,
                    'start': '2014-01-01', 'end': '2022-12-31'}
        nominal = build_search_plan([ENTITY], [SOURCE], dict(settings, preset='identity'))
        self.assertEqual(nominal['query_count'], 1)
        focused = build_search_plan([ENTITY], [SOURCE], dict(settings, preset='adverse', max_queries=2))
        self.assertEqual(focused['query_count'], 2)
        self.assertEqual(focused['queries_required'], 3)
        self.assertTrue(focused['truncated'])
        self.assertEqual([q['query_type'] for q in focused['queries']], ['identity', 'focus'])

    def test_context_is_secondary_bounded_and_respects_the_nominal_sweep_and_switch(self):
        entities = [dict(ENTITY, id=str(i), name=f'Persona Pública Ejemplo {i}',
                         context={'municipality': 'Municipio Ejemplo', 'state': 'Estado Ejemplo',
                                  'role': 'presidente municipal'}) for i in range(300)]
        second = dict(SOURCE, id='second', kind='duckduckgo_html',
                      url='https://html.duckduckgo.com/html/')
        settings = {'strategy': 'public', 'use_context': True, 'include_aliases': False, 'max_queries': 300}
        nominal = build_search_plan(entities, [SOURCE, second], settings)
        self.assertEqual(nominal['entities_planned'], 300)
        self.assertTrue(all(query['query_type'] == 'identity' for query in nominal['queries']))
        self.assertEqual(nominal['queries_required'], 1200)
        extended = build_search_plan(entities, [SOURCE, second], dict(settings, max_queries=1000))
        self.assertTrue(all(query['query_type'] == 'identity' for query in extended['queries'][:600]))
        self.assertEqual(extended['queries'][600]['query_type'], 'context')
        self.assertEqual(extended['queries'][600]['query'], entities[0]['name'] + ' Municipio Ejemplo')
        plain = build_search_plan(entities, [SOURCE, second], dict(settings, use_context=False, max_queries=1000))
        self.assertEqual(plain['query_count'], 600)
        self.assertTrue(all(query['query_type'] == 'identity' for query in plain['queries']))

    def test_public_context_accepts_imported_hint_shapes_and_skips_oversized_hints(self):
        entity = dict(ENTITY, context={'municipality': [{'value': 'Municipio\nEjemplo'}], 'state': 'Estado'})
        queries = public_queries(entity, {'use_context': True}, [ENTITY['name']])
        self.assertEqual([query['query_type'] for query in queries], ['identity', 'context'])
        self.assertEqual(queries[1]['context_hint'], 'Municipio Ejemplo')
        oversized = dict(ENTITY, context={'municipality': 'M' * 500, 'state': 'Estado'})
        self.assertEqual(public_queries(oversized, {'use_context': True}, [ENTITY['name']])[1]['context_hint'], 'Estado')
        invalid = dict(ENTITY, context={'municipality': {'unrecognized': ['municipio']}})
        self.assertEqual(len(public_queries(invalid, {'use_context': True}, [ENTITY['name']])), 1)

    def test_only_one_duckduckgo_format_per_plan_no_automatic_fallback(self):
        html = dict(SOURCE, id='html', kind='duckduckgo_html', url='https://html.duckduckgo.com/html/')
        lite = dict(SOURCE, id='lite', kind='duckduckgo_lite', url='https://lite.duckduckgo.com/lite/')
        plan = build_search_plan([ENTITY], [html, lite], {'strategy': 'public'})
        self.assertTrue(all(query['source_id'] == 'html' for query in plan['queries']))
        self.assertTrue(any('solo formato' in warning for warning in plan['warnings']))

    def test_public_source_is_also_available_in_targeted_but_not_shared_or_feeds(self):
        for strategy in ('targeted', 'public'):
            self.assertGreater(build_search_plan([ENTITY], [SOURCE], {'strategy': strategy})['query_count'], 0)
        for strategy in ('shared', 'feeds'):
            self.assertEqual(build_search_plan([ENTITY], [SOURCE], {'strategy': strategy})['query_count'], 0)
        self.assertEqual(normalize_search_config({'strategy': 'public'})['strategy'], 'public')

    def test_new_catalogue_sources_never_enable_or_authorize_article_bodies_by_default(self):
        config = json.loads((Path(__file__).resolve().parents[1] / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
        for source in config['sources']:
            if source.get('kind') in {'mwmbl', 'duckduckgo_html', 'duckduckgo_lite'}:
                for field in ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved'):
                    self.assertFalse(source[field])

    def test_web_snippet_with_adverse_words_remains_unscorable_without_editorial_body(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, online=True)
            news = NewsAcquirer(engine)
            engine.store.import_entities([ENTITY])
            news.config_path.write_text(json.dumps({'sources': [SOURCE], 'search': {'strategy': 'public'}}), encoding='utf-8')
            payload = {'source_id': 'public', 'query': ENTITY['name'], 'entity_ids': ['example'],
                       'names': [ENTITY['name']], 'start': '20140101000000', 'end': '20270101000000', 'result_limit': 50}
            engine.store.enqueue('news_query', payload, key='public-fixture')
            class FixtureFetcher:
                def fetch(self, url, _source):
                    return {'status': 'ok', 'url': url, 'body': json.dumps({'query': ENTITY['name'],
                        'number_of_results': 1, 'results': [{'url': 'https://editor.example/story',
                        'title': ENTITY['name'], 'content': 'Texto ficticio de control: fue detenida por fraude.'}]})}
            news.execute(engine.store.claim(kind_prefix='news_query'), FixtureFetcher())
            self.assertEqual(news.stats()['scopes'], {'feed_metadata_only': 1})
            with engine.store.connection() as con:
                self.assertEqual(con.execute("SELECT count(*) FROM jobs WHERE kind='news_article'").fetchone()[0], 0)
            with patch('peps.nlp.analyze', side_effect=AssertionError('Search snippets must not invoke NLP')):
                news.attribute(rescore=False)
            engine.rescore()
            audits = engine.explanation('example')['result']['evidence_audit']
            self.assertTrue(all('discovery_index_is_not_article_evidence' in item['exclusions'] for item in audits))


if __name__ == '__main__':
    unittest.main()
