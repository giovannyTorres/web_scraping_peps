"""Neutral functional regressions; not acquisition throughput evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now
from peps.workflow import GuidedWorkflow, WorkflowError


class OwnedAttributionCorpusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = Engine(temporary.name)
        self.news = NewsAcquirer(self.engine)
        self.source = {'id': 'neutral', 'kind': 'rss', 'enabled': True, 'terms_approved': True,
                       'url': 'https://example.org/feed', 'allowed_hosts': ['example.org'],
                       'download_articles': True, 'article_terms_approved': True}
        self.news.config_path.write_text(json.dumps({'sources': [self.source]}), encoding='utf-8')
        self.engine.store.import_entities([{'id': 'person', 'name': 'Persona Neutral Ejemplo'}])
        self.workflow = GuidedWorkflow(self.engine.store, sources_provider=self.news.sources)
        self.workflow.set_mode('local')
        self.workflow.confirm_sources()

    def seed_legacy(self, changed=False):
        url = 'https://example.org/cultura'
        text = 'Persona Neutral Ejemplo presentó un programa cultural con talleres y bibliotecas para la comunidad. '
        html = '<article><h1>Programa cultural</h1><p>' + text * 5 + '</p></article>'
        self.news._observe({'url': url, 'title': 'Programa cultural', 'text': text}, self.source,
                           self.source['url'], {'retrieved_at': now()}, None, False)
        with patch('peps.news.ARTICLE_EXTRACTION_VERSION', 'article-main-6'):
            if changed:
                with patch('peps.news.article_text', return_value=text * 2):
                    self.news._conserve_article({'article_id': digest(url), 'url': url}, self.source,
                        {'url': url, 'body': html, 'retrieved_at': now(), 'sha256': digest(html)})
            else:
                self.news._conserve_article({'article_id': digest(url), 'url': url}, self.source,
                    {'url': url, 'body': html, 'retrieved_at': now(), 'sha256': digest(html)})

    def attribute(self):
        receipts = []
        result = self.news.attribute(limit=None, rescore=False, mutation_receipts=receipts)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(result['reextraction']['examined'], 1)
        return receipts

    def assert_checkpoint_rejected(self, receipts):
        with self.assertRaisesRegex(WorkflowError, 'Los datos cambiaron durante la atribución'):
            self.workflow.mark_analysis(status='scoring', attribution_receipts=receipts)

    def online_partial(self):
        self.workflow.set_mode('online')
        self.workflow.confirm_sources()
        for index in range(7):
            self.engine.store.enqueue('news_article', {'source_id': 'neutral', 'url': 'https://example.org/pending/' + str(index)},
                                      key='pending-' + str(index))
        self.workflow.mark_download(status='paused')
        snapshot = self.workflow.accept_partial()
        self.assertEqual(snapshot['coverage']['pending_downloads'], 7)
        self.assertTrue(snapshot['coverage']['accepted_partial'])
        with self.engine.store.connection() as con:
            return deepcopy(self.workflow._state(con)['partial_acceptance'])

    def test_own_unchanged_local_reextraction_is_accepted(self):
        self.seed_legacy()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.assertTrue(self.workflow.mark_analysis(status='scoring', attribution_receipts=receipts))
        self.engine.rescore()
        self.workflow.mark_analysis()
        self.assertTrue(self.workflow.snapshot()['analysis_current'])
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_own_upgrade_creating_new_immutable_body_is_accepted(self):
        self.seed_legacy(changed=True)
        before = self.engine.store.stats()['documents']
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.assertEqual(self.engine.store.stats()['documents'], before + 1)
        self.assertTrue(self.workflow.mark_analysis(status='scoring', attribution_receipts=receipts))

    def test_missing_or_tampered_receipts_cannot_exempt_corpus_change(self):
        for mutation in ('missing', 'hash', 'duplicate'):
            with self.subTest(mutation=mutation):
                self.seed_legacy()
                self.workflow.mark_analysis(status='running')
                receipts = self.attribute()
                if mutation == 'missing':
                    receipts = None
                elif mutation == 'hash':
                    receipts[0]['event_hash'] = '0' * 64
                else:
                    receipts.append(deepcopy(receipts[0]))
                self.assert_checkpoint_rejected(receipts)

    def test_external_discovery_interleaved_after_own_reextraction_is_rejected(self):
        self.seed_legacy()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.engine.store.audit('news_discovery_completed', {'source_id': 'other', 'records': 1})
        self.assert_checkpoint_rejected(receipts)

    def test_external_local_reextraction_without_own_receipt_is_rejected(self):
        self.seed_legacy()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.engine.store.audit('news_article_acquired', {'document_id': receipts[0]['document_id'],
                                                        'local_reextraction': True})
        self.assert_checkpoint_rejected(receipts)

    def test_external_discovery_before_own_reextraction_cannot_be_hidden_by_the_last_event(self):
        self.seed_legacy()
        self.workflow.mark_analysis(status='running')
        self.engine.store.audit('news_discovery_completed', {'source_id': 'other', 'records': 1})
        receipts = self.attribute()
        self.assert_checkpoint_rejected(receipts)

    def test_unrelated_import_during_own_upgrade_is_not_masked_by_owned_body_growth(self):
        self.seed_legacy(changed=True)
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.engine.store.add_document({'url': 'https://example.org/otro', 'text': 'Otro documento cultural independiente.',
                                        'source_kind': 'news'}, [], lambda *_: {})
        self.assert_checkpoint_rejected(receipts)

    def test_bank_change_is_still_rejected_with_valid_owned_receipts(self):
        self.seed_legacy()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.engine.store.import_entities([{'id': 'another', 'name': 'Otra Persona Neutral'}])
        self.assert_checkpoint_rejected(receipts)

    def test_online_partial_rebinds_only_owned_body_upgrade_and_preserves_7_pending_and_limits(self):
        self.seed_legacy(changed=True)
        acceptance = self.online_partial()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.workflow.mark_analysis(status='scoring', attribution_receipts=receipts)
        self.engine.rescore()
        self.workflow.mark_analysis()
        snapshot = self.workflow.snapshot()
        self.assertTrue(snapshot['analysis_current'])
        self.assertTrue(snapshot['actions']['view_results']['enabled'])
        self.assertTrue(snapshot['coverage']['accepted_partial'])
        self.assertEqual(snapshot['coverage']['pending_downloads'], 7)
        with self.engine.store.connection() as con:
            rebound = self.workflow._state(con)['partial_acceptance']
        self.assertEqual(rebound['at'], acceptance['at'])
        self.assertEqual(rebound['limitations'], acceptance['limitations'])
        self.assertEqual(rebound['queue_signature'], acceptance['queue_signature'])
        self.assertNotEqual(rebound['signature']['corpus'], acceptance['signature']['corpus'])

    def test_online_partial_queue_change_still_rejects_even_with_owned_reextraction(self):
        self.seed_legacy()
        self.online_partial()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        with self.engine.store.connection() as con:
            con.execute("UPDATE jobs SET due=due+1 WHERE id=(SELECT id FROM jobs WHERE kind='news_article' LIMIT 1)")
        with self.assertRaisesRegex(WorkflowError, 'Finalice explícitamente'):
            self.workflow.mark_analysis(status='scoring', attribution_receipts=receipts)

    def test_online_partial_external_discovery_is_not_rebound(self):
        self.seed_legacy()
        self.online_partial()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.engine.store.audit('news_discovery_completed', {'source_id': 'other', 'records': 1})
        self.assert_checkpoint_rejected(receipts)

    def test_online_full_download_remains_current_after_owned_reextraction(self):
        self.seed_legacy()
        self.workflow.set_mode('online')
        self.workflow.confirm_sources()
        self.workflow.mark_download(status='completed')
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.workflow.mark_analysis(status='scoring', attribution_receipts=receipts)
        self.engine.rescore()
        self.workflow.mark_analysis()
        self.assertTrue(self.workflow.snapshot()['analysis_current'])

    def test_review_change_still_rejects_with_owned_receipts(self):
        self.seed_legacy()
        self.workflow.mark_analysis(status='running')
        receipts = self.attribute()
        self.engine.store.audit('evidence_review', {'evidence_id': 'neutral'})
        self.assert_checkpoint_rejected(receipts)


if __name__ == '__main__':
    unittest.main()
