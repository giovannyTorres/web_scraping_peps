"""Persisted prerequisites and honest, thread-safe progress for the local UI.

Reading a workflow never attributes evidence or calculates scores. Revisions
come from existing audit events and small SQL aggregates, not document hashes.
"""
from __future__ import annotations

from copy import deepcopy
import math
import threading
import time
import uuid

from .store import canonical, digest, now


class WorkflowError(ValueError):
    def __init__(self, reason, status=409):
        super().__init__(reason)
        self.reason, self.status = reason, status


class OperationTracker:
    """One operation at a time; counts are reported work, never simulated time."""

    _COUNTERS = ('processed', 'pending', 'deferred', 'blocked', 'dead', 'failed', 'discovery_pending',
                 'unreported_completed', 'ready', 'active_tasks', 'done', 'queued', 'running')
    _FLAGS = ('waiting', 'queue_complete', 'cancelled')
    _TIMES = ('next_retry_at', 'next_retry_seconds', 'wait_seconds')
    _TEXT = ('next_retry', 'current_source', 'job_kind', 'last_error')
    _STOP_REASONS = ('completed', 'cancelled', 'time_limit', 'job_limit', 'backoff', 'scheduled', 'external_lease')

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.cancel_event = threading.Event()
        self._started = self._phase_started = self._clock()
        self._phase_baseline = 0
        self._ended = None
        self._state = {'id': None, 'operation_id': None, 'kind': None, 'status': 'idle',
                       'phase': '', 'completed': 0, 'total': None, 'unit': '', 'message': '', 'result': None, 'details': {}}

    @property
    def busy(self):
        with self._lock:
            return self._state['status'] in ('running', 'cancel_requested')

    def start(self, kind, phase='', total=None, message='', operation_id=None, unit=''):
        with self._lock:
            if self.busy:
                raise WorkflowError('Hay una operación en curso; espere a que termine.')
            self._validate_count(total, 'total', optional=True)
            identifier = operation_id or uuid.uuid4().hex
            self.cancel_event.clear()
            self._started = self._phase_started = self._clock()
            self._phase_baseline, self._ended = 0, None
            self._state = {'id': identifier, 'operation_id': identifier, 'kind': kind, 'status': 'running',
                           'phase': phase, 'completed': 0, 'total': total, 'unit': unit, 'message': message, 'result': None, 'details': {}}
            return identifier

    @classmethod
    def _details(cls, values):
        """Keep public progress metadata bounded and free of transport objects."""
        result = {}
        for key in cls._COUNTERS:
            value = values.get(key)
            if type(value) is int and 0 <= value <= 2 ** 63 - 1:
                result[key] = value
        for key in cls._FLAGS:
            if type(values.get(key)) is bool:
                result[key] = values[key]
        for key in cls._TIMES:
            if key not in values:
                continue
            value = values[key]
            if value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0):
                result[key] = value
        for key in cls._TEXT:
            if key in values and (values[key] is None or isinstance(values[key], str)):
                result[key] = values[key][:2000] if isinstance(values[key], str) else None
        if 'stop_reason' in values and (values['stop_reason'] is None or values['stop_reason'] in cls._STOP_REASONS):
            result['stop_reason'] = values['stop_reason']
        if isinstance(values.get('jobs'), dict):
            result['jobs'] = {key: value for key, value in values['jobs'].items()
                              if key in ('queued', 'deferred', 'running', 'done', 'blocked', 'dead', 'cancelled')
                              and type(value) is int and 0 <= value <= 2 ** 63 - 1}
        if 'wait_seconds' in result and 'next_retry_seconds' not in result:
            result['next_retry_seconds'] = result['wait_seconds']
        return result

    @staticmethod
    def _validate_count(value, field, optional=False):
        if optional and value is None:
            return
        if type(value) is not int or value < 0:
            raise WorkflowError(field + ' requiere un entero no negativo.', 400)

    def update(self, event=None, **kwargs):
        if event is not None and not isinstance(event, dict):
            raise WorkflowError('El progreso requiere un objeto.', 400)
        values = {**(event or {}), **kwargs}
        with self._lock:
            if not self.busy:
                return False
            if values.get('operation_id', self._state['operation_id']) != self._state['operation_id']:
                return False
            completed, total = values.get('completed', self._state['completed']), values.get('total', self._state['total'])
            self._validate_count(completed, 'completed')
            self._validate_count(total, 'total', optional=True)
            if total is not None and completed > total:
                raise WorkflowError('El trabajo completado no puede exceder el total.', 400)
            phase = values.get('phase', self._state['phase'])
            if phase != self._state['phase']:
                self._phase_started, self._phase_baseline = self._clock(), completed
            elif completed < self._state['completed']:
                raise WorkflowError('El progreso no puede retroceder dentro de una fase.', 400)
            for key in ('phase', 'message', 'unit'):
                if key in values:
                    self._state[key] = str(values[key])
            self._state.update(completed=completed, total=total)
            self._state['details'].update(self._details(values))
            return True

    def finish(self, status='completed', message='', result=None, operation_id=None):
        if status not in ('completed', 'paused', 'failed'):
            raise WorkflowError('Estado final de operación inválido.', 400)
        with self._lock:
            if not self.busy or (operation_id is not None and operation_id != self._state['operation_id']):
                return False
            self._state.update(status=status, message=message or self._state['message'], result=deepcopy(result))
            if isinstance(result, dict):
                self._state['details'].update(self._details(result))
            self._ended = self._clock()
            return True

    def request_cancel(self):
        with self._lock:
            if not self.busy:
                raise WorkflowError('No hay una operación activa que pausar.')
            self.cancel_event.set()
            self._state.update(status='cancel_requested', message='Pausa solicitada; esperando que termine el trabajo actual.')
            return self._state['operation_id']

    def snapshot(self):
        with self._lock:
            value = deepcopy(self._state)
            value.update(deepcopy(value['details']))
            elapsed = max(0., (self._ended if self._ended is not None else self._clock()) - self._started)
            completed, total = value['completed'], value['total']
            percent = None if total is None or total == 0 else min(100, 100 * completed / total)
            if percent is not None and value['status'] != 'completed':
                percent = min(99., percent)
            phase_elapsed = max(0., self._clock() - self._phase_started)
            units = completed - self._phase_baseline
            eta = None
            if self.busy and not value.get('waiting') and total is not None and total > completed and units >= 3 and phase_elapsed >= 3:
                eta = max(1, math.ceil((total - completed) * phase_elapsed / units))
            if value.get('next_retry_at') is not None:
                value['next_retry_seconds'] = max(0., round(value['next_retry_at'] - time.time(), 3))
                value['details']['next_retry_seconds'] = value['next_retry_seconds']
            value.update(busy=self.busy, percent=round(percent, 1) if percent is not None else None,
                         elapsed_seconds=round(elapsed, 3), eta_seconds=eta,
                         last_result=deepcopy(value['result']), label=value['message'],
                         error=value['message'] if value['status'] == 'failed' else None)
            return value


