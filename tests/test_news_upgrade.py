"""A paused 2.3.1-style bank survives an additive, disabled catalogue upgrade."""
from copy import deepcopy
import http.client
import json
import tempfile
import threading
import time
import unittest

from peps.app import create_server
from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import canonical
from peps.workflow import GuidedWorkflow


class DisabledCatalogueUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(self.temp.name)
        self.news = NewsAcquirer(self.engine)
        self.old_sources = [{
            'id': 'legacy_public_feed', 'kind': 'rss', 'url': 'https://synthetic.invalid/feed',
            'enabled': True, 'terms_approved': True, 'download_articles': False,
            'article_terms_approved': False,
        }, {'id': 'gdelt_batches', 'kind': 'gdelt', 'base_url': 'https://synthetic.invalid/search',
            'enabled': False, 'terms_approved': False}]
        self.news.config_path.write_text(canonical({'sources': self.old_sources}), encoding='utf-8')
        self.engine.store.import_entities([{'id': 'legacy', 'name': 'Persona Ficticia Anterior', 'kind': 'person'}])
        self.engine.store.audit('news_source_configured', {'id': 'legacy_public_feed'})
        with self.engine.store.connection() as con:
            con.executemany('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,'
                            'discovered_at,scope,status) VALUES(?,?,?,?,?,?,?,?,?)', [
                (str(index), 'https://synthetic.invalid/' + str(index), 'synthetic', 'legacy_public_feed',
                 'Noticia ficticia ' + str(index), 'Resumen ficticio.', '2026-10-15',
                 'feed_metadata_only', 'discovered') for index in range(235)])
        for index in range(64):
            identifier = self.engine.store.enqueue('news_article', {'synthetic': index}, due=time.time() + 3600)
            with self.engine.store.connection() as con:
                con.execute("UPDATE jobs SET status='deferred' WHERE id=?", (identifier,))
        self.workflow = GuidedWorkflow(self.engine.store, self.news.sources)
        self.workflow.confirm_sources()
        self.workflow.mark_download(status='paused', details={'pending': 64, 'stop_reason': 'time_limit'})

    def request(self, server, token, path, data=None):
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
        connection.request('GET' if data is None else 'POST', path,
                           body=json.dumps(data) if data is not None else None,
                           headers={'X-PEPS-Token': token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_http_partial_close_preserves_235_metadata_references_without_enabling_empty_analysis(self):
        with self.engine.store.connection() as con:
            jobs_before = [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')]
        server, token = create_server(self.engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code, workflow = self.request(server, token, '/api/workflow')
            self.assertEqual(code, 200)
            self.assertTrue(workflow['sources_confirmed'])
            self.assertTrue(workflow['coverage']['resume_available'])
            self.assertTrue(workflow['actions']['finish_download']['enabled'])
            self.assertIsNone(workflow['coverage']['acquisition_context'])
            self.assertEqual(self.news.search_plan()['query_count'], 0)
            current_sources = self.news.sources()
            self.assertEqual(current_sources[:len(self.old_sources)], self.old_sources)
            self.assertTrue(all(source.get(field, False) is False
                                for source in current_sources[len(self.old_sources):]
                                for field in ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved')))
            code, workflow = self.request(server, token, '/api/workflow-finish-download', {})
            self.assertEqual(code, 200, workflow)
            self.assertFalse(workflow['actions']['analyze']['enabled'])
            self.assertFalse(workflow['actions']['analyze_available']['enabled'])
            self.assertIn('textos pertinentes', workflow['actions']['analyze']['reason'])
            self.assertEqual(workflow['activity']['material']['archived_articles'], 235)
            self.assertEqual(workflow['activity']['material']['relevant_articles'], 0)
            self.assertEqual(workflow['activity']['material']['analyzable_documents'], 0)
            self.assertTrue(workflow['coverage']['closed_partial'])
            self.assertEqual(workflow['coverage']['limitations']['articles'], 235)
            self.assertEqual(workflow['coverage']['limitations']['pending_downloads'], 64)
            for endpoint in ('/api/workflow-analyze', '/api/workflow-analyze-available'):
                code, response = self.request(server, token, endpoint, {})
                self.assertEqual(code, 409, response)
                self.assertIn('textos pertinentes', response['error'])
            self.assertEqual(self.engine.store.stats()['scores'], 0)
            with self.engine.store.connection() as con:
                self.assertEqual(jobs_before, [dict(row) for row in con.execute('SELECT * FROM jobs ORDER BY id')])
                self.assertEqual(con.execute('SELECT count(*) FROM news_articles').fetchone()[0], 235)
            self.assertTrue(self.engine.store.verify_audit()['ok'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_scope_changes_never_get_rebound_by_an_additive_upgrade(self):
        mutations = ('permission', 'url', 'source_audit', 'search_audit', 'bank', 'corpus')
        with self.engine.store.connection() as con:
            baseline = self.workflow._state(con)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                # A fresh engine isolates each authorization/corpus change.
                self.setUp()
                if mutation in ('permission', 'url'):
                    sources = deepcopy(self.old_sources)
                    sources[0]['terms_approved' if mutation == 'permission' else 'url'] = False if mutation == 'permission' else 'https://changed.invalid/'
                    self.news.config_path.write_text(canonical({'sources': sources}), encoding='utf-8')
                elif mutation in ('source_audit', 'search_audit'):
                    self.engine.store.audit('news_source_configured' if mutation == 'source_audit' else 'news_search_configured', {})
                elif mutation == 'bank':
                    self.engine.store.import_entities([{'id': 'new', 'name': 'Entidad Ficticia Nueva'}])
                else:
                    self.engine.store.add_document({'url': 'https://synthetic.invalid/new-body',
                        'text': 'Un cuerpo nuevo.', 'source_kind': 'official'}, [], lambda *args: {})
                NewsAcquirer(self.engine)
                self.assertFalse(self.workflow.snapshot()['coverage']['resume_available'] and self.workflow.snapshot()['actions']['finish_download']['enabled'])
                with self.engine.store.connection() as con:
                    self.assertEqual(self.workflow._state(con)['download']['signature'], baseline['download']['signature'])

    def test_active_lease_does_not_lose_migration_and_still_blocks_closure_until_expired(self):
        with self.engine.store.connection() as con:
            job_id = con.execute('SELECT id FROM jobs LIMIT 1').fetchone()[0]
            con.execute("UPDATE jobs SET status='running',owner='old-worker',lease_until=? WHERE id=?", (time.time() + 300, job_id))
        NewsAcquirer(self.engine)
        snapshot = self.workflow.snapshot()
        self.assertTrue(snapshot['sources_confirmed'])
        self.assertFalse(snapshot['actions']['finish_download']['enabled'])
        with self.engine.store.connection() as con:
            con.execute('UPDATE jobs SET lease_until=? WHERE id=?', (time.time() - 1, job_id))
        closed = self.workflow.finish_download()
        self.assertTrue(closed['coverage']['closed_partial'])
        self.assertFalse(closed['actions']['analyze']['enabled'])
        self.assertEqual(closed['activity']['material']['analyzable_documents'], 0)
        self.assertEqual(self.engine.store.stats()['scores'], 0)


if __name__ == '__main__':
    unittest.main()
