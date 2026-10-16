"""Recovery and bounded news sessions use real SQLite jobs and fixture HTTP."""
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now


class NoNetworkFetcher:
    def __init__(self, *_args, **_kwargs):
        pass

    def fetch(self, *_args, **_kwargs):
        raise AssertionError('This runtime fixture must not access a source')


class NewsRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.source = {'id': 'fixture', 'kind': 'rss', 'allowed_hosts': ['example.org'],
                       'enabled': True, 'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'url': 'https://example.org/feed'}
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': 'feeds'}}), encoding='utf-8')

    def run_fixture(self, **kwargs):
        kwargs.setdefault('seconds', 1)
        with patch('peps.collector.SafeFetcher', NoNetworkFetcher):
            return self.news.acquire(queue=False, **kwargs)

    def query_job(self, *, split_policy='expand'):
        payload = {'source_id': 'fixture', 'query': '"Persona Pública Sintética"',
                   'entity_ids': ['target'], 'names': ['Persona Pública Sintética'],
                   'start': '20200101000000', 'end': '20210101000000',
                   'result_limit': 2, 'split_policy': split_policy}
        job_id = self.store.enqueue('news_query', payload)
        with self.store.connection() as con:
            con.execute('INSERT INTO news_query_windows VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (job_id, 'fixture', json.dumps(payload['entity_ids']), payload['query'],
                         payload['start'], payload['end'], 'queued', 0, 0, None, ''))
        return job_id

    def configure_targeted(self):
        self.store.import_entities([{'id': 'target', 'name': 'Persona Pública Sintética'},
                                    {'id': 'other', 'name': 'Otra Persona Sintética'}])
        self.source.update(search_kind='wordpress_rss', search_url='https://example.org/',
                           max_records=250, min_window_seconds=86400)
        self.news.config_path.write_text(json.dumps({'sources': [self.source],
                                                     'search': {'strategy': 'targeted'}}), encoding='utf-8')

    def yearly_plan(self, year, mode='extended'):
        return self.news.set_search_config({'start': f'{year}-01-01', 'end': f'{year}-12-31',
                    'window': 'year', 'max_queries': 2, 'execution_mode': mode,
                    'session_seconds': 120 if mode == 'quick' else 900,
                    'include_aliases': False, 'use_context': False})

    def test_new_period_and_profile_retire_old_discovery_but_preserve_bodies_and_live_leases(self):
        self.configure_targeted()
        self.yearly_plan(2020)
        self.news.queue('replace-plan')
        with self.store.connection() as con:
            old = con.execute("SELECT id,due FROM jobs WHERE kind='news_query' ORDER BY id").fetchall()
            con.execute("UPDATE jobs SET status='running',owner='live-owner',lease_until=?,attempts=1 WHERE id=?",
                        (time.time() + 3600, old[0]['id']))
            con.execute("UPDATE jobs SET status='running',owner='expired-owner',lease_until=?,attempts=2 WHERE id=?",
                        (time.time() - 1, old[1]['id']))
        feed = self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']}, due=0)
        body = self.store.enqueue('news_article', {'source_id': 'fixture', 'article_id': 'retained', 'url': 'https://example.org/body'}, due=0)
        self.yearly_plan(2025, 'quick')
        replacement = self.news.queue('replace-plan')
        self.assertEqual(replacement['superseded'], 2)
        with self.store.connection() as con:
            jobs = {row['id']: dict(row) for row in con.execute('SELECT * FROM jobs')}
            window = con.execute('SELECT status FROM news_query_windows WHERE job_id=?', (old[1]['id'],)).fetchone()
        self.assertEqual(jobs[old[0]['id']]['status'], 'running')
        self.assertEqual(jobs[old[0]['id']]['owner'], 'live-owner')
        self.assertEqual(jobs[old[1]['id']]['status'], 'cancelled')
        self.assertIsNone(jobs[old[1]['id']]['owner'])
        self.assertEqual(jobs[old[1]['id']]['attempts'], 2)
        self.assertEqual(jobs[old[1]['id']]['due'], old[1]['due'])
        self.assertEqual(window['status'], 'cancelled')
        self.assertEqual(jobs[feed]['status'], 'cancelled')
        self.assertEqual(jobs[body]['status'], 'queued')
        selected = self.store.claim(kind_prefix='news_query')
        self.assertNotIn(selected['id'], {row['id'] for row in old})
        self.assertTrue(selected['payload']['start'].startswith('2025'))
        self.assertEqual(selected['payload']['split_policy'], 'defer')
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_same_plan_and_reactivation_preserve_retry_after_attempts_and_transport_error(self):
        self.configure_targeted()
        old_settings = self.yearly_plan(2020)
        self.news.queue('retry-plan')
        with self.store.connection() as con:
            old_id = con.execute("SELECT id FROM jobs WHERE kind='news_query' ORDER BY id LIMIT 1").fetchone()[0]
            due = time.time() + 3600
            con.execute("UPDATE jobs SET status='deferred',attempts=3,due=?,error='HTTP 429: Retry-After' WHERE id=?", (due, old_id))
        same = self.news.queue('retry-plan')
        self.assertEqual(same['superseded'], 0)
        with self.store.connection() as con:
            before = dict(con.execute('SELECT * FROM jobs WHERE id=?', (old_id,)).fetchone())
        self.yearly_plan(2021)
        self.news.queue('retry-plan')
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (old_id,)).fetchone()[0], 'cancelled')
        self.news.set_search_config(old_settings)
        restored = self.news.queue('retry-plan')
        self.assertEqual(restored['reactivated'], 2)
        with self.store.connection() as con:
            after = dict(con.execute('SELECT * FROM jobs WHERE id=?', (old_id,)).fetchone())
        for field in ('due', 'attempts', 'error', 'status', 'payload'):
            self.assertEqual(after[field], before[field], field)
        self.assertIsNone(after['owner'])
        self.assertIsNone(after['lease_until'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_resume_without_queue_keeps_old_discovery_unchanged_even_after_config_changes(self):
        self.configure_targeted()
        self.yearly_plan(2020)
        self.news.queue('resume-plan')
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='deferred',due=?,attempts=2,error='HTTP 429' WHERE kind='news_query'", (time.time() + 3600,))
            before = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.yearly_plan(2025, 'quick')
        result = self.run_fixture(seconds=0)
        with self.store.connection() as con:
            after = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.assertIsNone(result['queued'])
        self.assertEqual(after, before)

    def test_discovery_replacement_rolls_back_if_its_audit_cannot_commit(self):
        self.configure_targeted()
        self.yearly_plan(2020)
        self.news.queue('atomic-plan')
        with self.store.connection() as con:
            before = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.yearly_plan(2025, 'quick')
        audit = self.store._audit
        def reject(con, action, payload):
            if action == 'news_discovery_plan_superseded':
                raise RuntimeError('Synthetic audit failure')
            return audit(con, action, payload)
        with patch.object(self.store, '_audit', side_effect=reject), self.assertRaisesRegex(RuntimeError, 'Synthetic audit failure'):
            self.news.queue('atomic-plan')
        with self.store.connection() as con:
            after = [tuple(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        self.assertEqual(after, before)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_targeted_query_discards_irrelevant_metadata_even_when_download_all_is_requested(self):
        self.configure_targeted()
        self.query_job(split_policy='defer')
        body = '<rss><channel>' + ''.join(
            f'<item><title>{title}</title><link>https://example.org/r-{index}</link></item>'
            for index, title in enumerate(('Persona Pública Sintética presenta informe',
                                            'Otra Persona Sintética presenta informe',
                                            'Noticias generales de economía'))) + '</channel></rss>'
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                return dict(status='ok', url=url, body=body)
        with patch('peps.collector.SafeFetcher', Fetcher):
            result = self.news.acquire(queue=False, workers=1, seconds=2, max_articles=1, download_all=True)
        self.assertEqual(result['discovery_records'], 3)
        self.assertEqual(result['relevant_records'], 1)
        self.assertEqual(result['excluded_records'], 2)
        self.assertEqual(self.news.stats()['articles'], 1)
        self.assertEqual(self.news.articles()[0]['title'], 'Persona Pública Sintética presenta informe')
        self.assertEqual(self.store.stats()['jobs'], {'done': 1, 'queued': 1})
        self.assertEqual(self.news.stats()['identity_links'], 0)
        with self.store.connection() as con:
            details = json.loads(con.execute('SELECT details FROM news_observations').fetchone()[0])
            run = json.loads(con.execute('SELECT parser_metadata FROM news_discovery_runs').fetchone()[0])
            window = con.execute('SELECT status,error FROM news_query_windows').fetchone()
        self.assertFalse(details['retrieval_relevance']['identity_verified'])
        self.assertEqual(details['retrieval_relevance']['score_effect'], 'none')
        self.assertEqual(run['candidate_filter']['excluded_records'], 2)
        self.assertEqual(window['status'], 'truncated')
        self.assertIn('barrido inicial', window['error'])

    def test_irrelevant_saturated_query_does_not_expand_into_repeated_months_and_days(self):
        self.configure_targeted()
        self.query_job()
        body = '<rss><channel>' + ''.join(
            f'<item><title>Noticia general {index}</title><link>https://example.org/g-{index}</link></item>'
            for index in range(2)) + '</channel></rss>'
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                return dict(status='ok', url=url, body=body)
        events = []
        with patch('peps.collector.SafeFetcher', Fetcher):
            result = self.news.acquire(queue=False, workers=1, seconds=2, progress=events.append)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(result['excluded_records'], 2)
        self.assertEqual(self.news.stats()['articles'], 0)
        self.assertEqual(self.store.stats()['jobs'], {'done': 1})
        with self.store.connection() as con:
            window = con.execute('SELECT status,saturated,error FROM news_query_windows').fetchone()
        self.assertEqual(window['status'], 'truncated')
        self.assertEqual(window['saturated'], 1)
        self.assertIn('sin candidatos pertinentes', window['error'])
        self.assertEqual(events[-1]['excluded_records'], 2)

    def test_targeted_resume_filters_legacy_general_feed_and_keeps_explicit_feed_mode_compatible(self):
        self.configure_targeted()
        self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        body = ('<rss><channel><item><title>Noticia general</title>'
                '<link>https://example.org/general</link></item></channel></rss>')
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                return dict(status='ok', url=url, body=body)
        with patch('peps.collector.SafeFetcher', Fetcher):
            targeted = self.news.acquire(queue=False, workers=1, seconds=2)
            self.news.config_path.write_text(json.dumps({'sources': [self.source],
                                                         'search': {'strategy': 'feeds'}}), encoding='utf-8')
            self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']}, key='explicit-feed')
            explicit = self.news.acquire(queue=False, workers=1, seconds=2)
        self.assertEqual(targeted['excluded_records'], 1)
        self.assertEqual(explicit['excluded_records'], 0)
        self.assertEqual(explicit['relevant_records'], 0)
        self.assertEqual(self.news.stats()['articles'], 1)

    def test_body_budget_limits_each_entity_and_is_preserved_on_resume(self):
        self.configure_targeted()
        self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        body = '<rss><channel>' + ''.join(
            f'<item><title>{name} informe {index}</title><link>https://example.org/b-{index}</link></item>'
            for index, name in enumerate(['Persona Pública Sintética'] * 4 + ['Otra Persona Sintética'] * 2)) + '</channel></rss>'
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                return dict(status='ok', url=url, body=body)
        with patch('peps.collector.SafeFetcher', Fetcher):
            first = self.news.acquire(queue=False, workers=1, seconds=2, max_articles=1,
                                      body_budget=3, per_entity_body_limit=2)
            second = self.news.acquire(queue=False, workers=1, seconds=2, max_articles=0,
                                       body_budget=3, per_entity_body_limit=2)
        self.assertEqual(first['body_queued'], 3)
        self.assertEqual(first['body_skipped_budget'], 3)
        self.assertEqual(second['body_queued'], 0)
        self.assertEqual(second['pending'], 3)
        with self.store.connection() as con:
            payloads = [json.loads(row[0]) for row in con.execute("SELECT payload FROM jobs WHERE kind='news_article'")]
        identities = [payload['candidate_entity_ids'][0] for payload in payloads]
        self.assertEqual(identities.count('target'), 2)
        self.assertEqual(identities.count('other'), 1)

    def test_resume_recovers_stored_candidate_after_body_permission_without_new_search(self):
        self.configure_targeted()
        self.source['article_terms_approved'] = False
        self.news.config_path.write_text(json.dumps({'sources': [self.source],
                                                     'search': {'strategy': 'targeted'}}), encoding='utf-8')
        self.news._observe({'url': 'https://example.org/stored', 'title': 'Informe municipal'},
                           self.source, self.source['url'], {}, None)
        self.news._observe({'url': 'https://example.org/stored', 'title': 'Persona Pública Sintética informe',
                            'text': 'Resumen de la sesión.'}, self.source, self.source['url'], {}, None)
        calls = []
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                calls.append(url)
                body = '<article>' + ('Persona Pública Sintética presentó su informe público. ' * 5) + '</article>'
                return dict(status='ok', url=url, body=body, retrieved_at=now())
        with patch('peps.collector.SafeFetcher', Fetcher), patch.object(self.news, 'queue', side_effect=AssertionError('No new search')):
            before = self.news.acquire(queue=False, workers=1, seconds=2)
            self.assertEqual(calls, [])
            self.source['article_terms_approved'] = True
            self.news.config_path.write_text(json.dumps({'sources': [self.source],
                                                         'search': {'strategy': 'targeted'}}), encoding='utf-8')
            after = self.news.acquire(queue=False, workers=1, seconds=2)
        self.assertEqual(before['completed'], 0)
        self.assertEqual(after['completed'], 1)
        self.assertEqual(after['body_queued'], 1)
        self.assertEqual(calls, ['https://example.org/stored'])
        self.assertEqual(self.news.stats()['scopes'], {'full_article': 1})

    def test_repeated_discovery_counts_one_new_body_job_and_preserves_server_backoff(self):
        self.configure_targeted()
        for index in range(2):
            self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']}, key=f'repeat-{index}', due=0)
        body = ('<rss><channel><item><title>Persona Pública Sintética informe</title>'
                '<link>https://example.org/repeated</link></item></channel></rss>')
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                return dict(status='ok', url=url, body=body)
        # Exercise duplicate discovery explicitly. Acquisition now prioritizes
        # a ready original over the second feed, so this test must not depend
        # on that former scheduling order to establish a Retry-After receipt.
        from peps.relevance import RelevanceIndex
        receipts = []
        for _ in range(2):
            feed_job = self.store.claim(kind_prefix='news_feed')
            receipts.append(self.news.execute(feed_job, Fetcher(),
                relevance=RelevanceIndex(self.store.iter_entities()))['discovery_metadata'])
        self.assertEqual(sum(receipt['body_budget']['queued'] for receipt in receipts), 1)
        self.assertEqual(sum(receipt['candidate_filter']['retained_records'] for receipt in receipts), 2)
        job = self.store.claim(kind_prefix='news_article')
        due = time.time() + 3600
        self.store.finish(job, 'deferred', 'HTTP 429: Retry-After', due)
        with patch('peps.collector.SafeFetcher', NoNetworkFetcher):
            resumed = self.news.acquire(queue=False, workers=1, seconds=2)
        self.assertEqual(resumed['completed'], 0)
        self.assertEqual(resumed['body_queued'], 0)
        self.assertEqual(resumed['next_retry_at'], due)
        with self.store.connection() as con:
            persisted = con.execute('SELECT due,attempts FROM jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(persisted['due'], due)
        self.assertEqual(persisted['attempts'], 1)

    def test_legacy_irrelevant_body_queue_finishes_without_request_and_retains_audit_metadata(self):
        self.configure_targeted()
        self.news._observe({'url': 'https://example.org/legacy-general', 'title': 'Noticias generales de economía',
                            'text': 'Información ajena a las entidades.'}, self.source, self.source['url'], {}, None, True)
        self.assertEqual(len(self.news.articles()), 1)
        with patch('peps.collector.SafeFetcher', NoNetworkFetcher):
            result = self.news.acquire(queue=False, workers=1, seconds=2, relevant_only=True)
        self.assertEqual(result['body_skipped_irrelevant'], 1)
        self.assertEqual(result['completed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(self.store.stats()['documents'], 0)
        self.assertEqual(len(self.news.articles()), 1)
        self.assertEqual(self.news.articles(relevant_only=True), [])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_relevant_article_view_uses_current_bank_ids_and_keeps_legacy_rows(self):
        self.configure_targeted()
        self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        body = ('<rss><channel><item><title>Persona Pública Sintética informe</title>'
                '<link>https://example.org/relevant</link></item></channel></rss>')
        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                return dict(status='ok', url=url, body=body)
        with patch('peps.collector.SafeFetcher', Fetcher):
            self.news.acquire(queue=False, workers=1, seconds=2, max_articles=1)
        self.news._observe({'url': 'https://example.org/retained-general', 'title': 'Noticia general'},
                           self.source, self.source['url'], {}, None)
        self.assertEqual(len(self.news.articles()), 2)
        self.assertEqual([row['canonical_url'] for row in self.news.articles(relevant_only=True)],
                         ['https://example.org/relevant'])

    def test_useful_new_observation_of_a_legacy_url_is_not_rejected_by_its_old_card(self):
        self.configure_targeted()
        url = 'https://example.org/same-canonical'
        self.news._observe({'url': url, 'title': 'Informe municipal'}, self.source, self.source['url'], {}, None)
        self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        class Fetcher(NoNetworkFetcher):
            def fetch(self, requested, _source):
                body = ('<rss><channel><item><title>Persona Pública Sintética presenta informe</title>'
                        f'<link>{url}</link></item></channel></rss>') if requested.endswith('/feed') else (
                        '<article>' + ('Persona Pública Sintética presentó su informe público. ' * 5) + '</article>')
                return dict(status='ok', url=requested, body=body, retrieved_at=now())
        with patch('peps.collector.SafeFetcher', Fetcher):
            result = self.news.acquire(queue=False, workers=1, seconds=2)
        self.assertEqual(result['completed'], 2)
        self.assertEqual(result['body_skipped_irrelevant'], 0)
        self.assertEqual(self.news.stats()['articles'], 1)
        self.assertEqual(self.news.stats()['scopes'], {'full_article': 1})

    def test_task_stages_report_real_fetch_and_discovery_time_after_commit(self):
        self.source['source_name'] = 'Fuente sintética legible'
        self.news.config_path.write_text(json.dumps({'sources': [self.source], 'search': {'strategy': 'feeds'}}), encoding='utf-8')
        self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        events, persisted = [], []

        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, source):
                return {'url': url, 'status': 'ok', 'body': '<rss><channel><item><title>Informe</title>'
                        '<link>https://example.org/task</link></item></channel></rss>'}

        def observe(event):
            events.append(event)
            if event.get('task_event') == 'finish':
                persisted.append(self.news.stats()['articles'])

        with patch('peps.collector.SafeFetcher', Fetcher):
            result = self.news.acquire(queue=False, seconds=2, workers=1, progress=observe)
        tasks = [event for event in events if event.get('task_event')]
        self.assertEqual([event['task_event'] for event in tasks], ['start', 'stage', 'finish'])
        self.assertEqual([event['task_stage'] for event in tasks], ['fetch', 'discovery', 'discovery'])
        self.assertEqual([event['completed'] for event in tasks], [0, 0, 1])
        self.assertTrue(all('Fuente sintética legible' in event['task_label'] for event in tasks))
        self.assertEqual(persisted, [1])
        self.assertEqual(result['completed'], 1)
        final = tasks[-1]
        self.assertGreaterEqual(final['task_timings']['fetch'], 0)
        self.assertAlmostEqual(sum(final['task_timings'].values()), final['task_seconds'])
        self.assertEqual(final['task_status'], 'done')

    def test_exhausted_queued_and_deferred_jobs_are_retired_instead_of_remaining_pending(self):
        for state in ('queued', 'deferred'):
            job_id = self.store.enqueue('news_feed', {'state': state}, key=state)
            with self.store.connection() as con:
                con.execute('UPDATE jobs SET status=?,attempts=5 WHERE id=?', (state, job_id))
        result = self.run_fixture()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(result['dead'], 2)
        self.assertEqual(result['stop_reason'], 'completed')
        self.assertIn('2 tareas con error', result['message'])
        self.assertIn('cobertura disponible', result['message'])
        self.assertTrue(self.store.verify_audit()['ok'])
        self.assertEqual(self.store.retry_failed(), 2)
        self.assertIsNotNone(self.store.claim(kind_prefix='news_'))

    def test_live_lease_is_preserved_and_its_expiry_is_reported(self):
        self.store.enqueue('news_feed', {'state': 'other-worker'})
        job = self.store.claim(lease_seconds=3600, kind_prefix='news_')
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET attempts=5 WHERE id=?', (job['id'],))
        result = self.run_fixture()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['stop_reason'], 'external_lease')
        self.assertTrue(result['waiting'])
        self.assertGreater(result['next_retry_at'], time.time() + 3500)
        self.assertTrue(self.store.finish(job))

    def test_waiting_can_be_cancelled_without_network_or_fabricated_progress(self):
        self.store.enqueue('news_feed', {'source_id': 'fixture'}, due=time.time() + .5)
        events = []

        def observe(event):
            events.append(event)
            if event['event'] == 'activity' and event['waiting']:
                self.engine.stop_event.set()

        start = time.monotonic()
        result = self.run_fixture(progress=observe, workers=1)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(result['stop_reason'], 'cancelled')
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertTrue(any(event['waiting'] and event['event'] == 'activity' for event in events))
        self.assertTrue(all(event['completed'] == 0 for event in events))

    def test_expired_session_leaves_ready_jobs_with_explicit_reason(self):
        self.store.enqueue('news_feed', {'source_id': 'fixture'})
        result = self.run_fixture(seconds=0)
        self.assertEqual(result['stop_reason'], 'time_limit')
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['ready'], 1)
        self.assertFalse(result['waiting'])

    def test_resume_reports_server_backoff_and_never_queues_duplicate_discovery(self):
        job_id = self.store.enqueue('news_feed', {'source_id': 'fixture', 'url': self.source['url']})
        job = self.store.claim(kind_prefix='news_')
        due = time.time() + 3600
        self.store.finish(job, 'deferred', 'HTTP 429: Retry-After', due)
        with patch.object(self.news, 'queue', side_effect=AssertionError('Resume must not queue a new search')):
            result = self.run_fixture()
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['pending'], 1)
        self.assertEqual(result['stop_reason'], 'backoff')
        self.assertEqual(result['next_retry_at'], due)
        self.assertIn('fuente exige esperar', result['message'])
        with self.store.connection() as con:
            row = con.execute('SELECT attempts,due FROM jobs WHERE id=?', (job_id,)).fetchone()
        self.assertEqual(row['attempts'], 1)
        self.assertEqual(row['due'], due)

    def test_single_feed_keeps_all_workers_available_for_new_article_jobs(self):
        barrier = threading.Barrier(3)
        threads = set()
        threads_lock = threading.Lock()

        class Fetcher(NoNetworkFetcher):
            def fetch(self, url, _source):
                if url.endswith('/feed'):
                    body = '<rss><channel>' + ''.join(
                        f'<item><title>Informe {index}</title><link>https://example.org/a-{index}</link></item>'
                        for index in range(6)) + '</channel></rss>'
                else:
                    with threads_lock:
                        threads.add(threading.get_ident())
                    barrier.wait(timeout=2)
                    body = '<article>' + ('Información pública sobre la sesión ordinaria. ' * 5) + url + '</article>'
                return dict(status='ok', url=url, body=body, sha256=digest(body), retrieved_at=now())

        with patch('peps.collector.SafeFetcher', Fetcher):
            result = self.news.acquire(workers=3, seconds=4, download_all=True)
        self.assertEqual(result['completed'], 7)
        self.assertEqual(result['pending'], 0)
        self.assertEqual(len(threads), 3)

    def test_job_limit_preserves_ready_jobs_for_resume_with_a_reason(self):
        for index in range(3):
            self.store.enqueue('news_fixture', {'index': index})

        def execute(job, *_):
            self.store.finish(job)

        with patch.object(self.news, 'execute', side_effect=execute):
            first = self.run_fixture(max_articles=1, workers=1)
            rest = self.run_fixture(max_articles=3, workers=1)
        self.assertEqual(first['completed'], 1)
        self.assertEqual(first['pending'], 2)
        self.assertEqual(first['ready'], 2)
        self.assertEqual(first['stop_reason'], 'job_limit')
        self.assertFalse(first['waiting'])
        self.assertEqual(rest['completed'], 2)
        self.assertEqual(rest['pending'], 0)

    def test_missing_lease_is_recoverable_and_fifth_failure_has_terminal_article_status(self):
        job_id = self.store.enqueue('news_fixture', {})
        with self.store.connection() as con:
            con.execute("UPDATE jobs SET status='running',owner='lost',attempts=1,lease_until=NULL WHERE id=?", (job_id,))
        recovered = self.store.claim(kind_prefix='news_')
        self.assertEqual(recovered['id'], job_id)
        self.assertEqual(recovered['attempts'], 2)
        self.store.finish(recovered)
        self.news._observe({'url': 'https://example.org/failed', 'title': 'Informe', 'text': 'Resumen'},
                           self.source, self.source['url'], {'retrieved_at': now(), 'sha256': 'fixture'}, None, True)
        article_job = self.store.claim(kind_prefix='news_')
        with self.store.connection() as con:
            con.execute('UPDATE jobs SET attempts=5 WHERE id=?', (article_job['id'],))
        article_job['attempts'] = 5
        self.news._failure(article_job, {'status': 'error', 'error': 'Transport timeout'})
        article = self.news.articles()[0]
        self.assertEqual(article['status'], 'dead')
        self.assertEqual(self.store.stats()['jobs'], {'dead': 1, 'done': 1})


if __name__ == '__main__':
    unittest.main()
