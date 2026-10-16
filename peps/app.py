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

from .acquisition_coverage import acquisition_coverage
from .relevance import assessment_summary
from .engine import capacity
from .store import read_entities, now, canonical, digest
from .workflow import GuidedWorkflow, OperationTracker, WorkflowError


def _boolean(value, field):
    """Keep consent flags explicit: JSON strings such as "false" are not consent."""
    if not isinstance(value, bool):
        raise ValueError(field + ' debe ser booleano')
    return value


def _cache_options(data):
    mode = data.get('cache_mode', 'reuse')
    if mode not in ('reuse', 'revalidate', 'local'):
        raise ValueError('cache_mode debe ser reuse, revalidate o local')
    online = _boolean(data.get('online', False), 'online')
    if mode == 'local' and online:
        raise ValueError('Sólo caché local requiere online=false')
    if mode != 'local' and not online:
        raise ValueError('Active la conexión autorizada para descargar noticias')
    return mode, online


def create_server(engine, port=0):
    from .news import NewsAcquirer
    news = NewsAcquirer(engine)
    token = secrets.token_urlsafe(32)
    web = Path(__file__).parent / 'web'
    tracker = OperationTracker(store=engine.store)
    workflow = GuidedWorkflow(engine.store, sources_provider=news.sources)
    lock = threading.RLock()
    tickets = {}
    import_operations = {}
    import_ttl = 30 * 60

    def start_operation(kind, action, phase='', online=None, message=''):
        with lock:
            workflow.require(action, tracker.snapshot())
            operation_id = tracker.start(kind, phase=phase, message=message)
            if online is not None:
                engine.online = online
            engine.stop_event.clear()
            return operation_id

    def background(fn, online=None, kind='analysis', action='analyze', phase='', message=''):
        operation_id = start_operation(kind, action, phase, online, message)
        def execute():
            try:
                result = fn()
                paused = engine.stop_event.is_set() or (isinstance(result, dict) and
                    (result.get('cancelled') or result.get('pending', 0) or result.get('failed', 0) or result.get('status') == 'paused'))
                tracker.finish(status='paused' if paused else 'completed', result=result,
                               operation_id=operation_id,
                               message=(result.get('message') if isinstance(result, dict) else None) or
                                   ('Descarga en pausa. Puedes reanudarla o cerrar con la cobertura disponible.'
                                    if paused and kind in ('download', 'pipeline') else 'Proceso pausado.' if paused else 'Paso terminado.'))
            except Exception as exc:
                if kind in ('download', 'pipeline') and tracker.snapshot()['phase'] == 'download':
                    workflow.mark_download(status='failed', details={'error': str(exc)})
                tracker.finish(status='failed', message=f'{type(exc).__name__}: {exc}', operation_id=operation_id)
        threading.Thread(target=execute, daemon=True).start()
        return operation_id

    def synchronous(fn, kind, action):
        start_operation(kind, action, phase='scoring')
        try:
            result = fn()
            tracker.finish(result=result, message='Cambios guardados y evaluación actualizada.')
            return result
        except Exception as exc:
            tracker.finish(status='failed', message=str(exc))
            raise

    def acquire_news(resume=False, cache_mode='reuse'):
        workflow.mark_download(status='running', resume=resume,
                               details=None if resume else {'acquisition_context': {'search': news.search_config()}})
        settings = news.search_config()
        per_entity = 3 if settings['execution_mode'] == 'quick' else 10
        with engine.store.connection() as con:
            entities = con.execute('SELECT count(*) FROM entities').fetchone()[0]
        result = news.acquire(workers=8, seconds=settings['session_seconds'], max_articles=10000, queue=not resume,
                              progress=tracker.update, reset_stop=False, relevant_only=True,
                              per_entity_body_limit=per_entity, body_budget=min(10000, per_entity * entities),
                              cache_mode=cache_mode)
        incomplete = result.get('cancelled') or result.get('pending', 0) or result.get('failed', 0)
        status = workflow.mark_download(status='paused' if incomplete else 'completed', details=result)
        return dict(result, status='paused' if status == 'paused' else result.get('status', 'ok'))

    def run_pipeline(resume, cache_mode, analyze_after):
        downloaded = acquire_news(resume, cache_mode)
        if not analyze_after or downloaded.get('status') == 'paused' or engine.stop_event.is_set():
            return downloaded
        # The operation still owns the server lockout. Ignore only that busy
        # flag while checking every persisted analysis prerequisite again.
        ready = workflow.snapshot({'status': 'idle', 'busy': False})
        if not ready['actions']['analyze']['enabled']:
            return dict(downloaded, status='paused',
                        message='La búsqueda terminó con cobertura limitada. Revisa el resumen y elige «Analizar lo disponible» o amplía la búsqueda.')
        analyzed = analyze_bank()
        return dict(analyzed, download=downloaded,
                    message='Resultados preparados.' if analyzed.get('status') != 'paused' else 'Análisis en pausa.')

    def acquisition_context():
        """Archive the actual acquisition limits with each evaluation."""
        snapshot = workflow.snapshot(tracker.snapshot())
        coverage, counts, news_counts = snapshot['coverage'], snapshot['counts'], news.stats()
        archived = coverage.get('acquisition_context') or {}
        return {'mode': coverage['mode'], 'status': coverage['status'],
                'partial': bool(coverage.get('partial')), 'accepted_partial': bool(coverage.get('accepted_partial')),
                'pending_tasks': counts['pending_downloads'], 'failed_tasks': coverage['failed'],
                'articles': counts['articles'], 'full_articles': news_counts['scopes'].get('full_article', 0),
                'metadata_only_articles': sum(number for scope, number in news_counts['scopes'].items() if scope != 'full_article'),
                'search': copy.deepcopy(archived.get('search')), 'search_plan': copy.deepcopy(coverage.get('search_plan')),
                'planning_warnings': copy.deepcopy(coverage.get('plan_warnings', [])),
                'planning_truncated': bool(coverage.get('plan_truncated')),
                'limitations': copy.deepcopy(coverage.get('limitations', [])),
                'observed': acquisition_coverage(engine.store, news.sources()),
                'last_session_limits': copy.deepcopy(coverage.get('session_limits', {}))}

    def news_tasks(query):
        """Read a bounded queue page only when the user opens task details."""
        from .public_search import safe_result_url
        status = query.get('status', [''])[0]
        if status and status not in ('queued', 'deferred', 'running', 'blocked', 'dead', 'done', 'cancelled'):
            raise ValueError('Estado de tarea no válido')
        try:
            limit = int(query.get('limit', ['25'])[0])
            offset = int(query.get('offset', ['0'])[0])
        except (TypeError, ValueError):
            raise ValueError('Paginación no válida') from None
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000000:
            raise ValueError('Paginación fuera de rango')
        source = query.get('source_id', [''])[0]
        if len(source) > 200:
            raise ValueError('Fuente no válida')
        conditions, params = ["kind LIKE 'news_%'"], []
        if status:
            conditions.append('status=?')
            params.append(status)
        if source:
            conditions.append("json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END,'$.source_id')=?")
            params.append(source)
        where = ' AND '.join(conditions)
        with engine.store.connection() as con:
            total = con.execute('SELECT count(*) FROM jobs WHERE ' + where, params).fetchone()[0]
            rows = con.execute('SELECT id,kind,status,payload,attempts,due,error,updated_at FROM jobs WHERE ' + where +
                               ' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?', (*params, limit, offset)).fetchall()
        labels = {source['id']: source.get('source_name') or source.get('name') or source['id'] for source in news.sources()}
        stamp, items = time.time(), []
        for row in rows:
            payload = json.loads(row['payload'])
            source_id = payload.get('source_id')
            url = safe_result_url(payload.get('url') or payload.get('search_url') or '') or ''
            items.append({key: row[key] for key in ('id', 'kind', 'status', 'attempts', 'due', 'error', 'updated_at')})
            items[-1].update(source_id=source_id, label=labels.get(source_id, source_id or 'Documento'),
                             url=url, next_retry_seconds=max(0., row['due'] - stamp) if row['status'] == 'deferred' else None)
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'has_more': offset + len(items) < total}

    def analyze_bank():
        workflow.mark_analysis(status='running')
        attribution = news.attribute(limit=None, rescore=False, cancelable=True, progress=tracker.update)
        if engine.stop_event.is_set() or attribution.get('cancelled') or attribution.get('pending', 0):
            return {'status': 'paused', 'cancelled': engine.stop_event.is_set(), 'attribution': attribution}
        workflow.mark_analysis(status='scoring')
        scored = engine.rescore(progress=tracker.update, cancelable=True, acquisition=acquisition_context())
        if engine.stop_event.is_set():
            return {'status': 'paused', 'cancelled': True, 'scored': scored, 'attribution': attribution}
        result = {'scored': scored, 'attribution': attribution}
        workflow.mark_analysis(details=result)
        return result

    def stored_explanation(entity_id, run_id=None):
        """HTTP reads never launch scoring; computation belongs to step four."""
        if not run_id:
            workflow.require('view_results', tracker.snapshot())
        bundle = engine.store.score_run_bundle(entity_id, run_id=run_id)
        run = bundle['run']
        if not run:
            raise WorkflowError('Completa el paso «Analizar y calcular» para obtener el scoring.', 409)
        return {'entity': run['entity_snapshot'], 'result': run['result'],
                'explanation': run['result'].get('explainability', {}),
                'run': {'id': run['id'], 'created_at': run['created_at'], 'historical': bool(run_id)},
                'history': bundle['history'], 'evidence_count': len(run['evidence_manifest']),
                'assessment': run['result'].get('explainability', {}).get('assessment') or assessment_summary(run['result'])}

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
            workflow.require('import', tracker.snapshot())
            operation_id = tracker.start('import', phase='saving', message='Guardando la lista revisada…')
            engine.stop_event.clear()
            stamp = time.monotonic()
            operation = {'id': operation_id, 'status': 'queued', 'phase': 'saving',
                         'created_at': now(), 'preview_hash': preview_hash,
                         'request_hash': request_hash,
                         'updated_monotonic': stamp, 'result': None}
            import_operations[operation_id] = operation

        def execute_import():
            try:
                with lock:
                    operation.update(status='running', updated_monotonic=time.monotonic())
                report = commit_import(engine.store, payload)
                public = public_import_result(report)
                public.update(scored=0, scoring_status='not_started')
                tracker.update(phase='saving', completed=public.get('imported', 0),
                               total=public.get('imported', 0), unit='entidades', message='Lista guardada. Continúa con las fuentes.')
                with lock:
                    operation.update(status='completed', phase='done', result=public,
                                     updated_monotonic=time.monotonic())
                    tracker.finish(result={'operation_id': operation_id, **public}, message='Lista guardada. Continúa con las fuentes.')
            except Exception as exc:
                conflict = isinstance(exc, ImportConflict)
                reason = str(exc) if isinstance(exc, (ValueError, TypeError, KeyError)) else 'No se pudo guardar la importación. Revise la vista previa antes de reintentar.'
                with lock:
                    operation.update(status='error', phase='done', error=reason,
                        error_code='preview_stale' if conflict else 'invalid_import',
                        retry_preview=True, updated_monotonic=time.monotonic())
                    tracker.finish(status='failed', message=reason)
        threading.Thread(target=execute_import, daemon=True).start()
        return operation_id

    class Handler(BaseHTTPRequestHandler):
        server_version = 'WebScope/2'

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
                             "img-src 'self' data:; font-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            try:
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # Navigation and cancelled polling can close the browser socket
                # after a response was computed. The operation itself remains
                # tracked by the server; do not log a misleading worker crash.
                self.close_connection = True

        def authorized(self, api=True):
            expected = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected:
                self.reply({'error': 'Host no permitido'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + expected:
                self.reply({'error': 'Origen no permitido'}, 403)
                return False
            supplied_token = self.headers.get('X-PEPS-Token', '')
            if api and not (supplied_token.isascii() and secrets.compare_digest(supplied_token, token)):
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
                elif path in ('/app.js', '/import.js', '/workflow.js', '/style.css'):
                    self.reply((web / path[1:]).read_bytes(), content_type='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8')
                elif path in ('/fonts/Lato-Regular.woff2', '/fonts/Lato-Bold.woff2', '/fonts/SourceSerif4-Roman.woff2'):
                    self.reply((web / 'fonts' / Path(path).name).read_bytes(), content_type='font/woff2')
                elif path == '/api/stats':
                    operation = tracker.snapshot()
                    current_workflow = workflow.snapshot(operation)
                    counts = current_workflow['counts']
                    operation['task_progress']['estimates'] = tracker.estimate_phases('analysis', {
                        'attribution': counts['articles'], 'scoring': counts['entities']
                    }) + tracker.estimate_phases('download', {'download': None})
                    self.reply(dict(engine.store.stats(), online=engine.online, operation=operation,
                                    workflow=current_workflow, acquisition=acquisition_coverage(engine.store, news.sources()), capacity=capacity()))
                elif path == '/api/workflow':
                    self.reply(workflow.snapshot(tracker.snapshot()))
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
                    current = workflow.snapshot(tracker.snapshot())['actions']['view_results']['enabled']
                    rows = engine.store.list_entities(query.get('q', [''])[0], query.get('limit', [100])[0], query.get('offset', [0])[0],
                                                      summary=query.get('summary', ['0'])[0] == '1')
                    for row in rows:
                        row.pop('result', None)  # Internal SQL alias must not duplicate or expose stale scores.
                        row['score_available'] = row['score'] is not None
                        row['score_current'] = current and row['score_available']
                        if not current:
                            row['score'] = None
                            row.pop('assessment', None)
                    self.reply(rows)
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
                        self.reply(stored_explanation(entity_id, query.get('run_id', [None])[0]))
                elif path == '/api/run-evidence':
                    item = engine.store.run_evidence(query.get('run_id', [''])[0], query.get('id', [''])[0])
                    self.reply(item if item else {'error': 'Evidencia inexistente en esta evaluación'}, 200 if item else 404)
                elif path == '/api/sources':
                    self.reply(engine.sources())
                elif path == '/api/audit':
                    self.reply(engine.store.verify_audit())
                elif path == '/api/news':
                    value = {'stats': news.stats(), 'articles': news.articles(relevant_only=query.get('relevant', ['0'])[0] == '1'),
                             'acquisition': acquisition_coverage(engine.store, news.sources())}
                    if query.get('summary', ['0'])[0] != '1':
                        value.update(sources=news.sources(), plan=news.plan())
                    self.reply(value)
                elif path == '/api/news-search':
                    self.reply({'config': news.search_config(), 'plan': news.search_plan()})
                elif path == '/api/news-tasks':
                    self.reply(news_tasks(query))
                elif path == '/api/public-search':
                    from .public_search import public_search_links
                    entity = engine.store.get_entity(query.get('id', [''])[0])
                    if not entity:
                        self.reply({'error': 'Entidad inexistente'}, 404)
                    else:
                        self.reply(public_search_links(entity))
                elif path == '/api/export':
                    workflow.require('export', tracker.snapshot())
                    # Exportación completa y streaming: memoria acotada para bancos grandes.
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
                    self.send_header('Content-Disposition', 'attachment; filename=webscope-export.jsonl')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    for row in engine.store.export():
                        self.wfile.write((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8'))
                else:
                    self.reply({'error': 'No encontrado'}, 404)
            except (ValueError, TypeError, KeyError) as exc:
                self.reply({'error': str(exc)}, getattr(exc, 'status', 400))

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
                    workflow.require('import', tracker.snapshot())
                    self.reply(preview_import(engine.store, data))
                    return
                elif self.path == '/api/import-confirm':
                    self.reply({'operation_id': queue_import(data)}, 202)
                    return
                elif self.path == '/api/import-entities':
                    text = str(data['text'])
                    operation_id = background(lambda: {'imported': engine.store.import_entities(read_entities(text)),
                        'scored': 0, 'scoring_status': 'not_started'}, kind='import', action='import', phase='saving')
                    self.reply({'ok': True, 'operation_id': operation_id})
                    return
                elif self.path == '/api/import-documents':
                    text = str(data['text'])
                    operation_id = background(lambda: {'imported': engine.import_documents(
                        json.loads(x) for x in text.splitlines() if x.strip()), 'scored': 0,
                        'scoring_status': 'not_started'}, kind='document_import', action='import', phase='saving')
                    self.reply({'ok': True, 'operation_id': operation_id})
                    return
                elif self.path in ('/api/score', '/api/workflow-analyze', '/api/news-attribute'):
                    if self.path == '/api/news-attribute':
                        _boolean(data.get('force', False), 'force')
                    operation_id = background(analyze_bank, kind='analysis', action='analyze', phase='attribution',
                                              message='Vinculando noticias con las entidades de tu lista…')
                    self.reply({'ok': True, 'operation_id': operation_id})
                    return
                elif self.path == '/api/workflow-run':
                    cache_mode, online = _cache_options(data)
                    resume = _boolean(data.get('resume', False), 'resume')
                    analyze_after = _boolean(data.get('analyze_after_download', False), 'analyze_after_download')
                    settings = data.get('search')
                    if settings is not None and not isinstance(settings, dict):
                        raise ValueError('search requiere un objeto de configuración')
                    if resume and settings:
                        raise ValueError('Reanude con el alcance guardado; cambie la búsqueda al iniciar una nueva adquisición')
                    with lock:
                        current = workflow.require('configure_sources', tracker.snapshot())
                        if not current['counts']['entities']:
                            raise WorkflowError('Importe primero un banco de entidades.')
                        if current['mode'] != 'online':
                            raise WorkflowError('Seleccione el modo de búsqueda para descargar o consultar la caché.')
                        if not current['counts']['sources']:
                            raise WorkflowError('Seleccione una fuente habilitada con permisos aprobados.')
                        if resume and not current['coverage'].get('resume_available'):
                            raise WorkflowError('La cola no corresponde al alcance vigente o no tiene tareas pendientes; inicie una nueva búsqueda.')
                        if settings is not None:
                            news.set_search_config(settings)
                        if not resume and not news.search_plan()['query_count']:
                            raise WorkflowError('El plan no contiene consultas. Ajuste las fuentes o el tipo de búsqueda antes de iniciar.')
                        workflow.confirm_sources(tracker.snapshot())
                        operation_id = background(lambda: run_pipeline(resume, cache_mode, analyze_after), online=online,
                                                  kind='pipeline' if analyze_after else 'download', action='download', phase='download',
                                                  message='Consultando la caché local…' if cache_mode == 'local' else
                                                      'Reanudando las tareas pendientes…' if resume else 'Preparando la búsqueda…')
                    self.reply({'ok': True, 'operation_id': operation_id})
                    return
                elif self.path == '/api/workflow-analyze-available':
                    with lock:
                        # The user's explicit action records the incomplete
                        # coverage and starts analysis without another prompt.
                        workflow.accept_partial(tracker.snapshot())
                        operation_id = background(analyze_bank, kind='analysis', action='analyze', phase='attribution',
                                                  message='Analizando la información disponible con su cobertura registrada…')
                    self.reply({'ok': True, 'operation_id': operation_id})
                    return
                elif self.path == '/api/workflow-mode':
                    with lock:
                        value = workflow.set_mode(data.get('mode'), tracker.snapshot())
                        if value['mode'] == 'local':
                            engine.online = False
                        self.reply(value)
                    return
                elif self.path == '/api/workflow-confirm-sources':
                    with lock:
                        self.reply(workflow.confirm_sources(tracker.snapshot()))
                    return
                elif self.path in ('/api/workflow-accept-partial', '/api/workflow-finish-download'):
                    with lock:
                        self.reply(workflow.accept_partial(tracker.snapshot()))
                    return
                elif self.path == '/api/collect':
                    if not _boolean(data.get('online', False), 'online'):
                        raise ValueError('Active red autorizada para recolectar')
                    def collect():
                        queued = engine.queue_discovery()
                        return {'queued': queued, **engine.run(workers=4, max_jobs=1000, seconds=120,
                                                               progress=tracker.update, reset_stop=False)}
                    background(collect, online=True, kind='download', action='download', phase='download')
                elif self.path == '/api/stop':
                    with lock:
                        expected = data.get('operation_id')
                        if expected is not None and expected != tracker.snapshot()['operation_id']:
                            raise WorkflowError('La operación cambió. Actualiza su estado antes de pedir una pausa.')
                        tracker.request_cancel()
                        engine.stop_event.set()
                elif self.path == '/api/retry':
                    with lock:
                        workflow.require('configure_sources', tracker.snapshot())
                        engine.store.retry_failed()
                elif self.path == '/api/news-retry':
                    with lock:
                        workflow.require('configure_sources', tracker.snapshot())
                        result = engine.store.retry_news_jobs(data.get('job_ids'))
                    self.reply({'ok': True, **result, 'requeued': result['retried']})
                    return
                elif self.path == '/api/source':
                    enabled = _boolean(data['enabled'], 'enabled')
                    approved = _boolean(data['terms_approved'], 'terms_approved')
                    with lock:
                        workflow.require('configure_sources', tracker.snapshot())
                        engine.set_source(data['id'], enabled, approved)
                elif self.path == '/api/review':
                    if not data.get('expected_run_id'):
                        raise ValueError('Abra la evaluación actual antes de registrar una revisión')
                    def review():
                        entity_id = engine.store.review(data['id'], data['status'], data['actor'], data['reason'],
                            data.get('event_id'), data.get('identity_spans'), expected_run_id=data['expected_run_id'])
                        engine.rescore(entity_id, progress=tracker.update, acquisition=acquisition_context())
                        workflow.mark_analysis()
                        return {'entity_id': entity_id}
                    synchronous(review, kind='review', action='review')
                elif self.path == '/api/replay':
                    if tracker.busy:
                        raise WorkflowError('Espera a que termine el paso actual antes de reproducir una evaluación.')
                    self.reply(engine.replay_score(str(data['run_id'])))
                    return
                elif self.path == '/api/news-acquire':
                    cache_mode, online = _cache_options(data)
                    resume = _boolean(data.get('resume', False), 'resume')
                    with lock:
                        value = workflow.require('download', tracker.snapshot())
                        if resume and not value['coverage'].get('resume_available'):
                            raise WorkflowError('La cola no corresponde al alcance vigente o no tiene tareas pendientes; inicie una nueva búsqueda.')
                        if not resume and not news.search_plan()['query_count']:
                            raise WorkflowError('El plan no contiene consultas. Vuelva a Preparar fuentes: habilite una fuente de búsqueda dirigida o elija Feeds generales, y confirme el alcance.')
                        operation_id = background(lambda: acquire_news(resume, cache_mode), online=online, kind='download', action='download', phase='download',
                                                  message='Reanudando las tareas pendientes…' if resume else 'Preparando las búsquedas y descubriendo noticias…')
                    self.reply({'ok': True, 'operation_id': operation_id})
                    return
                elif self.path == '/api/news-search':
                    with lock:
                        workflow.require('configure_sources', tracker.snapshot())
                        news.set_search_config(data)
                        self.reply({'config': news.search_config(), 'plan': news.search_plan()})
                    return
                elif self.path == '/api/news-source':
                    values = [_boolean(data['enabled'], 'enabled'), _boolean(data['terms_approved'], 'terms_approved'),
                              _boolean(data.get('download_articles', False), 'download_articles'),
                              _boolean(data.get('article_terms_approved', False), 'article_terms_approved')]
                    with lock:
                        workflow.require('configure_sources', tracker.snapshot())
                        news.set_source(data['id'], *values)
                elif self.path == '/api/backup':
                    from .store import now
                    destination = engine.data_dir / 'backups' / ('webscope-' + now().replace(':', '-').replace('+', '_') + '.sqlite3')
                    engine.store.backup(destination)
                    self.reply({'file': str(destination)})
                    return
                elif self.path == '/api/export-ticket':
                    ticket = secrets.token_urlsafe(32)
                    with lock:
                        workflow.require('export', tracker.snapshot())
                        for old in [k for k, expiry in tickets.items() if expiry <= time.time()]:
                            tickets.pop(old, None)
                        tickets[ticket] = time.time() + 30
                    self.reply({'url': '/api/export?ticket=' + ticket})
                    return
                elif self.path == '/api/shutdown':
                    with lock:
                        workflow.require('configure_sources', tracker.snapshot())
                    engine.stop_event.set()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self.reply({'error': 'No encontrado'}, 404)
                    return
                self.reply({'ok': True})
            except (ValueError, KeyError, TypeError) as exc:
                self.reply({'error': str(exc)}, getattr(exc, 'status', 400))

    return ThreadingHTTPServer(('127.0.0.1', port), Handler), token


def serve(engine, open_browser=True):
    server, _ = create_server(engine)
    url = f'http://127.0.0.1:{server.server_port}'
    print('WebScope: ' + url, flush=True)
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
