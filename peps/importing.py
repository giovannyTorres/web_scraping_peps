"""Importación local explícita, previsualización sin escrituras y confirmación atómica.

SHA-256 vincula contenido, mapeo, opciones y banco. El CSV no se persiste.
Los datos importados son pistas del usuario, nunca anclas verificadas.
"""
import csv
import io
import json
import re
import unicodedata
from contextlib import nullcontext
from datetime import date, datetime

from .store import canonical, digest

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 128
MAX_FIELD = 64_000
FIELDS = ('ignore', 'name', 'given_name', 'first_surname', 'second_surname',
          'kind', 'id', 'aliases', 'role', 'state', 'municipality', 'city',
          'country', 'rfc', 'age', 'birth_date', 'organization', 'legal_name',
          'trade_names', 'abbreviations', 'context_note')
CONTEXT_FIELDS = set(FIELDS) - {'ignore', 'name', 'given_name', 'first_surname',
                              'second_surname', 'kind', 'id', 'aliases'}
KINDS = {'person', 'company', 'organization'}
IMPORT_VERSION = 'guided-entity-import/1'


class ImportConflict(ValueError):
    """La confirmación no corresponde a la vista previa o al banco actual."""


def _fold(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value).strip().casefold())
                   if not unicodedata.combining(c))


_KIND_NAMES = {
    'person': 'person', 'persona': 'person', 'persona fisica': 'person',
    'fisica': 'person', 'individual': 'person',
    'company': 'company', 'empresa': 'company', 'persona moral': 'company',
    'moral': 'company', 'sociedad': 'company',
    'organization': 'organization', 'organisation': 'organization',
    'organizacion': 'organization', 'institucion': 'organization',
}
_HEADERS = {
    'nombre': 'name', 'name': 'name', 'nombre completo': 'name',
    'nombre persona input': 'name', 'nombre persona': 'name',
    'nombres': 'given_name', 'given name': 'given_name',
    'primer apellido': 'first_surname', 'apellido paterno': 'first_surname',
    'segundo apellido': 'second_surname', 'apellido materno': 'second_surname',
    'tipo': 'kind', 'tipo entidad': 'kind', 'tipo de entidad': 'kind', 'kind': 'kind',
    'id': 'id', 'identificador': 'id', 'alias': 'aliases', 'aliases': 'aliases',
    'cargo': 'role', 'puesto': 'role', 'role': 'role', 'estado': 'state',
    'state': 'state', 'municipio': 'municipality', 'municipality': 'municipality',
    'ciudad': 'city', 'city': 'city', 'pais': 'country', 'country': 'country',
    'rfc': 'rfc', 'edad': 'age', 'age': 'age',
    'fecha nacimiento': 'birth_date', 'fecha de nacimiento': 'birth_date',
    'birth date': 'birth_date', 'organizacion': 'organization',
    'institucion': 'organization', 'organization': 'organization',
    'razon social': 'legal_name', 'legal name': 'legal_name',
    'nombre comercial': 'trade_names', 'trade names': 'trade_names',
    'siglas': 'abbreviations', 'abbreviations': 'abbreviations',
    'nota': 'context_note', 'notas': 'context_note', 'contexto': 'context_note',
    'context note': 'context_note',
}


def _bool(payload, key, default):
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(key + ' debe ser booleano')
    return value


