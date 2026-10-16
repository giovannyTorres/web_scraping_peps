"""Line boundaries preserve recall and never join separate headlines."""
import hashlib
import unittest

from peps.nlp import _sentences, analyze
from peps.scoring import score_entity


ENTITY = {'id': 'synthetic', 'name': 'José Peña Villaluz', 'kind': 'person'}


class SentenceBoundaryTests(unittest.TestCase):
    def assert_exact_offsets(self, text, result):
        for mention in result['mentions']:
            self.assertEqual(text[mention['start']:mention['end']], mention['text'])
        for finding in result['findings']:
            self.assertEqual(text[finding['start']:finding['end']], finding['quote'])
            self.assertGreaterEqual(finding['trigger_start'], finding['start'])
            self.assertLessEqual(finding['trigger_end'], finding['end'])
            self.assertNotIn('\n', finding['quote'])
            self.assertNotIn('\r', finding['quote'])

    def test_line_without_final_punctuation_is_a_complete_source_span(self):
        text = 'Primer titular sin punto\n\nSegundo titular sin punto\nÚltimo titular'
        spans = list(_sentences(text, []))
        self.assertEqual([span[2] for span in spans],
                         ['Primer titular sin punto', 'Segundo titular sin punto', 'Último titular'])
        for start, end, quote in spans:
            self.assertEqual(text[start:end], quote)

    def test_unpunctuated_headline_retains_a_real_literal_finding(self):
        for line_end in ('\n', '\r\n'):
            text = '📰 José Peña Villaluz fue acusado de fraude' + line_end + 'La audiencia está pendiente'
            result = analyze(text, ENTITY)
            self.assertTrue(any(finding['stage'] == 'allegation' for finding in result['findings']))
            self.assert_exact_offsets(text, result)

    def test_findings_of_other_headlines_do_not_transfer_to_a_neutral_entity(self):
        text = ('Otra persona fue condenada por fraude\n'
                'José Peña Villaluz presentó su informe anual\n'
                'Otra empresa recibió una sanción')
        result = analyze(text, ENTITY)
        self.assertEqual(result['findings'], [])
        self.assert_exact_offsets(text, result)

    def test_negation_and_victim_lines_remain_non_adverse(self):
        for line in ('José Peña Villaluz no fue acusado de fraude',
                     'José Peña Villaluz fue víctima de fraude',
                     'José Peña Villaluz fue absuelto de fraude'):
            text = line + '\nOtra persona fue condenada por corrupción'
            result = analyze(text, ENTITY)
            self.assert_exact_offsets(text, result)
            evidence = {'id': 'e', 'source_kind': 'official', 'source_url': 'https://example.org/source',
                        'text': text, 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                        'published_at': '2026-10-15'}
            self.assertEqual(score_entity(ENTITY, [evidence], '2026-10-16')['score'], 0)
            if 'no fue' in line:
                self.assertTrue(any(finding['negated'] for finding in result['findings']))

    def test_unicode_and_decomposed_accents_keep_original_line_offsets(self):
        text = ('Otra noticia\n📰 JOSE\u0301 PEÑA VILLALUZ fue acusado de fraude\n'
                'Continúa la información neutral')
        result = analyze(text, ENTITY)
        self.assertTrue(result['findings'])
        self.assert_exact_offsets(text, result)
        self.assertEqual(result['mentions'][0]['start'], text.index('JOSE'))


