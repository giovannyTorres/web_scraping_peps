"""Auditable review-priority scoring, never a probability or a guilt judgment."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any

from .nlp import analyze

MODEL_VERSION = "review-priority-1.1.0"
PARAMETERS = {
    "category_points": {"money_laundering": 38, "organized_crime": 42, "corruption": 34,
        "fraud": 30, "violence": 42, "tax": 26, "public_procurement": 26,
        "sanctions": 38, "reputational": 18},
    "stage_weights": {"allegation": .45, "investigation": .6, "arrest": .75,
        "wanted": .75, "conviction": 1.0, "sanction": .9,
        "acquittal": 0.0, "dismissed": 0.0, "denial": .25},
    "source_weights": {"government": .95, "official": .95, "judicial": .95,
        "news": .7, "ngo": .65, "public_comment": .2, "social": .2, "unknown": .4},
    "identity_floor": .65, "half_life_days": 730, "minimum_age_weight": .35,
    "undated_weight": .5, "maximum_score": 95,
    "uncertainty_weight": .7,
    "unresolved_event_window": "calendar_quarter",
    "exact_content_freshness": "earliest_accepted_publication_or_event_date",
    "aggregation": "100 * (1 - product(1 - event_points / 100)); capped at 95",
    "validation": "review.status=confirmed with nonempty actor and reason; never automatic",
    "news_identity_policy": "Unresolved identity does not score; supported body spans or explicit human-selected spans required.",
}
PARAMETERS_HASH = hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True,
    ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _combine(points: list[float]) -> int:
    return min(PARAMETERS["maximum_score"], max(0, round(100 * (1 - math.prod(1 - max(0, min(95, p)) / 100 for p in points)))))


def _event_key(entity: dict, evidence: dict, finding: dict, published: date | None) -> str:
    category = finding.get("category", "reputational")
    explicit = finding.get("event_id") or evidence.get("event_id")
    if explicit:
        return f"{entity.get('id')}|event:{explicit}"
    quote = finding.get("quote", "")
    docket = re.search(r"\b(?:expediente|carpeta|causa)\s*(?:numero|n[úu]m\.?|no\.?)?\s*([a-z0-9-]+/\d{2,4})", quote, re.I)
    if docket:
        return f"{entity.get('id')}|case:{docket.group(1).lower()}"
    # Deliberately conservative: unverified same-category reports in one quarter
    # are one candidate event. Review can assign distinct event_id values.
    quarter = f"{published.year}-Q{(published.month - 1) // 3 + 1}" if published else "undated"
    return f"{entity.get('id')}|{category}|unresolved:{quarter}"


def score_entity(entity: dict[str, Any], evidence: list[dict[str, Any]],
                 as_of: str | None = None) -> dict[str, Any]:
    """Return provisional/validated review priority and complete contribution audit.

    Rejected evidence, mere family association, negation, and unresolved nominal
    matches do not contribute. A confirmed human review must name its actor and
    reason. Sources with the same content and events are never added repeatedly.
    """
    today = _date(as_of) if as_of else datetime.now(timezone.utc).date()
    if today is None:
        raise ValueError("as_of debe ser una fecha ISO válida (AAAA-MM-DD)")
    audits: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    seen_content: dict[str, str] = {}
    content_events: dict[str, str] = {}
    content_dates: dict[str, date] = {}
    warnings: list[str] = []
    for index, item in enumerate(evidence):
        evidence_id = str(item.get("id", index))
        body = str(item.get("text") or "")
        digest = hashlib.sha256(body.encode()).hexdigest() if body else None
        audit = {"evidence_id": evidence_id, "source_url": item.get("source_url"),
            "source_kind": item.get("source_kind", "unknown"), "published_at": item.get("published_at"),
            "retrieved_at": item.get("retrieved_at"), "content_hash": item.get("content_hash"),
            "computed_content_hash": digest, "review": item.get("review") or {"status": "pending"},
            "exclusions": [], "findings": []}
        audits.append(audit)
        review = item.get("review") or {}
        status = str(review.get("status", "pending"))
        if status == "rejected":
            audit["exclusions"].append("human_rejected")
            continue
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            audit["exclusions"].append("invalid_evidence_metadata")
            continue
        if item.get("source_kind") == "discovery" or metadata.get("discovery_only"):
            audit["exclusions"].append("discovery_index_is_not_article_evidence")
            continue
        if item.get("source_kind") == "legacy" or metadata.get("requires_source_revalidation"):
            audit["exclusions"].append("legacy_source_requires_revalidation")
            continue
        resolution = item.get('identity_resolution')
        confirmed_review = status == 'confirmed' and bool(str(review.get('actor', '')).strip()) and bool(str(review.get('reason', '')).strip())
        analysis_entity, identity_spans = entity, None
        if metadata.get('news_module') or resolution is not None:
            from .identity import MODEL_VERSION as IDENTITY_VERSION, RULES_HASH as IDENTITY_RULES_HASH, entity_signature, entity_for_analysis
            if not isinstance(resolution, dict):
                audit['exclusions'].append('news_identity_not_attributed')
                continue
            audit['identity_resolution'] = resolution
            if confirmed_review and (review.get('content_hash') != digest or review.get('entity_signature') != entity_signature(entity)
                    or review.get('identity_rules_hash') != IDENTITY_RULES_HASH
                    or (item.get('identity_bank_revision') is not None and review.get('bank_revision') != item['identity_bank_revision'])):
                confirmed_review = False
                audit['exclusions'].append('human_identity_review_stale_or_incomplete')
            if (resolution.get('entity_id') != entity.get('id') or resolution.get('content_hash') != digest
                    or resolution.get('entity_signature') != entity_signature(entity)
                    or resolution.get('model_version') != IDENTITY_VERSION
                    or (resolution.get('features') or {}).get('rules_hash') != IDENTITY_RULES_HASH
                    or (item.get('identity_bank_revision') is not None and resolution.get('bank_revision') != item['identity_bank_revision'])):
                audit['exclusions'].append('stale_or_invalid_identity_resolution')
                continue
            mentions = [m for m in resolution.get('mentions', []) if isinstance(m.get('start'), int)
                        and isinstance(m.get('end'), int) and 0 <= m['start'] < m['end'] <= len(body)
                        and body[m['start']:m['end']] == m.get('text')]
            identity_spans = [m for m in mentions if m.get('identity_supported')]
            if confirmed_review and review.get('identity_spans'):
                allowed = {(m['start'], m['end']) for m in review['identity_spans'] if isinstance(m, dict)
                           and isinstance(m.get('start'), int) and isinstance(m.get('end'), int)}
                identity_spans = [m for m in mentions if (m['start'], m['end']) in allowed]
            elif confirmed_review and len(mentions) == 1:
                identity_spans = mentions
            if (resolution.get('status') != 'matched' and not confirmed_review) or not identity_spans:
                audit['exclusions'].append('identity_ambiguous_or_requires_selected_spans')
                continue
            analysis_entity = entity_for_analysis(entity, resolution)
            # A human may select a generated legal-name variant after checking
            # its source. Only those literal body spans are eligible for NLP.
            analysis_entity = dict(analysis_entity, aliases=list(dict.fromkeys([
                *analysis_entity.get('aliases', []), *(m['text'] for m in identity_spans)])))
        if not body.strip():
            audit["exclusions"].append("missing_source_body")
            continue
        if not item.get("source_url"):
            audit["exclusions"].append("missing_source_url")
            continue
        if item.get("content_hash") and item["content_hash"] != digest:
            audit["exclusions"].append("content_hash_mismatch")
            warnings.append(f"{evidence_id}: el hash no corresponde al contenido.")
            continue
        if digest in seen_content:
            audit["duplicate_of"] = seen_content[digest]
        else:
            seen_content[digest] = evidence_id
        canonical = analyze(body, analysis_entity, item.get("source_kind", "unknown"))
        cached = item.get("analysis") or {}
        if not isinstance(cached, dict):
            cached = {}
        compatible = bool(cached) and all(cached.get(key) == canonical.get(key) for key in
            ("entity_signature", "content_hash", "model_version", "rules_hash"))
        analysis = cached if compatible else canonical
        audit["analysis_recomputed"] = not compatible
        audit["analysis_model_version"] = analysis.get("model_version")
        audit["analysis_rules_hash"] = analysis.get("rules_hash")
        audit["entity_signature"] = canonical["entity_signature"]
        # An analysis must contain a literal entity mention backed by current body.
        literal_spans = {(m["start"], m["end"], m["text"]) for m in canonical["mentions"]}
        mention_valid = any(isinstance(m.get("start"), int) and isinstance(m.get("end"), int)
            and 0 <= m["start"] < m["end"] <= len(body)
            and body[m["start"]:m["end"]] == m.get("text")
            and (m["start"], m["end"], m.get("text")) in literal_spans
            and m.get("entity_id") == entity.get("id") for m in analysis.get("mentions", []))
        if not mention_valid:
            audit["exclusions"].append("no_verifiable_entity_mention")
            continue
        raw_confidence = (resolution.get('confidence', 0) if isinstance(resolution, dict) and resolution.get('status') == 'matched'
                          else analysis.get("identity_confidence", 0))
        try:
            raw_confidence = float(raw_confidence)
            identity = max(0., min(1., raw_confidence)) if math.isfinite(raw_confidence) else 0.
        except (TypeError, ValueError):
            identity = 0.
        confirmed = confirmed_review
        if status == "confirmed" and not confirmed:
            warnings.append(f"{evidence_id}: revisión incompleta; se conserva como provisional.")
        if identity < PARAMETERS["identity_floor"] and not confirmed:
            audit["exclusions"].append("ambiguous_identity")
            continue
        published = _date(item.get("published_at"))
        if published and published > today:
            audit["exclusions"].append("future_publication_date")
            continue
        age_days = (today - published).days if published else None
        age_weight = max(PARAMETERS["minimum_age_weight"], 2 ** (-age_days / PARAMETERS["half_life_days"])) if age_days is not None else PARAMETERS["undated_weight"]
        audit.update({"identity_confidence": identity, "validated": confirmed,
            "age_days": age_days, "age_weight": round(age_weight, 6)})
        for finding in analysis.get("findings", []):
            category, stage = finding.get("category"), finding.get("stage")
            result = {"category": category, "stage": stage,
                "relationship": finding.get("relationship"), "negated": finding.get("negated", False),
                "uncertain": finding.get("uncertain", False), "hypothetical": finding.get("hypothetical", False),
                "quote": finding.get("quote", ""), "start": finding.get("start"), "end": finding.get("end"),
                "exclusions": []}
            audit["findings"].append(result)
            start, end = finding.get("start"), finding.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(body) or body[start:end] != finding.get("quote"):
                result["exclusions"].append("unverifiable_quote")
                continue
            if finding.get("negated"):
                result["exclusions"].append("negated")
                continue
            if finding.get("hypothetical"):
                result["exclusions"].append("hypothetical_or_requested_action")
                continue
            if finding.get("relationship") != "direct":
                result["exclusions"].append("association_is_not_adverse_conduct")
                continue
            if identity_spans is not None:
                targets = [m for m in canonical['mentions'] if start <= m['start'] < end]
                trigger = finding.get('trigger_start', start)
                closest = min(targets, key=lambda m: min(abs(trigger - m['start']), abs(trigger - m['end'])), default=None)
                if closest is None or not any(m['start'] == closest['start'] and m['end'] == closest['end'] for m in identity_spans):
                    result['exclusions'].append('finding_outside_corroborated_identity_span')
                    continue
            if category not in PARAMETERS["category_points"] or stage not in PARAMETERS["stage_weights"]:
                result["exclusions"].append("unknown_rule")
                continue
            source = PARAMETERS["source_weights"].get(item.get("source_kind"), PARAMETERS["source_weights"]["unknown"])
            identity_weight = 1. if confirmed else identity
            event_date = _date(item.get("event_date") or finding.get("event_date"))
            if event_date and event_date > today:
                result["exclusions"].append("future_event_date")
                continue
            # Inferred dates can only reduce freshness, never make old material
            # fresh. Ambiguous/multiple dates are left for human review.
            effective_date = min(d for d in (published, event_date) if d) if published or event_date else None
            event_age = (today - effective_date).days if effective_date else None
            event_age_weight = max(PARAMETERS["minimum_age_weight"], 2 ** (-event_age / PARAMETERS["half_life_days"])) if event_age is not None else PARAMETERS["undated_weight"]
            uncertainty_weight = PARAMETERS["uncertainty_weight"] if finding.get("uncertain") else 1.
            base = PARAMETERS["category_points"][category]
            stage_weight = PARAMETERS["stage_weights"][stage]
            # Denials are retained as context and never generate a fresh adverse score.
            points = 0. if stage == "denial" else base * stage_weight * source * identity_weight * event_age_weight * uncertainty_weight
            key = content_events.setdefault(digest, _event_key(entity, item, finding, effective_date))
            if effective_date:
                content_dates[digest] = min(content_dates.get(digest, effective_date), effective_date)
            result.update({"event_group": key, "base_points": base,
                "stage_weight": stage_weight, "source_weight": source,
                "identity_weight": identity_weight, "age_weight": round(event_age_weight, 6),
                "event_date": event_date.isoformat() if event_date else None,
                "effective_date": effective_date.isoformat() if effective_date else None,
                "age_days": event_age, "uncertainty_weight": uncertainty_weight,
                "points_before_dedup": round(points, 6)})
            groups.setdefault(key, []).append({"evidence_id": evidence_id, "points": points,
                "validated": confirmed, "stage": stage, "category": category, "published": effective_date,
                "age_weight": event_age_weight,
                "audit": result, "content_hash": digest, "source_url": item.get("source_url"),
                "publisher_id": metadata.get('publisher_id') or item.get('source_url'),
                "content_cluster": metadata.get('content_cluster') or digest})
    components = []
    for key, members in sorted(groups.items()):
        for member in members:
            oldest_copy = content_dates.get(member["content_hash"])
            if oldest_copy and member["published"] and oldest_copy < member["published"]:
                duplicate_age = (today - oldest_copy).days
                duplicate_age_weight = max(PARAMETERS["minimum_age_weight"], 2 ** (-duplicate_age / PARAMETERS["half_life_days"]))
                member["points"] *= duplicate_age_weight / member["age_weight"]
                member["published"] = oldest_copy
                member["audit"]["deduplicated_effective_date"] = oldest_copy.isoformat()
                member["audit"]["points_after_duplicate_age"] = round(member["points"], 6)
        adverse = [m for m in members if m["points"] > 0]
        resolutions = [m for m in members if m["stage"] in {"acquittal", "dismissed"}]
        latest_by_category = {category: max((m["published"] for m in adverse if m["published"] and m["category"] == category), default=None)
                              for category in {m["category"] for m in members}}
        applicable = [m for m in resolutions if m["published"] and
            (latest_by_category[m["category"]] is None or m["published"] >= latest_by_category[m["category"]])]
        # An unreviewed exculpatory update suspends the provisional score pending
        # review; it cannot erase an already validated adverse record on its own.
        resolved_categories = {m["category"] for m in applicable}
        provisional = max((m["points"] for m in adverse if m["category"] not in resolved_categories), default=0.)
        validated_resolutions = [m for m in applicable if m["validated"]]
        validated_resolved_categories = {m["category"] for m in validated_resolutions}
        validated = max((m["points"] for m in adverse if m["validated"] and m["category"] not in validated_resolved_categories), default=0.)
        components.append({"event_group": key, "evidence_ids": sorted({m["evidence_id"] for m in members}),
            "source_support": {'distinct_publishers': len({m['publisher_id'] for m in members}),
                               'distinct_content_clusters': len({m['content_cluster'] for m in members}),
                               'publisher_ids': sorted({m['publisher_id'] for m in members if m['publisher_id']}),
                               'syndicated_copies_are_not_independent_corroboration': True},
            "provisional_points": round(provisional, 6), "validated_points": round(validated, 6),
            "aggregation": "maximum_per_event", "resolution": applicable[-1]["stage"] if applicable else None,
            "resolution_review_required": bool(applicable and not validated_resolutions)})
        for member in members:
            member["audit"]["event_provisional_points"] = round(provisional, 6)
    provisional_score = _combine([c["provisional_points"] for c in components])
    validated_score = _combine([c["validated_points"] for c in components])
    review_required = any(a.get("review", {}).get("status") != "rejected" and not a.get("validated", False) for a in audits) or any(c["resolution_review_required"] for c in components)
    score = max(provisional_score, validated_score)
    label = "alta" if score >= 50 else "media" if score >= 20 else "baja" if score > 0 else "sin señales puntuables"
    explanation = ["Prioridad de revisión documental; no mide culpabilidad ni probabilidad de delito.",
        f"{len(evidence)} documentos; {len(components)} grupos de eventos conservadores.",
        f"Prioridad provisional: {provisional_score}; prioridad validada por revisión humana: {validated_score}.",
        "Parentesco, mera asociación, negaciones y documentos rechazados no suman puntos.",
        "Un cero no acredita ausencia de riesgo; también puede indicar datos insuficientes."]
    return {"score": score, "label": label, "explanation": explanation,
        "model_version": MODEL_VERSION, "parameters_hash": PARAMETERS_HASH,
        "parameters": PARAMETERS, "as_of": today.isoformat(), "components": components,
        "review_required": review_required, "status": "provisional" if review_required else "validated" if any(not a["exclusions"] for a in audits) else "no_evidence",
        "provisional_score": provisional_score, "validated_score": validated_score,
        "evidence_audit": audits, "warnings": warnings,
        "meaning": "document_review_priority", "confidence_is_probability": False}
