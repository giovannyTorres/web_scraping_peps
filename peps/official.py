"""Streaming import of public official list snapshots; no network or identity verdict.

Only legacy OFAC XML, UNSC XML and SAT 69-B CSV are supported. Other schemas
fail explicitly. Listing is an administrative fact at the snapshot date, never
a criminal conviction or a positive identification of a same-name input entity.
"""
from __future__ import annotations

import csv
import hashlib
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET


class OfficialImportError(ValueError):
    """An input cannot be imported without guessing its official schema."""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _fold(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(value))
                            if not unicodedata.combining(c)).casefold().split())


def _value(node, name):
    return next((" ".join("".join(child.itertext()).split()) for child in node
                 if _local(child.tag) == name), "")


def _values(node, name):
    return list(dict.fromkeys(" ".join("".join(child.itertext()).split())
                             for child in node.iter() if _local(child.tag) == name
                             and "".join(child.itertext()).strip()))


def _date(value, ofac=False):
    if not value:
        return ""
    try:
        if ofac:
            return datetime.strptime(value, "%m/%d/%Y").date().isoformat()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        # Preserve the source value separately; never invent a date.
        return ""


def _check_file(path, max_bytes=500_000_000, xml=False):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > max_bytes:
        raise OfficialImportError("Snapshot exceeds configured size limit")
    checksum, tail, total = hashlib.sha256(), b"", 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total += len(chunk)
            if total > max_bytes:
                raise OfficialImportError("Snapshot exceeds configured size limit")
            checksum.update(chunk)
            if xml:
                # Handle UTF-16/32 declarations as well as UTF-8 without parsing
                # any entities. Match over chunk boundaries too.
                probe = (tail + chunk).replace(b"\x00", b"").upper()
                if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                    raise OfficialImportError("DTD and entity declarations are not allowed")
                tail = chunk[-64:]
    if not total:
        raise OfficialImportError("Empty snapshot")
    return {"file_sha256": checksum.hexdigest(), "file_bytes": total}


def _xml_records(path, expected_root, record_tags):
    _check_file(path, xml=True)
    stack, snapshot = [], ""
    stream = Path(path).open("rb")
    try:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = _local(element.tag)
            if event == "start":
                if not stack:
                    if tag != expected_root:
                        raise OfficialImportError("Unsupported XML root: " + tag)
                    snapshot = element.attrib.get("dateGenerated", "")
                stack.append(element)
                continue
            if tag == "Publish_Date":
                snapshot = (element.text or "").strip()
            if tag in record_tags:
                # Remove finished records from the parent to avoid keeping an
                # unbounded tree of empty nodes during large snapshot imports.
                parent = stack[-2] if len(stack) > 1 else None
                if parent is not None:
                    parent.remove(element)
                stack.pop()
                yield element, snapshot
                element.clear()
            else:
                stack.pop()
    except ET.ParseError as exc:
        raise OfficialImportError("Malformed XML: " + str(exc)) from exc
    finally:
        stream.close()


def iter_ofac_xml(path, list_name="sdn"):
    """Yield legacy sdnList records with namespace-independent field parsing.

    list_name must identify SDN versus non-SDN; non-SDN programs have differing
    measures and must not be described as uniformly blocking all assets.
    """
    if list_name not in {"sdn", "non_sdn"}:
        raise OfficialImportError("OFAC list_name must be sdn or non_sdn")
    for node, snapshot in _xml_records(path, "sdnList", {"sdnEntry"}):
        record_id = _value(node, "uid")
        name = " ".join(filter(None, (_value(node, "firstName"), _value(node, "lastName"))))
        if not record_id or not name:
            raise OfficialImportError("OFAC entry lacks uid or name")
        aliases = []
        for alias in node.iter():
            if _local(alias.tag) == "aka":
                alias_name = " ".join(filter(None, (_value(alias, "firstName"), _value(alias, "lastName"))))
                if alias_name and alias_name != name:
                    aliases.append(alias_name)
        identifiers = []
        for identifier in node.iter():
            if _local(identifier.tag) == "id":
                number = _value(identifier, "idNumber")
                if number:
                    identifiers.append({"type": _value(identifier, "idType"), "value": number,
                                        "country": _value(identifier, "idCountry")})
        yield {"source": "ofac", "list_name": list_name, "record_id": record_id,
               "name": name, "kind": "person" if _value(node, "sdnType").casefold() == "individual" else "organization",
               "source_entity_type": _value(node, "sdnType"),
               "aliases": list(dict.fromkeys(aliases)), "programs": _values(node, "program"),
               "identifiers": identifiers, "snapshot_date": _date(snapshot, ofac=True),
               "snapshot_date_raw": snapshot, "status": "listed_at_snapshot",
               "legal_nature": "administrative_sanctions_listing", "is_criminal_conviction": False,
               "identity_verified": False}


