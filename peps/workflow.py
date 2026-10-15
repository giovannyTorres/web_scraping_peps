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

    def __init__(self, clock=None):
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self.cancel_event = threading.Event()
        self._started = self._phase_started = self._clock()
        self._phase_baseline = 0
        self._ended = None
        self._state = {'id': None, 'operation_id': None, 'kind': None, 'status': 'idle',
                       'phase': '', 'completed': 0, 'total': None, 'unit': '', 'message': '', 'result': None}

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
                           'phase': phase, 'completed': 0, 'total': total, 'unit': unit, 'message': message, 'result': None}
            return identifier

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
            return True

    def finish(self, status='completed', message='', result=None, operation_id=None):
        if status not in ('completed', 'paused', 'failed'):
            raise WorkflowError('Estado final de operación inválido.', 400)
        with self._lock:
            if not self.busy or (operation_id is not None and operation_id != self._state['operation_id']):
                return False
            self._state.update(status=status, message=message or self._state['message'], result=deepcopy(result))
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
            elapsed = max(0., (self._ended if self._ended is not None else self._clock()) - self._started)
            completed, total = value['completed'], value['total']
            percent = None if total is None or total == 0 else min(100, 100 * completed / total)
            if percent is not None and value['status'] != 'completed':
                percent = min(99., percent)
            phase_elapsed = max(0., self._clock() - self._phase_started)
            units = completed - self._phase_baseline
            eta = None
            if self.busy and total is not None and total > completed and units >= 3 and phase_elapsed >= 3:
                eta = max(1, math.ceil((total - completed) * phase_elapsed / units))
            value.update(busy=self.busy, percent=round(percent, 1) if percent is not None else None,
                         elapsed_seconds=round(elapsed, 3), eta_seconds=eta,
                         last_result=deepcopy(value['result']), label=value['message'],
                         error=value['message'] if value['status'] == 'failed' else None)
            return value


