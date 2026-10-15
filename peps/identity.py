"""Indexed, conservative identity attribution for a local Spanish entity bank.

This module uses no network, fuzzy match, third-party model, or inference of
guilt. ``matched`` means a rule found corroborating local identity attributes;
it is not a verified legal identity, a probability, or a finding of wrongdoing.
Every result remains reviewable. Names alone never produce ``matched``.

Entity aliases and context are bank assertions, not facts learned from news.
All optional input fields and string aliases are unverified hints by default.
Structured entries can record ``name``, ``verified``/``status``, ``source`` and
validity dates. Explicit verification AND provenance are required for anchors.
``context.verified_fields`` and ``context.verified_aliases`` support reviewed
bank attributes; unverified hints cannot establish a match. Update the bank
and rebuild the resolver to apply corrections. Instances are read-only after
construction and can be shared by worker threads.
"""
from __future__ import annotations

import copy
from bisect import bisect_left, bisect_right
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

MODEL_VERSION = "es-mx-identity-indexed-1.0.0"
CONFIDENCE_KIND = "deterministic_heuristic_not_probability"
MAX_ALIAS_TOKENS = 32
MAX_COMPETITOR_IDS = 100
CONTEXT_RADIUS = 160
COMPANY_KINDS = frozenset({"company", "organization", "organisation", "empresa",
                           "sociedad", "organizacion", "institucion", "legal_entity"})
STOP = frozenset({"de", "del", "la", "las", "el", "los", "y", "e", "the", "and"})
GENERIC_CONTEXT = frozenset({"mexico", "mexicano", "mexicana", "republica mexicana",
                             "gobierno", "empresa", "persona", "ciudadano", "director"})
GENERIC_RFCS = frozenset({"XAXX010101000", "XEXX010101000"})
CONTEXT_BARRIERS = re.compile(
    r"\b(?:dijo|afirmo|informo|explico|denuncio|acuso|viajo|visito|consulto|sobre|contra|"
    r"reunion con|reunio con|hablo con|hablo sobre|converso con|entrevisto a|entrevisto al|"
    r"acompaño a|acompaño al|saludo a|recibio a|presento a|homonimo|homonima|confundirse|"
    r"distinto de|distinto del|distinta de|sin relacion|no es|no fue|nunca fue|"
    r"adquirio|adquirida|adquirido|compro|vendio|cedio|titularidad|propiedad de|de otra|de otro)\b")
LINK_WORDS = frozenset({"a", "al", "de", "del", "el", "la", "las", "los", "en", "y", "e", "como",
    "es", "fue", "era", "se", "desempeña", "funge", "trabaja", "actual", "actualmente", "ex",
    "anterior", "con", "numero", "rfc", "nacio", "nacido", "nacida", "nacimiento", "años"})
RFC_PATTERN = re.compile(r"(?<!\w)[A-ZÑ&]{3,4}\d{6}[A-Z\d]{3}(?!\w)", re.I)
TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)
LEGAL_SUFFIXES = tuple(sorted((
    ("s", "a", "b", "de", "c", "v"), ("s", "a", "p", "i", "de", "c", "v"),
    ("s", "de", "r", "l", "de", "c", "v"), ("s", "a", "de", "c", "v"),
    ("s", "de", "rl", "de", "cv"), ("sa", "de", "cv"), ("sab", "de", "cv"),
    ("sapi", "de", "cv"), ("s", "de", "rl"), ("s", "de", "r", "l"),
    ("sociedad", "anonima", "de", "capital", "variable"),
    ("sociedad", "anonima"), ("s", "a"), ("s", "a", "b"), ("sa",), ("sab",),
    ("s", "c"), ("sc",), ("a", "c"), ("ac",),
), key=len, reverse=True))
GROUP_KEYS = {
    "geography": ("state", "city", "jurisdiction", "jurisdictions", "municipality", "estado", "municipio"),
    "role": ("position", "role", "occupation", "roles", "cargo"),
    "organization": ("organization", "organizations", "employer", "agency"),
    "birth_date": ("birth_date", "date_of_birth", "fecha_nacimiento"),
    "age": ("age", "edad"),
}
RULES_HASH = hashlib.sha256(json.dumps({"version": MODEL_VERSION,
    "groups": GROUP_KEYS, "suffixes": LEGAL_SUFFIXES, "radius": CONTEXT_RADIUS,
    "confidence_kind": CONFIDENCE_KIND, "barriers": CONTEXT_BARRIERS.pattern,
    "generic_context": sorted(GENERIC_CONTEXT), "generic_rfcs": sorted(GENERIC_RFCS),
    "rfc_pattern": RFC_PATTERN.pattern, "max_alias_tokens": MAX_ALIAS_TOKENS,
    "link_words": sorted(LINK_WORDS),
    "max_competitor_ids": MAX_COMPETITOR_IDS, "verified_policy": "explicit_with_provenance",
    "context_groups_required": 2, "rfc_label_required": True, "body_anchor_required": True},
    sort_keys=True).encode()).hexdigest()