def _options(payload):
    if not isinstance(payload, dict):
        raise ValueError('La importación debe ser un objeto')
    mode = payload.get('mode', 'csv')
    if mode not in ('csv', 'single'):
        raise ValueError('mode debe ser csv o single')
    delimiter = payload.get('delimiter', 'auto')
    if delimiter not in ('auto', ',', ';', '\t', '|'):
        raise ValueError('Separador CSV no admitido')
    skip = payload.get('skip_rows', 0)
    if isinstance(skip, bool) or not isinstance(skip, int) or not 0 <= skip <= 100:
        raise ValueError('skip_rows debe ser un entero de 0 a 100 líneas iniciales')
    default_kind = payload.get('default_kind') or None
    if default_kind is not None and (not isinstance(default_kind, str) or default_kind not in KINDS):
        raise ValueError('Seleccione un tipo de entidad válido')
    kind_values = payload.get('kind_values', {})
    if not isinstance(kind_values, dict) or len(kind_values) > 1000:
        raise ValueError('kind_values debe ser un objeto de hasta 1000 equivalencias')
    for value, kind in kind_values.items():
        if not isinstance(value, str) or len(value) > 300 or not isinstance(kind, str) or kind not in KINDS:
            raise ValueError('Equivalencia de tipo no válida')
    policy = payload.get('existing_policy', 'skip')
    if policy not in ('skip', 'update'):
        raise ValueError('existing_policy debe ser skip o update')
    separator = payload.get('aliases_separator', '|')
    if separator not in ('|', ';', ','):
        raise ValueError('Separador de alias no admitido')
    date_format = payload.get('date_format', 'auto')
    if date_format not in ('auto', 'iso', 'dmy'):
        raise ValueError('date_format debe ser auto, iso o dmy')
    return dict(mode=mode, delimiter=delimiter, has_header=_bool(payload, 'has_header', True),
                skip_rows=skip, default_kind=default_kind, kind_values=kind_values,
                skip_invalid=_bool(payload, 'skip_invalid', False), existing_policy=policy,
                aliases_separator=separator, date_format=date_format)


def _parse_csv(payload, options):
    text = payload.get('text', '')
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_BYTES:
        raise ValueError('El CSV debe contener texto UTF-8 de hasta 16 MiB')
    if '\x00' in text:
        raise ValueError('El CSV contiene caracteres nulos; guárdelo como UTF-8')
    source_hash = digest(text)
    stream = io.StringIO(text.lstrip('\ufeff'), newline='')
    for _ in range(options['skip_rows']):
        if not stream.readline():
            break
    remaining = stream.read()
    delimiter = options['delimiter']
    if delimiter == 'auto':
        try:
            delimiter = csv.Sniffer().sniff(remaining[:64000], delimiters=',;\t|').delimiter
        except csv.Error:
            # Ragged rows defeat Sniffer. Inspect the first quoted CSV record
            # for each candidate instead of silently treating semicolon data
            # as a single column. The preview still exposes the inferred choice.
            choices = []
            for candidate in (',', ';', '\t', '|'):
                try:
                    first = next(csv.reader(io.StringIO(remaining, newline=''), delimiter=candidate, strict=True))
                except (csv.Error, StopIteration):
                    continue
                known = sum(re.sub(r'[\s_-]+', ' ', _fold(cell)).strip() in _HEADERS for cell in first)
                choices.append(((known, len(first)), candidate))
            delimiter = max(choices, key=lambda choice: choice[0])[1] if choices else ','
    reader = csv.reader(io.StringIO(remaining, newline=''), delimiter=delimiter, strict=True)
    header, rows, width = None, [], 0
    previous_line, record = options['skip_rows'], 0
    try:
        for cells in reader:
            start = previous_line + 1
            end = reader.line_num + options['skip_rows']
            previous_line = end
            record += 1
            if record > MAX_ROWS + int(options['has_header']):
                raise ValueError('El CSV supera el límite de 10,000 filas')
            if len(cells) > MAX_COLUMNS:
                raise ValueError('El CSV supera el límite de 128 columnas')
            if any(len(cell) > MAX_FIELD for cell in cells):
                raise ValueError('Una celda supera el límite de 64,000 caracteres')
            width = max(width, len(cells))
            if header is None and options['has_header']:
                header = cells
            else:
                rows.append(dict(row_number=record + options['skip_rows'], line_start=start,
                                 line_end=end, values=cells))
    except csv.Error as exc:
        raise ValueError('CSV no válido cerca de la línea %s: %s' %
                         (reader.line_num + options['skip_rows'], exc)) from None
    if not rows or not any(any(cell.strip() for cell in row['values']) for row in rows):
        raise ValueError('No hay filas de datos; revise encabezados y líneas iniciales')
    header = header or []
    columns = []
    seen_suggestions = set()
    suggestions = []
    for index in range(width):
        name = header[index] if index < len(header) else ''
        key = re.sub(r'[\s_-]+', ' ', _fold(name)).strip()
        suggestion = _HEADERS.get(key, 'ignore')
        if suggestion != 'aliases' and suggestion in seen_suggestions:
            suggestion = 'ignore'
        seen_suggestions.add(suggestion)
        suggestions.append(suggestion)
        examples = list(dict.fromkeys(row['values'][index] for row in rows
                                     if index < len(row['values']) and row['values'][index].strip()))[:3]
        columns.append(dict(index=index, header=name[:300], label=name[:300] or 'Columna %s' % (index + 1),
                            header_truncated=len(name) > 300,
                            examples=[value[:300] for value in examples], suggested_field=suggestion))
    return rows, columns, delimiter, source_hash, suggestions, len(header) if options['has_header'] else None


