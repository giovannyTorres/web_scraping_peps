"""Literal public-directory assertions, distinct from human identity review.

An assertion records what a captured directory listed at its observation date.
It certifies neither legal identity, current entitlement to practise, nor an
uninterrupted appointment. Source bytes must be checked at extraction/import;
the small retained proof binds the imported assertion and its literal record.
Its digest detects stale/edited inputs, not a forged publisher or forged user.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION = 1
METHOD = "literal_public_directory_record_v1"
ADAPTERS = {"cdmx_notary_html": "1", "nuevo_leon_municipal_pdf": "1"}
FIELDS = frozenset({"role", "state", "municipality", "notary_number", "office"})


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _spaces(value):
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _equal(a, b):
    return _spaces(a).casefold() == _spaces(b).casefold()


def _observed(value):
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise ValueError("La captura requiere observed_at ISO con zona horaria") from None
    if instant.tzinfo is None or instant > datetime.now(timezone.utc):
        raise ValueError("La observación no puede carecer de zona ni estar en el futuro")
    return instant.astimezone(timezone.utc).isoformat()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.data, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.data.append(data)


def _text(html):
    parser = _Text()
    parser.feed(html)
    return _spaces(" ".join(parser.data))


def _source_allowed(url, adapter):
    parsed = urlsplit(str(url))
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        return False
    if parsed.port not in {None, 443}:
        return False
    if adapter == "cdmx_notary_html":
        return parsed.hostname.lower() in {"colegiodenotarios.org.mx", "www.colegiodenotarios.org.mx"} and parsed.path.rstrip("/") == "/directorio"
    if adapter == "nuevo_leon_municipal_pdf":
        return parsed.hostname.lower() in {"nl.gob.mx", "www.nl.gob.mx"} and bool(re.search(
            r"/\d{8}_directorio_alcaldias\.pdf$", unquote(parsed.path)))
    return False


def _records(raw, adapter):
    if adapter == "cdmx_notary_html":
        html = raw.decode("utf-8")
        title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html, re.I | re.S)
        if not title or not _equal(_text(title.group(1)), "Colegio de Notarios de la Ciudad de México"):
            raise ValueError("La captura no tiene el encabezado del directorio CDMX")
        heads = list(re.finditer(r'<div\b[^>]*class=["\']nombre_dir["\'][^>]*>(.*?)</div\s*>', html, re.I | re.S))
        for ordinal, match in enumerate(heads, 1):
            heading = _text(match.group(1))
            fields = re.fullmatch(r"(Notario|Notaria) (.*?) - Notaría No\. (\d{1,5})", heading)
            if not fields or "VACANTE" in fields.group(2).upper():
                continue
            end = heads[ordinal].start() if ordinal < len(heads) else len(html)
            row_html = html[match.start():end]
            municipality = re.search(r"Alcaldía:\s*(.*?)\s*C\.P\.", _text(row_html))
            if not municipality:
                continue
            record = heading + "\nAlcaldía: " + municipality.group(1) + "\nCiudad de México"
            yield {"text": record, "name": fields.group(2), "role": fields.group(1),
                   "notary_number": fields.group(3), "municipality": municipality.group(1), "state": "Ciudad de México",
                   "locator": {"row_ordinal": ordinal, "raw_start": match.start(), "raw_end": end,
                               "raw_offset_kind": "UTF-8_decoded_Unicode_characters",
                               "raw_record_sha256": hashlib.sha256(row_html.encode()).hexdigest()}}
    elif adapter == "nuevo_leon_municipal_pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("Extraer esta captura PDF requiere el paquete open source opcional pypdf") from None
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("No se extraen directorios PDF cifrados")
        header = "Directorio de Alcaldías en Nuevo León"
        page_texts = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
        if not any(header.casefold() in _spaces(t).casefold() for t in page_texts):
            raise ValueError("La captura PDF no corresponde al directorio NL")
        for page, text in enumerate(page_texts, 1):
            for match in re.finditer(r"Presidencia Municipal de ([^\r\n]+)\s*\n\s*Responsable:\s*([^\r\n]+)", text):
                municipality, responsible = map(_spaces, match.groups())
                record = header + "\nPresidencia Municipal de " + municipality + "\nResponsable: " + responsible
                yield {"text": record, "responsible": responsible, "municipality": municipality,
                       "office": "Presidencia Municipal", "state": "Nuevo León",
                       "locator": {"page": page, "page_text_start": match.start(), "page_text_end": match.end(),
                                   "page_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                                   "raw_offset_kind": "pypdf_layout_Unicode_characters"}}


@lru_cache(maxsize=4)
def _parsed_records(raw, adapter):
    # Parse one public snapshot once for a full roster. Callers receive fresh
    # proof copies and cannot edit the cached records through a public API.
    return tuple(_records(raw, adapter))


def _span(text, value):
    # Requiring exact source characters prevents invented accent expansions,
    # inferred aliases, substring names and numbers belonging to another row.
    match = re.search(r"(?<!\w)" + re.escape(str(value)) + r"(?!\w)", text, re.I)
    if not match:
        raise ValueError("El valor no tiene una cita literal completa en el registro")
    return {"start": match.start(), "end": match.end(), "text": text[match.start():match.end()]}


def _record_matches(entity, record):
    name = record.get("name")
    if name is not None:
        return _equal(name, entity["name"])
    responsible = record.get("responsible", "")
    target = _spaces(entity["name"])
    if not responsible.casefold().endswith(target.casefold()):
        return False
    prefix = responsible[:-len(target)].strip()
    return bool(not prefix or re.fullmatch(r"(?:(?:C|P|Lic|Ing|Dr|Dra|Mtro|Mtra|M|V|Z)\.\s*)+", prefix, re.I))


def documented_profile(entity, *, raw, source_url, source_sha256, observed_at, adapter):
    """Copy one entity and add checked directory assertions; never reviews.

    No URL is requested. The caller retains the captured bytes, whose expected
    hash must originate in its acquisition receipt. Existing hints are retained
    when a source field does not match literally. There is no inferred tenure.
    """
    if not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024 or not raw:
        raise ValueError("La captura debe ser bytes y medir entre 1 byte y 16 MiB")
    if not re.fullmatch(r"[a-f0-9]{64}", str(source_sha256)) or hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError("La captura no corresponde al hash de procedencia")
    if adapter not in ADAPTERS or not _source_allowed(source_url, adapter):
        raise ValueError("Fuente o adaptador documental no autorizado")
    if not isinstance(entity, dict) or not entity.get("id") or not str(entity.get("name", "")).strip():
        raise ValueError("La entidad requiere id y nombre")
    observed_at = _observed(observed_at)
    candidates = [r for r in _parsed_records(raw, adapter) if _record_matches(entity, r)]
    if len(candidates) != 1:
        raise ValueError("El directorio debe contener exactamente un registro completo para esta entidad")
    record = candidates[0]
    name_span = _span(record["text"], _spaces(entity["name"]))
    result = copy.deepcopy(entity)
    context = result.setdefault("context", {})
    proofs = context.setdefault("documented_fields", {})
    if not isinstance(proofs, dict):
        raise ValueError("documented_fields debe ser un objeto")
    for field in FIELDS & record.keys() & context.keys():
        value = context[field]
        if isinstance(value, (dict, list)) or not _equal(value, record[field]):
            continue
        literal = _span(record["text"], str(record[field]))
        proof = {"schema_version": SCHEMA_VERSION, "method": METHOD,
                 "entity_id": str(entity["id"]), "entity_name": str(entity["name"]),
                 "field": field, "value": copy.deepcopy(value), "human_review": False,
                 "source": {"url": source_url, "sha256": source_sha256, "observed_at": observed_at,
                            "adapter": adapter, "adapter_version": ADAPTERS[adapter], "locator": record["locator"],
                            "extracted_record_sha256": hashlib.sha256(record["text"].encode()).hexdigest()},
                 "record": {"text": record["text"], "name_span": name_span, "field_span": literal},
                 "temporal_scope": {"kind": "source_listing_at_observation_only", "observed_date": observed_at[:10]},
                 "limitations": "La captura acredita la etiqueta del directorio en su observación; no identidad legal, vigencia de patente, continuidad histórica ni revisión humana."}
        proof["assertion_sha256"] = _digest(proof)
        proofs[field] = proof
    return result


def validate_documented_field(entity, field, value, proof):
    """Validate retained binding, literal spans, source policy and date scope.

    Actual raw-byte authenticity is checked by documented_profile at import;
    call documented_profile again with retained raw bytes to audit it later.
    This function does not contact/certify a publisher or a human reviewer.
    """
    try:
        if (not isinstance(proof, dict) or proof.get("schema_version") != SCHEMA_VERSION
                or proof.get("method") != METHOD or proof.get("human_review") is not False
                or field not in FIELDS or proof.get("field") != field or proof.get("value") != value
                or proof.get("entity_id") != str(entity.get("id")) or proof.get("entity_name") != entity.get("name")):
            return None
        digest = proof.get("assertion_sha256")
        if digest != _digest({k: v for k, v in proof.items() if k != "assertion_sha256"}):
            return None
        source, record = proof["source"], proof["record"]
        adapter = source["adapter"]
        if (adapter not in ADAPTERS or source["adapter_version"] != ADAPTERS[adapter]
                or not _source_allowed(source["url"], adapter)
                or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])):
            return None
        instant = _observed(source["observed_at"])
        if proof["temporal_scope"] != {"kind": "source_listing_at_observation_only", "observed_date": instant[:10]}:
            return None
        text = record["text"]
        if not isinstance(text, str) or len(text) > 2000:
            return None
        if source["extracted_record_sha256"] != hashlib.sha256(text.encode()).hexdigest():
            return None
        if adapter == "cdmx_notary_html":
            match = re.fullmatch(r"(Notario|Notaria) (.*?) - Notaría No\. (\d{1,5})\nAlcaldía: ([^\n]+)\nCiudad de México", text)
            if not match:
                return None
            source_fields = {"role": match.group(1), "name": match.group(2), "notary_number": match.group(3),
                             "municipality": match.group(4), "state": "Ciudad de México"}
            locator = source["locator"]
            if (type(locator["row_ordinal"]) is not int or locator["row_ordinal"] < 1
                    or type(locator["raw_start"]) is not int or type(locator["raw_end"]) is not int
                    or not 0 <= locator["raw_start"] < locator["raw_end"]
                    or not re.fullmatch(r"[a-f0-9]{64}", locator["raw_record_sha256"])):
                return None
        else:
            match = re.fullmatch(r"Directorio de Alcaldías en Nuevo León\nPresidencia Municipal de ([^\n]+)\nResponsable: ([^\n]+)", text)
            if not match or not _record_matches(entity, {"responsible": match.group(2)}):
                return None
            source_fields = {"name": entity["name"], "municipality": match.group(1),
                             "state": "Nuevo León", "office": "Presidencia Municipal"}
            locator = source["locator"]
            if (type(locator["page"]) is not int or locator["page"] < 1
                    or type(locator["page_text_start"]) is not int or type(locator["page_text_end"]) is not int
                    or not 0 <= locator["page_text_start"] < locator["page_text_end"]
                    or not re.fullmatch(r"[a-f0-9]{64}", locator["page_text_sha256"])):
                return None
        if not _equal(source_fields["name"], entity["name"]) or field not in source_fields or not _equal(source_fields[field], value):
            return None
        for key, target in (("name_span", entity["name"]), ("field_span", value)):
            span = record[key]
            if (type(span["start"]) is not int or type(span["end"]) is not int
                    or not 0 <= span["start"] < span["end"] <= len(text)
                    or text[span["start"]:span["end"]] != span["text"] or not _equal(span["text"], target)):
                return None
        return copy.deepcopy(proof)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
