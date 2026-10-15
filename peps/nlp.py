"""Conservative, offline Spanish screening rules with exact source offsets.

This is a transparent gazetteer and rule engine, not a statistical NER model.
It detects review candidates; it does not determine identity or establish facts.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from typing import Any

MODEL_VERSION = "es-gazetteer-rules-1.0.0"
RULES_VERSION = "mx-reputational-2026-10-15"


def _fold(value: str) -> str:
    # Preserve ñ: ano and año must never become the same gazetteer entry.
    value = unicodedata.normalize("NFC", value).lower()
    return "".join("ñ" if character == "ñ" else "".join(
        c for c in unicodedata.normalize("NFD", character) if not unicodedata.combining(c))
        for character in value)


def _normalized(value: str) -> tuple[str, list[int]]:
    """Fold accents while mapping every output character to the original text."""
    chars: list[str] = []
    offsets: list[int] = []
    for match in re.finditer(r".[\u0300-\u036f]*", value, re.S):
        for folded in _fold(match.group()):
            chars.append(folded)
            offsets.append(match.start())
    return "".join(chars), offsets


def _original_span(start: int, end: int, offsets: list[int], text: str) -> tuple[int, int]:
    a, b = offsets[start], offsets[end - 1] + 1
    # Include decomposed accent marks trailing the matched final letter.
    while b < len(text) and unicodedata.combining(text[b]):
        b += 1
    return a, b


CATEGORY_PATTERNS = {
    "money_laundering": r"\b(?:lavado de (?:dinero|activos)|blanqueo de capitales)\b",
    "organized_crime": r"\b(?:delincuencia organizada|crimen organizado|narcotrafico|cartel(?:es)?)\b",
    "corruption": r"\b(?:corrupcion|cohecho|sobornos?|peculado|enriquecimiento ilicito|desvio de recursos)\b",
    "fraud": r"\b(?:fraude|estafa|operaciones simuladas|defraudacion)\b",
    "violence": r"\b(?:homicidio|feminicidio|secuestro|tortura|desaparicion forzada|violencia sexual|abuso sexual|trata de personas)\b",
    "tax": r"\b(?:evasion fiscal|delito fiscal|creditos fiscales|69[- ]?b|facturas falsas|efos)\b",
    "public_procurement": r"\b(?:licitacion irregular|contratacion irregular|conflicto de interes|inhabilitacion|colusion)\b",
    "sanctions": r"\b(?:sancion(?:es|ado|ada)?|lista de sanciones|ofac|lista de personas bloqueadas)\b",
}
CATEGORY_SEVERITY = {"money_laundering": .85, "organized_crime": .95,
                     "corruption": .8, "fraud": .65, "violence": .95,
                     "tax": .6, "public_procurement": .6, "sanctions": .85,
                     "reputational": .35}
STAGE_PATTERNS = {
    "acquittal": r"\b(?:absuelt[oa]s?|absolvi[oa]|absolucion|inocencia acreditada|declarad[oa] inocente)\b",
    "dismissed": r"\b(?:sobreseimiento|sobrese(?:yo|ida|ido)|archiv(?:o|ada|ado|arse) (?:la |el )?(?:denuncia|investigacion|caso|causa)|desestim(?:o|ada|ado) (?:la )?(?:denuncia|acusacion)|descart(?:o|ada|ado) por falta de pruebas|dejo de ser investigad[oa]|liberad[oa] (?:sin cargos|por falta de pruebas)|(?:detenid[oa]|arrestad[oa]) por error)\b",
    "denial": r"\b(?:neg(?:o|aron|ado|ada) (?:las? )?(?:acusaciones?|hechos|cargos)|rechaz(?:o|aron) (?:las? )?acusaciones?)\b",
    "conviction": r"\b(?:condenad[oa]s?|sentenciad[oa]s?|sentencia condenatoria|sentenci(?:an|aron|o) a|conden(?:an|aron|o) a|condena de)\b",
    "arrest": r"\b(?:detenid[oa]s?|detuv(?:o|ieron)|detien(?:e|en)|arrestad[oa]s?|arrest(?:an|aron|o) a|aprehendid[oa]s?|capturad[oa]s?|prision preventiva)\b",
    "wanted": r"\b(?:orden de aprehension|ficha (?:roja|de busqueda)|profug[oa]|buscad[oa] por (?:la )?(?:justicia|fiscalia))\b",
    "investigation": r"\b(?:investigad[oa]s?|investiga(?:n|do|da)?|investigacion (?:contra|por|de)|vinculad[oa] a proceso|vinculacion a proceso|procesad[oa]s?)\b",
    "allegation": r"\b(?:acusad[oa]s?|acus(?:o|a|an|aron)|acusaciones?|denunciad[oa]s?|denunci(?:an|aron|o)|denuncia (?:contra|por)|señalad[oa]s? (?:de|por)|imputad[oa]s?|imput(?:an|aron|o) a|presunt[oa]s? responsables?)\b",
    "sanction": r"\b(?:sancionad[oa]s?|inhabilitad[oa]s?|incluid[oa] en (?:la )?lista de sanciones)\b",
}
FAMILY_PATTERN = re.compile(r"\b(?:ti[oa]s?|prim[oa]s?|herman[oa]s?|padre|madre|hij[oa]s?|sobrin[oa]s?|espos[oa]|conyuge|familiar(?:es)?)\b")
ASSOCIATE_PATTERN = re.compile(r"\b(?:soci[oa]s?|colaborador(?:a|es)?|excolaborador|amig[oa]s?|empresa de|empleado de)\b")
NEGATION_PATTERN = re.compile(r"\b(?:no|nunca|jamas|sin)\b(?:\s+\w+){0,5}\s*$")
RULES_HASH = hashlib.sha256(json.dumps({"categories": CATEGORY_PATTERNS,
    "stages": STAGE_PATTERNS, "severity": CATEGORY_SEVERITY},
    sort_keys=True, ensure_ascii=False).encode()).hexdigest()

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5,
          "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
          "octubre": 10, "noviembre": 11, "diciembre": 12}


def _dates(text: str) -> list[dict[str, Any]]:
    normalized, offsets = _normalized(text)
    candidates = []
    patterns = [(r"\b(\d{4})-(\d{2})-(\d{2})\b", "iso"),
                (r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", "dmy"),
                (r"\b(\d{1,2})\s+de\s+(" + "|".join(MONTHS) + r")\s+de(?:l)?\s+(\d{4})\b", "written")]
    for pattern, kind in patterns:
        for match in re.finditer(pattern, normalized):
            parts = match.groups()
            year, month, day = ((int(parts[0]), int(parts[1]), int(parts[2])) if kind == "iso"
                else (int(parts[2]), MONTHS[parts[1]], int(parts[0])) if kind == "written"
                else (int(parts[2]), int(parts[1]), int(parts[0])))
            try:
                iso = date(year, month, day).isoformat()
            except ValueError:
                continue
            a, b = _original_span(match.start(), match.end(), offsets, text)
            candidates.append({"start": a, "end": b, "text": text[a:b], "iso_date": iso, "precision": "day"})
    return sorted(candidates, key=lambda d: d["start"])


def _mentions(text: str, entity: dict[str, Any]) -> list[dict[str, Any]]:
    normalized, offsets = _normalized(text)
    names = [entity.get("name", ""), *(entity.get("aliases") or [])]
    candidates = []
    for name in sorted(set(str(n).strip() for n in names if n), key=len, reverse=True):
        folded = _fold(name)
        # Flexible ordinary/NBSP whitespace, exact punctuation, whole tokens.
        pattern = r"(?<!\w)" + r"\s+".join(re.escape(p) for p in folded.split()) + r"(?!\w)"
        for match in re.finditer(pattern, normalized):
            a, b = _original_span(match.start(), match.end(), offsets, text)
            if not any(a < prior["end"] and b > prior["start"] for prior in candidates):
                candidates.append({"start": a, "end": b, "text": text[a:b],
                                   "entity_id": entity.get("id"), "matched_alias": name})
    return sorted(candidates, key=lambda m: m["start"])


def _sentences(text: str, mentions: list[dict[str, Any]]):
    # Semicolons split independent allegations; periods inside initials do not.
    for match in re.finditer(r"[^\n.!?;]+(?:[.!?;]+|$)", text):
        start = match.start()
        # Explicit new-subject clauses must not donate offenses or resolutions
        # to the named entity in the previous clause.
        separator = r",?\s+\b(?:mientras(?:\s+que)?|en tanto que|por su parte)\b\s*|\s+(?:y|pero)\s+(?=[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)"
        for split in re.finditer(separator, match.group()):
            a, b = match.start() + split.start(), match.start() + split.end()
            if any(m["start"] < b and m["end"] > a for m in mentions):
                continue
            if re.match(r"\s+y\s+", split.group()) and not any(
                    re.search(pattern, _fold(text[start:a])) for pattern in STAGE_PATTERNS.values()):
                # "X y Y fueron acusados" is a joint subject, not two clauses.
                continue
            if start < a:
                yield start, a, text[start:a]
            start = b
        if start < match.end():
            yield start, match.end(), text[start:match.end()]


def _identity(text: str, entity: dict[str, Any], mentions: list[dict[str, Any]]) -> tuple[float, list[str]]:
    if not mentions:
        return 0.0, ["No hay mención literal verificable de la entidad."]
    reasons = ["Coincidencia literal del nombre o de un alias; no acredita identidad."]
    longest = max((m["matched_alias"] for m in mentions), key=len)
    tokens = re.findall(r"\w+", _fold(longest))
    stop = {"de", "del", "la", "las", "el", "los", "sa", "cv", "s", "a", "y"}
    meaningful = [t for t in tokens if t not in stop]
    kind = str(entity.get("kind", "person")).lower()
    confidence = .82 if len(meaningful) >= 3 else .68 if len(meaningful) == 2 else .35
    if kind in {"company", "organization", "empresa", "sociedad", "organisation"}:
        confidence = .82 if len(meaningful) >= 2 else .48
    if len(longest.strip()) <= 3:
        confidence = .2
    normalized = _fold(text)
    context_matches = []
    context = entity.get("context") or {}
    for key in ("state", "city", "country", "position", "role", "organization", "jurisdiction", "occupation"):
        values = context.get(key, [])
        for value in values if isinstance(values, list) else [values]:
            value = str(value).strip()
            if len(value) >= 4 and re.search(r"(?<!\w)" + re.escape(_fold(value)) + r"(?!\w)", normalized):
                context_matches.append(key)
                break
    if context_matches:
        confidence = min(.94, confidence + min(.12, .06 * len(context_matches)))
        reasons.append("Contexto coincidente: " + ", ".join(context_matches) + ".")
    if len(meaningful) < 2:
        reasons.append("Nombre corto o ambiguo: necesita desambiguación humana.")
    return round(confidence, 3), reasons


def _relation(normalized: str, mention_start: int, mention_end: int, trigger_start: int,
              trigger_end: int) -> str:
    before = normalized[:mention_start]
    after = normalized[mention_end:]
    # "el tío de X fue detenido", "X, cuyo tío fue detenido", "X y su tío...".
    family_before = FAMILY_PATTERN.search(before[-100:])
    family_after = FAMILY_PATTERN.search(after[:100])
    possessive_after = re.search(r"^\s*[,:(-]?\s*(?:cuyo|cuya|su|y su)\b", after)
    relative_of = re.search(r"\b(?:de|del)\s*$", before)
    if (family_before and relative_of and trigger_start >= mention_end) or (family_before and relative_of and trigger_end <= mention_start):
        return "family"
    if family_after and possessive_after and trigger_start > mention_end:
        if re.search(r"^\s+y su\s+\w+\s+(?:fueron|estan|son)\s*$", normalized[mention_end:trigger_start]):
            return "direct"
        return "family"
    if trigger_start > mention_end and re.search(
            r"\b(?:con|a)\s+su\s+(?:sobrin[oa]|prim[oa]|ti[oa]|herman[oa]|hij[oa]|padre|madre)\b.*\b(?:quien|el cual|la cual)\b",
            normalized[mention_end:trigger_start]):
        return "family"
    relative_after = re.match(r"\s*[,:(-]?\s*(?:hij[oa]|sobrin[oa]|prim[oa]|herman[oa]|espos[oa]) de\b", after)
    if relative_after and trigger_start > mention_end:
        linking_text = normalized[mention_end + relative_after.end():trigger_start]
        if "," not in linking_text:
            return "family"
    if ASSOCIATE_PATTERN.search(before[-100:]) and relative_of:
        return "associate"
    if ASSOCIATE_PATTERN.search(after[:100]) and possessive_after and trigger_start > mention_end:
        return "associate"
    between = normalized[min(mention_end, trigger_end):max(mention_start, trigger_start)]
    # Another explicitly named target between the entity and the trigger.
    if re.search(r"\b(?:contra|a)\s+[a-zñ]+(?:\s+[a-zñ]+){1,4}\s+(?:por|fue|ha sido)\b", between):
        return "context"
    return "direct"


def _reporter(normalized: str, mention_end: int) -> bool:
    after = normalized[mention_end:]
    return bool(re.search(r"^\s*(?:,[^,]{1,100},\s*)?,?\s*(?:presento|interpuso|ratifico|denunci(?:o|a|aron)|acus(?:o|a|aron)|investigo|investiga\s+a|detuvo|informo|reporto|explico|advirtio|presencio|sufrio|fue victima|propuso|dijo|afirmo|aseguro|critico|señalo|visito|conocio|defendio|anuncio|ordeno|exigio|recibio)\b", after))


def _negated(prefix: str) -> bool:
    scope = re.split(r"\b(?:pero|sin embargo|no obstante)\b", prefix)[-1]
    scope = re.sub(r",[^,]*,", " ", scope)
    if re.search(r"\b(?:es falso que|es mentira que|desmintio|nego|rechazo|descarto)\b(?:[\s,]+\w+){0,12}\s*$", scope):
        return True
    return bool(re.search(r"\b(?:no|nunca|jamas|sin)\b(?!\s+(?:solo|solamente|unicamente)\b)(?:[\s,]+\w+){0,12}\s*$", scope))


def analyze(text: str, entity: dict[str, Any], source_kind: str = "news") -> dict[str, Any]:
    """Return conservative candidates, source quotes, and stable character offsets.

    The caller must provide source body text, never a search query substituted
    for missing article text. Public comments always require human review.
    """
    text = str(text or "")
    mentions = _mentions(text, entity)
    dates = _dates(text)
    confidence, identity_reasons = _identity(text, entity, mentions)
    findings: list[dict[str, Any]] = []
    for sentence_start, sentence_end, sentence in _sentences(text, mentions):
        local_mentions = [m for m in mentions if sentence_start <= m["start"] < sentence_end]
        if not local_mentions:
            continue
        normalized, offsets = _normalized(sentence)
        stages = [(stage, match) for stage, pattern in STAGE_PATTERNS.items()
                  for match in re.finditer(pattern, normalized)]
        if not stages:
            continue
        contrasts = list(re.finditer(r"\b(?:pero|aunque|sin embargo)\b|\b[ye]\s+(?=(?:fue|sigue|continua|esta|es|ha|permanece|resulto|quedo|acusad|investigad|detenid|absuelt))", normalized))
        for stage, match in stages:
            left = max((split.end() for split in contrasts if split.end() <= match.start()), default=0)
            right = min((split.start() for split in contrasts if split.start() >= match.end()), default=len(normalized))
            categories = [category for category, pattern in CATEGORY_PATTERNS.items()
                          if re.search(pattern, normalized[left:right])] or ["reputational"]
            trigger_a, trigger_b = _original_span(match.start(), match.end(), offsets, sentence)
            mention = min(local_mentions, key=lambda m: abs(m["start"] - sentence_start - trigger_a))
            # Convert original offsets to folded offsets for syntactic rules.
            local_a = mention["start"] - sentence_start
            local_b = mention["end"] - sentence_start
            folded_a = len(_normalized(sentence[:local_a])[0])
            folded_b = len(_normalized(sentence[:local_b])[0])
            relationship = _relation(normalized, folded_a, folded_b, match.start(), match.end())
            if relationship == "direct" and _reporter(normalized, folded_b):
                relationship = "context"
            prefix = normalized[max(0, match.start() - 180):match.start()]
            negated = _negated(prefix)
            double_denial = stage == "allegation" and bool(re.search(
                r"\bno\s+(?:nego|rechazo|desmintio|descarto)\b", prefix))
            if double_denial:
                negated = False
            # "no negó las acusaciones" is not an exculpatory denial.
            if stage == "denial" and negated:
                continue
            for category in categories:
                finding = {"category": category, "stage": stage,
                    "subject": entity.get("name", "") if relationship == "direct" else "persona relacionada o tercero",
                    "relationship": relationship, "negated": negated,
                    "quote": sentence, "start": sentence_start, "end": sentence_end,
                    "trigger_start": sentence_start + trigger_a, "trigger_end": sentence_start + trigger_b,
                    "severity": CATEGORY_SEVERITY[category],
                    "uncertain": double_denial or bool(re.search(r"\b(?:presunt[oa]|supuest[oa]|habria|podria|sin confirmar|no comprobado)\b", normalized))}
                finding["hypothetical"] = bool(re.search(
                    r"\b(?:podria|podrian|sera|seria|serian|seran|sea|sean|fuera|fueran|fuese|fuesen|solicita|solicito|pide|pidio|exigio|exige|preve)(?:\s+\w+){0,8}\s*$", prefix))
                sentence_dates = [d for d in dates if sentence_start <= d["start"] < sentence_end]
                if len(sentence_dates) == 1:
                    finding["event_date"] = sentence_dates[0]["iso_date"]
                    finding["event_date_basis"] = "single_date_in_quote_requires_review"
                elif len(sentence_dates) > 1:
                    finding["event_date_ambiguous"] = True
                signature = (category, stage, relationship, negated, sentence_start)
                if not any((f["category"], f["stage"], f["relationship"], f["negated"], f["start"]) == signature for f in findings):
                    findings.append(finding)
    entity_signature = hashlib.sha256(json.dumps({key: entity.get(key) for key in
        ("id", "name", "kind", "aliases", "context")}, sort_keys=True,
        ensure_ascii=False, default=str, separators=(",", ":")).encode()).hexdigest()
    return {"mentions": mentions, "dates": dates, "identity_confidence": confidence,
            "identity_reasons": identity_reasons, "findings": findings,
            "entity_signature": entity_signature,
            "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            "review_required": bool(mentions), "model_version": MODEL_VERSION,
            "rules_version": RULES_VERSION, "rules_hash": RULES_HASH,
            "source_kind": source_kind,
            "limitations": ["Reglas y diccionario local; no resuelve correferencias generales.",
                            "Coincidencia nominal y acusaciones requieren revisión de identidad y fuente."]}
