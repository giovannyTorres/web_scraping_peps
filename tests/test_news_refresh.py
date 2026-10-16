"""Refreshing a story retires replaced evidence without deleting audit history."""
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer
from peps.store import digest, now


class _NewsFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name, online=True)
        self.news = NewsAcquirer(self.engine)
        self.store = self.engine.store
        self.store.import_entities([{'id': 'example', 'name': 'Persona Pública Ejemplo'}])
        self.source = {'id': 'fixture', 'kind': 'rss', 'allowed_hosts': ['example.org'],
                       'enabled': True, 'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'url': 'https://example.org/feed'}
        self.news.config_path.write_text(json.dumps({'sources': [self.source],
                                                     'search': {'strategy': 'feeds'}}), encoding='utf-8')
        self.counter = 0
        self.adverse = 'Persona Pública Ejemplo fue detenida por fraude. ' + 'Información ficticia para esta prueba. ' * 4
        self.neutral = 'Persona Pública Ejemplo presentó su informe anual. ' + 'Información ficticia para esta prueba. ' * 4

    def download(self, url, text):
        self.counter += 1
        article_id = digest(url)
        self.news._observe({'url': url, 'title': 'Persona Pública Ejemplo', 'text': 'Resumen ficticio',
                            'published_at': now()[:10]}, self.source, self.source['url'],
                           {'retrieved_at': now()}, None, False)
        self.store.enqueue('news_article', {'source_id': 'fixture', 'article_id': article_id, 'url': url},
                           key=f'refresh-fixture-{self.counter}')
        html = '<html><article><p>' + text + '</p></article></html>'
        class FixtureFetcher:
            def fetch(self, requested, _source):
                return {'status': 'ok', 'url': requested, 'body': html, 'retrieved_at': now(), 'sha256': digest(html)}
        self.news.execute(self.store.claim(kind_prefix='news_article'), FixtureFetcher())
        with self.store.connection() as con:
            return con.execute('SELECT document_id FROM news_articles WHERE id=?', (article_id,)).fetchone()[0]

    def review_original(self):
        self.news.attribute(rescore=False)
        evidence = self.store.evidence('example')[0]
        # Fictional fixture only: this exercises the review contract and does
        # not record a human decision about any real person or company.
        self.store.review(evidence['id'], 'confirmed', 'Fixture reviewer', 'Fictional identity selection',
                          identity_spans=[{'start': 0, 'end': len('Persona Pública Ejemplo')}])
        self.engine.rescore('example')
        explanation = self.engine.explanation('example')
        self.assertGreater(explanation['result']['score'], 0)
        return evidence['document_id'], explanation['run']['id']


class NewsRefreshTests(_NewsFixture):
    def test_refresh_removes_old_active_findings_and_preserves_reviewed_history(self):
        url = 'https://example.org/story'
        self.download(url, self.adverse)
        previous, run_id = self.review_original()
        current = self.download(url, self.neutral)
        self.assertNotEqual(current, previous)
        self.assertEqual(self.store.evidence('example'), [])
        with self.store.connection() as con:
            history = dict(con.execute('SELECT * FROM news_identity_history').fetchone())
            self.assertEqual(con.execute('SELECT count(*) FROM scores').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 2)
        self.assertEqual(history['document_id'], previous)
        self.assertEqual(json.loads(history['review'])['status'], 'confirmed')
        self.news.attribute(rescore=False)
        evidence = self.store.evidence('example')
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['document_id'], current)
        self.assertEqual(evidence[0]['review'], {})
        self.engine.rescore('example')
        self.assertEqual(self.engine.explanation('example')['result']['score'], 0)
        self.assertTrue(self.engine.replay_score(run_id)['matched'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_shared_duplicate_keeps_old_evidence_until_last_article_refreshes(self):
        first, second = 'https://example.org/first', 'https://example.org/second'
        previous = self.download(first, self.adverse)
        self.assertEqual(self.download(second, self.adverse), previous)
        self.review_original()
        current = self.download(first, self.neutral)
        self.assertEqual(self.store.evidence('example')[0]['document_id'], previous)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_identity_history').fetchone()[0], 0)
        self.assertEqual(self.download(second, self.neutral), current)
        self.assertEqual(self.store.evidence('example'), [])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_identity_history').fetchone()[0], 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_identical_refresh_preserves_current_review(self):
        url = 'https://example.org/story'
        self.download(url, self.adverse)
        previous, run_id = self.review_original()
        review = self.store.evidence('example')[0]['review']
        self.assertEqual(self.download(url, self.adverse), previous)
        self.assertEqual(self.store.evidence('example')[0]['review'], review)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_identity_history').fetchone()[0], 0)
        self.assertTrue(self.engine.replay_score(run_id)['matched'])

    def test_refresh_rollback_preserves_current_evidence_if_audit_fails(self):
        url = 'https://example.org/story'
        self.download(url, self.adverse)
        previous, run_id = self.review_original()
        audit = self.store._audit
        def reject(con, action, payload, *args):
            if action == 'news_replaced_evidence_retired':
                raise RuntimeError('Fixture audit failure')
            return audit(con, action, payload, *args)
        with patch.object(self.store, '_audit', side_effect=reject), self.assertRaisesRegex(RuntimeError, 'Fixture audit failure'):
            self.download(url, self.neutral)
        self.assertEqual(self.store.evidence('example')[0]['document_id'], previous)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT document_id FROM news_articles').fetchone()[0], previous)
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM news_identity_history').fetchone()[0], 0)
        self.assertTrue(self.engine.replay_score(run_id)['matched'])
        self.assertTrue(self.store.verify_audit()['ok'])


class LocalReextractionTests(_NewsFixture):
    def legacy(self, url='https://example.org/legacy', text=None, raw_body=None):
        with patch('peps.news.ARTICLE_EXTRACTION_VERSION', 'article-main-2'), \
             patch('peps.news.article_text', return_value=text or self.adverse):
            doc_id = self.download(url, raw_body or self.neutral)
            self.review_original()
        with self.store.connection() as con:
            row = dict(con.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone())
        row['metadata'] = json.loads(row['metadata'])
        return row

    def test_attribute_repairs_existing_bank_without_http_or_copying_old_review(self):
        original = self.legacy()
        old_metadata = original['metadata']
        with patch('peps.collector.SafeFetcher', side_effect=AssertionError('No network in local re-extraction')):
            result = self.news.attribute(rescore=False)
        self.assertEqual(result['reextraction'], {'upgraded': 1, 'examined': 1})
        evidence = self.store.evidence('example')
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['text'], self.neutral.strip())
        self.assertEqual(evidence[0]['review'], {})
        self.assertEqual(evidence[0]['retrieved_at'], original['retrieved_at'])
        self.assertEqual(evidence[0]['metadata']['raw_snapshot_sha256'], old_metadata['raw_snapshot_sha256'])
        self.assertEqual(evidence[0]['metadata']['extraction_source'], 'local_immutable_snapshot')
        with self.store.connection() as con:
            preserved = dict(con.execute('SELECT * FROM documents WHERE id=?', (original['id'],)).fetchone())
            review = json.loads(con.execute('SELECT review FROM news_identity_history').fetchone()[0])
        self.assertEqual(json.loads(preserved['metadata']), old_metadata)
        self.assertEqual(review['status'], 'confirmed')
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_paragraph_only_change_is_not_deduplicated_to_the_flattened_body(self):
        one = 'Persona Pública Ejemplo presentó su informe anual.'
        two = 'Otra persona fue detenida por fraude.'
        raw = one + '</p><p>' + two
        with patch('peps.news.ARTICLE_EXTRACTION_VERSION', 'article-main-2'), \
             patch('peps.news.article_text', return_value=one + ' ' + two):
            previous = self.download('https://example.org/paragraphs', raw)
        result = self.news.reextract_snapshots()
        self.assertEqual(result['upgraded'], 1)
        current = self.news.articles()[0]['document_id']
        self.assertNotEqual(previous, current)
        with self.store.connection() as con:
            body = con.execute('SELECT text FROM documents WHERE id=?', (current,)).fetchone()[0]
        self.assertEqual(body, one + '\n\n' + two)

    def test_unchanged_body_uses_receipt_without_mutating_old_document(self):
        with patch('peps.news.ARTICLE_EXTRACTION_VERSION', 'article-main-2'):
            doc_id = self.download('https://example.org/unchanged', self.neutral)
        first = self.news.reextract_snapshots()
        self.assertEqual(first['unchanged'], 1)
        with self.store.connection() as con:
            metadata = json.loads(con.execute('SELECT metadata FROM documents WHERE id=?', (doc_id,)).fetchone()[0])
            receipt = dict(con.execute('SELECT * FROM news_extraction_receipts').fetchone())
        self.assertEqual(metadata['extraction_version'], 'article-main-2')
        self.assertEqual(receipt['version'], 'article-main-3')
        Path(self.engine.data_dir / metadata['raw_snapshot_path']).unlink()
        self.assertEqual(self.news.reextract_snapshots(), {'examined': 0})

    def test_empty_new_editorial_body_retires_old_related_card(self):
        original = self.legacy(raw_body='<div class="entry-content"></div>')
        result = self.news.reextract_snapshots()
        self.assertEqual(result['insufficient'], 1)
        self.assertEqual(self.store.evidence('example'), [])
        row = self.news.articles()[0]
        self.assertEqual((row['scope'], row['status'], row['document_id']), ('feed_metadata_only', 'insufficient_body', None))
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents WHERE id=?', (original['id'],)).fetchone()[0], 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def assert_bad_snapshot_retired(self, variant):
        original = self.legacy('https://example.org/' + variant)
        path = self.engine.data_dir / original['metadata']['raw_snapshot_path']
        if variant == 'missing':
            path.unlink()
        elif variant == 'corrupt':
            path.write_bytes(gzip.compress(b'Corrupted snapshot'))
        else:
            path.write_bytes(gzip.compress(b'x' * 20_000_001))
        result = self.news.reextract_snapshots()
        self.assertEqual(result['unavailable_or_invalid_snapshot'], 1)
        self.assertEqual(self.store.evidence('example'), [])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_missing_snapshot_is_not_kept_as_a_usable_old_body(self):
        self.assert_bad_snapshot_retired('missing')

    def test_corrupt_snapshot_is_not_kept_as_a_usable_old_body(self):
        self.assert_bad_snapshot_retired('corrupt')

    def test_gzip_expansion_is_bounded(self):
        self.assert_bad_snapshot_retired('oversized')


if __name__ == '__main__':
    unittest.main()
