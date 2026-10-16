"""Retrieval regressions noticed while preparing real-name validation.

These strings are neutral fixtures, not downloaded facts about the named
entities. A retrieval hit must never become an identity or scoring decision.
"""
import unittest

from peps.identity import IdentityResolver
from peps.news import article_text
from peps.relevance import RelevanceIndex


class LiveCaseRetrievalRegressions(unittest.TestCase):
    def assert_candidate_only(self, result, entity_id):
        self.assertTrue(result['relevant'])
        self.assertEqual(result['entity_ids'], [entity_id])
        self.assertFalse(result['identity_verified'])
        self.assertEqual(result['score_effect'], 'none')

    def test_single_word_canonical_company_name_remains_retrievable(self):
        for name in ('Pemex', 'Odebrecht', 'Walmart', 'OHL'):
            with self.subTest(name=name):
                entity = {'id': 'company', 'name': name, 'kind': 'company', 'aliases': [], 'context': {}}
                result = RelevanceIndex([entity]).rank(name + ' presenta su informe anual.', '')
                self.assert_candidate_only(result, 'company')

    def test_single_word_person_still_requires_a_separate_context_clue(self):
        entity = {'id': 'person', 'name': 'Peña', 'kind': 'person', 'context': {}}
        result = RelevanceIndex([entity]).rank('Peña presenta su informe anual.', '')
        self.assertFalse(result['relevant'])
        entity.update(name='Enrique Peña Nieto', aliases=['Peña'])
        self.assertFalse(RelevanceIndex([entity]).rank('Peña presenta su informe anual.', '')['relevant'])

    def test_company_legal_suffix_is_not_required_in_editorial_metadata(self):
        entity = {'id': 'company', 'name': 'OHL México S.A.B. de C.V.', 'kind': 'company', 'context': {}}
        index = RelevanceIndex([entity])
        result = index.rank('OHL México presenta su informe anual.', '', names=[entity['name']])
        self.assert_candidate_only(result, 'company')
        self.assertTrue(result['matches'][0]['corporate_core'])
        self.assertLess(result['rank'], index.rank(entity['name'] + ' presenta su informe anual.', '')['rank'])
        self.assertFalse(index.rank('OHL México presenta su informe anual.', '', names=['Otra Empresa'])['relevant'])

    def test_business_names_supported_by_identity_are_available_to_retrieval(self):
        entity = {'id': 'company', 'name': 'Empresa de Construcción Ejemplo S.A. de C.V.',
                  'kind': 'company', 'context': {'state': 'Sonora',
                                               'business_names': ['Ingeniería Construcción Asociada']}}
        result = RelevanceIndex([entity]).rank('Ingeniería Construcción Asociada presenta su informe en Sonora.', '')
        self.assert_candidate_only(result, 'company')

    def test_long_company_alias_is_a_candidate_without_an_identity_verdict(self):
        entity = {'id': 'company', 'name': 'Petróleos Mexicanos', 'kind': 'company',
                  'aliases': ['Pemex'], 'context': {}}
        text = 'Pemex presenta su informe anual.'
        self.assert_candidate_only(RelevanceIndex([entity]).rank(text, ''), 'company')
        self.assertTrue(all(row['status'] != 'matched' for row in IdentityResolver([entity]).resolve(text)))

    def test_multiword_company_alias_does_not_require_the_bank_context_in_every_headline(self):
        entity = {'id': 'company', 'name': 'AHMSA', 'kind': 'company',
                  'context': {'legal_name': 'Altos Hornos de México S.A.B. de C.V.'}}
        text = 'Altos Hornos de México presenta su informe anual.'
        self.assert_candidate_only(RelevanceIndex([entity]).rank(text, ''), 'company')
        self.assertTrue(all(row['status'] != 'matched' for row in IdentityResolver([entity]).resolve(text)))

    def test_short_generic_or_rejected_company_aliases_remain_contextual(self):
        entity = {'id': 'company', 'name': 'Empresa Constructora Ejemplo', 'kind': 'company',
                  'aliases': ['ICA', 'Grupo', 'Empresa', 'México', 'Grupo México', 'S.A. de C.V.', '12345',
                              {'name': 'Pemex', 'status': 'rejected'}], 'context': {}}
        for name in ('ICA', 'Grupo', 'Empresa', 'México', 'Grupo México', 'S.A. de C.V.', '12345', 'Pemex'):
            with self.subTest(name=name):
                self.assertFalse(RelevanceIndex([entity]).rank(name + ' presenta su informe anual.', '')['relevant'])
        entity['context'] = {'state': 'Oaxaca'}
        self.assert_candidate_only(RelevanceIndex([entity]).rank('ICA presenta su informe en Oaxaca.', ''), 'company')

    def test_company_retrieval_preserves_competing_entities(self):
        entity = {'id': 'first', 'name': 'Pemex', 'kind': 'company', 'context': {}}
        result = RelevanceIndex([entity, dict(entity, id='second')]).rank('Pemex presenta su informe anual.', '')
        self.assertEqual(result['entity_ids'], ['first', 'second'])
        self.assertFalse(result['identity_verified'])

    def test_partial_company_word_is_not_a_literal_candidate(self):
        entity = {'id': 'company', 'name': 'Pemex', 'kind': 'company', 'context': {}}
        self.assertFalse(RelevanceIndex([entity]).rank('Pemexico presenta un informe.', '')['relevant'])