def _text(value, field, maximum=300):
    if not isinstance(value, str):
        raise ValueError(field + ' debe ser texto')
    value = value.strip()
    if len(value) > maximum:
        raise ValueError('%s supera %s caracteres' % (field, maximum))
    if any(ord(char) < 32 and char not in ('\t', '\n', '\r') for char in value):
        raise ValueError(field + ' contiene caracteres de control')
    return value


def _normalize(fields, options):
    name = _text(fields.get('name', ''), 'Nombre')
    if not name:
        name = ' '.join(_text(fields.get(key, ''), key) for key in
                        ('given_name', 'first_surname', 'second_surname')).strip()
        name = re.sub(r'\s+', ' ', name)
    if not name or len(name) > 300:
        raise ValueError('Nombre obligatorio de hasta 300 caracteres')
    source_kind = fields.get('kind', '')
    raw_kind = _text(source_kind, 'Tipo')
    # Equivalences chosen in the UI use the exact displayed CSV cell. Also
    # accept a trimmed key supplied by a local integration.
    kind = (options['kind_values'].get(source_kind) or options['kind_values'].get(raw_kind)
            or _KIND_NAMES.get(_fold(raw_kind)))
    if not raw_kind:
        kind = options['default_kind']
    if kind not in KINDS:
        raise ValueError('Tipo ausente o desconocido: identifíquelo o seleccione un tipo predeterminado')
    aliases = fields.get('aliases') or []
    if isinstance(aliases, str):
        aliases = [aliases]
    if not isinstance(aliases, list):
        raise ValueError('aliases debe ser una lista o texto')
    parts = []
    for value in aliases:
        parts.extend(_text(value, 'Alias', MAX_FIELD).split(options['aliases_separator']))
    aliases = list(dict.fromkeys(part.strip() for part in parts if part.strip()))
    if len(aliases) > 50 or any(len(alias) > 300 for alias in aliases):
        raise ValueError('Se admiten hasta 50 alias de máximo 300 caracteres')
    context = {}
    for field in sorted(CONTEXT_FIELDS):
        value = fields.get(field, '')
        if value in ('', None):
            continue
        maximum = 4000 if field == 'context_note' else (2000 if field in ('trade_names', 'abbreviations') else 300)
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(field + ' debe contener texto o un número simple')
        value = _text(str(value), field, maximum)
        if value:
            context[field] = value
    if context.get('rfc'):
        context['rfc'] = context['rfc'].upper()
        if not re.fullmatch(r'[A-ZÑ&]{3,4}[0-9]{6}[A-Z0-9]{3}', context['rfc']):
            raise ValueError('RFC con estructura no válida; esto no valida registro ni identidad fiscal')
    if 'age' in context:
        if not re.fullmatch(r'\d{1,3}', context['age']) or not 0 <= int(context['age']) <= 130:
            raise ValueError('Edad debe ser un entero de 0 a 130 años')
        context['age'] = int(context['age'])
    if 'birth_date' in context:
        value = context['birth_date']
        slash = re.fullmatch(r'(\d{1,2})/(\d{1,2})/\d{4}', value)
        if options['date_format'] == 'auto' and slash:
            day, month = map(int, slash.groups())
            if day <= 12 and month <= 12 and day != month:
                raise ValueError('Fecha ambigua: seleccione explícitamente el formato DD/MM/AAAA')
        formats = ('%Y-%m-%d', '%d/%m/%Y') if options['date_format'] == 'auto' else (
            ('%Y-%m-%d',) if options['date_format'] == 'iso' else ('%d/%m/%Y',))
        parsed = None
        for fmt in formats:
            try:
                parsed = datetime.strptime(value, fmt).date()
                break
            except ValueError:
                pass
        if parsed is None or parsed > date.today():
            raise ValueError('Fecha de nacimiento no válida: use AAAA-MM-DD o DD/MM/AAAA')
        context['birth_date'] = parsed.isoformat()
    if len(canonical(context)) > 64000:
        raise ValueError('Contexto superior a 64 mil caracteres')
    entity_id = _text(fields.get('id', ''), 'ID', 150) or digest(canonical([name.casefold(), kind, context]))[:24]
    return dict(id=entity_id, name=name, kind=kind, aliases=aliases, context=context)