class GuidedWorkflow:
    STEPS = (('data', 'Importar banco'), ('sources', 'Configurar fuentes'),
             ('download', 'Descargar documentos'), ('analysis', 'Analizar evidencia'), ('results', 'Consultar resultados'))
    ACTIONS = ('import', 'configure_sources', 'download', 'analyze', 'view_results', 'export', 'review', 'accept_partial', 'finish_download')

    def __init__(self, store, sources_provider=None):
        self.store, self.sources_provider = store, sources_provider or (lambda: [])
        with self.store.connection() as con:
            con.execute('CREATE TABLE IF NOT EXISTS workflow_state(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL,updated_at TEXT NOT NULL)')
            con.execute('INSERT OR IGNORE INTO workflow_state VALUES(1,?,?)',
                        (canonical({'version': 1, 'mode': 'online'}), now()))

    @staticmethod
    def _state(con):
        import json
        return json.loads(con.execute('SELECT payload FROM workflow_state WHERE id=1').fetchone()[0])

    @staticmethod
    def _save(con, state):
        con.execute('UPDATE workflow_state SET payload=?,updated_at=? WHERE id=1', (canonical(state), now()))

    @staticmethod
    def _last_event(con, *actions):
        return con.execute('SELECT COALESCE(MAX(seq),0) FROM audit WHERE action IN (' + ','.join('?' for _ in actions) + ')', actions).fetchone()[0]

    @staticmethod
    def _operation(operation):
        if hasattr(operation, 'snapshot'):
            operation = operation.snapshot()
        return operation if isinstance(operation, dict) else {}

    def migrate_disabled_catalogue(self, previous_sources, current_sources):
        """Rebind only an identical recorded scope plus strictly disabled additions.

        The original source audit revision, bank and corpus must still match.
        No historical period, permission, source definition or score is invented.
        """
        if (not isinstance(previous_sources, list) or not isinstance(current_sources, list) or
                len(current_sources) <= len(previous_sources) or
                canonical(current_sources[:len(previous_sources)]) != canonical(previous_sources) or
                any(not isinstance(source, dict) for source in current_sources)):
            return False
        identifiers = [source.get('id') for source in current_sources]
        if any(not isinstance(identifier, str) or not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
            return False
        additions = current_sources[len(previous_sources):]
        permission_fields = ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved')
        if any(source.get(field, False) is not False for source in additions for field in permission_fields):
            return False
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            _, signature, coverage, _ = self._read(con)
            old_source_signature = [signature['sources'][0], digest(canonical(previous_sources))]
            new_source_signature = [signature['sources'][0], digest(canonical(current_sources))]
            receipt = state.get('download') or {}
            old_acquisition = {**self._acquisition_signature(signature), 'sources': old_source_signature}
            if (signature['sources'] != new_source_signature or
                    receipt.get('signature') != old_acquisition or
                    state.get('sources_confirmed') != {'sources': old_source_signature, 'mode': state.get('mode', 'online')}):
                return False
            state['sources_confirmed']['sources'] = deepcopy(new_source_signature)
            state['download']['signature']['sources'] = deepcopy(new_source_signature)
            for key in ('partial_signature',):
                if state.get(key) == old_acquisition:
                    state[key]['sources'] = deepcopy(new_source_signature)
            acceptance = state.get('partial_acceptance') or {}
            if acceptance.get('signature') == old_acquisition:
                acceptance['signature']['sources'] = deepcopy(new_source_signature)
            # The acquisition remains usable; a new analysis must explicitly
            # consume it rather than relabelling an older scoring snapshot.
            state.pop('analysis', None)
            state.pop('analysis_in_progress', None)
            self._save(con, state)
            self.store._audit(con, 'news_disabled_catalogue_migrated', {
                'previous_sources_hash': old_source_signature[1], 'sources_hash': new_source_signature[1],
                'source_audit_revision': signature['sources'][0],
                'added_disabled_sources': [source['id'] for source in additions],
                'historical_context_preserved': True})
        return True

    def _read(self, con):
        try:
            sources = self.sources_provider()
            if not isinstance(sources, list) or any(not isinstance(source, dict) for source in sources):
                raise ValueError('Fuentes inválidas')
            source_error = None
        except (ValueError, TypeError, OSError):
            sources, source_error = [], 'Revise la configuración de fuentes.'
        bank = con.execute("SELECT value FROM meta WHERE key='news_identity_revision'").fetchone()
        documents = con.execute('SELECT count(*),COALESCE(MAX(rowid),0) FROM documents').fetchone()
        counts = {'entities': con.execute('SELECT count(*) FROM entities').fetchone()[0],
                  'documents': documents[0], 'sources': sum(s.get('enabled') is True and s.get('terms_approved') is True for s in sources),
                  'articles': 0, 'pending_downloads': 0,
                  'scores': con.execute('SELECT count(*) FROM scores').fetchone()[0]}
        queue_rows = con.execute("SELECT status,count(*),MAX(rowid),sum(attempts),MAX(updated_at),"
                                 "sum(COALESCE(lease_until,0)),MIN(due),MAX(due) FROM jobs "
                                 "WHERE kind GLOB 'news_*' GROUP BY status ORDER BY status").fetchall()
        statuses = {row[0]: row[1] for row in queue_rows}
        counts['pending_downloads'] = sum(statuses.get(status, 0) for status in ('queued', 'deferred', 'running'))
        observed_at = time.time()
        active_running = con.execute("SELECT count(*) FROM jobs WHERE kind GLOB 'news_*' AND status='running' AND lease_until>?", (observed_at,)).fetchone()[0]
        ready = con.execute("SELECT count(*) FROM jobs WHERE kind GLOB 'news_*' AND attempts<5 AND due<=? "
                            "AND (status IN ('queued','deferred') OR (status='running' AND COALESCE(lease_until,0)<=?))",
                            (observed_at, observed_at)).fetchone()[0]
        next_retry_at = con.execute("SELECT MIN(CASE WHEN status='running' THEN MAX(due,lease_until) ELSE due END) "
                                    "FROM jobs WHERE kind GLOB 'news_*' AND attempts<5 AND "
                                    "((status IN ('queued','deferred') AND due>?) OR (status='running' AND lease_until>?))",
                                    (observed_at, observed_at)).fetchone()[0]
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        unfetched = saturated = 0
        if 'news_articles' in tables:
            counts['articles'], unfetched = con.execute('SELECT count(*),COALESCE(sum(document_id IS NULL),0) FROM news_articles').fetchone()
        if 'news_query_windows' in tables:
            saturated = con.execute('SELECT count(*) FROM news_query_windows WHERE saturated=1').fetchone()[0]
        # Attribution may create discovery-only placeholders. They do not add
        # source bodies and must not invalidate the analysis that creates them.
        bodies = con.execute("SELECT count(*),COALESCE(MAX(rowid),0) FROM documents WHERE source_kind!='discovery'").fetchone()
        signature = {'bank': int(bank[0]) if bank else 0,
                     'corpus': [self._last_event(con, 'news_article_acquired', 'news_discovery_completed'), bodies[0], bodies[1]],
                     'sources': [self._last_event(con, 'news_source_configured', 'source_configured', 'news_search_configured'), digest(canonical(sources))],
                     'review': self._last_event(con, 'evidence_review'),
                     'identity': self._last_event(con, 'news_identity_resolved', 'news_identity_retired')}
        coverage = {'pending_downloads': counts['pending_downloads'], 'queued': statuses.get('queued', 0),
                    'deferred': statuses.get('deferred', 0), 'running': statuses.get('running', 0),
                    'active_running': active_running, 'expired_running': statuses.get('running', 0) - active_running,
                    'worker_active': bool(active_running), 'ready': ready,
                    'done': statuses.get('done', 0), 'blocked': statuses.get('blocked', 0),
                    'dead': statuses.get('dead', 0), 'cancelled': statuses.get('cancelled', 0),
                    'next_retry_at': next_retry_at,
                    'next_retry_seconds': max(0., next_retry_at - observed_at) if next_retry_at is not None else None,
                    'queue_signature': digest(canonical([list(row) for row in queue_rows])),
                    'failed': statuses.get('dead', 0) + statuses.get('blocked', 0),
                    'unfetched_articles': unfetched, 'saturated_windows': saturated,
                    'insufficient_documents': counts['documents'] == 0}
        return counts, signature, coverage, source_error

    @staticmethod
    def _acquisition_signature(signature):
        return {key: signature[key] for key in ('bank', 'corpus', 'sources')}

    def _partial_current(self, state, signature, coverage):
        acquisition = self._acquisition_signature(signature)
        if (state.get('download') or {}).get('signature') != acquisition:
            return False
        receipt = state.get('partial_acceptance')
        if receipt:
            return receipt.get('signature') == acquisition and receipt.get('queue_signature') == coverage['queue_signature']
        # Receipts written by 2.3.1 only allowed an empty inactive queue.
        return state.get('partial_signature') == acquisition and not coverage['pending_downloads']

    @staticmethod
    def _plan_summary(details):
        details = details if isinstance(details, dict) else {}
        queued = details.get('queued') or {}
        plan = queued.get('search_plan') if isinstance(queued, dict) else None
        plan = plan or details.get('search_plan')
        if not isinstance(plan, dict):
            return None
        fields = ('config', 'sources', 'warnings', 'truncated', 'queries_required', 'query_count',
                  'entities_planned', 'entities_covered', 'entities_total', 'coverage',
                  'requested_start', 'requested_end_exclusive')
        return {key: deepcopy(plan[key]) for key in fields if key in plan}

    def snapshot(self, operation=None):
        op = self._operation(operation)
        with self.store.connection() as con:
            con.execute('BEGIN')
            state = self._state(con)
            counts, signature, coverage, source_error = self._read(con)
        mode, busy = state.get('mode', 'online'), bool(op.get('busy') or op.get('status') in ('running', 'cancel_requested'))
        sources_confirmed = state.get('sources_confirmed') == {'sources': signature['sources'], 'mode': mode}
        pending = counts['pending_downloads'] > 0
        receipt = state.get('download') or {}
        acquisition_current = receipt.get('signature') == self._acquisition_signature(signature)
        previous_signature = receipt.get('signature') or {}
        resume_scope_current = all(previous_signature.get(key) == signature[key] for key in ('bank', 'sources'))
        coverage['resume_available'] = bool(pending and resume_scope_current)
        coverage['needs_new_search'] = bool(pending and not coverage['resume_available'])
        coverage['acquisition_current'] = acquisition_current
        active = busy or bool(coverage['active_running'])
        coverage['worker_active'] = active
        download_details = receipt.get('details') or {}
        plan = self._plan_summary(download_details)
        coverage['search_plan'] = plan
        coverage['acquisition_context'] = deepcopy(download_details.get('acquisition_context')) if isinstance(download_details, dict) else None
        coverage['plan_truncated'] = bool(plan and plan.get('truncated'))
        coverage['plan_warnings'] = deepcopy(plan.get('warnings', [])) if plan else []
        limited_plan = bool(plan and (plan.get('coverage') == 'best_effort_not_exhaustive' or
                            any(source.get('complete_archive') is False for source in plan.get('sources', []) if isinstance(source, dict))))
        coverage['partial'] = bool(pending or coverage['failed'] or coverage['unfetched_articles'] or coverage['saturated_windows'] or coverage['insufficient_documents'] or coverage['plan_truncated'] or limited_plan)
        coverage['accepted_partial'] = self._partial_current(state, signature, coverage)
        coverage['closed_partial'] = coverage['accepted_partial'] and receipt.get('status') == 'closed_partial'
        coverage['limitations'] = deepcopy((state.get('partial_acceptance') or {}).get('limitations'))
        coverage['stop_reason'] = download_details.get('stop_reason') if isinstance(download_details, dict) else None
        coverage['waiting'] = bool(coverage['deferred'] and not coverage['ready'] and not active)
        coverage['status'] = receipt.get('status', 'not_started')
        coverage['mode'] = mode
        downloaded = bool(acquisition_current and not active and
                          ((receipt.get('status') == 'completed' and not coverage['partial']) or coverage['accepted_partial']))
        local = mode == 'local'
        analysis_signature = {**signature, 'mode': mode}
        analysis_current = bool((state.get('analysis') or {}).get('signature') == analysis_signature and
                                counts['scores'] == counts['entities'] and counts['entities'] > 0 and not active)
        if not local and not downloaded:
            analysis_current = False
        if not sources_confirmed:
            analysis_current = False
        active_reason = 'Hay una operación en curso; espere a que termine.'
        pending_reason = 'Hay descargas pendientes; reanude o finalice explícitamente la descarga con la cobertura disponible.'
        data_reason = 'Importe primero un banco de entidades.'
        sources_reason = source_error or ('Confirme las fuentes y el modo para continuar.' if local or counts['sources'] else 'Seleccione una fuente habilitada con permisos aprobados.')
        download_reason = 'Complete la descarga o acepte explícitamente su cobertura parcial al terminar.'
        analyze_reason = (active_reason if active else data_reason if not counts['entities']
                          else sources_reason if not sources_confirmed else '' if local else sources_reason if not counts['sources'] else '' if downloaded else download_reason)
        if not active and not local and pending and not downloaded and counts['entities'] and sources_confirmed:
            analyze_reason = pending_reason
        actions = {action: {'enabled': True, 'reason': ''} for action in self.ACTIONS}
        for action in ('import', 'configure_sources'):
            actions[action] = {'enabled': not active, 'reason': active_reason if active else ''}
        actions['download'] = {'enabled': not active and bool(counts['entities']) and not local and bool(counts['sources']) and sources_confirmed,
                               'reason': active_reason if active else data_reason if not counts['entities'] else
                                         'Seleccione el modo online para descargar.' if local else sources_reason if not counts['sources'] or not sources_confirmed else ''}
        actions['analyze'] = {'enabled': not bool(analyze_reason), 'reason': analyze_reason}
        partial_reason = (active_reason if active else
                          'La cobertura parcial se acepta en modo online.' if local else
                          sources_reason if not sources_confirmed else
                          '' if acquisition_current and receipt.get('status') in ('completed', 'paused', 'failed', 'closed_partial') else
                          'La adquisición no corresponde a los datos vigentes; confirme las fuentes y reanude la descarga antes de finalizarla.')
        actions['accept_partial'] = {'enabled': not bool(partial_reason), 'reason': partial_reason}
        actions['finish_download'] = deepcopy(actions['accept_partial'])
        for action in ('view_results', 'export', 'review'):
            reason = active_reason if active else '' if analysis_current else 'Ejecute el análisis de los datos vigentes.'
            actions[action] = {'enabled': not bool(reason), 'reason': reason}
        steps = []
        for number, (step_id, label) in enumerate(self.STEPS, 1):
            if step_id == 'data':
                status, reason = ('complete', '') if counts['entities'] else ('ready', data_reason)
            elif step_id == 'sources':
                status, reason = ('complete', 'Modo local confirmado explícitamente.' if local else '') if sources_confirmed else ('ready' if counts['entities'] else 'blocked', sources_reason)
            elif step_id == 'download':
                status, reason = ('complete', 'Modo local: no se solicita adquisición de red.') if local else ('complete', 'Descarga finalizada con cobertura parcial; las tareas pendientes se conservan.' if coverage['closed_partial'] else '') if downloaded else ('paused' if receipt.get('status') in ('paused', 'closed_partial') else 'error' if receipt.get('status') == 'failed' else 'ready' if actions['download']['enabled'] else 'blocked', actions['download']['reason'] or download_reason)
            elif step_id == 'analysis':
                status, reason = ('complete', '') if analysis_current else ('ready' if actions['analyze']['enabled'] else 'blocked', analyze_reason)
            else:
                status, reason = ('ready', '') if actions['view_results']['enabled'] else ('blocked', actions['view_results']['reason'])
            operation_step = {'import': 'data', 'download': 'download', 'acquire': 'download', 'analysis': 'analysis', 'analyze': 'analysis', 'rescore': 'analysis'}.get(op.get('kind'))
            if busy and operation_step == step_id:
                status, reason = 'running', op.get('message') or active_reason
            steps.append({'id': step_id, 'number': number, 'label': label, 'status': status, 'reason': reason})
        recommended = next((step['id'] for step in steps if step['status'] != 'complete'), 'results')
        return {'mode': mode, 'sources_confirmed': sources_confirmed, 'steps': steps, 'recommended_step': recommended, 'actions': actions,
                'counts': counts, 'analysis_current': analysis_current, 'coverage': coverage}

    def require(self, action, operation=None):
        if action not in self.ACTIONS:
            raise WorkflowError('Acción de proceso desconocida.', 400)
        value = self.snapshot(operation)
        if not value['actions'][action]['enabled']:
            raise WorkflowError(value['actions'][action]['reason'])
        return value

    def set_mode(self, mode, operation=None):
        if mode not in ('local', 'online'):
            raise WorkflowError('El modo debe ser local u online.', 400)
        self.require('configure_sources', operation)
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            if state.get('mode') != mode:
                state['mode'] = mode
                state.pop('sources_confirmed', None)
                state.pop('analysis', None)
                self._save(con, state)
        return self.snapshot(operation)

    def confirm_sources(self, operation=None):
        value = self.require('configure_sources', operation)
        if value['mode'] == 'online' and not value['counts']['sources']:
            raise WorkflowError('Seleccione una fuente habilitada con permisos aprobados.', 400)
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            _, signature, _, _ = self._read(con)
            state['sources_confirmed'] = {'sources': signature['sources'], 'mode': state.get('mode', 'online')}
            self._save(con, state)
        return self.snapshot(operation)

    def mark_download(self, status='completed', details=None, *, resume=False):
        if status not in ('running', 'completed', 'paused', 'failed'):
            raise WorkflowError('Estado de adquisición inválido.', 400)
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            _, signature, coverage, _ = self._read(con)
            previous = state.get('download') or {}
            acquisition = self._acquisition_signature(signature)
            if status == 'running' and resume and any((previous.get('signature') or {}).get(key) != signature[key] for key in ('bank', 'sources')):
                raise WorkflowError('La cola pertenece a otra configuración o banco; inicie una nueva búsqueda para los datos vigentes.')
            resuming = bool(resume if status == 'running' else previous.get('resuming'))
            if resuming and isinstance(previous.get('details'), dict):
                prior = previous['details']
                details = deepcopy(details) if isinstance(details, dict) else {}
                for key in ('queued', 'search_plan', 'acquisition_context'):
                    if details.get(key) is None and key in prior:
                        details[key] = deepcopy(prior[key])
                details['resumed_existing_queue'] = True
            if status != 'running' and previous.get('status') == 'running' and isinstance(previous.get('details'), dict):
                details = deepcopy(details) if isinstance(details, dict) else {}
                if details.get('acquisition_context') is None and 'acquisition_context' in previous['details']:
                    details['acquisition_context'] = deepcopy(previous['details']['acquisition_context'])
            if status != 'running' and previous.get('status') == 'running':
                for key in ('bank', 'sources'):
                    acquisition[key] = previous['signature'][key]
            if status == 'completed' and coverage['pending_downloads']:
                status = 'paused'
            state['download'] = {'status': status, 'signature': acquisition, 'at': now(), 'details': deepcopy(details), 'resuming': resuming}
            state.pop('analysis', None)
            state.pop('partial_signature', None)
            state.pop('partial_acceptance', None)
            self._save(con, state)
        return status

    def accept_partial(self, operation=None):
        op = self._operation(operation)
        if op.get('busy') or op.get('status') in ('running', 'cancel_requested'):
            raise WorkflowError('Espere a que termine la operación para aceptar cobertura parcial.')
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            counts, signature, coverage, _ = self._read(con)
            receipt = state.get('download') or {}
            if state.get('mode') == 'local':
                raise WorkflowError('La cobertura parcial se acepta en modo online.')
            if coverage['active_running']:
                raise WorkflowError('Hay descargas en ejecución; espere a que terminen antes de finalizar la descarga.')
            if state.get('sources_confirmed') != {'sources': signature['sources'], 'mode': state.get('mode', 'online')}:
                raise WorkflowError('Confirme las fuentes y el modo antes de finalizar la descarga.')
            if receipt.get('status') not in ('completed', 'paused', 'failed', 'closed_partial') or receipt.get('signature') != self._acquisition_signature(signature):
                raise WorkflowError('La adquisición no corresponde a los datos vigentes; confirme las fuentes y reanude la descarga antes de finalizarla.')
            if not (receipt.get('status') == 'closed_partial' and self._partial_current(state, signature, coverage)):
                stamp = now()
                limitations = {key: deepcopy(coverage[key]) for key in (
                    'pending_downloads', 'queued', 'deferred', 'running', 'active_running', 'expired_running',
                    'done', 'blocked', 'dead', 'failed', 'cancelled', 'unfetched_articles', 'saturated_windows',
                    'insufficient_documents', 'next_retry_at', 'next_retry_seconds')}
                limitations.update(articles=counts['articles'], documents=counts['documents'], captured_at=stamp,
                    decision='Cierre explícito con cobertura disponible; la cola se conserva para una reanudación posterior.')
                limitations['search_plan'] = self._plan_summary(receipt.get('details'))
                state['partial_signature'] = self._acquisition_signature(signature)
                state['partial_acceptance'] = {'signature': self._acquisition_signature(signature),
                    'queue_signature': coverage['queue_signature'], 'at': stamp, 'limitations': limitations}
                state['download'] = dict(receipt, status='closed_partial', closed_at=stamp)
                state.pop('analysis', None)
                self._save(con, state)
                self.store._audit(con, 'workflow_download_closed_partial', state['partial_acceptance'])
        return self.snapshot(operation)

    def finish_download(self, operation=None):
        """Close only the acquisition decision; do not delete or cancel jobs."""
        return self.accept_partial(operation)

    def mark_analysis(self, status='completed', details=None):
        if status not in ('running', 'scoring', 'completed'):
            raise WorkflowError('Estado de análisis inválido.', 400)
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            counts, signature, coverage, _ = self._read(con)
            if coverage['active_running']:
                raise WorkflowError('Hay descargas en ejecución; el análisis no puede darse por terminado.')
            if (state.get('mode') != 'local' and coverage['pending_downloads'] and
                    not self._partial_current(state, signature, coverage)):
                raise WorkflowError('Finalice explícitamente la descarga con cobertura parcial antes de analizar sus datos.')
            relevant = {key: signature[key] for key in ('bank', 'corpus', 'sources', 'review')}
            if status == 'running':
                if not counts['entities']:
                    raise WorkflowError('Importe primero un banco de entidades.')
                state['analysis_in_progress'] = relevant
                state.pop('analysis', None)
                self._save(con, state)
                return True
            if status == 'scoring':
                baseline = state.get('analysis_in_progress')
                # Attribution may materialize source metadata. Accept its
                # resulting corpus only after preserving the identity bank,
                # permissions and human reviews that authorized the operation.
                if baseline is None or any(baseline.get(key) != relevant[key] for key in ('bank', 'corpus', 'sources', 'review')):
                    raise WorkflowError('Los datos cambiaron durante la atribución; vuelva a analizar el banco vigente.')
                state['analysis_in_progress'] = relevant
                self._save(con, state)
                return True
            if state.get('analysis_in_progress') is not None and state['analysis_in_progress'] != relevant:
                raise WorkflowError('Los datos cambiaron durante el análisis; vuelva a analizar el banco vigente.')
            if counts['scores'] != counts['entities'] or not counts['entities']:
                raise WorkflowError('El análisis no cubre el banco vigente completo.')
            state['analysis'] = {'signature': {**signature, 'mode': state.get('mode', 'online')}, 'at': now(), 'details': deepcopy(details)}
            state.pop('analysis_in_progress', None)
            self._save(con, state)
        return True