class RealThemeExtractionRegressions(unittest.TestCase):
    """Minimal structural reproductions, with original neutral filler text."""

    BODY = 'Enrique Peña Nieto presentó un informe público. ' + 'Un párrafo original de control. ' * 48
    CARD = 'Noticia ajena de la columna lateral. ' * 8

    def test_elementor_editorial_body_precedes_sidebar_article_cards(self):
        html = ('<html><body><div class="elementor-widget-theme-post-content" '
                'data-widget_type="theme-post-content.default"><p>' + self.BODY + '</p></div>'
                '<section><article class="elementor-post elementor-grid-item"><p>' + self.CARD +
                '</p></article></section></body></html>')
        self.assertEqual(article_text(html), self.BODY.strip())

    def test_nested_editorial_containers_do_not_duplicate_paragraphs(self):
        html = ('<main><article><div class="entry-content"><div itemprop="articleBody">'
                '<p>' + self.BODY + '</p></div></div></article></main>')
        extracted = article_text(html)
        self.assertEqual(extracted, self.BODY.strip())
        self.assertEqual(extracted.count('Enrique Peña Nieto'), 1)

    def test_editorial_container_wins_even_when_other_article_is_longer(self):
        html = ('<div class="post-content"><p>' + self.BODY + '</p></div>'
                '<article><p>' + self.CARD * 20 + '</p></article>')
        self.assertEqual(article_text(html), self.BODY.strip())

    def test_empty_editorial_body_does_not_fall_back_to_another_article(self):
        html = '<div class="entry-content"></div><article><p>' + self.CARD + '</p></article>'
        self.assertEqual(article_text(html), '')

    def test_navigation_and_related_cards_are_not_a_story_body(self):
        html = ('<nav>Menú editorial</nav><aside><article><p>' + self.CARD + '</p></article></aside>'
                '<article class="elementor-post elementor-grid-item"><p>' + self.CARD + '</p></article>')
        self.assertEqual(article_text(html), '')

    def test_related_link_inside_the_editorial_paragraph_is_not_article_evidence(self):
        html = ('<div class="entry-content"><p>La empresa presentó su informe anual.</p>'
                '<p><strong>Te puede interesar:</strong><span><a href="https://example.org/otra">'
                'Pemex enfrenta una denuncia por fraude.</a></span></p>'
                '<p>El documento original sigue disponible.</p></div>')
        self.assertEqual(article_text(html), 'La empresa presentó su informe anual.\n\n'
                         'El documento original sigue disponible.')

    def test_paragraph_boundaries_do_not_attach_another_headline_to_a_company(self):
        from peps.nlp import analyze
        html = ('<article><p>Facebook podría recibir una sanción.</p>'
                '<p>Otra persona fue condenada.</p><p>Odebrecht publicó su informe anual.</p></article>')
        extracted = article_text(html)
        self.assertEqual(extracted.count('\n\n'), 2)
        analysis = analyze(extracted, {'id': 'company', 'name': 'Odebrecht', 'kind': 'company'}, 'news')
        self.assertFalse(analysis['findings'])

    def test_tag_lists_and_related_sections_do_not_extend_the_article(self):
        html = ('<article><p>El informe editorial está disponible.</p><div>Tags: '
                '<a rel="tag">Empresa</a><br></div><h2>Contenido relacionado</h2>'
                '<p>Empresa fue condenada por fraude.</p></article>')
        self.assertEqual(article_text(html), 'El informe editorial está disponible.')

    def test_an_editorial_link_and_exculpatory_paragraph_remain_visible(self):
        html = ('<div class="entry-content"><p>La empresa negó las acusaciones de fraude.</p>'
                '<p>El <a href="https://example.org/informe">informe de la resolución</a> está disponible.</p></div>')
        self.assertEqual(article_text(html), 'La empresa negó las acusaciones de fraude.\n\n'
                         'El informe de la resolución está disponible.')

    def test_editorial_discussion_of_tags_does_not_truncate_a_denial_or_victim(self):
        for label in ('Tags: el término significa etiquetas.',
                      'Etiquetas: términos utilizados en el sistema estudiado.', 'Contenido relacionado'):
            with self.subTest(label=label):
                html = ('<article><p>' + label + '</p><p>Grupo Bimbo no fue acusado de fraude.</p>'
                        '<p>La empresa fue víctima de fraude.</p></article>')
                text = article_text(html)
                self.assertIn(label, text)
                self.assertIn('no fue acusado', text)
                self.assertIn('fue víctima', text)

    def test_lee_mas_navigation_does_not_replace_or_extend_genuine_editorial_claims(self):
        html = ('<article><p>La Empresa Bruma fue investigada por fraude.</p>'
                '<p>Lee más: <a href="https://example.org/otra">Otra persona fue condenada.</a></p>'
                '<p>La Empresa Bruma negó las acusaciones de fraude.</p></article>')
        self.assertEqual(article_text(html), 'La Empresa Bruma fue investigada por fraude.\n\n'
                         'La Empresa Bruma negó las acusaciones de fraude.')


if __name__ == '__main__':
    unittest.main()
