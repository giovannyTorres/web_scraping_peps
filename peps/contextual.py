# SPDX-License-Identifier: MIT
"""Offline orchestration of auxiliary contextual analysis, isolated from scoring.

The default imports no statistical NLP dependency. Identity restrictions are
checked before inference; selected mentions cannot borrow another homonym's role.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import json
from typing import Any

from . import context_model
from . import identity
from .identity import entity_for_analysis, entity_signature
from .nlp import _mentions, _sentences

MODEL_VERSION = "contextual-orchestrator-1.0.0"
MODES = ("discriminative", "advanced", "hybrid", "enriched")
CONFIG = {"max_input_chars": 60000, "max_identity_mentions": 256, "max_segments": 48,
    "identity_policy": "current_resolution_or_current_named_human_review_selected_mentions",
    "mixed_selected_homonym_clause": "exclude_clause",
    "hybrid_policy": "both_available_nonabstaining_same_role",
    "enriched_policy": "discriminative_role_plus_optional_advanced_extraction_no_vote",
    "score_effect": "auxiliary_only", "version": MODEL_VERSION}
CONFIG_HASH = hashlib.sha256(json.dumps(CONFIG, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
BASE_LIMITATIONS = [
    "Análisis contextual auxiliar: no modifica el score ni constituye un factor de su cálculo.",
    "Las citas acreditan el texto de entrada, no la veracidad de los hechos ni la corrección de una hipótesis del modelo.",
    "La selección de menciones restringe el análisis a sus cláusulas; no resuelve correferencias ni cronologías generales.",
]


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        default=str, separators=(",", ":")).encode()).hexdigest()


def _advanced_module():
    # This module, and therefore spaCy/model loading, is unreachable in default mode.
    return importlib.import_module(".advanced_nlp", __package__)


def _component_info(name: str) -> dict[str, Any]:
    try:
        if name == "discriminative":
            info = context_model.get_model_info()
            return {**info, "available": True, "model_hash": info["artifact_sha256"]}
        info = _advanced_module().get_model_info()
        if not isinstance(info, dict):
            raise ValueError("Invalid optional method metadata")
        return {**info, "available": bool(info.get("available"))}
    except Exception as exc:
        return {"available": False, "error_type": type(exc).__name__,
            "error": "Componente contextual no disponible; no se descarga ni instala automáticamente."}


def _identity_method_info() -> dict[str, str]:
    return {"model_version": identity.MODEL_VERSION, "rules_hash": identity.RULES_HASH}


def _method_config_hash(mode: str, signatures: dict[str, Any]) -> str:
    return _canonical_hash({"orchestrator": CONFIG_HASH, "identity": _identity_method_info(), "components": signatures, "mode": mode})


def get_method_info(mode: str = "discriminative") -> dict[str, Any]:
    """Return isolated configuration/model metadata, never cached mutable objects."""
    if mode not in MODES:
        return {"method": mode, "available": False, "model_version": MODEL_VERSION,
            "config_hash": CONFIG_HASH, "error": "Modo de análisis contextual no reconocido.",
            "score_effect": "auxiliary_only", "config": deepcopy(CONFIG), "components": {}}
    names = ["discriminative"] if mode == "discriminative" else ["advanced"] if mode == "advanced" else ["discriminative", "advanced"]
    components = {name: _component_info(name) for name in names}
    signatures = {name: {key: info.get(key) for key in
        ("available", "model_version", "model_hash", "config_hash", "runtime_config_hash")}
        for name, info in components.items()}
    available = (components["discriminative"]["available"] if mode == "enriched"
        else all(info["available"] for info in components.values()))
    return deepcopy({"method": mode, "available": bool(available), "model_version": MODEL_VERSION,
        "model_hash": _canonical_hash({name: info.get("model_hash") for name, info in components.items()}),
        "config_hash": _method_config_hash(mode, signatures),
        "identity_method": _identity_method_info(),
        "orchestrator_config_hash": CONFIG_HASH, "config": CONFIG, "components": components,
        "score_effect": "auxiliary_only"})


def _empty(mode: str, reason: str, *, error_type: str | None = None) -> dict[str, Any]:
    result = {"method": mode, "model_version": MODEL_VERSION,
        "config_hash": CONFIG_HASH, "available": False, "role": "insufficient",
        "predicted_role": "insufficient", "relevance": "insufficient", "abstained": True,
        "decision_margin": 0.0, "findings": [], "mentions": [], "entities": [],
        "relations": [], "events": [], "competing_roles": [], "warnings": [reason],
        "limitations": list(BASE_LIMITATIONS), "score_effect": "auxiliary_only"}
    if error_type:
        result["error_type"] = error_type
    return result


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {key: deepcopy(result.get(key)) for key in ("available", "method", "model_version",
        "model_hash", "config_hash", "runtime_config_hash", "role", "predicted_role", "abstained", "decision_margin")}


def _run_component(name: str, text: str, entity: dict[str, Any], source_kind: str) -> dict[str, Any]:
    try:
        function = context_model.analyze if name == "discriminative" else _advanced_module().analyze
        result = deepcopy(function(text, entity, source_kind))
        result.setdefault("available", True)
        for key in ("entities", "relations", "events", "warnings", "limitations", "findings", "mentions"):
            result.setdefault(key, [])
        return result
    except Exception as exc:
        return _empty(name, "El componente contextual falló; no se atribuyen conclusiones y el scoring permanece independiente.",
            error_type=type(exc).__name__)


def _run_method(mode: str, text: str, entity: dict[str, Any], source_kind: str) -> dict[str, Any]:
    if mode in {"discriminative", "advanced"}:
        result = _run_component(mode, text, entity, source_kind)
        return {**result, "component_results": {mode: _summary(result)}, "_inference_attempted": True}
    discriminative = _run_component("discriminative", text, entity, source_kind)
    advanced = _run_component("advanced", text, entity, source_kind)
    result = deepcopy(discriminative)
    result.update(method=mode, component_results={"discriminative": _summary(discriminative), "advanced": _summary(advanced)}, _inference_attempted=True)
    for key in ("entities", "relations", "events"):
        result[key] = deepcopy(advanced.get(key, [])) if advanced.get("available") else []
    result["limitations"] = list(dict.fromkeys([*result["limitations"], *advanced["limitations"]]))
    result["warnings"] = list(dict.fromkeys([*result["warnings"], *advanced["warnings"]]))
    if not advanced.get("available"):
        result["warnings"].append("NLP opcional no disponible; no se añadieron entidades, relaciones ni eventos.")
    elif discriminative.get("role") != advanced.get("role") or bool(discriminative.get("abstained")) != bool(advanced.get("abstained")):
        result["warnings"].append("Desacuerdo entre clasificador y NLP: las extracciones son hipótesis auxiliares; revise los fragmentos.")
    if mode == "enriched":
        result["role_policy"] = CONFIG["enriched_policy"]
        result["warnings"].append("El papel final procede del clasificador discriminativo; NLP sólo añade extracciones y no vota ni modifica el score.")
    else:
        agreed = (discriminative.get("available") and advanced.get("available")
            and not discriminative.get("abstained") and not advanced.get("abstained")
            and discriminative.get("role") == advanced.get("role"))
        result["role_policy"] = CONFIG["hybrid_policy"]
        result["available"] = bool(discriminative.get("available") and advanced.get("available"))
        if not agreed:
            result.update(role="insufficient", relevance="insufficient", abstained=True)
            result["warnings"].append("Modo híbrido experimental: sin acuerdo de ambos componentes se abstiene.")
    return result


def _valid_span(value: Any, text: str) -> bool:
    return (isinstance(value, dict) and type(value.get("start")) is int and type(value.get("end")) is int
        and 0 <= value["start"] < value["end"] <= len(text)
        and isinstance(value.get("text"), str) and text[value["start"]:value["end"]] == value["text"])


def _identity_scope(entity: dict[str, Any], item: dict[str, Any], text: str, digest: str):
    """Return (entity, selected mentions or None, all identity mentions, status, error)."""
    metadata = item.get("metadata") or {}
    resolution = item.get("identity_resolution")
    review = item.get("review") or {}
    if not isinstance(review, dict):
        return entity, None, [], "invalid", "Metadatos de revisión inválidos."
    if not (metadata.get("news_module") or resolution is not None):
        return entity, None, [], "unverified", None
    if not isinstance(resolution, dict):
        return entity, None, [], "unresolved", "Identidad de noticia pendiente de atribución; no se infiere un papel para un posible homónimo."
    signature = entity_signature(entity)
    features = resolution.get("features") or {}
    current = (isinstance(features, dict) and resolution.get("entity_id") == entity.get("id")
        and resolution.get("content_hash") == digest and resolution.get("entity_signature") == signature
        and resolution.get("model_version") == identity.MODEL_VERSION and features.get("rules_hash") == identity.RULES_HASH
        and (item.get("identity_bank_revision") is None or resolution.get("bank_revision") == item["identity_bank_revision"]))
    if not current:
        return entity, None, [], "stale", "Resolución de identidad obsoleta, incompleta o incompatible con el texto y la entidad actuales."
    raw_mentions = resolution.get("mentions", [])
    if not isinstance(raw_mentions, list) or len(raw_mentions) > CONFIG["max_identity_mentions"]:
        return entity, None, [], "invalid", "Presupuesto o formato de menciones de identidad inválido."
    mentions = [deepcopy(mention) for mention in raw_mentions if _valid_span(mention, text)]
    selected = [mention for mention in mentions if mention.get("identity_supported") is True]
    confirmed = (review.get("status") == "confirmed" and bool(str(review.get("actor") or "").strip())
        and bool(str(review.get("reason") or "").strip()) and review.get("content_hash") == digest
        and review.get("entity_signature") == signature and review.get("identity_rules_hash") == identity.RULES_HASH
        and (item.get("identity_bank_revision") is None or review.get("bank_revision") == item["identity_bank_revision"]))
    if confirmed and review.get("identity_spans"):
        spans = review["identity_spans"]
        if not isinstance(spans, list) or len(spans) > CONFIG["max_identity_mentions"]:
            return entity, None, [], "invalid", "Selección humana de menciones inválida."
        allowed = {(span["start"], span["end"]) for span in spans if _valid_span(span, text)}
        selected = [mention for mention in mentions if (mention["start"], mention["end"]) in allowed]
    elif confirmed and len(mentions) == 1:
        selected = mentions
    if (resolution.get("status") != "matched" and not confirmed) or not selected:
        return entity, None, [], "unresolved", "Identidad ambigua o sin menciones corroboradas/seleccionadas; no se infiere un papel contextual."
    scoped_entity = entity_for_analysis(entity, resolution)
    scoped_entity["aliases"] = list(dict.fromkeys([*scoped_entity.get("aliases", []), *(span["text"] for span in selected)]))
    return scoped_entity, selected, mentions, "human_selected" if confirmed else "matched_supported", None


def _lift(value: Any, offset: int) -> Any:
    """Translate all nested source offsets; token/event/dependency spans remain exact."""
    if isinstance(value, list):
        return [_lift(item, offset) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _lift(item, offset) for key, item in value.items()}
    for key in ("start", "end", "trigger_start", "trigger_end", "evidence_start", "evidence_end"):
        if type(value.get(key)) is int:
            result[key] = value[key] + offset
    return result


def _unique(values: list[Any]) -> list[Any]:
    seen, result = set(), []
    for value in values:
        signature = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        if signature not in seen:
            seen.add(signature)
            result.append(value)
    return result


def _merge(results: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    if not results:
        return _empty(mode, "No hay cláusulas elegibles vinculadas a las menciones seleccionadas.")
    result = deepcopy(results[0])
    for key in ("findings", "mentions", "entities", "relations", "events", "warnings", "limitations"):
        result[key] = _unique([value for item in results for value in item.get(key, [])])
    active_roles = sorted({item["role"] for item in results if not item.get("abstained")})
    result["competing_roles"] = active_roles
    if len(active_roles) != 1 or any(item.get("abstained") for item in results):
        result.update(role="insufficient", relevance="insufficient", abstained=True)
        if len(active_roles) > 1:
            result["warnings"].append("Las cláusulas seleccionadas tienen papeles distintos; requiere revisión temporal y contextual.")
    best = max(results, key=lambda item: item.get("decision_margin") or 0.0)
    result["decision_margin"] = best.get("decision_margin", 0.0)
    result["predicted_role"] = best.get("predicted_role", "insufficient")
    result["available"] = all(item.get("available", True) for item in results)
    names = sorted({name for item in results for name in item.get("component_results", {})})
    result["component_results"] = {}
    for name in names:
        parts = [item["component_results"][name] for item in results if name in item.get("component_results", {})]
        combined = deepcopy(parts[0])
        roles = sorted({part.get("role", "insufficient") for part in parts})
        combined["roles_observed"] = roles
        combined["available"] = all(part.get("available") for part in parts)
        combined["abstained"] = len(roles) != 1 or any(part.get("abstained") for part in parts)
        combined["role"] = "insufficient" if combined["abstained"] else roles[0]
        result["component_results"][name] = combined
    # Per-clause decisions share model/config metadata in component_results.
    result["segment_decisions"] = [{"start": min((finding["start"] for finding in item.get("findings", [])), default=None),
        "end": max((finding["end"] for finding in item.get("findings", [])), default=None),
        "components": {name: {key: info.get(key) for key in ("role", "abstained", "decision_margin")}
            for name, info in item.get("component_results", {}).items()}} for item in results]
    return result


def _analyze_scoped(mode: str, text: str, entity: dict[str, Any], source_kind: str,
                    selected: list[dict[str, Any]], identity_mentions: list[dict[str, Any]]) -> dict[str, Any]:
    if context_model._mention_budget_exceeded(text, entity):
        return _empty(mode, "Presupuesto de menciones o alias excedido; no se ejecutó inferencia contextual.")
    mentions = _unique([*_mentions(text, entity), *identity_mentions])
    selected_keys = {(mention["start"], mention["end"]) for mention in selected}
    results, excluded = [], 0
    exhausted = False
    for start, end, quote in _sentences(text, mentions):
        local = [mention for mention in mentions if start <= mention["start"] and mention["end"] <= end]
        selected_here = [mention for mention in local if (mention["start"], mention["end"]) in selected_keys]
        if not selected_here:
            continue
        if any((mention["start"], mention["end"]) not in selected_keys for mention in local):
            excluded += 1
            continue
        if len(results) >= CONFIG["max_segments"]:
            exhausted = True
            break
        analysis = _run_method(mode, quote, entity, source_kind)
        analysis = _lift(analysis, start)
        # Only current selected mentions may appear as attributed targets.
        analysis["mentions"] = [mention for mention in analysis.get("mentions", [])
            if (mention.get("start"), mention.get("end")) in selected_keys]
        analysis["findings"] = [finding for finding in analysis.get("findings", [])
            if any((mention.get("start"), mention.get("end")) in selected_keys for mention in finding.get("mentions", []))]
        if not analysis["findings"] or not analysis["mentions"]:
            analysis.update(role="insufficient", relevance="insufficient", abstained=True)
            analysis["warnings"].append("Sin fragmentos verificables ligados a la mención seleccionada; no se atribuye un papel.")
        results.append(analysis)
    result = _merge(results, mode)
    if excluded:
        result.update(role="insufficient", relevance="insufficient", abstained=True)
        result["warnings"].append("Se excluyeron cláusulas con una mención seleccionada y otra homónima no seleccionada para evitar transferirle su papel.")
        result["excluded_mixed_identity_clauses"] = excluded
    if exhausted:
        result.update(role="insufficient", relevance="insufficient", abstained=True, truncated=True)
        result["warnings"].append("Presupuesto de cláusulas seleccionadas excedido; se requiere revisar el texto completo.")
    result["_inference_attempted"] = bool(results)
    result["selected_identity_spans"] = [{"start": span["start"], "end": span["end"], "text": span["text"]} for span in selected]
    return result


def _validate_output(result: dict[str, Any], text: str) -> None:
    """Do not publish a component's invented quote or out-of-range source span."""
    def walk(value: Any):
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            if "start" in value or "end" in value:
                if type(value.get("start")) is not int or type(value.get("end")) is not int or not 0 <= value["start"] < value["end"] <= len(text):
                    raise ValueError("Invalid contextual source span")
                fragment = value.get("quote", value.get("text"))
                if fragment is not None and text[value["start"]:value["end"]] != fragment:
                    raise ValueError("Contextual source quote mismatch")
            for item in value.values():
                walk(item)
    for key in ("findings", "mentions", "entities", "relations", "events", "selected_identity_spans"):
        walk(result.get(key, []))


