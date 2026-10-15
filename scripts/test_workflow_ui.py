"""Synthetic, offline browser validation of the five-step guided workflow.

Developer dependency: playwright==1.62.0 and a separately installed Chromium.
Run: python scripts/test_workflow_ui.py [--browser-path /path/to/chromium]
Screenshots and the measured report go to ignored runtime/guided-ui-validation.
The controlled download fixture never makes HTTP requests or uses real people.
Use --plan-only to inspect scenarios without starting a browser or server.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SCENARIOS = [
    'Five explicit steps, one current step and keyboard navigation.',
    'Import saves fictional entities and advances without automatically scoring.',
    'Sources and local/online scope require explicit user selection.',
    'Controlled download exposes truthful progress, elapsed time and unknown ETA.',
    'Concurrent mutation APIs reject imports, source changes, analysis and duplicate starts.',
    'Double click, a second tab and reload preserve the same server operation.',
    'Pause waits for the active request and resume processes remaining work.',
    'Analysis runs separately; results remain unavailable until explicit analysis.',
    'Mobile and desktop screenshots, no horizontal overflow, no external requests or browser errors.',
]


class ControlledOperations:
    """A deterministic acquisition boundary controlled by the browser test.

    Waiting represents an in-flight request. Setting stop_event only requests
    cancellation: the operation remains active until release is set, just as a
    bounded network call must finish before its worker can acknowledge a pause.
    """

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.calls = 0
        self.completed = 0
        self.queued = False
        self.queue_modes = []
        self.lock = threading.Lock()

    def acquire(self, acquirer, *args, progress=None, **kwargs):
        with self.lock:
            self.calls += 1
            self.queue_modes.append(kwargs.get('queue', True))
        if kwargs.get('reset_stop', True):
            acquirer.engine.stop_event.clear()
        if not self.queued:
            for number in range(2):
                acquirer.store.enqueue('news_fixture', {'fixture': number}, key='guided-fixture-' + str(number))
            self.queued = True
        self.entered.set()
        self.finished.clear()
        if progress:
            progress({'phase': 'download', 'event': 'start', 'completed': self.completed,
                      'total': None, 'unit': 'tareas', 'message': 'Solicitud sintética en curso',
                      'pending': 2 - self.completed, 'deferred': 0, 'blocked': 0,
                      'dead': 0, 'queue_complete': False})
        if not self.release.wait(20):
            raise TimeoutError('The browser did not release the controlled download fixture')
        cancelled = acquirer.engine.stop_event.is_set()
        if not cancelled:
            while job := acquirer.store.claim(kind_prefix='news_'):
                acquirer.store.finish(job)
                self.completed += 1
            entity = next(acquirer.store.iter_entities())
            acquirer.engine.import_documents([{
                'url': 'https://fixture.invalid/documento-sintetico',
                'title': 'Boletín oficial enteramente sintético', 'source_kind': 'official',
                'entity_ids': [entity['id']],
                'text': entity['name'] + ' fue condenada por fraude. Texto ficticio para validar la interfaz.',
                'published_at': datetime.now(timezone.utc).date().isoformat(),
            }])
        if progress:
            progress({'phase': 'download', 'event': 'finish', 'completed': self.completed,
                      'total': None, 'unit': 'tareas', 'message': 'Pausa sintética completada' if cancelled else 'Descarga sintética completada',
                      'pending': 2 - self.completed, 'deferred': 0, 'blocked': 0,
                      'dead': 0, 'queue_complete': not cancelled, 'cancelled': cancelled})
        self.finished.set()
        return {'status': 'paused' if cancelled else 'ok', 'processed': 0 if cancelled else 2,
                'completed': self.completed, 'cancelled': cancelled,
                'queue_complete': not cancelled, 'bank': acquirer.stats()}

    def prepare_resume(self):
        self.entered.clear()
        self.release.clear()
        self.finished.clear()


def wait_until(page, predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(25)
    raise AssertionError(description)


def api(page, path, payload=None):
    """Call the real server API, retaining status so server locks are inspectable."""
    return page.evaluate("""async ({path,payload})=>{
      const options={headers:{'X-PEPS-Token':document.querySelector('meta[name="peps-token"]').content}};
      if(payload!==null){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(payload);}
      const response=await fetch('/api/'+path,options);
      return {status:response.status,body:await response.json()};
    }""", {'path': path, 'payload': payload})


@contextmanager
def capture_failure(page_provider, artifacts, report):
    """Capture while Chromium is still alive, before Playwright unwinds."""
    try:
        yield
    except Exception:
        page = page_provider()
        if page is not None and not page.is_closed():
            try:
                page.screenshot(path=str(artifacts / 'failure.png'), full_page=True, timeout=3000)
                report['screenshots'].append('runtime/guided-ui-validation/failure.png')
            except Exception as screenshot_error:
                report['screenshot_error'] = str(screenshot_error)
        raise


def validate_branding(page, address, font_responses, report, checked):
    """Exercise the rendered identity and actual local font loading in Chromium."""
    from playwright.sync_api import expect

    expect(page).to_have_title(re.compile(r'WebScope'))
    expect(page.locator('.brand')).to_contain_text('WebScope')
    checked('webscope_title_and_visible_wordmark')
    fonts = page.evaluate("""async()=>{
      const normalize=value=>String(value).replace(/[\\s"']/g,'').toLowerCase();
      const faces=Array.from(document.fonts);
      const families={lato:faces.find(face=>normalize(face.family)==='lato')?.family,
        serif:faces.find(face=>normalize(face.family)==='sourceserif4')?.family};
      const requested=[];
      for(const [key,weight]of [['lato',400],['lato',700],['serif',700]]){
        if(!families[key]){requested.push({family:key,weight,loaded:0,checked:false});continue;}
        const query=weight+' 18px '+JSON.stringify(families[key]);
        const loaded=await document.fonts.load(query,'WebScope, evidencia pública de México. Águila sintética.');
        requested.push({family:families[key],weight,loaded:loaded.length,checked:document.fonts.check(query)});
      }
      await document.fonts.ready;
      const style=selector=>{const value=getComputedStyle(document.querySelector(selector));
        return {fontFamily:value.fontFamily,fontWeight:value.fontWeight,color:value.color,
          backgroundColor:value.backgroundColor,borderLeftColor:value.borderLeftColor};};
      return {requested,faces:Array.from(document.fonts).map(face=>({family:face.family,weight:face.weight,status:face.status})),
        root:style(':root'),body:style('body'),heading:style('h1'),sidebar:style('.app-sidebar'),
        wordmarkAccent:style('.brand span'),navigation:style('[data-workflow-step="data"]'),
        primary:style('#single-save')};
    }""")
    checked('local_lato_regular_and_bold_and_serif_are_loaded',
            len(fonts['requested']) == 3 and all(item['loaded'] > 0 and item['checked'] for item in fonts['requested']))
    normalize_family = lambda family: family.split(',')[0].strip(' \"\'').replace(' ', '').lower()
    checked('rendered_body_uses_lato', normalize_family(fonts['body']['fontFamily']) == 'lato')
    checked('rendered_heading_uses_source_serif_4', normalize_family(fonts['heading']['fontFamily']) == 'sourceserif4')
    checked('heading_font_is_a_loaded_variable_face', any(
            normalize_family(face['family']) == 'sourceserif4' and face['status'] == 'loaded' and
            len(face['weight'].split()) == 2 for face in fonts['faces']))
    checked('font_files_are_served_locally_with_woff2_mime', len({item['url'] for item in font_responses}) >= 3 and all(
            item['url'].startswith(address + '/') and item['status'] == 200 and
            item['content_type'].split(';')[0].strip().lower() == 'font/woff2' for item in font_responses))
    checked('canonical_palette_is_rendered_in_root_sidebar_navigation_and_wordmark',
            fonts['root']['color'] == 'rgb(7, 33, 70)' and fonts['sidebar']['backgroundColor'] == 'rgb(7, 33, 70)' and
            fonts['navigation']['backgroundColor'] == 'rgb(0, 68, 129)' and
            fonts['navigation']['borderLeftColor'] == 'rgb(20, 100, 165)' and
            fonts['primary']['backgroundColor'] == 'rgb(0, 68, 129)' and
            fonts['wordmarkAccent']['color'] == 'rgb(45, 204, 205)')
    page.locator('#single-save').hover()
    expect(page.locator('#single-save')).to_have_css('background-color', 'rgb(20, 100, 165)')
    checked('primary_hover_uses_canonical_light_blue')
    page.mouse.move(0, 0)
    expect(page.locator('#single-save')).to_have_css('background-color', 'rgb(0, 68, 129)')
    report['branding'] = fonts
    report['branding']['font_responses'] = list(font_responses)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()
    if args.plan_only:
        print(json.dumps({'kind': 'guided_workflow_browser_scenarios', 'scenarios': SCENARIOS}, ensure_ascii=False, indent=2))
        return
    run_validation(args)


def run_validation(args):
    from playwright.sync_api import expect, sync_playwright
    from peps.app import create_server
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.workflow import OperationTracker

    artifacts = ROOT / 'runtime' / 'guided-ui-validation'
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {'kind': 'synthetic_offline_guided_workflow_browser_validation',
              'processed_at': datetime.now(timezone.utc).isoformat(),
              'playwright_version': importlib.metadata.version('playwright'),
              'fixtures': 'Una empresa ficticia, una fuente .invalid, dos tareas locales y una fase controlada de diez fragmentos sintéticos; sin HTTP externo.',
              'scenarios': SCENARIOS, 'checks': {}, 'timings_ms': {}, 'screenshots': []}

    def checked(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    def screenshot(page, name):
        if name in ('desktop-empty-workflow', 'mobile-workflow'):
            page.evaluate("""()=>{window.scrollTo({top:0,left:0,behavior:'instant'});
              for(const element of document.querySelectorAll('main,.app-sidebar'))
                element.scrollTo({top:0,left:0,behavior:'instant'});}""")
        page.screenshot(path=str(artifacts / (name + '.png')), full_page=False)
        report['screenshots'].append('runtime/guided-ui-validation/' + name + '.png')

    controlled = ControlledOperations()
    analysis_entered, analysis_release, analysis_eta_ready = threading.Event(), threading.Event(), threading.Event()
    measured_clock = [0.0]
    synthetic_fragments = tuple('Fragmento enteramente sintético número ' + str(index) for index in range(10))
    original_attribute = NewsAcquirer.attribute

    def synthetic_analysis(acquirer, *args, progress=None, **kwargs):
        analysis_entered.set()
        if progress:
            progress({'phase': 'attribution', 'event': 'start', 'completed': 0,
                      'total': len(synthetic_fragments), 'unit': 'fragmentos sintéticos', 'message': 'Atribución sintética en curso'})
            # Advance the injected clock only for explicitly completed fixture
            # units. ETA assertions never depend on the runner's wall-clock speed.
            for completed in range(1, 4):
                measured_clock[0] += 1.5
                progress({'phase': 'attribution', 'event': 'progress', 'completed': completed,
                          'total': len(synthetic_fragments), 'unit': 'fragmentos sintéticos',
                          'message': 'Tres fragmentos sintéticos revisados' if completed == 3 else 'Fragmento sintético revisado'})
            analysis_eta_ready.set()
        if not analysis_release.wait(20):
            raise TimeoutError('The browser did not release the controlled analysis fixture')
        if progress:
            progress({'phase': 'attribution', 'event': 'finish', 'completed': len(synthetic_fragments),
                      'total': len(synthetic_fragments), 'unit': 'fragmentos sintéticos',
                      'message': 'Diez fragmentos sintéticos revisados'})
        return original_attribute(acquirer, *args, progress=None, **kwargs)

    errors, network, responses, font_responses = [], [], [], []
    active_page = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(tmp)
            engine.config_path.write_text(json.dumps({'sources': [{
                'id': 'synthetic_source', 'name': 'Fuente técnica sintética', 'kind': 'rss',
                'url': 'https://fixture.invalid/feed', 'enabled': False, 'terms_approved': False}]}), encoding='utf-8')
            news = NewsAcquirer(engine)
            news.config_path.write_text(json.dumps({'sources': [{
                'id': 'synthetic_rss', 'source_name': 'Fuente de noticias sintética', 'kind': 'rss',
                'url': 'https://fixture.invalid/feed.xml', 'base_url': 'https://fixture.invalid',
                'allowed_hosts': ['fixture.invalid'], 'enabled': False, 'terms_approved': False,
                'download_articles': False, 'article_terms_approved': False,
                'review_note': 'Fixture local sin solicitudes externas.', 'min_interval_seconds': 1}]}), encoding='utf-8')
            with patch.object(NewsAcquirer, 'acquire', lambda acquirer, *a, **kw: controlled.acquire(acquirer, *a, **kw)), \
                    patch.object(NewsAcquirer, 'attribute', synthetic_analysis), \
                    patch('peps.app.OperationTracker', lambda **kwargs: OperationTracker(
                        **dict(kwargs, clock=lambda: measured_clock[0]))):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                try:
                    with sync_playwright() as playwright, capture_failure(lambda: active_page, artifacts, report):
                        browser = playwright.chromium.launch(executable_path=args.browser_path, headless=True, args=['--no-sandbox'])
                        report['browser_version'] = browser.version
                        context = browser.new_context(viewport={'width': 1512, 'height': 1100}, device_scale_factor=1)
                        page = context.new_page()
                        active_page = page
                        context.on('page', lambda item: item.on('pageerror', lambda error: errors.append(str(error))))
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        context.on('request', lambda request: network.append(request.url))
                        def observe_response(response):
                            responses.append((response.url, response.status))
                            if response.url.split('?', 1)[0].endswith('.woff2'):
                                font_responses.append({'url': response.url, 'status': response.status,
                                                       'content_type': response.headers.get('content-type', '')})
                        context.on('response', observe_response)
                        # Abort any accidental attempt before it can reach the network.
                        context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(address + '/') else route.abort())
                        page.goto(address)
                        page.locator('#workflow-progress').wait_for(state='attached')
                        validate_branding(page, address, font_responses, report, checked)
                        initial = api(page, 'workflow')['body']
                        checked('five_explicit_steps', [step['id'] for step in initial['steps']] == ['data', 'sources', 'download', 'analysis', 'results'])
                        checked('empty_bank_starts_with_data', initial['recommended_step'] == 'data' and not initial['actions']['analyze']['enabled'])
                        checked('results_and_export_blocked_before_analysis', not initial['actions']['view_results']['enabled'] and not initial['actions']['export']['enabled'])
                        wait_until(page, lambda: page.locator('[data-workflow-step][aria-current="step"]').count() == 1,
                                   'The process must expose exactly one current step')
                        checked('one_accessible_current_step')
                        checked('five_accessible_step_bars_and_global_progress',
                                page.locator('[data-workflow-step] progress.step-progress').count() == 5 and
                                page.locator('#workflow-progress').count() == 1)
                        checked('pending_step_bars_start_at_zero', all(value == '0' for value in
                                page.locator('[data-workflow-step] progress.step-progress').evaluate_all('elements=>elements.map(element=>element.getAttribute("value"))')))
                        page.locator('[data-workflow-step="data"]').focus()
                        page.keyboard.press('Enter')
                        checked('step_navigation_works_with_keyboard', page.locator('[data-panel="import"]').is_visible())
                        screenshot(page, 'desktop-empty-workflow')

                        page.locator('[data-panel="import"]').wait_for()
                        page.locator('#single-name').fill('Empresa Ficticia Aurora de Prueba')
                        page.locator('#single-kind').select_option('company')
                        started = time.perf_counter()
                        page.locator('#single-save').click()
                        page.locator('[data-panel="sources"]').wait_for()
                        wait_until(page, lambda: engine.store.stats()['entities'] == 1, 'Imported entity was not committed')
                        report['timings_ms']['single_import_to_sources'] = (time.perf_counter() - started) * 1000
                        entity = next(engine.store.iter_entities())
                        checked('import_does_not_autoscore', engine.store.get_score(entity['id']) is None and engine.store.latest_score_run(entity['id']) is None)
                        checked('import_advances_to_sources', page.locator('[data-panel="sources"]').is_visible())
                        checked('explanation_api_does_not_score_implicitly', api(page, 'explanation?id=' + entity['id'])['status'] == 409 and engine.store.latest_score_run(entity['id']) is None)
                        checked('analysis_requires_explicit_mode_and_source_step', api(page, 'workflow-analyze', {})['status'] == 409)

                        page.locator('#workflow-mode-online').click()
                        wait_until(page, lambda: api(page, 'workflow')['body']['mode'] == 'online', 'Online mode was not persisted')
                        checked('download_rejects_missing_source_authorization', api(page, 'news-acquire', {'online': True})['status'] == 409)
                        allowed = api(page, 'news-source', {'id': 'synthetic_rss', 'enabled': True, 'terms_approved': True,
                                                          'download_articles': False, 'article_terms_approved': False})
                        checked('explicit_source_authorization_saved', allowed['status'] == 200)
                        page.reload()
                        page.locator('[data-panel="sources"]').wait_for()
                        page.locator('#workflow-search-strategy').select_option('feeds')
                        page.locator('#workflow-save-search').click()
                        wait_until(page, lambda: api(page, 'news-search')['body']['config']['strategy'] == 'feeds',
                                   'The fixture must explicitly select its generic RSS feed')
                        page.locator('#workflow-sources-next').click()
                        page.locator('[data-panel="news"]').wait_for()
                        checked('download_needs_explicit_network_consent', api(page, 'news-acquire', {'online': False})['status'] in (400, 409))
                        guarded_import = {'mode': 'single', 'entity': {'name': 'Empresa Ficticia Carga Bloqueada', 'kind': 'company'}}
                        guarded_preview = api(page, 'import-preview', guarded_import)
                        assert guarded_preview['status'] == 200 and guarded_preview['body']['ready']
                        guarded_confirm = dict(guarded_import, preview_hash=guarded_preview['body']['preview_hash'])
                        page.locator('#news-online').check()
                        before = len([url for url in network if url.endswith('/api/news-acquire')])
                        page.locator('#news-acquire').evaluate('element=>{element.click();element.click();}')
                        wait_until(page, controlled.entered.is_set, 'Controlled download was not entered')
                        busy = api(page, 'stats')['body']['operation']
                        checked('double_click_starts_one_download', controlled.calls == 1 and
                                len([url for url in network if url.endswith('/api/news-acquire')]) == before + 1)
                        operation_id = busy.get('operation_id') or busy.get('id')
                        checked('download_reports_honest_indeterminate_progress', busy['busy'] and busy['total'] is None and
                                busy['percent'] is None and busy['eta_seconds'] is None and busy['elapsed_seconds'] >= 0)
                        wait_until(page, lambda: page.locator('#workflow-operation').is_visible(), 'Download progress was not shown')
                        checked('indeterminate_progress_does_not_invent_percentage', page.locator('#workflow-progress').get_attribute('value') is None)
                        checked('running_download_step_has_indeterminate_bar', page.locator('#workflow-step-progress-download').get_attribute('value') is None)
                        screenshot(page, 'desktop-running-download')
                        mutations = {
                            'import-preview': guarded_import, 'import-confirm': guarded_confirm,
                            'import-entities': {'text': 'id,name,kind\nextra,Empresa Ficticia Extra,company'},
                            'import-documents': {'text': '{}'},
                            'source': {'id': 'synthetic_source', 'enabled': True, 'terms_approved': True},
                            'news-source': {'id': 'synthetic_rss', 'enabled': False, 'terms_approved': False},
                            'workflow-mode': {'mode': 'local'}, 'workflow-confirm-sources': {},
                            'workflow-analyze': {}, 'score': {}, 'news-attribute': {},
                            'news-acquire': {'online': True}, 'export-ticket': {}, 'retry': {},
                            'collect': {'online': True},
                            'review': {'id': 'fictional-note', 'expected_run_id': 'fictional-run',
                                       'status': 'confirmed', 'actor': 'Revisor Ficticio', 'reason': 'Prueba de bloqueo.'},
                            'replay': {'run_id': 'fictional-run'},
                        }
                        for endpoint, payload in mutations.items():
                            response = api(page, endpoint, payload)
                            checked('busy_server_rejects_' + endpoint.replace('-', '_'), response['status'] == 409)
                        checked('busy_explanation_request_cannot_change_scores', api(page, 'explanation?id=' + entity['id'])['status'] == 409 and engine.store.latest_score_run(entity['id']) is None)

                        second = context.new_page()
                        second.goto(address)
                        wait_until(second, lambda: api(second, 'stats')['body']['operation']['busy'], 'Second tab lost the running operation')
                        second_operation = api(second, 'stats')['body']['operation']
                        checked('second_tab_observes_same_operation', (second_operation.get('operation_id') or second_operation.get('id')) == operation_id)
                        second.close()
                        page.bring_to_front()
                        page.reload()
                        wait_until(page, lambda: api(page, 'stats')['body']['operation']['busy'], 'Reload lost operation state')
                        reloaded = api(page, 'stats')['body']['operation']
                        checked('reload_preserves_operation_without_restart', (reloaded.get('operation_id') or reloaded.get('id')) == operation_id and controlled.calls == 1)
                        checked('pause_request_accepted', api(page, 'stop', {})['status'] == 200)
                        waiting = api(page, 'stats')['body']['operation']
                        checked('pause_waits_for_inflight_request', waiting['busy'] and waiting['status'] == 'cancel_requested' and not controlled.finished.is_set())
                        controlled.release.set()
                        wait_until(page, lambda: not api(page, 'stats')['body']['operation']['busy'], 'Pause was not acknowledged after the request ended')
                        paused = api(page, 'stats')['body']['operation']
                        checked('paused_operation_does_not_claim_completion', paused['status'] == 'paused' and paused['percent'] != 100)
                        wait_until(page, lambda: page.locator('[data-workflow-step="download"]').get_attribute('data-status') == 'paused', 'Paused step was not rendered')
                        paused_bar = page.locator('#workflow-step-progress-download').get_attribute('value')
                        checked('paused_step_never_shows_complete_progress', paused_bar is None or float(paused_bar) <= 99)
                        pending_flow = api(page, 'workflow')['body']
                        checked('pending_tasks_need_resume_or_explicit_partial_close',
                                pending_flow['actions']['accept_partial']['enabled'] and
                                api(page, 'workflow-analyze', {})['status'] == 409)
                        screenshot(page, 'desktop-paused-download')

                        controlled.prepare_resume()
                        resumed = api(page, 'news-acquire', {'online': True, 'resume': True})
                        checked('resume_accepted', resumed['status'] in (200, 202))
                        wait_until(page, controlled.entered.is_set, 'Resume did not start the remaining tasks')
                        controlled.release.set()
                        wait_until(page, lambda: not api(page, 'stats')['body']['operation']['busy'], 'Resumed acquisition did not finish')
                        checked('resume_preserves_single_queue_and_completes_remaining_tasks', controlled.calls == 2 and controlled.completed == 2 and engine.store.stats()['jobs'].get('done') == 2)
                        checked('resume_does_not_enqueue_discovery_again', controlled.queue_modes == [True, False])
                        checked('completed_download_still_requires_analysis', not api(page, 'workflow')['body']['analysis_current'] and engine.store.latest_score_run(entity['id']) is None)
                        page.reload()
                        page.locator('[data-panel="news"]').wait_for()
                        page.locator('#news-acquire').click()
                        page.locator('[data-panel="analysis"]').wait_for()
                        forecast = api(page, 'stats')['body']['operation']['task_progress']['estimates']
                        scoring_forecast = next(item for item in forecast if item['id'] == 'scoring')
                        checked('first_analysis_forecast_has_no_invented_numeric_eta',
                                scoring_forecast['eta_kind'] == 'unknown' and scoring_forecast['eta_seconds'] is None)
                        page.locator('#workflow-analyze').focus()
                        page.keyboard.press('Enter')
                        wait_until(page, analysis_entered.is_set, 'Keyboard did not start explicit analysis')
                        analyzing = api(page, 'stats')['body']['operation']
                        checked('analysis_has_measured_units_and_bounded_percent', analyzing['busy'] and analyzing['completed'] in (1, 3) and analyzing['total'] == 10 and 0 < analyzing['percent'] < 100)
                        wait_until(page, lambda: 'de 10 fragmentos sintéticos' in page.locator('#workflow-operation-detail').inner_text(),
                                   'Analysis progress was not rendered')
                        checked('analysis_holds_server_mutation_lock', api(page, 'import-entities', mutations['import-entities'])['status'] == 409 and api(page, 'news-acquire', {'online': True})['status'] == 409)
                        screenshot(page, 'desktop-analysis-progress')
                        wait_until(page, analysis_eta_ready.is_set, 'The controlled phase did not emit its third completed unit')
                        estimated = api(page, 'stats')['body']['operation']
                        checked('eta_uses_completed_units_and_elapsed_sample', estimated['completed'] == 3 and estimated['total'] == 10 and
                                estimated['elapsed_seconds'] >= 3 and isinstance(estimated['eta_seconds'], (int, float)) and estimated['eta_seconds'] > 0)
                        phase = next(item for item in estimated['task_progress']['phases'] if item['id'] == 'attribution')
                        checked('phase_eta_is_measured_from_three_controlled_completed_units',
                                phase['eta_kind'] == 'measured' and phase['completed'] == 3 and
                                phase['elapsed_seconds'] == 4.5 and phase['eta_seconds'] > 0)
                        expect(page.locator('#workflow-operation-eta')).to_contain_text(re.compile(r'restante estimado:', re.I))
                        expect(page.locator('#workflow-phase-progress [data-phase="attribution"] .timing-eta')).to_have_attribute('data-kind', 'measured')
                        checked('numeric_eta_is_visible_after_sufficient_sample')
                        checked('analysis_step_bar_tracks_current_phase',
                                float(page.locator('#workflow-step-progress-analysis').get_attribute('value')) == 30 and
                                'fase actual' in page.locator('#workflow-step-progress-analysis').get_attribute('aria-valuetext'))
                        screenshot(page, 'desktop-analysis-eta')
                        analysis_release.set()
                        wait_until(page, lambda: not api(page, 'stats')['body']['operation']['busy'], 'Explicit analysis did not finish')
                        page.locator('[data-panel="bank"]').wait_for()
                        final = api(page, 'workflow')['body']
                        checked('explicit_analysis_enables_results_and_export', final['analysis_current'] and final['actions']['view_results']['enabled'] and final['actions']['export']['enabled'])
                        checked('completed_steps_show_complete_bars', all(
                                page.locator('#workflow-step-progress-' + step['id']).get_attribute('value') == '100'
                                for step in final['steps'] if step['status'] == 'complete'))
                        page.get_by_role('row', name='Consultar Empresa Ficticia Aurora de Prueba', exact=True).click()
                        page.get_by_role('heading', name='¿Por qué obtuvo este score?').wait_for()
                        checked('results_show_real_saved_score', page.locator('.score-number strong').inner_text() == str(engine.store.get_score(entity['id'])['score']) and engine.store.get_score(entity['id'])['score'] > 0)
                        screenshot(page, 'desktop-results')
                        page.set_viewport_size({'width': 390, 'height': 844})
                        checked('mobile_has_no_horizontal_page_overflow', page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
                        screenshot(page, 'mobile-results')
                        # New contexts and reloads must not force the completed flow back to import.
                        page.reload()
                        page.locator('[data-panel="bank"]').wait_for()
                        checked('completed_flow_survives_reload', api(page, 'workflow')['body']['analysis_current'])
                        screenshot(page, 'mobile-workflow')
                        page.locator('#workflow-mobile-step').select_option('sources')
                        page.locator('[data-panel="sources"]').wait_for()
                        checked('mobile_step_selector_is_operable', page.locator('#workflow-mode-local').is_visible())
                        page.locator('#workflow-mode-local').click()
                        wait_until(page, lambda: api(page, 'workflow')['body']['mode'] == 'local', 'Explicit local mode was not persisted')
                        local = api(page, 'workflow')['body']
                        checked('switching_scope_invalidates_previous_results', not local['analysis_current'] and not local['actions']['view_results']['enabled'])
                        checked('local_mode_blocks_download_api', not local['actions']['download']['enabled'] and api(page, 'news-acquire', {'online': True})['status'] == 409)
                        page.locator('#workflow-sources-next').click()
                        page.locator('[data-panel="news"]').wait_for()
                        page.locator('#news-acquire').click()
                        page.locator('[data-panel="analysis"]').wait_for()
                        page.locator('#workflow-analyze').click()
                        wait_until(page, lambda: api(page, 'workflow')['body']['analysis_current'], 'Local analysis did not publish current results')
                        page.locator('[data-panel="bank"]').wait_for()
                        checked('explicit_local_analysis_does_not_download', controlled.calls == 2 and api(page, 'workflow')['body']['coverage']['mode'] == 'local')
                        screenshot(page, 'mobile-local-results')
                        checked('no_external_requests', not [url for url in network if not url.startswith(address + '/')])
                        checked('no_browser_errors', not errors)
                        checked('audit_chain_preserved', engine.store.verify_audit()['ok'])
                        report['page_errors'] = errors
                        report['network_request_count'] = len(network)
                        report['expected_server_conflicts'] = sum(status == 409 for _, status in responses)
                        browser.close()
                finally:
                    controlled.release.set()
                    analysis_release.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['passed'] = 'error' not in report and bool(report['checks']) and all(report['checks'].values())
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        output = args.output or artifacts / 'validation.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding='utf-8')
        print(rendered)


if __name__ == '__main__':
    main()