class CorporateSubjectTests(unittest.TestCase):
    COMPANY = {'id': 'synthetic-company', 'name': 'Constructora Bruma', 'kind': 'company'}

    def evidence(self, text):
        return {'id': 'e', 'source_kind': 'official', 'source_url': 'https://example.org/source',
                'text': text, 'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                'published_at': '2026-10-15'}

    def score(self, text):
        return score_entity(self.COMPANY, [self.evidence(text)], '2026-10-16')['score']

    def assert_offsets(self, text, findings):
        for finding in findings:
            self.assertEqual(text[finding['start']:finding['end']], finding['quote'])
            self.assertTrue(text[finding['trigger_start']:finding['trigger_end']])

    def test_officers_employees_and_representatives_do_not_donate_conduct_to_company(self):
        for text in ('Está detenido el exdirector de Constructora Bruma por fraude.',
                     'El director general de Constructora Bruma fue investigado por fraude.',
                     'El representante legal de Constructora Bruma fue condenado por fraude.',
                     'Los empleados de Constructora Bruma fueron detenidos por fraude.',
                     'Constructora Bruma, cuyo exdirector fue investigado por fraude, publicó un informe.'):
            with self.subTest(text=text):
                findings = analyze(text, self.COMPANY)['findings']
                self.assertTrue(findings)
                self.assertTrue(all(finding['relationship'] != 'direct' for finding in findings))
                self.assertEqual(self.score(text), 0)
                self.assert_offsets(text, findings)

    def test_explicit_investigated_people_leave_later_company_mention_as_context(self):
        text = ('Acerca del caso de los políticos panistas actualmente investigados, se mencionaron '
                'las investigaciones de Constructora Bruma y su relación con otra empresa.')
        findings = analyze(text, self.COMPANY)['findings']
        self.assertTrue(findings)
        self.assertTrue(all(finding['relationship'] == 'context' for finding in findings))
        self.assertEqual(self.score(text), 0)
        self.assert_offsets(text, findings)

    def test_company_that_is_actually_investigated_retains_its_direct_finding(self):
        for text in ('Constructora Bruma fue investigada por fraude.',
                     'La autoridad investiga a Constructora Bruma por fraude.',
                     'El director de Constructora Bruma declaró que Constructora Bruma fue investigada por fraude.',
                     'Constructora Bruma y su director fueron investigados por fraude.'):
            with self.subTest(text=text):
                findings = analyze(text, self.COMPANY)['findings']
                self.assertTrue(any(finding['relationship'] == 'direct' for finding in findings))
                self.assertGreater(self.score(text), 0)
                self.assert_offsets(text, findings)

    def test_officer_relative_clause_does_not_hide_genuine_company_predicate(self):
        text = 'Constructora Bruma, cuyo director fue detenido, fue investigada por fraude.'
        findings = analyze(text, self.COMPANY)['findings']
        self.assertTrue(any(finding['stage'] == 'arrest' and finding['relationship'] == 'context' for finding in findings))
        self.assertTrue(any(finding['stage'] == 'investigation' and finding['relationship'] == 'direct' for finding in findings))
        self.assertGreater(self.score(text), 0)

    def test_administrative_fine_is_sanction_not_criminal_conviction(self):
        for text in ('La autoridad multó con nueve millones de pesos a Constructora Bruma por no notificar una operación.',
                     'La autoridad multa a Constructora Bruma por no notificar una operación.',
                     'Constructora Bruma fue multada por no notificar una operación.'):
            with self.subTest(text=text):
                findings = analyze(text, self.COMPANY)['findings']
                self.assertTrue(findings)
                self.assertEqual({(finding['category'], finding['stage'], finding['relationship']) for finding in findings},
                                 {('sanctions', 'sanction', 'direct')})
                self.assertGreater(self.score(text), 0)
                self.assert_offsets(text, findings)

    def test_possible_denied_or_another_persons_fine_does_not_score_company(self):
        for text in ('Constructora Bruma podría ser multada por no notificar una operación.',
                     'Posible multa a Constructora Bruma por no notificar una operación.',
                     'Constructora Bruma negó haber sido multada.',
                     'Constructora Bruma pagó la multa de su exdirector.',
                     'La autoridad multó al director de Constructora Bruma por fraude.'):
            with self.subTest(text=text):
                self.assertEqual(self.score(text), 0)
                self.assert_offsets(text, analyze(text, self.COMPANY)['findings'])


if __name__ == '__main__':
    unittest.main()
