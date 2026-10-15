"""Interfaz local: loopback, token por sesión, límites de entrada y CSP."""
import json
import copy
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .engine import capacity
from .store import read_entities, now, canonical, digest


def create_server(engine, port=0):
    from .news import NewsAcquirer
    news = NewsAcquirer(engine)
    token = secrets.token_urlsafe(32)
    web = Path(__file__).parent / 'web'
    state = {'busy': False, 'last_result': None, 'error': None}
    lock = threading.Lock()
    tickets = {}
    import_operations = {}
    import_ttl = 30 * 60

    def background(fn):
        with lock:
            if state['busy']:
                raise ValueError('Ya hay una operación en curso')
            state.update(busy=True, error=None, last_result=None, operation_id=None)
        def execute():
            try:
                state['last_result'] = fn()
            except Exception as exc:
                state['error'] = f'{type(exc).__name__}: {exc}'
            finally:
                state['busy'] = False
        threading.Thread(target=execute, daemon=True).start()

    def prune_imports(reserve_slot=False):
        cutoff = time.monotonic() - import_ttl
        expired = [key for key, value in import_operations.items()
                   if value['updated_monotonic'] < cutoff and value['status'] in ('completed', 'error')]
        for key in expired:
            del import_operations[key]
        while len(import_operations) > 32 - int(reserve_slot):
            old = next((key for key, value in import_operations.items()
                        if value['status'] in ('completed', 'error')), None)
            if old is None:
                break
            del import_operations[old]

    def public_import_result(report):
        value = {key: copy.deepcopy(report[key]) for key in
                 ('imported', 'counts', 'warnings', 'source_hash', 'bank_revision') if key in report}
        for key, limit in (('accepted_ids', 100), ('existing_ids', 100), ('sample_entities', 20)):
            values = report.get(key, [])
            value[key] = copy.deepcopy(values[:limit])
            if len(values) > limit:
                value[key + '_truncated'] = True
        return value

    def queue_import(payload):
        """Keep receipts separate from global activity; never retain CSV text."""
        from .importing import commit_import, ImportConflict
        preview_hash = payload.get('preview_hash')
        if not isinstance(preview_hash, str) or len(preview_hash) != 64 or any(c not in '0123456789abcdef' for c in preview_hash):
            raise ValueError('Revise la vista previa antes de confirmar la importación')
        request_hash = digest(canonical(payload))
        with lock:
            prune_imports(reserve_slot=True)
            for operation in import_operations.values():
                if (operation['preview_hash'] == preview_hash and operation['request_hash'] == request_hash
                        and operation['status'] != 'error'):
                    return operation['id']
            if state['busy']:
                raise ValueError('Hay una operación en curso. Espere a que termine antes de importar.')
            operation_id = secrets.token_urlsafe(24)
            stamp = time.monotonic()
            operation = {'id': operation_id, 'status': 'queued', 'phase': 'saving',
                         'created_at': now(), 'preview_hash': preview_hash,
                         'request_hash': request_hash,
                         'updated_monotonic': stamp, 'result': None}
            import_operations[operation_id] = operation
            state.update(busy=True, error=None, last_result=None, operation_id=operation_id)

        def execute_import():
            try:
                with lock:
                    operation.update(status='running', updated_monotonic=time.monotonic())
                report = commit_import(engine.store, payload)
                public = public_import_result(report)
                with lock:
                    operation.update(phase='scoring', result=copy.deepcopy(public), updated_monotonic=time.monotonic())
                accepted = report.get('accepted_ids', [])
                try:
                    scored = engine.rescore(accepted) if accepted else 0
                    public.update(scored=scored, scoring_status='completed')
                except Exception as exc:
                    # Import is committed. Do not suggest it failed or invite
                    # a second import merely because subsequent scoring failed.
                    public.update(scored=None, scoring_status='pending',
                                  scoring_error='Los datos se guardaron. La evaluación local quedó pendiente; consulte la entidad para reintentar.',
                                  scoring_error_type=type(exc).__name__)
                with lock:
                    operation.update(status='completed', phase='done', result=public,
                                     updated_monotonic=time.monotonic())
                    state['last_result'] = {'operation_id': operation_id, **public}
            except Exception as exc:
                conflict = isinstance(exc, ImportConflict)
                reason = str(exc) if isinstance(exc, (ValueError, TypeError, KeyError)) else 'No se pudo guardar la importación. Revise la vista previa antes de reintentar.'
                with lock:
                    operation.update(status='error', phase='done', error=reason,
                        error_code='preview_stale' if conflict else 'invalid_import',
                        retry_preview=True, updated_monotonic=time.monotonic())
                    state['error'] = reason
            finally:
                with lock:
                    state['busy'] = False
        threading.Thread(target=execute_import, daemon=True).start()
        return operation_id

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PEPS-MX/2'

        def log_message(self, *_):
            pass

        def reply(self, data, code=200, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; "
                             "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(data)

        def authorized(self, api=True):
            expected = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected:
                self.reply({'error': 'Host no permitido'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + expected:
                self.reply({'error': 'Origen no permitido'}, 403)
                return False
            if api and not secrets.compare_digest(self.headers.get('X-PEPS-Token', ''), token):
                self.reply({'error': 'Sesión inválida'}, 403)
                return False
            return True

        def do_GET(self):
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            ticket = query.get('ticket', [''])[0] if path == '/api/export' else ''
            download_allowed = False
            if ticket:
                with lock:
                    expires = tickets.pop(ticket, 0)
                    download_allowed = expires > time.time()
            if not self.authorized(path.startswith('/api/') and not download_allowed):
                return
            try:
                if path == '/':
                    self.reply((web / 'index.html').read_text(encoding='utf-8').replace('__TOKEN__', token).encode('utf-8'),
                               content_type='text/html; charset=utf-8')
                elif path in ('/app.js', '/import.js', '/style.css'):
                    self.reply((web / path[1:]).read_bytes(), content_type='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
                elif path == '/api/stats':
                    self.reply(dict(engine.store.stats(), online=engine.online, operation=state.copy(), capacity=capacity()))
                elif path == '/api/import-status':
                    with lock:
                        prune_imports()
                        operation = copy.deepcopy(import_operations.get(query.get('id', [''])[0]))
                    if operation is None:
                        self.reply({'error': 'Seguimiento no disponible. Consulte el banco para comprobar si los datos ya se guardaron.'}, 404)
                    else:
                        operation.pop('preview_hash', None)
                        operation.pop('request_hash', None)
                        operation.pop('updated_monotonic', None)
                        self.reply(operation)
                elif path == '/api/entities':
                    self.reply(engine.store.list_entities(query.get('q', [''])[0], query.get('limit', [100])[0], query.get('offset', [0])[0]))
                elif path == '/api/entity':
                    entity_id = query.get('id', [''])[0]
                    entity = engine.store.get_entity(entity_id)
                    if not entity:
                        self.reply({'error': 'Entidad inexistente'}, 404)
                    else:
                        self.reply({'entity': entity, 'evidence': engine.store.evidence(entity_id, 200), 'evidence_limit': 200})
                elif path == '/api/explanation':
                    entity_id = query.get('id', [''])[0]
                    if not engine.store.get_entity(entity_id):
                        self.reply({'error': 'Entidad inexistente'}, 404)
                    else:
                        self.reply(engine.explanation(entity_id, query.get('run_id', [None])[0]))
                elif path == '/api/run-evidence':
                    item = engine.store.run_evidence(query.get('run_id', [''])[0], query.get('id', [''])[0])
                    self.reply(item if item else {'error': 'Evidencia inexistente en esta evaluación'}, 200 if item else 404)
                elif path == '/api/sources':
                    self.reply(engine.sources())
                elif path == '/api/audit':
                    self.reply(engine.store.verify_audit())
                elif path == '/api/news':
                    self.reply({'stats': news.stats(), 'articles': news.articles(), 'sources': news.sources(), 'plan': news.plan()})
                elif path == '/api/export':
                    # Exportación completa y streaming: memoria acotada para bancos grandes.
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename=peps-export.jsonl')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    for row in engine.store.export():
                        self.wfile.write((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8'))
                else:
                    self.reply({'error': 'No encontrado'}, 404)
            except (ValueError, TypeError, KeyError) as exc:
                self.reply({'error': str(exc)}, 400)

        def do_POST(self):
            if not self.authorized():
                return
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length < 1 or length > 16_000_000:
                    self.reply({'error': 'Entrada vacía o mayor de 16 MB; use CLI para cargas masivas'}, 413)
                    return
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('La petición debe contener un objeto JSON')
                if self.path == '/api/import-preview':
                    from .importing import preview_import
                    self.reply(preview_import(engine.store, data))
                    return
                elif self.path == '/api/import-confirm':
                    self.reply({'operation_id': queue_import(data)}, 202)
                    return
                elif self.path == '/api/import-entities':
                    text = str(data['text'])
                    background(lambda: {'imported': engine.store.import_entities(read_entities(text)), 'scored': engine.rescore()})
                elif self.path == '/api/import-documents':
                    text = str(data['text'])
                    background(lambda: {'imported': engine.import_documents(json.loads(x) for x in text.splitlines() if x.strip()),
                                        'scored': engine.rescore()})
                elif self.path == '/api/score':
                    background(lambda: {'scored': engine.rescore()})
                elif self.path == '/api/collect':
                    engine.online = bool(data.get('online'))
                    if not engine.online:
                        raise ValueError('Active red autorizada para recolectar')
                    def collect():
                        queued = engine.queue_discovery()
                        return {'queued': queued, **engine.run(workers=4, max_jobs=1000, seconds=120)}
                    background(collect)
                elif self.path == '/api/stop':
                    engine.stop_event.set()
                elif self.path == '/api/retry':
                    engine.store.retry_failed()
                elif self.path == '/api/source':
                    engine.set_source(data['id'], data['enabled'], data['terms_approved'])
                elif self.path == '/api/review':
                    if not data.get('expected_run_id'):
                        raise ValueError('Abra la evaluación actual antes de registrar una revisión')
                    entity_id = engine.store.review(data['id'], data['status'], data['actor'], data['reason'], data.get('event_id'), data.get('identity_spans'), expected_run_id=data['expected_run_id'])
                    engine.rescore(entity_id)
                elif self.path == '/api/replay':
                    self.reply(engine.replay_score(str(data['run_id'])))
                    return
                elif self.path == '/api/news-acquire':
                    engine.online = bool(data.get('online'))
                    if not engine.online:
                        raise ValueError('Active la conexión autorizada para descargar noticias')
                    background(lambda: news.acquire(workers=4, seconds=120, max_articles=1000))
                elif self.path == '/api/news-attribute':
                    background(lambda: news.attribute(limit=1000, force=bool(data.get('force'))))
                elif self.path == '/api/news-source':
                    news.set_source(data['id'], data['enabled'], data['terms_approved'], data.get('download_articles', False),
                                    data.get('article_terms_approved', False))
                elif self.path == '/api/backup':
                    from .store import now
                    destination = engine.data_dir / 'backups' / ('peps-' + now().replace(':', '-').replace('+', '_') + '.sqlite3')
                    engine.store.backup(destination)
                    self.reply({'file': str(destination)})
                    return
                elif self.path == '/api/export-ticket':
                    ticket = secrets.token_urlsafe(32)
                    with lock:
                        for old in [k for k, expiry in tickets.items() if expiry <= time.time()]:
                            tickets.pop(old, None)
                        tickets[ticket] = time.time() + 30
                    self.reply({'url': '/api/export?ticket=' + ticket})
                    return
                elif self.path == '/api/shutdown':
                    engine.stop_event.set()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.reply({'error': 'No encontrado'}, 404)
                    return
                self.reply({'ok': True})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, 400)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler), token


def serve(engine, open_browser=True):
    server, _ = create_server(engine)
    url = f'http://127.0.0.1:{server.server_port}'
    print('PEPS MX: ' + url, flush=True)
    print('Datos locales: ' + str(engine.data_dir), flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop_event.set()
        server.server_close()