def iter_un_xml(path):
    """Yield individuals and entities from the UN consolidated legacy XML."""
    for node, snapshot in _xml_records(path, "CONSOLIDATED_LIST", {"INDIVIDUAL", "ENTITY"}):
        individual = _local(node.tag) == "INDIVIDUAL"
        parts = ["FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME"] if individual else ["FIRST_NAME"]
        name = " ".join(filter(None, (_value(node, part) for part in parts)))
        record_id = _value(node, "DATAID")
        if not record_id or not name:
            raise OfficialImportError("UN entry lacks DATAID or name")
        yield {"source": "un", "list_name": "consolidated", "record_id": record_id,
               "reference_number": _value(node, "REFERENCE_NUMBER"), "name": name,
               "kind": "person" if individual else "organization",
               "aliases": list(dict.fromkeys(_values(node, "ALIAS_NAME"))),
               "programs": _values(node, "UN_LIST_TYPE"), "identifiers": [],
               "snapshot_date": _date(snapshot), "snapshot_date_raw": snapshot,
               "listed_on": _value(node, "LISTED_ON"), "status": "listed_at_snapshot",
               "legal_nature": "security_council_sanctions_listing", "is_criminal_conviction": False,
               "identity_verified": False}


def _sat_status(value):
    folded = _fold(value)
    statuses = {
        "presunto": "presumed", "presuntos": "presumed",
        "definitivo": "definitive", "definitivos": "definitive",
        "desvirtuado": "rebutted", "desvirtuados": "rebutted",
        "sentencia favorable": "favorable_judgment", "sentencias favorables": "favorable_judgment",
        "con sentencia favorable": "favorable_judgment",
    }
    return statuses.get(folded, "unknown")


def iter_sat69b_csv(path, encoding=None):
    """Read SAT CSV incrementally, retaining fiscal procedural statuses.

    Recognizes the official RFC, name and contributor-status headers, with a
    bounded preamble and comma/semicolon/tab delimiters. Unknown status remains
    unknown. It is never silently promoted to definitive.
    """
    _check_file(path)
    if encoding is None:
        with Path(path).open("rb") as binary:
            sample = binary.read(65536)
        try:
            sample.decode("utf-8-sig")
            encoding = "utf-8-sig"
        except UnicodeDecodeError:
            encoding = "cp1252"
    with Path(path).open("r", encoding=encoding, newline="") as stream:
        headers = None
        name_keys = {"nombredelcontribuyente", "nombrecontribuyente", "nombre", "razonsocial", "denominacionorazonsocial"}
        status_keys = {"situaciondelcontribuyente", "situacioncontribuyente", "situacion", "estatus", "estado"}
        # Recognize the delimiter from a real schema header, not punctuation
        # frequency: corporate names may contain many quoted commas.
        for delimiter in (",", ";", "\t"):
            stream.seek(0)
            reader = csv.reader(stream, delimiter=delimiter)
            for _ in range(30):
                row = next(reader, None)
                if row is None:
                    break
                keys = [_fold(column).replace(" ", "").replace("\ufeff", "") for column in row]
                if "rfc" in keys and any(key in name_keys for key in keys):
                    if len(set(keys)) != len(keys):
                        raise OfficialImportError("Duplicate normalized CSV columns")
                    headers = row
                    rfc_index = keys.index("rfc")
                    name_index = next(index for index, key in enumerate(keys) if key in name_keys)
                    status_index = next((index for index, key in enumerate(keys) if key in status_keys), None)
                    if status_index is None:
                        raise OfficialImportError("SAT CSV lacks contributor status column")
                    break
            if headers is not None:
                break
        if headers is None:
            raise OfficialImportError("SAT CSV lacks recognizable RFC and name headers")
        for row in reader:
            if not any(value.strip() for value in row):
                continue
            if len(row) != len(headers):
                raise OfficialImportError("Malformed SAT row at line " + str(reader.line_num))
            name, rfc = row[name_index].strip(), row[rfc_index].strip().upper()
            if not name or not rfc:
                raise OfficialImportError("SAT row lacks RFC or name at line " + str(reader.line_num))
            status_raw = row[status_index].strip()
            raw = dict(zip(headers, row))
            record_id = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
            yield {"source": "sat69b", "list_name": "article_69b", "record_id": record_id,
                   "name": name, "kind": "unknown", "aliases": [], "programs": ["CFF 69-B"],
                   "identifiers": [{"type": "RFC", "value": rfc, "country": "MX"}],
                   "status": _sat_status(status_raw), "status_raw": status_raw,
                   "snapshot_date": "", "snapshot_date_raw": "", "raw_fields": raw,
                   "legal_nature": "fiscal_administrative_record", "is_criminal_conviction": False,
                   "identity_verified": False}


