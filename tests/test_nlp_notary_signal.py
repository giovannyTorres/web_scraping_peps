"""Conservative enacted notarial measures; synthetic names only."""
import unittest
from peps.nlp import analyze

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

if __name__ == '__main__':
    unittest.main()
