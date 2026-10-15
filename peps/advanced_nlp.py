"""Optional offline Spanish NER/dependency analysis; never a risk-score factor.

The statistical pipeline must be installed explicitly. Import and inference never
download models. Rules interpret dependency paths, rather than keyword proximity.
Quotes attest the input text, not the truth of an allegation or a parser decision.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any

from .nlp import _fold, _mentions

MODEL_NAME = "es_core_news_sm"
MODEL_VERSION = "es-spacy-dependency-1.0.1"
MODEL_WHEEL_SHA256 = "e451a83d6df79b87e9eed0cb553f03e99e36a3bab18a7b79f0dcfd1fdf875e12"
CONFIG = {"max_chars": 80000, "max_findings": 80, "max_dependency_hops": 4,
          "model": MODEL_NAME, "model_version": "3.8.0", "spacy_version": "3.8.11"}
ROLE_RELEVANCE = {"accused": "direct_adverse_candidate", "victim": "victim_context",
    "complainant": "reporter_context", "incidental": "incidental", "negated": "negated_claim",
    "resolved": "reported_resolution", "insufficient": "insufficient", "indirect": "indirect_only"}
ADVERSE = {"acusar", "imputar", "investigar", "detener", "arrestar", "condenar", "sentenciar",
    "sancionar", "inhabilitar", "procesar", "responsabilizar", "vincular", "cometer",
    "defraudar", "estafar", "denunciar", "señalar", "participar", "implicar"}
REPORTING = {"denunciar", "acusar", "informar", "reportar", "alertar", "advertir",
    "interponer", "presentar", "ratificar"}
VICTIM = {"victima", "perjudicar", "afectar", "sufrir", "padecer", "estafar", "defraudar",
    "despojar", "robar", "extorsionar", "agraviado", "perjudicado"}
RESOLUTION = {"absolver", "exonerar", "sobreseer", "desestimar", "archivar", "inocente",
    "absuelto", "absuelta", "absolucion", "sobreseimiento", "exoneracion"}
DENIAL = {"negar", "desmentir", "rechazar", "descartar"}
KIN = {"hermano", "hermana", "padre", "madre", "hijo", "hija", "tio", "tia", "primo",
    "prima", "sobrino", "sobrina", "esposo", "esposa", "socio", "socia", "colaborador",
    "familiar", "filial", "proveedor", "empleado", "abogado"}
EVENT_NOUNS = {"fraude", "corrupcion", "lavado", "estafa", "acusacion", "investigacion",
    "denuncia", "delito", "soborno", "sancion", "sentencia", "absolucion", "evasion"}
RULE_SPEC = {"adverse": sorted(ADVERSE), "reporting": sorted(REPORTING), "victim": sorted(VICTIM),
    "resolution": sorted(RESOLUTION), "denial": sorted(DENIAL), "kin": sorted(KIN),
    "event_nouns": sorted(EVENT_NOUNS), "config": CONFIG, "version": MODEL_VERSION}
CONFIG_HASH = hashlib.sha256(json.dumps(RULE_SPEC, sort_keys=True).encode()).hexdigest()
_LOCK = threading.Lock()
LIMITATIONS = [
    "NER y dependencias estadísticos pueden equivocarse; las relaciones son hipótesis de revisión.",
    "Un alias exacto no verifica identidad ni resuelve homónimos o correferencias.",
    "El modelo pequeño no incluye vectores semánticos; la similitud expuesta es léxica de lemas.",
    "Negaciones, citas atribuidas, cronología y relaciones complejas requieren revisión humana.",
    "Una acusación o una negativa reportada no acreditan culpabilidad ni inocencia.",
    "Este análisis auxiliar no altera ni reconstruye los factores numéricos del scoring.",
]


@lru_cache(maxsize=1)
def _pipeline():
    try:
        import spacy
        nlp = spacy.load(MODEL_NAME)
        if spacy.__version__ != CONFIG["spacy_version"] or nlp.meta.get("version") != CONFIG["model_version"]:
            return None, {"available": False, "error": "Versiones NLP distintas de las fijadas; reinstale requirements-nlp.lock.txt y el modelo verificado."}
        if not {"parser", "ner", "lemmatizer"}.issubset(nlp.pipe_names):
            return None, {"available": False, "error": "El modelo local no incluye parser, NER y lematizador requeridos."}
        nlp.max_length = CONFIG["max_chars"] + 1
        digest = hashlib.sha256()
        base = Path(nlp.path)
        for file in sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
            digest.update(file.relative_to(base).as_posix().encode())
            digest.update(file.read_bytes())
        return nlp, {"available": True, "spacy_version": spacy.__version__,
            "pipeline_version": nlp.meta["version"], "pipeline": list(nlp.pipe_names),
            "license": nlp.meta.get("license"), "model_hash": digest.hexdigest(),
            "model_wheel_sha256": MODEL_WHEEL_SHA256,
            "model_bytes": sum(p.stat().st_size for p in base.rglob("*") if p.is_file()),
            "vectors": nlp.meta.get("vectors", {})}
    except (ImportError, OSError) as exc:
        return None, {"available": False, "error": "NLP opcional no instalado; ejecute instalación explícita documentada en docs/NLP_LOCAL.md.",
                      "error_type": type(exc).__name__}


def get_model_info() -> dict[str, Any]:
    _, info = _pipeline()
    return deepcopy({**info, "method": "spacy", "model_version": MODEL_VERSION,
            "config_hash": CONFIG_HASH, "config": dict(CONFIG)})


def _lemma(token) -> str:
    return _fold(token.lemma_ or token.text)


def _span(span, label: str | None = None) -> dict[str, Any]:
    result = {"text": span.text, "start": span.start_char, "end": span.end_char}
    if label:
        result["label"] = label
    return result


def _token_span(token) -> dict[str, Any]:
    return {"text": token.text, "start": token.idx, "end": token.idx + len(token.text),
            "lemma": _lemma(token)}


def _path(target, predicate):
    path = [target]
    while path[-1] != predicate and len(path) <= CONFIG["max_dependency_hops"]:
        if path[-1].head == path[-1]:
            return None
        path.append(path[-1].head)
    return path if path[-1] == predicate else None


def _hypothetical(predicate) -> bool:
    # Conditional/subjunctive morphology alone does not negate an allegation.
    # A conditional protasis ("si ...") explicitly makes this event hypothetical.
    return any(_lemma(child) == "si" and child.dep_ == "mark" for child in predicate.children)


def _negated(predicate) -> bool:
    children = list(predicate.children)
    return any(_lemma(t) in {"no", "nunca", "jamas"} and t.dep_ == "advmod"
               and not any(_lemma(c) in {"solo", "solamente"} for c in t.children)
               for t in children)


def _passive(predicate) -> bool:
    return "Part" in predicate.morph.get("VerbForm") and any(
        _lemma(c) == "ser" and c.dep_.startswith("aux") for c in predicate.children)


def _unresolved_negation_scope(predicate) -> str | None:
    """Negated acquittal or denial is neither a resolution nor a new allegation.

    Include a finite complement ("No se descartó que X cometiera ...") without
    donating negation across unrelated coordinated clauses. This deliberately
    abstains instead of applying classical double-negation to reported speech.
    """
    cursor = predicate
    for _ in range(CONFIG["max_dependency_hops"]):
        lemma = _lemma(cursor)
        if _negated(cursor) and lemma in RESOLUTION | DENIAL:
            return ("negated_resolution_does_not_establish_adverse_fact" if lemma in RESOLUTION
                    else "negated_denial_does_not_establish_adverse_fact")
        if cursor.dep_ not in {"ccomp", "csubj", "xcomp", "acl"} or cursor.head == cursor:
            break
        cursor = cursor.head
    return None


def _classify(target, sentence) -> tuple[str, list[dict[str, Any]]]:
    candidates = []
    for predicate in sentence:
        lemma = _lemma(predicate)
        if lemma not in ADVERSE | REPORTING | VICTIM | RESOLUTION | DENIAL | EVENT_NOUNS:
            continue
        path = _path(target, predicate)
        if not path or len(path) < 2:
            continue
        edge = path[-2]
        indirect = any(_lemma(t) in KIN for t in path[1:-1])
        subject = edge.dep_.startswith("nsubj")
        argument = edge.dep_ in {"obj", "iobj", "obl", "nsubj", "nsubj:pass"}
        passive = _passive(predicate)
        unresolved_negation = _unresolved_negation_scope(predicate)
        role, status = "insufficient", "unresolved_dependency"
        if unresolved_negation:
            role, status = "insufficient", unresolved_negation
        elif indirect:
            role, status = "indirect", "third_party_event"
        elif not argument:
            continue
        elif _hypothetical(predicate):
            role, status = "insufficient", "hypothetical_event"
        elif lemma in RESOLUTION:
            # Active authority subject ("Fiscalía exoneró a X") is not exonerated.
            if not subject or passive or predicate.pos_ in {"ADJ", "NOUN"}:
                role, status = "resolved", "reported_resolution_not_verified"
        elif lemma in DENIAL:
            role, status = "negated", "reported_denial_not_exoneration"
        elif _negated(predicate):
            role, status = "negated", "negated_event"
        elif lemma in VICTIM and (lemma == "victima" or (subject and not passive and lemma in {"sufrir", "padecer"})
                or (not subject and lemma in {"perjudicar", "afectar", "estafar", "defraudar", "robar", "extorsionar", "despojar"})
                or passive and subject and lemma in {"estafar", "defraudar", "perjudicar", "afectar", "robar", "extorsionar", "despojar"}):
            role, status = "victim", "reported_victim_context"
        elif lemma in REPORTING and subject and not passive:
            role, status = "complainant", "reported_statement_or_complaint"
        elif lemma in ADVERSE:
            # A direct active subject is the authority for transitive enforcement
            # verbs, and the actor for conduct verbs. Dependency matters here.
            conduct = lemma in {"cometer", "defraudar", "estafar", "participar"}
            if not subject or passive or conduct:
                role = "accused"
                status = "reported_judicial_outcome" if lemma in {"condenar", "sentenciar", "sancionar"} else "reported_allegation_not_proven"
        elif lemma in EVENT_NOUNS and any(_lemma(c) in {"contra"} for c in target.children):
            role, status = "accused", "reported_allegation_not_proven"
        if role != "insufficient" or status == "hypothetical_event" or unresolved_negation:
            candidates.append({"role": role, "status": status, "event": _token_span(predicate),
                "dependency_path": [{**_token_span(t), "dependency": t.dep_} for t in path],
                "argument": edge.dep_, "passive": passive, "negated": _negated(predicate),
                "hypothetical": _hypothetical(predicate)})
    # Use the nearest syntactically connected event. Distinct nearest roles abstain.
    if not candidates:
        return "incidental", []
    distance = min(len(c["dependency_path"]) for c in candidates)
    nearest = [c for c in candidates if len(c["dependency_path"]) == distance]
    roles = {c["role"] for c in nearest}
    return next(iter(roles)) if len(roles) == 1 else "insufficient", candidates


def _lexical_similarity(sentence, context_doc):
    if context_doc is None:
        return None
    a, b = (Counter(_lemma(t) for t in doc if t.is_alpha and not t.is_stop) for doc in (sentence, context_doc))
    denom = math.sqrt(sum(v*v for v in a.values()) * sum(v*v for v in b.values()))
    return round(sum(value * b[key] for key, value in a.items()) / denom, 6) if denom else 0.0


def analyze(text: str, entity: dict[str, Any], source_kind: str = "news") -> dict[str, Any]:
    text = str(text or "")
    nlp, info = _pipeline()
    analyzed_text = text[:CONFIG["max_chars"]]
    truncated = len(text) > CONFIG["max_chars"]
    mentions = _mentions(analyzed_text, entity)
    base = {"method": "spacy", "model_version": MODEL_VERSION,
        "model_hash": info.get("model_hash"), "config_hash": CONFIG_HASH,
        "model_info": deepcopy(info), "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "entity_signature": hashlib.sha256(json.dumps({k: entity.get(k) for k in
            ("id", "name", "kind", "aliases", "context")}, sort_keys=True, ensure_ascii=False, default=str,
            separators=(",", ":")).encode()).hexdigest(),
        "score_effect": "auxiliary_only", "source_kind": source_kind,
        "analyzed_chars": len(analyzed_text), "truncated": truncated,
        "mentions": mentions, "limitations": list(LIMITATIONS)}
    warnings = ["Identidad sin verificar: coincidencia de nombre o alias; posible homónimo."]
    if truncated:
        warnings.append("Presupuesto de texto/menciones excedido: análisis parcial; revise la nota completa.")
    if nlp is None:
        return {**base, "available": False, "role": "insufficient", "predicted_role": "insufficient",
            "relevance": ROLE_RELEVANCE["insufficient"], "abstained": True,
            "findings": [], "entities": [], "relations": [], "events": [],
            "competing_roles": [], "warnings": warnings + [info["error"]]}
    findings, relations, events = [], [], []
    context = entity.get("context") or {}
    context_text = " ".join(str(v) for v in context.values() if v)
    with _LOCK:
        doc = nlp(analyzed_text)
        context_doc = nlp(context_text[:4000]) if context_text else None
    entities = [{**_span(ent, ent.label_), "detector": "statistical_ner"} for ent in doc.ents]
    for mention in mentions:
        span = doc.char_span(mention["start"], mention["end"], alignment_mode="expand")
        if span is None:
            continue
        if len(findings) >= CONFIG["max_findings"]:
            truncated = True
            break
        sentence = span.root.sent
        role, links = _classify(span.root, sentence)
        local_entities = [e for e in entities if sentence.start_char <= e["start"] and e["end"] <= sentence.end_char]
        local_events = []
        for link in links:
            event = {**link["event"], "status": link["status"], "negated": link["negated"],
                "hypothetical": link["hypothetical"], "method": "dependency_rule"}
            if event not in events:
                events.append(event)
            local_events.append(event)
            relation = {"entity_id": entity.get("id"), "mention": dict(mention),
                "event": link["event"], "role": link["role"], "status": link["status"],
                "argument": link["argument"], "passive": link["passive"],
                "dependency_path": link["dependency_path"], "method": "dependency_rule",
                "identity_status": "exact_alias_candidate_not_identity_verification"}
            relations.append(relation)
        findings.append({"role": role, "predicted_role": role, "relevance": ROLE_RELEVANCE[role],
            "abstained": role == "insufficient", "quote": sentence.text,
            "start": sentence.start_char, "end": sentence.end_char,
            "mentions": [dict(mention)], "entities": local_entities, "events": local_events,
            "relations": relations[-len(links):] if links else [],
            "lexical_context_similarity": _lexical_similarity(sentence, context_doc),
            "evidence_status": "exact_input_quote_not_fact_verification"})
    competing = sorted({f["role"] for f in findings if f["role"] not in {"incidental", "insufficient"}})
    role = competing[0] if len(competing) == 1 else "incidental" if findings and not competing else "insufficient"
    if any(f["role"] == "insufficient" for f in findings) or truncated:
        role = "insufficient"
    if len(competing) > 1:
        warnings.append("Papeles distintos entre fragmentos; posible contradicción o cambio temporal sin resolver.")
    if any(event["status"] in {"negated_resolution_does_not_establish_adverse_fact",
            "negated_denial_does_not_establish_adverse_fact"} for event in events):
        warnings.append("Negación de una resolución o de una negativa: se requiere revisión; no acredita absolución ni constituye un nuevo hecho adverso.")
    if truncated and not base["truncated"]:
        warnings.append("Presupuesto de texto/menciones excedido: análisis parcial; revise la nota completa.")
    if not findings:
        warnings.append("No hay mención literal completa del nombre o alias dentro del texto analizado.")
    return {**base, "truncated": truncated, "available": True, "role": role, "predicted_role": role,
        "relevance": ROLE_RELEVANCE[role], "abstained": role == "insufficient",
        "competing_roles": competing, "findings": findings, "entities": entities,
        "relations": relations, "events": events, "warnings": warnings}
