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


if __name__ == '__main__':
    unittest.main()
