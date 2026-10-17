"""Exact body dedup preserves each capture and immutable historical versions."""
import gzip
import json
import tempfile
import unicodedata
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from peps.engine import Engine
from peps.news import ARTICLE_EXTRACTION_VERSION, NewsAcquirer, news_fingerprint
from peps.news_body_quality import classify_body
from peps.store import digest, now


NAME = 'Persona Pública Ejemplo'
BODY = (NAME + ' presentó el calendario de actividades de las bibliotecas municipales. '
        'El programa ofrece talleres gratuitos, lectura y actividades culturales para todos los vecinos. '
        'La agenda publicada informa los horarios, las sedes y el procedimiento de inscripción. '
        'El equipo municipal explicó cómo se actualizará la información del servicio durante el año.')


def page(body=BODY, *, title=NAME, structured=None, classes='single-post'):
    declaration = ('<script type="application/ld+json">' + json.dumps(structured) + '</script>') if structured else ''
    return ('<html><head><title>' + title + '</title>' + declaration + '</head>'
            '<body class="' + classes + '"><h1>' + title + '</h1>'
            '<article class="entry-content"><p>' + body + '</p></article></body></html>')


class ProfileStructureTests(unittest.TestCase):
    def test_explicit_profile_main_entity_is_a_reference(self):
        html = page(structured={'@type': 'ProfilePage', 'mainEntity': {'@type': 'Person', 'name': NAME}})
        quality = classify_body(html, BODY, 'https://example.org/biografia')
        self.assertEqual(quality['status'], 'profile_reference')
        self.assertFalse(quality['body_usable'])

    def test_profile_path_and_page_post_type_are_both_required(self):
        html = page(classes='single-perfil perfil-template-default')
        self.assertEqual(classify_body(html, BODY, 'https://example.org/perfil/persona')['status'], 'profile_reference')
        self.assertTrue(classify_body(page(), BODY, 'https://example.org/perfil/persona')['body_usable'])
        self.assertTrue(classify_body(html, BODY, 'https://example.org/noticia')['body_usable'])

    def test_news_article_with_author_person_and_profile_in_title_is_news(self):
        html = page(title='Perfil del programa cultural', classes='single-perfil', structured={
            '@type': 'NewsArticle', 'author': {'@type': 'Person', 'name': 'Autor Ficticio'},
            'about': {'@type': 'Person', 'name': NAME}})
        self.assertTrue(classify_body(html, BODY, 'https://example.org/perfil/programa')['body_usable'])

    def test_sidebar_person_does_not_make_a_profile(self):
        html = page(structured={'@type': 'WebPage', 'author': {'@type': 'Person', 'name': NAME}})
        self.assertTrue(classify_body(html, BODY, 'https://example.org/perfil/programa')['body_usable'])

    def test_generic_cms_article_metadata_does_not_override_explicit_profile(self):
        html = page(structured=[{'@type': 'Article'},
                    {'@type': 'ProfilePage', 'mainEntity': {'@type': 'Person', 'name': NAME}}])
        html = html.replace('</head>', '<meta property="og:type" content="article"></head>')
        self.assertEqual(classify_body(html, BODY, 'https://example.org/perfil/persona')['status'], 'profile_reference')


class BodyAliasTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = Engine(self.directory.name)
        self.store = self.engine.store
        self.news = NewsAcquirer(self.engine)
        self.store.import_entities([{'id': 'person', 'name': NAME}])
        self.sources = [{'id': host, 'kind': 'rss', 'publisher_id': host,
                         'allowed_hosts': [host], 'enabled': True, 'terms_approved': True,
                         'download_articles': True, 'article_terms_approved': True,
                         'url': 'https://' + host + '/feed'} for host in ('one.example', 'two.example')]
        self.news.config_path.write_text(json.dumps({'sources': self.sources}), encoding='utf-8')

    def conserve(self, url, html, *, stamp=None):
        source = next(source for source in self.sources if url.startswith('https://' + source['id'] + '/'))
        stamp = stamp or now()
        self.news._observe({'url': url, 'title': NAME, 'text': 'Resumen del programa cultural.'}, source,
                           source['url'], {'retrieved_at': stamp}, None)
        return self.news._conserve_article({'article_id': digest(url), 'url': url}, source,
            {'url': url, 'body': html, 'retrieved_at': stamp, 'sha256': digest(html)})

    def test_cross_publisher_copy_keeps_two_captures_and_one_body_and_evidence(self):
        original, copy = 'https://one.example/story', 'https://two.example/reprint'
        html_a, html_b = page(), page(title='Otra cabecera para la misma noticia')
        doc_id = self.conserve(original, html_a)['document_id']
        self.assertEqual(self.conserve(copy, html_b), {'status': 'duplicate_reference'})
        self.news.attribute(force=True, rescore=False)
        with self.store.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM news_articles WHERE scope='full_article'").fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
            alias = dict(con.execute('SELECT * FROM news_body_aliases').fetchone())
            original_metadata = json.loads(con.execute('SELECT metadata FROM documents WHERE id=?', (doc_id,)).fetchone()[0])
            self.assertEqual(con.execute('SELECT count(*) FROM news_observations').fetchone()[0], 2)
            self.assertIsNone(con.execute('SELECT document_id FROM news_articles WHERE id=?', (digest(copy),)).fetchone()[0])
        self.assertEqual(alias['canonical_document_id'], doc_id)
        self.assertEqual(alias['body_sha256'], digest(BODY))
        self.assertEqual(alias['observed_url'], copy)
        self.assertEqual(json.loads(alias['source_policy'])['id'], 'two.example')
        self.assertEqual(gzip.decompress((self.engine.data_dir / alias['raw_snapshot_path']).read_bytes()).decode(), html_b)
        self.assertEqual(gzip.decompress((self.engine.data_dir / original_metadata['raw_snapshot_path']).read_bytes()).decode(), html_a)
        self.assertEqual(len(self.store.evidence('person')), 1)
        self.assertEqual([row['id'] for row in self.news.articles(scope='duplicate_reference')], [digest(copy)])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_alias_reuse_does_not_queue_http_and_explicit_revalidation_can_change_body(self):
        original, copy = 'https://one.example/story', 'https://two.example/reprint'
        self.conserve(original, page())
        self.conserve(copy, page())
        source = self.sources[1]
        record = {'url': copy, 'title': NAME, 'text': BODY}
        reused = self.news._observe(record, source, source['url'], {'retrieved_at': now()}, None, True)
        self.assertEqual(reused['status'], 'duplicate_reference')
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.news._active_cache_mode = 'revalidate'
        queued = self.news._observe(record, source, source['url'], {'retrieved_at': now()}, None, True)
        self.assertEqual(queued['status'], 'article_queued')
        changed = self.conserve(copy, page(BODY + ' Se añadió una nueva fecha al calendario.'))
        self.assertEqual(changed['status'], 'upgraded')
        with self.store.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM news_articles WHERE scope='full_article'").fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM news_body_aliases').fetchone()[0], 1)

    def test_export_preserves_alias_receipts_and_exact_canonical_version_without_copying_body(self):
        original, copy = 'https://one.example/story', 'https://two.example/reprint'
        canonical_id = self.conserve(original, page())['document_id']
        copy_html = page(title='Una cabecera diferente para la copia')
        self.conserve(copy, copy_html)
        with self.store.connection() as con:
            saved_alias = dict(con.execute('SELECT * FROM news_body_aliases').fetchone())
            saved_quality = dict(con.execute('SELECT * FROM news_body_quality_receipts').fetchone())
        records = list(self.store.export())
        alias = next(row for row in records if row['record_type'] == 'news_body_alias')
        quality = next(row for row in records if row['record_type'] == 'news_body_quality_receipt')
        self.assertEqual({key: value for key, value in alias.items() if key != 'record_type'}, saved_alias)
        self.assertEqual({key: value for key, value in quality.items() if key != 'record_type'}, saved_quality)
        documents = [row for row in records if row['record_type'] == 'document']
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document['id'], canonical_id)
        self.assertEqual(document['content_hash'], alias['body_sha256'])
        self.assertEqual(digest(document['text']), alias['body_sha256'])
        self.assertEqual(document['metadata']['extraction_version'], ARTICLE_EXTRACTION_VERSION)
        classification = json.loads(alias['classification'])
        self.assertEqual(classification['canonical_document_id'], canonical_id)
        self.assertEqual(classification['capture_metadata']['extraction_version'], ARTICLE_EXTRACTION_VERSION)
        self.assertEqual(json.loads(alias['source_policy'])['id'], 'two.example')
        self.assertNotIn('text', alias)
        captured = gzip.decompress((self.engine.data_dir / alias['raw_snapshot_path']).read_bytes()).decode('utf-8')
        self.assertEqual(captured, copy_html)
        self.assertEqual(digest(captured), alias['raw_snapshot_sha256'])
        self.assertEqual(quality['raw_snapshot_path'], alias['raw_snapshot_path'])
        self.assertEqual(quality['raw_snapshot_sha256'], alias['raw_snapshot_sha256'])

    def test_normalized_cluster_never_collapses_different_unicode_or_paragraph_offsets(self):
        nfd = unicodedata.normalize('NFD', BODY)
        self.assertNotEqual(digest(BODY), digest(nfd))
        self.assertEqual(news_fingerprint(BODY), news_fingerprint(nfd))
        first = self.conserve('https://one.example/first', page())['document_id']
        second = self.conserve('https://two.example/second', page(nfd))['document_id']
        self.assertNotEqual(first, second)
        with self.store.connection() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM news_articles WHERE scope='full_article'").fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 2)

    def test_alias_is_pinned_to_old_version_after_original_changes_and_review_stays_historical(self):
        original, copy = 'https://one.example/story', 'https://two.example/reprint'
        old_doc = self.conserve(original, page())['document_id']
        self.conserve(copy, page())
        self.news.attribute(force=True, rescore=False)
        evidence = self.store.evidence('person')[0]
        self.store.review(evidence['id'], 'confirmed', 'Fixture reviewer', 'Fictional test only',
                          identity_spans=[{'start': 0, 'end': len(NAME)}])
        self.engine.rescore('person')
        run_id = self.engine.explanation('person')['run']['id']
        new_doc = self.conserve(original, page(BODY + ' Se modificó la agenda del próximo mes.'))['document_id']
        self.assertNotEqual(old_doc, new_doc)
        self.assertEqual(self.store.evidence('person'), [])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT canonical_document_id FROM news_body_aliases').fetchone()[0], old_doc)
            self.assertEqual(con.execute('SELECT text FROM documents WHERE id=?', (old_doc,)).fetchone()[0], BODY)
            self.assertEqual(json.loads(con.execute('SELECT review FROM news_identity_history').fetchone()[0])['status'], 'confirmed')
        self.news.attribute(force=True, rescore=False)
        self.assertEqual(self.store.evidence('person')[0]['review'], {})
        self.assertTrue(self.engine.replay_score(run_id)['matched'])
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_repeated_same_capture_has_one_alias_receipt(self):
        self.conserve('https://one.example/story', page())
        url, stamp = 'https://two.example/reprint', now()
        self.conserve(url, page(), stamp=stamp)
        self.conserve(url, page(), stamp=stamp)
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM news_body_aliases').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 1)

    def test_simultaneous_cross_publisher_captures_elect_one_canonical_body(self):
        urls = ['https://one.example/story', 'https://two.example/reprint']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda url: self.conserve(url, page()), urls))
        self.assertEqual(sorted(result['status'] for result in results), ['duplicate_reference', 'upgraded'])
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM news_body_aliases').fetchone()[0], 1)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_failed_alias_audit_rolls_back_alias_and_current_row_atomically(self):
        self.conserve('https://one.example/story', page())
        audit = self.store._audit
        def reject(con, action, payload, *args):
            if action == 'news_duplicate_reference_conserved':
                raise RuntimeError('Fixture alias audit failure')
            return audit(con, action, payload, *args)
        with patch.object(self.store, '_audit', side_effect=reject):
            with self.assertRaisesRegex(RuntimeError, 'Fixture alias audit failure'):
                self.conserve('https://two.example/reprint', page())
        with self.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT scope FROM news_articles WHERE id=?", (digest('https://two.example/reprint'),)).fetchone()[0], 'feed_metadata_only')
            table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_body_aliases'").fetchone()
            self.assertFalse(table)
        self.assertTrue(self.store.verify_audit()['ok'])

    def test_explicit_profile_is_captured_but_never_becomes_news_or_metadata_evidence(self):
        url = 'https://one.example/perfil/persona'
        html = page(classes='single-perfil perfil-template-default')
        self.assertEqual(self.conserve(url, html), {'status': 'profile_reference'})
        self.assertEqual([row['id'] for row in self.news.articles(scope='profile_reference')], [digest(url)])
        with patch('socket.create_connection', side_effect=AssertionError('No HTTP')):
            self.news.attribute(force=True, rescore=False)
        with self.store.connection() as con:
            receipt = dict(con.execute('SELECT * FROM news_body_quality_receipts').fetchone())
            self.assertEqual(con.execute('SELECT count(*) FROM documents').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM evidence').fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM news_articles WHERE scope='full_article'").fetchone()[0], 0)
        self.assertEqual(gzip.decompress((self.engine.data_dir / receipt['raw_snapshot_path']).read_bytes()).decode(), html)


if __name__ == '__main__':
    unittest.main()