def _fold(value: str) -> str:
    # Preserve the Spanish ñ, including its decomposed Unicode spelling.
    normalized = unicodedata.normalize("NFC", str(value)).lower()
    return "".join("ñ" if c == "ñ" else "".join(
        x for x in unicodedata.normalize("NFD", c) if not unicodedata.combining(x))
        for c in normalized)


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int


def _tokens(text: str) -> list[_Token]:
    chars, offsets = [], []
    for match in re.finditer(r".[\u0300-\u036f]*", text, re.S):
        for char in _fold(match.group()):
            chars.append(char)
            offsets.append((match.start(), match.end()))
    folded = "".join(chars)
    tokens = [_Token(m.group(), offsets[m.start()][0], offsets[m.end() - 1][1])
              for m in TOKEN_PATTERN.finditer(folded)]
    merged, i = [], 0
    while i < len(tokens):
        j = i + 1
        if len(tokens[i].value) == 1:
            while (j < len(tokens) and len(tokens[j].value) == 1 and
                   re.fullmatch(r"\.\s*", text[tokens[j - 1].end:tokens[j].start])):
                j += 1
        if j > i + 1:
            end = tokens[j - 1].end
            if text[end:end + 1] == ".":
                end += 1
            merged.append(_Token("".join(t.value for t in tokens[i:j]), tokens[i].start, end))
        else:
            merged.append(tokens[i])
        i = j
    return merged


def _sequence(value: str) -> tuple[str, ...]:
    return tuple(t.value for t in _tokens(str(value)))


def normalize_name(value: str) -> str:
    """Comparable token spelling, preserving ñ; this never proves identity."""
    return " ".join(_sequence(str(value)))


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, (list, tuple)) else [value]


def _iso(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def _validity(value: dict[str, Any], published: str | None) -> bool | None:
    begin = _iso(value.get("valid_from") or value.get("start_date") or value.get("from"))
    end = _iso(value.get("valid_to") or value.get("end_date") or value.get("to"))
    if not (begin or end) or not published:
        return None
    return not ((begin and published < begin) or (end and published > end))


def _dated_without_reference(details: dict[str, Any], published: str | None) -> bool:
    return not published and any(_iso(details.get(key)) for key in
        ("valid_from", "start_date", "from", "valid_to", "end_date", "to"))


def _trusted(value: dict[str, Any]) -> bool:
    return not (value.get("validated") is False or value.get("verified") is False or
                str(value.get("status", "")).lower() in
                {"pending", "unvalidated", "rejected", "revoked", "unverified"})


def _verification(context: dict[str, Any], key: str, details: dict[str, Any] | None = None,
                  explicit: bool = False) -> dict[str, Any]:
    """Verification is an explicit reviewed assertion with recorded provenance.

    A bare true, RFC syntax, search query, publisher name, or list of field
    names is insufficient. Provenance is recorded, not fetched or certified.
    """
    entry = details or {}
    source = entry.get("source") or entry.get("provenance")
    verified = explicit or entry.get("verified") is True or entry.get("validated") is True or str(
        entry.get("status", "")).lower() in {"verified", "validated", "confirmed"}
    verified_fields = context.get("verified_fields", {})
    field = verified_fields.get(key) if isinstance(verified_fields, dict) else None
    if isinstance(verified_fields, list):
        field = next((v for v in verified_fields if isinstance(v, dict)
                      and (v.get("field") or v.get("key")) == key), None)
        if field is None and key in verified_fields:
            provenance = context.get("provenance") or context.get("field_sources") or {}
            if isinstance(provenance, dict) and provenance.get(key):
                field = {"verified": True, "source": provenance[key]}
    if isinstance(field, dict) and _trusted(field):
        field_verified = field.get("verified") is True or field.get("validated") is True or str(
            field.get("status", "")).lower() in {"verified", "validated", "confirmed"}
        if field_verified and (field.get("source") or field.get("provenance")):
            verified, source = True, field.get("source") or field.get("provenance")
    source_valid = ((isinstance(source, str) and len(source.strip()) >= 3) or
        (isinstance(source, dict) and any(source.get(k) for k in
            ("url", "uri", "id", "reference", "record", "file", "sha256", "review_id", "source_id", "source"))))
    return {"verified": bool(verified and source_valid and _trusted(entry)),
            "source": copy.deepcopy(source) if source_valid else None}


def entity_signature(entity: dict[str, Any]) -> str:
    """Stable bank signature; IDs remain distinct even for identical names."""
    data = {key: entity.get(key) for key in ("id", "name", "kind", "aliases", "context")}
    data["id"] = "" if data.get("id") is None else str(data["id"]).strip()
    data["context"] = data.get("context") or {}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
        default=str, separators=(",", ":")).encode()).hexdigest()


