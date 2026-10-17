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
from functools import lru_cache
from typing import Any

from .identity import COMPANY_KINDS

MODEL_VERSION = "es-gazetteer-rules-1.0.7"
RULES_VERSION = "mx-reputational-2026-10-17-r4"
SENTENCE_PATTERN = r"[^\r\n.!?;]+(?:[.!?;]+|(?=\r?\n|$))"


def _fold_uncached(value: str) -> str:
    # Preserve ñ: ano and año must never become the same gazetteer entry.
    value = unicodedata.normalize("NFC", value).lower()
    return "".join("ñ" if character == "ñ" else "".join(
        c for c in unicodedata.normalize("NFD", character) if not unicodedata.combining(c))
        for character in value)


@lru_cache(maxsize=4096)
def _fold_short_cached(value: str) -> str:
    return _fold_uncached(value)


def _fold(value: str) -> str:
    # Normalization visits the same small Unicode clusters millions of times.
    # Cache only their immutable string transformation, never bodies, findings
    # or identity decisions. Long clusters and other long inputs stay uncached.
    return _fold_short_cached(value) if len(value) <= 32 else _fold_uncached(value)


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
    "organized_crime": r"\b(?:delincuencia organizada|crimen organizado|narcotrafico|cartel(?:es)?|cjng)\b",
    "corruption": r"\b(?:corrupcion|cohecho|sobornos?|peculado|enriquecimiento ilicito|desvio de recursos)\b",
    "fraud": r"\b(?:fraude|estafa|operaciones simuladas|defraudacion)\b",
    "violence": r"\b(?:homicidio|feminicidio|secuestro|tortura|desaparicion forzada|violencia sexual|abuso sexual|trata de personas)\b",
    "tax": r"\b(?:evasion fiscal|delito fiscal|creditos fiscales|69[- ]?b|facturas falsas|efos)\b",
    "public_procurement": r"\b(?:licitacion irregular|contratacion irregular|conflicto de interes|inhabilitacion|colusion)\b",
    "sanctions": r"\b(?:sancion(?:es|ado|ada)?|lista de sanciones|ofac|lista de personas bloqueadas|multas?|multad[oa]s?|multo|multaron|multan)\b",
}
CATEGORY_SEVERITY = {"money_laundering": .85, "organized_crime": .95,
                     "corruption": .8, "fraud": .65, "violence": .95,
                     "tax": .6, "public_procurement": .6, "sanctions": .85,
                     "reputational": .35}
STAGE_PATTERNS = {
    "acquittal": r"\b(?:absuelt[oa]s?|absolvi[oa]|absolucion|inocencia acreditada|declarad[oa] inocente)\b",
    "dismissed": r"\b(?:sobreseimiento|sobrese(?:yo|ida|ido)|archiv(?:o|ada|ado|arse) (?:la |el )?(?:denuncia|investigacion|caso|causa)|desestim(?:o|ada|ado) (?:la )?(?:denuncia|acusacion)|descart(?:o|ada|ado) por falta de pruebas|dejo de ser investigad[oa]|liberad[oa] (?:sin cargos|por falta de pruebas)|(?:detenid[oa]|arrestad[oa]) por error)\b",
    "denial": r"\b(?:neg(?:o|aron|ado|ada) (?:las? )?(?:acusaciones?|hechos|cargos)|rechaz(?:o|aron) (?:las? )?acusaciones?)\b",
    "conviction": r"\b(?:condenad[oa]s?|sentenciad[oa]s?|sentencia condenatoria|sentenci(?:an|aron|o) a|conden(?:an|aron|o) a|condena de|purga una condena)\b",
    "arrest": r"\b(?:detenid[oa]s?|detuv(?:o|ieron)|detien(?:e|en)|arrestad[oa]s?|arrest(?:an|aron|o) a|aprehendid[oa]s?|capturad[oa]s?|prision preventiva)\b",
    "wanted": r"\b(?:orden de aprehension|ficha (?:roja|de busqueda)|profug[oa]|buscad[oa] por (?:la )?(?:justicia|fiscalia))\b",
    "investigation": r"\b(?:investigad[oa]s?|investiga(?:n|do|da)?|investigacion (?:contra|por|de)|vinculad[oa] a proceso|vinculacion a proceso|procesad[oa]s?)\b",
    "allegation": r"\b(?:acusad[oa]s?|acus(?:o|a|an|aron)|acusaciones?|denunciad[oa]s?|denunci(?:an|aron|o)|denuncia (?:contra|por)|señalad[oa]s? (?:de|por)|imputad[oa]s?|imput(?:an|aron|o) a|presunt[oa]s? responsables?)\b",
    "sanction": r"\b(?:sancionad[oa]s?|inhabilitad[oa]s?|incluid[oa] en (?:la )?lista de sanciones|multad[oa]s?|mult(?:o|aron|an)|multa(?=\s+(?:a|con)\b))\b",
}
FAMILY_PATTERN = re.compile(r"\b(?:ti[oa]s?|prim[oa]s?|herman[oa]s?|padre|madre|hij[oa]s?|sobrin[oa]s?|espos[oa]|conyuge|familiar(?:es)?)\b")
ASSOCIATE_PATTERN = re.compile(r"\b(?:soci[oa]s?|colaborador(?:a|es)?|excolaborador|amig[oa]s?|empresa de|empleado de)\b")
NEGATION_PATTERN = re.compile(r"\b(?:no|nunca|jamas|sin)\b(?:\s+\w+){0,5}\s*$")
CORPORATE_ROLE = (r"(?:ex[- ]?)?(?:director(?:a|es|as)?|directiv[oa]s?|gerentes?|presidentes?|"
                  r"consejer[oa]s?|administrador(?:a|es|as)?|representantes?|emplead[oa]s?|"
                  r"trabajador(?:a|es|as)?|funcionari[oa]s?)")
CORPORATE_OFFICER_OF = re.compile(
    r"\b" + CORPORATE_ROLE + r"(?:\s+(?:general(?:es)?|adjunt[oa]s?|ejecutiv[oa]s?|"
    r"financier[oa]s?|comercial(?:es)?|juridic[oa]s?|legal(?:es)?|regional(?:es)?|"
    r"interin[oa]s?)){0,2}\s+(?:de|del)\s+(?:(?:la|el)\s+)?"
    r"(?:(?:empresa|compañia|estatal)\s+)?$")
OTHER_ACTOR_PASSIVE = re.compile(
    r"\b(?:politicos|panistas|legisladores|senadores|diputados|funcionarios|"
    r"servidores publicos|exsenadores|exdirectores|directivos|personas)"
    r"(?:\s+\w+){0,4}\s*$")

# Candidate: an enacted notarial administrative measure, with an explicit
# recipient and adverse grounds in the same target-containing clause.
NOTARIAL_MEASURE = re.compile(
    r"\b(?:(?:revoco|revocaron|cancelo|cancelaron|retiro|retiraron|"
    r"revocad[oa]|cancelad[oa]|retirad[oa])\s+(?:de manera definitiva\s+)?"
    r"(?:la|el|su)\s+(?:patente|nombramiento)\b|"
    r"(?:decidio|decidieron|determino|determinaron)\s+"
    r"(?:suspender\s+(?:las\s+)?funciones\s+del ejercicio\s+notarial|"
    r"revocar\s+de\s+la\s+notaria(?:\s+(?:numero\s+)?\d+)?)|"
    r"inhabilit(?:o|aron)(?:\s+de\s+manera\s+definitiva)?)\b")
