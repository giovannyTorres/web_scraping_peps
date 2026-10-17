import json
import unittest
from unittest.mock import patch

from peps.article_fallback import CONFIG_SHA256, _effective_config_sha256, recover_article_text, runtime_details
from peps.news import article_text


TITLE = 'Informe público del archivo documental local'
PARAGRAPHS = (
    'El archivo documental publicó una relación de actividades verificables y explicó el proceso '
    'de conservación de documentos públicos. Cada registro conserva la fuente, la fecha de consulta '
    'y una descripción suficiente para que otra persona pueda revisar su procedencia.',
    'El equipo describió las labores de clasificación y consulta realizadas durante el periodo. '
    'La información de este informe corresponde al mismo documento, conserva su contexto original '
    'y permite revisar los hechos sin atribuir conclusiones a personas distintas.',
)


def page(content, *, title=TITLE):
    return '<html><head><title>' + title + '</title></head><body>' + content + '</body></html>'


def recovery_page():
    return page('<div class="main right-sidebar"><h1>' + TITLE + '</h1><article>'
        '<div class="post-content-wrap has-share-float"><div class="entry-content">' +
        ''.join('<p>' + p + '</p>' for p in PARAGRAPHS) +
        '</div></div></article></div><aside><p>Otra nota ajena denuncia corrupción y fraude.</p></aside>')


class ArticleFallbackTests(unittest.TestCase):
    def test_documented_local_extraction_is_present_and_offline(self):
        with patch('socket.socket.connect', side_effect=AssertionError('Unexpected network')), \
                patch('socket.getaddrinfo', side_effect=AssertionError('Unexpected DNS')):
            text = article_text(recovery_page())
            details = runtime_details()
        for p in PARAGRAPHS:
            self.assertIn(p, text)
        self.assertNotIn('Otra nota', text)
        self.assertTrue(details['available'])
        self.assertEqual(details['dependency_version'], '2.3.1')
        self.assertEqual(details['config_sha256'], CONFIG_SHA256)
        self.assertFalse(details['network'])
        json.dumps(details)

    def test_usable_existing_body_never_calls_external_extractor(self):
        text = PARAGRAPHS[0] + '\n\n' + PARAGRAPHS[1]
        html = page('<article class="entry-content"><p>' + PARAGRAPHS[0] +
                    '</p><p>' + PARAGRAPHS[1] + '</p></article>')
        with patch('peps.article_fallback.recover_article_text', side_effect=AssertionError('Must retain body')):
            self.assertEqual(article_text(html), text)

    def test_explicit_empty_editorial_cannot_borrow_recommendations(self):
        html = page('<h1>' + TITLE + '</h1><div class="entry-content"></div>'
            '<article><p>' + PARAGRAPHS[0] + '</p><p>' + PARAGRAPHS[1] + '</p></article>')
        with patch('peps.article_fallback.recover_article_text', side_effect=AssertionError('Empty story is final')):
            self.assertEqual(article_text(html), '')

    def test_short_explicit_editorial_cannot_be_replaced_by_different_story(self):
        html = page('<h1>' + TITLE + '</h1><div class="entry-content"><p>Entrada propia breve.</p></div>'
            '<article><p>' + PARAGRAPHS[0] + '</p><p>' + PARAGRAPHS[1] + '</p></article>')
        self.assertEqual(article_text(html), 'Entrada propia breve.')

    def test_structured_paywall_abstains_without_library_call(self):
        html = recovery_page().replace('</head>', '<script type="application/ld+json">'
            '{"@type":"NewsArticle","isAccessibleForFree":false}</script></head>')
        with patch('peps.article_fallback.runtime_details', side_effect=AssertionError('Paywall must abstain first')):
            self.assertEqual(recover_article_text(html), '')

    def test_navigation_or_consent_is_not_an_article(self):
        html = page('<h1>Preferencias de privacidad del sitio</h1><p>' + PARAGRAPHS[0] + '</p><p>' +
                    PARAGRAPHS[1] + '</p>', title='Preferencias de privacidad')
        self.assertEqual(recover_article_text(html), '')

    def test_recommendation_headline_cannot_become_a_paragraph(self):
        html = recovery_page()
        with patch('trafilatura.extract', return_value='Titular de otra noticia ajena\n' + '\n'.join(PARAGRAPHS)):
            self.assertEqual(recover_article_text(html), '')

    def test_only_exact_advertising_labels_are_removed(self):
        with patch('trafilatura.extract', return_value=PARAGRAPHS[0] + '\n[Publicidad]\n' + PARAGRAPHS[1]):
            text = recover_article_text(recovery_page())
        self.assertEqual(text, '\n\n'.join(PARAGRAPHS))

    def test_source_paragraphs_are_required_not_only_extractor_length(self):
        with patch('trafilatura.extract', return_value=PARAGRAPHS[0] + '\n' + PARAGRAPHS[1]):
            html = page('<h1>' + TITLE + '</h1><nav><p>' + PARAGRAPHS[0] + '</p><p>' +
                        PARAGRAPHS[1] + '</p></nav>')
            self.assertEqual(recover_article_text(html), '')

    def test_missing_required_dependency_is_an_explicit_error(self):
        with patch('peps.article_fallback.runtime_details', return_value={'available': False}):
            with self.assertRaisesRegex(RuntimeError, 'requirements-extraction'):
                recover_article_text(recovery_page())

    def test_effective_config_hash_covers_inherited_defaults(self):
        import trafilatura
        before = runtime_details()
        original = trafilatura.settings.DEFAULT_CONFIG['DEFAULT']['MIN_EXTRACTED_SIZE']
        try:
            trafilatura.settings.DEFAULT_CONFIG['DEFAULT']['MIN_EXTRACTED_SIZE'] = '777'
            after = runtime_details()
        finally:
            trafilatura.settings.DEFAULT_CONFIG['DEFAULT']['MIN_EXTRACTED_SIZE'] = original
        self.assertNotEqual(before['effective_config_sha256'], after['effective_config_sha256'])
        self.assertEqual(before['config_sha256'], after['config_sha256'])
        self.assertEqual(before['settings_cfg_sha256'], after['settings_cfg_sha256'])
        self.assertEqual(len(before['settings_cfg_sha256']), 64)

    def test_extractor_receives_the_same_effective_config_that_is_hashed(self):
        seen = {}
        def extract(_html, *, config, **_kwargs):
            seen['effective_hash'] = _effective_config_sha256(config)
            return '\n'.join(PARAGRAPHS)
        with patch('trafilatura.extract', side_effect=extract):
            self.assertEqual(recover_article_text(recovery_page()), '\n\n'.join(PARAGRAPHS))
        self.assertEqual(seen['effective_hash'], runtime_details()['effective_config_sha256'])


if __name__ == '__main__':
    unittest.main()
