"""Reproduce 235 preserved articles / 64 deferred tasks through real local HTTP.

Uses the real SQLite queue, acquisition scheduler, ingestion, workflow and
analysis. Only external HTTP transport is replaced with fictional responses.
Run with Playwright 1.62.0 and Chromium; reports stay under ignored runtime/.
"""
import argparse
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

from test_workflow_ui import api, wait_until


class OfflineTransport:
    """A transport boundary, never a replacement for acquire/claim/finish."""

    def __init__(self):
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.hold = False

    def fetch(self, url, source):
        from peps.store import digest, now
        self.calls.append(url)
        if self.hold:
            self.entered.set()
            if not self.release.wait(30):
                raise TimeoutError('Browser did not release the in-flight fixture')
            return {'status': 'deferred', 'error': 'Espera sintética del servidor',
                    'error_code': 'host_interval', 'retry_at': time.time() + 3600}
        number = int(url.rsplit('/', 1)[-1])
        body = ('<html><head><title>Boletín ficticio ' + str(number) + '</title></head><body><article>'
                '<h1>Boletín enteramente sintético número ' + str(number) + '</h1>'
                '<p>Documento inventado exclusivamente para probar recuperación de descargas. '
                'Este texto no describe personas reales ni constituye una noticia o conducta adversa. '
                'El identificador único de este cuerpo es ' + str(number) + '.</p></article></body></html>')
        return {'status': 'ok', 'url': url, 'final_url': url, 'body': body,
                'sha256': digest(body), 'retrieved_at': now()}

    def metrics_snapshot(self):
        return {'synthetic_calls': len(self.calls), 'external_http_calls': 0}

    def close(self):
        pass


def jobs_snapshot(store):
    with store.connection() as con:
        return [dict(row) for row in con.execute(
            'SELECT id,kind,payload,status,attempts,due,owner,lease_until FROM jobs ORDER BY id')]


