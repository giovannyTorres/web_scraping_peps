"""Minimal neutral theme structures; actual source replays are separate."""
import unittest
import json
import tempfile
from unittest.mock import patch

from peps.engine import Engine
from peps.news import ARTICLE_EXTRACTION_VERSION, NewsAcquirer, article_text
from peps.news_body_quality import classify_body
from peps.store import digest, now


class PublisherExtractionTests(unittest.TestCase):
    def test_legal_valueless_attributes_are_preserved_without_type_errors(self):
        html = ('<html class><body class><article class="entry-content">'
                '<p class itemprop>Texto editorial visible.</p><a rel href>Un enlace sin destino.</a>'
                '</article></body></html>')
        self.assertEqual(article_text(html), 'Texto editorial visible.\n\nUn enlace sin destino.')

    def test_valueless_hidden_attribute_still_hides_content(self):
        html = '<article class="entry-content"><p class hidden>Invisible.</p><p>Visible.</p></article>'
        self.assertEqual(article_text(html), 'Visible.')

    def test_semantic_article_layout_does_not_hide_editorial_body(self):
        html = ('<article class="wi-single has-sidebar sidebar-left">'
                '<div class="entry-content"><p>La persona presentó el informe.</p>'
                '<p>La respuesta permanece disponible.</p></div>'
                '<div class="sidebar-left"><p>Noticia ajena.</p></div></article>')
        self.assertEqual(article_text(html), 'La persona presentó el informe.\n\n'
                         'La respuesta permanece disponible.')

    def test_article_layout_does_not_override_related_navigation_class(self):
        html = ('<article class="has-sidebar sidebar-left related"><div class="entry-content">'
                '<p>Noticia ajena.</p></div></article>')
        self.assertEqual(article_text(html), '')

    def test_divi_body_wins_over_blog_cards_and_keeps_response(self):
        html = ('<div class="et_pb_module et_pb_post_content"><p>La persona presentó el informe.</p>'
                '<p>La persona negó las acusaciones.</p></div>'
                '<article class="et_pb_post et_pb_blog_item_0_0"><div class="post-content">'
                '<p>' + 'Noticia ajena ficticia. ' * 30 + '</p></div></article>')
        self.assertEqual(article_text(html), 'La persona presentó el informe.\n\n'
                         'La persona negó las acusaciones.')

    def test_divi_empty_editorial_body_does_not_fall_back_to_blog_cards(self):
        html = ('<div class="et_pb_post_content"></div>'
                '<article class="et_pb_post et_pb_blog_item_0_2"><div class="post-content">'
                '<p>' + 'Noticia ajena ficticia. ' * 30 + '</p></div></article>')
        self.assertEqual(article_text(html), '')

    def test_ordinary_divi_article_is_not_mistaken_for_a_blog_card(self):
        html = '<article class="et_pb_post"><div class="entry-content"><p>Texto real.</p></div></article>'
        self.assertEqual(article_text(html), 'Texto real.')

    def test_quadratin_editorial_container_wins_over_semantic_recommendation_cards(self):
        html = ('<div class="q-content"><div class="q-content__info">'
                '<p>La persona presentó el informe original.</p>'
                '<p>La persona negó las acusaciones.</p></div>'
                '<div class="q-content__related"><article><p>' + 'Titular ajeno ficticio. ' * 10 +
                '</p></article></div></div>')
        self.assertEqual(article_text(html), 'La persona presentó el informe original.\n\n'
                         'La persona negó las acusaciones.')

    def test_quadratin_empty_story_never_falls_back_to_semantic_recommendations(self):
        html = ('<div class="q-content__info"></div><article><p>' +
                'Noticia ajena ficticia de recomendación. ' * 20 + '</p></article>')
        self.assertEqual(article_text(html), '')

    def test_quadratin_body_inside_related_card_or_hidden_root_is_excluded(self):
        for wrapper in ('<aside>', '<div class="related">', '<div hidden>'):
            with self.subTest(wrapper=wrapper):
                tag = 'aside' if wrapper == '<aside>' else 'div'
                html = wrapper + '<div class="q-content__info"><p>Noticia ajena.</p></div></' + tag + '>'
                self.assertEqual(article_text(html), '')

    def test_quadratin_wider_wrapper_is_not_a_body_signal(self):
        html = ('<div class="q-content"><div class="q-content__info"></div>'
                '<p>Texto de navegación ajeno a la historia.</p></div>')
        self.assertEqual(article_text(html), '')

    def test_multimedia_quality_parser_accepts_valueless_attributes(self):
        html = ('<article class="has-sidebar sidebar-left"><div class="et_pb_post_content">'
                '<audio class src="/grabacion.mp3">audio</audio><p class>Persona Pública Ejemplo '
                'nos habla sobre el calendario de actividades de la ciudad.</p></div></article>')
        self.assertFalse(classify_body(html, article_text(html))['body_usable'])

    def test_version_six_snapshot_is_migrated_locally_and_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            news = NewsAcquirer(engine)
            source = {'id': 'fixture', 'kind': 'rss', 'url': 'https://example.org/feed',
                      'allowed_hosts': ['example.org'], 'enabled': True, 'terms_approved': True,
                      'download_articles': True, 'article_terms_approved': True}
            news.config_path.write_text(json.dumps({'sources': [source]}), encoding='utf-8')
            url = 'https://example.org/article'
            correct = ('La persona presentó el informe completo ante la comunidad. '
                       'La respuesta de la persona se conserva en el documento original.')
            incorrect = 'Noticia ajena ficticia de una tarjeta de navegación. ' * 4
            html = ('<div class="et_pb_post_content"><p>' + correct + '</p></div>'
                    '<article class="et_pb_blog_item_0_0"><div class="post-content"><p>'
                    + incorrect + '</p></div></article>')
            news._observe({'url': url, 'title': 'Informe', 'text': 'Referencia.'}, source,
                          source['url'], {'retrieved_at': now()}, None, False)
            with patch('peps.news.ARTICLE_EXTRACTION_VERSION', 'article-main-6'), \
                    patch('peps.news.article_text', return_value=incorrect):
                old = news._conserve_article({'article_id': digest(url), 'url': url}, source,
                          {'url': url, 'body': html, 'retrieved_at': now(), 'sha256': digest(html)})
            with patch('peps.collector.SafeFetcher', side_effect=AssertionError('Local migration only')):
                self.assertEqual(news.reextract_snapshots(), {'upgraded': 1, 'examined': 1})
                self.assertEqual(news.reextract_snapshots(), {'examined': 0})
            with engine.store.connection() as con:
                current = con.execute('SELECT d.text,d.metadata FROM news_articles n JOIN documents d '
                                      'ON d.id=n.document_id').fetchone()
                archived = con.execute('SELECT text FROM documents WHERE id=?', (old['document_id'],)).fetchone()[0]
            self.assertEqual(current['text'], correct)
            self.assertEqual(archived, incorrect)
            self.assertEqual(json.loads(current['metadata'])['extraction_version'], ARTICLE_EXTRACTION_VERSION)
            self.assertEqual(json.loads(current['metadata'])['raw_snapshot_sha256'], digest(html))
            self.assertTrue(engine.store.verify_audit()['ok'])

    def test_version_seven_wrong_card_is_recovered_from_the_same_local_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            news = NewsAcquirer(engine)
            source = {'id': 'fixture', 'kind': 'rss', 'url': 'https://example.org/feed',
                      'allowed_hosts': ['example.org'], 'enabled': True, 'terms_approved': True,
                      'download_articles': True, 'article_terms_approved': True}
            news.config_path.write_text(json.dumps({'sources': [source]}), encoding='utf-8')
            url = 'https://example.org/quadratin-layout'
            correct = ('Persona Pública Ejemplo presentó su informe ante la comunidad. '
                       'La persona negó las acusaciones y solicitó conservar su respuesta íntegra.')
            incorrect = 'Una tarjeta lateral pertenece a una noticia distinta. ' * 3
            html = ('<div class="q-content__info"><p>' + correct + '</p></div>'
                    '<article><p>' + incorrect + '</p></article>')
            news._observe({'url': url, 'title': 'Persona Pública Ejemplo', 'text': 'Referencia.'},
                          source, source['url'], {'retrieved_at': now()}, None, False)
            with patch('peps.news.ARTICLE_EXTRACTION_VERSION', 'article-main-7'), \
                    patch('peps.news.article_text', return_value=incorrect):
                old = news._conserve_article({'article_id': digest(url), 'url': url}, source,
                          {'url': url, 'body': html, 'retrieved_at': now(), 'sha256': digest(html)})
            with patch('peps.collector.SafeFetcher', side_effect=AssertionError('No network for local repair')):
                self.assertEqual(news.reextract_snapshots(), {'upgraded': 1, 'examined': 1})
                self.assertEqual(news.reextract_snapshots(), {'examined': 0})
            with engine.store.connection() as con:
                current = con.execute('SELECT d.text,d.metadata FROM news_articles n JOIN documents d '
                                      'ON d.id=n.document_id').fetchone()
                archived = con.execute('SELECT text FROM documents WHERE id=?', (old['document_id'],)).fetchone()[0]
            self.assertEqual(current['text'], correct)
            self.assertEqual(archived, incorrect)
            self.assertEqual(json.loads(current['metadata'])['extraction_version'], ARTICLE_EXTRACTION_VERSION)
            self.assertEqual(json.loads(current['metadata'])['raw_snapshot_sha256'], digest(html))
            self.assertTrue(engine.store.verify_audit()['ok'])


if __name__ == '__main__':
    unittest.main()
