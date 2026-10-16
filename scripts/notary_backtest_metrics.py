"""Offline, finite-reference retrieval evaluation; never infer a person's risk.

``evaluate_backtest`` accepts a frozen gold cohort and separately reviewed
entity/article pairs. Labels describe reported documents/incidents, not guilt.
The reference recall is bounded by that supplied snapshot, not all of the web.
Unjudged results never count as relevant and never disappear from the bounds.
See ``minimal_payload`` in the unit tests for a complete schema-v1 example.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from peps.news import canonical_url

LABELS = frozenset(('yes', 'no', 'unknown'))
PREDICTIONS = frozenset(('yes', 'no', 'abstain'))


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(name + ' must be an object')
    return value


def _rows(value, name):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(name + ' must be an array of objects')
    return value


def _label(value, name):
    if not isinstance(value, str) or value not in LABELS:
        raise ValueError(name + ' must be yes, no or unknown')
    return value


def _day(value, name, optional=False):
    if value in (None, '') and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(name + ' must be an ISO date or timestamp')
    try:
        # ISO dates are common in publisher metadata; ISO timestamps are accepted
        # only after validating the whole timestamp, never by slicing bad text.
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date()
    except ValueError as exc:
        raise ValueError(name + ' must be an ISO date or timestamp') from exc


def _instant(value, name):
    if not isinstance(value, str):
        raise ValueError(name + ' must be a timezone-aware ISO timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError(name + ' must be a timezone-aware ISO timestamp') from exc
    if result.tzinfo is None:
        raise ValueError(name + ' must include a timezone')
    return result.astimezone(timezone.utc)


def gold_snapshot_hash(payload: dict[str, Any]) -> str:
    """Hash the supplied gold fields, excluding predictions and acquired results.

    Array order is ignored; every supplied gold field (including provenance and
    date labels) participates. Freeze this before starting acquisition. This is
    an integrity receipt, not proof that references were chosen independently.
    """
    _object(payload, 'payload')
    cases = _rows(payload.get('cases'), 'cases')
    references = _rows(payload.get('reference_articles'), 'reference_articles')
    ordered = lambda rows: sorted(rows, key=lambda row: json.dumps(
        row, sort_keys=True, ensure_ascii=False, separators=(',', ':')))
    material = {'schema_version': payload.get('schema_version'),
                'cutoff': payload.get('cutoff'),
                'cases': ordered(cases), 'reference_articles': ordered(references)}
    data = json.dumps(material, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054):
    """Wilson 95% interval; an empty denominator is unknown, never 100%."""
    if isinstance(total, bool) or isinstance(successes, bool) or not isinstance(total, int) or not isinstance(successes, int):
        raise ValueError('Wilson counts must be integers')
    if total < 0 or successes < 0 or successes > total:
        raise ValueError('Wilson counts must satisfy 0 <= successes <= total')
    if not math.isfinite(z) or z <= 0:
        raise ValueError('Wilson z must be finite and positive')
    if total == 0:
        return None
    p = successes / total
    factor = 1 + z * z / total
    middle = (p + z * z / (2 * total)) / factor
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / factor
    return {'lower': max(0.0, middle - radius), 'upper': min(1.0, middle + radius)}


def _ratio(numerator, denominator, reason=None):
    result = {'value': numerator / denominator if denominator else None,
              'numerator': numerator, 'denominator': denominator,
              'wilson95': wilson_interval(numerator, denominator)}
    if not denominator:
        result['undefined_reason'] = reason or 'empty denominator'
    return result


def _undefined(reason):
    return {'value': None, 'numerator': None, 'denominator': None,
            'wilson95': None, 'undefined_reason': reason}


def _f1(precision, recall):
    p, r = precision['value'], recall['value']
    return None if p is None or r is None else (2 * p * r / (p + r) if p + r else 0.0)


def _classification(rows):
    """Rows are (gold label, explicit prediction); unknown truth stays visible."""
    matrix = Counter(tp=0, fp=0, fn=0, tn=0)
    positive_abstentions = negative_abstentions = unknown_abstentions = unknown_truth = 0
    for truth, prediction in rows:
        if truth == 'unknown':
            unknown_truth += 1
            unknown_abstentions += prediction == 'abstain'
        elif prediction == 'abstain':
            positive_abstentions += truth == 'yes'
            negative_abstentions += truth == 'no'
        else:
            matrix[{('yes', 'yes'): 'tp', ('no', 'yes'): 'fp',
                    ('yes', 'no'): 'fn', ('no', 'no'): 'tn'}[(truth, prediction)]] += 1
    tp, fp, fn, tn = (matrix[key] for key in ('tp', 'fp', 'fn', 'tn'))
    positives = tp + fn + positive_abstentions
    negatives = tn + fp + negative_abstentions
    known = positives + negatives
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, positives)
    specificity = _ratio(tn, negatives)
    subset_accuracy = _ratio(tp + tn, known)
    balanced = ((recall['value'] + specificity['value']) / 2
                if positives and negatives else None)
    f1_denominator = 2 * tp + fp + fn + positive_abstentions
    known_f1 = 2 * tp / f1_denominator if f1_denominator else None
    abstentions = positive_abstentions + negative_abstentions + unknown_abstentions
    total = known + unknown_truth
    return {'confusion_matrix': dict(matrix),
            'positive_support': positives, 'negative_support': negatives,
            'unknown_truth_support': unknown_truth,
            'abstentions': {'positive': positive_abstentions, 'negative': negative_abstentions,
                            'unknown_truth': unknown_abstentions},
            'known_label_precision': precision, 'known_positive_recall': recall,
            'known_negative_specificity': specificity,
            'known_label_f1': known_f1,
            'adjudicated_subset_accuracy': subset_accuracy,
            'answered_adjudicated_subset_accuracy': _ratio(tp + tn, tp + fp + fn + tn),
            'accuracy': (_undefined('unknown ground truth prevents cohort-wide accuracy')
                         if unknown_truth else subset_accuracy),
            'balanced_accuracy': (None if unknown_truth else balanced),
            'adjudicated_subset_balanced_accuracy': balanced,
            'decision_coverage': _ratio(total - abstentions, total),
            'adjudicated_decision_coverage': _ratio(tp + fp + fn + tn, known),
            'confidence_interval_scope': 'Wilson intervals are marginal proportions, not F1 or balanced-accuracy intervals',
            'abstention_policy': 'known-label abstentions remain in recall, specificity and accuracy denominators'}


def _distribution(values):
    ordered = sorted(values)
    if not ordered:
        return {'min': None, 'p50': None, 'p95': None, 'max': None, 'mean': None}
    # Nearest-rank quantiles are exact and easy to reproduce for small cohorts.
    return {'min': ordered[0], 'p50': ordered[max(0, math.ceil(.5 * len(ordered)) - 1)],
            'p95': ordered[max(0, math.ceil(.95 * len(ordered)) - 1)],
            'max': ordered[-1], 'mean': sum(ordered) / len(ordered)}


def _normalize_pairs(rows, cases, cutoff, *, references):
    merged = {}
    excluded = []
    label_ignored = 0
    for index, raw in enumerate(rows):
        entity_id = raw.get('entity_id')
        if entity_id not in cases:
            raise ValueError('article entity_id is not in cases: ' + str(entity_id))
        url = canonical_url(raw.get('url'))
        published = _day(raw.get('published_at'), 'published_at', optional=True)
        if published and published > cutoff:
            if references:
                raise ValueError('reference article published after cutoff: ' + url)
            excluded.append({'entity_id': entity_id, 'url': url, 'reason': 'published_after_cutoff'})
            continue
        relevance = _label(raw.get('relevance', 'unknown'), 'relevance')
        incident = _label(raw.get('incident', 'unknown'), 'incident')
        if references:
            reviewed = True
            eligible = raw.get('retrieval_eligible')
            if eligible is not None and type(eligible) is not bool:
                raise ValueError('retrieval_eligible must be boolean or null')
        else:
            reviewed = raw.get('reviewed_independently', False)
            if type(reviewed) is not bool or type(raw.get('has_text')) is not bool:
                raise ValueError('reviewed_independently and has_text must be booleans')
            if not reviewed:
                label_ignored += relevance != 'unknown' or incident != 'unknown'
                relevance = incident = 'unknown'
            eligible = None
        row = {'entity_id': entity_id, 'url': url, 'published_at': published.isoformat() if published else None,
               'relevance': relevance, 'incident': incident,
               'has_text': True if references else raw['has_text'],
               'reviewed_independently': reviewed, 'retrieval_eligible': eligible,
               'source_ids': {str(raw['source_id'])} if raw.get('source_id') else set(),
               'body_hashes': set()}
        if raw.get('body_sha256'):
            body_hash = raw['body_sha256']
            if not isinstance(body_hash, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', body_hash):
                raise ValueError('body_sha256 must be a 64-character SHA256 hex digest')
            row['body_hashes'].add(body_hash.lower())
        key = (entity_id, url)
        if key in merged:
            previous = merged[key]
            for field in ('relevance', 'incident', 'published_at', 'retrieval_eligible'):
                unknown = 'unknown' if field in ('relevance', 'incident') else None
                if previous[field] != unknown and row[field] != unknown and previous[field] != row[field]:
                    raise ValueError('conflicting duplicate ' + field + ': ' + entity_id + ' ' + url)
                if previous[field] == unknown:
                    previous[field] = row[field]
            previous['has_text'] |= row['has_text']
            previous['reviewed_independently'] |= row['reviewed_independently']
            previous['source_ids'].update(row['source_ids'])
            previous['body_hashes'].update(row['body_hashes'])
        else:
            merged[key] = row
    return merged, excluded, label_ignored


def _stage_report(selected, references, cases):
    keys = set(selected)
    positives = {key for key, row in references.items() if row['relevance'] == 'yes'}
    eligible = {key for key in positives if references[key]['retrieval_eligible'] is True}
    unknown_eligibility = {key for key in positives if references[key]['retrieval_eligible'] is None}
    selected_urls = {key[1] for key in keys}
    reference_urls = {key[1] for key in positives}
    # An article shared across several target people is one URL but several
    # entity/article judgments. URL discovery must not imply name attribution.
    eligible_urls = {key[1] for key in eligible}
    reviewed_yes = sum(row['relevance'] == 'yes' for row in selected.values())
    reviewed_no = sum(row['relevance'] == 'no' for row in selected.values())
    unknown = len(selected) - reviewed_yes - reviewed_no
    precision = _ratio(reviewed_yes, reviewed_yes + reviewed_no)
    recall = _ratio(len(keys & positives), len(positives))
    counts = Counter(row['entity_id'] for row in selected.values())
    relevant_entities = {row['entity_id'] for row in selected.values() if row['relevance'] == 'yes'}
    reference_entities = {key[0] for key in positives}
    per_entity_recall = []
    defined_entity_recalls = []
    for entity_id in sorted(cases):
        entity_gold = {key for key in positives if key[0] == entity_id}
        metric = _ratio(len(keys & entity_gold), len(entity_gold))
        per_entity_recall.append({'entity_id': entity_id, **metric})
        if metric['value'] is not None:
            defined_entity_recalls.append(metric['value'])
    closed_rows = [(row['relevance'], 'yes' if key in keys else 'no')
                   for key, row in references.items()]
    per_source = defaultdict(set)
    for key, row in selected.items():
        for source_id in row['source_ids']:
            per_source[source_id].add(key)
    return {'unique_entity_article_pairs': len(selected),
            'unique_article_urls': len({key[1] for key in selected}),
            'unique_original_body_hashes': len(set().union(*(row['body_hashes'] for row in selected.values()))),
            'text_pairs_without_body_hash': sum(row['has_text'] and not row['body_hashes'] for row in selected.values()),
            'entities_with_any_result': _ratio(len(counts), len(cases)),
            'entities_with_reviewed_relevant_result': _ratio(len(relevant_entities), len(cases)),
            'pairs_per_entity': _distribution([counts[entity_id] for entity_id in cases]),
            'judgments': {'relevant': reviewed_yes, 'irrelevant': reviewed_no,
                          'unknown_or_not_independently_reviewed': unknown},
            'judgment_coverage': _ratio(reviewed_yes + reviewed_no, len(selected)),
            'precision_among_reviewed': precision,
            'precision_all_results_bounds': {
                'lower': reviewed_yes / len(selected) if selected else None,
                'upper': (reviewed_yes + unknown) / len(selected) if selected else None,
                'denominator': len(selected), 'unjudged': unknown},
            'reference_recall_all': recall,
            'reference_recall_known_eligible': _ratio(len(keys & eligible), len(eligible)),
            'reference_unique_url_recall_all': _ratio(len(selected_urls & reference_urls), len(reference_urls)),
            'reference_unique_url_recall_known_eligible': _ratio(len(selected_urls & eligible_urls), len(eligible_urls)),
            'reference_unique_url_recall_scope': 'URL discovery for any entity; exact entity attribution is measured separately by pair recall. A URL is known eligible when any relevant gold pair marks it true.',
            'reference_per_entity_recall_all': per_entity_recall,
            'reference_macro_entity_recall_all': {
                'value': (sum(defined_entity_recalls) / len(defined_entity_recalls)
                          if defined_entity_recalls else None),
                'entities_with_references': len(defined_entity_recalls),
                'entities_without_references': len(cases) - len(defined_entity_recalls),
                'scope': 'Mean of per-entity pair recall over entities with at least one relevant frozen reference; no-reference entities remain undefined.'},
            'reference_eligibility': {'known_eligible': len(eligible),
                                      'known_ineligible': len(positives - eligible - unknown_eligibility),
                                      'unknown': len(unknown_eligibility)},
            'reference_entity_coverage': _ratio(len({key[0] for key in keys & positives}), len(reference_entities)),
            'f1_reviewed_precision_and_frozen_reference_recall': _f1(precision, recall),
            'closed_reference_classification': _classification(closed_rows),
            'per_source_entity_article_pairs': {key: len(value) for key, value in sorted(per_source.items())}}


def evaluate_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and evaluate the schema-v1 payload without network or mutation."""
    _object(payload, 'payload')
    if type(payload.get('schema_version')) is not int or payload['schema_version'] != 1:
        raise ValueError('schema_version must be 1')
    cutoff = _day(payload.get('cutoff'), 'cutoff')
    case_rows = _rows(payload.get('cases'), 'cases')
    cases = {}
    for row in case_rows:
        entity_id = row.get('entity_id')
        if not isinstance(entity_id, str) or not entity_id.strip() or entity_id in cases:
            raise ValueError('case entity_id must be a unique nonempty string')
        if not isinstance(row.get('cohort'), str) or not row['cohort'].strip():
            raise ValueError('case cohort must be a nonempty string')
        label = _label(row.get('incident_label', 'unknown'), 'incident_label')
        adjudicated = row.get('label_adjudicated', False)
        if type(adjudicated) is not bool:
            raise ValueError('label_adjudicated must be boolean')
        if label != 'unknown' and not adjudicated:
            raise ValueError('known entity labels require explicit adjudication')
        label_as_of = _day(row.get('label_as_of'), 'label_as_of', optional=True)
        if label != 'unknown' and label_as_of and label_as_of > cutoff:
            raise ValueError('known entity label_as_of is after cutoff')
        # A group described as no known incidents is a sampling stratum, not a
        # verified negative. Explicit closed-corpus adjudication is still allowed.
        cases[entity_id] = dict(row, incident_label=label, label_adjudicated=adjudicated)
    references, _, _ = _normalize_pairs(
        _rows(payload.get('reference_articles'), 'reference_articles'), cases, cutoff, references=True)
    retrieved_rows = _rows(payload.get('retrieved', []), 'retrieved')
    retrieved, excluded, ignored = _normalize_pairs(retrieved_rows, cases, cutoff, references=False)
    snapshot = _object(payload.get('gold_snapshot'), 'gold_snapshot')
    frozen_at = _instant(snapshot.get('frozen_at'), 'gold_snapshot.frozen_at')
    expected_hash = gold_snapshot_hash(payload)
    if snapshot.get('sha256') != expected_hash:
        raise ValueError('gold_snapshot.sha256 does not match frozen gold fields')
    run = _object(payload.get('run', {}), 'run')
    started_at = _instant(run['started_at'], 'run.started_at') if run.get('started_at') else None
    if started_at and frozen_at > started_at:
        raise ValueError('gold snapshot was frozen after retrieval run started (leakage)')
    if run.get('completed_at'):
        completed = _instant(run['completed_at'], 'run.completed_at')
        if started_at and completed < started_at:
            raise ValueError('run.completed_at precedes run.started_at')
    predictions = {}
    for row in _rows(payload.get('entity_predictions', []), 'entity_predictions'):
        entity_id = row.get('entity_id')
        if entity_id not in cases or entity_id in predictions:
            raise ValueError('prediction entity_id must be known and unique')
        if row.get('prediction') not in PREDICTIONS:
            raise ValueError('prediction must be yes, no or abstain')
        predictions[entity_id] = row['prediction']
    # If the same pair has both a frozen label and a later independent review,
    # disagreement is an adjudication problem, not a number to average away.
    for key in references.keys() & retrieved.keys():
        for field in ('relevance', 'incident'):
            gold, review = references[key][field], retrieved[key][field]
            if gold != 'unknown' and review != 'unknown' and gold != review:
                raise ValueError('retrieved review disagrees with frozen reference ' + field + ': ' + key[1])
    texts = {key: row for key, row in retrieved.items() if row['has_text']}
    document_predictions = defaultdict(dict)
    for row in _rows(payload.get('document_predictions', []), 'document_predictions'):
        entity_id = row.get('entity_id')
        key = (entity_id, canonical_url(row.get('url')))
        variant = row.get('variant', 'default')
        if not isinstance(variant, str) or not variant.strip():
            raise ValueError('document prediction variant must be a nonempty string')
        if key not in texts:
            raise ValueError('document prediction requires a retrieved original text: ' + key[1])
        if key in document_predictions[variant] or row.get('prediction') not in PREDICTIONS:
            raise ValueError('document predictions must be unique per variant and yes, no or abstain')
        if row.get('target', 'reported_adverse_role_at_publication') != 'reported_adverse_role_at_publication':
            raise ValueError('document prediction target differs from reviewed adverse role at publication')
        document_predictions[variant][key] = row['prediction']
    document_classification = {}
    for variant, predicted in sorted(document_predictions.items()):
        # Relevance and incident are independent human judgments. A homonym or
        # unreviewed text cannot silently become a negative incident example.
        audited = {key: row for key, row in texts.items()
                   if row['reviewed_independently'] and row['relevance'] == 'yes'}
        document_classification[variant] = {
            **_classification([(row['incident'], predicted.get(key, 'abstain'))
                               for key, row in audited.items()]),
            'scope': 'adverse role reported at publication on independently reviewed relevant entity-text pairs',
            'target': 'reported_adverse_role_at_publication',
            'audited_relevant_text_pairs': len(audited),
            'predictions_excluded_irrelevant_or_unreviewed': len(set(predicted) - set(audited)),
            'missing_predictions_counted_as_abstentions': len(set(audited) - set(predicted))}
    cohorts = {}
    for cohort in sorted({row['cohort'] for row in cases.values()}):
        subset = {key: row for key, row in cases.items() if row['cohort'] == cohort}
        cohorts[cohort] = {
            'entities': len(subset),
            'candidates': _stage_report({key: row for key, row in retrieved.items() if key[0] in subset},
                                        {key: row for key, row in references.items() if key[0] in subset}, subset),
            'texts': _stage_report({key: row for key, row in texts.items() if key[0] in subset},
                                   {key: row for key, row in references.items() if key[0] in subset}, subset)}
    known_positives = {key for key, row in cases.items() if row['incident_label'] == 'yes'}
    found_incident = {row['entity_id'] for row in texts.values()
                      if row['relevance'] == 'yes' and row['incident'] == 'yes'}
    limitations = run.get('source_limitations', [])
    if not isinstance(limitations, list) or any(not isinstance(value, str) for value in limitations):
        raise ValueError('run.source_limitations must be an array of strings')
    return {'schema_version': 1, 'evaluation_kind': 'finite-frozen-reference-retrieval',
            'cutoff': cutoff.isoformat(),
            'gold_snapshot': {'sha256': expected_hash, 'frozen_at': snapshot['frozen_at'],
                              'freeze_precedes_run_verified': started_at is not None,
                              'selection_independence': 'requires external provenance; hash alone cannot prove independence'},
            'volume': {'entities': len(cases), 'raw_retrieved_rows': len(retrieved_rows),
                       'duplicate_pair_rows_removed': len(retrieved_rows) - len(excluded) - len(retrieved),
                       'excluded_post_cutoff_rows': len(excluded),
                       'raw_reference_rows': len(payload['reference_articles']),
                       'unique_reference_pairs': len(references),
                       'unique_reference_urls': len({key[1] for key in references}),
                       'nonindependent_labels_ignored': ignored},
            'candidates': _stage_report(retrieved, references, cases),
            'texts': _stage_report(texts, references, cases),
            'cohorts': cohorts,
            'entity_classification': _classification([
                (row['incident_label'], predictions.get(entity_id, 'abstain'))
                for entity_id, row in cases.items()]),
            'document_classification': document_classification,
            'known_positive_incident_document_detection': _ratio(
                len(known_positives & found_incident), len(known_positives)),
            'excluded_rows': excluded,
            'source_limitations': limitations,
            'interpretation': [
                'Recall is relative to the frozen supplied reference set, not all public news.',
                'Candidate pairs, original texts and unique article URLs have different denominators.',
                'Precision uses independent relevance judgments; unjudged results remain in bounds.',
                'Closed-reference accuracy concerns retrieval within that finite labeled corpus only.',
                'No-known-incident controls do not become verified negative entities.',
                'Entity labels describe documented reports or administrative incidents, not guilt or personal risk.',
                'Missing publication dates are retained and make the temporal scope partially unverifiable.',
                'Confidence intervals describe binomial uncertainty, not sample-selection bias.'],
            'missing_publication_dates': {
                'retrieved_pairs': sum(row['published_at'] is None for row in retrieved.values()),
                'reference_pairs': sum(row['published_at'] is None for row in references.values()),
                'known_entity_label_as_of': sum(row['incident_label'] != 'unknown' and not row.get('label_as_of')
                                                for row in cases.values())}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Schema-v1 JSON payload')
    parser.add_argument('--output', type=Path, help='Write JSON report; otherwise print')
    args = parser.parse_args(argv)
    report = evaluate_backtest(json.loads(args.input.read_text(encoding='utf-8')))
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding='utf-8')
    else:
        print(rendered, end='')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
