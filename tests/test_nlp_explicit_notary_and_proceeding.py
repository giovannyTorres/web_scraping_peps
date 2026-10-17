"""Neutral semantic regressions developed after real false negatives were unsealed."""
import unittest

from peps.nlp import analyze
from peps.scoring import score_entity

ENTITY = {'id': 'neutral-proceeding', 'name': 'Ana Nopal Villaluz',
          'kind': 'person', 'aliases': [], 'context': {}}


class ExplicitNotaryAndProceedingTests(unittest.TestCase):
    def adverse(self, text):
        return [finding for finding in analyze(text, ENTITY)['findings']
                if finding['relationship'] == 'direct' and not finding['negated']
                and not finding['hypothetical']]

    def test_professional_possessive_patent_requires_explicit_notary_and_adverse_grounds(self):
        for title in ('notario público', 'notaria pública', 'notario'):
            text = ('El gobierno revocó la patente del ' + title +
                    ' Ana Nopal Villaluz por faltas administrativas graves.')
            with self.subTest(title=title):
                findings = self.adverse(text)
                self.assertEqual({f['stage'] for f in findings}, {'sanction'})
                self.assertTrue(all(text[f['start']:f['end']] == f['quote'] for f in findings))
                self.assertTrue(all(text[f['trigger_start']:f['trigger_end']] == 'revocó la patente'
                                    for f in findings))

    def test_professional_patent_never_borrows_another_recipient_or_unrelated_object(self):
        for text in (
            'Por irregularidades, Ana Nopal Villaluz informó que el gobierno revocó la patente '
            'del notario Otro Nombre Distinto.',
            'Por irregularidades, el notario Ana Nopal Villaluz revocó la patente del notario Otro Nombre Distinto.',
            'El gobierno revocó la patente del notario Otro Nombre Distinto por faltas administrativas. '
            'Ana Nopal Villaluz informó sobre el asunto.',
            'Por irregularidades, el gobierno revocó la licencia de construcción del notario Ana Nopal Villaluz.',
            'Por irregularidades, el gobierno revocó la patente del hermano del notario Ana Nopal Villaluz.',
            'El gobierno revocó la patente del notario Ana Nopal Villaluz por cambio de adscripción.',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.adverse(text), [])

    def test_patent_negation_hypothesis_and_voluntary_exit_stay_unscored(self):
        for text in (
            'Por irregularidades, el gobierno no revocó la patente del notario Ana Nopal Villaluz.',
            'Por irregularidades, es falso que el gobierno revocó la patente del notario Ana Nopal Villaluz.',
            'Por irregularidades, el gobierno podría revocar la patente del notario Ana Nopal Villaluz.',
            'Por irregularidades, el gobierno revocó la patente del notario Ana Nopal Villaluz '
            'tras su renuncia voluntaria.',
            'El gobierno revocó la patente del notario Ana Nopal Villaluz sin irregularidades.',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.adverse(text), [])

    def test_release_with_same_subject_continuing_proceeding_is_only_an_investigation(self):
        for link in ('abandonó la cárcel, sin embargo,', 'abandonó prisión, pero'):
            text = ('La ex titular, Ana Nopal Villaluz, ' + link +
                    ' continuará su proceso con medidas cautelares, informó la autoridad.')
            with self.subTest(link=link):
                findings = self.adverse(text)
                self.assertEqual({(f['stage'], f['category']) for f in findings},
                                 {('investigation', 'reputational')})
                self.assertTrue(all(text[f['start']:f['end']] == f['quote'] for f in findings))
                self.assertTrue(all(text[f['trigger_start']:f['trigger_end']] ==
                    'continuará su proceso con medidas cautelares' for f in findings))
                self.assertFalse(any(f['stage'] in ('conviction', 'arrest', 'acquittal', 'dismissed')
                                     for f in analyze(text, ENTITY)['findings']))

    def test_liberty_alone_and_another_subject_never_create_a_direct_proceeding(self):
        for text in (
            'Ana Nopal Villaluz abandonó la cárcel y quedó en libertad bajo caución.',
            'Ana Nopal Villaluz abandonó la cárcel, sin embargo, continuará su proceso electoral.',
            'Ana Nopal Villaluz abandonó la cárcel, pero Otro Nombre Distinto '
            'continuará su proceso con medidas cautelares.',
            'Ana Nopal Villaluz informó que Otro Nombre Distinto abandonó la cárcel, '
            'sin embargo, continuará su proceso con medidas cautelares.',
            'El hermano de Ana Nopal Villaluz abandonó la cárcel, sin embargo, '
            'continuará su proceso con medidas cautelares.',
            'Ana Nopal Villaluz no abandonó la cárcel, sin embargo, '
            'continuará su proceso con medidas cautelares.',
            'Ana Nopal Villaluz abandonó la cárcel, sin embargo, '
            'no continuará su proceso con medidas cautelares.',
            'Ana Nopal Villaluz podría abandonar la cárcel, sin embargo, '
            'continuará su proceso con medidas cautelares.',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.adverse(text), [])

    def test_new_candidates_never_bypass_source_body_or_human_validation(self):
        text = ('Ana Nopal Villaluz abandonó la cárcel, sin embargo, '
                'continuará su proceso con medidas cautelares.')
        evidence = {'id': 'neutral-proceeding-proof', 'source_kind': 'news',
                    'source_url': 'https://example.org/proceeding', 'text': text,
                    'published_at': '2026-10-15'}
        result = score_entity(ENTITY, [evidence], '2026-10-17')
        self.assertGreater(result['provisional_score'], 0)
        self.assertEqual(result['validated_score'], 0)
        self.assertTrue(result['review_required'])
        for altered in ({**evidence, 'text': '', 'title': text},
                        {**evidence, 'source_kind': 'discovery'},
                        {**evidence, 'metadata': {'discovery_only': True}}):
            with self.subTest(evidence=altered):
                self.assertEqual(score_entity(ENTITY, [altered], '2026-10-17')['score'], 0)


if __name__ == '__main__':
    unittest.main()