NOTARIAL_RECEIVED_MEASURE = re.compile(
    r"\brecibio\s+(?:(?:una|la)\s+)?(?:(?:segunda|nueva|otra)\s+)?"
    r"(?:suspension\s+de\s+(?:sus|las)\s+(?:actividades|funciones)\s+notariales|"
    r"inhabilitacion(?:\s+definitiva)?\s+a\s+su\s+cargo\s+como\s+fedatario)\b")
NOTARIAL_GROUNDS = re.compile(
    r"\b(?:irregularidad(?:es)?|irregulares|anomalias?|incumpl(?:imiento|ir)|"
    r"resolverse\s+(?:una\s+)?(?:segunda\s+)?queja|falta(?:s)?(?:\s+grave)?\s+de\s+probidad|"
    r"faltas?\s+administrativas?|responsabilidad\s+notarial|"
    r"proceso\s+sancionador|procedimiento\s+sancionador|fraude|corrupcion)\b")
NOTARIAL_VOLUNTARY = re.compile(
    r"\b(?:renuncia|renuncio|jubilacion|fallecimiento|solicitud\s+propia|"
    r"propia\s+voluntad|voluntari[oa])\b")
NOTARIAL_RECEIVER_TITLE = re.compile(
    r"(?:(?:el|la)\s+)?(?:notari[oa](?:\s+public[oa])?(?:\s+titular)?|"
    r"titular(?:\s+de\s+la\s+notaria(?:\s+publica)?)?)"
    r"(?:\s+numero\s+[\w-]+|\s+\d+)?(?:\s+(?:de|del)\s+[\w -]{1,130})?\s*,?\s*$")
NOTARIAL_TARGET_APPOSITIVE = re.compile(
    r"^\s*,\s*(?:notari[oa](?:\s+public[oa])?|fedatario|"
    r"titular\s+de\s+la\s+notaria)\b")
NOTARIAL_RECEIVER_EXCLUSIONS = re.compile(
    r"\b(?:quien|que|segun|explico|informo|denuncio|aseguro|declaro|"
    r"familiar|sobrino|hijo|padre|madre|hermano|esposo|reporto)\b")
NOTARIAL_BOUND_OBJECT = re.compile(
    r"\b(?:patente|nombramiento)(?:\s+profesional)?\s+(?:de|como)\s+(?:la\s+)?notari[oa]\b|"
    r"\bejercicio\s+notarial\b|\brevocar\s+de\s+la\s+notaria\b")
NOTARIAL_ADMINISTRATIVE_PROCEEDING = re.compile(
    r"\b(?:responsabilidad\s+notarial|proceso\s+sancionador|procedimiento\s+sancionador)\b")
NOTARIAL_POLICY = "enacted_measure_explicit_recipient_same_clause_adverse_grounds_no_voluntary_3"

# A reported departure from custody followed by the same named subject's
# continued proceeding is neither an acquittal nor a conviction. Do not borrow
# offences from later pronoun-only sentences or resolve another named actor.
POST_RELEASE_PROCESS = re.compile(
    r"\bcontinuara\s+su\s+proceso\s+con\s+medidas\s+cautelares\b")
POST_RELEASE_SUBJECT_LINK = re.compile(
    r"\s*,?\s*abandono\s+(?:la\s+carcel|(?:la\s+)?prision)\s*,\s*"
    r"(?:sin\s+embargo|pero|no\s+obstante)\s*,?\s*")

# Nominal resolutions and explicit lists are common in administrative news.
# Their recipient is established below by grammar/structure, never by an
# adverse word anywhere in the document or by an automatically invented alias.
ADMINISTRATIVE_MEASURE = re.compile(
    r"\b(?:impuso\s+(?:(?:una|la)\s+)?amonestacion(?:\s+publica)?|"
    r"anuncio\s+(?:la\s+)?destitucion|"
    r"(?:revocacion\s+e\s+inhabilitacion)(?:\s+definitiva|\s+de\s+nombramiento)?|"
    r"(?:ha\s+quedado|fue)\s*,?\s*(?:de\s+manera\s+definitiva\s*,?\s*)?"
    r"separad[oa]\s+como\s+titular\s+de\s+la\s+notaria|"
    r"separar\s+definitivamente\s+del\s+cargo\s+como\s+notario)\b")
PROFESSIONAL_LIST_HEADING = re.compile(
    r"\b(?:los\s+)?notarios\s+sancionados\s*:\s*$")
ACCOUNT_SUSPENSION_LIST = re.compile(
    r"\b(?:notarios|fedatarios)\b[^.!?\n]{0,100}\ba\s+los\s+que\s+se\s+les\s+"
    r"suspendieron\s+las\s+cuentas\s+y\s+funciones\s+son\b")
ALLEGATION_LIST = re.compile(
    r"\ben\s+las\s+acusaciones\s+aparecen\s+los\s+nombres\s+de\s+"
    r"(?:los\s+)?(?:notarios|funcionarios|servidores\s+publicos)(?:\s+publicos)?\b")
REVOCATION_LIST = re.compile(r"\brevocaciones\b")
STRUCTURED_CONTEXT_POLICY = (
    "explicit_recipient_nominal_measure_or_bounded_identifying_list_1;"
    "paragraphs_max_3_characters_max_4000_no_general_pronoun_resolution;"
    "list_findings_reputational_not_offences_from_other_paragraphs;"
    "publication_role_only_no_identity_confirmation")

# Explicit documentary constructions, developed on the unsealed V1 development
# corpus. These are grammatical recipient scopes, not general coreference or
# permission to transfer an article's adverse vocabulary to every named person.
PUBLIC_RECIPIENT_ROLE = (r"(?:president[ea]|alcalde(?:sa)?|director(?:a)?|titular|"
                         r"encargad[oa]|funcionari[oa]|servidor(?:a)?)")
RECIPIENT_EXCLUSION = re.compile(
    r"\b(?:herman[oa]|hij[oa]|padre|madre|espos[oa]|sobrin[oa]|familiar|"
    r"abogad[oa]|defensor[oa]|testigo|victima|denunciante|quejoso|representado|"
    r"segun|informo|explico|declaro|atestiguo|testifico|resolvio|firmante)\b")
ADMIN_ADMONITION = re.compile(
    r"\b(?:(?:se\s+)?(?:impuso|impusieron|emitio|emitieron)\s*"
    r"(?:(?:0?\d+|una|unas|la|las)\s+)?amonestacion(?:es)?(?:\s+publica(?:s)?)?|"
    r"recibio\s+(?:(?:0?\d+|una|unas)\s+)?amonestacion(?:es)?(?:\s+publica(?:s)?)?|"
    r"(?:el\s+resto\s+de\s+)?amonestaciones(?:\s+publicas)?\s+se\s+impuso)\b")
ADMIN_ORDER = re.compile(
    r"\bimpuso\s+(?:(?:\d+|un|uno|dos|tres|cuatro|cinco)\s+)?"
    r"arrestos?\s+administrativos?\b")
ADMIN_PROCEDURE = re.compile(
    r"\bprocedimiento\s+(?:administrativo\s+sancionador\s+)?(?:de\s+)?"
    r"queja\s+en\s+materia\s+de\s+fiscalizacion\s*,?\s*"
    r"instaurad[oa]\s+en\s+contra\b")