def _revoke_verification(context, changed, all_fields=False):
    """Never transfer a reviewed assertion to a different imported value.

    IdentityResolver supports dictionary/list verified_fields, provenance maps,
    structured values and verified_aliases. Unchanged reviewed values retain
    their provenance. A renamed/retyped ID invalidates every inherited anchor.
    """
    def affected(key):
        return all_fields or any(key == field or key.startswith(field + '.') for field in changed)

    verified = context.get('verified_fields')
    if isinstance(verified, dict):
        context['verified_fields'] = {key: value for key, value in verified.items() if not affected(str(key))}
    elif isinstance(verified, list):
        context['verified_fields'] = [value for value in verified if not affected(
            str(value.get('field') or value.get('key') or '') if isinstance(value, dict) else str(value))]
    for key in ('provenance', 'field_sources'):
        source = context.get(key)
        if isinstance(source, dict):
            context[key] = {field: value for field, value in source.items() if not affected(str(field))}
    if all_fields:
        # These entries are implicitly verified by their container in the
        # resolver. Remove that container after a changed entity identity.
        context.pop('verified_aliases', None)
        def unverify(value):
            if isinstance(value, dict):
                if any(key in value for key in ('verified', 'validated')) or str(value.get('status', '')).lower() in (
                        'verified', 'validated', 'confirmed'):
                    value.update(verified=False, validated=False, status='unverified')
                for item in list(value.values()):
                    unverify(item)
            elif isinstance(value, list):
                for item in value:
                    unverify(item)
        unverify(context)


def _single(payload, options):
    entity = payload.get('entity')
    if not isinstance(entity, dict):
        raise ValueError('entity debe ser un objeto')
    allowed = set(FIELDS) - {'ignore'} | {'context'}
    if set(entity) - allowed:
        raise ValueError('Campos individuales no admitidos: ' + ', '.join(sorted(set(entity) - allowed)))
    context = entity.get('context') or {}
    if not isinstance(context, dict) or set(context) - CONTEXT_FIELDS:
        raise ValueError('El contexto sólo admite pistas de identidad y notas; no admite anclas verificadas')
    if entity.get('name') and any(entity.get(key) for key in ('given_name', 'first_surname', 'second_surname')):
        raise ValueError('Seleccione nombre completo o sus componentes, no ambos')
    fields = dict(context)
    fields.update({key: value for key, value in entity.items() if key != 'context'})
    return fields, digest(canonical(entity))


def _mapping(payload, width):
    mapping = payload.get('mapping')
    if mapping is None:
        return None
    if not isinstance(mapping, list) or len(mapping) != width or any(field not in FIELDS for field in mapping):
        raise ValueError('mapping requiere un campo admitido por cada columna, en el mismo orden')
    seen = set()
    for field in mapping:
        if field not in ('ignore', 'aliases') and field in seen:
            raise ValueError('No puede asignar dos columnas al campo ' + field)
        seen.add(field)
    if 'name' in seen and seen.intersection({'given_name', 'first_surname', 'second_surname'}):
        raise ValueError('Seleccione nombre completo o sus componentes, no ambos')
    return mapping


def _bank(con):
    bank = {}
    revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
    for row in con.execute('SELECT id,name,kind,aliases,context FROM entities ORDER BY id'):
        bank[row['id']] = tuple(row)[1:]
    return bank, revision, digest(canonical(bank))


