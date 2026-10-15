"""Faithful presentation of the scorer's trace. No model-generated reasons."""
from __future__ import annotations

import hashlib
import importlib
import json
import marshal
import sys
from collections import Counter
from pathlib import Path

VERSION = 'score-explanation-1.0.0'
CATEGORIES = {'money_laundering': 'lavado de dinero', 'organized_crime': 'crimen organizado',
    'corruption': 'corrupción', 'fraud': 'fraude', 'violence': 'violencia', 'tax': 'materia fiscal',
    'public_procurement': 'contratación pública', 'sanctions': 'sanciones', 'reputational': 'señal reputacional'}
STAGES = {'allegation': 'acusación reportada, sin prueba judicial verificada',
    'investigation': 'investigación reportada', 'arrest': 'detención reportada',
    'wanted': 'búsqueda reportada', 'conviction': 'condena reportada en el texto',
    'sanction': 'sanción reportada', 'acquittal': 'absolución reportada',
    'dismissed': 'archivo o sobreseimiento reportado', 'denial': 'negativa de acusaciones reportada'}
EXCLUSIONS = {'human_rejected': 'Documento rechazado por revisión humana',
    'no_verifiable_entity_mention': 'Sin mención literal verificable',
    'missing_source_body': 'Falta el cuerpo original', 'missing_source_url': 'Falta la URL de procedencia',
    'ambiguous_identity': 'Identidad insuficiente', 'news_identity_not_attributed': 'Noticia sin atribución de identidad',
    'identity_ambiguous_or_requires_selected_spans': 'Identidad ambigua o menciones sin seleccionar',
    'discovery_index_is_not_article_evidence': 'Un índice o resultado de búsqueda no es evidencia del artículo',
    'legacy_source_requires_revalidation': 'Fuente histórica pendiente de revalidación',
    'negated': 'Afirmación negada', 'hypothetical_or_requested_action': 'Acción hipotética o solicitada',
    'association_is_not_adverse_conduct': 'La asociación no atribuye una conducta directa',
    'cached_analysis_content_mismatch': 'Análisis almacenado alterado o inconsistente',
    'finding_outside_corroborated_identity_span': 'Hallazgo fuera de la mención de identidad corroborada',
    'stale_or_invalid_identity_resolution': 'Resolución de identidad obsoleta o inválida',
    'human_identity_review_stale_or_incomplete': 'Revisión humana de identidad obsoleta o incompleta; requiere nueva validación',
    'invalid_evidence_review': 'Metadatos de revisión inválidos',
    'invalid_identity_mentions': 'Formato de menciones de identidad inválido',
    'invalid_human_identity_selection': 'Selección humana de menciones inválida',
    'content_hash_mismatch': 'El cuerpo no corresponde a su hash'}