POLITICAL_RECALL = re.compile(
    r"\bfiled\s+(?:a\s+|the\s+)?petition\s+(?:last\s+week\s+|"
    r"yesterday\s+)?to\s+recall\s+(?:(?:the\s+)?(?:mayor|governor|"
    r"council(?:wo)?man|councillor)\s+)?")
EXPLICIT_DISPOSITION = re.compile(
    r"\b(?:este|esta|dicho|dicha|el\s+mismo|la\s+misma|el\s+presente)\s+"
    r"(?:procedimiento|expediente|queja|sancion|amonestacion|arresto\s+administrativo)\s+"
    r"(?:fue\s+(?:archivad[oa]|sobreseid[oa]|desechad[oa]|desestimad[oa]|anulad[oa])|"
    r"quedo\s+sin\s+efecto)\b")
DOCUMENTARY_CONTEXT_POLICY = (
    "explicit_administrative_recipient_lists_max_12_paragraphs_4000_chars;"
    "adjacent_identifying_continuations_unique_named_subject_no_nearest_name;"
    "procedure_title_investigation_only_outcome_unread;"
    "english_filed_recall_petition_governance_controversy_not_crime;"
    "category_continuity_only_explicit_role_followup_to_named_arrest_1")

RULES_HASH = hashlib.sha256(json.dumps({"rules_version": RULES_VERSION, "categories": CATEGORY_PATTERNS,
    "stages": STAGE_PATTERNS, "severity": CATEGORY_SEVERITY, "sentences": SENTENCE_PATTERN,
    "corporate_officer": CORPORATE_OFFICER_OF.pattern,
    "notarial_measure": NOTARIAL_MEASURE.pattern,
    "notarial_received_measure": NOTARIAL_RECEIVED_MEASURE.pattern,
    "notarial_grounds": NOTARIAL_GROUNDS.pattern,
    "notarial_voluntary": NOTARIAL_VOLUNTARY.pattern,
    "notarial_receiver_title": NOTARIAL_RECEIVER_TITLE.pattern,
    "notarial_target_appositive": NOTARIAL_TARGET_APPOSITIVE.pattern,
    "notarial_receiver_exclusions": NOTARIAL_RECEIVER_EXCLUSIONS.pattern,
    "notarial_bound_object": NOTARIAL_BOUND_OBJECT.pattern,
    "notarial_administrative_proceeding": NOTARIAL_ADMINISTRATIVE_PROCEEDING.pattern,
    "notarial_policy": NOTARIAL_POLICY,
    "post_release_process": POST_RELEASE_PROCESS.pattern,
    "post_release_subject_link": POST_RELEASE_SUBJECT_LINK.pattern,
    "administrative_measure": ADMINISTRATIVE_MEASURE.pattern,
    "professional_list_heading": PROFESSIONAL_LIST_HEADING.pattern,
    "account_suspension_list": ACCOUNT_SUSPENSION_LIST.pattern,
    "allegation_list": ALLEGATION_LIST.pattern,
    "revocation_list": REVOCATION_LIST.pattern,
    "structured_context_policy": STRUCTURED_CONTEXT_POLICY,
    "public_recipient_role": PUBLIC_RECIPIENT_ROLE,
    "recipient_exclusion": RECIPIENT_EXCLUSION.pattern,
    "admin_admonition": ADMIN_ADMONITION.pattern,
    "admin_order": ADMIN_ORDER.pattern,
    "admin_procedure": ADMIN_PROCEDURE.pattern,
    "political_recall": POLITICAL_RECALL.pattern,
    "explicit_disposition": EXPLICIT_DISPOSITION.pattern,
    "documentary_context_policy": DOCUMENTARY_CONTEXT_POLICY,
    "other_actor_passive": OTHER_ACTOR_PASSIVE.pattern,
    "corporate_relation_policy": "officer_and_explicit_other_subject_are_context_1"},
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
    for match in re.finditer(SENTENCE_PATTERN, text):
        start = match.start()
        # Explicit new-subject clauses must not donate offenses or resolutions
        # to the named entity in the previous clause.
        separator = (r",?\s+\b(?:mientras(?:\s+que)?|en tanto que|por su parte)\b\s*|"
                     r",\s+(?=(?:un|una|el|la)\s+(?:ciudadan[oa]|testigo|abogad[oa]|"
                     r"victima|fiscal|denunciante)\s+(?:denunci[oó]|inform[oó]|declar[oó]|"
                     r"atestigu[oó]|testific[oó])\b)|"
                     r"\s+(?:y|pero)\s+(?=[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)")
        for split in re.finditer(separator, match.group()):
            a, b = match.start() + split.start(), match.start() + split.end()
            if any(m["start"] < b and m["end"] > a for m in mentions):
                continue
            if re.match(r"\s+y\s+", split.group()):
                preceding = _fold(text[start:a])
                if not any(re.search(pattern, preceding) for pattern in STAGE_PATTERNS.values()):
                    # "X y Y fueron acusados" is a joint subject.
                    continue
                if (re.search(r"\b(?:se\s+investiga|investigan|acusaron|denunciaron)\b", preceding)
                        and not re.search(r"\b(?:fue|fueron|ha\s+sido|han\s+sido)\b", preceding)
                        and not re.match(r"(?:[^,.;\n]{1,100},\s*)?(?:fue|es|era|informo|declaro|"
                                         r"denuncio|acuso|presento|resulto|quedo)\b", _fold(text[b:]))):
                    # "Se investiga la complicidad de X y Y, notario..."
                    # coordinates two objects of the same investigation.
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
    if trigger_end <= mention_start and (
            re.search(r"\b(?:secuestro|homicidio|feminicidio|tortura|violencia sexual|abuso sexual)\s+(?:de|contra)\s*$", before)
            or re.search(r"\b(?:representad[oa]\s+por|segun\s+(?:atestiguo|testifico))\b[^;.!?]{0,100}$", before)
            or re.search(r"\b(?:su\s+abogad[oa]|su\s+defensor[oa])\s*$", before)):
        return "context"
    # Another explicitly named target between the entity and the trigger.
    if re.search(r"\b(?:contra|a)\s+[a-zñ]+(?:\s+[a-zñ]+){1,4}\s+(?:por|fue|ha sido)\b", between):
        return "context"
    return "direct"


def _reporter(normalized: str, mention_end: int) -> bool:
    after = normalized[mention_end:]
    return bool(re.search(r"^\s*(?:,[^,]{1,100},\s*)?,?\s*(?:presento|interpuso|ratifico|denunci(?:o|a|aron)|acus(?:o|a|aron)|investigo|investiga\s+a|detuvo|informo|reporto|explico|advirtio|presencio|atestiguo|testifico|resolvio|sufrio|fue victima|propuso|dijo|afirmo|aseguro|critico|señalo|visito|conocio|defendio|anuncio|ordeno|exigio|recibio)\b", after))


def _corporate_context(normalized: str, mention_start: int, mention_end: int,
                       trigger_start: int, trigger_end: int) -> bool:
    # An officer's conduct is not automatically the company's conduct. Require
    # a local grammatical ownership link, rather than a role anywhere nearby.
    if CORPORATE_OFFICER_OF.search(normalized[max(0, mention_start - 180):mention_start]):
        return True
    after = normalized[mention_end:]
    relative = re.match(r"\s*,?\s*(?:cuy[oa]s?|su)\s+" + CORPORATE_ROLE + r"\b", after)
    if trigger_start > mention_end and relative:
        if ',' not in normalized[mention_end + relative.end():trigger_start]:
            return True
    # "políticos actualmente investigados ... caso de Empresa" names the
    # investigated people explicitly. A later corporate mention is context;
    # active "políticos investigan a Empresa" remains a direct target.
    trigger = normalized[trigger_start:trigger_end]
    passive = re.fullmatch(r"(?:investigad|acusad|detenid|procesad|sancionad|condenad|sentenciad)[oa]s?", trigger)
    return bool(trigger_end <= mention_start and passive and OTHER_ACTOR_PASSIVE.search(
        normalized[max(0, trigger_start - 140):trigger_start]))


def _negated(prefix: str) -> bool:
    scope = re.split(r"\b(?:pero|sin embargo|no obstante)\b", prefix)[-1]
    scope = re.sub(r",[^,]*,", " ", scope)
    if re.search(r"\b(?:es falso que|es mentira que|desmintio|nego|rechazo|descarto)\b(?:[\s,]+\w+){0,12}\s*$", scope):
        return True
    return bool(re.search(r"\b(?:no|nunca|jamas|sin)\b(?!\s+(?:solo|solamente|unicamente)\b)(?:[\s,]+\w+){0,12}\s*$", scope))



def _notarial_measure_applies(normalized: str, match, sentence: str,
                             sentence_start: int, mentions: list[dict[str, Any]]) -> bool:
    """Fail closed on missing recipient, distant references or voluntary acts.

    This lexical candidate is not identity or legal-outcome verification. It
    deliberately does not resolve pronouns, missing aliases, or other clauses.
    """
    grounds = [ground for ground in NOTARIAL_GROUNDS.finditer(normalized)
               if not _negated(normalized[max(0, ground.start() - 180):ground.start()])]
    if not grounds or NOTARIAL_VOLUNTARY.search(normalized):
        return False
    for mention in mentions:
        start = len(_normalized(sentence[:mention['start'] - sentence_start])[0])
        end = len(_normalized(sentence[:mention['end'] - sentence_start])[0])
        if NOTARIAL_RECEIVED_MEASURE.fullmatch(match.group()):
            # The literal named subject must receive a professional measure,
            # rather than report a complaint or another person's suspension.
            if (0 <= match.start() - end <= 20
                    and re.fullmatch(r"\s*,?\s*", normalized[end:match.start()])):
                return True
            continue
        if not 0 <= start - match.end() <= 260:
            continue
        between = normalized[match.end():start]
        if (re.fullmatch(r"(?:revoco|revocaron)\s+(?:de manera definitiva\s+)?"
                         r"(?:la|su)\s+patente", match.group())
                and re.fullmatch(r"\s+del\s+notari[oa](?:\s+public[oa])?\s+", between)):
            # "revocó la patente del notario NAME" names the professional
            # recipient explicitly; adverse grounds are still mandatory above.
            return True
        if (between.strip() == 'de'
                and re.fullmatch(r"(?:revoco|revocaron|cancelo|cancelaron|retiro|retiraron)\s+"
                                 r"(?:de manera definitiva\s+)?(?:la|el|su)\s+"
                                 r"(?:patente|nombramiento)", match.group())
                and NOTARIAL_TARGET_APPOSITIVE.match(normalized[end:])):
            # "revocó el nombramiento de NAME, notario..." binds the
            # professional title to this recipient, not an unrelated license.
            return True
        recipient_markers = list(re.finditer(r"\b(?:a|al)\s+", between))
        if len(recipient_markers) != 1:
            continue
        object_context = normalized[match.start():match.end()] + between[:recipient_markers[0].start()]
        bound_professional_object = NOTARIAL_BOUND_OBJECT.search(object_context)
        administrative_proceeding = NOTARIAL_ADMINISTRATIVE_PROCEEDING.search(normalized)
        receiver_title = between[recipient_markers[0].end():]
        if receiver_title.strip() and (not NOTARIAL_RECEIVER_TITLE.fullmatch(receiver_title)
                                      or NOTARIAL_RECEIVER_EXCLUSIONS.search(receiver_title)):
            continue
        inhabilitated_notary = (match.group().startswith('inhabilit')
                               and not between[:recipient_markers[0].start()].strip()
                               and NOTARIAL_RECEIVER_TITLE.fullmatch(receiver_title))
        if not bound_professional_object and not administrative_proceeding and not inhabilitated_notary:
            continue
        scope = normalized[max(0, match.start() - 90):start]
        if re.search(r"\b(?:notari[oa]s?|notarial(?:es)?)\b", scope):
            return True
    return False


def _post_release_process_matches(normalized, sentence, sentence_start, mentions):
    for match in POST_RELEASE_PROCESS.finditer(normalized):
        for mention in mentions:
            end = len(_normalized(sentence[:mention['end'] - sentence_start])[0])
            if (end <= match.start()
                    and POST_RELEASE_SUBJECT_LINK.fullmatch(normalized[end:match.start()])):
                yield match
                break


def _structured_findings(text: str, entity: dict[str, Any], mentions: list[dict[str, Any]]):
    """Recognize explicit resolution recipients and identified list members.

    A finding spanning paragraphs is permitted only by an identifying connector
    (a colon/list, "se trata del", or a bounded continuation of that same list).
    Ordinary adjacent headlines and arbitrary nearby allegations never join.
    Quotes retain the heading and literal name so the link is inspectable.
    """
    paragraphs = list(re.finditer(r"[^\r\n]+(?:\r?\n(?![ \t]*\r?\n)[^\r\n]+)*", text))

    def emit(a, b, trigger_a, trigger_b, stage, basis, uncertain=False):
        quote = text[a:b]
        folded, _ = _normalized(quote)
        local_trigger = len(_normalized(text[a:trigger_a])[0])
        local_trigger_end = len(_normalized(text[a:trigger_b])[0])
        prefix = folded[max(0, local_trigger - 180):local_trigger]
        if _negated(prefix) or re.search(
                r"\b(?:podria|podrian|posible|posibles|solicita|solicito|pide|pidio|"
                r"exige|exigio|propone|propuso)(?:\s+\w+){0,8}\s*$", prefix):
            return
        recipients = [m for m in mentions if a <= m['start'] and m['end'] <= b]
        if not any(_relation(folded,
                             len(_normalized(text[a:m['start']])[0]),
                             len(_normalized(text[a:m['end']])[0]),
                             local_trigger, local_trigger_end) == 'direct'
                   and (basis == 'explicit_reported_conduct'
                        or not _reporter(folded, len(_normalized(text[a:m['end']])[0])))
                   for m in recipients):
            return
        category = ('sanctions' if basis == 'explicit_administrative_recipient'
                    and ADMIN_ADMONITION.search(folded) else 'reputational')
        yield {"category": category, "stage": stage,
               "subject": entity.get("name", ""), "relationship": "direct",
               "negated": False, "hypothetical": False, "uncertain": uncertain,
               "quote": quote, "start": a, "end": b,
               "trigger_start": trigger_a, "trigger_end": trigger_b,
               "severity": CATEGORY_SEVERITY[category],
               "attribution_basis": basis,
               "context_review_required": True}

    def original_match(paragraph, match):
        _, offsets = _normalized(paragraph.group())
        a, b = _original_span(match.start(), match.end(), offsets, paragraph.group())
        return paragraph.start() + a, paragraph.start() + b

    for index, paragraph in enumerate(paragraphs):
        local = [m for m in mentions if paragraph.start() <= m['start'] < paragraph.end()]
        if not local:
            continue
        folded, _ = _normalized(paragraph.group())
        for mention in local:
            start = len(_normalized(text[paragraph.start():mention['start']])[0])
            end = len(_normalized(text[paragraph.start():mention['end']])[0])
            before, after = folded[:start], folded[end:]
            adverse_grounds = any(not _negated(folded[max(0, ground.start() - 180):ground.start()])
                                  for ground in NOTARIAL_GROUNDS.finditer(folded))
            for match in ADMINISTRATIVE_MEASURE.finditer(folded):
                applies = False
                label = match.group()
                if label.startswith('impuso') and end >= match.end():
                    receiver = folded[match.end():start]
                    applies = bool(re.fullmatch(
                        r"\s+(?:a|al)\s+(?:(?:la|el)\s+)?(?:presidente\s+municipal|"
                        r"alcalde(?:sa)?|notari[oa](?:\s+public[oa])?|funcionari[oa])"
                        r"(?:\s+(?:de|del)\s+[a-zñ -]{1,100})?\s*,?\s*", receiver))
                elif label.startswith('anuncio') and end >= match.end():
                    applies = (bool(re.fullmatch(r"\s+de\s+", folded[match.end():start]))
                               and bool(NOTARIAL_TARGET_APPOSITIVE.match(after)
                                        or re.match(r"\s*,\s*titular\s+de\s+la\s+notaria\b", after))
                               and adverse_grounds)
                elif label.startswith('revocacion') and match.start() >= end:
                    # NAME (Notaría Pública Número ...): Revocación e
                    # inhabilitación is a row, not a reporter's discussion.
                    applies = bool(re.fullmatch(
                        r"\s*\(\s*notaria\s+(?:publica\s+)?(?:numero\s+)?[^()\n]{1,130}"
                        r"\)\s*:\s*(?:resolucion\s+de\s+)?", folded[end:match.start()]))
                elif label.startswith(('ha quedado', 'fue')) and match.start() >= end:
                    applies = (bool(re.fullmatch(r"\s*,?\s*", folded[end:match.start()]))
                               and bool(re.search(r"\bsancion(?:es)?\s+administrativa", folded)))
                elif label.startswith('separar') and start >= match.end():
                    applies = (bool(re.fullmatch(
                        r"(?:\s+titular)?\s+de\s+la\s+notaria\s+(?:publica\s+)?"
                        r"(?:numero\s+)?\d+\s*,[^.!?\n]{0,150}\bal\s+licenciado\s+",
                        folded[match.end():start]))
                               and bool(re.search(r"\bsancion\s+administrativa\b", before)))
                if applies and not NOTARIAL_VOLUNTARY.search(folded):
                    ta, tb = original_match(paragraph, match)
                    yield from emit(paragraph.start(), paragraph.end(), ta, tb, 'sanction',
                                    'explicit_administrative_recipient')

            political = re.search(r"\bincurrio\s+en\s+violencia\s+politica\s+en\s+razon\s+de\s+genero\b", after)
            if (political and political.start() <= 15
                    and re.search(r"\btribunal\b[^.!?\n]{0,150}\bdetermino\s+que\b", before)):
                _, offsets = _normalized(paragraph.group())
                a, b = _original_span(end + political.start(), end + political.end(), offsets, paragraph.group())
                yield from emit(paragraph.start(), paragraph.end(), paragraph.start() + a,
                                paragraph.start() + b, 'sanction', 'explicit_electoral_resolution')

            repression = re.match(r"\s+ordeno\s+[^.!?\n]{0,70}\bla\s+represion\b", after)
            if repression:
                _, offsets = _normalized(paragraph.group())
                a, b = _original_span(end + repression.start(), end + repression.end(), offsets, paragraph.group())
                yield from emit(paragraph.start(), paragraph.end(), paragraph.start() + a,
                                paragraph.start() + b, 'allegation', 'explicit_reported_conduct', True)

            # A professional list heading must be followed exclusively by
            # numbered professional entries, not a new prose paragraph.
            for prior in range(max(0, index - 25), index):
                heading = paragraphs[prior]
                normalized_heading = _fold(heading.group())
                match = PROFESSIONAL_LIST_HEADING.search(normalized_heading)
                if not match or paragraph.end() - heading.start() > 4000:
                    continue
                entries = paragraphs[prior + 1:index + 1]
                if (entries and all(re.match(r"\s*\d{1,3}\.\s+", p.group())
                                    and re.search(r"\bnotari[oa]\b", _fold(p.group())) for p in entries)
                        and re.fullmatch(r"\s*\d{1,3}\.\s*", before)
                        and re.match(r"\s*,\s*notari[oa]\b", after)):
                    ta, tb = original_match(heading, match)
                    yield from emit(heading.start(), paragraph.end(), ta, tb, 'sanction',
                                    'member_of_explicit_numbered_sanction_list')

            # Lists whose grammatical subject is already the sanctioned group.
            # A continuation must explicitly say "Así como a..." and keep the
            # same NAME, notario... structure for every recipient.
            for prior in range(max(0, index - 1), index + 1):
                heading = paragraphs[prior]
                hf = _fold(heading.group())
                match = ACCOUNT_SUSPENSION_LIST.search(hf)
                if (match and paragraph.end() - heading.start() <= 4000
                        and (prior == index or re.match(r"\s*asi\s+como\s+a\s+", folded))
                        and re.match(r"\s*,\s*notari[oa]\s+public[oa]\b", after)
                        and (prior != index or start >= match.end())):
                    ta, tb = original_match(heading, match)
                    yield from emit(heading.start(), paragraph.end(), ta, tb, 'sanction',
                                    'member_of_explicit_professional_account_suspension_list')

            for prior in range(max(0, index - 1), index + 1):
                heading = paragraphs[prior]
                match = ALLEGATION_LIST.search(_fold(heading.group()))
                continuation = bool(re.match(r"\s*otros\s+(?:notarios|funcionarios)\s+mas\s+son\s*,", folded))
                if (match and paragraph.end() - heading.start() <= 3000
                        and (prior == index and start >= match.end() or prior < index and continuation)
                        and not _reporter(folded, end)):
                    ta, tb = original_match(heading, match)
                    yield from emit(heading.start(), paragraph.end(), ta, tb, 'allegation',
                                    'member_of_explicit_accusation_list', True)

            # "Fue suspendido... Se trata del notario NAME" identifies the
            # previous sentence's sole professional recipient explicitly.
            identifier = re.search(r"\bse\s+trata\s+del\s+notario\s+$", before)
            if identifier:
                # An identifying followup can be a new line inside the same
                # paragraph, as well as the next blank-separated paragraph.
                same_paragraph = before[:identifier.start()].strip()
                prior = paragraph if same_paragraph else paragraphs[index - 1] if index else None
                pf = same_paragraph or (_fold(prior.group()) if prior else '')
                match = re.search(r"\bfue\s+suspendido\s+de\s+sus\s+actividades\b", pf)
                if (prior and match and len(pf) <= 600
                        and re.match(r"\s*el\s+titular\s+de\s+la\s+notaria\s+\d+\b", pf)
                        and any(not _negated(pf[max(0, ground.start() - 180):ground.start()])
                                for ground in NOTARIAL_GROUNDS.finditer(pf))):
                    ta, tb = original_match(prior, match)
                    yield from emit(prior.start(), paragraph.end(), ta, tb, 'sanction',
                                    'explicit_identifying_followup_to_professional_suspension')

            # An explicitly continued accusation: no allegation is inherited
            # from a generic preceding investigation or an unrelated headline.
            involved = re.search(r"\binvolucro\s+tambien\s+al\s+notario\s+\d+\s*$", before)
            if involved and index:
                prior = paragraphs[index - 1]
                if (len(prior.group()) <= 1200
                        and re.search(r"\bsegun\s+la\s+denuncia\s+actual\b", _fold(prior.group()))):
                    ta, tb = original_match(paragraph, involved)
                    yield from emit(prior.start(), paragraph.end(), ta, tb, 'allegation',
                                    'explicit_continuation_of_same_reported_complaint', True)

            # A historical summary can introduce a list in the following
            # sentence, or explicitly continue "el año siguiente... la de".
            # Every accepted name must itself have a professional recipient
            # link. An unrelated observer elsewhere in the summary is omitted.
            summary = re.search(r"\bson\s+las\s+del\s+notario\b", before)
            revocations = list(REVOCATION_LIST.finditer(before))
            last_item = re.split(r"[;.!?]", before)[-1]
            professional_receiver = bool(re.search(
                r"\b(?:el\s+)?notario(?:\s+publico)?\s+\d+\s+de\s+[^,;.!?]{1,80},\s*$", last_item))
            if summary and revocations and professional_receiver:
                ta, tb = original_match(paragraph, revocations[-1])
                yield from emit(paragraph.start(), paragraph.end(), ta, tb, 'sanction',
                                'explicit_member_of_enumerated_revocation_summary')
            if index and re.match(r"\s*el\s+año\s+siguiente\s+solo\s+hubo\s+una\s+y\s+fue\s+la\s+del\s+notario\b", folded):
                prior = paragraphs[index - 1]
                pf = _fold(prior.group())
                match = re.match(r"\s*las\s+primeras\s+(revocaciones)\s+se\s+dieron\b", pf)
                recipient = (professional_receiver or bool(re.search(
                    r"\bse\s+registro\s+la\s+de\s*$", before)
                    and re.match(r"\s*,\s*notario\s+publico\s+\d+\b", after)))
                if match and len(prior.group()) <= 700 and len(paragraph.group()) <= 700 and recipient:
                    _, offsets = _normalized(prior.group())
                    a, b = _original_span(match.start(1), match.end(1), offsets, prior.group())
                    yield from emit(prior.start(), paragraph.end(), prior.start() + a,
                                    prior.start() + b, 'sanction', 'explicit_year_continuation_of_revocation_summary')

            institutional_allegation = re.match(
                r"\s*,\s*(?:expresidente\s+[^;.!?]{0,100}\s+y\s+)?relacionad[oa]\s*,\s*"
                r"de\s+acuerdo\s+con\s+(?:oficios|documentos)\s+[^;.!?]{1,160}"
                r"\ben\s+actos\s+de\s+corrupcion\b", after)
            if institutional_allegation:
                _, offsets = _normalized(paragraph.group())
                a, b = _original_span(end + institutional_allegation.start(),
                                     end + institutional_allegation.end(), offsets, paragraph.group())
                yield from emit(paragraph.start(), paragraph.end(), paragraph.start() + a,
                                paragraph.start() + b, 'allegation', 'explicit_document_attributed_corruption_allegation', True)


def _documentary_context_findings(text, entity, mentions):
    """Bounded, explicitly identified recipients; never article-wide transfer.

    A source index can document that a proceeding was instituted, while still
    not providing the underlying resolution or its disposition. This function
    records that narrow role and does not change body quality or identity.
    """
    paragraphs = list(re.finditer(r"[^\r\n]+(?:\r?\n(?![ \t]*\r?\n)[^\r\n]+)*", text))

    def exact(paragraph, match):
        _, offsets = _normalized(paragraph.group())
        a, b = _original_span(match.start(), match.end(), offsets, paragraph.group())
        return paragraph.start() + a, paragraph.start() + b

    def emit(a, b, ta, tb, stage, category, basis, **metadata):
        prefix = _fold(text[max(a, ta - 180):ta])
        if _negated(prefix) or re.search(
                r"\b(?:podria|podrian|solicito|solicita|pidio|pide|propuso|propone|"
                r"exigio|exige)(?:\s+\w+){0,12}\s*$", prefix):
            return
        yield {"category": category, "stage": stage, "subject": entity.get('name', ''),
               "relationship": "direct", "negated": False, "hypothetical": False,
               "uncertain": stage == 'allegation', "quote": text[a:b], "start": a, "end": b,
               "trigger_start": ta, "trigger_end": tb, "severity": CATEGORY_SEVERITY[category],
               "attribution_basis": basis, "context_review_required": True, **metadata}
        if metadata.get('documentary_scope') not in {
                'administrative_measure', 'administrative_procedure_title', 'administrative_arrest_order'}:
            return
        # Only an explicit reference to this same singular measure/proceeding
        # may carry a disposition across the next paragraph. Never infer the
        # outcome of a linked resolution, or use another case in an index.
        resolution_end = b
        following = next((p for p in paragraphs if p.start() >= b), None)
        if (following and following.start() - b <= 4 and len(following.group()) <= 600
                and EXPLICIT_DISPOSITION.match(_fold(following.group()).lstrip())):
            resolution_end = following.end()
        rf, ro = _normalized(text[a:resolution_end])
        for disposition in EXPLICIT_DISPOSITION.finditer(rf):
            local_trigger_end = len(_normalized(text[a:tb])[0])
            intervening = rf[local_trigger_end:disposition.start()]
            if re.search(r"\b(?:otro|otra|nuevo|nueva)\s+(?:procedimiento|expediente|queja|sancion|caso)\b|"
                         r"\b(?:abrio|instauro|inicio)\b[^.!?\n]{0,100}\b(?:procedimiento|expediente|queja)\b",
                         intervening):
                continue
            if _negated(rf[max(0, disposition.start()-80):disposition.start()]):
                continue
            da,db = _original_span(disposition.start(),disposition.end(),ro,text[a:resolution_end])
            yield {"category":category,"stage":"dismissed","subject":entity.get('name',''),
                   "relationship":"direct","negated":False,"hypothetical":False,"uncertain":False,
                   "quote":text[a:resolution_end],"start":a,"end":resolution_end,
                   "trigger_start":a+da,"trigger_end":a+db,"severity":CATEGORY_SEVERITY[category],
                   "attribution_basis":"explicit_same_administrative_proceeding_disposition",
                   "context_review_required":True}

    def recipient(entry, start, end):
        """A named list member with a public-office apposition, in either order."""
        before, after = _fold(entry[:start]), _fold(entry[end:])
        if RECIPIENT_EXCLUSION.search(before):
            return False
        title = (r"(?:(?:ex|entonces)\s+)?" + PUBLIC_RECIPIENT_ROLE +
                 r"(?:\s+(?:municipal|public[oa]|general|de\s+la\s+unidad)){0,3}"
                 r"(?:\s+(?:de|del)\s+[^,;.!?]{1,140})?")
        lead = r"\s*,?\s*(?:(?:y\s+)?(?:a|al|para)\s+)?(?:(?:el|la)\s+)?"
        if re.fullmatch(lead + title + r"\s*,?\s*", before):
            return bool(re.match(r"\s*(?:[.;,]|$)", after))
        return bool(re.fullmatch(lead, before)
                    and re.match(r"\s*,\s*" + title + r"(?:\s*[.;]|\s*$)", after))

    def sole_subject(paragraph, require_role=True):
        local = [m for m in mentions if paragraph.start() <= m['start'] < paragraph.end()]
        if len(local) != 1 or len(paragraph.group()) > 1000:
            return None
        mention = local[0]
        before = _fold(text[paragraph.start():mention['start']])
        after = _fold(text[mention['end']:paragraph.end()])
        if not re.fullmatch(r"\s*(?:(?:mientras\s+que|por\s+su\s+parte)\s*,?\s*)?"
                            r"(?:(?:el|la)\s+(?:ex\s+)?(?:alcalde(?:sa)?|president[ea]\s+municipal)\s+)?", before):
            return None
        if require_role and not (re.match(r"\s*,\s*(?:el\s+|la\s+)?(?:alcalde(?:sa)?|president[ea])\b", after)
                                 or re.search(r"\b(?:alcalde(?:sa)?|president[ea]\s+municipal)\s*$", before)):
            return None
        # Strip only the target's own office apposition before checking the
        # event for another actor. "With the mayor/síndico" is another person
        # even without a full name; two-token names are not invisible either.
        raw_after = text[mention['end']:paragraph.end()]
        apposition = re.match(r"\s*,\s*(?:el\s+|la\s+)?(?:alcalde(?:sa)?|president[ea])\b[^,;.!?]{0,160},",
                              raw_after, re.I)
        event = raw_after[apposition.end():] if apposition else raw_after
        if re.search(r"\b(?:se\s+reunio|acudio|acompañad[oa]|junto\s+con|exedil|exalcalde|exalcaldesa|sindico)\b|"
                     r"\bcon\s+(?:el|la|un|una)\s+(?:alcalde(?:sa)?|president[ea]|funcionari[oa]|director(?:a)?)\b",
                     _fold(event)):
            return None
        # Reject a second explicitly named actor. Institutional titles are not
        # person names; uncertain multi-person antecedents deliberately abstain.
        proper = r"[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+(?:de|del|la|los))?(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,5}"
        for named in re.finditer(proper, event):
            if re.search(r"\b(?:de|del|en)\s+$", event[:named.start()], re.I):
                continue
            if not re.match(r"(?:Partido|Movimiento|Instituto|Gobierno|Secretaría|Consejo|Comisión|Dirección|Unidad|Operación|Seguridad|Obras|Estado)\b", named.group()):
                return None
        if re.search(r"\b(?:segun|informo|explico|declaro|abogad[oa]|testigo|victima)\b", after):
            return None
        return mention

    for index, paragraph in enumerate(paragraphs):
        folded, offsets = _normalized(paragraph.group())
        local = [m for m in mentions if paragraph.start() <= m['start'] < paragraph.end()]
        for mention in local:
            start, end = mention['start'] - paragraph.start(), mention['end'] - paragraph.start()
            fs, fe = len(_normalized(paragraph.group()[:start])[0]), len(_normalized(paragraph.group()[:end])[0])
            for measure in ADMIN_ADMONITION.finditer(folded):
                if measure.end() > fs:
                    continue
                # A semicolon defines one recipient entry. Do not let a later
                # witness/observer inherit the measure heading.
                entry_start = max(folded.rfind(';', measure.end(), fs) + 1, measure.end())
                entry_end = folded.find(';', fe)
                entry_end = len(folded) if entry_end < 0 else entry_end
                entry = folded[entry_start:entry_end]
                if recipient(entry, fs - entry_start, fe - entry_start):
                    ta, tb = exact(paragraph, measure)
                    yield from emit(paragraph.start(), paragraph.end(), ta, tb, 'sanction', 'sanctions',
                                    'explicit_public_admonition_recipient', documentary_scope='administrative_measure')

            for procedure in ADMIN_PROCEDURE.finditer(folded):
                if procedure.end() > fs or len(folded) > 5000:
                    continue
                receiver = folded[procedure.end():fs]
                candidate = (r"\s*(?:del?\s+|de\s+la\s+)?"
                             r"(?:(?:partido(?:\s+politico)?\s+[^,;.!?]{1,100})\s+y\s+)?"
                             r"(?:(?:su|el|la)\s+)?(?:(?:otrora|entonces)\s+)?"
                             r"candidat[oa](?:\s+comun)?\s+(?:a\s+la|al?\s+(?:cargo\s+de\s+)?)\s+"
                             r"(?:presidencia\s+municipal|diputacion(?:\s+(?:local|federal))?|"
                             r"gubernatura|senaduria|presidencia\s+de\s+la\s+republica)"
                             r"(?:\s+[^,;.!?]{1,100})?\s*,\s*")
                if (RECIPIENT_EXCLUSION.search(receiver)
                        or not (not receiver.strip() or re.fullmatch(candidate, receiver))):
                    continue
                if not re.search(r"\b(?:resolucion|consejo\s+general|instituto|tribunal)\b", folded[:procedure.start()]):
                    continue
                ta, tb = exact(paragraph, procedure)
                yield from emit(paragraph.start(), paragraph.end(), ta, tb, 'investigation', 'sanctions',
                                'explicit_instituted_administrative_procedure',
                                documentary_scope='administrative_procedure_title',
                                procedural_outcome='not_inferred', underlying_resolution_read=False)

            for recall in POLITICAL_RECALL.finditer(folded):
                if recall.end() != fs:
                    continue
                if re.match(r"\s*['’]s\b", folded[fe:]):
                    continue
                prefix = folded[max(0, recall.start() - 100):recall.start()]
                if re.search(r"\b(?:not|never|no|may|might|would|could)\b(?:\W+\w+){0,8}\W*$", prefix):
                    continue
                ta, tb = exact(paragraph, recall)
                yield from emit(paragraph.start(), paragraph.end(), ta, tb, 'allegation', 'reputational',
                                'explicit_filed_political_recall_petition',
                                documentary_scope='political_governance_controversy',
                                criminal_conduct_reported=False, removal_from_office_established=False)

            # A heading and nothing but bounded recipient paragraphs may form
            # one list. A new prose paragraph ends the list immediately.
            for prior_index in range(max(0, index - 12), index):
                heading = paragraphs[prior_index]
                hf = _fold(heading.group())
                measure = ADMIN_ADMONITION.search(hf)
                if (not measure or paragraph.end() - heading.start() > 4000
                        or not re.fullmatch(r"\s*,?\s*a\s+los\s+siguientes\s+"
                                            r"(?:servidores\s+publicos(?:\s+y\s+ex\s+servidores\s+publicos)?)\s*:\s*",
                                            hf[measure.end():])):
                    continue
                entries = paragraphs[prior_index + 1:index + 1]
                if not all(len(p.group()) <= 350 and re.match(
                        r"\s*(?:al|a\s+la)\s+(?:entonces\s+)?" + PUBLIC_RECIPIENT_ROLE + r"\b", _fold(p.group()))
                        and not RECIPIENT_EXCLUSION.search(_fold(p.group())) for p in entries):
                    continue
                if recipient(paragraph.group(), start, end):
                    ta, tb = exact(heading, measure)
                    yield from emit(heading.start(), paragraph.end(), ta, tb, 'sanction', 'sanctions',
                                    'explicit_public_admonition_recipient_list', documentary_scope='administrative_measure')

            if index:
                prior = paragraphs[index - 1]
                pf = _fold(prior.group())
                order = ADMIN_ORDER.search(pf)
                link = re.match(r"\s*uno\s+de\s+ellos\s+fue\s+para\s+", folded)
                if (order and link and len(pf) <= 650 and len(folded) <= 1500
                        and fs >= link.end() and recipient(folded[link.end():], fs-link.end(), fe-link.end())):
                    ta, tb = exact(prior, order)
                    yield from emit(prior.start(), paragraph.end(), ta, tb, 'sanction', 'sanctions',
                                    'explicit_recipient_of_administrative_arrest_order',
                                    documentary_scope='administrative_arrest_order', detention_executed=False)

        if not index:
            continue
        prior = paragraphs[index - 1]
        previous = sole_subject(prior)
        fine = re.match(r"\s*este\s+ultimo\s+(fue\s+multad[oa])\b", folded)
        if (previous and fine and len(folded) <= 1200
                and re.search(r"\b(?:resolucion|transparencia|solicitud\s+de\s+informacion)\b", folded)):
            a, b = _original_span(fine.start(1), fine.end(1), offsets, paragraph.group())
            yield from emit(prior.start(), paragraph.end(), paragraph.start()+a, paragraph.start()+b,
                            'sanction', 'sanctions', 'explicit_sole_subject_last_reference',
                            documentary_scope='administrative_measure')

        previous = sole_subject(prior, require_role=False)
        role_link = re.match(r"\s*(?:al\s+exedil|a\s+la\s+exedil|al\s+exalcalde|a\s+la\s+exalcaldesa)\s+(se\s+le\s+acusa)\b", folded)
        if not previous or not role_link or len(folded) > 1800:
            continue
        previous_text = _fold(text[previous['end']:prior.end()])
        arrest = re.match(r"\s+fue\s+(detenid[oa])\b", previous_text)
        if not arrest or _negated(folded[:role_link.end(1)-len(role_link.group(1))]):
            continue
        clause = _fold(next(iter(_sentences(paragraph.group(), [])))[2])
        categories = [c for c,p in CATEGORY_PATTERNS.items() if any(
            not _negated(clause[max(0,m.start()-100):m.start()]) for m in re.finditer(p,clause))]
        for category in categories:
            a,b = _original_span(role_link.start(1),role_link.end(1),offsets,paragraph.group())
            yield from emit(prior.start(),paragraph.end(),paragraph.start()+a,paragraph.start()+b,
                            'allegation',category,'explicit_same_arrested_officeholder_accusation')
            _, prior_offsets = _normalized(prior.group())
            pos = len(_normalized(text[prior.start():previous['end']])[0])
            a,b = _original_span(pos+arrest.start(1),pos+arrest.end(1),prior_offsets,prior.group())
            yield from emit(prior.start(),paragraph.end(),prior.start()+a,prior.start()+b,
                            'arrest',category,'explicit_named_arrest_offence_continuation')


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
        notarial_matches = [match for pattern in (NOTARIAL_MEASURE, NOTARIAL_RECEIVED_MEASURE)
                            for match in pattern.finditer(normalized)
                            if _notarial_measure_applies(normalized, match, sentence, sentence_start, local_mentions)]
        stages.extend(("sanction", match) for match in notarial_matches)
        stages.extend(("investigation", match) for match in
                      _post_release_process_matches(normalized, sentence, sentence_start, local_mentions))
        if not stages:
            continue
        contrasts = list(re.finditer(r"\b(?:pero|aunque|sin embargo)\b|\b[ye]\s+(?=(?:fue|sigue|continua|esta|es|ha|permanece|resulto|quedo|acusad|investigad|detenid|absuelt))", normalized))
        for stage, match in stages:
            left = max((split.end() for split in contrasts if split.end() <= match.start()), default=0)
            right = min((split.start() for split in contrasts if split.start() >= match.end()), default=len(normalized))
            category_scope = normalized[left:right]
            categories = [category for category, pattern in CATEGORY_PATTERNS.items()
                          if any(not _negated(category_scope[max(0,match.end()-left,m.start()-100):m.start()])
                                 for m in re.finditer(pattern, category_scope))] or ["reputational"]
            trigger_a, trigger_b = _original_span(match.start(), match.end(), offsets, sentence)
            mention = min(local_mentions, key=lambda m: abs(m["start"] - sentence_start - trigger_a))
            # Convert original offsets to folded offsets for syntactic rules.
            local_a = mention["start"] - sentence_start
            local_b = mention["end"] - sentence_start
            folded_a = len(_normalized(sentence[:local_a])[0])
            folded_b = len(_normalized(sentence[:local_b])[0])
            relationship = _relation(normalized, folded_a, folded_b, match.start(), match.end())
            if (relationship == 'direct' and _fold(entity.get('kind', 'person')) in COMPANY_KINDS
                    and _corporate_context(normalized, folded_a, folded_b, match.start(), match.end())):
                relationship = 'context'
            if (relationship == "direct" and _reporter(normalized, folded_b)
                    and match not in notarial_matches):
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
                    r"\b(?:podria|podrian|sera|seria|serian|seran|sea|sean|fuera|fueran|fuese|fuesen|solicita|solicito|pide|pidio|exigio|exige|preve|posibles?|eventuales?|riesgo de)(?:\s+\w+){0,8}\s*$", prefix))
                # A date in the later acquittal clause must never date the
                # earlier investigation merely because both share a sentence.
                date_left = max(left, max((m.end() for _,m in stages if m.end() <= match.start()),default=0))
                date_right = min(right, min((m.start() for _,m in stages if m.start() >= match.end()),default=len(normalized)))
                clause_start = sentence_start + (offsets[date_left] if date_left < len(offsets) else len(sentence))
                clause_end = sentence_start + (offsets[date_right] if date_right < len(offsets) else len(sentence))
                sentence_dates = [d for d in dates if clause_start <= d["start"] and d['end'] <= clause_end]
                if len(sentence_dates) == 1:
                    finding["event_date"] = sentence_dates[0]["iso_date"]
                    finding["event_date_basis"] = "single_date_in_quote_requires_review"
                elif len(sentence_dates) > 1:
                    finding["event_date_ambiguous"] = True
                signature = (category, stage, relationship, negated, sentence_start)
                if not any((f["category"], f["stage"], f["relationship"], f["negated"], f["start"]) == signature for f in findings):
                    findings.append(finding)
    for finding in _structured_findings(text, entity, mentions):
        if not any(f['stage'] == finding['stage'] and f['relationship'] == 'direct'
                   and f['trigger_start'] == finding['trigger_start'] for f in findings):
            findings.append(finding)
    for finding in _documentary_context_findings(text, entity, mentions):
        # A directly bound continuation can supply the missing offence for
        # this same arrest trigger. Replace only its generic fallback, never
        # unrelated events or substantive categories elsewhere in the article.
        if finding['category'] != 'reputational':
            findings = [f for f in findings if not (
                f['category'] == 'reputational' and f['stage'] == finding['stage']
                and f['relationship'] == 'direct' and f['trigger_start'] == finding['trigger_start'])]
        if not any(f['stage'] == finding['stage'] and f['category'] == finding['category']
                   and f['relationship'] == 'direct' and f['trigger_start'] == finding['trigger_start'] for f in findings):
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
