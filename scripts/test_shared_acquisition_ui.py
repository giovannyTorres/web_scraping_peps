"""Offline browser checks for the shared-index acquisition presentation.

Saved strategy, dates, budgets and permissions use the real local server and
SQLite bank of 300 fictional entities. Shared coverage counters are explicitly
injected presentation fixtures, not benchmark measurements or crawler output.
The crawler's data correctness and throughput require separate runtime tests.
Only loopback HTTP is permitted; no source is contacted or auto-authorized.
Run: python scripts/test_shared_acquisition_ui.py [--browser-path /path/to/chromium]
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    from peps.app import create_server
    from peps.engine import Engine
    from test_workflow_ui import api, wait_until

    artifacts = ROOT / 'runtime/shared-acquisition-ui-validation'
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {'kind': 'offline_shared_index_ui', 'processed_at': datetime.now(timezone.utc).isoformat(),
              'checks': {}, 'screenshots': [], 'fixture': {'entities': 300, 'real_saved_configuration': True,
                  'shared_metrics_are_injected_presentation_fixture': True, 'external_network': False}}
    errors, requests = [], []
    shared_stats = {}

    def checked(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    def screenshot(page, name, locator):
        locator.scroll_into_view_if_needed()
        locator.evaluate("element=>window.scrollTo({top:Math.max(0,window.scrollY+element.getBoundingClientRect().top-18),behavior:'instant'})")
        page.screenshot(path=str(artifacts / (name + '.png')))
        report['screenshots'].append('runtime/shared-acquisition-ui-validation/' + name + '.png')

    def permissions(page):
        response = api(page, 'news')
        assert response['status'] == 200, ('GET /api/news failed while reading source permissions: '
            f"HTTP {response['status']} · {json.dumps(response['body'], ensure_ascii=False)[:1000]}")
        sources = response['body'].get('sources')
        assert isinstance(sources, list), 'GET /api/news did not return a complete source catalogue'
        return {source['id']: {key: bool(source.get(key)) for key in
                ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved')}
                for source in sources}

    try:
        with tempfile.TemporaryDirectory(prefix='webscope-shared-ui-') as tmp:
            engine = Engine(tmp)
            engine.store.import_entities([{'id': f'shared-ui-{i:03d}', 'kind': 'company',
                                           'name': f'Entidad Ficticia Compartida {i:03d}'} for i in range(300)])
            config = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
            config['search'].update(strategy='targeted', execution_mode='quick', session_seconds=120,
                                    start='2020-01-01', end='2025-12-31')
            config['sources'] = [
                {'id': 'synthetic_wp_index', 'source_name': 'Índice WordPress ficticio', 'kind': 'wordpress_api',
                 'document_kind': 'news', 'url': 'https://fixture.invalid/wp-json/wp/v2/posts',
                 'api_url': 'https://fixture.invalid/wp-json/wp/v2/posts', 'allowed_hosts': ['fixture.invalid'],
                 'enabled': True, 'terms_approved': True, 'download_articles': False,
                 'article_terms_approved': False, 'min_interval_seconds': 5, 'max_records': 100},
                {'id': 'synthetic_sitemap_index', 'source_name': 'Sitemap ficticio', 'kind': 'sitemap',
                 'document_kind': 'news', 'url': 'https://sitemap.fixture.invalid/sitemap.xml',
                 'allowed_hosts': ['sitemap.fixture.invalid'], 'enabled': False, 'terms_approved': False,
                 'download_articles': False, 'article_terms_approved': False, 'min_interval_seconds': 5}]
            (engine.data_dir / 'news_sources.json').write_text(json.dumps(config), encoding='utf-8')
            with patch('peps.collector.SafeFetcher', side_effect=AssertionError('External transport is forbidden')):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=args.browser_path, headless=True,
                                                             args=['--no-sandbox'])
                        context = browser.new_context(viewport={'width': 1512, 'height': 1100})
                        context.on('request', lambda request: requests.append(request.url))

                        def route_request(route):
                            if not route.request.url.startswith(address + '/'):
                                route.abort()
                            elif route.request.url == address + '/api/stats' and shared_stats:
                                response = route.fetch()
                                body = response.json()
                                body['acquisition'].update(shared_stats)
                                route.fulfill(response=response, json=body)
                            else:
                                route.continue_()

                        context.route('**/*', route_request)
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        try:
                            page.goto(address)
                            page.evaluate('document.fonts.ready')
                            expect(page.locator('[data-panel="sources"]')).to_be_visible()
                            before = permissions(page)
                            page.locator('#source-show-all').check()
                            for name in ('Índice WordPress ficticio', 'Sitemap ficticio'):
                                source = page.locator('#news-sources .source').filter(has_text=name)
                                expect(source).to_contain_text('Índice compartido · una página para toda la lista')
                                expect(source.locator('input[type=checkbox]').nth(1)).not_to_be_checked()
                                assert source.locator('input[type=checkbox]').count() == 2
                            checked('wordpress_and_sitemap_keep_normal_explicit_body_permissions')
                            page.locator('#workflow-search-strategy').select_option('shared')
                            expect(page.locator('#workflow-search-preset')).to_be_disabled()
                            catalogue = {source['id']: source for source in api(page, 'news')['body']['sources']}
                            real_shared_ids = {'contralinea_index', 'aristegui_index', 'desinformemonos_index',
                                               'contralinea_sitemap', 'mcci_sitemap', 'articulo19_sitemap'}
                            checked('six_real_catalogue_index_sources_are_available_without_automatic_permissions',
                                    real_shared_ids <= set(catalogue) and all(not catalogue[source_id]['enabled'] and
                                        not catalogue[source_id]['terms_approved'] and not catalogue[source_id]['download_articles']
                                        for source_id in real_shared_ids))
                            rows = page.locator('#news-sources>.source')
                            shared_count = sum(source.get('kind') in ('wordpress_api', 'sitemap') for source in catalogue.values())
                            checked('shared_strategy_places_all_capable_sources_first',
                                    rows.evaluate_all('(rows,count)=>rows.slice(0,count).every(row=>row.dataset.sharedIndex==="true")&&rows.slice(count).every(row=>row.dataset.sharedIndex==="false")', shared_count))
                            for source_id in real_shared_ids:
                                source = page.locator('[data-source-id="' + source_id + '"]')
                                expect(source).to_be_visible()
                                expect(source).to_contain_text(catalogue[source_id]['source_name'])
                            checked('all_six_real_source_cards_are_rendered_with_their_catalogue_names')
                            omitted = page.locator('#news-sources>.source[data-shared-index="false"]')
                            omitted.first.locator('.source-conditions').evaluate('element=>element.open=true')
                            expect(omitted.first.locator('.source-shared-omitted')).to_be_visible()
                            checked('non_index_sources_stay_visible_with_scope_and_text_enrichment_explanation',
                                    omitted.count() == len(catalogue) - shared_count and
                                    omitted.first.locator('input[type=checkbox]').first.is_visible())
                            expect(page.locator('#workflow-shared-source-summary')).to_contain_text('sus permisos de texto siguen disponibles')
                            expect(page.locator('#workflow-shared-strategy-note')).to_contain_text('neutrales o adversas')
                            expect(page.locator('#workflow-budget-preview')).to_contain_text('Cada página se contrasta con toda la lista')
                            checked('shared_strategy_explains_neutral_preservation_and_disables_query_preset')
                            page.locator('#workflow-save-search').click()
                            wait_until(page, lambda: api(page, 'news-search')['body']['config']['strategy'] == 'shared',
                                       'Shared strategy was not saved')
                            saved = api(page, 'news-search')['body']
                            checked('real_plan_uses_shared_pages_without_fabricating_directed_queries',
                                    saved['plan']['entities_total'] == 300 and saved['plan']['entities_planned'] == 0 and
                                    saved['plan']['shared_discovery']['sources'] == 1 and
                                    saved['plan']['shared_discovery']['initial_pages'] > 0)
                            expect(page.locator('#workflow-search-plan')).to_contain_text('Cada página se contrasta con 300 entidades')
                            expect(page.locator('#workflow-search-plan')).not_to_contain_text('0 de 300 entidades incluidas')
                            checked('plan_labels_local_matching_instead_of_completed_entity_queries')
                            for seconds in (1800, 3600, 7200):
                                page.locator('#workflow-profile-extended').check()
                                page.locator('#workflow-time-budget').select_option(str(seconds))
                                page.locator('#workflow-save-search').click()
                                wait_until(page, lambda: api(page, 'news-search')['body']['config']['session_seconds'] == seconds,
                                           'Extended shared budget was not saved')
                            checked('all_extended_budgets_remain_available_for_shared_index')
                            page.locator('#workflow-profile-quick').check()
                            page.locator('#workflow-save-search').click()
                            wait_until(page, lambda: api(page, 'news-search')['body']['config']['session_seconds'] == 120,
                                       'Quick shared budget was not saved')
                            checked('shared_configuration_never_grants_source_or_body_permissions', permissions(page) == before)
                            screenshot(page, 'desktop-shared-strategy', page.locator('.search-options-grid').first)
                            page.reload()
                            expect(page.locator('#workflow-search-strategy')).to_have_value('shared')
                            expect(page.locator('#workflow-search-preset')).to_be_disabled()
                            checked('saved_shared_strategy_reorders_real_catalogue_after_reload',
                                    page.locator('#news-sources>.source').evaluate_all('(rows,count)=>rows.slice(0,count).every(row=>row.dataset.sharedIndex==="true")', shared_count))
                            checked('shared_strategy_and_preset_lock_survive_reload')
                            page.locator('#workflow-auto-analyze').uncheck()
                            assert api(page, 'workflow-confirm-sources', {})['status'] == 200
                            page.locator('[data-tab="news"]').click()
                            expect(page.locator('[data-panel="news"]')).to_be_visible()
                            expect(page.locator('#workflow-session-budget')).to_contain_text('Índice compartido: 1 fuente preparada')
                            expect(page.locator('#workflow-session-budget')).not_to_contain_text('Primera sesión: hasta 0 de 300')
                            page.locator('#workflow-index-details > summary').click()
                            expect(page.locator('#workflow-shared-coverage')).to_be_visible()
                            expect(page.locator('#workflow-shared-progress-label')).to_have_text('Todavía no se han leído páginas del índice.')
                            checked('unstarted_index_has_zero_progress_and_no_false_full_coverage')
                            shared_stats.update(current_bank_index_ready=True, shared_pages_read=6, shared_records_checked=600, shared_pages_pending=3,
                                                entities_checked_locally=300, entities_queried=0,
                                                entities_with_candidates=7, entities_with_bodies=2)
                            wait_until(page, lambda: page.locator('#workflow-shared-pages').inner_text() == '6',
                                       'Injected presentation counters were not rendered')
                            for suffix, value in [('pages', '6'), ('records', '600'), ('entities', '300 de 300')]:
                                expect(page.locator('#workflow-shared-' + suffix)).to_have_text(value)
                            expect(page.locator('#workflow-entities-queried')).to_have_text('0 de 300')
                            expect(page.locator('#workflow-entities-candidates')).to_have_text('7 de 300')
                            expect(page.locator('#workflow-entities-bodies')).to_have_text('2 de 300')
                            checked('local_300_of_300_matching_is_separate_from_zero_directed_queries_and_partial_bodies')
                            checked('known_queue_progress_uses_observed_read_plus_pending',
                                    page.locator('#workflow-shared-progress').evaluate('p=>p.value===6&&p.max===9'))
                            expect(page.locator('#workflow-shared-progress')).to_have_attribute('aria-valuetext',
                                '6 páginas leídas y 3 pendientes conocidas. No representa todo el archivo.')
                            expect(page.locator('.shared-coverage-limit')).to_contain_text('puede crecer')
                            checked('progress_explicitly_limits_denominator_to_known_queue')
                            screenshot(page, 'desktop-shared-coverage', page.locator('#workflow-entity-coverage'))
                            shared_stats.update(shared_pages_pending=0, shared_pages_limited=1)
                            wait_until(page, lambda: page.locator('#workflow-shared-progress').evaluate('p=>p.max===6'),
                                       'Known queue completion was not rendered')
                            expect(page.locator('.shared-coverage-limit')).to_contain_text('no representa la cobertura de tus nombres ni de todo el histórico')
                            checked('complete_known_queue_does_not_claim_complete_historical_coverage')
                            expect(page.locator('#workflow-shared-limit-notice')).to_be_visible()
                            expect(page.locator('#workflow-shared-limit-notice')).to_contain_text('El recorrido alcanzó un límite y sigue parcial.')
                            expect(page.locator('#workflow-shared-limit-notice')).to_contain_text('Prioriza la búsqueda por nombre')
                            checked('reaching_page_cap_explains_partial_archive_and_concrete_ways_to_expand')
                            page.set_viewport_size({'width': 390, 'height': 844})
                            checked('mobile_shared_coverage_has_no_page_overflow', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                            screenshot(page, 'mobile-shared-coverage', page.locator('#workflow-shared-coverage'))
                            page.locator('#workflow-mobile-step').select_option('sources')
                            expect(page.locator('[data-panel="sources"]')).to_be_visible()
                            checked('mobile_shared_strategy_has_no_page_overflow', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                            screenshot(page, 'mobile-shared-strategy', page.locator('.search-options-grid').first)
                            page.evaluate('''()=>{window.sharedSourceNodeFixture=Array.from(document.querySelectorAll('#news-sources>.source')).map(row=>({row,inputs:Array.from(row.querySelectorAll('input'))}));}''')
                            page.locator('#workflow-search-strategy').select_option('targeted')
                            expect(page.locator('#workflow-search-preset')).to_be_enabled()
                            expect(page.locator('#workflow-shared-strategy-note')).to_be_hidden()
                            checked('returning_to_directed_strategy_restores_query_preset')
                            checked('restoring_catalogue_order_reuses_same_source_cards_and_permission_inputs', page.evaluate('''()=>window.sharedSourceNodeFixture.every(({row,inputs})=>row.isConnected&&inputs.every(input=>input.isConnected&&row.contains(input)))'''))
                            checked('targeted_strategy_restores_original_catalogue_order', rows.evaluate_all('rows=>rows.every((row,index)=>Number(row.dataset.catalogueOrder)===index)'))
                            expect(omitted.first.locator('.source-shared-omitted')).to_be_hidden()
                            # A strategy change moves existing DOM nodes even while
                            # one source's permission POST is waiting for its reply.
                            held = {}

                            def hold_permission_reply(route):
                                held['response'] = route.fetch()
                                held['route'] = route

                            context.route('**/api/news-source', hold_permission_reply)
                            editable = page.locator('[data-source-id="desinformemonos_search"]')
                            page.locator('#source-show-all').check()
                            enabled_input = editable.locator('input[type=checkbox]').first
                            enabled_input.check()
                            wait_until(page, lambda: 'route' in held, 'The permission save was not held')
                            expect(enabled_input).to_be_disabled()
                            page.locator('#workflow-search-strategy').select_option('shared')
                            expect(enabled_input).to_be_checked()
                            expect(enabled_input).to_be_disabled()
                            checked('source_order_change_preserves_checked_disabled_inputs_during_inflight_save', page.evaluate('''()=>window.sharedSourceNodeFixture.every(({row,inputs})=>row.isConnected&&inputs.every(input=>input.isConnected&&row.contains(input)))'''))
                            held['route'].fulfill(response=held['response'])
                            context.unroute('**/api/news-source', hold_permission_reply)
                            expect(enabled_input).to_be_enabled()
                            enabled_input.uncheck()
                            wait_until(page, lambda: permissions(page) == before, 'The explicit permission test was not restored')
                            checked('source_order_changes_never_grant_or_remove_permissions', permissions(page) == before)
                            checked('no_crawler_started_and_no_external_requests', not api(page, 'stats')['body']['operation']['busy'] and
                                    all(url.startswith(address + '/') for url in requests))
                            checked('browser_has_no_page_errors', not errors)
                            checked('audit_chain_is_valid', engine.store.verify_audit()['ok'])
                            report.update(page_errors=errors, browser_version=browser.version,
                                          network_request_count=len(requests), saved_shared_plan=saved['plan'])
                        except Exception:
                            page.screenshot(path=str(artifacts / 'failure.png'), full_page=True)
                            raise
                        finally:
                            browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        report['passed'] = 'error' not in report and bool(report['checks']) and all(report['checks'].values())
        output = args.output or artifacts / 'validation.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'output': str(output)}))


if __name__ == '__main__':
    main()
