"""Offline, label-blind predictions on authorized, conserved article bodies.

This is a frozen evaluation adapter, not a new model or a scoring engine. It
never reads adjudication labels, downloads text, verifies an identity, or treats
reported allegations as established facts. Two adapters target the adverse role
reported at publication: a direct adverse signal and an auxiliary contextual role.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from peps import context_model, nlp
from peps.news import canonical_url

MAX_TEXT_BYTES = 1_000_000
HARD_TEXT_BYTES = 8_000_000
MAX_DOCUMENTS = 100_000
MAX_ENTITIES = 10_000
EXCULPATORY_STAGES = frozenset(('acquittal', 'dismissed', 'denial'))
POSITIVE_STAGES = frozenset(nlp.STAGE_PATTERNS) - EXCULPATORY_STAGES
# analyze() also emits the legitimate "reputational" fallback when a direct
# procedural stage has no narrower category keyword. The evaluation adapter
# must include every category the unchanged production analyzer can emit.
ADVERSE_CATEGORIES = frozenset(nlp.CATEGORY_SEVERITY)
OTHER_KNOWN_ROLES = frozenset(context_model.ROLE_RELEVANCE) - {'accused', 'insufficient'}
VARIANTS = ('nlp_direct_adverse', 'context_role_auxiliary')
CONTEXT_FIELDS = ('state', 'city', 'country', 'position', 'role', 'organization',
                  'jurisdiction', 'occupation')


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _day(value: Any, name: str, optional: bool = False) -> date | None:
    if optional and value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValueError(name + ' must be an ISO date or timestamp')
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if instant.tzinfo is None:
            raise ValueError('timestamps must include a timezone')
        return instant.astimezone(timezone.utc).date()
    except ValueError as exc:
        raise ValueError(name + ' must be an ISO date or timezone-aware timestamp') from exc


def _entities(rows: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ENTITIES:
        raise ValueError('entities must be a nonempty bounded array')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('entity must be an object')
        identifier, name = row.get('id'), row.get('name')
        if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 150
                or not isinstance(name, str) or not name.strip() or len(name) > 512
                or identifier in result):
            raise ValueError('entities require unique IDs and bounded names')
        aliases = row.get('aliases') or []
        if (not isinstance(aliases, list) or len(aliases) > 32
                or any(not isinstance(alias, str) or not alias.strip() or len(alias) > 512
                       for alias in aliases)):
            raise ValueError('invalid or excessive entity aliases')
        context = row.get('context') or {}
        if not isinstance(context, dict):
            raise ValueError('entity context must be an object')
        safe_context = {}
        for field in CONTEXT_FIELDS:
            value = context.get(field)
            if value in (None, ''):
                continue
            values = value if isinstance(value, list) else [value]
            if len(values) > 20 or any(not isinstance(part, str) or len(part) > 512 for part in values):
                raise ValueError('invalid or excessive entity context')
            safe_context[field] = values
        # Deliberately whitelist nominal input. Incident/cohort/review fields
        # cannot enter either analyzer or its entity signature.
        result[identifier] = {'id': identifier, 'name': name, 'kind': 'person',
                              'aliases': list(aliases), 'context': safe_context}
    return result


def _policy(max_text_bytes: int) -> dict[str, Any]:
    return {'version': 'notary-document-adapters-2',
            'adapter_revision': 'include_production_reputational_fallback',
            'revision_kind': 'evaluation_adapter_correction_not_model_improvement',
            'production_model_or_rules_modified': False,
            'nlp_positive_stages': sorted(POSITIVE_STAGES),
            'nlp_adverse_categories': sorted(ADVERSE_CATEGORIES),
            'nlp_required_relationship': 'direct',
            'nlp_exclude_negated_and_hypothetical': True,
            'nlp_target': 'reported_adverse_role_at_publication',
            'nlp_direct_adverse_and_exculpatory': 'abstain_unresolved_contradiction',
            'context_positive_role': 'accused',
            'context_negative_roles': sorted(OTHER_KNOWN_ROLES),
            'context_target': 'reported_adverse_role_at_publication',
            'missing_publication_date': 'abstain',
            'publication_after_cutoff': 'abstain',
            'missing_or_bounded_body': 'abstain_without_prefix_inference',
            'max_text_bytes': max_text_bytes,
            'identity_status': 'not_verified', 'scoring_performed': False,
            'adjudication_labels_consumed_by_inference': False}


def _provenance(policy: dict[str, Any]) -> dict[str, Any]:
    info = context_model.get_model_info()
    paths = {'evaluation_adapter': Path(__file__), 'nlp': Path(nlp.__file__),
             'context_model': Path(context_model.__file__),
             'context_artifact': context_model.MODEL_PATH}
    return {'source_sha256': {key: _hash(path.read_bytes()) for key, path in paths.items()},
            'nlp_model_version': nlp.MODEL_VERSION,
            'nlp_rules_version': nlp.RULES_VERSION, 'nlp_rules_hash': nlp.RULES_HASH,
            'context_model_version': info['model_version'],
            'context_artifact_sha256': info['artifact_sha256'],
            'context_config_hash': info['config_hash'],
            'context_runtime_config_hash': info['runtime_config_hash'],
            'context_training_corpus_sha256': info['corpus_sha256'],
            'context_training_kind': 'synthetic_examples_not_real_news_validation',
            'adapter_policy_sha256': _hash(_canonical(policy))}


def _body(path_value: Any, root: Path, maximum: int) -> tuple[str | None, dict[str, Any]]:
    if path_value in (None, ''):
        return None, {'body_status': 'missing_body', 'content_sha256': None}
    if not isinstance(path_value, str) or len(path_value) > 4096:
        raise ValueError('text_path must be a bounded path')
    candidate = Path(path_value)
    try:
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError('text_path cannot be resolved safely') from exc
    if not resolved.is_relative_to(root):
        raise ValueError('text_path escapes the authorized bank')
    relative = str(resolved.relative_to(root))
    if not resolved.is_file():
        return None, {'body_status': 'missing_body', 'text_path': relative, 'content_sha256': None}
    # Read at most one byte beyond the budget; never silently analyze a prefix.
    # The open path is checked again through its descriptor on systems with
    # /proc, closing the usual symlink substitution gap for this offline runner.
    try:
        with resolved.open('rb') as handle:
            descriptor_path = Path('/proc/self/fd') / str(handle.fileno())
            if descriptor_path.exists() and not descriptor_path.resolve().is_relative_to(root):
                raise ValueError('opened text_path escapes the authorized bank')
            data = handle.read(maximum + 1)
    except OSError:
        return None, {'body_status': 'unreadable_body', 'text_path': relative,
                      'content_sha256': None}
    metadata = {'text_path': relative, 'observed_bytes': len(data)}
    if len(data) > maximum:
        return None, {**metadata, 'body_status': 'text_byte_budget_exceeded',
                      'content_sha256': None, 'observed_prefix_sha256': _hash(data)}
    metadata['content_sha256'] = _hash(data)
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None, {**metadata, 'body_status': 'invalid_utf8'}
    return (text, {**metadata, 'body_status': 'complete', 'text_chars': len(text)}) if text.strip() else (
        None, {**metadata, 'body_status': 'empty_body', 'text_chars': len(text)})


def _nlp_prediction(text: str, entity: dict[str, Any]) -> dict[str, Any]:
    result = nlp.analyze(text, entity, source_kind='news')
    positive = [finding for finding in result['findings']
                if finding.get('relationship') == 'direct'
                and finding.get('stage') in POSITIVE_STAGES
                and finding.get('category') in ADVERSE_CATEGORIES
                and not finding.get('negated') and not finding.get('hypothetical')]
    exculpatory = [finding for finding in result['findings']
                   if finding.get('relationship') == 'direct'
                   and finding['stage'] in EXCULPATORY_STAGES
                   and not finding.get('negated') and not finding.get('hypothetical')]
    conflicting = bool(positive and exculpatory)
    prediction = ('abstain' if conflicting else 'yes' if positive else
                  'no' if result['mentions'] else 'abstain')
    # Offsets permit body-based auditing without committing whole quotes.
    fields = ('category', 'stage', 'relationship', 'negated', 'hypothetical',
              'start', 'end', 'trigger_start', 'trigger_end')
    return {'prediction': prediction,
            'reason': 'adverse_and_exculpatory_chronology_unresolved' if conflicting else 'direct_adverse_signal' if positive else (
                'no_exact_target_mention' if not result['mentions'] else 'no_qualifying_direct_signal'),
            'target': 'reported_adverse_role_at_publication', 'mention_count': len(result['mentions']),
            'qualifying_finding_count': len(positive),
            'qualifying_findings': [{key: finding.get(key) for key in fields} for finding in positive],
            'exculpatory_finding_count': len(exculpatory),
            'analyzer_content_sha256': result['content_hash']}


def _context_prediction(text: str, entity: dict[str, Any]) -> dict[str, Any]:
    result = context_model.analyze(text, entity, source_kind='news')
    role = result['role']
    if result['abstained'] or result['truncated'] or role == 'insufficient':
        prediction = 'abstain'
    elif role == 'accused':
        prediction = 'yes'
    elif role in OTHER_KNOWN_ROLES:
        prediction = 'no'
    else:
        prediction = 'abstain'
    return {'prediction': prediction, 'target': 'reported_adverse_role_at_publication',
            'reason': ('context_budget_exceeded' if result['truncated'] else
                       'context_abstained' if prediction == 'abstain' else 'context_role'),
            'role': role, 'predicted_role': result['predicted_role'],
            'decision_margin': result['decision_margin'],
            'competing_roles': result['competing_roles'],
            'truncated': result['truncated'], 'processed_chars': result['processed_chars'],
            'analyzer_content_sha256': result['content_hash'],
            'score_effect': 'auxiliary_only'}


def evaluate_models(payload: dict[str, Any], bank_root: str | Path,
                    max_text_bytes: int = MAX_TEXT_BYTES) -> dict[str, Any]:
    """Predict from bodies/nominal fields only; all gold/review fields are ignored.

    ``payload`` requires ``entities``, ``cutoff``, and ``retrieved`` (``documents``
    or ``reviews`` are accepted aliases). A document requires entity_id, url,
    text_path and published_at. Missing/empty text or publication date abstains.
    Post-cutoff articles are not analyzed. Paths must resolve inside bank_root.
    Labels can be joined to the returned frozen predictions in a separate step.
    """
    started_wall, started_cpu = time.perf_counter(), time.process_time()
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object')
    if isinstance(max_text_bytes, bool) or not isinstance(max_text_bytes, int) or not 1 <= max_text_bytes <= HARD_TEXT_BYTES:
        raise ValueError('max_text_bytes is outside the safe evaluation budget')
    root = Path(bank_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('bank_root must be an existing directory')
    cutoff = _day(payload.get('cutoff'), 'cutoff')
    if cutoff > datetime.now(timezone.utc).date():
        raise ValueError('cutoff cannot be in the future')
    entities = _entities(payload.get('entities'))
    rows = payload.get('retrieved', payload.get('documents', payload.get('reviews')))
    if not isinstance(rows, list) or len(rows) > MAX_DOCUMENTS:
        raise ValueError('retrieved must be a bounded array')
    policy = _policy(max_text_bytes)
    provenance = _provenance(policy)
    predictions, inputs, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get('entity_id') not in entities:
            raise ValueError('document requires a known entity_id')
        entity_id, url = row['entity_id'], row.get('url')
        if not isinstance(url, str) or len(url) > 8192:
            raise ValueError('document requires a bounded public article URL')
        parts = urlsplit(url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            raise ValueError('article URL must be HTTP(S) without credentials')
        url = canonical_url(url)
        if (entity_id, url) in seen:
            raise ValueError('duplicate entity/article pair; deduplicate before prediction')
        seen.add((entity_id, url))
        published = _day(row.get('published_at'), 'published_at', optional=True)
        if published is None:
            text, body = None, {'body_status': 'publication_date_unknown', 'content_sha256': None}
        elif published > cutoff:
            text, body = None, {'body_status': 'publication_after_cutoff', 'content_sha256': None}
        elif row.get('truncated') is True or row.get('body_truncated') is True:
            text, body = None, {'body_status': 'acquisition_body_truncated', 'content_sha256': None}
        elif row.get('has_text') is False:
            text, body = None, {'body_status': 'acquisition_has_no_text', 'content_sha256': None}
        else:
            text, body = _body(row.get('text_path'), root, max_text_bytes)
        expected_hash = row.get('body_sha256')
        if expected_hash is not None:
            if (not isinstance(expected_hash, str) or len(expected_hash) != 64
                    or any(char not in '0123456789abcdef' for char in expected_hash)):
                raise ValueError('body_sha256 must be a lowercase SHA-256 digest')
            body['expected_body_sha256'] = expected_hash
            if text is not None and body['content_sha256'] != expected_hash:
                text = None
                body['body_status'] = 'body_hash_mismatch'
        # Only these sanitized values enter the prediction input receipt.
        inputs.append({'entity_id': entity_id, 'url': url,
                       'published_at': published.isoformat() if published else None, **body})
        common = {'entity_id': entity_id, 'url': url,
                  'published_at': published.isoformat() if published else None, **body}
        for variant, function, target in (
            ('nlp_direct_adverse', _nlp_prediction, 'reported_adverse_role_at_publication'),
            ('context_role_auxiliary', _context_prediction, 'reported_adverse_role_at_publication')):
            cpu_start, wall_start = time.process_time(), time.perf_counter()
            decision = (function(text, entities[entity_id]) if text is not None else
                        {'prediction': 'abstain', 'reason': body['body_status'], 'target': target})
            predictions.append({**common, 'variant': variant, **decision,
                                'cpu_seconds': time.process_time() - cpu_start,
                                'wall_seconds': time.perf_counter() - wall_start})
    # Time fields are observations, excluded from the semantic frozen receipt.
    stable_predictions = [{key: value for key, value in row.items()
                           if key not in {'cpu_seconds', 'wall_seconds'}} for row in predictions]
    ordered = lambda values: sorted(values, key=lambda row: _canonical(row))
    input_receipt = {'entities': ordered(list(entities.values())), 'cutoff': cutoff.isoformat(),
                     'documents': ordered(inputs), 'policy_sha256': provenance['adapter_policy_sha256']}
    return {'schema_version': 1, 'cutoff': cutoff.isoformat(), 'policy': policy,
            'provenance': provenance, 'prediction_inputs_sha256': _hash(_canonical(input_receipt)),
            'predictions_sha256': _hash(_canonical(ordered(stable_predictions))),
            'document_count': len(rows), 'entity_count': len(entities),
            'document_predictions': predictions,
            'prediction_counts': {variant: dict(Counter(row['prediction'] for row in predictions
                                                        if row['variant'] == variant)) for variant in VARIANTS},
            'total_cpu_seconds': time.process_time() - started_cpu,
            'total_wall_seconds': time.perf_counter() - started_wall,
            'limitations': [
                'Adapter v2 corrects a v1 category-filter omission; it is not an improvement to the unchanged NLP model.',
                'Both adapters target the adverse role reported at publication, not a person-level incident history.',
                'No inference establishes identity, guilt, innocence, or absence of incidents.',
                'A no prediction means no qualifying signal or another recognized role in this text.',
                'Context model was trained on synthetic examples; this adapter does not retrain it.',
                'These predictions require independent adjudication before quality metrics are reported.',
                'Publication metadata bounds the snapshot; it does not prove first public availability.']}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True, type=Path)
    parser.add_argument('--bank-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--max-text-bytes', type=int, default=MAX_TEXT_BYTES)
    args = parser.parse_args(argv)
    if args.payload.stat().st_size > 64_000_000:
        parser.error('payload exceeds 64 MB')
    payload = json.loads(args.payload.read_text(encoding='utf-8-sig'))
    result = evaluate_models(payload, args.bank_root, args.max_text_bytes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + '.tmp-' + str(os.getpid()))
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({'document_count': result['document_count'],
                      'predictions_sha256': result['predictions_sha256'],
                      'output': str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
