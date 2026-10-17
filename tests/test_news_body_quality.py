"""Neutral regression fixtures; operational evidence is a separate real replay."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from peps.engine import Engine
from peps.news import NewsAcquirer, article_text
from peps.news_body_quality import classify_body, quarantine_existing_references
from peps.store import digest, now


INTRO = ('Persona Pública Ejemplo, representante de Ciudad Ejemplo, '
         'nos habla sobre el programa cultural y su calendario de actividades.')
MEDIA = '<audio controls src="/grabacion.mp3">audio</audio>'


def page(text=INTRO, media=MEDIA):
    return '<html><body><div class="entry-content">' + media + '<p>' + text + '</p></div></body></html>'


class BodyQualityTests(unittest.TestCase):
    def test_brief_audio_introduction_is_a_reference(self):
        html = page()
        result = classify_body(html, article_text(html))
        self.assertEqual(result['status'], 'multimedia_reference')
        self.assertFalse(result['body_usable'])
        self.assertEqual(result['media_types'], ['audio'])

    def test_native_video_source_also_counts(self):
        html = page(media='<video controls><source src="/grabacion.mp4" type="video/mp4"></video>')
        self.assertFalse(classify_body(html, article_text(html))['body_usable'])

    def test_short_factual_reporting_with_audio_is_retained(self):
        html = page('Persona Pública Ejemplo presentó su informe anual y anunció la apertura '
                    'de dos bibliotecas públicas el próximo lunes.')
        self.assertTrue(classify_body(html, article_text(html))['body_usable'])

    def test_developed_story_with_audio_and_intro_is_retained(self):
        html = page(INTRO + ' El informe describe las obras realizadas durante el año. ' * 15)
        self.assertTrue(classify_body(html, article_text(html))['body_usable'])

    def test_intro_without_media_is_retained(self):
        html = page(media='')
        self.assertTrue(classify_body(html, article_text(html))['body_usable'])

    def test_sidebar_player_and_global_metadata_do_not_replace_editorial_media(self):
        html = ('<html><head><meta property="og:audio" content="/grabacion.mp3"></head>'
                '<body><div class="entry-content"><p>' + INTRO + '</p>'
                '<aside>' + MEDIA + '</aside></div>' + MEDIA + '</body></html>')
        self.assertTrue(classify_body(html, article_text(html))['body_usable'])

    def test_empty_player_does_not_establish_available_media(self):
        html = page(media='<audio controls>audio</audio>')
        self.assertTrue(classify_body(html, article_text(html))['body_usable'])

    def test_declared_visible_transcript_is_retained(self):
        html = page(media=MEDIA + '<p itemprop="transcript">Transcripción disponible.</p>')
        self.assertTrue(classify_body(html, article_text(html))['body_usable'])


class BodyQualityPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = Engine(self.temporary.name)
        self.news = NewsAcquirer(self.engine)
        self.engine.store.import_entities([{'id': 'person', 'name': 'Persona Pública Ejemplo'}])
        self.source = {'id': 'fixture', 'kind': 'rss', 'enabled': True,
                       'terms_approved': True, 'download_articles': True,
                       'article_terms_approved': True, 'url': 'https://example.org/feed',
                       'allowed_hosts': ['example.org']}
        self.news.config_path.write_text(json.dumps({'sources': [self.source]}), encoding='utf-8')

    def conserve(self, html, url='https://example.org/entrevista'):
        self.news._observe({'url': url, 'title': 'Persona Pública Ejemplo', 'text': 'Ficha de entrevista.'},
                           self.source, self.source['url'], {'retrieved_at': now()}, None, False)
        return self.news._conserve_article({'article_id': digest(url), 'url': url}, self.source,
                  {'url': url, 'body': html, 'retrieved_at': now(), 'sha256': digest(html)})

    def row(self):
        with self.engine.store.connection() as con:
            return dict(con.execute('SELECT * FROM news_articles').fetchone())

    def test_scoring_preflight_preserves_custom_catalogue_bytes(self):
        before = self.news.config_path.read_bytes()
        self.engine.rescore()
        self.assertEqual(self.news.config_path.read_bytes(), before)
        self.assertEqual([s['id'] for s in self.news.sources()], ['fixture'])

    def test_new_reference_archives_raw_html_without_creating_evidence_or_document(self):
        html = page()
        self.assertEqual(self.conserve(html), {'status': 'multimedia_reference'})
        row = self.row()
        self.assertEqual(row['scope'], 'multimedia_reference')
        self.assertIsNone(row['document_id'])
        with self.engine.store.connection() as con:
            receipt = dict(con.execute('SELECT * FROM news_body_quality_receipts').fetchone())
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
        raw = gzip.decompress((self.engine.data_dir / receipt['raw_snapshot_path']).read_bytes())
        self.assertEqual(raw.decode(), html)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt['raw_snapshot_sha256'])
        with patch('socket.create_connection', side_effect=AssertionError('No HTTP')):
            result = self.news.attribute(force=True, rescore=False)
        self.assertEqual(result['identity_links'], {'multimedia_reference': 1})
        self.assertIsNone(self.row()['document_id'])
        self.assertEqual(self.engine.store.evidence('person'), [])
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_download_job_finishes_as_reference_without_retry(self):
        html = page()
        url = 'https://example.org/entrevista'
        self.news._observe({'url': url, 'title': 'Persona Pública Ejemplo', 'text': 'Ficha de entrevista.'},
                           self.source, self.source['url'], {'retrieved_at': now()}, None, False)
        job_id = self.engine.store.enqueue('news_article',
                    {'article_id': digest(url), 'source_id': 'fixture', 'url': url}, key='local-original')
        job = self.engine.store.claim(kind_prefix='news_article')
        class CapturedOriginal:
            def fetch(self, requested, source):
                return {'status': 'ok', 'url': requested, 'body': html,
                        'retrieved_at': now(), 'sha256': digest(html)}
        result = self.news.execute(job, CapturedOriginal())
        self.assertEqual(result['body_result'], 'multimedia_reference')
        with self.engine.store.connection() as con:
            saved = con.execute('SELECT status,error FROM jobs WHERE id=?', (job_id,)).fetchone()
        self.assertEqual(saved['status'], 'done')
        self.assertFalse(saved['error'])

    def test_direct_scoring_preflight_retires_legacy_evidence_and_keeps_history(self):
        html = page()
        with patch('peps.news_body_quality.classify_body', return_value={'body_usable': True}):
            old = self.conserve(html)
            self.news.attribute(force=True, rescore=False)
            self.engine.rescore()
        document_id = old['document_id']
        with self.engine.store.connection() as con:
            original = dict(con.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone())
            original_runs = [tuple(row) for row in con.execute('SELECT * FROM score_runs ORDER BY id')]
            self.assertGreater(con.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
        self.assertEqual(self.engine.rescore(), 1)
        self.assertIsNone(self.row()['document_id'])
        self.assertEqual(self.row()['scope'], 'multimedia_reference')
        self.assertEqual(self.engine.store.evidence('person'), [])
        with self.engine.store.connection() as con:
            self.assertEqual(dict(con.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()), original)
            self.assertGreater(con.execute('SELECT count(*) FROM news_identity_history').fetchone()[0], 0)
            runs = {tuple(row) for row in con.execute('SELECT * FROM score_runs')}
            self.assertTrue(set(original_runs) <= runs)
            latest = json.loads(con.execute('SELECT payload FROM score_runs ORDER BY created_at DESC LIMIT 1').fetchone()[0])
            self.assertEqual(latest['evidence_manifest'], [])
            self.assertTrue(any(json.loads(row[2])['evidence_manifest'] for row in original_runs))
            before = con.execute('SELECT count(*) FROM audit').fetchone()[0]
        self.assertEqual(quarantine_existing_references(self.news), {})
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM audit').fetchone()[0], before)
        self.assertTrue(self.engine.store.verify_audit()['ok'])

    def test_reference_reobservation_requires_explicit_revalidation_to_queue_body(self):
        html = page()
        self.conserve(html)
        record = {'url': 'https://example.org/entrevista', 'title': 'Persona Pública Ejemplo',
                  'text': 'Ficha de entrevista.'}
        result = self.news._observe(record, self.source, self.source['url'],
                                    {'retrieved_at': now()}, None, True)
        self.assertEqual(result['status'], 'multimedia_reference')
        with self.engine.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.news._active_cache_mode = 'revalidate'
        result = self.news._observe(record, self.source, self.source['url'],
                                    {'retrieved_at': now()}, None, True)
        self.assertEqual(result['status'], 'article_queued')

    def test_corrupt_snapshot_is_reported_without_unverified_reclassification(self):
        html = page()
        with patch('peps.news_body_quality.classify_body', return_value={'body_usable': True}):
            old = self.conserve(html)
        with self.engine.store.connection() as con:
            metadata = json.loads(con.execute('SELECT metadata FROM documents WHERE id=?', (old['document_id'],)).fetchone()[0])
        snapshot = self.engine.data_dir / metadata['raw_snapshot_path']
        snapshot.write_bytes(gzip.compress(b'<article>Different bytes.</article>'))
        self.assertEqual(quarantine_existing_references(self.news), {'examined': 1, 'unverified_capture': 1})
        self.assertEqual(self.row()['document_id'], old['document_id'])
        self.assertEqual(self.row()['scope'], 'full_article')


if __name__ == '__main__':
    unittest.main()