def preview_import(store, payload, connection=None):
    """Valida y devuelve una muestra; no guarda texto, entidades ni auditoría."""
    return _preview(store, payload, connection)[0]


def _preview(store, payload, connection=None):
    options = _options(payload)
    warnings = ['Los datos importados son pistas del usuario; no certifican identidad ni culpabilidad.']
    if options['mode'] == 'single':
        fields, source_hash = _single(payload, options)
        columns, delimiter, mapping, suggestions, header_width = [], None, [], [], None
        raw_rows = [dict(row_number=1, line_start=1, line_end=1, values=[], fields=fields)]
        explicit = True
    else:
        raw_rows, columns, delimiter, source_hash, suggestions, header_width = _parse_csv(payload, options)
        mapping = _mapping(payload, len(columns))
        explicit = mapping is not None
        if not explicit:
            warnings.append('Identifique el tipo de dato de cada columna; las sugerencias no se aplican automáticamente.')
        elif not any(field in mapping for field in ('name', 'given_name')):
            warnings.append('Seleccione una columna de nombre completo o nombres.')
            explicit = False
        if mapping is not None and 'kind' not in mapping and not options['default_kind']:
            warnings.append('Seleccione el tipo de entidad o asigne una columna de tipo.')
            explicit = False
        if mapping is not None and 'ignore' in mapping:
            warnings.append('%s columna(s) se ignorarán según el mapeo seleccionado.' % mapping.count('ignore'))
    with (nullcontext(connection) if connection is not None else store.connection()) as con:
        bank, revision, bank_hash = _bank(con)
    names = {}
    for entity_id, old in bank.items():
        names.setdefault(old[0].casefold(), set()).add(entity_id)
    counts = {key: 0 for key in ('total', 'valid', 'invalid', 'new', 'update', 'unchanged',
                               'skipped', 'duplicates', 'conflicting_ids', 'accepted')}
    counts['total'] = len(raw_rows)
    all_rows, errors, candidates, seen, kind_values, existing_ids = [], [], [], {}, set(), []
    for raw in raw_rows:
        result = {key: value for key, value in raw.items() if key != 'fields'}
        result.update(entity=None, status='unmapped', errors=[], warnings=[])
        fields = raw.get('fields', {})
        if options['mode'] == 'csv' and mapping is not None:
            fields = {}
            for index, field in enumerate(mapping):
                value = raw['values'][index] if index < len(raw['values']) else ''
                if field == 'aliases':
                    fields.setdefault('aliases', []).append(value)
                elif field != 'ignore':
                    fields[field] = value
            if fields.get('kind'):
                kind_values.add(fields['kind'])
            expected_width = header_width if header_width is not None else len(columns)
            if len(raw['values']) != expected_width:
                result['warnings'].append('Fila irregular: %s celdas frente a %s esperadas; revise todas las columnas.' %
                                          (len(raw['values']), expected_width))
        if explicit:
            try:
                entity = _normalize(fields, options)
                old = bank.get(entity['id'])
                if old is not None and options['existing_policy'] == 'update':
                    # Import is additive for hints: blank CSV cells never erase
                    # existing identity context, aliases or provenance.
                    merged = dict(json.loads(old[3]))
                    changed = {key for key, value in entity['context'].items() if merged.get(key) != value}
                    merged.update(entity['context'])
                    entity['context'] = merged
                    previous_aliases = json.loads(old[2])
                    entity['aliases'] = list(dict.fromkeys(previous_aliases + entity['aliases']))
                    if entity['aliases'] != previous_aliases:
                        changed.add('alias')
                        changed.add('aliases')
                    identity_changed = (entity['name'], entity['kind']) != old[:2]
                    _revoke_verification(merged, changed, all_fields=identity_changed)
                    if len(entity['aliases']) > 50 or len(canonical(merged)) > 64000:
                        raise ValueError('La combinación con los datos existentes excede los límites de alias o contexto')
                    if identity_changed:
                        result['warnings'].append('La actualización cambiará el nombre o tipo del ID existente; compruebe que corresponde a la misma entidad. Se revocan las verificaciones heredadas.')
                    elif changed:
                        result['warnings'].append('Las pistas modificadas y los alias agregados no heredan verificaciones anteriores; requieren revisión.')
                    result['warnings'].append('Se conservan alias y contexto existentes; sólo se agregan o actualizan pistas no vacías.')
                result['entity'] = entity
                values = (entity['name'], entity['kind'], canonical(entity['aliases']), canonical(entity['context']))
                entity_id = entity['id']
                if old is not None and options['existing_policy'] == 'skip' and old != values:
                    # Keep proposed values above for duplicate/conflict checks;
                    # the entity shown to the user must be what remains stored.
                    result['entity'] = dict(id=entity_id, name=old[0], kind=old[1],
                                            aliases=json.loads(old[2]), context=json.loads(old[3]))
                    result['warnings'].append('Los datos propuestos se omiten para este ID existente; se muestran el nombre, tipo y contexto que permanecen en el banco. Seleccione actualizar para incorporar sus pistas no vacías.')
                if entity_id in seen:
                    if seen[entity_id] == values:
                        result['status'] = 'duplicate'
                        counts['duplicates'] += 1
                        result['warnings'].append('Registro idéntico repetido; se procesa una sola vez según la política seleccionada. Un nombre por sí solo no acredita identidad.')
                    else:
                        result['status'] = 'conflict'
                        counts['conflicting_ids'] += 1
                        raise ValueError('El mismo ID identifica filas con datos diferentes; asigne IDs distintos o resuelva la discrepancia')
                else:
                    seen[entity_id] = values
                    old = bank.get(entity_id)
                    if old == values:
                        result['status'] = 'unchanged'
                        counts['unchanged'] += 1
                        existing_ids.append(entity_id)
                    elif old is not None and options['existing_policy'] == 'skip':
                        result['status'] = 'skipped'
                        counts['skipped'] += 1
                        existing_ids.append(entity_id)
                    else:
                        result['status'] = 'update' if old is not None else 'new'
                        counts[result['status']] += 1
                        candidates.append(entity)
                    displayed = result['entity']
                    homonyms = names.get(displayed['name'].casefold(), set()) - {entity_id}
                    if homonyms:
                        result['warnings'].append('Nombre compartido con otras entidades; se conservan por ID, sin fusionar homónimos.')
                    names.setdefault(displayed['name'].casefold(), set()).add(entity_id)
                counts['valid'] += 1
                if not result['entity']['context']:
                    result['warnings'].append('Sólo nombre y tipo: evidencia insuficiente para resolver homónimos.')
            except (ValueError, TypeError) as exc:
                if result['status'] != 'conflict':
                    result['status'] = 'invalid'
                counts['invalid'] += 1
                result['errors'].append(str(exc))
                if len(errors) < 100:
                    errors.append(dict(row_number=raw['row_number'], line_start=raw['line_start'],
                                       line_end=raw['line_end'], message=str(exc)))
        all_rows.append(result)
    counts['accepted'] = len(candidates)
    counts['invalid_skipped'] = counts['invalid'] if options['skip_invalid'] and not counts['conflicting_ids'] else 0
    counts['skipped_total'] = counts['skipped'] + counts['duplicates'] + counts['invalid_skipped']
    if counts['invalid'] > len(errors):
        warnings.append('Se muestran los primeros 100 errores; hay %s filas inválidas.' % counts['invalid'])
    if len(all_rows) > 20:
        warnings.append('Se muestran 20 filas de %s; todas fueron validadas.' % len(all_rows))
    if mapping is None and suggestions and 'kind' in suggestions:
        index = suggestions.index('kind')
        kind_values.update(row['values'][index] for row in raw_rows
                           if index < len(row['values']) and row['values'][index].strip())
    ready = explicit and not counts['conflicting_ids'] and (
        not counts['invalid'] or options['skip_invalid']) and bool(candidates or counts['unchanged'] or counts['skipped'] or counts['duplicates'])
    fingerprint = digest(canonical(dict(version=IMPORT_VERSION, source_hash=source_hash,
                                        options=options, mapping=mapping, bank_hash=bank_hash,
                                        bank_revision=revision)))
    visible_kinds = [value for value in sorted(kind_values) if len(value) <= 300][:1000]
    if len(visible_kinds) != len(kind_values):
        warnings.append('Tipos de entidad demasiado largos o más de 1,000 valores distintos: revise y simplifique la columna de tipo.')
    sample = []
    for row in all_rows[:20]:
        row = dict(row)
        truncated = [index for index, value in enumerate(row['values']) if len(value) > 500]
        row['values'] = [value[:500] for value in row['values']]
        row['values_truncated'] = truncated
        if truncated:
            row['warnings'] = row['warnings'] + ['La vista previa recorta celdas largas a 500 caracteres; la validación utiliza los valores completos.']
        sample.append(row)
    # Receipts lead with actual accepted entities, including those beyond the
    # first twenty input rows. Existing entities are added with persisted labels;
    # repeated source rows never consume multiple slots for the same ID.
    entity_samples, sampled_ids = [], set()
    for candidate in candidates:
        entity_id = candidate['id']
        if entity_id not in sampled_ids:
            entity_samples.append(dict(id=entity_id, name=candidate['name'], kind=candidate['kind'],
                                       status='update' if entity_id in bank else 'new'))
            sampled_ids.add(entity_id)
        if len(entity_samples) == 20:
            break
    if len(entity_samples) < 20:
        for row in all_rows:
            entity = row['entity']
            if entity is None or row['status'] not in ('skipped', 'unchanged', 'duplicate'):
                continue
            entity_id = entity['id']
            if entity_id in bank and entity_id not in sampled_ids:
                old = bank[entity_id]
                entity_samples.append(dict(id=entity_id, name=old[0], kind=old[1], status=row['status']))
                sampled_ids.add(entity_id)
            if len(entity_samples) == 20:
                break
    response = dict(version=IMPORT_VERSION, columns=columns, suggested_mapping=suggestions,
                    mapping=mapping, delimiter=delimiter, rows=sample, counts=counts,
                    errors=errors, warnings=warnings, kind_values=visible_kinds,
                    kind_suggestions={value: options['kind_values'].get(value) or _KIND_NAMES.get(_fold(value))
                                      for value in visible_kinds},
                    ready=bool(ready), preview_hash=fingerprint, bank_revision=revision,
                    existing_ids=list(dict.fromkeys(existing_ids))[:100],
                    sample_entities=entity_samples,
                    rows_truncated=max(0, len(all_rows) - 20), errors_truncated=max(0, counts['invalid'] - len(errors)))
    return response, candidates, dict(source_hash=source_hash, options=options, mapping=mapping)


