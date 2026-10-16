"""Social search yields bounded editorial leads, never trusted social bodies."""
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from peps.relevance import RelevanceIndex
from peps.social_discovery import (MAX_EXCERPT_CHARS, build_lemmy_search_url,
                                   external_social_search_links, parse_lemmy_results)


SOURCE = {'id': 'lemmy', 'kind': 'lemmy_search', 'api_url': 'https://lemmy.example/api/v3/search',
          'allowed_hosts': ['lemmy.example'], 'max_records': 10}
ENTITY = {'id': 'person', 'name': 'Enrique Peña Nieto', 'kind': 'person'}


def post(number=1, **changes):
    value = {'id': number, 'name': 'Un informe sobre México', 'url': 'https://editor.example/story',
             'ap_id': 'https://community.example/post/' + str(number),
             'body': 'Una discusión pública sobre Enrique Peña Nieto, pendiente de corroborar.',
             'published': '2022-01-02T12:00:00Z'}
    value.update(changes)
    return {'post': value, 'creator': {'name': 'Profile never retained', 'bio': 'No profile copying'}}


def response(posts=None):
    return {'status': 'ok', 'url': build_lemmy_search_url(ENTITY, SOURCE),
            'body': json.dumps({'posts': [post()] if posts is None else posts})}


class LemmyURLTests(unittest.TestCase):
    def test_plain_query_is_encoded_without_inventing_boolean_dates_or_credentials(self):
        url = build_lemmy_search_url(ENTITY, SOURCE, limit=5, page=2)
        self.assertEqual(urlsplit(url).path, '/api/v3/search')
        self.assertEqual(parse_qs(urlsplit(url).query), {
            'q': ['Enrique Peña Nieto'], 'type_': ['Posts'], 'sort': ['TopAll'],
            'listing_type': ['All'], 'limit': ['5'], 'page': ['2']})
        special = 'Empresa & Asociados / México "Norte"'
        self.assertEqual(parse_qs(urlsplit(build_lemmy_search_url(special, SOURCE)).query)['q'], [special])

    def test_only_public_allowlisted_verified_v3_endpoints_are_planned(self):
        for url in ('http://lemmy.example/api/v3/search', 'https://localhost/api/v3/search',
                    'https://127.0.0.1/api/v3/search', 'https://[::1]/api/v3/search',
                    'https://user:secret@lemmy.example/api/v3/search', 'https://lemmy.example:8443/api/v3/search',
                    'https://lemmy.example/api/v4/search', 'https://lemmy.example/api/v3/search?q=other',
                    'https://lemmy.example/api/v3/search#secret', 'https://other.example/api/v3/search'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                build_lemmy_search_url(ENTITY, dict(SOURCE, api_url=url))

    def test_queries_limits_and_pages_are_bounded(self):
        for query in ('', 'x' * 301, None, 'Persona\r\nInjected', 42):
            with self.subTest(query=query), self.assertRaises(ValueError):
                build_lemmy_search_url(query, SOURCE)
        for kwargs in ({'limit': 0}, {'limit': 51}, {'limit': True}, {'page': 11}, {'page': False}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                build_lemmy_search_url(ENTITY, SOURCE, **kwargs)

    def test_manual_social_links_are_explicit_external_searches(self):
        links = external_social_search_links({'name': 'Enrique Peña Nieto & México'})
        self.assertEqual({link['id'] for link in links}, {'bluesky', 'mastodon', 'youtube', 'x', 'reddit'})
        for link in links:
            self.assertTrue(link['external'])
            self.assertFalse(link['automated'])
            self.assertTrue(link['may_require_login'])
            self.assertIn('búsqueda externa', link['label'])
            parameter = 'search_query' if link['id'] == 'youtube' else 'q'
            self.assertEqual(parse_qs(urlsplit(link['url']).query)[parameter], ['Enrique Peña Nieto & México'])


class LemmyParserTests(unittest.TestCase):
    def test_post_and_article_provenance_and_dates_remain_separate(self):
        fetch = response()
        with patch('socket.getaddrinfo', side_effect=AssertionError('Parser must not resolve DNS')):
            records = parse_lemmy_results(fetch, SOURCE)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['url'], 'https://editor.example/story')
        self.assertEqual(record['source_kind'], 'discovery')
        self.assertIsNone(record['published_at'])
        self.assertIsNone(record['discovered_at'])
        metadata = record['metadata']
        self.assertTrue(metadata['user_generated_content'])
        self.assertTrue(metadata['discovery_only'])
        self.assertFalse(metadata['article_body_available'])
        self.assertFalse(metadata['identity_verified'])
        self.assertEqual(metadata['score_effect'], 'none')
        self.assertEqual(metadata['social_post_url'], 'https://community.example/post/1')
        self.assertEqual(metadata['post_published_at'], '2022-01-02T12:00:00+00:00')
        self.assertNotIn('Profile never retained', json.dumps(record))
        self.assertNotIn('creator', metadata)

    def test_bounded_excerpt_keeps_target_mention_without_copying_full_ugc_body(self):
        body = 'Introducción general. ' * 200 + '<p>Enrique Peña Nieto aparece en este comentario.</p>' + ' Más contexto.' * 100
        records = parse_lemmy_results(response([post(body=body)]), SOURCE)
        excerpt = records[0]['text']
        self.assertIn('Enrique Peña Nieto', excerpt)
        self.assertLessEqual(len(excerpt), MAX_EXCERPT_CHARS)
        self.assertNotEqual(body, excerpt)
        self.assertTrue(records[0]['metadata']['ugc_excerpt_truncated'])
        self.assertTrue(RelevanceIndex([ENTITY]).rank(records[0]['title'], excerpt)['relevant'])

    def test_scripts_profiles_and_full_discussion_bodies_are_not_retained(self):
        body = '<script>HIDDEN SCRIPT</script><p>Enrique Peña Nieto publicó un informe.</p>'
        record = parse_lemmy_results(response([post(body=body)]), SOURCE)[0]
        self.assertNotIn('HIDDEN SCRIPT', json.dumps(record))
        self.assertNotIn('body', record)
        self.assertNotIn('creator', record)
        self.assertEqual(len(record['metadata']['ugc_body_sha256']), 64)

    def test_self_posts_private_and_unsafe_urls_never_become_original_article_candidates(self):
        urls = [None, 'http://editor.example/a', 'https://localhost/a', 'https://private.internal/a',
                'https://127.0.0.1/a', 'https://169.254.169.254/a', 'https://[::1]/a',
                'https://user:secret@editor.example/a', 'https://editor.example:444/a',
                'https://editor.example\\@127.0.0.1/a', 'https://lemmy.example/post/1',
                'https://community.example/post/1', 'https://community.example/other']
        fetch = response([post(index + 1, url=url) for index, url in enumerate(urls)])
        records = parse_lemmy_results(fetch, dict(SOURCE, max_records=50))
        self.assertEqual(records, [])
        self.assertEqual(fetch['parse_metadata']['excluded_non_editorial'], len(urls))

    def test_same_editorial_link_is_one_candidate_with_both_social_referrers(self):
        fetch = response([post(1), post(2)])
        records = parse_lemmy_results(fetch, SOURCE)
        self.assertEqual(len(records), 1)
        self.assertEqual(fetch['parse_metadata']['duplicate_editorial_urls'], 1)
        self.assertEqual([item['social_post_url'] for item in records[0]['metadata']['social_referrers']],
                         ['https://community.example/post/1', 'https://community.example/post/2'])

    def test_missing_remote_post_url_uses_local_instance_id_and_untraceable_post_is_excluded(self):
        fetch = response([post(5, ap_id=None)])
        self.assertEqual(parse_lemmy_results(fetch, SOURCE)[0]['metadata']['social_post_url'],
                         'https://lemmy.example/post/5')
        fetch = response([post(-1, ap_id=None)])
        self.assertEqual(parse_lemmy_results(fetch, SOURCE), [])
        self.assertEqual(fetch['parse_metadata']['excluded_non_editorial'], 1)

    def test_valid_empty_and_bounded_truncated_responses_are_explicit(self):
        fetch = response([])
        self.assertEqual(parse_lemmy_results(fetch, SOURCE), [])
        self.assertEqual(fetch['parse_metadata']['observed_entries'], 0)
        fetch = response([post(1), post(2, url='https://editor.example/another')])
        self.assertEqual(len(parse_lemmy_results(fetch, SOURCE, max_records=1)), 1)
        self.assertEqual(fetch['parse_metadata']['observed_entries'], 2)
        self.assertTrue(fetch['parse_metadata']['truncated'])

    def test_bad_responses_error_envelopes_and_malformed_posts_fail_explicitly(self):
        invalid = ('', '<html>Unavailable</html>', '[]', '{}', '{"posts":null}',
                   '{"error":"login_required","posts":[]}', '{"success":false,"posts":[]}',
                   '{"posts":[null]}', '{"posts":[{}]}', '{"posts":[{"post":{"name":""}}]}')
        for body in invalid:
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_lemmy_results(dict(response(), body=body), SOURCE)
        for limit in (0, True, 51):
            with self.assertRaises(ValueError):
                parse_lemmy_results(response(), SOURCE, max_records=limit)
        with self.assertRaises(ValueError):
            parse_lemmy_results(dict(response(), body=' ' * 2_000_001), SOURCE)

    def test_unapproved_response_instance_fails_and_transport_denials_remain_empty(self):
        fetch = response()
        fetch['final_url'] = 'https://other.example/api/v3/search'
        with self.assertRaises(ValueError):
            parse_lemmy_results(fetch, SOURCE)
        for status in ('blocked', 'deferred', 'error', 'offline'):
            self.assertEqual(parse_lemmy_results(dict(response(), status=status), SOURCE), [])


class SocialPipelineTests(unittest.TestCase):
    def setUp(self):
        from peps.engine import Engine
        from peps.news import NewsAcquirer
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.engine.store.import_entities([ENTITY])
        self.source = dict(SOURCE, enabled=True, terms_approved=True, document_kind='discovery',
                           url=SOURCE['api_url'], download_articles=False, article_terms_approved=False)
        self.publisher = {'id': 'publisher', 'kind': 'rss', 'document_kind': 'news',
                          'url': 'https://editor.example/feed', 'allowed_hosts': ['editor.example'],
                          'enabled': True, 'terms_approved': True, 'download_articles': True,
                          'article_terms_approved': True}

    def observe(self, publisher=False):
        from peps.sources import parse_results
        self.news.config_path.write_text(json.dumps({'sources': [self.source, self.publisher] if publisher
                                                     else [self.source]}))
        fetch = response([post(body='Enrique Peña Nieto fue acusado de fraude. SOCIAL_EXCERPT_ONLY')])
        record = parse_results(fetch, self.source)[0]
        self.news._observe(record, self.source, fetch['url'], fetch, None,
                           candidate=RelevanceIndex([ENTITY]).rank(record['title'], record['text']))

    def test_social_metadata_never_invokes_risk_nlp_or_creates_article_evidence(self):
        self.observe()
        with patch('peps.nlp.analyze', side_effect=AssertionError('UGC is a lead, not an article body')):
            result = self.news.attribute(rescore=False)
        self.assertEqual(result['articles_processed'], 1)
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
            article = con.execute('SELECT scope,document_id,published_at FROM news_articles').fetchone()
        self.assertEqual(article['scope'], 'feed_metadata_only')
        self.assertIsNone(article['document_id'])
        self.assertIsNone(article['published_at'])

    def test_separately_permitted_editorial_body_uses_only_its_own_text_in_nlp(self):
        from peps.nlp import analyze
        self.observe(publisher=True)
        job = self.engine.store.claim(kind_prefix='news_article')
        self.assertIsNotNone(job)
        body = ('Enrique Peña Nieto presentó su informe de actividades públicas. '
                'El documento editorial describe una agenda y datos generales. ') * 3
        fetcher = unittest.mock.Mock()
        fetcher.fetch.return_value = {'status': 'ok', 'body': '<html><title>Informe neutral</title>'
                                      '<article><p>' + body + '</p></article></html>',
                                      'url': 'https://editor.example/story'}
        self.news.execute(job, fetcher)
        with patch('peps.nlp.analyze', wraps=analyze) as nlp:
            self.news.attribute(rescore=False)
        self.assertGreater(nlp.call_count, 0)
        for call in nlp.call_args_list:
            self.assertNotIn('SOCIAL_EXCERPT_ONLY', call.args[0])
            self.assertIn('El documento editorial', call.args[0])
        with self.engine.store.connection() as con:
            article = con.execute('SELECT scope,document_id FROM news_articles').fetchone()
            document = con.execute('SELECT source_kind,metadata FROM documents WHERE id=?',
                                   (article['document_id'],)).fetchone()
        self.assertEqual(article['scope'], 'full_article')
        self.assertEqual(document['source_kind'], 'news')
        self.assertFalse(json.loads(document['metadata']).get('discovery_only', False))


if __name__ == '__main__':
    unittest.main()
