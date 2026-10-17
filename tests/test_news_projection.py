"""Current-bank coverage is a derived cache, never historical ID membership."""
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from peps.acquisition_coverage import acquisition_coverage
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.news_projection import PROJECTION_VERSION, ensure_projection, projection_ready, update_projection
from peps.relevance import RelevanceIndex, MODEL_VERSION
from peps.store import canonical, now


class NewsProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name)
        self.store = self.engine.store
        self.news = NewsAcquirer(self.engine)
        self.entities = [{'id': 'one', 'name': 'Primera Persona Sintética'},
                         {'id': 'two', 'name': 'Segunda Persona Sintética'}]
        self.store.import_entities(self.entities)
        self.serial = 0

    def index(self):
        return RelevanceIndex(self.store.iter_entities())

    def article(self, title='Primera Persona Sintética compareció.', summary=''):
        self.serial += 1
        article = f'article-{self.serial:05}'
        with self.store.connection() as con:
            con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,scope,status) '
                        'VALUES(?,?,?,?,?,?,?,?,?)',
                        (article, f'https://synthetic.invalid/{article}', 'synthetic', 'synthetic', title, summary,
                         now(), 'feed_metadata_only', 'discovered'))
        return article

    def observe(self, article, title, *, update=True, ids=None):
        self.serial += 1
        candidate = self.index().rank(title, '')
        if ids is not None:
            candidate.update(relevant=bool(ids), entity_ids=ids)
        with self.store.connection() as con:
            con.execute('INSERT INTO news_observations(id,article_id,source_id,discovery_url,observed_url,retrieved_at,details) '
                        'VALUES(?,?,?,?,?,?,?)', (f'observation-{self.serial:05}', article, 'synthetic',
                        'https://synthetic.invalid/feed', f'https://synthetic.invalid/{article}', now(),
                        canonical({'title': title, 'summary': '', 'retrieval_relevance': candidate})))
            if update:
                revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
                update_projection(con, article, candidate, bank_revision=revision)

    def coverage(self):
        return acquisition_coverage(self.store, [])

    def rebuild(self):
        return ensure_projection(self.news, self.index())

    def test_directed_query_projects_secondary_names_without_expanding_discovery_filter(self):
        source = {'id': 'synthetic', 'kind': 'rss', 'enabled': True,
                  'terms_approved': True, 'download_articles': False,
                  'url': 'https://synthetic.invalid/feed', 'allowed_hosts': ['synthetic.invalid']}
        self.news.config_path.write_text(json.dumps({'sources': [source]}))
        self.rebuild()
        title = 'Primera Persona Sintética se reunió con Segunda Persona Sintética.'
        identifier = self.store.enqueue('news_query', {'source_id': 'synthetic',
            'url': source['url'], 'entity_ids': ['one'], 'names': [self.entities[0]['name']]}, key='directed')
        job = self.store.claim(kind_prefix='news_query')
        class LocalResponse:
            def fetch(self, url, requested_source):
                return {'status': 'ok', 'url': url, 'body': '<rss/>',
                        'sha256': 'a' * 64, 'retrieved_at': now(), 'parse_metadata': {'truncated': False}}
        records = [{'url': 'https://synthetic.invalid/article', 'title': title, 'text': ''},
                   {'url': 'https://synthetic.invalid/secondary-only',
                    'title': 'Segunda Persona Sintética presentó un informe.', 'text': ''}]
        index = self.index()
        with self.store.connection() as con:
            index.bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        with patch('peps.sources.parse_results', return_value=records), \
             patch.object(self.news, '_query_url', return_value=source['url']):
            self.news.execute(job, LocalResponse(), relevance=index)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT status FROM jobs WHERE id=?', (identifier,)).fetchone()[0], 'done')
            self.assertEqual(con.execute('SELECT count(*) FROM news_articles').fetchone()[0], 1)
            self.assertEqual({row[0] for row in con.execute('SELECT entity_id FROM news_current_candidates')}, {'one', 'two'})
            observed = json.loads(con.execute('SELECT details FROM news_observations').fetchone()[0])
            self.assertEqual(observed['retrieval_relevance']['entity_ids'], ['one'])
            self.assertTrue(projection_ready(con))

    def test_attribution_publishes_invalidated_projection_and_remains_locally_idempotent(self):
        article = self.article('Primera Persona Sintética con Segunda Persona Sintética.')
        self.observe(article, 'Primera Persona Sintética con Segunda Persona Sintética.', ids=['one'])
        with self.store.connection() as con:
            con.execute("DELETE FROM meta WHERE key='news_projection_ready'")
        self.news.attribute(rescore=False)
        with self.store.connection() as con:
            self.assertTrue(projection_ready(con))
            before = [tuple(row) for row in con.execute('SELECT * FROM news_current_candidates ORDER BY article_id,entity_id')]
            self.assertEqual({row[1] for row in before}, {'one', 'two'})
        self.news.attribute(rescore=False)
        with self.store.connection() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM news_current_candidates ORDER BY article_id,entity_id')], before)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_cancelled_attribution_never_stamps_partial_projection_as_ready(self):
        article = self.article()
        self.observe(article, 'Primera Persona Sintética compareció.')
        with self.store.connection() as con:
            con.execute("DELETE FROM meta WHERE key='news_projection_ready'")
        self.engine.stop_event.set()
        self.news.attribute(rescore=False, cancelable=True)
        with self.store.connection() as con:
            self.assertFalse(projection_ready(con))

    def test_retiring_original_body_invalidates_body_only_candidate_projection(self):
        from peps.news_body_quality import quarantine_reference
        for quarantine in (False, True):
            article = self.article('Boletín general sin nombre.')
            document = self.store.add_document({'url': 'https://synthetic.invalid/' + article,
                'title': 'Boletín', 'text': 'Primera Persona Sintética compareció.',
                'source_kind': 'news', 'source_id': 'synthetic'}, [], lambda *_: {})
            with self.store.connection() as con:
                con.execute("UPDATE news_articles SET scope='full_article',document_id=? WHERE id=?", (document, article))
                con.execute("DELETE FROM meta WHERE key='news_projection_ready'")
            self.rebuild()
            with self.store.connection() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM news_current_candidates WHERE article_id=?', (article,)).fetchone()[0], 1)
            if quarantine:
                quarantine_reference(self.news, article, {'reason': 'Ficha de audio sin transcripción.'},
                    {'raw_snapshot_sha256': 'a' * 64, 'raw_snapshot_path': 'news_snapshots/example.html.gz'},
                    expected_document_id=document)
            else:
                self.news._invalidate_snapshot_body({'article_id': article, 'document_id': document}, 'Texto insuficiente')
            with self.store.connection() as con:
                self.assertFalse(projection_ready(con))
            self.rebuild()
            with self.store.connection() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM news_current_candidates WHERE article_id=?', (article,)).fetchone()[0], 0)
                self.assertIsNotNone(con.execute('SELECT 1 FROM documents WHERE id=?', (document,)).fetchone())

    def test_unbuilt_projection_does_not_trust_old_observation_entity_ids(self):
        article = self.article('Nota general sin nombre actual.')
        self.observe(article, 'Nota antigua', update=False, ids=['one'])
        before = self.coverage()
        self.assertFalse(before['current_bank_index_ready'])
        self.assertEqual(before['entities_with_candidates'], 0)
        self.assertTrue(self.rebuild())
        self.assertTrue(self.coverage()['current_bank_index_ready'])
        self.assertEqual(self.coverage()['entities_with_candidates'], 0)

    def test_upgrade_rebuilds_29_ready_marker_to_recover_a_lead_buried_under_90_observations(self):
        article = self.article('Boletín general sin referencias nominales.')
        self.observe(article, 'Primera Persona Sintética compareció.', update=False)
        for _ in range(90):
            self.observe(article, 'Actualización general sin nombres del banco.', update=False)
        with self.store.connection() as con:
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
            # 2.9 considered only the latest ten observations and could mark an
            # empty projection as ready while the older lead remained in raw.
            legacy = {'bank_revision': str(revision), 'relevance_version': MODEL_VERSION}
            con.execute("INSERT OR REPLACE INTO meta VALUES('news_projection_ready',?)", (canonical(legacy),))
            raw = [tuple(row) for row in con.execute('SELECT * FROM news_observations ORDER BY id')]
            self.assertFalse(projection_ready(con))
        self.assertFalse(self.coverage()['current_bank_index_ready'])
        self.assertTrue(self.rebuild())
        with self.store.connection() as con:
            self.assertTrue(projection_ready(con))
            marker = json.loads(con.execute("SELECT value FROM meta WHERE key='news_projection_ready'").fetchone()[0])
            self.assertEqual(marker['projection_version'], PROJECTION_VERSION)
            self.assertEqual([row[0] for row in con.execute('SELECT entity_id FROM news_current_candidates')], ['one'])
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM news_observations ORDER BY id')], raw)
        self.assertEqual(self.coverage()['entities_with_candidates'], 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_same_id_renamed_bank_drops_stale_candidates_and_preserves_raw_history(self):
        article = self.article()
        self.observe(article, 'Primera Persona Sintética compareció.')
        self.assertEqual(self.coverage()['entities_with_candidates'], 1)
        with self.store.connection() as con:
            raw = [tuple(row) for row in con.execute('SELECT * FROM news_observations')]
        self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
        self.assertFalse(self.coverage()['current_bank_index_ready'])
        self.assertEqual(self.coverage()['entities_with_candidates'], 0)
        self.assertTrue(self.rebuild())
        self.assertEqual(self.coverage()['entities_with_candidates'], 0)
        with self.store.connection() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM news_observations')], raw)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_new_bank_id_can_match_stored_metadata_without_network_or_nlp(self):
        article = self.article('Tercera Persona Sintética presentó informe.')
        self.observe(article, 'Tercera Persona Sintética presentó informe.')
        self.rebuild()
        self.store.import_entities([{'id': 'three', 'name': 'Tercera Persona Sintética'}])
        with patch('peps.nlp.analyze', side_effect=AssertionError('NLP on coverage')), \
             patch('socket.create_connection', side_effect=AssertionError('network on projection')):
            self.assertTrue(self.rebuild())
            self.assertEqual(self.coverage()['entities_with_candidates'], 1)
        with self.store.connection() as con:
            self.assertEqual([row[0] for row in con.execute('SELECT entity_id FROM news_current_candidates')], ['three'])

    def test_relevant_article_view_uses_current_projection_after_same_id_rename(self):
        article = self.article()
        self.observe(article, 'Primera Persona Sintética compareció.')
        self.assertEqual([row['id'] for row in self.news.articles(relevant_only=True)], [article])
        self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
        self.assertEqual(self.news.articles(relevant_only=True), [])
        self.assertEqual([row['id'] for row in self.news.articles()], [article])
        self.assertTrue(self.rebuild())
        self.assertEqual(self.news.articles(relevant_only=True), [])
        self.observe(article, 'Persona Completamente Distinta compareció.')
        self.assertEqual([row['id'] for row in self.news.articles(relevant_only=True)], [article])

    def test_repeated_url_observations_add_ids_without_overwriting_previous_candidate(self):
        article = self.article()
        self.observe(article, 'Primera Persona Sintética compareció.')
        self.observe(article, 'Segunda Persona Sintética compareció.')
        value = self.coverage()
        self.assertTrue(value['current_bank_index_ready'])
        self.assertEqual(value['entities_with_candidates'], 2)
        self.assertEqual(value['relevant_articles'], 1)
        with self.store.connection() as con:
            pairs = {row[0] for row in con.execute('SELECT entity_id FROM news_current_candidates')}
        self.assertEqual(pairs, {'one', 'two'})

    def test_candidate_from_old_bank_cannot_be_stamped_as_current_after_same_id_rename(self):
        old_index = self.index()
        with self.store.connection() as con:
            old_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        stale = old_index.rank('Primera Persona Sintética compareció.', '')
        self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
        article = self.article()
        with self.store.connection() as con:
            update_projection(con, article, stale, bank_revision=old_revision)
        value = self.coverage()
        self.assertFalse(value['current_bank_index_ready'])
        self.assertEqual(value['entities_with_candidates'], 0)
        self.rebuild()
        self.assertEqual(self.coverage()['entities_with_candidates'], 0)

    def test_candidate_with_unknown_bank_provenance_requests_local_rebuild(self):
        article = self.article()
        self.rebuild()
        with self.store.connection() as con:
            update_projection(con, article, self.index().rank('Primera Persona Sintética compareció.', ''))
        self.assertFalse(self.coverage()['current_bank_index_ready'])
        self.assertEqual(self.coverage()['entities_with_candidates'], 0)
        self.rebuild()
        self.assertEqual(self.coverage()['entities_with_candidates'], 1)

    def test_rebuild_preserves_all_current_ids_from_latest_unique_snippets(self):
        article = self.article()
        self.observe(article, 'Primera Persona Sintética compareció.', update=False)
        self.observe(article, 'Segunda Persona Sintética compareció.', update=False)
        self.assertTrue(self.rebuild())
        self.assertEqual(self.coverage()['entities_with_candidates'], 2)

    def test_malformed_observation_details_are_skipped_without_altering_raw_history(self):
        article = self.article('Nota general sin personas.')
        self.observe(article, 'Segunda Persona Sintética compareció.', update=False)
        with self.store.connection() as con:
            for identifier, details in (('broken-json', '{incomplete'), ('list-json', '[]'),
                                        ('null-json', 'null'), ('string-json', '"legacy text"')):
                con.execute('INSERT INTO news_observations(id,article_id,source_id,discovery_url,observed_url,retrieved_at,details) '
                            'VALUES(?,?,?,?,?,?,?)', (identifier, article, 'synthetic',
                            'https://synthetic.invalid/feed', f'https://synthetic.invalid/{article}', now(), details))
            original = [tuple(row) for row in con.execute('SELECT * FROM news_observations ORDER BY id')]
        self.assertTrue(self.rebuild())
        value = self.coverage()
        self.assertTrue(value['current_bank_index_ready'])
        self.assertEqual(value['entities_with_candidates'], 1)
        self.assertEqual(value['relevant_articles'], 1)
        with self.store.connection() as con:
            self.assertEqual([tuple(row) for row in con.execute('SELECT * FROM news_observations ORDER BY id')], original)
            self.assertEqual([row[0] for row in con.execute('SELECT entity_id FROM news_current_candidates')], ['two'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_rebuild_reuses_signed_index_but_never_an_unknown_model(self):
        self.article()
        index = self.index()
        with self.store.connection() as con:
            index.bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        from peps.relevance import MODEL_VERSION
        index.model_version = MODEL_VERSION
        with patch('peps.relevance.RelevanceIndex', side_effect=AssertionError('Duplicate bank indexing')):
            self.assertTrue(ensure_projection(self.news, index))
        self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
        with self.store.connection() as con:
            index.bank_revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        index.model_version = 'outdated-model'
        with patch('peps.relevance.RelevanceIndex', wraps=RelevanceIndex) as rebuilt:
            self.assertTrue(ensure_projection(self.news, index))
            self.assertEqual(rebuilt.call_count, 1)
        self.assertEqual(self.coverage()['entities_with_candidates'], 0)

    def test_rebuild_memo_does_not_leak_observation_ids_between_identical_cards(self):
        article = self.article('Información general', 'Resumen neutral repetido.')
        self.observe(article, 'Primera Persona Sintética compareció.', update=False)
        for _ in range(30):
            self.article('Información general', 'Resumen neutral repetido.')
        self.assertTrue(self.rebuild())
        self.assertEqual(self.coverage()['relevant_articles'], 1)
        with self.store.connection() as con:
            payload = json.loads(con.execute("SELECT payload FROM audit WHERE action='news_current_candidates_rebuilt' "
                                             'ORDER BY seq DESC LIMIT 1').fetchone()[0])
        cache = payload['relevance_cache']
        self.assertGreaterEqual(cache['hits'], 30)
        self.assertLessEqual(cache['entries'], cache['maximum_entries'])
        self.assertLessEqual(cache['accounted_bytes'], cache['maximum_bytes'])

    def test_rebuild_recovers_old_relevant_snippet_after_many_new_unrelated_observations(self):
        article = self.article('Información general')
        self.observe(article, 'Primera Persona Sintética compareció.', update=False)
        for index in range(100):
            self.observe(article, f'Boletín neutral {index % 3}', update=False)
        self.assertTrue(self.rebuild())
        self.assertEqual(self.coverage()['relevant_articles'], 1)
        self.assertEqual(self.coverage()['entities_with_candidates'], 1)

    def test_cancel_during_one_articles_observation_scan_rolls_back_rebuild(self):
        article = self.article('Información general')
        for _ in range(20):
            self.observe(article, 'Primera Persona Sintética compareció.', update=False)
        original = self.news._stored_candidate
        checks = [0]
        def interrupted_scan(row, index, connection=None, *, should_stop=None):
            def cancel_after_three():
                checks[0] += 1
                return checks[0] >= 3 or should_stop()
            return original(row, index, connection=connection, should_stop=cancel_after_three)
        with patch.object(self.news, '_stored_candidate', side_effect=interrupted_scan):
            self.assertFalse(self.rebuild())
        self.assertFalse(self.coverage()['current_bank_index_ready'])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_current_candidates').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM news_observations').fetchone()[0], 20)
        self.assertTrue(self.rebuild())
        self.assertEqual(self.coverage()['relevant_articles'], 1)

    def test_rebuild_is_once_per_bank_and_relevance_model_version(self):
        self.article()
        with patch.object(self.news, '_stored_candidate', wraps=self.news._stored_candidate) as ranked:
            self.assertTrue(self.rebuild())
            self.assertTrue(self.rebuild())
            self.assertEqual(ranked.call_count, 1)
            with patch('peps.relevance.MODEL_VERSION', 'synthetic-next-model'):
                self.assertFalse(self.coverage()['current_bank_index_ready'])
                self.assertTrue(self.rebuild())
                self.assertEqual(ranked.call_count, 2)
        with self.store.connection() as con:
            audits = [json.loads(row[0]) for row in con.execute(
                "SELECT payload FROM audit WHERE action='news_current_candidates_rebuilt'")]
        self.assertEqual(len(audits), 2)
        self.assertEqual(audits[-1]['articles_checked'], 1)
        self.assertEqual(audits[-1]['candidate_pairs'], 1)

    def test_cancelled_rebuild_rolls_back_derived_rows_and_ready_marker(self):
        first = self.article()
        self.observe(first, 'Primera Persona Sintética compareció.')
        self.article('Segunda Persona Sintética compareció.')
        self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
        original = self.news._stored_candidate
        def cancel_after_one(*args, **kwargs):
            result = original(*args, **kwargs)
            self.engine.stop_event.set()
            return result
        with patch.object(self.news, '_stored_candidate', side_effect=cancel_after_one):
            self.assertFalse(self.rebuild())
        self.assertFalse(self.coverage()['current_bank_index_ready'])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_current_candidates').fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT count(*) FROM audit WHERE action='news_current_candidates_rebuilt'").fetchone()[0], 0)
        self.engine.stop_event.clear()
        self.assertTrue(self.rebuild())
        self.assertEqual(self.coverage()['entities_with_candidates'], 1)

    def test_expired_deadline_does_not_publish_partial_projection(self):
        self.article()
        self.assertFalse(ensure_projection(self.news, self.index(), deadline=time.monotonic() - 1))
        with self.store.connection() as con:
            self.assertFalse(projection_ready(con))
            self.assertEqual(con.execute('SELECT count(*) FROM news_current_candidates').fetchone()[0], 0)

    def test_deadline_interrupts_entity_index_construction_before_scanning_articles(self):
        self.store.import_entities([{'id': f'bank-{i:04}', 'name': f'Persona Sintética Construcción {i:04}'} for i in range(80)])
        self.article()
        original = RelevanceIndex
        clock = [0.0]
        def expensive_index(entities):
            def costly_entities():
                for entity in entities:
                    clock[0] += .01
                    yield entity
            return original(costly_entities())
        with patch('peps.news_projection.time.monotonic', side_effect=lambda: clock[0]),              patch('peps.relevance.RelevanceIndex', side_effect=expensive_index),              patch.object(self.news, '_stored_candidate', side_effect=AssertionError('Article scan after deadline')):
            self.assertFalse(ensure_projection(self.news, None, deadline=.05))
        self.assertLessEqual(clock[0], .06)
        self.assertFalse(self.coverage()['current_bank_index_ready'])

    def test_sqlite_writer_deadline_is_a_controlled_incomplete_rebuild(self):
        self.article()
        with self.store.connection() as writer:
            writer.execute('BEGIN IMMEDIATE')
            start = time.monotonic()
            self.assertFalse(ensure_projection(self.news, self.index(), deadline=start + .05))
            self.assertLess(time.monotonic() - start, .5)
        self.assertFalse(self.coverage()['current_bank_index_ready'])
        self.assertTrue(self.rebuild())

    def test_current_identity_links_count_but_missing_stale_and_rejected_do_not(self):
        document = self.store.add_document({'url': 'https://synthetic.invalid/body', 'text': 'Texto de prueba.',
                                           'source_kind': 'news'}, [], lambda *_: {})
        article = self.article('Nota general')
        with self.store.connection() as con:
            revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
            con.execute("UPDATE news_articles SET document_id=?,scope='full_article' WHERE id=?", (document, article))
            con.execute('INSERT INTO news_identity_links VALUES(?,?,?)',
                        (document, 'one', canonical({'status': 'candidate', 'bank_revision': revision - 1})))
            con.execute('INSERT INTO news_identity_links VALUES(?,?,?)',
                        (document, 'two', canonical({'status': 'candidate'})))
        self.assertEqual(self.coverage()['entities_with_bodies'], 0)
        with self.store.connection() as con:
            con.execute('UPDATE news_identity_links SET resolution=? WHERE entity_id=?',
                        (canonical({'status': 'candidate', 'bank_revision': revision}), 'one'))
            con.execute('UPDATE news_identity_links SET resolution=? WHERE entity_id=?',
                        (canonical({'status': 'rejected', 'bank_revision': revision}), 'two'))
        self.assertEqual(self.coverage()['entities_with_bodies'], 1)
        expected = self.coverage()
        self.store._audit_json = False
        self.assertEqual(self.coverage(), expected)

    def test_read_cache_is_invalidated_by_observation_and_returns_independent_values(self):
        article = self.article()
        self.observe(article, 'Primera Persona Sintética compareció.')
        import peps.acquisition_coverage as coverage_module
        with patch.object(coverage_module, '_shared_coverage', wraps=coverage_module._shared_coverage) as counted:
            initial = self.coverage()
            initial['permissions']['prepared_sources'] = 999
            second = self.coverage()
            self.assertEqual(counted.call_count, 1)
            self.assertEqual(second['permissions']['prepared_sources'], 0)
            self.observe(article, 'Segunda Persona Sintética compareció.')
            self.assertEqual(self.coverage()['entities_with_candidates'], 2)
            self.assertEqual(counted.call_count, 2)

    def test_session_heartbeats_are_current_without_recomputing_coverage(self):
        self.article()
        self.rebuild()
        import peps.acquisition_coverage as coverage_module
        with patch.object(coverage_module, '_shared_coverage', wraps=coverage_module._shared_coverage) as counted:
            self.assertIsNone(self.coverage()['session'])
            for elapsed in (1, 2):
                with self.store.connection() as con:
                    con.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                ('news_last_session', canonical({'elapsed_seconds': elapsed, 'nested': {'value': elapsed}})))
                result = self.coverage()
                self.assertEqual(result['session']['elapsed_seconds'], elapsed)
                result['session']['nested']['value'] = 'mutated'
            self.assertEqual(counted.call_count, 1)
            self.assertEqual(self.coverage()['session']['nested']['value'], 2)

    def test_session_bank_match_refreshes_on_same_id_rename_and_session_heartbeat(self):
        with self.store.connection() as con:
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
            con.execute('INSERT INTO meta VALUES(?,?)', ('news_last_session', canonical({'bank_revision': revision})))
        import peps.acquisition_coverage as coverage_module
        with patch.object(coverage_module, '_shared_coverage', wraps=coverage_module._shared_coverage) as counted:
            first = self.coverage()
            self.assertEqual(first['current_bank_revision'], revision)
            self.assertIs(first['session_matches_bank'], True)
            self.assertIs(self.coverage()['session_matches_bank'], True)
            self.assertEqual(counted.call_count, 1)
            self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
            renamed = self.coverage()
            self.assertNotEqual(renamed['current_bank_revision'], revision)
            self.assertIs(renamed['session_matches_bank'], False)
            self.assertEqual(renamed['session']['bank_revision'], revision)
            self.assertEqual(counted.call_count, 2)
            with self.store.connection() as con:
                con.execute('UPDATE meta SET value=? WHERE key=?',
                            (canonical({'bank_revision': int(renamed['current_bank_revision'])}), 'news_last_session'))
            self.assertIs(self.coverage()['session_matches_bank'], True)
            self.assertEqual(counted.call_count, 2)
            self.store._audit_json = False
            self.assertIs(self.coverage()['session_matches_bank'], True)

    def test_session_bank_match_is_unknown_when_unmeasured_or_revision_absent(self):
        self.assertIsNone(self.coverage()['session_matches_bank'])
        for payload in ({}, {'bank_revision': None}, [], 'invalid'):
            with self.subTest(payload=payload):
                with self.store.connection() as con:
                    con.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                ('news_last_session', canonical(payload)))
                self.assertIsNone(self.coverage()['session_matches_bank'])
                self.assertIsNotNone(self.coverage()['current_bank_revision'])

    def test_directed_search_receipts_do_not_survive_renaming_same_entity_id(self):
        with self.store.connection() as con:
            revision = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0]
        job = self.store.enqueue('news_query', {'entity_ids': ['one'], 'bank_revision': revision}, key='completed')
        with self.store.connection() as con:
            con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                        (job, 'synthetic', 'news_query', 'https://synthetic.invalid/search', 0,
                         canonical({'candidate_filter': {'excluded_records': 3}}), 'hash', now()))
        self.assertEqual(self.coverage()['entities_queried'], 1)
        self.store.import_entities([{'id': 'one', 'name': 'Persona Completamente Distinta'}])
        value = self.coverage()
        self.assertEqual(value['entities_queried'], 0)
        self.assertEqual(value['excluded_records'], 0)


if __name__ == '__main__':
    unittest.main()
