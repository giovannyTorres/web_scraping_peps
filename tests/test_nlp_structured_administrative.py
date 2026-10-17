"""Semantic controls for explicit administrative recipients and article lists.

Invented names exercise grammar only; performance claims use captured articles.
"""
import unittest

from peps.nlp import analyze


ENTITY = {'id': 'semantic-administrative', 'name': 'Ana Nopal Villaluz',
          'kind': 'person', 'aliases': [], 'context': {}}


class StructuredAdministrativeTests(unittest.TestCase):
    def direct(self, text):
        result = analyze(text, ENTITY)
        for finding in result['findings']:
            self.assertEqual(text[finding['start']:finding['end']], finding['quote'])
            self.assertTrue(text[finding['trigger_start']:finding['trigger_end']].strip())
            self.assertGreaterEqual(finding['trigger_start'], finding['start'])
            self.assertLessEqual(finding['trigger_end'], finding['end'])
        return [f for f in result['findings'] if f['relationship'] == 'direct'
                and not f['negated'] and not f['hypothetical']]

    def test_enacted_reprimand_and_professional_dismissal_are_administrative_only(self):
        for text in (
            'El instituto impuso una amonestación pública a la alcaldesa de Villa Nopal, '
            'Ana Nopal Villaluz, por incumplir con obligaciones de transparencia.',
            'El Gobierno anunció la destitución de Ana Nopal Villaluz, titular de la '
            'Notaría 12, por faltas administrativas.',
        ):
            with self.subTest(text=text):
                findings = self.direct(text)
                category = 'sanctions' if 'amonestación' in text else 'reputational'
                self.assertEqual({(f['stage'], f['category']) for f in findings},
                                 {('sanction', category)})
                self.assertTrue(all(f['context_review_required'] for f in findings))

    def test_reprimand_and_dismissal_do_not_borrow_another_recipient_or_negated_grounds(self):
        for text in (
            'El instituto no impuso una amonestación pública a la alcaldesa de Villa Nopal, Ana Nopal Villaluz.',
            'Es falso que el instituto impuso una amonestación pública a la alcaldesa de Villa Nopal, Ana Nopal Villaluz.',
            'Ana Nopal Villaluz informó que el instituto impuso una amonestación pública al alcalde Otro Nombre Distinto.',
            'El instituto impuso una amonestación pública al hermano de Ana Nopal Villaluz.',
            'El Gobierno anunció la destitución de Ana Nopal Villaluz, titular de la Notaría 12, sin faltas administrativas.',
            'El Gobierno anunció la destitución de Ana Nopal Villaluz, titular de la Notaría 12, por renuncia voluntaria.',
            'El Gobierno podría anunciar la destitución de Ana Nopal Villaluz, titular de la Notaría 12, por irregularidades.',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.direct(text), [])

    def test_professional_colon_row_is_explicit_but_directory_row_is_neutral(self):
        text = ('Ana Nopal Villaluz (Notaría Pública Número 12 de Villa Nopal): '
                'Revocación e inhabilitación definitiva (11/03/2025).')
        self.assertEqual({f['stage'] for f in self.direct(text)}, {'sanction'})
        for altered in (
            text.replace('Revocación e inhabilitación definitiva', 'Nombramiento y toma de protesta'),
            text.replace('Revocación e inhabilitación definitiva', 'No hubo revocación e inhabilitación'),
            text.replace('Notaría Pública Número 12 de Villa Nopal', 'Empresa de consultoría'),
            'Ana Nopal Villaluz explicó la revocación e inhabilitación de otro notario.',
            'La hermana de ' + text,
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_numbered_sanction_list_retains_heading_and_name_without_joining_new_prose(self):
        text = ('Los notarios sancionados:\n\n'
                '1. Otro Nombre Distinto, Notario Público número 4 (marzo de 2024).\n\n'
                '2. Ana Nopal Villaluz, Notario Público número 12 (junio de 2025).')
        findings = self.direct(text)
        self.assertEqual({f['stage'] for f in findings}, {'sanction'})
        self.assertTrue(all('Los notarios sancionados:' in f['quote'] for f in findings))
        for altered in (
            text.replace('Los notarios sancionados:', 'Los notarios inscritos:'),
            text.replace('Los notarios sancionados:', 'Los notarios no sancionados:'),
            text.replace('2. Ana Nopal Villaluz, Notario Público',
                         'Ana Nopal Villaluz comentó el caso.\n\n2. Otro Nombre Tercero, Notario Público'),
            text.replace('2. Ana Nopal Villaluz', 'Otra noticia sin relación.\n\n2. Ana Nopal Villaluz'),
            text.replace('2. Ana Nopal Villaluz', '2. El hermano de Ana Nopal Villaluz'),
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_professional_account_suspension_list_and_explicit_continuation(self):
        header = ('Los notarios a los que se les suspendieron las cuentas y funciones son '
                  'Otro Nombre Distinto, notario público 4; ')
        for suffix in (
            'Ana Nopal Villaluz, notario público 12.',
            'Otro Nombre Tercero, notario público 5.\n\nAsí como a Ana Nopal Villaluz, notario público 12.',
        ):
            with self.subTest(suffix=suffix):
                findings = self.direct(header + suffix)
                self.assertEqual({f['stage'] for f in findings}, {'sanction'})
        for suffix in (
            'Otra Persona Neutral, notario público 5.\n\nAna Nopal Villaluz, notario público 12, informó del asunto.',
            'Otra Persona Neutral, notario público 5.\n\nLa directora Ana Nopal Villaluz publicó estadísticas.',
            'El hermano de Ana Nopal Villaluz, notario público 12.',
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.direct(header + suffix), [])

    def test_reported_accusation_list_is_an_allegation_and_never_a_conviction(self):
        header = ('En las acusaciones aparecen los nombres de los notarios públicos '
                  'Otro Nombre Distinto (5); ')
        for suffix in (
            'Ana Nopal Villaluz (12).',
            'Otro Nombre Tercero (6).\n\nOtros notarios más son, Ana Nopal Villaluz 12.',
        ):
            with self.subTest(suffix=suffix):
                findings = self.direct(header + suffix)
                self.assertEqual({f['stage'] for f in findings}, {'allegation'})
                self.assertTrue(all(f['uncertain'] for f in findings))
        for altered in (
            header.replace('En las acusaciones', 'En el directorio') + 'Ana Nopal Villaluz (12).',
            header + 'Otra Persona Neutral (6).\n\nAna Nopal Villaluz denunció las irregularidades.',
            header + 'Otra Persona Neutral (6).\n\nOtros notarios más son, el hermano de Ana Nopal Villaluz 12.',
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_identifying_followup_links_only_the_same_professional_suspension(self):
        source = ('El titular de la notaría 12 fue suspendido de sus actividades por dos días, '
                  'debido a anomalías en su actuación.')
        for separator in ('\n', '\n\n'):
            text = source + separator + 'Se trata del notario Ana Nopal Villaluz, sobre quien existe una queja.'
            with self.subTest(separator=separator):
                self.assertEqual({f['stage'] for f in self.direct(text)}, {'sanction'})
        for suffix in (
            'Ana Nopal Villaluz comentó la noticia.',
            'Se trata del notario Otro Nombre Distinto, según Ana Nopal Villaluz.',
            'Se trata del notario hermano de Ana Nopal Villaluz.',
        ):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.direct(source + '\n\n' + suffix), [])
        self.assertEqual(self.direct(source.replace('a anomalías', 'sin anomalías') +
                                    '\n\nSe trata del notario Ana Nopal Villaluz.'), [])

    def test_explicit_electoral_resolution_is_separate_from_criminal_conviction(self):
        text = ('El Tribunal Electoral determinó que la alcaldesa de Villa Nopal, Ana Nopal Villaluz, '
                'incurrió en violencia política en razón de género contra otra funcionaria.')
        self.assertEqual({(f['stage'], f['category']) for f in self.direct(text)},
                         {('sanction', 'reputational')})
        for altered in (
            text.replace('incurrió', 'no incurrió'),
            text.replace('determinó', 'no determinó'),
            text.replace('Ana Nopal Villaluz,', 'Otra Persona Distinta,') + ' Ana Nopal Villaluz informó.',
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_investigation_of_joint_objects_never_drops_the_second_named_recipient(self):
        text = ('Se investiga la complicidad de dos notarios involucrados en estos hechos, '
                'Otro Nombre Distinto y Ana Nopal Villaluz, notario público número 12.')
        self.assertTrue(any(f['stage'] == 'investigation' for f in self.direct(text)))
        text = ('Otro Nombre Distinto fue investigado por fraude y Ana Nopal Villaluz '
                'presentó el informe público.')
        self.assertEqual(self.direct(text), [])

    def test_historical_revocation_summary_requires_explicit_recipient_links(self):
        source = ('Las primeras revocaciones se dieron en marzo de 2020 y fueron para los '
                  'notarios Otro Nombre Distinto y Otro Nombre Tercero.\n\n')
        for continuation in (
            'El año siguiente sólo hubo una y fue la del notario público 12 de Villa Nopal, Ana Nopal Villaluz.',
            'El año siguiente sólo hubo una y fue la del notario público 5 de Villa Nopal, '
            'Otra Persona Neutral, mientras que en 2022 se registró la de Ana Nopal Villaluz, notario público 12.',
        ):
            with self.subTest(continuation=continuation):
                self.assertEqual({f['stage'] for f in self.direct(source + continuation)}, {'sanction'})
        text = ('Con cuatro casos, 2025 es el año con más revocaciones. Son las del notario 5 de '
                'Villa Nopal, Otra Persona Neutral; el notario 12 de Villa Nopal, Ana Nopal Villaluz.')
        self.assertEqual({f['stage'] for f in self.direct(text)}, {'sanction'})
        for altered in (
            source + 'Ana Nopal Villaluz, notario público 12, explicó las decisiones.',
            source + 'El año siguiente se presentaron nuevos notarios. Ana Nopal Villaluz acudió a la reunión.',
            text.replace('Ana Nopal Villaluz.', 'Otra Persona Distinta, según informó Ana Nopal Villaluz.'),
            text.replace('más revocaciones', 'ninguna revocación'),
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_official_document_allegation_requires_same_subject_and_explicit_adverse_conduct(self):
        text = ('Ana Nopal Villaluz, expresidente del Tribunal de Justicia y relacionado, de acuerdo con '
                'oficios de dos autoridades, en actos de corrupción e irregularidades durante su gestión.')
        findings = self.direct(text)
        self.assertEqual({f['stage'] for f in findings}, {'allegation'})
        self.assertTrue(all(f['uncertain'] for f in findings))
        for altered in (
            text.replace('relacionado', 'no relacionado'),
            text.replace('actos de corrupción', 'actos oficiales'),
            'Ana Nopal Villaluz informó del caso. ' + text.replace('Ana Nopal Villaluz', 'Otro Nombre Distinto'),
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_continued_complaint_requires_explicit_accusation_context(self):
        source = ('Según la denuncia actual, otros cuatro notarios habrían ayudado al otorgamiento '
                  'de permisos irregulares.\n\n')
        target = 'Además, involucró también al notario 12 Ana Nopal Villaluz.'
        findings = self.direct(source + target)
        self.assertEqual({f['stage'] for f in findings}, {'allegation'})
        for altered in (
            target,
            source.replace('Según la denuncia actual', 'Según el directorio actual') + target,
            source + target.replace('involucró', 'no involucró'),
            source + target.replace('Ana Nopal Villaluz', 'Otro Nombre Distinto') +
            ' Ana Nopal Villaluz comentó el caso.',
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_reported_order_of_repression_is_an_allegation_without_a_conviction(self):
        text = ('Ana Nopal Villaluz ordenó a granaderos y policías la represión a manifestantes '
                'que protestaban en la plaza pública.')
        self.assertEqual({f['stage'] for f in self.direct(text)}, {'allegation'})
        for altered in (
            text.replace('ordenó', 'no ordenó'),
            text.replace('ordenó', 'podría haber ordenado'),
            'El hermano de ' + text,
            'Ana Nopal Villaluz informó que Otro Nombre Distinto ordenó a policías la represión a manifestantes.',
        ):
            with self.subTest(text=altered):
                self.assertEqual(self.direct(altered), [])

    def test_admission_alone_never_becomes_a_conviction_and_no_alias_is_invented(self):
        text = ('Ana Nopal Villaluz compareció ante el juez.\n\n'
                'Villaluz se declaró culpable de un delito y espera la sentencia.')
        self.assertEqual(self.direct(text), [])
        self.assertEqual([m['text'] for m in analyze(text, ENTITY)['mentions']], ['Ana Nopal Villaluz'])


if __name__ == '__main__':
    unittest.main()