def analyze_evidence(entity: dict[str, Any], item: dict[str, Any], mode: str = "discriminative") -> dict[str, Any]:
    """Return reviewable auxiliary results without allowing failures into scoring."""
    evidence_id = str(item.get("id", item.get("evidence_id", ""))) if isinstance(item, dict) else ""
    text = str(item.get("text") or "") if isinstance(item, dict) else ""
    digest = None
    source_kind = str(item.get("source_kind", "unknown")) if isinstance(item, dict) else "unknown"
    identity_status, inferred = "unverified", False
    try:
        digest = hashlib.sha256(text.encode()).hexdigest()
        if not isinstance(entity, dict) or not isinstance(item, dict):
            raise ValueError("Invalid contextual input")
        metadata, review = item.get("metadata") or {}, item.get("review") or {}
        reason = None
        if mode not in MODES:
            reason = "Modo de análisis contextual no reconocido."
        elif not isinstance(metadata, dict) or not isinstance(review, dict):
            reason = "Metadatos o revisión de evidencia inválidos."
        elif source_kind == "discovery" or metadata.get("discovery_only"):
            reason = "Índice de descubrimiento: no es cuerpo de nota y no se ejecuta inferencia."
        elif source_kind == "legacy" or metadata.get("requires_source_revalidation"):
            reason = "Fuente heredada pendiente de revalidación: no se ejecuta inferencia."
        elif review.get("status") == "rejected":
            reason = "La revisión humana rechazó esta evidencia; no se ejecuta inferencia."
        elif not text.strip():
            reason = "No existe cuerpo de nota verificable para el análisis."
        elif item.get("content_hash") and item["content_hash"] != digest:
            reason = "El hash de la nota no corresponde al texto actual; no se ejecuta inferencia."
        elif len(text) > CONFIG["max_input_chars"]:
            reason = "La nota excede el presupuesto local de entrada; no se ejecuta inferencia y se requiere revisión completa."
        if reason:
            result = _empty(mode, reason)
        else:
            scoped_entity, selected, mentions, identity_status, reason = _identity_scope(entity, item, text, digest)
            if reason:
                result = _empty(mode, reason)
            else:
                if context_model._mention_budget_exceeded(text, scoped_entity):
                    result = _empty(mode, "Presupuesto de menciones o alias excedido; no se ejecutó inferencia contextual.")
                else:
                    result = (_run_method(mode, text, scoped_entity, source_kind) if selected is None
                        else _analyze_scoped(mode, text, scoped_entity, source_kind, selected, mentions))
                    inferred = result.pop("_inference_attempted", False)
                    _validate_output(result, text)
                    if identity_status == "unverified":
                        result["warnings"].append("Sin resolución de identidad: papel nominal auxiliar pendiente de verificar; no acredita identidad ni culpabilidad.")
        result["limitations"] = list(dict.fromkeys([*BASE_LIMITATIONS, *result.get("limitations", [])]))
    except Exception as exc:
        result = _empty(mode, "No pudo completarse el análisis auxiliar; revise la configuración o la nota. El scoring no se modifica.", error_type=type(exc).__name__)
    try:
        signature = entity_signature(entity) if isinstance(entity, dict) else None
    except Exception:
        signature = None
    components = result.get("component_results", {})
    signatures = {name: {key: info.get(key) for key in
        ("available", "model_version", "model_hash", "config_hash", "runtime_config_hash")}
        for name, info in components.items()}
    result.update(method=mode, model_version=MODEL_VERSION,
        model_hash=_canonical_hash({name: info.get("model_hash") for name, info in components.items()}),
        config_hash=_method_config_hash(mode, signatures), identity_method=_identity_method_info(),
        orchestrator_version=MODEL_VERSION, orchestrator_config_hash=CONFIG_HASH,
        source_kind=source_kind, content_hash=digest, entity_signature=signature,
        identity_status=identity_status, inference_attempted=inferred, score_effect="auxiliary_only",
        total_chars=len(text), source_url=item.get("source_url") if isinstance(item, dict) else None)
    return {"evidence_id": evidence_id, "analysis": result}
