"""Fast acquisition relevance, deliberately separate from identity and scoring.

A literal name is enough to retrieve a candidate article, never to validate its
identity or an adverse fact. Aliases need a nearby bank context hint. No offence
keywords are needed: victim, denial and neutral stories remain retrievable.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from .identity import _sequence, _tokens

MODEL_VERSION = 'metadata-relevance-1.0.0'


@dataclass(frozen=True)
class _Phrase:
    entity_id: str
    text: str
    tokens: tuple
    kind: str
    context: tuple


class RelevanceIndex:
    """Read-only token index; safe to reuse across acquisition worker threads."""

    def __init__(self, entities, context_radius=160):
        phrases = []
        for entity in entities:
            entity_id = str(entity['id'])
            context = entity.get('context') or {}
            hints = []
            if isinstance(context, dict):
                for key in ('state', 'municipality', 'city', 'role', 'position', 'cargo', 'organization', 'employer'):
                    values = context.get(key) or []
                    for value in values if isinstance(values, list) else [values]:
                        if isinstance(value, dict):
                            value = value.get('value') or value.get('name')
                        if isinstance(value, str) and (sequence := _sequence(value)) and sequence not in (('mexico',), ('gobierno',), ('persona',)):
                            hints.append(sequence)
            variants = [(entity.get('name'), 'name')]
            variants.extend((alias, 'alias') for alias in entity.get('aliases') or [])
            if isinstance(context, dict):
                for key in ('verified_aliases', 'legal_name', 'legal_names', 'trade_names', 'abbreviations'):
                    values = context.get(key) or []
                    if isinstance(values, dict):
                        values = [dict(value, name=name) if isinstance(value, dict) else name for name, value in values.items()]
                    if not isinstance(values, list):
                        values = [values]
                    variants.extend((value, 'alias') for value in values)
            seen = set()
            for value, kind in variants:
                if isinstance(value, dict):
                    if value.get('verified') is False or value.get('validated') is False or value.get('status') in ('rejected', 'revoked'):
                        continue
                    value = value.get('name') or value.get('value') or value.get('alias')
                if not isinstance(value, str) or not (sequence := _sequence(value)) or len(sequence) > 32 or sequence in seen:
                    continue
                seen.add(sequence)
                phrases.append(_Phrase(entity_id, value, sequence, kind, tuple(hints)))
        frequency = Counter(token for phrase in phrases for token in set(phrase.tokens))
        anchors = defaultdict(list)
        for phrase in phrases:
            anchor = min(range(len(phrase.tokens)), key=lambda index: (frequency[phrase.tokens[index]], index))
            anchors[phrase.tokens[anchor]].append((anchor, phrase))
        self._anchors = dict(anchors)
        self.context_radius = max(0, int(context_radius))

    def rank(self, title, summary, *, entity_ids=None, names=None):
        allowed_ids = {str(value) for value in entity_ids} if entity_ids is not None else None
        allowed_names = {_sequence(value) for value in names if isinstance(value, str)} if names is not None else None
        matches, uncorroborated_alias = [], False
        metadata = str(title or '') + '\n' + str(summary or '')
        context_tokens = _tokens(metadata)
        token_starts = [token.start for token in context_tokens]
        token_ends = [token.end for token in context_tokens]
        for part, raw in (('title', title), ('summary', summary)):
            text = str(raw or '')
            base = 0 if part == 'title' else len(str(title or '')) + 1
            tokens = context_tokens[bisect_left(token_starts, base):bisect_right(token_ends, base + len(text))]
            words = tuple(token.value for token in tokens)
            for index, token in enumerate(tokens):
                for anchor, phrase in self._anchors.get(token.value, ()):
                    if allowed_ids is not None and phrase.entity_id not in allowed_ids:
                        continue
                    if allowed_names is not None and phrase.tokens not in allowed_names:
                        continue
                    start, end = index - anchor, index - anchor + len(phrase.tokens)
                    if start < 0 or end > len(tokens) or words[start:end] != phrase.tokens:
                        continue
                    global_start, global_end = tokens[start].start, tokens[end - 1].end
                    char_start, char_end = global_start - base, global_end - base
                    nearby = context_tokens[bisect_left(token_starts, max(0, global_start - self.context_radius)):
                                            bisect_right(token_ends, global_end + self.context_radius)]
                    local = tuple(t.value for t in nearby)
                    supported = any(any(local[offset:offset + len(hint)] == hint
                                         and (nearby[offset + len(hint) - 1].end <= global_start or nearby[offset].start >= global_end)
                                         for offset in range(len(local) - len(hint) + 1)) for hint in phrase.context)
                    if (phrase.kind == 'alias' or len(phrase.tokens) == 1) and not supported:
                        uncorroborated_alias = True
                        continue
                    matches.append({'entity_id': phrase.entity_id, 'phrase': phrase.text, 'kind': phrase.kind,
                                    'part': part, 'start': char_start, 'end': char_end,
                                    'text': text[char_start:char_end], 'context_supported': supported})
        matches.sort(key=lambda match: (match['part'] != 'title', match['start'], match['entity_id'], match['kind']))
        identities = sorted({match['entity_id'] for match in matches})
        priority = max((100 if match['kind'] == 'name' and match['part'] == 'title' else
                        80 if match['kind'] == 'name' else 60 if match['part'] == 'title' else 40
                        for match in matches), default=0)
        return {'relevant': bool(matches), 'rank': priority, 'entity_ids': identities, 'matches': matches,
                'reason': 'literal_name_or_contextual_alias' if matches else 'alias_without_context' if uncorroborated_alias else 'no_target_mention',
                'model_version': MODEL_VERSION, 'identity_verified': False, 'score_effect': 'none'}


def zero_score_reasons(result, *, acquisition=None):
    """Explain an actual zero from scorer traces; never reinterpret a source."""
    if result.get('score') != 0:
        return []
    audits = result.get('evidence_audit') or []
    if not audits:
        return [{'code': 'no_entity_evidence', 'label': 'No hay evidencia atribuida a esta entidad.', 'count': 0}]
    counts = Counter()
    for audit in audits:
        exclusions = set(audit.get('exclusions') or [])
        if 'discovery_index_is_not_article_evidence' in exclusions:
            counts['metadata_only_not_body'] += 1
        if 'missing_source_body' in exclusions:
            counts['missing_source_body'] += 1
        if 'missing_source_url' in exclusions:
            counts['missing_source_url'] += 1
        if exclusions & {'news_identity_not_attributed', 'stale_or_invalid_identity_resolution', 'ambiguous_identity',
                         'identity_ambiguous_or_requires_selected_spans', 'no_verifiable_entity_mention'}:
            counts['identity_pending'] += 1
        if 'human_rejected' in exclusions:
            counts['human_rejected'] += 1
        if not exclusions and not audit.get('findings'):
            counts['no_adverse_findings'] += 1
        for finding in audit.get('findings') or []:
            finding_exclusions = set(finding.get('exclusions') or [])
            if finding_exclusions & {'negated', 'hypothetical_or_requested_action'} or finding.get('stage') in ('acquittal', 'dismissed', 'denial'):
                counts['negated_or_exculpatory'] += 1
            if 'association_is_not_adverse_conduct' in finding_exclusions:
                counts['indirect_conduct'] += 1
    if not counts:
        counts['other_exclusions_or_rounding'] = 1
    labels = {'metadata_only_not_body': 'Sólo hay metadatos de descubrimiento; falta el cuerpo original del artículo.',
              'missing_source_body': 'Falta un cuerpo de fuente verificable.', 'missing_source_url': 'Falta la URL de procedencia.',
              'identity_pending': 'La identidad o mención requiere corroboración; una coincidencia nominal no valida a la persona.',
              'human_rejected': 'La revisión humana rechazó la evidencia.',
              'no_adverse_findings': 'El texto evaluado no contiene hallazgos adversos puntuables para esta entidad.',
              'negated_or_exculpatory': 'Se conservaron negaciones, acciones hipotéticas o resoluciones exculpatorias sin generar puntos adversos.',
              'indirect_conduct': 'La referencia es indirecta o una asociación; no atribuye una conducta adversa directa.',
              'other_exclusions_or_rounding': 'Consulte las exclusiones y el redondeo de la traza; cero no acredita ausencia de riesgo.'}
    return [{'code': code, 'label': labels[code], 'count': count} for code, count in sorted(counts.items())]


def assessment_summary(result):
    """Keep a computed zero distinct from having no assessable source text."""
    score = result.get('score', 0)
    reasons = zero_score_reasons(result)
    audits = result.get('evidence_audit') or []
    pending_codes = {'news_identity_not_attributed', 'stale_or_invalid_identity_resolution', 'ambiguous_identity',
                     'identity_ambiguous_or_requires_selected_spans', 'no_verifiable_entity_mention',
                     'invalid_identity_mentions', 'invalid_human_identity_selection'}
    text_codes = {'discovery_index_is_not_article_evidence', 'missing_source_body'}
    # A stale human confirmation may downgrade a document to provisional, but
    # it does not exclude otherwise canonical, attributable source findings.
    admissible = any(not (set(audit.get('exclusions') or []) - {'human_identity_review_stale_or_incomplete'}) for audit in audits)
    if score > 0 or admissible:
        state, label, display = 'evaluated', 'Evaluado' if score else 'Evaluado: sin señales puntuables', score
    elif audits and all(set(audit.get('exclusions') or []) & text_codes for audit in audits):
        state, label, display = 'insufficient_text', 'Falta el cuerpo original de la noticia', None
    elif any(set(audit.get('exclusions') or []) & pending_codes for audit in audits):
        state, label, display = 'pending_identity', 'Identidad pendiente de corroboración', None
    else:
        state, label, display = 'insufficient_evidence', 'Sin evidencia atribuida suficiente', None
    return {'state': state, 'label': label, 'display_score': display,
            'reason_codes': [reason['code'] for reason in reasons], 'zero_reasons': reasons}
