"""Exact publication filtering when publisher search returns broader dates."""
import json
import tempfile
import unittest
from unittest.mock import Mock

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.relevance import RelevanceIndex
from peps.shared_crawl import in_period, publication_period_status
from peps.store import digest


class PublicationPeriodTests(unittest.TestCase):
    def test_compact_bounds_are_half_open_and_use_utc_offsets(self):
        payload = {'start': '20220210000000', 'end': '20220309000000'}
        for value, expected in (
                ('2022-02-09T23:59:59Z', False),
                ('2022-02-10T00:00:00Z', True),
                ('2022-03-08T23:59:59Z', True),
                ('2022-03-09T00:00:00Z', False),
                ('2022-03-09T01:00:00+02:00', True),
                ('2022-02-10T01:00:00+02:00', False),
                ('2021-12-31T23:59:59Z', False),
                ('2023-01-01T00:00:00Z', False)):
            with self.subTest(value=value):
                self.assertEqual(in_period({'published_at': value}, payload), expected)

    def test_shared_iso_bounds_and_unknown_dates_keep_their_explicit_meaning(self):
        payload = {'start': '2020-01-01T00:00:00+00:00', 'end': '2021-01-01T00:00:00+00:00'}
        self.assertTrue(in_period({'published_at': '2020-06-01T12:00:00'}, payload))
        self.assertFalse(in_period({'published_at': '2021-01-01T00:00:00Z'}, payload))
        for value in (None, '', 'not-a-date'):
            record = {'published_at': value, 'lastmod': '2023-01-01T00:00:00Z'}
            self.assertTrue(in_period(record, payload))
            self.assertEqual(publication_period_status(record, payload), 'publication_date_unknown')

    def test_unknown_bounds_are_explicit_and_never_claim_a_dated_match(self):
        record = {'published_at': '2022-02-01T00:00:00Z'}
        for payload in ({}, {'start': 'invalid', 'end': '2023-01-01'},
                        {'start': '20230101000000', 'end': '20220101000000'}):
            self.assertTrue(in_period(record, payload))
            self.assertEqual(publication_period_status(record, payload), 'period_bounds_unknown')

    def test_targeted_rss_excludes_outside_dates_before_observation_and_preserves_prior_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, online=True)
            news = NewsAcquirer(engine)
            entity = {'id': 'person', 'name': 'Enrique Peña Nieto', 'kind': 'person'}
            engine.store.import_entities([entity])
            index = RelevanceIndex([entity])
            source = {'id': 'publisher', 'kind': 'rss', 'search_kind': 'wordpress_rss',
                      'url': 'https://publisher.invalid/feed/', 'search_url': 'https://publisher.invalid/',
                      'allowed_hosts': ['publisher.invalid'], 'enabled': True, 'terms_approved': True,
                      'download_articles': False, 'robots_required': False, 'min_interval_seconds': 0,
                      'search_max_records': 100}
            settings = {'strategy': 'targeted', 'execution_mode': 'quick', 'preset': 'identity',
                        'start': '2022-02-10', 'end': '2022-03-08',
                        'include_aliases': False, 'use_context': False}
            news.config_path.write_text(json.dumps({'sources': [source], 'search': settings}))
            # An older preserved article must remain auditable; the new period
            # filters new discoveries without deleting the existing corpus.
            legacy = {'url': 'https://publisher.invalid/legacy', 'title': entity['name'] + ' archivo',
                      'text': '', 'published_at': '2020-01-01T00:00:00Z'}
            news._observe(legacy, source, source['url'], {}, None,
                          candidate=index.rank(legacy['title'], ''))
            news.queue('partial-period')
            job = engine.store.claim(kind_prefix='news_query')
            self.assertEqual((job['payload']['start'], job['payload']['end']),
                             ('20220210000000', '20220309000000'))
            rows = [('before', '2022-02-09T23:59:59Z'), ('start', '2022-02-10T00:00:00Z'),
                    ('last', '2022-03-08T23:59:59Z'), ('end', '2022-03-09T00:00:00Z'),
                    ('old', '2020-12-31T12:00:00Z'), ('future', '2026-01-01T00:00:00Z'),
                    ('unknown', None), ('invalid', 'not-a-date')]
            items = ''.join('<item><title>' + entity['name'] + ' ' + key + '</title><link>'
                            'https://publisher.invalid/' + key + '</link>'
                            + ('<pubDate>' + date + '</pubDate>' if date else '') + '</item>'
                            for key, date in rows)
            body = '<rss><channel><title>Archivo</title>' + items + '</channel></rss>'
            fetcher = Mock()
            fetcher.fetch.return_value = {'status': 'ok', 'body': body, 'url': job['payload']['url'],
                                          'sha256': digest(body), 'retrieved_at': '2026-10-16T00:00:00Z'}
            news.execute(job, fetcher, relevance=index)
            with engine.store.connection() as con:
                urls = {row[0] for row in con.execute('SELECT canonical_url FROM news_articles')}
                metadata = json.loads(con.execute('SELECT parser_metadata FROM news_discovery_runs').fetchone()[0])
                documents = con.execute('SELECT count(*) FROM documents').fetchone()[0]
            self.assertEqual(urls, {'https://publisher.invalid/' + key
                                    for key in ('legacy', 'start', 'last', 'unknown', 'invalid')})
            self.assertEqual(documents, 0)
            period = metadata['publication_period_filter']
            self.assertEqual(period['excluded_records'], 4)
            self.assertEqual(period['unknown_date_records'], 2)
            self.assertEqual(period['unknown_bound_records'], 0)
            self.assertTrue(period['unknown_dates_are_candidates_not_verified_period_coverage'])
            self.assertEqual(metadata['candidate_filter']['retained_records'], 4)
            self.assertTrue(engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
