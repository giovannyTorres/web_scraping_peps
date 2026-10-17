"""Conservative enacted notarial measures; synthetic names only."""
import unittest
from peps.nlp import analyze
from peps.scoring import score_entity

ENTITY = {'aliases': [], 'id': 'synthetic-revocation', 'kind': 'person', 'name': 'Ana Nopal Villaluz'}
FIXTURES = [(True,
  'Por irregularidades, el gobierno revocó la patente de notario público a Ana Nopal Villaluz.'),
 (True,
  'Por falta grave de probidad, el gobernador revocó la patente de la notaría pública número 13 de '
  'Villa Nopal a Ana Nopal Villaluz.'),
 (True,
  'Tras un procedimiento de responsabilidad notarial, el gobierno revocó de manera definitiva el '
  'nombramiento, así como su inhabilitación al notario número 126 del municipio de Villa Nopal, '
  'Ana Nopal Villaluz.'),
 (True,
  'Tras concluir el proceso sancionador, las autoridades determinaron revocar de la Notaría 102 a '
  'Ana Nopal Villaluz.'),
 (True,
  'La Secretaría decidió suspender las funciones del ejercicio notarial a Ana Nopal Villaluz, a '
  'quien acreditó falta de probidad.'),
 (True,
  'Por irregularidades, le fue cancelado su nombramiento como notario público a Ana Nopal '
  'Villaluz.'),
 (False,
  'Por irregularidades, el notario Ana Nopal Villaluz revocó el poder notarial otorgado por otra '
  'persona.'),
 (False,
  'Por irregularidades, el notario Ana Nopal Villaluz revocó la patente a Otra Persona Distinta.'),
 (False,
  'Por irregularidades, el gobierno no revocó la patente de notario público a Ana Nopal Villaluz.'),
 (False,
  'Por irregularidades, el gobierno podría revocar la patente de notario público a Ana Nopal '
  'Villaluz.'),
 (False,
  'Por irregularidades, se solicita revocar la patente de notario público a Ana Nopal Villaluz.'),
 (False,
  'El gobierno revocó la patente de notario público a Ana Nopal Villaluz por su renuncia '
  'voluntaria, sin irregularidades.'),
 (False,
  'Ana Nopal Villaluz explicó que por irregularidades el gobierno revocó la patente de notario '
  'público a Otra Persona Distinta.'),
 (False,
  'Por irregularidades, el gobierno revocó la patente de notario público a Otra Persona Distinta, '
  'según explicó Ana Nopal Villaluz.'),
 (False,
  'Por irregularidades, el gobierno revocó la patente de notario público al notario Otra Persona '
  'Distinta, familiar de Ana Nopal Villaluz.'),
 (False,
  'Ana Nopal Villaluz acudió como experto a explicar la revocación de patentes profesionales.'),
 (False,
  'El gobierno revocó la patente de notario público a Otra Persona Distinta. Ana Nopal Villaluz '
  'explicó las irregularidades.'),
 (False,
  'Por irregularidades, el gobierno revocó la licencia de construcción a Ana Nopal Villaluz, quien '
  'también es notario.'),
 (False,
  'Por irregularidades, el gobierno revocó la licencia de construcción al notario Ana Nopal '
  'Villaluz.'),
 (False,
  'Por irregularidades, el gobierno revocó el nombramiento de presidente de una asociación al '
  'notario Ana Nopal Villaluz.'),
 (False,
  'El gobierno revocó la patente de notario público a Ana Nopal Villaluz por cambio regular de '
  'adscripción.')]

