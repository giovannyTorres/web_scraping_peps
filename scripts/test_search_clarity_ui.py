"""Offline browser regression for current-list search status and recovery.

Uses the real local HTTP application with explicitly injected presentation states.
The 652 names, archived 235 references, 527 pages and counters are synthetic;
this validates rendering and request ordering, never search yield or throughput.
External browser requests and source transport are forbidden.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    from peps.app import create_server
    from peps.engine import Engine

    output = ROOT / 'runtime/search-clarity-ui-validation'
    output.mkdir(parents=True, exist_ok=True)
    report = {'kind': 'offline_injected_search_presentation', 'external_network': False,
              'recorded_at': datetime.now(timezone.utc).isoformat(), 'checks': {}, 'screenshots': []}
    mode = 'waiting'
    config = {'strategy': 'shared', 'preset': 'identity', 'start': '2020-01-01',
              'end': '2026-10-16', 'execution_mode': 'extended', 'session_seconds': 3600,
              'window': 'auto', 'max_queries': 1000, 'include_aliases': True, 'use_context': True}
    mutations, errors, external, task_reads = [], [], [], []
    base_stats = None

    def checked(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    def plan():
        return {'query_count': 652, 'entities_total': 652, 'entities_planned': 652,
                'execution': {'mode': 'extended', 'session_seconds': 3600},
                'shared_discovery': {'sources': 3 if config['strategy'] == 'shared' else 0}, 'sources': []}

    def snapshot(base):
        value = deepcopy(base)
        active = mode in ('waiting', 'running', 'pausing', 'analysis', 'analysis_scoring', 'nominal_preparing', 'nominal_running')
        analyzing = mode in ('analysis', 'analysis_scoring')
        body_mode = mode in ('running', 'pausing', 'paused_text', 'analysis', 'analysis_scoring')
        candidates, bodies = (5, 3) if body_mode else (0, 0)
        activity = {'state': 'running' if analyzing or mode == 'nominal_running' else 'preparing' if mode == 'nominal_preparing' else 'failed' if mode == 'nominal_failed' else 'paused' if mode in ('historical', 'paused_text') else mode, 'label': 'Analizando información' if analyzing else mode,
                    'detail': 'Solicitud del artículo pertinente' if body_mode else 'Esperando el siguiente intento permitido.',
                    'owner_active': active, 'active_requests': 2 if mode == 'running' else 0,
                    'can_pause': active, 'source_id': 'fixture', 'source_label': 'Fuente de prueba',
                    'last_error': 'HTTP 403 · fuente bloqueada; revisión necesaria' if mode == 'nominal_failed' else 'HTTP 429 · pausa solicitada' if mode == 'waiting' else None,
                    'next_retry_at': time.time() + 120 if mode == 'waiting' else None,
                    'next_retry_seconds': 120 if mode == 'waiting' else None,
                    'session_tasks': {'scope': 'current_session', 'completed': 17, 'pending': 7},
                    'current_bank_tasks': {'scope': 'current_bank', 'done': 133, 'running': 2 if mode == 'running' else 0,
                        'queued': 1, 'deferred': 6, 'failed': 6, 'pending': 7, 'ready': 1},
                    'historical_tasks': {'scope': 'all_saved', 'done': 719, 'pending': 7},
                    'material': {'scope': 'current_bank', 'body_articles': bodies, 'local_documents': 0,
                        'analyzable_documents': bodies, 'relevant_articles': candidates, 'archived_articles': 235,
                        'archived_documents': 235}}
        if mode in ('nominal_preparing', 'nominal_running'):
            activity['session_tasks'].update(completed=0)
            activity['current_bank_tasks'].update(done=0, failed=0, blocked=0, dead=0)
        if mode == 'nominal_failed':
            activity['current_bank_tasks'].update(queued=0, deferred=0, failed=1, blocked=1, pending=0, ready=0)
        if mode == 'historical':
            activity['current_bank_tasks'].update(done=0, running=0, queued=0, deferred=0, failed=0, pending=0, ready=0)
        value.update(entities=652, documents=235, evidence=0, jobs={'done': 719, 'queued': 1, 'deferred': 6, 'running': 0})
        value['operation'] = {'busy': active, 'operation_id': 'presentation-operation', 'kind': 'pipeline' if analyzing else 'download',
            'phase': 'scoring' if mode == 'analysis_scoring' else 'attribution' if analyzing else 'download', 'status': 'cancel_requested' if mode == 'pausing' else 'running' if active else 'paused',
            'label': 'Tarea aplazada; 133 completadas y 7 pendientes.', 'elapsed_seconds': 296,
            'completed': 133, 'total': None, 'percent': None, 'unit': 'jobs', 'activity': activity,
            'task_progress': {'active': [], 'phases': [], 'queue': {'pending': 7, 'eta_kind': 'unknown'}}}
        workflow = value['workflow']
        workflow.update(mode='online', sources_confirmed=True, recommended_step='download', analysis_current=False,
            counts={'entities': 652, 'documents': 235, 'articles': 235, 'pending_downloads': 7, 'sources': 3},
            activity=activity, coverage={'worker_active': active, 'active_running': 0, 'pending_downloads': 7,
                'done': 719, 'queued': 1, 'deferred': 6, 'failed': 6, 'status': 'running' if active else 'paused',
                'partial': True, 'stop_reason': 'backoff' if mode == 'waiting' else 'cancelled',
                'resume_available': True, 'activity': activity})
        for step in workflow['steps']:
            step.update(status='complete' if step['id'] in ('data', 'sources') else 'running' if step['id'] == 'download' and active else 'ready' if step['id'] == 'download' else 'blocked', reason='Primero termina la búsqueda.')
            if analyzing and step['id'] == 'analysis': step['status'] = 'running'
        workflow['actions'] = {name: {'enabled': not active if name in ('download', 'configure_sources') else False, 'reason': 'Completa el paso anterior.'}
            for name in ('download', 'configure_sources', 'analyze', 'accept_partial', 'analyze_available', 'view_results', 'export', 'import')}
        workflow['actions']['pause_and_analyze'] = {'enabled': body_mode and active and mode != 'pausing', 'reason': 'Esperará a que termine la petición activa.'}
        workflow['actions']['analyze_available'] = {'enabled': body_mode and not active, 'reason': 'Sólo textos de esta lista.'}
        value['acquisition'] = {'entities_total': 652, 'entities_queried': 18 if body_mode else 0,
            'entities_with_candidates': 4 if body_mode else 0, 'entities_with_bodies': 2 if body_mode else 0,
            'relevant_articles': candidates, 'body_articles': bodies, 'metadata_only_articles': candidates - bodies,
            'current_bank_index_ready': True, 'shared_pages_read': 527, 'shared_records_checked': 52063,
            'entities_checked_locally': 652, 'shared_pages_pending': 7, 'excluded_records': 52063,
            'session_matches_bank': True, 'permissions': {'prepared_sources': 3, 'body_authorized_sources': 3},
            'session': {'started_at': '2026-10-16T20:42:30+00:00', 'network_requests': 135,
                        'http_cache_hits': 0, 'bodies_new': bodies}}
        return value

    try:
        with tempfile.TemporaryDirectory(prefix='webscope-clarity-ui-') as tmp, patch('peps.collector.SafeFetcher', side_effect=AssertionError('External HTTP forbidden')):
            engine = Engine(tmp)
            server, _ = create_server(engine)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            address = f'http://127.0.0.1:{server.server_port}'
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=args.browser_path, headless=True, args=['--no-sandbox'])
                    context = browser.new_context(viewport={'width': 1365, 'height': 1000})
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))

                    def route(request):
                        nonlocal mode, config, base_stats
                        url = request.request.url
                        if not url.startswith(address + '/'):
                            external.append(url); request.abort(); return
                        path = url.split('/api/', 1)[-1]
                        method = request.request.method
                        if '/api/' not in url:
                            request.continue_(); return
                        if method == 'POST':
                            payload = request.request.post_data_json
                            mutations.append({'path': path, 'payload': payload})
                            if path == 'stop': mode = 'paused'
                            elif path == 'news-search': config = payload
                            elif path == 'workflow-run': mode = 'running'
                            elif path == 'workflow-pause-and-analyze-available': mode = 'pausing'
                            else: raise AssertionError('Unexpected mutation: ' + path)
                            body = {'config': config, 'plan': plan()} if path == 'news-search' else {'operation_id': 'presentation-operation'}
                        elif path == 'stats':
                            if base_stats is None: base_stats = request.fetch().json()
                            body = snapshot(base_stats)
                        elif path == 'news-search': body = {'config': config, 'plan': plan()}
                        elif path.startswith('news?'): body = {'stats': {}, 'articles': [], 'sources': [], 'plan': {}}
                        elif path.startswith('news-tasks?'):
                            task_reads.append(path)
                            body = {'scope': 'current_bank', 'items': [], 'total': 0}
                        elif path.startswith('sources'): body = []
                        else: request.continue_(); return
                        request.fulfill(status=200, content_type='application/json', body=json.dumps(body))

                    context.route('**/*', route)
                    page.goto(address, wait_until='networkidle')
                    expect(page.locator('[data-panel=news]')).to_be_visible()
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Esperando a las fuentes')
                    expect(page.locator('#workflow-entities-queried')).to_have_text('0 de 652')
                    expect(page.locator('#workflow-entities-bodies')).to_have_text('0 de 652')
                    expect(page.locator('#workflow-live-wait')).to_contain_text('Fuente de prueba')
                    expect(page.locator('#workflow-live-wait')).to_contain_text('HTTP 429')
                    checked('waiting_has_one_truthful_status_and_exact_source_retry', 'Próximo intento permitido:' in page.locator('#workflow-live-wait').inner_text())
                    checked('index_comparisons_never_count_as_web_queries', page.locator('#workflow-entity-progress').get_attribute('value') == '0')
                    checked('archived_235_and_global_719_are_not_visible_as_current', not page.get_by_text('235', exact=True).is_visible() and not page.get_by_text('719', exact=True).is_visible())
                    checked('waiting_does_not_display_download_active_or_empty_active_task_contradiction', 'Descarga activa' not in page.locator('[data-panel=news]').inner_text() and not page.locator('#workflow-operation').is_visible())
                    checked('zero_texts_do_not_offer_analysis', not page.locator('#workflow-accept-partial').is_visible() and not page.locator('#workflow-pause-analyze').is_visible())
                    expect(page.locator('#workflow-targeted-recovery')).to_be_enabled()
                    page.screenshot(path=str(output / 'waiting-desktop.png'), full_page=True)
                    report['screenshots'].append(str(output / 'waiting-desktop.png'))
                    page.set_viewport_size({'width': 390, 'height': 844})
                    checked('mobile_has_no_horizontal_overflow', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    page.screenshot(path=str(output / 'waiting-mobile.png'), full_page=True)
                    report['screenshots'].append(str(output / 'waiting-mobile.png'))
                    config['strategy'] = 'targeted'
                    for startup_mode, startup_label in [('nominal_preparing', 'Preparando las consultas'), ('nominal_running', 'Buscando información')]:
                        mode = startup_mode
                        page.reload(wait_until='networkidle')
                        expect(page.locator('#workflow-download-card-title')).to_have_text(startup_label)
                        expect(page.locator('#workflow-zero-results')).to_be_hidden()
                        expect(page.locator('#workflow-targeted-recovery')).to_be_hidden()
                        expect(page.locator('#workflow-entity-progress')).to_have_attribute('value', '0')
                        checked(startup_mode + '_shows_progress_without_premature_empty_warning_or_abort_recovery')
                    mode = 'nominal_failed'
                    page.reload(wait_until='networkidle')
                    expect(page.locator('#workflow-download-card-title')).to_have_text('La búsqueda necesita atención')
                    expect(page.locator('#workflow-live-wait')).to_be_visible()
                    expect(page.locator('#workflow-live-wait')).to_contain_text('HTTP 403')
                    expect(page.locator('#workflow-live-wait')).not_to_contain_text('Próximo intento')
                    expect(page.locator('#workflow-targeted-recovery')).to_be_hidden()
                    expect(page.locator('#workflow-news-settings')).to_be_enabled()
                    checked('failed_nominal_search_exposes_source_error_and_settings_without_identical_recovery')
                    checked('startup_and_failed_presentations_never_start_or_cancel_work', not mutations)
                    config['strategy'] = 'shared'
                    mode = 'historical'
                    page.reload(wait_until='networkidle')
                    page.evaluate('refresh(true)')
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Búsqueda en pausa')
                    page.locator('#workflow-inspection > summary').click()
                    expect(page.locator('#workflow-tasks-failed')).to_have_text('0')
                    checked('prior_bank_errors_do_not_leak_into_current_list_notes', page.locator('#workflow-download-issues').inner_text() == '' and not page.locator('#workflow-download-issues').is_visible())
                    page.locator('#workflow-inspection > summary').click()
                    mode = 'waiting'
                    page.evaluate('refresh(true)')
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Esperando a las fuentes')
                    page.locator('#workflow-targeted-recovery').click()
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Buscando información')
                    checked('directed_recovery_waits_for_pause_before_configure_and_restart', [m['path'] for m in mutations] == ['stop', 'news-search', 'workflow-run'])
                    checked('recovery_preserves_dates_permissions_and_existing_work', mutations[1]['payload']['start'] == '2020-01-01' and mutations[1]['payload']['end'] == '2026-10-16' and mutations[1]['payload']['strategy'] == 'targeted' and mutations[2]['payload']['resume'] is False)
                    expect(page.locator('#workflow-pause-analyze')).to_be_visible()
                    expect(page.locator('#workflow-entities-bodies')).to_have_text('2 de 652')
                    checked('real_body_stage_enables_safe_pause_and_analyze_control')
                    page.locator('#workflow-pause-analyze').click()
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Confirmando la pausa')
                    checked('pause_and_analyze_requests_guarded_backend_transition', mutations[-1]['path'] == 'workflow-pause-and-analyze-available' and mutations[-1]['payload']['operation_id'] == 'presentation-operation')
                    mode = 'paused_text'
                    page.evaluate('refresh(true)')
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Búsqueda en pausa')
                    expect(page.locator('#workflow-accept-partial')).to_be_visible()
                    checked('acknowledged_pause_with_text_offers_partial_analysis')
                    page.locator('#workflow-inspection > summary').click()
                    expect(page.locator('#workflow-tasks-completed')).to_have_text('133')
                    expect(page.locator('#workflow-task-scope')).to_contain_text('Esta sesión completó 17 tareas')
                    checked('list_and_session_task_counts_have_explicit_distinct_scopes')
                    page.locator('#workflow-queue-details > summary').click()
                    expect(page.locator('#workflow-task-status')).to_contain_text('0 tareas mostradas')
                    checked('task_browser_requests_only_current_bank', bool(task_reads) and all('scope=current_bank' in path for path in task_reads))
                    mode = 'analysis'
                    page.evaluate('refresh(true)')
                    expect(page.locator('[data-panel=analysis]')).to_be_visible()
                    checked('pipeline_navigates_to_analysis_as_soon_as_attribution_begins')
                    page.locator('#workflow-mobile-step').select_option('news')
                    expect(page.locator('[data-panel=news]')).to_be_visible()
                    expect(page.locator('#workflow-download-card-title')).to_have_text('Descarga finalizada · análisis en curso')
                    mode = 'analysis_scoring'
                    page.evaluate('refresh(true)')
                    expect(page.locator('[data-panel=news]')).to_be_visible()
                    checked('manual_navigation_after_phase_transition_is_preserved_without_false_search_label')
                    checked('no_external_requests_or_browser_errors', not errors and not external)
                    report['browser_version'] = browser.version
                    browser.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['passed'] = not report.get('error') and bool(report['checks']) and all(report['checks'].values())
        (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'output': str(output / 'validation.json')}))


if __name__ == '__main__':
    main()
