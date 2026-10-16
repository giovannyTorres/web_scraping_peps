"""The live harness must distinguish plans, requests, receipts and source bodies."""
from copy import deepcopy
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'validate_real_people.py'
SPEC = importlib.util.spec_from_file_location('validate_real_people', SCRIPT)
VALIDATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATION)


class RealPeopleValidationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.roster = self.root / 'roster.json'
        self.people = [{'id': 'one', 'name': 'Persona Sintética Uno', 'entity_group': 'municipal_president',
                        'source_url': 'https://public.example/roster', 'context': {'office': 'Presidente municipal'}},
                       {'id': 'two', 'name': 'Persona Sintética Dos', 'entity_group': 'notary',
                        'source_url': 'https://public.example/notaries'}]

    def save(self, people=None):
        self.roster.write_text(json.dumps(self.people if people is None else people), encoding='utf-8')

    def test_small_pilot_is_explicit_and_office_copy_does_not_invent_verification(self):
        self.save()
        with self.assertRaisesRegex(ValueError, '150'):
            VALIDATION.load_roster(self.roster)
        people, metadata = VALIDATION.load_roster(self.roster, allow_small=True)
        self.assertEqual(people[0]['context']['role'], 'Presidente municipal')
        self.assertNotIn('verified_fields', people[0]['context'])
        self.assertEqual(len(metadata['import_hint_adjustments']), 1)
        self.assertNotIn('role', json.loads(self.roster.read_text())[0]['context'])

    def test_missing_provenance_and_duplicate_people_are_rejected(self):
        self.save([self.people[0], deepcopy(self.people[0])])
        with self.assertRaisesRegex(ValueError, 'duplicados'):
            VALIDATION.load_roster(self.roster, allow_small=True)
        people = deepcopy(self.people)
        people[0].pop('source_url')
        self.save(people)
        with self.assertRaisesRegex(ValueError, 'procedencia'):
            VALIDATION.load_roster(self.roster, allow_small=True)

    def test_complete_roster_requires_both_150_person_cohorts(self):
        people = []
        for group in ('municipal_president', 'notary'):
            for number in range(150):
                people.append({'id': f'{group}{number}', 'name': f'Persona Sintética {group}{number}',
                               'entity_group': group, 'source_url': 'https://public.example/roster'})
        self.save({'people': people})
        loaded, metadata = VALIDATION.load_roster(self.roster)
        self.assertEqual(len(loaded), 300)
        self.assertEqual(metadata['groups'], {'municipal_president': 150, 'notary': 150})
        people[-1]['entity_group'] = 'municipal_president'
        self.save(people)
        with self.assertRaisesRegex(ValueError, '150'):
            VALIDATION.load_roster(self.roster)

    def test_search_permissions_never_enable_bodies_from_result_hosts(self):
        args = SimpleNamespace(sources='mwmbl_web,duckduckgo_web', body_sources='contralinea_index',
                               start='2016-01-01', end='2026-01-01')
        config = VALIDATION.make_config(args, 300)
        self.assertEqual(config['search']['max_queries'], 300)
        for source in config['sources']:
            self.assertEqual(source['download_articles'], source['id'] == 'contralinea_index')
            self.assertTrue(source['robots_required'])
            self.assertGreaterEqual(source['min_interval_seconds'], 5)
        args.body_sources = 'arbitrary-result-host'
        with self.assertRaises(ValueError):
            VALIDATION.make_config(args, 300)

    def test_plan_and_http_failure_do_not_become_completed_search_or_body(self):
        engine = Engine(self.root / 'bank')
        NewsAcquirer(engine)
        engine.store.import_entities(self.people)
        url = 'https://mwmbl.org/api/v2/search/?q=Persona+Sint%C3%A9tica+Uno'
        engine.store.enqueue('news_query', {'source_id': 'mwmbl_web', 'url': url,
                                          'entity_ids': ['one']}, key='one-query')
        jobs = VALIDATION.queue_receipt(engine.store)
        rows, _ = VALIDATION.person_report(engine, self.people, jobs,
                                           [{'url': url, 'http_status': 429}])
        self.assertTrue(rows[0]['queried_over_network'])
        self.assertFalse(rows[0]['search_completed'])
        self.assertEqual(rows[0]['search']['queries_planned'], 1)
        self.assertEqual(rows[0]['relevant_full_bodies'], 0)
        self.assertFalse(rows[1]['queried_over_network'])
        self.assertFalse(rows[1]['score_computed'])
        self.assertNotIn('queries_planned', rows[1]['search'])

    def test_cache_copy_mutations_leave_original_queue_and_backoff_intact(self):
        engine = Engine(self.root / 'bank')
        NewsAcquirer(engine)
        engine.store.enqueue('news_query', {'source_id': 'mwmbl_web'}, key='preserved', due=4_000_000_000)
        with engine.store.connection() as con:
            con.execute("UPDATE jobs SET status='deferred',attempts=2,error='HTTP 429'")
        before = VALIDATION.queue_receipt(engine.store)
        target = self.root / 'copy'
        VALIDATION.copy_bank(engine.data_dir, target)
        copied = Engine(target)
        with copied.store.connection() as con:
            con.execute("UPDATE jobs SET status='blocked',due=0,error='local cache miss'")
        self.assertEqual(VALIDATION.queue_receipt(engine.store), before)
        self.assertNotEqual(VALIDATION.queue_receipt(copied.store), before)
        self.assertTrue(engine.store.verify_audit()['ok'])

    def test_exhausted_request_cap_does_not_contact_http_or_penalize_host_or_job(self):
        engine = Engine(self.root / 'bank', online=True)
        news = NewsAcquirer(engine)
        args = SimpleNamespace(max_requests=1)
        events, fetch_events = [{'url': 'https://public.example/earlier'}], []
        fetcher_type = VALIDATION.make_observed_fetcher(engine, args, events, fetch_events, [],
                                                       threading.Lock(), time.monotonic() + 60)
        with fetcher_type({}, engine.data_dir) as fetcher:
            with patch.object(VALIDATION.SafeFetcher, 'fetch', side_effect=AssertionError('HTTP after cap')) as http:
                result = fetcher.fetch('https://public.example/search', {'id': 'mwmbl_web'})
            self.assertEqual(http.call_count, 0)
            self.assertEqual(result['error_code'], 'session_limit')
            self.assertEqual(result['network_requests'], 0)
            self.assertEqual(len(events), 1)
            with self.assertRaises(VALIDATION._ValidationBudgetStopped):
                fetcher._transport('https://public.example/search', {}, 1, 100, [])
        self.assertEqual(VALIDATION.network_state(engine.data_dir)['hosts'], [])
        engine.store.enqueue('news_query', {'source_id': 'mwmbl_web'}, key='limited')
        job = engine.store.claim(kind_prefix='news_')
        self.assertTrue(news._failure(job, result))
        saved = VALIDATION.queue_receipt(engine.store)[0]
        self.assertEqual(saved['attempts'], 0)
        self.assertEqual(saved['status'], 'deferred')

    def test_resume_restores_same_plan_without_requeuing_done_or_resetting_backoff(self):
        self.save()
        people, roster = VALIDATION.load_roster(self.roster, allow_small=True)
        engine = Engine(self.root / 'bank')
        engine.store.import_entities(people)
        engine.store.audit('real_people_validation_started', {'roster_sha256': roster['sha256'], 'groups': roster['groups']})
        news = NewsAcquirer(engine)
        args = SimpleNamespace(sources='mwmbl_web,duckduckgo_web', body_sources='contralinea_index',
                               start='2016-01-01', end='2026-01-01')
        config = VALIDATION.make_config(args, 2)
        news.config_path.write_text(json.dumps(config))
        news.queue('real-people-public')
        with engine.store.connection() as con:
            original = [dict(row) for row in con.execute("SELECT * FROM jobs ORDER BY id")]
            done_id, pending_id = original[0]['id'], original[1]['id']
            future = time.time() + 3600
            con.execute("UPDATE jobs SET status='done',attempts=1 WHERE id=?", (done_id,))
            con.execute("UPDATE jobs SET status='deferred',due=?,attempts=2,error='HTTP 429' WHERE id=?", (future, pending_id))
        shared = deepcopy(config)
        shared['search'].update(strategy='shared', max_queries=2)
        news.config_path.write_text(json.dumps(shared))
        news.queue('real-people-publisher-index')
        self.assertEqual(next(job for job in VALIDATION.queue_receipt(engine.store) if job['id'] == pending_id)['status'], 'cancelled')
        VALIDATION.validate_resume_bank(engine.data_dir, people, roster)
        previous = VALIDATION.validate_resume_plan(news, config)
        self.assertEqual(len(previous), 2)
        news.config_path.write_text(json.dumps(config))
        with patch('socket.getaddrinfo', side_effect=AssertionError('No DNS while preparing resume')) as dns:
            news.queue('real-people-public')
        jobs = {job['id']: job for job in VALIDATION.queue_receipt(engine.store)}
        self.assertEqual(dns.call_count, 0)
        self.assertEqual(jobs[done_id]['status'], 'done')
        self.assertEqual(jobs[done_id]['attempts'], 1)
        self.assertEqual(jobs[pending_id]['status'], 'deferred')
        self.assertEqual(jobs[pending_id]['due'], future)
        self.assertEqual(jobs[pending_id]['attempts'], 2)
        self.assertEqual(sum(job['kind'] == 'news_query' for job in jobs.values()), 2)
        changed = deepcopy(config)
        changed['search']['start'] = '2017-01-01'
        with self.assertRaisesRegex(ValueError, 'plan nominal'):
            VALIDATION.validate_resume_plan(news, changed)
        with engine.store.connection() as con:
            con.execute("UPDATE entities SET name='Nombre cambiado' WHERE id='one'")
        with self.assertRaisesRegex(ValueError, 'han cambiado'):
            VALIDATION.validate_resume_bank(engine.data_dir, people, roster)

    def test_cumulative_http_excludes_cache_only_events_from_person_queries(self):
        engine = Engine(self.root / 'bank')
        NewsAcquirer(engine)
        engine.store.import_entities(self.people)
        url = 'https://mwmbl.org/api/v2/search/?q=Persona+Sint%C3%A9tica+Uno'
        with VALIDATION.SafeFetcher({}, engine.data_dir) as fetcher:
            fetcher._record_event(url, 'content', network_requests=1, http_status=200, outcome='200')
            fetcher._record_event(url, 'content', network_requests=0, outcome='cache_hit', cache_status='local')
            fetcher._record_event('https://mwmbl.org/robots.txt', 'robots', network_requests=1, http_status=200, outcome='200')
        engine.store.enqueue('news_query', {'source_id': 'mwmbl_web', 'url': url, 'entity_ids': ['one']}, key='nominal')
        history = VALIDATION.http_history(engine.data_dir)
        self.assertEqual(len(history), 2)
        rows, _ = VALIDATION.person_report(engine, self.people, VALIDATION.queue_receipt(engine.store), history)
        self.assertEqual(rows[0]['search']['query_http_requests'], 1)
        self.assertFalse(rows[0]['search_completed'])

    def test_all_harness_sqlite_connections_are_closed_without_garbage_collection(self):
        self.save()
        people, roster = VALIDATION.load_roster(self.roster, allow_small=True)
        engine = Engine(self.root / 'bank')
        NewsAcquirer(engine)
        engine.store.import_entities(people)
        engine.store.audit('real_people_validation_started', {'roster_sha256': roster['sha256'], 'groups': roster['groups']})
        with VALIDATION.SafeFetcher({}, engine.data_dir):
            pass
        connections = []
        original_connect = sqlite3.connect

        class TrackedConnection(sqlite3.Connection):
            explicitly_closed = False

            def close(self):
                self.explicitly_closed = True
                return super().close()

        def connect(*args, **kwargs):
            kwargs['factory'] = TrackedConnection
            connection = original_connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with patch.object(VALIDATION.sqlite3, 'connect', side_effect=connect):
            VALIDATION.validate_resume_bank(engine.data_dir, people, roster)
            VALIDATION.http_history(engine.data_dir)
            VALIDATION.network_state(engine.data_dir)
            VALIDATION.copy_bank(engine.data_dir, self.root / 'copied')
        self.assertGreaterEqual(len(connections), 7)
        self.assertTrue(all(connection.explicitly_closed for connection in connections))

    def migration_fixture(self):
        from peps.relevance import RelevanceIndex
        from peps.sources import parse_results
        engine = Engine(self.root / 'migration')
        engine.store.import_entities(self.people)
        news = NewsAcquirer(engine)
        config = VALIDATION.make_config(SimpleNamespace(sources='mwmbl_web', body_sources='',
            start='2016-01-01', end='2026-01-01'), 2)
        news.config_path.write_text(json.dumps(config))
        news.queue('real-people-public')
        source = next(source for source in news.sources() if source['id'] == 'mwmbl_web')
        payload = next(json.loads(job['payload']) for job in VALIDATION.queue_receipt(engine.store)
                       if json.loads(job['payload']).get('entity_ids') == ['one'])
        body = json.dumps({'query': payload['query'], 'number_of_results': 1, 'results': [
            {'url': 'https://public.example/original', 'title': 'Persona Sintética Uno: informe',
             'text': 'Persona Sintética Uno presenta un informe p&uacute;blico &amp; documentado.'}]})
        result = {'status': 'ok', 'url': payload['url'], 'body': body,
                  'sha256': VALIDATION.digest(body), 'retrieved_at': '2026-01-01T00:00:00+00:00'}
        fetched_at = time.time()
        result['validated_at'] = VALIDATION._iso(fetched_at)
        record = parse_results(dict(result), source)[0]
        with VALIDATION.SafeFetcher({}, engine.data_dir):
            pass
        with closing(sqlite3.connect(engine.data_dir / 'network.sqlite3')) as con:
            con.execute('INSERT INTO http_cache(url,body,content_type,sha256,retrieved_at,fetched_at,expires_at,final_url) '
                        'VALUES(?,?,?,?,?,?,?,?)', (payload['url'], body, 'application/json', result['sha256'],
                        result['retrieved_at'], fetched_at, time.time() + 60, payload['url']))
            con.commit()
        candidate = RelevanceIndex(engine.store.iter_entities()).rank(record['title'], record['text'],
            entity_ids=payload['entity_ids'], names=payload['names'])
        candidate['bank_revision'] = '1'
        historical = deepcopy(candidate)
        historical['model_version'] = 'metadata-relevance-1.1.0'
        news._observe(record, source, payload['url'], result, None, candidate=historical)
        before = VALIDATION.corpus_snapshot(engine)
        cache = VALIDATION.cached_snapshot(engine.data_dir)
        news._observe(record, source, payload['url'], result, None, candidate=candidate)
        return engine, news, record, source, payload, result, candidate, before, cache

    def test_versioned_cache_observation_requires_exact_lineage_and_repeat_idempotence(self):
        engine, news, record, source, payload, result, candidate, before, cache = self.migration_fixture()
        after = VALIDATION.corpus_snapshot(engine)
        proof = VALIDATION.verify_cache_enrichment(news, before, after, cache)
        self.assertTrue(proof['passed'], proof)
        self.assertEqual(proof['new_observations'], 1)
        self.assertEqual(proof['new_articles'], 0)
        news._observe(record, source, payload['url'], result, None, candidate=candidate)
        self.assertEqual(VALIDATION.corpus_snapshot(engine), after)
        corrupted = deepcopy(after)
        historical_key = next(iter(before['news_observations']))
        corrupted['news_observations'][historical_key]['retrieved_at'] = 'changed'
        self.assertFalse(VALIDATION.verify_cache_enrichment(news, before, corrupted, cache)['passed'])
        unexplained = deepcopy(after)
        key, row = next((key, row) for key, row in unexplained['news_observations'].items()
                        if key not in before['news_observations'])
        unexplained['news_observations'].pop(key)
        details = json.loads(row['details'])
        details['unexplained_nonce'] = 'invented-audit-field'
        row['details'] = VALIDATION.canonical(details)
        row['retrieved_at'] = '2099-01-01T00:00:00+00:00'
        row['id'] = VALIDATION.digest(VALIDATION.canonical([row['article_id'], row['source_id'],
            row['discovery_url'], row['discovery_hash'], details]))
        unexplained['news_observations'][VALIDATION.canonical([row['id']])] = row
        proof = VALIDATION.verify_cache_enrichment(news, before, unexplained, cache)
        self.assertTrue(proof['checks']['observation_ids_match_production_content_hash'])
        self.assertFalse(proof['passed'])
        self.assertFalse(proof['checks']['every_new_observation_reproduced_from_original_cache_current_model_and_policy'])

    def test_model_migration_does_not_excuse_same_model_duplicates_or_invented_cache(self):
        engine, news, _record, _source, _payload, _result, _candidate, before, cache = self.migration_fixture()
        after = VALIDATION.corpus_snapshot(engine)
        extra = deepcopy(next(row for row in after['news_observations'].values()
                              if json.loads(row['details'])['retrieval_relevance']['model_version'] != 'metadata-relevance-1.1.0'))
        details = json.loads(extra['details'])
        details['unexplained_nonce'] = 'duplicate'
        extra['details'] = VALIDATION.canonical(details)
        extra['id'] = VALIDATION.digest(VALIDATION.canonical([extra['article_id'], extra['source_id'],
            extra['discovery_url'], extra['discovery_hash'], details]))
        after['news_observations'][VALIDATION.canonical([extra['id']])] = extra
        proof = VALIDATION.verify_cache_enrichment(news, before, after, cache)
        self.assertFalse(proof['passed'])
        self.assertFalse(proof['checks']['no_same_model_snapshot_policy_observation_duplicates'])
        proof = VALIDATION.verify_cache_enrichment(news, before, VALIDATION.corpus_snapshot(engine), {})
        self.assertFalse(proof['passed'])
        self.assertFalse(proof['checks']['every_new_observation_reproduced_from_original_cache_current_model_and_policy'])
        current_only_before = deepcopy(before)
        current_only_before['news_observations'] = {}
        current_only_before['news_articles'] = {}
        current_only_after = VALIDATION.corpus_snapshot(engine)
        current_only_after['news_observations'] = {key: row for key, row in current_only_after['news_observations'].items()
            if json.loads(row['details'])['retrieval_relevance']['model_version'] != 'metadata-relevance-1.1.0'}
        proof = VALIDATION.verify_cache_enrichment(news, current_only_before, current_only_after, cache)
        self.assertTrue(proof['checks']['every_new_observation_reproduced_from_original_cache_current_model_and_policy'])
        self.assertFalse(proof['checks']['older_model_present_if_metadata_grows'])
        self.assertFalse(proof['passed'])
        with closing(sqlite3.connect(engine.data_dir / 'network.sqlite3')) as con:
            con.execute("UPDATE http_cache SET body=body||' '")
            con.commit()
        corrupted_cache = VALIDATION.cached_snapshot(engine.data_dir)
        proof = VALIDATION.verify_cache_enrichment(news, before, VALIDATION.corpus_snapshot(engine), corrupted_cache)
        self.assertTrue(proof['checks']['cached_http_bytes_and_metadata_unchanged'])
        self.assertFalse(proof['passed'])
        self.assertFalse(proof['checks']['every_new_observation_reproduced_from_original_cache_current_model_and_policy'])
        self.assertTrue(any('raw UTF-8/ASCII SHA256' in violation['reason'] for violation in proof['violations']))


if __name__ == '__main__':
    unittest.main()
