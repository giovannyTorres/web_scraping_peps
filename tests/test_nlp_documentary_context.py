"""Fictional semantic controls; unsealed V1 is development, never held-out.

These pairs vary who receives a measure, the connective, and its disposition.
They check every actionable route, not only a newly added rule's output.
"""
import hashlib
import unittest

from peps.document_review import document_review
from peps.nlp import analyze


ENTITY = {'id': 'invented-public-role', 'name': 'Ana Nopal Villaluz',
          'kind': 'person', 'aliases': [], 'context': {}}


class DocumentaryContextTests(unittest.TestCase):
    def direct(self, text):
        result = analyze(text, ENTITY)
        for finding in result['findings']:
            self.assertEqual(text[finding['start']:finding['end']], finding['quote'])
            self.assertTrue(text[finding['trigger_start']:finding['trigger_end']])
            self.assertLessEqual(finding['start'], finding['trigger_start'])
            self.assertLessEqual(finding['trigger_end'], finding['end'])
        return [f for f in result['findings'] if f['relationship'] == 'direct'
                and not f['negated'] and not f['hypothetical']
                and f['stage'] not in ('acquittal', 'dismissed', 'denial')]

    def test_admonition_recipient_is_sanction_even_when_a_citizen_later_reports_it(self):
        text = ('El pleno impuso una amonestación pública para la Presidenta Municipal del '
                'Ayuntamiento de Villa Nopal, Ana Nopal Villaluz, un ciudadano denunció '
                'la falta de información en el portal público.')
        self.assertEqual({(f['stage'], f['category']) for f in self.direct(text)},
                         {('sanction', 'sanctions')})
        for replacement in ('no impuso', 'podría imponer', 'solicitó una'):
            altered = text.replace('impuso una', replacement)
            self.assertEqual(self.direct(altered), [], altered)

    def test_semicolon_list_scope_does_not_reach_the_witness_or_an_unrelated_paragraph(self):
        header = ('El resto de amonestaciones se impuso a Otra Persona Distinta, '
                  'alcaldesa de Villa Azul; ')
        text = header + 'Ana Nopal Villaluz, presidenta de Villa Nopal; Otra Persona Tercera, directora del DIF.'
        self.assertEqual({(f['stage'], f['category']) for f in self.direct(text)},
                         {('sanction', 'sanctions')})
        for suffix in (
            'la abogada Ana Nopal Villaluz informó de las medidas.',
            'Otra Persona Tercera, directora del DIF. Ana Nopal Villaluz firmó como testigo.',
            'el hermano de Ana Nopal Villaluz, presidente de Villa Nopal.',
            'Otra Persona Tercera, directora del DIF.\n\nAna Nopal Villaluz publicó su informe.',
        ):
            self.assertEqual(self.direct(header + suffix), [], suffix)

    def test_admonition_heading_requires_an_unbroken_explicit_recipient_list(self):
        header = ('Se impuso 01 amonestación pública, a los siguientes servidores públicos '
                  'y ex servidores públicos:\n\n')
        entry = 'Al presidente de Villa Nopal, Ana Nopal Villaluz.'
        text = header + 'A la directora del DIF de Villa Azul, Otra Persona Distinta.\n\n' + entry
        findings = self.direct(text)
        self.assertEqual({(f['stage'], f['category']) for f in findings}, {('sanction', 'sanctions')})
        self.assertTrue(all('Se impuso 01' in f['quote'] for f in findings))
        for altered in (
            header.replace('Se impuso', 'Se propuso') + entry,
            header + 'La feria abrió sus puertas.\n\n' + entry,
            header + entry.replace('Al presidente', 'Al hermano del presidente'),
            header + entry.replace('Ana Nopal Villaluz.', 'Otra Persona Distinta, según informó Ana Nopal Villaluz.'),
        ):
            self.assertEqual(self.direct(altered), [], altered)

    def test_last_reference_is_adjacent_unique_and_administrative(self):
        subject = 'Mientras que Ana Nopal Villaluz, alcaldesa de Villa Nopal, ganó la reelección por el partido local.'
        event = 'Éste último fue multado por incumplir una resolución de transparencia y una solicitud de información.'
        findings = self.direct(subject + '\n\n' + event)
        self.assertEqual({(f['stage'], f['category']) for f in findings}, {('sanction', 'sanctions')})
        for altered in (
            subject + '\n\nLa plaza fue inaugurada.\n\n' + event,
            subject.replace('Ana Nopal Villaluz,', 'Ana Nopal Villaluz y Otro Nombre Distinto,') + '\n\n' + event,
            subject.replace('Ana Nopal Villaluz,', 'Otro Nombre Distinto y Ana Nopal Villaluz,') + '\n\n' + event,
            subject + '\n\n' + event.replace('fue multado', 'no fue multado'),
            subject + '\n\nÉste último fue detenido por un delito.',
            'Ana Nopal Villaluz, alcaldesa de Villa Nopal, se reunió con Juan Pérez.\n\n' + event,
            'Ana Nopal Villaluz, alcaldesa de Villa Nopal, acudió con el síndico.\n\n' + event,
        ):
            self.assertEqual(self.direct(altered), [], altered)

    def test_identified_administrative_order_never_means_executed_criminal_arrest(self):
        header = 'El instituto de transparencia impuso tres arrestos administrativos por doce horas cada uno.'
        recipient = 'Uno de ellos fue para la presidenta de Villa Nopal, Ana Nopal Villaluz, por incumplir obligaciones.'
        findings = self.direct(header + '\n\n' + recipient)
        self.assertEqual({(f['stage'], f['category']) for f in findings}, {('sanction', 'sanctions')})
        self.assertTrue(all(f['detention_executed'] is False for f in findings))
        for altered in (
            header.replace('impuso', 'solicitó') + '\n\n' + recipient,
            header.replace('impuso', 'no impuso') + '\n\n' + recipient,
            header + '\n\n' + recipient.replace('la presidenta', 'la hermana de la presidenta'),
            header + '\n\n' + recipient.replace('Ana Nopal Villaluz', 'Otra Persona Distinta') + ' Según Ana Nopal Villaluz.',
        ):
            self.assertEqual(self.direct(altered), [], altered)

    def test_instituted_procedure_title_distinguishes_respondent_from_complainant_and_counsel(self):
        title = ('Resolución del Consejo General del instituto, respecto del procedimiento '
                 'administrativo sancionador de queja en materia de fiscalización, instaurado en contra '
                 'del Partido Vecinal y su otrora candidata a la Presidencia Municipal de Villa Nopal, '
                 'Ana Nopal Villaluz, en el proceso electoral local, identificado como EXP/42.')
        findings = self.direct(title)
        self.assertEqual({(f['stage'], f['category']) for f in findings}, {('investigation', 'sanctions')})
        self.assertTrue(all(f['underlying_resolution_read'] is False for f in findings))
        self.assertTrue(all(f['procedural_outcome'] == 'not_inferred' for f in findings))
        for altered in (
            title.replace('instaurado en contra', 'no instaurado en contra'),
            title.replace('Ana Nopal Villaluz,', 'Otra Persona Distinta,') + ' Ana Nopal Villaluz presentó la queja.',
            title.replace('su otrora candidata', 'la abogada de su otrora candidata'),
            title.replace('instaurado en contra', 'presentado por Ana Nopal Villaluz contra'),
        ):
            self.assertEqual(self.direct(altered), [], altered)

    def test_recall_petition_is_governance_controversy_without_a_criminal_or_removal_claim(self):
        text = ('A resident filed a petition last week to recall Mayor Ana Nopal Villaluz.\n\n'
                'The gesture is symbolic because no legal framework allows the recall.')
        findings = self.direct(text)
        self.assertEqual({(f['stage'], f['category']) for f in findings}, {('allegation', 'reputational')})
        self.assertTrue(all(f['criminal_conduct_reported'] is False for f in findings))
        self.assertTrue(all(f['removal_from_office_established'] is False for f in findings))
        for altered in (
            text.replace('filed', 'did not file'),
            text.replace('filed', 'may file'),
            'Ana Nopal Villaluz filed a petition to recall Mayor Other Invented Person.',
            'Ana Nopal Villaluz signed the petition filed by a resident against Mayor Other Invented Person.',
            "A resident filed a petition to recall Ana Nopal Villaluz's husband, the mayor.",
        ):
            self.assertEqual(self.direct(altered), [], altered)

    def test_same_arrested_officeholder_can_supply_offence_only_through_explicit_continuation(self):
        subject = 'Ana Nopal Villaluz fue detenida con tres servidores públicos más de Villa Nopal.'
        followup = 'A la exedil se le acusa de encabezar una red de corrupción en el ayuntamiento.'
        findings = self.direct(subject + '\n\n' + followup)
        self.assertEqual({(f['stage'], f['category']) for f in findings},
                         {('arrest', 'corruption'), ('allegation', 'corruption')})
        for altered in (
            subject + '\n\nOtro exedil fue acusado de corrupción.',
            subject.replace('con tres servidores públicos', 'con Otro Nombre Distinto, alcalde de Villa Azul, y servidores públicos') + '\n\n' + followup,
            subject + '\n\n' + followup.replace('se le acusa', 'no se le acusa'),
            subject + '\n\nLa plaza está abierta.\n\n' + followup,
            'Ana Nopal Villaluz fue detenida junto con el exedil de Villa Azul.\n\n' + followup,
            'Ana Nopal Villaluz fue detenida con Juan Pérez, exedil de Villa Azul.\n\n' + followup,
        ):
            self.assertFalse(any(f['category'] == 'corruption' for f in self.direct(altered)), altered)
        for suffix in ('fraude, no de corrupción.',
                       'fraude; el CJNG fue mencionado por la víctima.',
                       'fraude y Juan Pérez está ligado al CJNG.'):
            altered=subject+'\n\nA la exedil se le acusa de '+suffix
            self.assertEqual({f['category'] for f in self.direct(altered)}, {'fraud'})

    def test_crime_acronym_requires_a_local_adverse_role_and_never_a_victim_or_authority(self):
        text = 'Ana Nopal Villaluz es señalada de tener vínculos con el CJNG.'
        self.assertEqual({(f['stage'], f['category']) for f in self.direct(text)},
                         {('allegation', 'organized_crime')})
        for altered in (
            'Ana Nopal Villaluz fue víctima de amenazas del CJNG.',
            'Ana Nopal Villaluz denunció las amenazas del CJNG.',
            'La jueza Ana Nopal Villaluz resolvió que Otro Nombre Distinto fue acusado de vínculos con el CJNG.',
            'Otro Nombre Distinto fue acusado de secuestro de Ana Nopal Villaluz.',
            'En las acusaciones aparecen los nombres de los notarios públicos Otro Nombre Distinto (5), representado por su abogada Ana Nopal Villaluz.',
            'En las acusaciones aparecen los nombres de los notarios públicos Otro Nombre Distinto (5), según atestiguó Ana Nopal Villaluz.',
        ):
            self.assertEqual(self.direct(altered), [], altered)

    def test_later_acquittal_date_never_dates_the_older_investigation(self):
        for link in ('y', ', posteriormente', 'antes de ser'):
            text = 'Ana Nopal Villaluz fue investigada en 2020 '+link+' absuelta el 13 de marzo de 2025.'
            findings = analyze(text, ENTITY)['findings']
            self.assertFalse(any(f.get('event_date') == '2025-03-13' for f in findings if f['stage'] == 'investigation'))
            self.assertTrue(any(f.get('event_date') == '2025-03-13' for f in findings if f['stage'] == 'acquittal'))

    def test_documentary_metadata_and_resolution_remain_literal_without_personal_contribution(self):
        text = ('El instituto impuso una amonestación pública para la presidenta de Villa Nopal, Ana Nopal Villaluz. '
                'Después Ana Nopal Villaluz fue absuelta en el mismo caso.')
        row = {'id':'invented-evidence','document_id':'invented-document','source_kind':'official',
               'source_url':'https://fixture.invalid/administrative','text':text,
               'content_hash':hashlib.sha256(text.encode()).hexdigest(),'analysis':analyze(text, ENTITY)}
        result = document_review(ENTITY,[row])
        self.assertTrue(result['items'])
        for lead in result['items']:
            self.assertFalse(lead['adverse_alert'])
            self.assertIsNone(lead['personal_score_contribution'])
            self.assertEqual(text[lead['start']:lead['end']],lead['quote'])

    def test_same_procedure_disposition_suppresses_alert_without_borrowing_another_case(self):
        title = ('Resolución del instituto sobre el procedimiento administrativo sancionador de queja '
                 'en materia de fiscalización, instaurado en contra de la candidata a la Presidencia '
                 'Municipal de Villa Nopal, Ana Nopal Villaluz, expediente FICTICIO/42.')
        for followup,expected in (
            ('Este procedimiento fue archivado por falta de pruebas.',False),
            ('Otro procedimiento fue archivado por falta de pruebas.',True),
            ('El instituto abrió otro procedimiento contra Otro Nombre Distinto. '
             'Este procedimiento fue archivado por falta de pruebas.',True),
        ):
            text=title+'\n\n'+followup
            row={'id':'invented-procedure','source_kind':'official','source_url':'https://fixture.invalid/procedure',
                 'text':text,'content_hash':hashlib.sha256(text.encode()).hexdigest(),'analysis':analyze(text,ENTITY)}
            leads=document_review(ENTITY,[row])['items']
            self.assertTrue(leads)
            self.assertTrue(all(lead['adverse_alert'] is expected for lead in leads))
            self.assertTrue(all(lead['underlying_resolution_read'] is False for lead in leads))
            self.assertTrue(all(lead['procedural_outcome']=='not_inferred' for lead in leads))

    def test_political_card_keeps_noncriminal_semantics_and_unicode_offsets(self):
        text='📌 A resident filed a petition to recall Mayor ANA NOPAL VILLALUZ.'
        row={'id':'invented-recall','source_kind':'news','source_url':'https://fixture.invalid/recall',
             'text':text,'content_hash':hashlib.sha256(text.encode()).hexdigest(),'analysis':analyze(text,ENTITY)}
        leads=document_review(ENTITY,[row])['items']
        self.assertEqual(len(leads),1)
        lead=leads[0]
        self.assertEqual(text[lead['start']:lead['end']],lead['quote'])
        self.assertEqual(text[lead['trigger_start']:lead['trigger_end']],'filed a petition to recall Mayor ')
        self.assertFalse(lead['criminal_conduct_reported'])
        self.assertFalse(lead['removal_from_office_established'])
        self.assertIsNone(lead['personal_score_contribution'])


if __name__ == '__main__':
    unittest.main()