def seed_case(engine, news, transport):
    from peps.store import digest, now
    entity = {'id': 'recovery-fixture', 'name': 'Entidad Sintética Aurora de Recuperación',
              'kind': 'company', 'aliases': ['Aurora Sintética de Prueba'],
              'context': {'city': 'Puebla', 'role': 'Organización enteramente ficticia'}}
    engine.store.import_entities([entity])
    source = {'id': 'fixture_rss', 'source_name': 'Archivo RSS ficticio', 'kind': 'rss',
              'document_kind': 'news', 'url': 'https://fixture.invalid/feed.xml',
              'base_url': 'https://fixture.invalid', 'allowed_hosts': ['fixture.invalid'],
              'article_allowed_hosts': ['fixture.invalid'], 'enabled': True, 'terms_approved': True,
              'download_articles': True, 'article_terms_approved': True, 'min_interval_seconds': 0,
              'minimum_article_chars': 80, 'terms_url': 'https://fixture.invalid/terms'}
    historical = {'id': 'fixture_gdelt', 'source_name': 'Búsqueda histórica ficticia', 'kind': 'gdelt',
                  'base_url': 'https://fixture.invalid/gdelt', 'allowed_hosts': ['fixture.invalid'],
                  'enabled': True, 'terms_approved': True, 'download_articles': False,
                  'article_terms_approved': False, 'max_records': 250, 'min_interval_seconds': 0}
    archive = {'id': 'fixture_archive', 'source_name': 'Archivo histórico ficticio', 'kind': 'rss',
               'search_kind': 'wordpress_rss', 'search_url': 'https://archive.fixture.invalid/',
               'base_url': 'https://archive.fixture.invalid/', 'allowed_hosts': ['archive.fixture.invalid'],
               'enabled': True, 'terms_approved': True, 'download_articles': False,
               'search_max_records': 10, 'min_window_seconds': 86400}
    news.config_path.write_text(json.dumps({'sources': [source, historical, archive]}), encoding='utf-8')
    rows = []
    with engine.store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        for number in range(235):
            url = 'https://fixture.invalid/article/' + str(number)
            news._observe({'url': url, 'title': (entity['name'] + ': nota ficticia ' if number == 171 else 'Noticia ficticia ') + str(number),
                           'text': 'Metadatos sintéticos sin atribución de conducta.',
                           'published_at': '2024-06-15'}, source, source['url'],
                          {'sha256': digest('feed-fixture'), 'retrieved_at': now()}, None, connection=con)
            rows.append((digest(url), url))
        engine.store._audit(con, 'news_discovery_completed', {'fixture': True, 'records': 235})
    due = time.time() + 3600
    with engine.store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        for number, (article_id, url) in enumerate(rows[171:], 171):
            job_id = engine.store.enqueue('news_article',
                {'article_id': article_id, 'source_id': source['id'], 'url': url},
                key='recovery-fixture-' + str(number), due=due, connection=con)
            con.execute("UPDATE jobs SET status='deferred',attempts=2,error='Espera sintética del servidor' WHERE id=?",
                        (job_id,))
            con.execute("UPDATE news_articles SET status='deferred',error='Espera sintética del servidor' WHERE id=?",
                        (article_id,))
        engine.store._audit(con, 'synthetic_recovery_fixture',
                            {'articles': 235, 'full_bodies': 0, 'deferred_tasks': 64, 'retry_at': due})
    return entity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--docs-screenshots', action='store_true',
                        help='Copy the two paused-state screenshots to docs/images for review')
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    from peps.app import create_server
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.news_search import build_search_plan
    from peps.workflow import GuidedWorkflow

    artifacts = ROOT / 'runtime/download-recovery-ui-validation'
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {'kind': 'synthetic_real_scheduler_download_recovery',
              'processed_at': datetime.now(timezone.utc).isoformat(), 'checks': {}, 'screenshots': [],
              'fixture': {'articles': 235, 'full_bodies': 0, 'metadata_only_articles': 235, 'deferred_tasks': 64,
                          'retry_seconds': 3600, 'real_scheduler': True, 'real_local_http': True,
                          'replaced_boundary': 'external HTTP transport only'}}

    def checked(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    def screenshot(page, name):
        page.screenshot(path=str(artifacts / (name + '.png')), full_page=False)
        report['screenshots'].append('runtime/download-recovery-ui-validation/' + name + '.png')
        if args.docs_screenshots and name in ('desktop-235-news-64-pending', 'mobile-235-news-64-pending'):
            target = ROOT / 'docs/images' / ('descarga-recuperable-' +
                ('escritorio' if name.startswith('desktop') else 'movil') + '.png')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifacts / (name + '.png'), target)

    def idle(page, kind='download'):
        def finished():
            operation = api(page, 'stats')['body']['operation']
            return operation['kind'] == kind and not operation['busy']
        wait_until(page, finished,
                   'The real local operation did not finish', timeout=25)
        return api(page, 'stats')['body']

    def reachable(page, locator):
        locator.scroll_into_view_if_needed()
        expect(locator).to_be_in_viewport()
        expect(locator).to_be_enabled()
        return locator.evaluate('element=>{const r=element.getBoundingClientRect();'
            'const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);'
            'return !!hit&&(hit===element||element.contains(hit));}')

    errors, requests, started_payloads = [], [], []
    transport = OfflineTransport()
    page = None
    try:
        with tempfile.TemporaryDirectory(prefix='webscope-recovery-') as tmp:
            engine = Engine(tmp)
            news = NewsAcquirer(engine)
            entity = seed_case(engine, news, transport)
            baseline = jobs_snapshot(engine.store)
            checked('fixture_has_235_metadata_articles_zero_bodies_64_deferred_tasks', news.stats()['articles'] == 235 and
                    engine.store.stats()['documents'] == 0 and engine.store.stats()['jobs'] == {'deferred': 64})
            transport.calls.clear()
            with patch('peps.collector.SafeFetcher', lambda *_, **__: transport):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = 'http://127.0.0.1:' + str(server.server_port)
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=args.browser_path,
                            headless=True, args=['--no-sandbox'])
                        context = browser.new_context(viewport={'width': 1366, 'height': 768})
                        page = context.new_page()
                        context.on('page', lambda item: item.on('pageerror', lambda error: errors.append(str(error))))
                        page.on('pageerror', lambda error: errors.append(str(error)))

                        def observe(request):
                            requests.append(request.url)
                            if request.url.endswith('/api/news-acquire') and request.method == 'POST':
                                started_payloads.append(request.post_data_json)

                        context.on('request', observe)
                        context.route('**/*', lambda route: route.continue_()
                                      if route.request.url.startswith(address + '/') else route.abort())
                        try:
                            page.goto(address)
                            page.locator('[data-panel="sources"]').wait_for()
                            page.locator('#workflow-search-strategy').select_option('targeted')
                            page.locator('#workflow-search-preset').select_option('identity')
                            page.locator('#workflow-history-start').fill('2016-01-01')
                            page.locator('#workflow-history-end').fill('2026-10-15')
                            for detail in page.locator('#workflow-history-window').locator('xpath=ancestor::details').all():
                                if detail.get_attribute('open') is None:
                                    detail.locator(':scope > summary').click()
                            page.locator('#workflow-history-window').select_option('year')
                            page.locator('#workflow-search-max-queries').fill('1000')
                            page.locator('#workflow-search-aliases').check()
                            page.locator('#workflow-search-context').check()
                            # The default start is already 2016-01-01 in 2026:
                            # polling that one field can accept the old config
                            # before this save POST has finished on slower CI.
                            with page.expect_response(lambda response: response.url == address + '/api/news-search' and
                                                      response.request.method == 'POST') as saved_scope:
                                page.locator('#workflow-save-search').click()
                            saved_response = saved_scope.value
                            saved_body = saved_response.json()
                            report['historical_search_save'] = {'status': saved_response.status,
                                'submitted': saved_response.request.post_data_json,
                                'returned_config': saved_body.get('config')}
                            assert saved_response.status == 200, report['historical_search_save']
                            expect(page.locator('#workflow-save-search')).to_be_enabled()
                            search = api(page, 'news-search')['body']
                            config, plan = search['config'], search['plan']
                            checked('targeted_historical_scope_is_saved_through_real_ui',
                                    config['strategy'] == 'targeted' and config['preset'] == 'identity' and
                                    config['start'] == '2016-01-01' and config['end'] == '2026-10-15' and
                                    config['include_aliases'] is True and config['use_context'] is True)
                            checked('historical_plan_is_entity_directed_and_does_not_claim_complete_archive',
                                    bool(plan['queries']) and all(query['entity_ids'] and query['start'] and query['end']
                                    for query in plan['queries']) and all(not source['complete_archive'] for source in plan['sources']))
                            # HTTP previews are deliberately bounded. Verify the actual
                            # complete planner against the configuration saved by the UI.
                            full_plan = build_search_plan(engine.store.iter_entities(), news.sources(), config)
                            checked('authorized_archive_plans_named_entity_searches_starting_in_2016', any(
                                query['source_id'] == 'fixture_archive' and query['start'].startswith('2016') and
                                'year=2016' in query['url'] for query in full_plan['queries']) and
                                plan['query_count'] == full_plan['query_count'])
                            report['oldest_historical_query'] = next(query for query in full_plan['queries']
                                if query['source_id'] == 'fixture_archive' and query['start'].startswith('2016'))
                            report['search_config'], report['search_plan'] = config, plan
                            screenshot(page, 'targeted-historical-coverage')
                            page.locator('#workflow-sources-next').click()
                            page.locator('[data-panel="news"]').wait_for()
                            # This persisted receipt represents the already-running scope
                            # in the reported session, rather than a different old query.
                            workflow = GuidedWorkflow(engine.store, sources_provider=news.sources)
                            workflow.mark_download(status='running', details={
                                'acquisition_context': {'search': news.search_config()}})
                            workflow.mark_download(status='paused', details={
                                'stop_reason': 'backoff', 'pending': 64, 'queued': None})
                            page.reload()
                            page.locator('[data-panel="news"]').wait_for()
                            scope = api(page, 'workflow')['body']['coverage']
                            checked('matching_persisted_scope_allows_resuming_the_existing_queue',
                                    scope['resume_available'] and not scope['needs_new_search'])
                            page.locator('#news-online').check()
                            page.locator('#news-acquire').click()
                            paused = idle(page)
                            checked('real_scheduler_reports_backoff_without_fetching_future_jobs',
                                    paused['operation']['status'] == 'paused' and
                                    paused['operation']['last_result']['stop_reason'] == 'backoff' and not transport.calls)
                            checked('backoff_reports_a_real_future_retry_instead_of_a_frozen_percentage',
                                    paused['operation']['last_result']['waiting'] is True and
                                    paused['operation']['last_result']['next_retry_at'] > time.time() + 3500 and
                                    paused['operation']['last_result']['wait_seconds'] > 3500 and
                                    paused['operation']['percent'] is None)
                            checked('resume_keeps_exact_job_ids_attempts_due_and_payloads', jobs_snapshot(engine.store) == baseline)
                            checked('235_articles_and_64_pending_are_visible',
                                    api(page, 'workflow')['body']['counts']['articles'] == 235 and
                                    api(page, 'workflow')['body']['counts']['pending_downloads'] == 64)
                            expect(page.locator('#workflow-news-count')).to_have_text('235')
                            expect(page.locator('#m-jobs')).to_have_text('64')
                            expect(page.locator('#workflow-tasks-deferred')).to_have_text('64')
                            expect(page.locator('#workflow-tasks-retained')).to_have_text('64')
                            expect(page.locator('#workflow-coverage-note')).to_contain_text('235')
                            expect(page.locator('#workflow-download-wait')).to_contain_text('Próximo intento permitido')
                            wait_until(page, lambda: 'pausa' in page.locator('#workflow-download-status').inner_text().lower(),
                                       'The paused status was not rendered')
                            checked('analysis_is_guarded_until_explicit_partial_close',
                                    api(page, 'workflow-analyze', {})['status'] == 409)
                            page.locator('.download-grid').evaluate(
                                "element=>element.scrollIntoView({block:'start',behavior:'instant'})")
                            screenshot(page, 'desktop-235-news-64-pending')
                            page.reload()
                            page.locator('[data-panel="news"]').wait_for()
                            checked('paused_queue_survives_reload_without_automatic_acquisition',
                                    len(started_payloads) == 1 and jobs_snapshot(engine.store) == baseline)
                            page.mouse.wheel(0, 12000)
                            page.wait_for_timeout(100)
                            scroll = page.evaluate('window.scrollY')
                            page.wait_for_timeout(1300)
                            checked('polling_preserves_scroll_in_the_large_news_register',
                                    scroll > 0 and abs(page.evaluate('window.scrollY') - scroll) < 4)
                            page.set_viewport_size({'width': 390, 'height': 844})
                            checked('mobile_has_no_horizontal_overflow',
                                    page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                            partial = page.locator('#workflow-accept-partial')
                            checked('mobile_partial_close_is_reachable_by_scrolling', reachable(page, partial))
                            screenshot(page, 'mobile-235-news-64-pending')
                            partial.click()
                            page.locator('[data-panel="analysis"]').wait_for()
                            flow = api(page, 'workflow')['body']
                            checked('inactive_partial_close_keeps_all_64_jobs_and_records_limitations',
                                    jobs_snapshot(engine.store) == baseline and flow['coverage']['accepted_partial'] and
                                    flow['coverage']['closed_partial'] and bool(flow['coverage']['limitations']) and
                                    flow['actions']['analyze']['enabled'] and engine.store.stats()['scores'] == 0)
                            checked('partial_close_does_not_claim_full_download_completion',
                                    flow['counts']['pending_downloads'] == 64 and flow['coverage']['partial'])
                            page.locator('#workflow-analyze').click()
                            analyzed = idle(page, 'analysis')
                            checked('actual_analysis_completes_with_the_unchanged_inactive_queue',
                                    analyzed['operation']['status'] == 'completed' and
                                    analyzed['workflow']['analysis_current'] and jobs_snapshot(engine.store) == baseline)
                            explanation = api(page, 'explanation?id=' + entity['id'])
                            checked('saved_score_archives_235_metadata_only_articles_and_64_pending_tasks',
                                    explanation['status'] == 200 and
                                    explanation['body']['result']['provenance']['acquisition']['metadata_only_articles'] == 235 and
                                    explanation['body']['result']['provenance']['acquisition']['pending_tasks'] == 64 and
                                    explanation['body']['result']['provenance']['acquisition']['accepted_partial'] is True)
                            checked('partial_acquisition_snapshot_remains_replayable', api(page, 'replay',
                                {'run_id': explanation['body']['run']['id']})['body']['matched'])
                            checked('all_235_preserved_articles_remain_unique', news.stats()['articles'] == 235)
                            page.locator('[data-panel="bank"]').wait_for()
                            screenshot(page, 'mobile-partial-results')

                            # An eligible fixture is made ready by the test setup, never by
                            # a resume endpoint that would bypass an upstream Retry-After.
                            selected = next(row['id'] for row in baseline if row['status'] == 'deferred'
                                and json.loads(row['payload'])['url'].endswith('/171'))
                            with engine.store.connection() as con:
                                con.execute('UPDATE jobs SET due=? WHERE id=?', (time.time() - 1, selected))
                            ready_baseline = jobs_snapshot(engine.store)
                            transport.hold = True
                            page.locator('#workflow-mobile-step').select_option('news')
                            page.locator('[data-panel="news"]').wait_for()
                            page.locator('#news-online').check()
                            resume = page.locator('#workflow-resume-download')
                            checked('mobile_resume_is_reachable_by_scrolling', reachable(page, resume))
                            resume.click()
                            wait_until(page, transport.entered.is_set, 'The real worker did not claim the ready fixture')
                            checked('every_resume_suppresses_new_discovery',
                                    len(started_payloads) == 2 and all(payload.get('resume') is True for payload in started_payloads))
                            busy = api(page, 'stats')['body']['operation']
                            checked('resume_revokes_previous_partial_results_while_the_worker_is_active',
                                    busy['busy'] and not api(page, 'workflow')['body']['analysis_current'] and
                                    api(page, 'explanation?id=' + entity['id'])['status'] == 409)
                            for endpoint, payload in (
                                ('workflow-finish-download', {}), ('workflow-accept-partial', {}),
                                ('workflow-analyze', {}), ('workflow-mode', {'mode': 'local'}),
                                ('import-entities', {'text': 'name\nOtra entidad ficticia\n'}),
                            ):
                                checked('active_worker_blocks_' + endpoint.replace('-', '_'), api(page, endpoint, payload)['status'] == 409)
                            checked('pause_is_only_a_request_while_transport_is_active',
                                    api(page, 'stop', {'operation_id': busy['operation_id']})['status'] == 200 and
                                    api(page, 'stats')['body']['operation']['busy'])
                            transport.release.set()
                            stopped = idle(page)
                            after = jobs_snapshot(engine.store)
                            checked('resume_does_not_add_jobs_or_rewrite_other_backoff_tasks',
                                    {row['id'] for row in after} == {row['id'] for row in baseline} and
                                    [row for row in after if row['id'] != selected] ==
                                    [row for row in ready_baseline if row['id'] != selected])
                            checked('real_worker_acknowledges_user_pause_and_preserves_64_pending',
                                    stopped['operation']['status'] == 'paused' and
                                    stopped['operation']['last_result']['stop_reason'] == 'cancelled' and
                                    stopped['workflow']['counts']['pending_downloads'] == 64)
                            checked('backoff_does_not_consume_an_extra_retry_attempt',
                                    next(row for row in after if row['id'] == selected)['attempts'] == 2)
                            page.reload()
                            page.locator('[data-panel="news"]').wait_for()
                            partial = page.locator('#workflow-accept-partial')
                            checked('partial_close_remains_available_after_user_pause', reachable(page, partial))
                            partial.click()
                            page.locator('[data-panel="analysis"]').wait_for()
                            page.locator('#workflow-analyze').click()
                            final = idle(page, 'analysis')
                            checked('analysis_can_be_repeated_after_another_explicit_inactive_close',
                                    final['workflow']['analysis_current'] and news.stats()['articles'] == 235 and
                                    final['workflow']['counts']['pending_downloads'] == 64)
                            report['final_workflow'] = final['workflow']
                            unchanged_jobs = jobs_snapshot(engine.store)
                            page.locator('#workflow-mobile-step').select_option('sources')
                            page.locator('[data-panel="sources"]').wait_for()
                            page.locator('#workflow-history-end').fill('2025-12-31')
                            page.locator('#workflow-save-search').click()
                            wait_until(page, lambda: api(page, 'news-search')['body']['config']['end'] == '2025-12-31',
                                       'The changed historical scope was not saved')
                            page.locator('#workflow-sources-next').click()
                            page.locator('[data-panel="news"]').wait_for()
                            changed_scope = api(page, 'workflow')['body']
                            checked('changing_the_historical_range_requires_a_new_search_without_rewriting_old_jobs',
                                    changed_scope['coverage']['needs_new_search'] and
                                    not changed_scope['coverage']['resume_available'] and
                                    not changed_scope['analysis_current'] and jobs_snapshot(engine.store) == unchanged_jobs)
                            expect(page.locator('#news-acquire')).to_have_attribute('data-download-action', 'start_new')
                            expect(page.locator('#news-acquire')).to_have_text('Iniciar nueva búsqueda')
                            checked('ui_distinguishes_a_new_historical_search_from_resuming_the_old_scope')
                            checked('api_rejects_resuming_a_different_scope_without_touching_the_old_queue',
                                    api(page, 'news-acquire', {'online': True, 'resume': True})['status'] == 409 and
                                    jobs_snapshot(engine.store) == unchanged_jobs)
                            report['changed_scope_workflow'] = changed_scope
                            checked('no_external_browser_requests_or_browser_errors',
                                    not errors and all(url.startswith(address + '/') for url in requests))
                            checked('audit_chain_is_valid_after_recovery_and_analysis', engine.store.verify_audit()['ok'])
                            report['resume_payloads'] = started_payloads
                            report['transport_runtime_calls'] = transport.calls
                            report['browser_version'] = browser.version
                        except Exception:
                            if page is not None and not page.is_closed():
                                screenshot(page, 'failure')
                            raise
                        finally:
                            transport.release.set()
                            browser.close()
                finally:
                    transport.release.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['passed'] = 'error' not in report and bool(report['checks']) and all(report['checks'].values())
        output = args.output or artifacts / 'validation.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'output': str(output)}))


if __name__ == '__main__':
    main()