def entity_for_analysis(entity: dict[str, Any], resolution: dict[str, Any]) -> dict[str, Any]:
    """Return an isolated NLP entity with only accepted, observed body aliases.

    The caller must also verify ``content_hash`` against the actual body.
    Unmatched or stale resolutions never introduce observed variants.
    """
    result = copy.deepcopy(entity)
    if (resolution.get("status") != "matched" or str(entity.get("id")) != str(resolution.get("entity_id"))
            or entity_signature(entity) != resolution.get("entity_signature")):
        return result
    aliases = []
    for mention in resolution.get("mentions", []):
        value = mention.get("text")
        if (isinstance(value, str) and value.strip() and isinstance(mention.get("start"), int)
                and isinstance(mention.get("end"), int) and 0 <= mention["start"] < mention["end"]
                and mention.get("identity_supported") is True):
            aliases.append(value)
    result["aliases"] = list(dict.fromkeys(aliases))
    return result


@dataclass(frozen=True)
class _Variant:
    entity_id: str
    name: str
    sequence: tuple[str, ...]
    source: str
    trusted: bool
    details: dict[str, Any]
    verification: dict[str, Any]
    corporate_core: bool = False


class IdentityResolver:
    """Build an exact token trie once; lookup cost depends on document tokens.

    ``metadata`` may supply ``published_at`` to check dated bank roles. Query
    names, requested entity IDs, publisher locations and any other metadata
    never count as corroboration. ``mentions`` always address the original
    body in Python Unicode character offsets; title spans are kept separately.
    """

    def __init__(self, entities: Iterable[dict[str, Any]]):
        self._entities: dict[str, dict[str, Any]] = {}
        self._signatures: dict[str, str] = {}
        self._profiles: dict[str, list[dict[str, Any]]] = {}
        self._variants: list[_Variant] = []
        self._trie: dict = {}
        self._rfcs: dict[str, set[str]] = {}
        self._entity_rfcs: dict[str, set[str]] = {}
        self._rfc_verifications: dict[str, dict[str, dict[str, Any]]] = {}
        self._nodes = 1
        self._max_tokens = 0
        self._skipped = 0
        for original in entities:
            entity = copy.deepcopy(original)
            entity_id = "" if entity.get("id") is None else str(entity["id"]).strip()
            if not entity_id or not str(entity.get("name", "")).strip():
                raise ValueError("Cada entidad requiere id y nombre no vacíos")
            if entity_id in self._entities:
                raise ValueError("ID de entidad duplicado: " + entity_id)
            entity["id"] = entity_id
            entity["context"] = entity.get("context") or {}
            self._entities[entity_id] = entity
            self._signatures[entity_id] = entity_signature(entity)
            self._profiles[entity_id] = self._profile(entity)
            self._rfc_verifications[entity_id] = self._identifiers(entity)
            self._entity_rfcs[entity_id] = set(self._rfc_verifications[entity_id])
            for rfc in self._entity_rfcs[entity_id]:
                self._rfcs.setdefault(rfc, set()).add(entity_id)
            self._add_entity_variants(entity)
        self.stats = {"entity_count": len(self._entities), "variant_count": len(self._variants),
                      "trie_nodes": self._nodes, "rfc_count": len(self._rfcs),
                      "max_alias_tokens": self._max_tokens, "skipped_long_aliases": self._skipped,
                      "model_version": MODEL_VERSION, "rules_hash": RULES_HASH}

    @staticmethod
    def _identifiers(entity: dict[str, Any]) -> dict[str, dict[str, Any]]:
        context = entity["context"]
        candidates = [context.get("rfc")]
        identifiers = context.get("identifiers", {})
        if isinstance(identifiers, dict):
            candidates.extend(_values(identifiers.get("rfc") or identifiers.get("RFC")))
        elif isinstance(identifiers, list):
            candidates.extend(i for i in identifiers if isinstance(i, dict)
                              and str(i.get("type", "")).lower() == "rfc" and _trusted(i))
        found = {}
        for value in candidates:
            for item in _values(value):
                details = item if isinstance(item, dict) else {}
                if isinstance(item, dict):
                    if not _trusted(item):
                        continue
                    item = item.get("value", "")
                rfc = re.sub(r"[\s-]", "", str(item or "")).upper()
                if RFC_PATTERN.fullmatch(rfc) and rfc not in GENERIC_RFCS:
                    verification = _verification(context, "rfc", details)
                    company = _fold(entity.get("kind", "person")) in COMPANY_KINDS
                    verification["format_compatible"] = len(rfc) == (12 if company else 13)
                    verification["verified"] = verification["verified"] and verification["format_compatible"]
                    found[rfc] = verification
        return found

    @staticmethod
    def _profile(entity: dict[str, Any]) -> list[dict[str, Any]]:
        context, profile = entity["context"], []
        for group, keys in GROUP_KEYS.items():
            for key in keys:
                for raw in _values(context.get(key)):
                    details = raw if isinstance(raw, dict) else {}
                    value = (details.get("name") or details.get("value") or details.get("title")
                             or details.get("role")) if details else raw
                    if value and _trusted(details):
                        seq = _sequence(str(value))
                        if seq and " ".join(seq) not in GENERIC_CONTEXT and (len("".join(seq)) >= 4 or group == "age"):
                            profile.append({"group": group, "key": key, "value": str(value),
                                            "sequence": seq, "details": details,
                                            "verification": _verification(context, key, details)})
                    if group == "role" and details:
                        for nested, nested_group in (("organization", "organization"),
                                                      ("jurisdiction", "geography")):
                            for item in _values(details.get(nested)):
                                seq = _sequence(str(item))
                                if seq and " ".join(seq) not in GENERIC_CONTEXT:
                                    profile.append({"group": nested_group, "key": key + "." + nested,
                                        "value": str(item), "sequence": seq, "details": details,
                                        "verification": _verification(context, key + "." + nested, details)})
        return profile

    def _add_entity_variants(self, entity: dict[str, Any]) -> None:
        variants = [(entity["name"], "canonical_name")]
        variants.extend((a, "alias") for a in _values(entity.get("aliases")))
        context = entity["context"]
        for key in ("trade_names", "business_names", "legal_name", "legal_names", "abbreviations"):
            variants.extend((a, key) for a in _values(context.get(key)))
        verified_aliases = context.get("verified_aliases", [])
        if isinstance(verified_aliases, dict):
            verified_aliases = [{"name": name, "source": source, "verified": True}
                                if not isinstance(source, dict) else {"name": name, **source}
                                for name, source in verified_aliases.items()]
        variants.extend((a, "verified_aliases") for a in _values(verified_aliases))
        seen = set()
        company = _fold(entity.get("kind", "person")) in COMPANY_KINDS
        for raw, source in variants:
            details = raw if isinstance(raw, dict) else {}
            name = (details.get("name") or details.get("value") or details.get("alias")) if details else raw
            if not name:
                continue
            seq = _sequence(str(name))
            if not seq:
                continue
            if len(seq) > MAX_ALIAS_TOKENS:
                self._skipped += 1
                continue
            forms = [(seq, False)]
            if company:
                for suffix in LEGAL_SUFFIXES:
                    if len(seq) > len(suffix) and seq[-len(suffix):] == suffix:
                        forms.append((seq[:-len(suffix)], True))
                        break
            for sequence, core in forms:
                verification = _verification(context, source, details, explicit=source == "verified_aliases")
                signature = (sequence, _trusted(details), verification["verified"],
                             json.dumps(details, sort_keys=True, default=str))
                if signature in seen:
                    continue
                seen.add(signature)
                variant = _Variant(entity["id"], str(name), sequence, source,
                                   _trusted(details), details, verification, core)
                index = len(self._variants)
                self._variants.append(variant)
                node = self._trie
                for token in sequence:
                    if token not in node:
                        node[token] = {}
                        self._nodes += 1
                    node = node[token]
                node.setdefault(None, []).append(index)
                self._max_tokens = max(self._max_tokens, len(sequence))

    def _lookup(self, text: str, part: str) -> tuple[list[dict[str, Any]], int, int]:
        tokens, hits, steps = _tokens(text), [], 0
        for i, token in enumerate(tokens):
            node = self._trie
            for j in range(i, min(len(tokens), i + self._max_tokens)):
                steps += 1
                current = tokens[j]
                if j > i:
                    gap = text[tokens[j - 1].end:current.start]
                    if not re.fullmatch(r"[\s,'’&\-]*", gap) or "\n\n" in gap:
                        break
                node = node.get(current.value)
                if node is None:
                    break
                for variant_id in node.get(None, ()):
                    variant = self._variants[variant_id]
                    hits.append({"entity_id": variant.entity_id, "start": token.start,
                                 "end": current.end, "text": text[token.start:current.end],
                                 "part": part, "variant_id": variant_id})
        # Keep the longest overlapping variant for each entity, preserving every
        # separate occurrence. Distinct IDs are never coalesced by name/hash.
        kept, last_end = [], {}
        for hit in sorted(hits, key=lambda h: (h["start"], -(h["end"] - h["start"]), h["entity_id"],
                not self._variants[h["variant_id"]].verification["verified"], h["variant_id"])):
            if last_end.get(hit["entity_id"], -1) >= hit["end"]:
                continue
            kept.append(hit)
            last_end[hit["entity_id"]] = hit["end"]
        return kept, len(tokens), steps

    @staticmethod
    def _scope(text: str, hit: dict[str, Any], bounds: tuple[list[int], list[int]]) -> tuple[int, int]:
        start, end = hit["start"], hit["end"]
        left, right = max(0, start - CONTEXT_RADIUS), min(len(text), end + CONTEXT_RADIUS)
        before, after = text[left:start], text[end:right]
        def separators(value: str):
            for match in re.finditer(r"[.!?;\n]|\b(?:mientras(?: que)?|por su parte|en cambio)\b", value, re.I):
                # Periods in initials and corporate abbreviations are not
                # sentence boundaries; dots between words still are.
                if match.group() == "." and re.search(r"\b[A-Za-zÁÉÍÓÚÑáéíóúñ]\.$", value[:match.end()]):
                    continue
                yield match
        boundaries = list(separators(before))
        if boundaries:
            left += boundaries[-1].end()
        boundary = next(separators(after), None)
        if boundary:
            right = end + boundary.start()
        starts, ends = bounds
        previous = bisect_right(ends, start) - 1
        following = bisect_left(starts, end)
        if previous >= 0:
            left = max(left, ends[previous])
        if following < len(starts):
            right = min(right, starts[following])
        return left, right

    def _support(self, text: str, hit: dict[str, Any], bounds: tuple[list[int], list[int]], published: str | None) -> dict[str, Any]:
        variant = self._variants[hit["variant_id"]]
        a, b = self._scope(text, hit, bounds)
        window = _tokens(text[a:b])
        values, attributes, expired = [t.value for t in window], [], []
        link_words = set(LINK_WORDS)
        link_words.update(token for expected in self._profiles[hit["entity_id"]] for token in expected["sequence"])
        if _fold(self._entities[hit["entity_id"]].get("kind", "person")) in COMPANY_KINDS:
            link_words.update(token for suffix in LEGAL_SUFFIXES for token in suffix)
        def linked(begin: int, end: int) -> bool:
            if begin < hit["end"] and end > hit["start"]:
                return False
            between = text[min(end, hit["end"]):max(begin, hit["start"])]
            # Only a directly attached nominal/copular identity phrase can
            # donate anchors. Arbitrary predicates and another subject's
            # attributes are refused without needing a catalogue of verbs.
            return (not CONTEXT_BARRIERS.search(_fold(between)) and
                    all(t.value in link_words for t in _tokens(between)))
        for expected in self._profiles[hit["entity_id"]]:
            seq = expected["sequence"]
            for i in range(len(values) - len(seq) + 1):
                if tuple(values[i:i + len(seq)]) != seq:
                    continue
                begin, end = a + window[i].start, a + window[i + len(seq) - 1].end
                between = text[min(end, hit["end"]):max(begin, hit["start"])]
                # Context on another subject, reported speech or travel cannot
                # establish the subject's jurisdiction/employer/occupation.
                if not linked(begin, end):
                    continue
                if re.search(r"\b(?:no|nunca|jamas)\s+(?:(?:es|fue|ha|sido|trabaja|pertenece|ejerce|el|la)\s+){0,4}$",
                             _fold(text[max(a, begin - 50):begin])):
                    continue
                if expected["group"] == "birth_date" and not re.search(
                        r"\b(?:nacio|nacido|nacida|nacimiento)\b", _fold(text[max(a, begin - 35):begin])):
                    continue
                if expected["group"] == "age" and not re.match(r"\s*años\b", _fold(text[end:end + 10])):
                    continue
                attribute = {"group": expected["group"], "key": expected["key"],
                             "value": expected["value"], "start": begin, "end": end,
                             "text": text[begin:end], "part": hit["part"],
                             "date_compatible": _validity(expected["details"], published),
                             "verified": expected["verification"]["verified"] and not _dated_without_reference(expected["details"], published),
                             "date_requires_review": _dated_without_reference(expected["details"], published),
                             "provenance": expected["verification"]["source"]}
                (expired if attribute["date_compatible"] is False else attributes).append(attribute)
                break
        local_rfcs = set()
        for match in RFC_PATTERN.finditer(text[a:b]):
            begin, end = a + match.start(), a + match.end()
            between = text[min(end, hit["end"]):max(begin, hit["start"])]
            labeled = re.search(r"\brfc\s*(?:numero\s*)?[:#–-]?\s*$", _fold(text[max(a, begin - 20):begin]))
            if not labeled or len(between) > 70 or not linked(begin, end):
                continue
            local_rfcs.add(match.group().upper())
        local_rfcs -= GENERIC_RFCS
        matched_rfcs = sorted(local_rfcs & self._entity_rfcs[hit["entity_id"]])
        conflicting_rfcs = sorted(local_rfcs - self._entity_rfcs[hit["entity_id"]]) if self._entity_rfcs[hit["entity_id"]] else []
        verified_rfcs = sorted(r for r in matched_rfcs if self._rfc_verifications[hit["entity_id"]][r]["verified"])
        verified_rfc_conflict = bool(conflicting_rfcs and any(
            v["verified"] for v in self._rfc_verifications[hit["entity_id"]].values()))
        meaningful = [v for v in variant.sequence if v not in STOP]
        short = len(meaningful) < 2 or len("".join(meaningful)) <= 4
        alias_validity = _validity(variant.details, published)
        trusted = variant.trusted and alias_validity is not False and not _dated_without_reference(variant.details, published)
        groups = sorted({item["group"] for item in attributes})
        verified_groups = sorted({item["group"] for item in attributes if item["verified"]})
        # A geographic keyword alone (e.g. a place visited) is never sufficient.
        contextual = len(groups) >= 2 and ("role" in groups or "organization" in groups)
        company = _fold(self._entities[hit["entity_id"]].get("kind", "person")) in COMPANY_KINDS
        verified_context = contextual and (len(verified_groups) >= 2 or
            (company and variant.verification["verified"] and bool(verified_groups)))
        canonical_reference = variant.source == "canonical_name" or variant.verification["verified"]
        identity_negated = bool(re.search(r"\b(?:no (?:es|fue)|no se trata de|distint[oa] de|homonim[oa] de)\s*$",
                                         _fold(text[max(a, hit["start"] - 45):hit["start"]])))
        strong = trusted and canonical_reference and not identity_negated and not conflicting_rfcs and (bool(verified_rfcs) or verified_context)
        return {"hit": hit, "groups": groups, "attributes": attributes, "expired_attributes": expired,
                "matched_rfcs": matched_rfcs, "conflicting_rfcs": conflicting_rfcs,
                "verified_rfcs": verified_rfcs, "verified_rfc_conflict": verified_rfc_conflict,
                "identifier_attributes": [{"type": "rfc", "value": r,
                    "verified": self._rfc_verifications[hit["entity_id"]][r]["verified"],
                    "provenance": self._rfc_verifications[hit["entity_id"]][r]["source"],
                    "format_compatible": self._rfc_verifications[hit["entity_id"]][r]["format_compatible"]}
                    for r in matched_rfcs],
                "verified_groups": verified_groups, "alias_verified": variant.verification["verified"],
                "alias_provenance": variant.verification["source"],
                "explicit_identity_negation": identity_negated,
                "short_or_acronym": short, "trusted_alias": trusted, "alias_date_compatible": alias_validity,
                "strong": strong, "scope": {"start": a, "end": b, "part": hit["part"]}}

    def resolve(self, text: str, title: str = "", metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        text, title = str(text or ""), str(title or "")
        published = _iso((metadata or {}).get("published_at"))
        body_hits, body_tokens, body_steps = self._lookup(text, "body")
        title_hits, title_tokens, title_steps = self._lookup(title, "title")
        hits = body_hits + title_hits
        bounds = {part: (sorted({h["start"] for h in hits if h["part"] == part}),
                         sorted({h["end"] for h in hits if h["part"] == part})) for part in ("body", "title")}
        supports = [self._support(text if h["part"] == "body" else title, h, bounds[h["part"]], published) for h in hits]
        span_groups: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
        for support in supports:
            h = support["hit"]
            span_groups.setdefault((h["part"], h["start"], h["end"]), []).append(support)
        # Long/short overlapping aliases form one competition group. A token
        # trie has few such spans per position; identical-name banks use a
        # single aggregate instead of pairwise entity comparisons.
        aggregates = {}
        cluster, cluster_end, cluster_part = [], -1, ""
        def complete_cluster():
            entries = [entry for key in cluster for entry in span_groups[key]]
            aggregate = ({s["hit"]["entity_id"] for s in entries},
                         {s["hit"]["entity_id"] for s in entries if s["strong"]})
            aggregate = (*aggregate, sorted(aggregate[0])[:MAX_COMPETITOR_IDS + 1])
            for key in cluster:
                aggregates[key] = aggregate
        for key in sorted(span_groups):
            if cluster and (key[0] != cluster_part or key[1] >= cluster_end):
                complete_cluster()
                cluster = []
            cluster.append(key)
            cluster_end = max(cluster_end, key[2]) if key[0] == cluster_part else key[2]
            cluster_part = key[0]
        if cluster:
            complete_cluster()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for support in supports:
            grouped.setdefault(support["hit"]["entity_id"], []).append(support)
        identifier_only: dict[str, set[str]] = {}
        for content in (text, title):
            for match in RFC_PATTERN.finditer(content):
                rfc = match.group().upper()
                for entity_id in self._rfcs.get(rfc, ()):
                    if entity_id not in grouped:
                        identifier_only.setdefault(entity_id, set()).add(rfc)
        results = []
        for entity_id in sorted(set(grouped) | set(identifier_only)):
            items = grouped.get(entity_id, [])
            competing, strong_items, competing_count = set(), [], 0
            for item in items:
                hit = item["hit"]
                all_ids, strong_ids, sample_ids = aggregates[(hit["part"], hit["start"], hit["end"])]
                competing.update(i for i in sample_ids if i != entity_id)
                competing_count = max(competing_count, len(all_ids) - (entity_id in all_ids))
                strong_rivals = len(strong_ids) - (entity_id in strong_ids)
                # Shared RFCs do not break a tie between duplicate legal names.
                if item["strong"] and not strong_rivals and hit["part"] == "body":
                    strong_items.append(item)
            if strong_items:
                status, confidence = "matched", .97 if any(i["verified_rfcs"] for i in strong_items) else .90
                reasons = ["Nombre/alias del cuerpo y anclas explícitamente verificadas con procedencia concuerdan localmente."]
            elif items and all(i["verified_rfc_conflict"] for i in items):
                status, confidence = "rejected", .05
                reasons = ["Atributos locales identifican un homónimo o el RFC contradice el registro."]
            elif competing_count:
                hint_rank = max((.10 * len(i["groups"]) + .15 * bool(i["matched_rfcs"])
                                 - .10 * bool(i["conflicting_rfcs"]) for i in items), default=0)
                status, confidence = "ambiguous", round(max(.15, min(.70, .35 + hint_rank)), 3)
                reasons = ["La misma mención corresponde a varios registros; falta corroboración exclusiva."]
            else:
                hint_rank = max((.10 * len(i["groups"]) + .15 * bool(i["matched_rfcs"])
                                 - .10 * bool(i["conflicting_rfcs"]) for i in items), default=0)
                status, confidence = "candidate", round(max(.25, min(.75, .50 + hint_rank)), 3) if items else .40
                reasons = ["Coincidencia nominal/identificador; los datos opcionales son pistas hasta verificarse con procedencia."]
            if items and all(i["short_or_acronym"] for i in items) and status != "matched":
                confidence = min(confidence, .25)
                reasons.append("Nombre corto, sigla o núcleo comercial: requiere contexto discriminante.")
            if any(not i["trusted_alias"] for i in items):
                reasons.append("Alias sin validar, revocado o fuera de su intervalo; no establece identidad.")
            if any(i["expired_attributes"] for i in items):
                reasons.append("Se excluyeron cargos/atributos incompatibles con la fecha de publicación.")
            if any(i["conflicting_rfcs"] and not i["verified_rfc_conflict"] for i in items):
                reasons.append("RFC del artículo difiere de una pista no verificada; no se presupone correcto el dato de entrada.")
            if identifier_only.get(entity_id):
                reasons.append("RFC del banco presente, pero sin nombre/alias corroborado en el fragmento.")
            entity = self._entities[entity_id]
            signature = self._signatures[entity_id]
            mentions, title_mentions = [], []
            for item in items:
                hit, variant = item["hit"], self._variants[item["hit"]["variant_id"]]
                mention = {"start": hit["start"], "end": hit["end"], "text": hit["text"],
                           "entity_id": entity_id, "matched_alias": variant.name,
                           "alias_source": variant.source, "corporate_core": variant.corporate_core,
                           "alias_validated": item["alias_verified"], "alias_provenance": item["alias_provenance"],
                           "identity_supported": item in strong_items}
                (mentions if hit["part"] == "body" else title_mentions).append(mention)
            results.append({"entity_id": entity_id, "status": status, "confidence": confidence,
                "mentions": sorted(mentions, key=lambda m: m["start"]), "reasons": reasons,
                "features": {"confidence_kind": CONFIDENCE_KIND, "review_required": status != "matched",
                    "scoring_requires_human_review": True,
                    "body_mentions": len(mentions), "title_mentions": title_mentions,
                    "competing_entity_count_lower_bound": max(competing_count, len(competing)),
                    "competing_entity_ids_truncated": competing_count > MAX_COMPETITOR_IDS or len(competing) > MAX_COMPETITOR_IDS,
                    "supports": [{k: v for k, v in item.items() if k != "hit"} for item in items],
                    "identifier_only_rfcs": sorted(identifier_only.get(entity_id, ())),
                    "published_at": published, "rules_hash": RULES_HASH,
                    "lookup": {"tokens": body_tokens + title_tokens, "trie_steps": body_steps + title_steps,
                               "candidate_entities": len(grouped) + len(identifier_only), "mention_hits": len(hits)},
                    "limitations": ["Reglas exactas; sin correferencia, fuzzy, verificación externa ni inferencia de culpabilidad.",
                        "matched necesita revisión humana antes de validar un riesgo reputacional."]},
                "competing_entity_ids": sorted(competing)[:MAX_COMPETITOR_IDS], "entity_signature": signature,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "model_version": MODEL_VERSION})
        return results
