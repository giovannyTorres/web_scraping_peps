# SPDX-License-Identifier: MIT
"""Small discriminative Spanish role classifier; inference uses only stdlib.

The checked-in artifact is trained on synthetic examples, not validated news.
Predictions are auxiliary hypotheses and never contribute to the scoring formula.
No pickle, networking, model downloads, NER or generative inference is used here.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .nlp import _fold, _mentions, _sentences

MODEL_PATH = Path(__file__).parent / "models" / "context-role-es-v1.json"
MODEL_VERSION = "es-context-discriminative-1.0.0"
EXPECTED_ARTIFACT_SHA256 = "ba227bc782936a1e19901f8a1464618e160a5e525593c71affd1705f45a8f563"
RUNTIME_LIMITS = {"max_input_chars": 60000, "max_mention_candidates": 128,
                  "max_aliases": 32, "max_name_chars": 512}
RUNTIME_CONFIG_HASH = hashlib.sha256(json.dumps(RUNTIME_LIMITS, sort_keys=True,
    separators=(",", ":")).encode()).hexdigest()
ROLE_RELEVANCE = {"accused": "direct_adverse_candidate", "victim": "victim_context",
    "complainant": "reporter_context", "incidental": "incidental", "negated": "negated_claim",
    "resolved": "reported_resolution", "insufficient": "insufficient", "indirect": "indirect_only"}
LIMITATIONS = [
    "Clasificador entrenado con ejemplos sintéticos; requiere validación con notas revisadas del proyecto.",
    "El margen es diferencia entre funciones de decisión; no es probabilidad ni confianza calibrada.",
    "La coincidencia del nombre no verifica identidad ni resuelve homónimos o correferencias.",
    "La cita acredita el texto de entrada; no demuestra que la interpretación del clasificador sea correcta.",
    "Las decisiones son auxiliares y no modifican ni explican contribuciones del scoring existente.",
]


def normalize_context(value: str) -> str:
    """Shared deterministic preprocessing used by training and stdlib inference."""
    return re.sub(r"\s+", " ", _fold(value)).strip()


def _ngrams(value: str, branch: dict[str, Any]) -> Counter:
    lo, hi = branch["ngram_range"]
    if branch["analyzer"] == "word":
        units = re.findall(r"\b\w\w+\b", value)
        return Counter(" ".join(units[i:i+n]) for n in range(lo, hi+1)
                       for i in range(len(units)-n+1))
    return Counter(value[i:i+n] for n in range(lo, hi+1)
                   for i in range(len(value)-n+1))


def _validate_model(model: dict[str, Any]) -> None:
    """Fail closed on invalid dimensions or non-finite numeric model content."""
    def finite(value, maximum=1000.0, positive=False):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and abs(value) <= maximum and (not positive or value > 0))
    if not isinstance(model, dict) or model.get("model_version") != MODEL_VERSION:
        raise ValueError("Unsupported discriminative model version")
    classes = model.get("classes")
    if classes != sorted(ROLE_RELEVANCE):
        raise ValueError("Invalid discriminative model classes")
    intercept = model.get("intercept")
    if not isinstance(intercept, list) or len(intercept) != len(classes) or not all(finite(v) for v in intercept):
        raise ValueError("Invalid discriminative model intercept")
    config = model.get("config")
    if not isinstance(config, dict):
        raise ValueError("Missing discriminative model configuration")
    if not finite(config.get("minimum_margin"), maximum=20.0) or config["minimum_margin"] < 0:
        raise ValueError("Invalid discriminative decision threshold")
    if not isinstance(config.get("max_clauses"), int) or not 1 <= config["max_clauses"] <= 128:
        raise ValueError("Invalid clause budget")
    if not isinstance(config.get("max_clause_chars"), int) or not 1 <= config["max_clause_chars"] <= 8192:
        raise ValueError("Invalid character budget")
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(encoded).hexdigest() != model.get("config_hash"):
        raise ValueError("Discriminative configuration hash mismatch")
    branches = model.get("branches")
    if not isinstance(branches, list) or len(branches) != 2:
        raise ValueError("Invalid discriminative feature branches")
    for analyzer, branch in zip(("word", "char"), branches):
        if not isinstance(branch, dict) or branch.get("analyzer") != analyzer:
            raise ValueError("Invalid feature analyzer")
        if branch.get("ngram_range") != ([1, 3] if analyzer == "word" else [3, 5]):
            raise ValueError("Invalid feature ngram range")
        if not finite(branch.get("weight"), maximum=2.0, positive=True):
            raise ValueError("Invalid feature weight")
        vocabulary, idf, coef = branch.get("vocabulary"), branch.get("idf"), branch.get("coef")
        if (not isinstance(vocabulary, list) or not 1 <= len(vocabulary) <= 20000
                or any(not isinstance(term, str) or not 1 <= len(term) <= 200 for term in vocabulary)
                or len(set(vocabulary)) != len(vocabulary)):
            raise ValueError("Invalid feature vocabulary")
        if not isinstance(idf, list) or len(idf) != len(vocabulary) or not all(finite(v, positive=True) for v in idf):
            raise ValueError("Invalid feature IDF dimensions or values")
        if (not isinstance(coef, list) or len(coef) != len(classes)
                or any(not isinstance(row, list) or len(row) != len(vocabulary)
                       or not all(finite(v) for v in row) for row in coef)):
            raise ValueError("Invalid coefficient dimensions or values")


@lru_cache(maxsize=1)
def _load_model() -> dict[str, Any]:
    if not 0 < MODEL_PATH.stat().st_size <= 3_000_000:
        raise ValueError("Discriminative artifact exceeds size limit")
    raw = MODEL_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_ARTIFACT_SHA256:
        raise ValueError("Discriminative artifact integrity check failed")
    def reject_constant(value):
        raise ValueError("Non-finite constant in discriminative artifact: " + value)
    model = json.loads(raw, parse_constant=reject_constant)
    _validate_model(model)
    model["artifact_sha256"] = digest
    for branch in model["branches"]:
        branch["lookup"] = {term: index for index, term in enumerate(branch["vocabulary"])}
    return model


def get_model_info() -> dict[str, Any]:
    model = _load_model()
    result = {key: model[key] for key in ("model_version", "artifact_sha256", "config_hash",
        "corpus_sha256", "training", "license", "classes", "config")}
    result.update(runtime_limits=RUNTIME_LIMITS, runtime_config_hash=RUNTIME_CONFIG_HASH)
    # Callers must not mutate the cache while the published hashes stay the same.
    return deepcopy(result)


def _mention_budget_exceeded(text: str, entity: dict[str, Any]) -> bool:
    aliases = entity.get("aliases") or []
    if not isinstance(aliases, list) or len(aliases) > RUNTIME_LIMITS["max_aliases"]:
        return True
    names = [str(entity.get("name") or ""), *(str(alias) for alias in aliases)]
    if any(len(name) > RUNTIME_LIMITS["max_name_chars"] for name in names):
        return True
    normalized = _fold(text)
    count = 0
    for name in set(name.strip() for name in names if name.strip()):
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in _fold(name).split()) + r"(?!\w)"
        for unused in re.finditer(pattern, normalized):
            count += 1
            if count > RUNTIME_LIMITS["max_mention_candidates"]:
                return True
    return False


def _decision(value: str) -> dict[str, Any]:
    model = _load_model()
    value = normalize_context(value)
    scores = list(model["intercept"])
    contributions: list[tuple[str, str, list[float]]] = []
    for branch in model["branches"]:
        counts = _ngrams(value, branch)
        active = {}
        for term, count in counts.items():
            index = branch["lookup"].get(term)
            if index is not None:
                active[index] = (1 + math.log(count)) * branch["idf"][index]
        length = math.sqrt(sum(weight * weight for weight in active.values())) or 1.0
        for index, weight in active.items():
            weight = weight / length * branch["weight"]
            effects = [row[index] * weight for row in branch["coef"]]
            scores = [s + effect for s, effect in zip(scores, effects)]
            contributions.append((branch["analyzer"], branch["vocabulary"][index], effects))
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    winner, runner_up = order[:2]
    # Feature effects explain this classifier's discriminant, never the risk score.
    supporting = sorted(contributions,
        key=lambda item: item[2][winner] - item[2][runner_up], reverse=True)[:6]
    return {"predicted_role": model["classes"][winner],
        "decision_margin": round(scores[winner] - scores[runner_up], 6),
        "decision_scores": {role: round(score, 6) for role, score in zip(model["classes"], scores)},
        "feature_evidence": [{"analyzer": analyzer, "feature": term,
            "margin_contribution": round(effects[winner] - effects[runner_up], 6),
            "scope": "auxiliary_classifier_only"} for analyzer, term, effects in supporting
            if effects[winner] > effects[runner_up]]}


def _masked_quote(quote: str, start: int, mentions: list[dict[str, Any]]) -> str:
    result = quote
    for mention in sorted(mentions, key=lambda mention: mention["start"], reverse=True):
        a, b = mention["start"] - start, mention["end"] - start
        result = result[:a] + " ENTITY_TARGET " + result[b:]
    return result


def analyze(text: str, entity: dict[str, Any], source_kind: str = "news") -> dict[str, Any]:
    """Classify exact target-containing clauses and retain source offsets.

    Multiple conflicting roles cause abstention. Classifier scores and feature
    contributions are not risk-score factors. Identity must be reviewed separately.
    """
    text = str(text or "")
    model = _load_model()
    original_text = text
    text = text[:RUNTIME_LIMITS["max_input_chars"]]
    input_truncated = len(text) < len(original_text)
    mention_limit = _mention_budget_exceeded(text, entity)
    mentions = [] if mention_limit else _mentions(text, entity)
    config = model["config"]
    findings = []
    warnings = ["Identidad sin verificar: el nombre o alias exacto no basta para atribuir hechos."]
    if input_truncated:
        warnings.append("Texto limitado a un prefijo por presupuesto de entrada; resultado global abstiene y requiere revisión completa.")
    if mention_limit:
        warnings.append("Límite de menciones o alias excedido; no se atribuyen papeles automáticamente.")
    truncated = input_truncated or mention_limit
    for start, end, quote in _sentences(text, mentions):
        local = [m for m in mentions if start <= m["start"] and m["end"] <= end]
        if not local:
            continue
        # Bounded work per note; each truncation is explicit and keeps exact offsets.
        if len(findings) >= config["max_clauses"]:
            truncated = True
            break
        if len(quote) > config["max_clause_chars"]:
            center = local[0]["start"]
            start = max(start, center - config["max_clause_chars"] // 2)
            end = min(end, start + config["max_clause_chars"])
            quote = text[start:end]
            local = [m for m in local if start <= m["start"] and m["end"] <= end]
            truncated = True
            if not local:
                continue
        decision = _decision(_masked_quote(quote, start, local))
        abstained = decision["decision_margin"] < config["minimum_margin"] or decision["predicted_role"] == "insufficient"
        role = "insufficient" if abstained else decision["predicted_role"]
        findings.append({**decision, "role": role, "relevance": ROLE_RELEVANCE[role],
            "abstained": abstained, "quote": quote, "start": start, "end": end,
            "mentions": local, "evidence_status": "exact_input_quote_not_fact_verification"})
    if truncated:
        warnings.append("Texto limitado por presupuesto local de cláusulas/caracteres; se requiere revisar el texto completo.")
    active = [finding for finding in findings if not finding["abstained"]]
    competing = sorted({finding["role"] for finding in active})
    disagreement = len(competing) > 1
    if disagreement:
        warnings.append("Hay papeles contextuales distintos entre fragmentos; no se resuelve su cronología ni contradicción automáticamente.")
    if not findings:
        warnings.append("No hay fragmentos con mención literal completa del nombre o alias.")
    best = max(active or findings, key=lambda finding: finding["decision_margin"], default=None)
    abstained = not active or disagreement or truncated
    role = "insufficient" if abstained else best["role"]
    signature = hashlib.sha256(json.dumps({key: entity.get(key) for key in
        ("id", "name", "kind", "aliases", "context")}, sort_keys=True,
        ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()
    return {"method": "discriminative", "model_version": model["model_version"],
        "model_hash": model["artifact_sha256"], "config_hash": model["config_hash"],
        "content_hash": hashlib.sha256(original_text.encode()).hexdigest(), "entity_signature": signature,
        "runtime_config_hash": RUNTIME_CONFIG_HASH, "processed_chars": len(text),
        "total_chars": len(original_text), "truncated": truncated,
        "role": role, "relevance": ROLE_RELEVANCE[role],
        "predicted_role": best["predicted_role"] if best else "insufficient",
        "decision_margin": best["decision_margin"] if best else 0.0,
        "abstained": abstained, "competing_roles": competing, "mentions": mentions,
        "findings": findings, "warnings": warnings, "limitations": list(LIMITATIONS),
        "score_effect": "auxiliary_only", "source_kind": source_kind}
