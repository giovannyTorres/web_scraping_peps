"""SQLite WAL, trabajos con leases y bitácora encadenada SHA-256.

Un archivo de base de datos debe residir en disco local, nunca en carpeta de red.
La cadena detecta alteraciones accidentales; no sustituye una firma externa.
"""
import csv
import hashlib
import io
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing, nullcontext
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


SCORE_RUN_SCHEMA_VERSION = 1
_SCORE_INPUT_FIELDS = (
    'id', 'title', 'source_url', 'source_kind', 'published_at', 'retrieved_at',
    'content_hash', 'metadata', 'analysis', 'review', 'identity_resolution',
    'identity_bank_revision', 'event_id', 'event_date',
)
_DOCUMENT_FIELDS = (
    'id', 'url', 'title', 'content_hash', 'source_id', 'source_kind',
    'published_at', 'retrieved_at', 'metadata',
)


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
              INSERT OR IGNORE INTO meta VALUES('schema_version','1');
              INSERT OR IGNORE INTO meta VALUES('news_identity_revision','0');
              CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,
                aliases TEXT NOT NULL,context TEXT NOT NULL,updated_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS entity_name ON entities(name);
              CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,url TEXT NOT NULL,title TEXT NOT NULL,
                text TEXT NOT NULL,content_hash TEXT NOT NULL,source_id TEXT,source_kind TEXT NOT NULL,
                published_at TEXT,retrieved_at TEXT NOT NULL,metadata TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS doc_url ON documents(url);
              CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,entity_id TEXT NOT NULL REFERENCES entities(id),
                document_id TEXT NOT NULL REFERENCES documents(id),analysis TEXT NOT NULL,
                review TEXT NOT NULL DEFAULT '{}',UNIQUE(entity_id,document_id));
              CREATE INDEX IF NOT EXISTS evidence_entity ON evidence(entity_id);
              CREATE TABLE IF NOT EXISTS news_identity_links(document_id TEXT NOT NULL REFERENCES documents(id),
                entity_id TEXT NOT NULL REFERENCES entities(id),resolution TEXT NOT NULL,
                PRIMARY KEY(document_id,entity_id));
              CREATE TABLE IF NOT EXISTS news_identity_history(id TEXT PRIMARY KEY,document_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,resolution TEXT NOT NULL,analysis TEXT NOT NULL,review TEXT NOT NULL,
                retired_at TEXT NOT NULL,reason TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS scores(entity_id TEXT PRIMARY KEY REFERENCES entities(id),
                result TEXT NOT NULL,computed_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS score_runs(id TEXT PRIMARY KEY,entity_id TEXT NOT NULL,
                payload TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS score_runs_entity ON score_runs(entity_id,created_at DESC,id);
              CREATE TRIGGER IF NOT EXISTS score_runs_no_update BEFORE UPDATE ON score_runs
                BEGIN SELECT RAISE(ABORT,'score_runs es un historial append-only'); END;
              CREATE TRIGGER IF NOT EXISTS score_runs_no_delete BEFORE DELETE ON score_runs
                BEGIN SELECT RAISE(ABORT,'score_runs es un historial append-only'); END;
              CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,
                status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,due REAL NOT NULL,
                lease_until REAL,owner TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,due,lease_until);
              CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,
                actor TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS audit_action ON audit(action);
            """)
            con.execute('BEGIN IMMEDIATE')
            version = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            if version != '1':
                raise RuntimeError('Versión de base de datos no compatible; conserve respaldo.')
            try:
                con.execute("SELECT json_extract('{\"supported\":true}','$.supported')").fetchone()
            except sqlite3.OperationalError:
                self._audit_json = False
            else:
                self._audit_json = True
                con.execute("CREATE INDEX IF NOT EXISTS audit_score_run_hash ON audit(json_extract(payload,'$.run_hash')) "
                            "WHERE action='score_run_saved'")
            # Additive migration: existing current scores remain readable, but
            # their original inputs cannot be reconstructed retroactively.
            if 'run_id' not in {row['name'] for row in con.execute('PRAGMA table_info(scores)')}:
                con.execute('ALTER TABLE scores ADD COLUMN run_id TEXT REFERENCES score_runs(id)')
            con.execute("INSERT OR IGNORE INTO meta VALUES('score_run_schema_version','1')")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _audit(self, con, action, payload, actor='local'):
        previous = con.execute("SELECT event_hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        previous = previous[0] if previous else '0' * 64
        stamp, body = now(), canonical(payload)
        event_hash = digest(canonical([stamp, actor, action, body, previous]))
        con.execute("INSERT INTO audit(at,actor,action,payload,previous_hash,event_hash) VALUES(?,?,?,?,?,?)",
                    (stamp, actor, action, body, previous, event_hash))

    def audit(self, action, payload, actor='local'):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            self._audit(con, action, payload, actor)

    def verify_audit(self):
        previous, count = '0' * 64, 0
        last_scores = {}
        last_score_runs = {}
        last_identities = {}
        announced_runs = {}
        with self.connection() as con:
            con.execute('BEGIN')
            for row in con.execute('SELECT * FROM audit ORDER BY seq'):
                expected = digest(canonical([row['at'], row['actor'], row['action'], row['payload'], previous]))
                if row['previous_hash'] != previous or row['event_hash'] != expected:
                    return {'ok': False, 'failed_seq': row['seq'], 'checked': count}
                previous, count = expected, count + 1
                if row['action'] == 'entity_scored':
                    event = json.loads(row['payload'])
                    last_scores[event['id']] = event['result_hash']
                    last_score_runs[event['id']] = event.get('run_id')
                elif row['action'] == 'news_identity_resolved':
                    event = json.loads(row['payload'])
                    last_identities[(event['document_id'], event['entity_id'])] = event['resolution_hash']
                elif row['action'] == 'score_run_saved':
                    try:
                        event = json.loads(row['payload'])
                        run_id = event['run_hash']
                        if run_id in announced_runs and announced_runs[run_id] != event:
                            raise ValueError('Conflicting score run announcements')
                        announced_runs[run_id] = event
                    except (ValueError, TypeError, KeyError):
                        return {'ok': False, 'failed_seq': row['seq'], 'reason': 'invalid_score_run_announcement', 'checked': count}
            document_hashes = {}
            for row in con.execute('SELECT * FROM documents'):
                try:
                    document_hashes[row['id']] = self._document_record_hash(row)
                except (ValueError, TypeError, KeyError):
                    return {'ok': False, 'failed_document': row['id'], 'checked': count}
            stored_runs = {}
            for row in con.execute('SELECT * FROM score_runs'):
                event = announced_runs.get(row['id'])
                if event is None:
                    return {'ok': False, 'failed_score_run': row['id'], 'reason': 'orphan_score_run', 'checked': count}
                try:
                    run = self._decode_score_run(row)
                    self._check_run_announcement(run, event)
                    self._check_run_documents(run, document_hashes=document_hashes)
                except (ValueError, TypeError, KeyError):
                    return {'ok': False, 'failed_score_run': row['id'], 'reason': 'score_run_integrity', 'checked': count}
                stored_runs[row['id']] = (row['entity_id'], digest(canonical(run['result'])))
            missing = set(announced_runs) - set(stored_runs)
            if missing:
                return {'ok': False, 'failed_score_run': sorted(missing)[0], 'reason': 'missing_score_run', 'checked': count}
            for row in con.execute('SELECT entity_id,result,run_id FROM scores'):
                if last_scores.get(row['entity_id']) != digest(row['result']):
                    return {'ok': False, 'failed_score': row['entity_id'], 'checked': count}
                if row['run_id'] is not None and stored_runs.get(row['run_id']) != (row['entity_id'], digest(row['result'])):
                    return {'ok': False, 'failed_score': row['entity_id'], 'reason': 'current_run_mismatch', 'checked': count}
                if last_score_runs.get(row['entity_id']) != row['run_id']:
                    return {'ok': False, 'failed_score': row['entity_id'], 'reason': 'current_run_pointer_changed', 'checked': count}
            for row in con.execute('SELECT document_id,entity_id,resolution FROM news_identity_links'):
                if last_identities.get((row['document_id'], row['entity_id'])) != digest(row['resolution']):
                    return {'ok': False, 'failed_identity_link': row['entity_id'], 'document_id': row['document_id'], 'checked': count}
        return {'ok': True, 'checked': count, 'head_hash': previous,
                'score_runs_checked': len(stored_runs),
                'scope': 'event_chain_document_content_current_scores_and_historical_score_runs_with_document_metadata',
                'limitation': 'Una reescritura completa requiere detectar con un hash o respaldo externo.'}

    def import_entities(self, rows):
        count, ids, changed = 0, [], 0
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError('Cada entidad debe ser un objeto')
                name = str(row.get('name') or row.get('nombre_persona_input') or row.get('nombre') or '').strip()
                if not name or len(name) > 300:
                    raise ValueError('Cada entidad necesita name (máximo 300 caracteres).')
                kind = str(row.get('kind') or 'person')
                if kind not in ('person', 'company', 'organization'):
                    raise ValueError('kind debe ser person, company u organization')
                aliases = row.get('aliases') or row.get('alias_input') or []
                if isinstance(aliases, str):
                    aliases = [x.strip() for x in aliases.split('|') if x.strip()]
                if not isinstance(aliases, list) or len(aliases) > 50 or any(not isinstance(x, str) or not x.strip() or len(x) > 300 for x in aliases):
                    raise ValueError('aliases requiere hasta 50 textos no vacíos de máximo 300 caracteres')
                context = row.get('context') or {k: str(v) for k, v in row.items()
                            if k.endswith('_input') and k not in ('nombre_persona_input', 'alias_input') and v}
                if isinstance(context, str):
                    context = json.loads(context or '{}')
                if not isinstance(context, dict) or len(canonical(context)) > 64000:
                    raise ValueError('context debe ser un objeto JSON de máximo 64 mil caracteres')
                context = dict(context)
                optional_fields = {
                    'role': ('cargo', 'position', 'role', 'cargo_input'),
                    'state': ('estado', 'state', 'estado_input'),
                    'municipality': ('municipio', 'municipality', 'municipio_input'),
                    'city': ('city', 'ciudad'), 'rfc': ('RFC', 'rfc', 'rfc_input'),
                    'age': ('edad', 'age'),
                    'birth_date': ('birth_date', 'date_of_birth', 'fecha_nacimiento', 'fecha_de_nacimiento'),
                    'legal_name': ('razon_social',), 'trade_names': ('nombre_comercial', 'trade_names'),
                    'abbreviations': ('siglas',)}
                for field, alternatives in optional_fields.items():
                    for alias in alternatives:
                        if row.get(alias) not in (None, ''):
                            if field not in context:
                                context[field] = row[alias]
                            elif context[field] != row[alias]:
                                context.setdefault('import_hint_conflicts', {})[alias] = row[alias]
                if len(canonical(context)) > 64000:
                    raise ValueError('Contexto y pistas opcionales exceden 64 mil caracteres')
                entity_id = str(row.get('id') or digest(canonical([name.casefold(), kind, context]))[:24])
                if len(entity_id) > 150:
                    raise ValueError('ID demasiado largo')
                previous = con.execute('SELECT name,kind,aliases,context FROM entities WHERE id=?', (entity_id,)).fetchone()
                values = (name, kind, canonical(aliases), canonical(context))
                if previous is None or tuple(previous) != values:
                    con.execute('INSERT INTO entities VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                                'name=excluded.name,kind=excluded.kind,aliases=excluded.aliases,context=excluded.context,updated_at=excluded.updated_at',
                                (entity_id, *values, now()))
                    con.execute('DELETE FROM scores WHERE entity_id=?', (entity_id,))
                    changed += 1
                count += 1
                ids.append(entity_id)
            if changed:
                con.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='news_identity_revision'")
                con.execute('DELETE FROM scores WHERE entity_id IN (SELECT entity_id FROM news_identity_links)')
            self._audit(con, 'entities_imported', {'count': count, 'changed': changed, 'ids_hash': digest(canonical(ids))})
        return count

    @staticmethod
    def entity(row):
        return dict(row, aliases=json.loads(row['aliases']), context=json.loads(row['context']))

    def get_entity(self, entity_id):
        with self.connection() as con:
            row = con.execute('SELECT * FROM entities WHERE id=?', (entity_id,)).fetchone()
            return self.entity(row) if row else None

    def iter_entities(self, batch=500):
        last = ''
        while True:
            with self.connection() as con:
                rows = con.execute('SELECT * FROM entities WHERE id>? ORDER BY id LIMIT ?', (last, batch)).fetchall()
            if not rows:
                break
            for row in rows:
                yield self.entity(row)
            last = rows[-1]['id']

    def list_entities(self, search='', limit=100, offset=0):
        with self.connection() as con:
            rows = con.execute('SELECT e.*,s.result FROM entities e LEFT JOIN scores s ON s.entity_id=e.id '
                               'WHERE e.name LIKE ? ORDER BY e.name LIMIT ? OFFSET ?',
                               ('%' + search + '%', min(max(int(limit), 1), 500), max(int(offset), 0))).fetchall()
            return [dict(self.entity(r), score=json.loads(r['result']) if r['result'] else None) for r in rows]

    def add_document(self, doc, entity_ids, analyzer, connection=None, entity_lookup=None):
        text = str(doc.get('text') or '')
        if len(text) > 2_000_000:
            raise ValueError('Documento mayor de 2 millones de caracteres: divídalo conservando su procedencia')
        if not text.strip():
            raise ValueError('Documento vacío')
        url = str(doc.get('url') or '')
        metadata = dict(doc.get('metadata') or {})
        if doc.get('event_id'):
            metadata['event_id'] = str(doc['event_id'])
        content_hash = digest(text)
        document_id = digest(url + '\n' + content_hash)
        stamp = doc.get('retrieved_at') or now()
        analyses = []
        for entity_id in dict.fromkeys(entity_ids):
            entity = entity_lookup.get(entity_id) if entity_lookup is not None else self.get_entity(entity_id)
            if entity is None:
                raise ValueError('Entidad inexistente: ' + entity_id)
            # Los offsets y el hash se refieren siempre al mismo cuerpo original.
            analysis = analyzer(text, entity, doc.get('source_kind', 'news'))
            analyses.append((entity_id, analysis))
        with (nullcontext(connection) if connection is not None else self.connection()) as con:
            if not con.in_transaction:
                con.execute('BEGIN IMMEDIATE')
            con.execute('INSERT OR IGNORE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (document_id, url, str(doc.get('title') or ''), text, content_hash,
                         doc.get('source_id', 'manual'), doc.get('source_kind', 'news'), doc.get('published_at'),
                         stamp, canonical(metadata)))
            for entity_id, analysis in analyses:
                evidence_id = digest(entity_id + document_id)
                con.execute('INSERT OR IGNORE INTO evidence(id,entity_id,document_id,analysis) VALUES(?,?,?,?)',
                            (evidence_id, entity_id, document_id, canonical(analysis)))
                con.execute('DELETE FROM scores WHERE entity_id=?', (entity_id,))
            self._audit(con, 'document_ingested', {'id': document_id, 'hash': content_hash,
                         'entities': [e for e, _ in analyses], 'url': url})
        return document_id

    def evidence(self, entity_id, limit=None, connection=None):
        sql = ('SELECT d.*,e.id AS evidence_id,e.document_id,e.analysis,e.review,n.resolution AS identity_resolution FROM evidence e JOIN documents d '
               'ON e.document_id=d.id LEFT JOIN news_identity_links n ON n.document_id=d.id AND n.entity_id=e.entity_id '
               'WHERE e.entity_id=? ORDER BY d.retrieved_at DESC')
        args = [entity_id]
        if limit is not None:
            sql += ' LIMIT ?'
            args.append(int(limit))
        with (nullcontext(connection) if connection is not None else self.connection()) as con:
            revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
            rows = []
            for r in con.execute(sql, args):
                metadata, review = json.loads(r['metadata']), json.loads(r['review'])
                rows.append(dict(r, id=r['evidence_id'], analysis=json.loads(r['analysis']), review=review,
                            source_url=r['url'], metadata=metadata,
                            identity_resolution=json.loads(r['identity_resolution']) if r['identity_resolution'] else None,
                            identity_bank_revision=revision,
                            event_id=review.get('event_id') or metadata.get('event_id')))
            return rows

    def review(self, evidence_id, status, actor, reason, event_id=None, identity_spans=None, expected_run_id=None):
        if status not in ('pending', 'confirmed', 'rejected') or not actor.strip() or not reason.strip():
            raise ValueError('Revisión requiere estado, revisor y motivo')
        value = {'status': status, 'actor': actor.strip(), 'reason': reason.strip(), 'at': now()}
        if event_id:
            value['event_id'] = str(event_id).strip()[:300]
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT e.entity_id,e.document_id,e.review,d.text,d.content_hash FROM evidence e JOIN documents d '
                              'ON d.id=e.document_id WHERE e.id=?', (evidence_id,)).fetchone()
            if not row:
                raise ValueError('Evidencia inexistente')
            from .identity import entity_signature, RULES_HASH
            entity_row = con.execute('SELECT * FROM entities WHERE id=?', (row['entity_id'],)).fetchone()
            revision = int(con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()[0])
            if expected_run_id is not None:
                stale = 'La evaluación cambió; recargue antes de revisar'
                current = con.execute('SELECT run_id FROM scores WHERE entity_id=?', (row['entity_id'],)).fetchone()
                if current is None or current['run_id'] != expected_run_id:
                    raise ValueError(stale)
                try:
                    run = self._load_score_run(con, expected_run_id, row['entity_id'], validate_documents=False)
                    item = next((entry for entry in run['evidence_manifest'] if entry['id'] == evidence_id), None) if run else None
                    if (item is None or item.get('document_id') != row['document_id']
                            or item.get('content_hash') != row['content_hash']
                            or item.get('identity_bank_revision') != revision
                            or item.get('review') != json.loads(row['review'])
                            or entity_signature(run['entity_snapshot']) != entity_signature(self.entity(entity_row))):
                        raise ValueError(stale)
                    self._check_run_documents(dict(run, evidence_manifest=[item]), connection=con)
                except (ValueError, KeyError, TypeError):
                    raise ValueError(stale) from None
            value.update(entity_signature=entity_signature(self.entity(entity_row)), content_hash=row['content_hash'], bank_revision=revision,
                         identity_rules_hash=RULES_HASH)
            if identity_spans is not None:
                if not isinstance(identity_spans, list) or len(identity_spans) > 100:
                    raise ValueError('identity_spans requiere una lista de hasta 100 menciones')
                selected = []
                for span in identity_spans:
                    if not isinstance(span, dict) or not isinstance(span.get('start'), int) or not isinstance(span.get('end'), int):
                        raise ValueError('Cada mención requiere offsets start/end enteros')
                    if not 0 <= span['start'] < span['end'] <= len(row['text']):
                        raise ValueError('Mención fuera del cuerpo original')
                    selected.append(dict(start=span['start'], end=span['end'], text=row['text'][span['start']:span['end']]))
                value['identity_spans'] = selected
            con.execute('UPDATE evidence SET review=? WHERE id=?', (canonical(value), evidence_id))
            con.execute('DELETE FROM scores WHERE entity_id=?', (row['entity_id'],))
            self._audit(con, 'evidence_review', {'id': evidence_id, **value}, actor)
        return row['entity_id']

    @staticmethod
    def _document_record_hash(row):
        """Hash immutable source fields without storing another copy of text."""
        if digest(row['text']) != row['content_hash']:
            raise ValueError('El cuerpo del documento no coincide con su hash')
        if digest(row['url'] + '\n' + row['content_hash']) != row['id']:
            raise ValueError('La URL o identidad del documento fue alterada')
        value = {key: row[key] for key in _DOCUMENT_FIELDS}
        value['metadata'] = json.loads(value['metadata'])
        return digest(canonical(value))

    @staticmethod
    def _decode_score_run(row):
        if digest(row['payload']) != row['id']:
            raise ValueError('Hash de evaluación histórica inválido')
        payload = json.loads(row['payload'])
        if (not isinstance(payload, dict) or type(payload.get('schema_version')) is not int
                or payload['schema_version'] != SCORE_RUN_SCHEMA_VERSION
                or not isinstance(payload.get('entity_snapshot'), dict)
                or payload['entity_snapshot'].get('id') != row['entity_id']
                or not isinstance(payload.get('evidence_manifest'), list)
                or not isinstance(payload.get('result'), dict)):
            raise ValueError('Formato de evaluación histórica inválido')
        manifest = payload['evidence_manifest']
        if any(not isinstance(item, dict) for item in manifest):
            raise ValueError('Manifiesto de evidencias inválido')
        indices = [item.get('input_index') for item in manifest]
        ids = [item.get('id') for item in manifest]
        if (any(type(index) is not int for index in indices) or sorted(indices) != list(range(len(manifest)))
                or any(not isinstance(item_id, str) or not item_id for item_id in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError('Orden o identificadores del manifiesto inválidos')
        return dict(payload, id=row['id'], entity_id=row['entity_id'], created_at=row['created_at'])

    @staticmethod
    def _check_run_announcement(run, event):
        if (event.get('run_hash') != run['id'] or event.get('id') != run['entity_id']
                or event.get('created_at') != run['created_at']
                or event.get('result_hash') != digest(canonical(run['result']))):
            raise ValueError('La evaluación no coincide con su evento de auditoría')

    @staticmethod
    def _assert_audit_chain(con):
        """Fail closed on a partial rewrite, within the caller's read snapshot.

        Checking only the announcing event is insufficient: an editor can
        change that event and its own hash while leaving the next link broken.
        Do not cache this decision across connections or transactions.
        """
        previous = '0' * 64
        for row in con.execute('SELECT seq,at,actor,action,payload,previous_hash,event_hash FROM audit ORDER BY seq'):
            expected = digest(canonical([row['at'], row['actor'], row['action'], row['payload'], previous]))
            if row['previous_hash'] != previous or row['event_hash'] != expected:
                raise ValueError(f"Cadena de auditoría inválida en la secuencia {row['seq']}; historial no verificable")
            previous = expected

    def _check_run_documents(self, run, connection=None, document_hashes=None):
        for item in run['evidence_manifest']:
            document_id = item.get('document_id')
            if not isinstance(document_id, str) or not isinstance(item.get('document_record_hash'), str):
                raise ValueError('Referencia documental inválida')
            if document_hashes is not None:
                actual = document_hashes.get(document_id)
            else:
                row = connection.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
                actual = self._document_record_hash(row) if row else None
            if actual is None or actual != item['document_record_hash']:
                raise ValueError('Documento histórico ausente o metadatos/cuerpo alterados')

    def _load_score_run(self, con, run_id, entity_id=None, validate_documents=True, verify_chain=True):
        if verify_chain:
            self._assert_audit_chain(con)
        row = con.execute('SELECT * FROM score_runs WHERE id=?', (run_id,)).fetchone()
        if row is None or (entity_id is not None and row['entity_id'] != entity_id):
            return None
        run = self._decode_score_run(row)
        announcement = None
        if self._audit_json:
            query = ("SELECT * FROM audit WHERE action='score_run_saved' "
                     "AND json_extract(payload,'$.run_hash')=? ORDER BY seq")
            lookup = run_id
        else:
            # The row hash has already been verified as a SHA-256 hex digest;
            # no wildcard or caller-controlled SQL can enter this LIKE pattern.
            # Older SQLite builds can filter canonical event JSON in SQLite C
            # using audit(action), without parsing every event in Python.
            query = "SELECT * FROM audit WHERE action='score_run_saved' AND payload LIKE ? ORDER BY seq"
            lookup = '%"run_hash":"' + run_id + '"%'
        for audit_row in con.execute(query, (lookup,)):
            event = json.loads(audit_row['payload'])
            if not isinstance(event, dict):
                raise ValueError('Evento de auditoría de evaluación inválido')
            if event.get('run_hash') != run_id:
                continue
            expected = digest(canonical([audit_row['at'], audit_row['actor'], audit_row['action'],
                                         audit_row['payload'], audit_row['previous_hash']]))
            if expected != audit_row['event_hash']:
                raise ValueError('Evento de auditoría de la evaluación alterado')
            self._check_run_announcement(run, event)
            announcement = event
            break
        if announcement is None:
            raise ValueError('Evaluación histórica sin evento de auditoría')
        if validate_documents:
            self._check_run_documents(run, connection=con)
        return run

    def _evidence_manifest(self, con, entity_id, evidence):
        manifest = []
        ids = set()
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in ids:
                raise ValueError('Cada evidencia histórica requiere un ID único')
            ids.add(item['id'])
            link = con.execute('SELECT document_id FROM evidence WHERE id=? AND entity_id=?',
                               (item['id'], entity_id)).fetchone()
            if link is None or (item.get('document_id') is not None and item['document_id'] != link['document_id']):
                raise ValueError('La evidencia no pertenece a la entidad/documento evaluados')
            row = con.execute('SELECT * FROM documents WHERE id=?', (link['document_id'],)).fetchone()
            if row is None or item.get('text') != row['text']:
                raise ValueError('El texto evaluado debe coincidir con el documento original')
            entry = {key: item[key] for key in _SCORE_INPUT_FIELDS if key in item}
            entry.update(document_id=link['document_id'], input_index=index,
                         document_record_hash=self._document_record_hash(row))
            manifest.append(entry)
        return sorted(manifest, key=lambda item: item['id'])

    def save_score(self, entity_id, result, connection=None, entity_snapshot=None, evidence_snapshot=None):
        """Save an immutable run and current pointer in the same transaction.

        Snapshots should be the exact inputs evaluated by the caller. The
        compatible fallback reads current inputs from this transaction; it
        cannot recover inputs of a result computed earlier against other data.
        """
        with (nullcontext(connection) if connection is not None else self.connection()) as con:
            if not con.in_transaction:
                con.execute('BEGIN IMMEDIATE')
            if entity_snapshot is None:
                row = con.execute('SELECT * FROM entities WHERE id=?', (entity_id,)).fetchone()
                entity_snapshot = self.entity(row) if row else None
            if not isinstance(entity_snapshot, dict) or entity_snapshot.get('id') != entity_id or not isinstance(result, dict):
                raise ValueError('La evaluación requiere entidad y resultado válidos')
            if evidence_snapshot is None:
                evidence_snapshot = self.evidence(entity_id, connection=con)
            payload = {'schema_version': SCORE_RUN_SCHEMA_VERSION, 'entity_snapshot': entity_snapshot,
                       'evidence_manifest': self._evidence_manifest(con, entity_id, evidence_snapshot), 'result': result}
            encoded, result_encoded = canonical(payload), canonical(result)
            run_id, stamp = digest(encoded), now()
            inserted = con.execute('INSERT OR IGNORE INTO score_runs(id,entity_id,payload,created_at) VALUES(?,?,?,?)',
                                   (run_id, entity_id, encoded, stamp)).rowcount
            if inserted:
                self._audit(con, 'score_run_saved', {'id': entity_id, 'run_hash': run_id,
                            'result_hash': digest(result_encoded), 'created_at': stamp})
            else:
                # An orphan/corrupt existing row must not be legitimized by a
                # later save. It needs integrity review, not another event.
                # This write path checks the local record but never claims to
                # verify the historical chain. Every public history read does
                # so in its own snapshot; scanning the chain for every saved
                # entity would make bulk scoring quadratic.
                self._load_score_run(con, run_id, entity_id, verify_chain=False)
            con.execute('INSERT OR REPLACE INTO scores(entity_id,result,computed_at,run_id) VALUES(?,?,?,?)',
                        (entity_id, result_encoded, stamp, run_id))
            self._audit(con, 'entity_scored', {'id': entity_id, 'result_hash': digest(result_encoded),
                                             'model_version': result.get('model_version'), 'run_id': run_id})
        return run_id

    def get_score(self, entity_id):
        """Return the current result; invalidated scores remain absent."""
        with self.connection() as con:
            row = con.execute('SELECT result FROM scores WHERE entity_id=?', (entity_id,)).fetchone()
            return json.loads(row['result']) if row else None

    def latest_score_run(self, entity_id):
        """Return only the current run, never silently revive invalidated history."""
        with self.connection() as con:
            con.execute('BEGIN')
            row = con.execute('SELECT run_id,result FROM scores WHERE entity_id=?', (entity_id,)).fetchone()
            if row is None or row['run_id'] is None:
                return None
            run = self._load_score_run(con, row['run_id'], entity_id)
            if run is None or canonical(run['result']) != row['result']:
                raise ValueError('Puntero de evaluación actual inválido')
            return run

    def score_history(self, entity_id, limit=50):
        """Bounded summaries; full document validation occurs on opening a run."""
        with self.connection() as con:
            con.execute('BEGIN')
            # Once for this read transaction, rather than once per summary.
            self._assert_audit_chain(con)
            current = con.execute('SELECT run_id FROM scores WHERE entity_id=?', (entity_id,)).fetchone()
            current_id = current['run_id'] if current else None
            rows = con.execute('SELECT * FROM score_runs WHERE entity_id=? ORDER BY created_at DESC,id LIMIT ?',
                               (entity_id, min(max(int(limit), 1), 200)))
            history = []
            for row in rows:
                run = self._load_score_run(con, row['id'], entity_id, validate_documents=False, verify_chain=False)
                result = run['result']
                history.append(dict(id=run['id'], entity_id=entity_id, created_at=run['created_at'],
                    score=result.get('score'), label=result.get('label'), as_of=result.get('as_of'),
                    model_version=result.get('model_version'), current=run['id'] == current_id))
            return history

    def get_score_run(self, run_id, entity_id=None):
        with self.connection() as con:
            con.execute('BEGIN')
            return self._load_score_run(con, run_id, entity_id)

    def materialize_run_evidence(self, run):
        """Reconstruct original scoring inputs from validated immutable bodies.

        The persisted payload is reloaded instead of trusting caller edits.
        Review/identity snapshots remain historical, even if current links have
        been removed or the bank revision has changed.
        """
        if not isinstance(run, dict) or not isinstance(run.get('id'), str):
            raise ValueError('Se requiere una evaluación histórica identificada')
        with self.connection() as con:
            con.execute('BEGIN')
            stored = self._load_score_run(con, run['id'], run.get('entity_id'))
            if stored is None:
                raise ValueError('Evaluación histórica inexistente')
            result = []
            for item in sorted(stored['evidence_manifest'], key=lambda entry: entry['input_index']):
                row = con.execute('SELECT text FROM documents WHERE id=?', (item['document_id'],)).fetchone()
                value = {key: item[key] for key in _SCORE_INPUT_FIELDS if key in item}
                value.update(text=row['text'], document_id=item['document_id'])
                result.append(value)
            return result

    def run_evidence(self, run_id, evidence_id):
        """Target one historical evidence regardless of current UI page limits."""
        with self.connection() as con:
            con.execute('BEGIN')
            run = self._load_score_run(con, run_id, validate_documents=False)
            if run is None:
                return None
            item = next((entry for entry in run['evidence_manifest'] if entry['id'] == evidence_id), None)
            if item is None:
                return None
            self._check_run_documents(dict(run, evidence_manifest=[item]), connection=con)
            row = con.execute('SELECT * FROM documents WHERE id=?', (item['document_id'],)).fetchone()
            value = {key: item[key] for key in _SCORE_INPUT_FIELDS if key in item}
            value.update(text=row['text'], document_id=item['document_id'], url=row['url'],
                         source_id=row['source_id'], run_id=run_id, entity_id=run['entity_id'])
            return value

    def enqueue(self, kind, payload, key=None, due=None, connection=None):
        job_id = digest(key or kind + canonical(payload))
        with (nullcontext(connection) if connection is not None else self.connection()) as con:
            con.execute('INSERT OR IGNORE INTO jobs(id,kind,payload,status,due,created_at,updated_at) VALUES(?,?,?,\'queued\',?,?,?)',
                        (job_id, kind, canonical(payload), due if due is not None else time.time(), now(), now()))
        return job_id

    def claim(self, owner=None, lease_seconds=120, kind_prefix=None):
        owner = owner or uuid.uuid4().hex
        current = time.time()
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            # Cinco intentos, incluidos procesos que murieron con un lease, van a dead letter.
            con.execute("UPDATE jobs SET status='dead',error='Lease expirado; límite de intentos' "
                        "WHERE status='running' AND lease_until<? AND attempts>=5", (current,))
            row = con.execute("SELECT * FROM jobs WHERE attempts<5 AND due<=? AND (? IS NULL OR kind LIKE ?) AND "
                              "(status IN ('queued','deferred') OR (status='running' AND lease_until<?)) "
                              "ORDER BY due,id LIMIT 1", (current, kind_prefix, (kind_prefix or '') + '%', current)).fetchone()
            if not row:
                return None
            con.execute("UPDATE jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?",
                        (owner, current + lease_seconds, now(), row['id']))
            return dict(row, payload=json.loads(row['payload']), owner=owner, attempts=row['attempts'] + 1)

    def finish(self, job, status='done', error='', retry_at=None, consume_attempt=True):
        if status not in ('done', 'deferred', 'dead', 'blocked'):
            raise ValueError('Estado de tarea inválido')
        if status == 'deferred' and consume_attempt and job['attempts'] >= 5:
            status = 'dead'
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            changed = con.execute('UPDATE jobs SET status=?,error=?,due=?,lease_until=NULL,updated_at=?,attempts=attempts-? '
                                  "WHERE id=? AND status='running' AND owner=?", (status, error[:2000],
                                  retry_at if retry_at is not None else time.time(), now(), 0 if consume_attempt else 1,
                                  job['id'], job['owner'])).rowcount
            if changed:
                self._audit(con, 'job_finished', {'id': job['id'], 'status': status, 'error': error[:500]})
            return bool(changed)

    def renew(self, job, lease_seconds=300):
        with self.connection() as con:
            return bool(con.execute("UPDATE jobs SET lease_until=? WHERE id=? AND owner=? AND status='running'",
                                    (time.time() + lease_seconds, job['id'], job['owner'])).rowcount)

    def next_due(self, kind_prefix=None):
        with self.connection() as con:
            row = con.execute("SELECT min(due) FROM jobs WHERE status IN ('queued','deferred') AND attempts<5 "
                              "AND (? IS NULL OR kind LIKE ?)", (kind_prefix, (kind_prefix or '') + '%')).fetchone()
            return row[0]

    def retry_failed(self):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            n = con.execute("UPDATE jobs SET status='queued',attempts=0,due=?,error='' WHERE status IN ('dead','blocked')",
                            (time.time(),)).rowcount
            self._audit(con, 'jobs_requeued', {'count': n})
            return n

    def stats(self):
        with self.connection() as con:
            counts = {table: con.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                      for table in ('entities', 'documents', 'evidence', 'scores', 'audit')}
            counts['jobs'] = dict(con.execute('SELECT status,count(*) FROM jobs GROUP BY status'))
            counts['recent_errors'] = [dict(r) for r in con.execute(
                "SELECT kind,status,error,updated_at FROM jobs WHERE error!='' ORDER BY updated_at DESC LIMIT 10")]
            return counts

    def export(self):
        # Un registro por entidad/documento/vínculo. Incluso una sola entidad con
        # millones de evidencias se exporta con memoria acotada.
        with self.connection() as con:
            con.execute('BEGIN')
            for row in con.execute('SELECT e.*,s.result,s.run_id FROM entities e LEFT JOIN scores s ON s.entity_id=e.id ORDER BY e.id'):
                entity = self.entity(row)
                entity.pop('result', None)
                entity.pop('run_id', None)
                yield dict(entity, record_type='entity', score=json.loads(row['result']) if row['result'] else None,
                           score_run_id=row['run_id'])
            for row in con.execute('SELECT * FROM documents ORDER BY id'):
                yield dict(row, record_type='document', metadata=json.loads(row['metadata']))
            for row in con.execute('SELECT * FROM evidence ORDER BY id'):
                yield dict(row, record_type='evidence', analysis=json.loads(row['analysis']), review=json.loads(row['review']))
            for row in con.execute('SELECT * FROM news_identity_links ORDER BY document_id,entity_id'):
                yield dict(row, record_type='identity_resolution', resolution=json.loads(row['resolution']))
            for row in con.execute('SELECT * FROM news_identity_history ORDER BY id'):
                yield dict(row, record_type='retired_identity', resolution=json.loads(row['resolution']),
                           analysis=json.loads(row['analysis']), review=json.loads(row['review']))
            for row in con.execute('SELECT * FROM score_runs ORDER BY created_at,id'):
                # A run contains references and decisions, never another body.
                yield dict(self._decode_score_run(row), record_type='score_run')
            available = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, record_type in [('news_articles', 'news_article'), ('news_observations', 'news_observation'),
                                       ('news_discovery_runs', 'news_discovery_run'), ('news_query_windows', 'news_query_window')]:
                if table in available:
                    for row in con.execute('SELECT * FROM ' + table):
                        yield dict(row, record_type=record_type)

    def backup(self, target):
        target = Path(target).resolve()
        if target == self.path:
            raise ValueError('El respaldo requiere una ruta distinta')
        target.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con, closing(sqlite3.connect(str(target))) as dest:
            con.backup(dest)


def read_entities(text):
    if text.lstrip().startswith(('{', '[')):
        if text.lstrip().startswith('['):
            yield from json.loads(text)
        else:
            for line in text.splitlines():
                if line.strip():
                    yield json.loads(line)
    else:
        yield from csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