class GuidedWorkflow:
    STEPS = (('data', 'Importar banco'), ('sources', 'Configurar fuentes'),
             ('download', 'Descargar documentos'), ('analysis', 'Analizar evidencia'), ('results', 'Consultar resultados'))
    ACTIONS = ('import', 'configure_sources', 'download', 'analyze', 'view_results', 'export', 'review', 'accept_partial')

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
        statuses = dict(con.execute("SELECT status,count(*) FROM jobs WHERE kind GLOB 'news_*' GROUP BY status"))
        counts['pending_downloads'] = sum(statuses.get(status, 0) for status in ('queued', 'deferred', 'running'))
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
                     'sources': [self._last_event(con, 'news_source_configured', 'source_configured'), digest(canonical(sources))],
                     'review': self._last_event(con, 'evidence_review'),
                     'identity': self._last_event(con, 'news_identity_resolved', 'news_identity_retired')}
        coverage = {'pending_downloads': counts['pending_downloads'], 'queued': statuses.get('queued', 0),
                    'deferred': statuses.get('deferred', 0), 'running': statuses.get('running', 0),
                    'active_running': con.execute("SELECT count(*) FROM jobs WHERE kind GLOB 'news_*' AND status='running' AND lease_until>?", (time.time(),)).fetchone()[0],
                    'failed': statuses.get('dead', 0) + statuses.get('blocked', 0),
                    'unfetched_articles': unfetched, 'saturated_windows': saturated,
                    'insufficient_documents': counts['documents'] == 0}
        return counts, signature, coverage, source_error

    @staticmethod
    def _acquisition_signature(signature):
        return {key: signature[key] for key in ('bank', 'corpus', 'sources')}

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
        coverage['partial'] = bool(coverage['failed'] or coverage['unfetched_articles'] or coverage['saturated_windows'] or coverage['insufficient_documents'])
        coverage['accepted_partial'] = bool(state.get('partial_signature') == self._acquisition_signature(signature) and acquisition_current)
        coverage['status'] = receipt.get('status', 'not_started')
        coverage['mode'] = mode
        downloaded = bool(acquisition_current and not pending and
                          ((receipt.get('status') == 'completed' and not coverage['partial']) or coverage['accepted_partial']))
        local = mode == 'local'
        analysis_signature = {**signature, 'mode': mode}
        analysis_current = bool((state.get('analysis') or {}).get('signature') == analysis_signature and
                                counts['scores'] == counts['entities'] and counts['entities'] > 0 and not pending)
        if not local and not downloaded:
            analysis_current = False
        if not sources_confirmed:
            analysis_current = False
        active_reason = 'Hay una operación en curso; espere a que termine.'
        pending_reason = 'Hay descargas pendientes; reanude o termine la adquisición antes de analizar.'
        data_reason = 'Importe primero un banco de entidades.'
        sources_reason = source_error or ('Confirme las fuentes y el modo para continuar.' if local or counts['sources'] else 'Seleccione una fuente habilitada con permisos aprobados.')
        download_reason = 'Complete la descarga o acepte explícitamente su cobertura parcial al terminar.'
        analyze_reason = (active_reason if busy else pending_reason if pending else data_reason if not counts['entities']
                          else sources_reason if not sources_confirmed else '' if local else sources_reason if not counts['sources'] else '' if downloaded else download_reason)
        actions = {action: {'enabled': True, 'reason': ''} for action in self.ACTIONS}
        for action in ('import', 'configure_sources'):
            actions[action] = {'enabled': not busy and not coverage['active_running'], 'reason': active_reason if busy or coverage['active_running'] else ''}
        actions['download'] = {'enabled': not busy and bool(counts['entities']) and not local and bool(counts['sources']) and sources_confirmed and not coverage['active_running'],
                               'reason': active_reason if busy or coverage['active_running'] else data_reason if not counts['entities'] else
                                         'Seleccione el modo online para descargar.' if local else sources_reason if not counts['sources'] or not sources_confirmed else ''}
        actions['analyze'] = {'enabled': not bool(analyze_reason), 'reason': analyze_reason}
        partial_reason = (active_reason if busy else pending_reason if pending else
                          'La cobertura parcial se acepta en modo online.' if local else
                          '' if acquisition_current and receipt.get('status') in ('completed', 'paused', 'failed') else
                          'Termine una adquisición de los datos vigentes antes de aceptar cobertura parcial.')
        actions['accept_partial'] = {'enabled': not bool(partial_reason), 'reason': partial_reason}
        for action in ('view_results', 'export', 'review'):
            reason = active_reason if busy else pending_reason if pending else '' if analysis_current else 'Ejecute el análisis de los datos vigentes.'
            actions[action] = {'enabled': not bool(reason), 'reason': reason}
        steps = []
        for number, (step_id, label) in enumerate(self.STEPS, 1):
            if step_id == 'data':
                status, reason = ('complete', '') if counts['entities'] else ('ready', data_reason)
            elif step_id == 'sources':
                status, reason = ('complete', 'Modo local confirmado explícitamente.' if local else '') if sources_confirmed else ('ready' if counts['entities'] else 'blocked', sources_reason)
            elif step_id == 'download':
                status, reason = ('complete', 'Modo local: no se solicita adquisición de red.') if local else ('complete', '') if downloaded else ('paused' if receipt.get('status') == 'paused' else 'error' if receipt.get('status') == 'failed' else 'ready' if actions['download']['enabled'] else 'blocked', actions['download']['reason'] or download_reason)
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
                state.pop('partial_signature', None)
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

    def mark_download(self, status='completed', details=None):
        if status not in ('running', 'completed', 'paused', 'failed'):
            raise WorkflowError('Estado de adquisición inválido.', 400)
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            _, signature, coverage, _ = self._read(con)
            previous = state.get('download') or {}
            acquisition = self._acquisition_signature(signature)
            if status != 'running' and previous.get('status') == 'running':
                for key in ('bank', 'sources'):
                    acquisition[key] = previous['signature'][key]
            if status == 'completed' and coverage['pending_downloads']:
                status = 'paused'
            state['download'] = {'status': status, 'signature': acquisition, 'at': now(), 'details': deepcopy(details)}
            state.pop('analysis', None)
            state.pop('partial_signature', None)
            self._save(con, state)
        return status

    def accept_partial(self, operation=None):
        op = self._operation(operation)
        if op.get('busy') or op.get('status') in ('running', 'cancel_requested'):
            raise WorkflowError('Espere a que termine la operación para aceptar cobertura parcial.')
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            _, signature, coverage, _ = self._read(con)
            receipt = state.get('download') or {}
            if state.get('mode') == 'local':
                raise WorkflowError('La cobertura parcial se acepta en modo online.')
            if coverage['pending_downloads']:
                raise WorkflowError('Hay descargas pendientes; reanude o termine la adquisición antes de aceptar cobertura parcial.')
            if receipt.get('status') not in ('completed', 'paused', 'failed') or receipt.get('signature') != self._acquisition_signature(signature):
                raise WorkflowError('Termine una adquisición de los datos vigentes antes de aceptar cobertura parcial.')
            state['partial_signature'] = self._acquisition_signature(signature)
            state.pop('analysis', None)
            self._save(con, state)
        return self.snapshot(operation)

    def mark_analysis(self, status='completed', details=None):
        if status not in ('running', 'scoring', 'completed'):
            raise WorkflowError('Estado de análisis inválido.', 400)
        with self.store.connection() as con:
            con.execute('BEGIN IMMEDIATE')
            state = self._state(con)
            counts, signature, coverage, _ = self._read(con)
            if coverage['pending_downloads']:
                raise WorkflowError('Hay descargas pendientes; el análisis no puede darse por terminado.')
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
                if baseline is None or any(baseline.get(key) != relevant[key] for key in ('bank', 'sources', 'review')):
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