def scoring_fingerprint():
    """Identify executable authority; bytecode fallback supports frozen bundles."""
    from .scoring import MODEL_VERSION, PARAMETERS_HASH
    files = {}
    for name in ('scoring', 'nlp', 'identity'):
        module = importlib.import_module('peps.' + name)
        path = Path(module.__file__)
        if path.is_file() and path.suffix == '.py':
            files[name] = {'method': 'source_sha256', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            code = module.__loader__.get_code(module.__name__) if hasattr(module.__loader__, 'get_code') else None
            files[name] = {'method': 'bytecode_sha256', 'python': sys.version,
                'sha256': hashlib.sha256(marshal.dumps(code)).hexdigest() if code else None}
    return {'model_version': MODEL_VERSION, 'parameters_hash': PARAMETERS_HASH, 'files': files}


def explain_score(result):
    """Counterfactual event removals use the actual rounded/capped aggregation.

    Removal deltas are not additive attributions. Candidate selection is recorded
    by the scorer, including ties, deduplication and exculpatory suspensions.
    """
    from .scoring import _combine
    components = result.get('components', [])
    audits = {a['evidence_id']: a for a in result.get('evidence_audit', [])}
    factors = []
    for index, component in enumerate(components):
        remaining = components[:index] + components[index+1:]
        without = max(_combine([c['provisional_points'] for c in remaining]),
                      _combine([c['validated_points'] for c in remaining]))
        citations = []
        for selection in component.get('selections', []):
            audit = audits.get(selection['evidence_id'], {})
            findings = audit.get('findings', [])
            i = selection['finding_index']
            if not 0 <= i < len(findings):
                continue
            finding = findings[i]
            citations.append({**selection, 'quote': finding['quote'], 'start': finding['start'],
                'end': finding['end'], 'source_url': audit.get('source_url'),
                'category_label': CATEGORIES.get(finding['category'], finding['category']),
                'stage_label': STAGES.get(finding['stage'], finding['stage']),
                'calculation_inputs': finding.get('calculation_inputs'),
                'points_before_dedup': finding.get('points_before_dedup'),
                'selected_for_score': (selection['provisional_selected'] and result['provisional_score'] >= result['validated_score'])
                    or (selection['validated_selected'] and result['validated_score'] >= result['provisional_score']),
                'role_in_calculation': 'resolution' if selection['resolution_applied'] else
                    'selected_maximum' if selection['provisional_selected'] or selection['validated_selected'] else 'support_only_not_added'})
        factors.append({**component, 'score_without_event': without,
            'score_delta_when_removed': result['score'] - without,
            'title': ' / '.join(sorted({c['category_label'] for c in citations})) or component['event_group'],
            'evidence': citations})
    factors.sort(key=lambda f: (-f['score_delta_when_removed'], -max(f['provisional_points'], f['validated_points']), f['event_group']))
    exclusions = Counter()
    for audit in audits.values():
        exclusions.update(audit.get('exclusions', []))
        for finding in audit.get('findings', []):
            exclusions.update(finding.get('exclusions', []))
    warnings = list(result.get('warnings', []))
    if result.get('review_required'):
        warnings.append('Hay documentos pendientes de revisión; el resultado conserva su carácter provisional.')
    if not result.get('validated_score'):
        warnings.append('No hay una contribución positiva validada por revisión humana.')
    if any(c.get('resolution_review_required') for c in components):
        warnings.append('Existe una resolución exculpatoria pendiente de revisión; consulte ambas ramas.')
    if any('|unresolved:' in c['event_group'] for c in components):
        warnings.append('Algunos eventos se agrupan por categoría y trimestre; ese agrupamiento no verifica que sean el mismo hecho.')
    if not result['score']:
        warnings.append('Cero no acredita ausencia de riesgo. Consulte cobertura, exclusiones y evidencia insuficiente.')
    auxiliary = result.get('contextual_analysis', [])
    roles = {}
    for item in auxiliary:
        roles.setdefault(item['analysis'].get('role', 'insufficient'), []).append(item['evidence_id'])
        role = item['analysis'].get('role')
        audit = audits.get(item['evidence_id'], {})
        if role in {'victim', 'complainant', 'incidental', 'negated', 'indirect', 'resolved'} and any(
                finding.get('points_before_dedup', 0) > 0 and not finding.get('exclusions') for finding in audit.get('findings', [])):
            warnings.append(f"{item['evidence_id']}: el scorer detectó una señal puntuable y el clasificador auxiliar sugiere otro papel contextual ({role}). Posible falso positivo: requiere revisión; el score no se modificó.")
    if 'accused' in roles and ('resolved' in roles or 'negated' in roles):
        warnings.append('Los análisis auxiliares muestran acusaciones y resoluciones o negativas en notas distintas. No se ha establecido que correspondan al mismo evento ni su cronología.')
    return {'version': VERSION, 'interpretation': {
        'title': f"Prioridad documental {result['label']}", 'score': result['score'],
        'meaning': 'Prioridad heurística de revisión documental; no es probabilidad ni culpabilidad.',
        'scale': {'minimum': 0, 'maximum': 95, 'display_denominator': 100},
        'ranges': [{'min': 0, 'max': 0, 'label': 'sin señales puntuables'},
            {'min': 1, 'max': 19, 'label': 'baja'}, {'min': 20, 'max': 49, 'label': 'media'},
            {'min': 50, 'max': 95, 'label': 'alta'}],
        'comparison': '85 significa prioridad alta y 40, media. No equivalen a 85 % y 40 % de probabilidad ni a una razón de riesgos.',
        'branches': 'El resultado es el máximo de las ramas provisional y validada; no se suman.',
        'as_of': result.get('as_of')},
        'factors': factors, 'warnings': list(dict.fromkeys(warnings)),
        'exclusions': [{'code': code, 'label': EXCLUSIONS.get(code, code.replace('_', ' ')), 'count': count}
            for code, count in sorted(exclusions.items())],
        'coverage': {'documents': len(audits), 'event_groups': len(components),
            'excluded_documents': sum(bool(a.get('exclusions')) for a in audits.values()),
            'reviewed_documents': sum(bool(a.get('validated')) for a in audits.values())},
        'limitations': ['Las señales describen lo reportado por las fuentes, sin verificar por sí mismas los hechos.',
            'Los cambios al retirar eventos no son sumables: la agregación es no lineal, redondeada y limitada a 95.',
            'Las interpretaciones de IA/NLP son auxiliares: nunca se presentan como factores numéricos del score.',
            'La revisión humana documenta una comprobación; no convierte una acusación en hecho probado.']}
