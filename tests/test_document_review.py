"""Documentary leads cannot confirm identities or inflate personal scores."""
import copy
import hashlib
import tempfile
import unittest

from peps.document_review import document_review
from peps.nlp import analyze
from peps.engine import Engine

ENTITY = {'id': 'fictional-notary', 'name': 'Aurora Sintética del Valle',
          'kind': 'person', 'aliases': [], 'context': {}}


def evidence(text=None, **extra):
    text = text or 'Aurora Sintética del Valle fue acusada de fraude.'
    item = {'id': 'fictional-evidence', 'document_id': 'fictional-document',
            'title': 'Caso enteramente ficticio', 'source_url': 'https://fixture.invalid/article',
            'source_kind': 'news', 'text': text, 'published_at': '2026-01-15',
            'content_hash': hashlib.sha256(text.encode()).hexdigest(), 'metadata': {},
            'review': {'status': 'pending'}, 'identity_resolution': {'status': 'candidate'}}
    item['analysis'] = analyze(text, ENTITY, 'news')
    item.update(extra)
    return item


class DocumentReviewTests(unittest.TestCase):
    def test_historical_bundle_uses_frozen_inputs_after_current_review_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            engine.store.import_entities([ENTITY])
            engine.import_documents([{'url': 'https://fixture.invalid/original',
                'title': 'Documento ficticio', 'source_kind': 'news', 'entity_ids': [ENTITY['id']],
                'text': 'Aurora Sintética del Valle fue acusada de fraude.', 'published_at': '2026-01-15'}])
            engine.rescore()
            original = engine.store.score_run_bundle(ENTITY['id'], include_evidence=True)
            run_id = original['run']['id']
            first = document_review(original['run']['entity_snapshot'], original['evidence'])
            self.assertGreater(first['total_leads'], 0)
            with engine.store.connection() as con:
                con.execute("UPDATE evidence SET review=?", ('{"status":"rejected"}',))
                engine.store._audit(con, 'fictional_review_changed', {'test_only': True})
            engine.rescore()
            current = engine.store.score_run_bundle(ENTITY['id'], include_evidence=True)
            self.assertEqual(document_review(current['run']['entity_snapshot'], current['evidence'])['total_leads'], 0)
            historical = engine.store.score_run_bundle(ENTITY['id'], run_id=run_id, include_evidence=True)
            self.assertEqual(first, document_review(historical['run']['entity_snapshot'], historical['evidence']))
            self.assertEqual(len(historical['history']), 2)

    def test_candidate_lead_has_literal_quote_without_personal_score_or_confirmation(self):
        row = evidence()
        before = copy.deepcopy(row)
        result = document_review(ENTITY, [row])
        self.assertGreater(result['total_leads'], 0)
        for lead in result['items']:
            self.assertEqual(row['text'][lead['start']:lead['end']], lead['quote'])
            self.assertEqual(lead['identity_status'], 'candidate')
            self.assertIsNone(lead['personal_score_contribution'])
        self.assertEqual(result['identity_confirmations_created'], 0)
        self.assertEqual(row, before)

    def test_victim_negation_and_other_subject_do_not_become_alerts(self):
        for text in ('Aurora Sintética del Valle denunció que otra persona cometió fraude.',
                     'Aurora Sintética del Valle no fue acusada de fraude.',
                     'Aurora Sintética del Valle fue víctima de fraude.'):
            with self.subTest(text=text):
                self.assertEqual(document_review(ENTITY, [evidence(text)])['total_leads'], 0)

    def test_reported_acquittal_keeps_both_quotes_without_an_adverse_alert(self):
        text = ('Aurora Sintética del Valle fue acusada de fraude. '
                'Posteriormente Aurora Sintética del Valle fue absuelta de fraude.')
        result = document_review(ENTITY, [evidence(text)])
        self.assertEqual(result['total_leads'], 1)
        lead = result['items'][0]
        self.assertFalse(lead['adverse_alert'])
        self.assertEqual(lead['chronology_state'], 'mixed_requires_review')
        self.assertIsNone(lead['personal_score_contribution'])
        self.assertEqual(len(lead['resolution_context']), 1)
        resolution = lead['resolution_context'][0]
        self.assertEqual(text[resolution['start']:resolution['end']], resolution['quote'])
        self.assertEqual(resolution['stage'], 'acquittal')

    def test_exact_copy_does_not_multiply_leads_or_documents(self):
        row = evidence('📌 Aurora Sintética del Valle fue acusada de fraude.')
        result = document_review(ENTITY, [row, dict(row, id='copy', document_id='copy')])
        self.assertEqual(result['total_leads'], document_review(ENTITY, [row])['total_leads'])
        self.assertEqual(result['documents_with_leads'], 1)

    def test_rejected_and_discovery_documents_are_not_alerts(self):
        row = evidence()
        for overrides in ({'review': {'status': 'rejected'}},
                          {'identity_resolution': {'status': 'rejected'}},
                          {'source_kind': 'discovery'}, {'metadata': {'discovery_only': True}}):
            self.assertEqual(document_review(ENTITY, [dict(row, **overrides)])['total_leads'], 0)

    def test_modified_cached_decisions_and_original_bytes_are_rejected(self):
        row = evidence()
        forged = copy.deepcopy(row)
        forged['analysis']['findings'][0]['stage'] = 'conviction'
        result = document_review(ENTITY, [forged])
        self.assertEqual(result['total_leads'], 0)
        self.assertEqual(result['invalid_inputs'], 1)
        result = document_review(ENTITY, [dict(row, text=row['text'] + ' bytes modificados')])
        self.assertEqual(result['total_leads'], 0)
        self.assertEqual(result['invalid_inputs'], 1)

    def test_old_analysis_requires_reanalysis_without_changing_historical_inputs(self):
        row = evidence()
        row['analysis']['model_version'] = 'old-model'
        result = document_review(ENTITY, [row])
        self.assertEqual(result['total_leads'], 0)
        self.assertEqual(result['reanalyze_required'], 1)
        self.assertEqual(row['analysis']['model_version'], 'old-model')


if __name__ == '__main__':
    unittest.main()