class NotarialMeasureTests(unittest.TestCase):
    def test_measure_requires_professional_object_explicit_recipient_and_adverse_grounds(self):
        for expected, text in FIXTURES:
            with self.subTest(expected=expected, text=text):
                result = analyze(text, ENTITY)
                findings = [finding for finding in result['findings']
                            if finding['stage'] == 'sanction' and finding['relationship'] == 'direct'
                            and not finding['negated'] and not finding['hypothetical']]
                self.assertEqual(bool(findings), expected)
                for finding in findings:
                    self.assertEqual(text[finding['start']:finding['end']], finding['quote'])

    def test_received_enacted_professional_measure_is_not_a_reporter(self):
        for text in (
            'Por presuntas anomalías, la notaria Ana Nopal Villaluz, recibió una suspensión '
            'de sus actividades notariales por doce días.',
            'La notaria Ana Nopal Villaluz, recibió una segunda inhabilitación definitiva '
            'a su cargo como fedatario, al resolverse una segunda queja.',
        ):
            with self.subTest(text=text):
                findings = analyze(text, ENTITY)['findings']
                self.assertEqual({(f['stage'], f['relationship']) for f in findings},
                                 {('sanction', 'direct')})
                self.assertTrue(all(not f['negated'] and not f['hypothetical'] for f in findings))
                for finding in findings:
                    self.assertEqual(text[finding['start']:finding['end']], finding['quote'])
                    self.assertTrue(text[finding['trigger_start']:finding['trigger_end']].startswith('recibió'))

    def test_active_disqualification_requires_explicit_notarial_recipient(self):
        for title in ('la notaria', 'la titular de la notaría 50 de Villa Nopal,',
                      'la titular de la notaría número 50 de Villa Nopal,'):
            text = ('La Consejería inhabilitó de manera definitiva a ' + title +
                    ' Ana Nopal Villaluz por actuaciones irregulares en instrumentos públicos.')
            with self.subTest(text=text):
                self.assertTrue(any(f['stage'] == 'sanction' and f['relationship'] == 'direct'
                                    for f in analyze(text, ENTITY)['findings']))

    def test_possessive_recipient_requires_immediate_professional_appositive(self):
        text = ('El gobierno revocó el nombramiento de Ana Nopal Villaluz, notaria número 15 '
                'de Villa Nopal, por incumplir disposiciones de la Ley del Notariado.')
        self.assertTrue(any(f['stage'] == 'sanction' and f['relationship'] == 'direct'
                            for f in analyze(text, ENTITY)['findings']))
        for replacement in ('presidenta de una asociación', 'familiar de la notaria',
                            'quien explicó el nombramiento de la notaria'):
            with self.subTest(replacement=replacement):
                altered = text.replace('notaria número 15 de Villa Nopal', replacement)
                self.assertEqual(analyze(altered, ENTITY)['findings'], [])

    def test_notarial_changes_do_not_attribute_receipt_reports_or_another_recipient(self):
        for text in (
            'Por anomalías, la notaria Ana Nopal Villaluz recibió la noticia de una suspensión '
            'de las actividades notariales de Otra Persona Distinta.',
            'Por anomalías, la notaria Ana Nopal Villaluz informó que Otra Persona Distinta '
            'recibió una suspensión de sus actividades notariales.',
            'Por irregularidades, la notaria Ana Nopal Villaluz inhabilitó a Otra Persona Distinta.',
            'La autoridad inhabilitó para conducir a la notaria Ana Nopal Villaluz por irregularidades.',
            'La notaria Ana Nopal Villaluz recibió una suspensión de su cuenta bancaria por anomalías.',
            'Se anunció una suspensión de actividades notariales por irregularidades. '
            'Ana Nopal Villaluz describió la queja.',
        ):
            with self.subTest(text=text):
                self.assertEqual(analyze(text, ENTITY)['findings'], [])

    def test_denied_hypothetical_voluntary_and_unfounded_measures_are_not_adverse(self):
        for text in (
            'Por anomalías, Ana Nopal Villaluz no recibió una suspensión de sus actividades notariales.',
            'Por irregularidades, el gobierno nunca inhabilitó a la notaria Ana Nopal Villaluz.',
            'Por irregularidades, el gobierno podría inhabilitar a la notaria Ana Nopal Villaluz.',
            'La autoridad pide revocar el nombramiento de Ana Nopal Villaluz, notaria de Villa Nopal, '
            'por incumplir disposiciones.',
            'La autoridad revocó el nombramiento de Ana Nopal Villaluz, notaria de Villa Nopal, '
            'por renuncia voluntaria y sin irregularidades.',
            'La notaria Ana Nopal Villaluz recibió una suspensión de sus actividades notariales '
            'por solicitud propia, sin anomalías.',
            'La autoridad revocó el nombramiento de Ana Nopal Villaluz, notaria de Villa Nopal, '
            'sin irregularidades ni incumplimiento.',
        ):
            with self.subTest(text=text):
                findings = analyze(text, ENTITY)['findings']
                self.assertFalse(any(f['relationship'] == 'direct' and not f['negated']
                                     and not f['hypothetical'] for f in findings))

    def test_provisional_priority_uses_body_and_preserves_unreviewed_identity(self):
        text = ('Por anomalías, Ana Nopal Villaluz recibió una suspensión '
                'de sus actividades notariales.')
        evidence = {'id': 'synthetic-professional-measure', 'source_kind': 'news',
                    'source_url': 'https://example.org/professional-measure',
                    'text': text, 'published_at': '2026-10-15'}
        score = score_entity(ENTITY, [evidence], '2026-10-17')
        self.assertGreater(score['provisional_score'], 0)
        self.assertEqual(score['validated_score'], 0)
        self.assertTrue(score['review_required'])
        self.assertEqual(score['meaning'], 'document_review_priority')
        for altered in ({**evidence, 'text': '', 'title': text},
                        {**evidence, 'source_kind': 'discovery'},
                        {**evidence, 'metadata': {'discovery_only': True}}):
            self.assertEqual(score_entity(ENTITY, [altered], '2026-10-17')['score'], 0)

if __name__ == '__main__':
    unittest.main()
