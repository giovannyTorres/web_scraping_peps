"""Backtesting denominators and leakage regressions; synthetic identities only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts.notary_backtest_metrics import (
    evaluate_backtest, gold_snapshot_hash, main, wilson_interval,
)


def minimal_payload():
    """A positive stratum and an unknown control are not binary ground truth."""
    payload = {
        'schema_version': 1, 'cutoff': '2025-12-31',
        'gold_snapshot': {'frozen_at': '2026-01-01T00:00:00Z'},
        'run': {'started_at': '2026-01-02T00:00:00Z',
                'completed_at': '2026-01-02T00:01:00Z',
                'source_limitations': ['Fixture publisher is a finite source.']},
        'cases': [
            {'entity_id': 'positive', 'cohort': 'known_incident',
             'incident_label': 'yes', 'label_adjudicated': True},
            {'entity_id': 'control', 'cohort': 'no_known_incident',
             'incident_label': 'unknown', 'label_adjudicated': False}],
        'reference_articles': [
            {'entity_id': 'positive', 'url': 'https://example.test/hit',
             'published_at': '2020-01-01', 'relevance': 'yes', 'incident': 'yes',
             'retrieval_eligible': True},
            {'entity_id': 'positive', 'url': 'https://example.test/miss',
             'published_at': '2020-02-01', 'relevance': 'yes', 'incident': 'yes',
             'retrieval_eligible': False},
            {'entity_id': 'control', 'url': 'https://example.test/control',
             'published_at': '2020-03-01', 'relevance': 'yes', 'incident': 'no',
             'retrieval_eligible': True}],
        'retrieved': [
            {'entity_id': 'positive', 'url': 'https://example.test/hit',
             'published_at': '2020-01-01', 'has_text': True,
             'relevance': 'yes', 'incident': 'yes', 'reviewed_independently': True},
            {'entity_id': 'control', 'url': 'https://example.test/control',
             'published_at': '2020-03-01', 'has_text': True,
             'relevance': 'yes', 'incident': 'no', 'reviewed_independently': True}]}
    return seal(payload)


def seal(payload):
    payload['gold_snapshot']['sha256'] = gold_snapshot_hash(payload)
    return payload


def result(entity_id, url, relevance='unknown', incident='unknown', **extra):
    return {'entity_id': entity_id, 'url': url, 'published_at': '2020-01-01',
            'has_text': True, 'relevance': relevance, 'incident': incident,
            'reviewed_independently': relevance != 'unknown', **extra}


class NotaryBacktestMetricsTests(unittest.TestCase):
    def test_positive_unknown_controls_never_inflate_tn_or_accuracy(self):
        payload = minimal_payload()
        payload['entity_predictions'] = [
            {'entity_id': 'positive', 'prediction': 'yes'},
            {'entity_id': 'control', 'prediction': 'no'}]
        report = evaluate_backtest(payload)
        entity = report['entity_classification']
        self.assertEqual(entity['confusion_matrix'], {'tp': 1, 'fp': 0, 'fn': 0, 'tn': 0})
        self.assertEqual(entity['unknown_truth_support'], 1)
        self.assertIsNone(entity['accuracy']['value'])
        self.assertIsNone(entity['balanced_accuracy'])
        self.assertEqual(entity['known_positive_recall']['value'], 1)
        self.assertEqual(report['known_positive_incident_document_detection']['numerator'], 1)
        self.assertIn('finite source', report['source_limitations'][0])

    def test_reference_recall_preserves_inaccessible_misses(self):
        report = evaluate_backtest(minimal_payload())
        stage = report['texts']
        self.assertEqual(stage['reference_recall_all']['numerator'], 2)
        self.assertEqual(stage['reference_recall_all']['denominator'], 3)
        self.assertEqual(stage['reference_recall_known_eligible']['value'], 1)
        self.assertEqual(stage['reference_eligibility'],
                         {'known_eligible': 2, 'known_ineligible': 1, 'unknown': 0})
        self.assertEqual(stage['reference_entity_coverage']['denominator'], 2)
        self.assertEqual(report['cohorts']['known_incident']['texts']['reference_recall_all']['value'], .5)

    def test_unique_url_recall_does_not_imply_attribution_for_three_people(self):
        payload = minimal_payload()
        payload['cases'].append({'entity_id': 'third', 'cohort': 'no_known_incident',
                                  'incident_label': 'unknown', 'label_adjudicated': False})
        shared = payload['reference_articles'][0]
        payload['reference_articles'] = [dict(shared, entity_id=entity_id,
            retrieval_eligible=entity_id == 'third')
            for entity_id in ('positive', 'control', 'third')]
        payload['reference_articles'].append(dict(shared,
            url='https://EXAMPLE.test/hit?utm_source=duplicate#section',
            retrieval_eligible=False))
        payload['retrieved'] = [dict(payload['retrieved'][0],
            url='https://EXAMPLE.test/hit?fbclid=discarded#fragment')]
        stage = evaluate_backtest(seal(payload))['texts']
        self.assertEqual(stage['reference_recall_all']['denominator'], 3)
        self.assertEqual(stage['reference_recall_all']['value'], 1 / 3)
        self.assertEqual(stage['reference_unique_url_recall_all']['denominator'], 1)
        self.assertEqual(stage['reference_unique_url_recall_all']['value'], 1)
        self.assertEqual(stage['reference_recall_known_eligible']['value'], 0)
        self.assertEqual(stage['reference_unique_url_recall_known_eligible']['value'], 1)
        self.assertIn('any entity', stage['reference_unique_url_recall_scope'])

    def test_macro_entity_recall_excludes_no_reference_support_as_unknown(self):
        payload = minimal_payload()
        payload['cases'].append({'entity_id': 'third', 'cohort': 'no_known_incident',
                                  'incident_label': 'unknown', 'label_adjudicated': False})
        stage = evaluate_backtest(seal(payload))['texts']
        macro = stage['reference_macro_entity_recall_all']
        self.assertEqual(macro['value'], .75)
        self.assertEqual(macro['entities_with_references'], 2)
        self.assertEqual(macro['entities_without_references'], 1)
        third = next(row for row in stage['reference_per_entity_recall_all'] if row['entity_id'] == 'third')
        self.assertIsNone(third['value'])
        self.assertEqual(third['denominator'], 0)

    def test_unknown_source_eligibility_is_separate_not_an_implicit_exclusion(self):
        payload = minimal_payload()
        payload['reference_articles'][1]['retrieval_eligible'] = None
        report = evaluate_backtest(seal(payload))
        self.assertEqual(report['texts']['reference_recall_all']['denominator'], 3)
        self.assertEqual(report['texts']['reference_eligibility']['unknown'], 1)

    def test_canonical_duplicate_pairs_and_shared_url_have_different_volumes(self):
        payload = minimal_payload()
        duplicate = dict(payload['retrieved'][0],
                         url='https://EXAMPLE.test/hit?utm_source=a#fragment')
        payload['retrieved'].append(duplicate)
        payload['retrieved'].append(result('control', 'https://example.test/hit?fbclid=x', 'no'))
        stage = evaluate_backtest(payload)
        self.assertEqual(stage['volume']['duplicate_pair_rows_removed'], 1)
        self.assertEqual(stage['texts']['unique_entity_article_pairs'], 3)
        self.assertEqual(stage['texts']['unique_article_urls'], 2)
        self.assertAlmostEqual(stage['texts']['precision_among_reviewed']['value'], 2 / 3)

    def test_semantic_query_parameters_remain_distinct_articles(self):
        payload = minimal_payload()
        payload['retrieved'] += [result('positive', 'https://example.test/story?id=1'),
                                 result('positive', 'https://example.test/story?id=2')]
        self.assertEqual(evaluate_backtest(payload)['candidates']['unique_article_urls'], 4)

    def test_content_hashes_separate_shared_body_from_distinct_urls(self):
        payload = minimal_payload()
        payload['retrieved'][0]['body_sha256'] = 'a' * 64
        payload['retrieved'][1]['body_sha256'] = 'a' * 64
        report = evaluate_backtest(payload)
        self.assertEqual(report['texts']['unique_article_urls'], 2)
        self.assertEqual(report['texts']['unique_original_body_hashes'], 1)
        self.assertEqual(report['texts']['text_pairs_without_body_hash'], 0)

    def test_metadata_candidate_does_not_count_as_text_recall(self):
        payload = minimal_payload()
        payload['retrieved'][0]['has_text'] = False
        report = evaluate_backtest(payload)
        self.assertEqual(report['candidates']['reference_recall_all']['numerator'], 2)
        self.assertEqual(report['texts']['reference_recall_all']['numerator'], 1)
        self.assertEqual(report['texts']['entities_with_any_result']['value'], .5)
        self.assertEqual(report['known_positive_incident_document_detection']['value'], 0)

    def test_missing_reviews_have_precision_bounds_and_never_become_relevant(self):
        payload = minimal_payload()
        payload['retrieved'].append(result('positive', 'https://example.test/unreviewed',
                                            'yes', 'yes', reviewed_independently=False))
        report = evaluate_backtest(payload)
        precision = report['texts']['precision_among_reviewed']
        bounds = report['texts']['precision_all_results_bounds']
        self.assertEqual(precision['value'], 1)
        self.assertEqual(precision['denominator'], 2)
        self.assertEqual(bounds, {'lower': 2 / 3, 'upper': 1, 'denominator': 3, 'unjudged': 1})
        self.assertEqual(report['volume']['nonindependent_labels_ignored'], 1)
        self.assertEqual(report['texts']['judgment_coverage']['value'], 2 / 3)

    def test_false_positives_reduce_precision_and_bound(self):
        payload = minimal_payload()
        payload['retrieved'].append(result('positive', 'https://example.test/irrelevant', 'no'))
        report = evaluate_backtest(payload)
        self.assertEqual(report['texts']['judgments']['irrelevant'], 1)
        self.assertAlmostEqual(report['texts']['precision_among_reviewed']['value'], 2 / 3)
        self.assertAlmostEqual(report['texts']['f1_reviewed_precision_and_frozen_reference_recall'], 2 / 3)

    def test_zero_denominators_are_unknown_not_perfect(self):
        payload = minimal_payload()
        payload['cases'] = []
        payload['reference_articles'] = []
        payload['retrieved'] = []
        report = evaluate_backtest(seal(payload))
        for key in ('precision_among_reviewed', 'reference_recall_all', 'entities_with_any_result',
                    'reference_unique_url_recall_all', 'reference_unique_url_recall_known_eligible'):
            self.assertIsNone(report['texts'][key]['value'])
            self.assertEqual(report['texts'][key]['denominator'], 0)
            self.assertIsNone(report['texts'][key]['wilson95'])
        self.assertIsNone(report['entity_classification']['accuracy']['value'])
        self.assertIsNone(report['texts']['precision_all_results_bounds']['upper'])
        self.assertIsNone(report['texts']['reference_macro_entity_recall_all']['value'])

    def test_missing_predictions_are_abstentions_and_penalize_unconditional_accuracy(self):
        payload = minimal_payload()
        payload['document_predictions'] = [
            {'entity_id': 'positive', 'url': 'https://example.test/hit',
             'variant': 'fixture-model', 'prediction': 'yes'}]
        model = evaluate_backtest(payload)['document_classification']['fixture-model']
        self.assertEqual(model['abstentions']['negative'], 1)
        self.assertEqual(model['accuracy']['value'], .5)
        self.assertEqual(model['answered_adjudicated_subset_accuracy']['value'], 1)
        self.assertEqual(model['decision_coverage']['value'], .5)
        self.assertEqual(model['balanced_accuracy'], .5)
        self.assertEqual(model['missing_predictions_counted_as_abstentions'], 1)

    def test_document_models_measure_incident_classification_separately_from_retrieval(self):
        payload = minimal_payload()
        payload['document_predictions'] = [
            {'entity_id': row['entity_id'], 'url': row['url'], 'variant': variant,
             'prediction': 'yes' if variant == 'all-positive' else row['incident']}
            for row in payload['retrieved'] for variant in ('all-positive', 'review-fixture')]
        report = evaluate_backtest(payload)
        bad = report['document_classification']['all-positive']
        good = report['document_classification']['review-fixture']
        self.assertEqual(bad['confusion_matrix'], {'tp': 1, 'fp': 1, 'fn': 0, 'tn': 0})
        self.assertEqual(bad['accuracy']['value'], .5)
        self.assertEqual(bad['balanced_accuracy'], .5)
        self.assertAlmostEqual(bad['known_label_f1'], 2 / 3)
        self.assertEqual(good['accuracy']['value'], 1)
        self.assertEqual(good['balanced_accuracy'], 1)
        self.assertEqual(report['texts']['precision_among_reviewed']['value'], 1)
        self.assertIsNone(report['entity_classification']['accuracy']['value'])

    def test_document_predictions_cannot_use_unfetched_gold_articles(self):
        payload = minimal_payload()
        payload['document_predictions'] = [{'entity_id': 'positive',
                                             'url': 'https://example.test/miss', 'prediction': 'yes'}]
        with self.assertRaisesRegex(ValueError, 'requires a retrieved original text'):
            evaluate_backtest(payload)

    def test_unreviewed_or_irrelevant_text_is_not_a_negative_model_example(self):
        payload = minimal_payload()
        payload['retrieved'] += [result('positive', 'https://example.test/homonym', 'no'),
                                 result('positive', 'https://example.test/unreviewed')]
        payload['document_predictions'] = [
            {'entity_id': row['entity_id'], 'url': row['url'], 'prediction': 'no'}
            for row in payload['retrieved']]
        model = evaluate_backtest(payload)['document_classification']['default']
        self.assertEqual(model['audited_relevant_text_pairs'], 2)
        self.assertEqual(model['predictions_excluded_irrelevant_or_unreviewed'], 2)
        self.assertEqual(model['confusion_matrix']['tn'], 1)
        self.assertEqual(model['confusion_matrix']['fn'], 1)
        self.assertEqual(model['known_label_f1'], 0)

    def test_independently_unknown_incident_prevents_cohort_accuracy(self):
        payload = minimal_payload()
        payload['retrieved'].append(result('positive', 'https://example.test/undecided', 'yes'))
        payload['document_predictions'] = [
            {'entity_id': row['entity_id'], 'url': row['url'], 'prediction': 'no'}
            for row in payload['retrieved']]
        model = evaluate_backtest(payload)['document_classification']['default']
        self.assertEqual(model['unknown_truth_support'], 1)
        self.assertIsNone(model['accuracy']['value'])
        self.assertEqual(model['adjudicated_subset_accuracy']['value'], .5)

    def test_gold_hash_is_stable_to_order_but_detects_label_or_provenance_changes(self):
        payload = minimal_payload()
        expected = gold_snapshot_hash(payload)
        payload['cases'].reverse()
        payload['reference_articles'].reverse()
        self.assertEqual(gold_snapshot_hash(payload), expected)
        payload['retrieved'] = []
        payload['run']['started_at'] = '2026-01-03T00:00:00Z'
        self.assertEqual(gold_snapshot_hash(payload), expected)
        payload['reference_articles'][0]['provenance'] = 'changed after freezing'
        with self.assertRaisesRegex(ValueError, 'does not match'):
            evaluate_backtest(payload)

    def test_freeze_after_run_is_rejected_not_merely_flagged(self):
        payload = minimal_payload()
        payload['gold_snapshot']['frozen_at'] = '2026-01-03T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'leakage'):
            evaluate_backtest(payload)

    def test_post_cutoff_entity_labels_and_mismatched_model_targets_are_rejected(self):
        payload = minimal_payload()
        payload['cases'][0]['label_as_of'] = '2026-01-01'
        with self.assertRaisesRegex(ValueError, 'label_as_of is after cutoff'):
            evaluate_backtest(seal(payload))
        payload = minimal_payload()
        payload['document_predictions'] = [{'entity_id': 'positive',
            'url': 'https://example.test/hit', 'prediction': 'yes', 'target': 'current_guilt'}]
        with self.assertRaisesRegex(ValueError, 'target differs'):
            evaluate_backtest(payload)

    def test_missing_run_time_leaves_leakage_guard_explicitly_unverified(self):
        payload = minimal_payload()
        del payload['run']['started_at']
        self.assertFalse(evaluate_backtest(payload)['gold_snapshot']['freeze_precedes_run_verified'])

    def test_future_reference_is_rejected_and_future_retrieved_is_excluded(self):
        payload = minimal_payload()
        payload['reference_articles'][0]['published_at'] = '2026-01-01'
        with self.assertRaisesRegex(ValueError, 'after cutoff'):
            evaluate_backtest(seal(payload))
        payload = minimal_payload()
        payload['retrieved'].append(result('positive', 'https://example.test/future',
                                            published_at='2026-01-01'))
        report = evaluate_backtest(payload)
        self.assertEqual(report['volume']['excluded_post_cutoff_rows'], 1)
        self.assertEqual(report['texts']['unique_entity_article_pairs'], 2)
        self.assertEqual(report['excluded_rows'][0]['reason'], 'published_after_cutoff')

    def test_unknown_publication_dates_are_explicit(self):
        payload = minimal_payload()
        payload['retrieved'][0]['published_at'] = None
        report = evaluate_backtest(payload)
        self.assertEqual(report['missing_publication_dates']['retrieved_pairs'], 1)
        self.assertEqual(report['texts']['reference_recall_all']['numerator'], 2)

    def test_conflicting_duplicate_reviews_and_gold_disagreement_require_adjudication(self):
        payload = minimal_payload()
        payload['retrieved'].append(dict(payload['retrieved'][0], relevance='no'))
        with self.assertRaisesRegex(ValueError, 'conflicting duplicate relevance'):
            evaluate_backtest(payload)
        payload = minimal_payload()
        payload['retrieved'][0]['incident'] = 'no'
        with self.assertRaisesRegex(ValueError, 'disagrees with frozen reference incident'):
            evaluate_backtest(payload)

    def test_input_validation_rejects_guessed_or_non_adjudicated_entity_labels(self):
        payload = minimal_payload()
        payload['cases'][1]['incident_label'] = 'no'
        with self.assertRaisesRegex(ValueError, 'explicit adjudication'):
            evaluate_backtest(seal(payload))
        payload = minimal_payload()
        payload['retrieved'][0]['entity_id'] = 'invented-name-match'
        with self.assertRaisesRegex(ValueError, 'not in cases'):
            evaluate_backtest(payload)

    def test_explicit_verified_negative_supports_binary_entity_metrics(self):
        payload = minimal_payload()
        payload['cases'][1].update(incident_label='no', label_adjudicated=True,
                                    scope='Closed reviewed fixture corpus only')
        payload['entity_predictions'] = [{'entity_id': 'positive', 'prediction': 'yes'},
                                          {'entity_id': 'control', 'prediction': 'no'}]
        entity = evaluate_backtest(seal(payload))['entity_classification']
        self.assertEqual(entity['accuracy']['value'], 1)
        self.assertEqual(entity['balanced_accuracy'], 1)
        self.assertEqual(entity['confusion_matrix']['tn'], 1)

    def test_wilson_intervals_bound_small_samples_and_empty_support(self):
        interval = wilson_interval(1, 1)
        self.assertAlmostEqual(interval['lower'], .20654931437723745)
        self.assertAlmostEqual(interval['upper'], 1)
        self.assertIsNone(wilson_interval(0, 0))
        with self.assertRaises(ValueError):
            wilson_interval(2, 1)
        with self.assertRaises(ValueError):
            wilson_interval(True, 1)

    def test_cli_writes_portable_json_without_mutating_input(self):
        payload = minimal_payload()
        preserved = deepcopy(payload)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            output = Path(directory) / 'report.json'
            source.write_text(json.dumps(payload), encoding='utf-8')
            self.assertEqual(main([str(source), '--output', str(output)]), 0)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['gold_snapshot']['sha256'], payload['gold_snapshot']['sha256'])
            self.assertEqual(payload, preserved)


if __name__ == '__main__':
    unittest.main()