def commit_import(store, payload):
    """Confirma de manera atómica exclusivamente el contenido ya previsualizado."""
    if not isinstance(payload, dict) or not isinstance(payload.get('preview_hash'), str):
        raise ImportConflict('Obtenga y revise una vista previa antes de confirmar')
    with store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        preview, candidates, metadata = _preview(store, payload, connection=con)
        if preview['preview_hash'] != payload['preview_hash']:
            raise ImportConflict('El contenido, las opciones o el banco cambiaron; revise una nueva vista previa')
        if not preview['ready']:
            raise ValueError('La importación no está lista: revise el mapeo, tipos y errores de las filas')
        if candidates:
            store.import_entities(candidates, connection=con)
        store._audit(con, 'guided_entities_imported', dict(
            version=IMPORT_VERSION, source_hash=metadata['source_hash'],
            mapping=[dict(index=index, field=field) for index, field in enumerate(metadata['mapping'] or [])],
            options={key: value for key, value in metadata['options'].items() if key != 'kind_values'},
            kind_mapping_count=len(metadata['options']['kind_values']),
            counts=preview['counts'], preview_hash=preview['preview_hash'], bank_revision=preview['bank_revision']))
    return dict(imported=len(candidates), accepted_ids=[entity['id'] for entity in candidates],
                counts=preview['counts'], preview_hash=preview['preview_hash'],
                warnings=preview['warnings'], existing_ids=preview['existing_ids'],
                sample_entities=preview['sample_entities'])