def official_record_document(record, source_url, file_sha256="", retrieved_at=None):
    """Convert one structured list record to the bank's document input format.

    The URL must identify the actual imported official resource. Input identity
    matching still happens separately and requires human corroboration.
    """
    parts = urlsplit(source_url)
    allowed = {
        "ofac": {"ofac.treasury.gov", "sanctionslist.ofac.treas.gov", "sanctionslistservice.ofac.treas.gov", "www.treasury.gov"},
        "un": {"main.un.org", "scsanctions.un.org", "www.un.org"},
        "sat69b": {"www.sat.gob.mx", "wwwmat.sat.gob.mx", "omawww.sat.gob.mx"},
    }
    if parts.scheme != "https" or parts.hostname not in allowed.get(record.get("source"), set()) or parts.username or parts.password or parts.port not in {None, 443}:
        raise OfficialImportError("Source URL must be an approved official HTTPS resource")
    name, source = record["name"], record["source"]
    stamp = record.get("snapshot_date") or "fecha de publicación no identificada"
    if source == "sat69b":
        status = record.get("status")
        if status in {"rebutted", "favorable_judgment"}:
            title = "Actualización fiscal favorable: " + name
            text = f"{name}. El SAT registra una actualización fiscal favorable. Situación publicada: {record.get('status_raw', '')}."
        else:
            title = "SAT: registro fiscal del artículo 69-B para " + name
            text = f"{name} figura en el registro fiscal del SAT, artículo 69-B. Situación publicada: {record.get('status_raw', '')}."
    else:
        label = "OFAC " + record.get("list_name", "") if source == "ofac" else "Consejo de Seguridad ONU"
        title = f"{label}: registro {record['record_id']} de {name}"
        text = f"{name} figura en una lista de sanciones de {label}, según la copia de {stamp}. Registro: {record['record_id']}. Programas: {', '.join(record.get('programs', []))}."
    if record.get("aliases"):
        text += " Nombres alternativos registrados: " + "; ".join(record["aliases"]) + "."
    metadata = {"official_record": record, "source_snapshot_sha256": file_sha256,
                "interpretation": "List membership at snapshot; no criminal conviction or verified input identity inferred.",
                "record_document_key": f"{source}:{record.get('list_name')}:{record['record_id']}:{file_sha256}"}
    return {"url": source_url, "title": title, "text": text,
            "source_kind": "official", "source_id": source + "_" + record.get("list_name", ""),
            "published_at": record.get("snapshot_date") or None,
            "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(),
            **metadata, "metadata": metadata}


def load_official_file(path, source, on_record=None, **options):
    """Validate and consume a snapshot, returning explicit complete/partial status.

    on_record processes each record without materializing the whole list. A
    partial result must not be treated as a successful complete refresh.
    """
    count, checksum = 0, {}
    try:
        checksum = _check_file(path, xml=source in {"ofac", "un"})
        parsers = {"ofac": iter_ofac_xml, "un": iter_un_xml, "sat69b": iter_sat69b_csv}
        if source not in parsers:
            raise OfficialImportError("Unsupported official source: " + str(source))
        for record in parsers[source](path, **options):
            if on_record:
                on_record(record)
            count += 1
        if count == 0:
            raise OfficialImportError("No records parsed; refusing to report a successful empty refresh")
        return {"status": "ok", "complete": True, "source": source, "records": count, **checksum}
    except FileNotFoundError as exc:
        return {"status": "missing", "complete": False, "source": source, "records": 0, "error": str(exc)}
    except (OfficialImportError, UnicodeError, csv.Error, OSError, TypeError) as exc:
        return {"status": "partial" if count else "invalid", "complete": False,
                "source": source, "records": count, "error": str(exc), **checksum}
