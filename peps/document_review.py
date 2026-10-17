"""Literal documentary leads, separate from identity and personal scoring."""
from __future__ import annotations

import hashlib

from .explainability import CATEGORIES, STAGES

VERSION = 'document-review-2'
_ORDER = {'sanction': 0, 'conviction': 1, 'arrest': 2, 'wanted': 3,
          'investigation': 4, 'allegation': 5}


def document_review(entity, evidence, *, limit=100):
    """Read validated run inputs without changing their identity or review.

    Stored decisions must still match their canonical model and original body.
    Older model snapshots require an explicit reanalysis, never a silent rewrite
    of their historical score. A documentary lead is not an attributed event.
    """
    from .identity import entity_for_analysis
    from .nlp import analyze
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('El límite de fragmentos debe estar entre 1 y 500')
    leads, seen, outdated, invalid, documents = [], set(), 0, 0, set()
    for item in evidence:
        metadata, review = item.get('metadata') or {}, item.get('review') or {}
        if (not isinstance(metadata, dict) or not isinstance(review, dict)
                or review.get('status') == 'rejected' or item.get('source_kind') == 'discovery'
                or metadata.get('discovery_only')):
            continue
        body = item.get('text') or ''
        if not isinstance(body, str) or not body.strip() or not item.get('source_url'):
            continue
        content_hash = hashlib.sha256(body.encode()).hexdigest()
        if item.get('content_hash') != content_hash:
            invalid += 1
            continue
        resolution = item.get('identity_resolution')
        if isinstance(resolution, dict) and resolution.get('status') == 'rejected':
            continue
        target = entity_for_analysis(entity, resolution) if isinstance(resolution, dict) else entity
        canonical = analyze(body, target, item.get('source_kind', 'unknown'))
        stored = item.get('analysis')
        if not isinstance(stored, dict) or any(stored.get(key) != canonical.get(key) for key in
                ('entity_signature', 'content_hash', 'model_version', 'rules_hash')):
            outdated += 1
            continue
        if any(stored.get(key) != canonical.get(key) for key in ('mentions', 'findings', 'identity_confidence')):
            invalid += 1
            continue
        resolutions = []
        for finding in stored.get('findings', []):
            start, end = finding.get('start'), finding.get('end')
            if (finding.get('stage') in {'acquittal', 'dismissed'}
                    and finding.get('relationship') == 'direct' and not finding.get('negated')
                    and not finding.get('hypothetical') and type(start) is int and type(end) is int
                    and 0 <= start < end <= len(body) and body[start:end] == finding.get('quote')):
                resolutions.append({'start': start, 'end': end, 'quote': finding['quote'],
                                    'stage': finding['stage'], 'stage_label': STAGES[finding['stage']]})
        for finding in stored.get('findings', []):
            start, end = finding.get('start'), finding.get('end')
            if (finding.get('stage') not in _ORDER or finding.get('category') not in CATEGORIES
                    or finding.get('relationship') != 'direct' or finding.get('negated')
                    or finding.get('hypothetical') or type(start) is not int or type(end) is not int
                    or not 0 <= start < end <= len(body) or body[start:end] != finding.get('quote')):
                continue
            if not any(mention.get('entity_id') == entity.get('id')
                       and start <= mention.get('start', -1) < end for mention in stored.get('mentions', [])):
                continue
            key = (content_hash, start, end, finding['category'], finding['stage'])
            if key in seen:
                continue
            seen.add(key)
            documents.add(content_hash)
            lead = {'evidence_id': item['id'], 'document_id': item.get('document_id'),
                'title': item.get('title') or 'Documento sin título', 'source_url': item['source_url'],
                'published_at': item.get('published_at'), 'content_hash': content_hash,
                'start': start, 'end': end, 'quote': finding['quote'], 'category': finding['category'],
                'category_label': CATEGORIES[finding['category']], 'stage': finding['stage'],
                'stage_label': STAGES[finding['stage']], 'uncertain': bool(finding.get('uncertain')),
                'adverse_alert': not bool(resolutions),
                'chronology_state': 'mixed_requires_review' if resolutions else 'reported_adverse_lead',
                'resolution_context': resolutions,
                'identity_status': resolution.get('status') if isinstance(resolution, dict) else 'unresolved',
                'human_review_status': review.get('status', 'pending'),
                'personal_score_contribution': None, 'analysis_model_version': stored.get('model_version')}
            # Preserve narrowly scoped documentary semantics, without treating
            # a petition as criminal conduct or a procedure title as a read
            # resolution. Offsets still reference the immutable original body.
            for key in ('trigger_start', 'trigger_end', 'attribution_basis', 'context_review_required',
                        'documentary_scope', 'procedural_outcome', 'underlying_resolution_read',
                        'criminal_conduct_reported', 'removal_from_office_established', 'detention_executed'):
                if key in finding:
                    lead[key] = finding[key]
            leads.append(lead)
    leads.sort(key=lambda item: (_ORDER[item['stage']], item['uncertain'], item['content_hash'], item['start']))
    return {'version': VERSION, 'scope': 'documentary_leads_not_personal_risk',
            'documents_with_leads': len(documents), 'total_leads': len(leads), 'items': leads[:limit],
            'truncated': len(leads) > limit, 'reanalyze_required': outdated, 'invalid_inputs': invalid,
            'identity_confirmations_created': 0,
            'note': 'Fragmentos documentales para revisión. No confirman identidad, conducta ni culpabilidad; '
                    'no modifican el score personal ni sustituyen la revisión humana.'}
