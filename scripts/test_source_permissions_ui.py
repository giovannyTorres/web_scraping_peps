"""Offline functional UI checks for discovery versus public-original reading.

Fictional endpoints are never contacted. This checks permissions, persisted
choices and controls, not search performance, volume or scoring quality.
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
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, expect
    from peps.app import create_server
    from peps.engine import Engine
    from peps.news import NewsAcquirer

    output = ROOT / 'runtime/source-permissions-ui-validation'
    output.mkdir(parents=True, exist_ok=True)
    report = {'kind': 'offline_functional_source_permissions', 'network_benchmark': False,
              'at': datetime.now(timezone.utc).isoformat(), 'checks': {}, 'layout': {}}

    def check(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    # Start from the actual new-install catalogue, not an already useful plan.
    with tempfile.TemporaryDirectory() as fresh_tmp, patch('peps.collector.SafeFetcher', side_effect=AssertionError('No acquisition while preparing a fresh installation')):
        fresh_engine = Engine(fresh_tmp)
        fresh_engine.store.import_entities([
            {'id': f'fresh-{index:03d}', 'name': f'Persona Sintética de Prueba {index:03d}'}
            for index in range(300)])
        fresh_news = NewsAcquirer(fresh_engine)
        server, _ = create_server(fresh_engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        fresh_errors, fresh_external, fresh_mutations = [], [], []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=args.browser_path, headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.on('pageerror', lambda error: fresh_errors.append(str(error)))
                page.on('request', lambda request: fresh_external.append(request.url) if not request.url.startswith(address + '/') else fresh_mutations.append(request.url) if request.method == 'POST' else None)
                page.goto(address)
                page.locator('#workflow-mobile-step').select_option('sources')
                expect(page.locator('#public-source-setup')).to_be_visible()
                expect(page.locator('#workflow-sources-next')).to_be_disabled()
                check('fresh_install_starts_with_zero_queries_and_no_source_permissions',
                      fresh_news.search_plan()['query_count'] == 0
                      and all(not source.get('enabled') and not source.get('terms_approved') for source in fresh_news.sources()))
                page.locator('#prepare-public-sources').click()
                estimate = page.locator('#workflow-first-session-estimate')
                expect(estimate).to_be_visible()
                expect(estimate).to_contain_text('120 de 300 entidades')
                expect(estimate).to_contain_text('podrían iniciarse consultas')
                expect(estimate).to_contain_text('no garantiza cobertura')
                check('quick_120_of_300_limit_is_visible_outside_collapsed_details', estimate.locator('xpath=ancestor::details').count() == 0)
                page.locator('#news-online').check()
                expect(page.locator('#workflow-sources-next')).to_be_enabled()
                check('fresh_zero_plan_becomes_startable_after_shortcut_without_reload_or_scope_submit',
                      fresh_news.search_plan()['query_count'] == 300
                      and fresh_mutations == [address + '/api/news-source', address + '/api/news-source'])
                page.screenshot(path=str(output / 'fresh-300-quick-mobile.png'), full_page=True)
                before = fresh_news.search_config()
                with page.expect_response(lambda response: response.url == address + '/api/news-search'
                                          and response.request.method == 'POST') as expanded:
                    page.locator('#workflow-use-ten-minutes').click()
                assert expanded.value.status == 200, expanded.value.text()
                expect(page.locator('#workflow-time-budget')).to_have_value('600')
                expect(estimate).to_contain_text('300 de 300 entidades')
                expect(page.locator('#workflow-sources-next')).to_be_enabled()
                after = fresh_news.search_config()
                check('one_explicit_ten_minute_action_recalculates_plan_and_preserves_other_options',
                      after['execution_mode'] == 'extended' and after['session_seconds'] == 600
                      and all(after[key] == value for key, value in before.items()
                              if key not in ('execution_mode', 'session_seconds')))
                lumy = page.locator('[data-source-id="lumy_general_v1"]')
                lumy.locator('input').uncheck()
                expect(page.locator('#workflow-sources-next')).to_be_disabled()
                expect(page.locator('#workflow-search-plan')).to_contain_text('0 consultas')
                lumy.locator('input').check()
                expect(page.locator('#workflow-sources-next')).to_be_enabled()
                expect(estimate).to_contain_text('300 de 300 entidades')
                check('individual_permission_changes_recalculate_zero_and_nonzero_plans_without_reentry')
                def fail_plan_read(route):
                    if route.request.method == 'GET':
                        route.fulfill(status=503, content_type='application/json', body='{"error":"synthetic local plan read failed"}')
                    else:
                        route.continue_()
                page.route('**/api/news-search', fail_plan_read)
                lumy.locator('input').uncheck()
                expect(page.locator('#workflow-search-status')).to_contain_text('No se pudo cargar la búsqueda')
                expect(page.locator('#workflow-sources-next')).to_be_disabled()
                page.unroute('**/api/news-search', fail_plan_read)
                lumy.locator('input').check()
                expect(page.locator('#workflow-sources-next')).to_be_enabled()
                check('failed_plan_refresh_keeps_start_disabled_until_a_fresh_plan_is_read')
                saved = fresh_news.search_config()
                page.locator('#workflow-history-start').fill('2020-01-01')
                originals = page.locator('[data-source-id="public_research_articles"]')
                originals.locator('input').uncheck()
                expect(originals.locator('.source-permission-badge')).to_have_text('Lectura pública sin activar')
                expect(page.locator('#workflow-search-status')).to_contain_text('Permisos actualizados')
                expect(page.locator('#workflow-history-start')).to_have_value('2020-01-01')
                expect(estimate).to_be_hidden()
                check('source_plan_refresh_preserves_unsaved_search_draft_and_saved_settings', fresh_news.search_config() == saved)
                check('fresh_setup_and_budget_change_never_start_acquisition_or_external_requests',
                      not fresh_external and not any('/api/workflow-run' in url for url in fresh_mutations))
                check('fresh_mobile_controls_have_no_page_overflow_or_browser_errors',
                      page.evaluate('document.documentElement.scrollWidth<=innerWidth') and not fresh_errors)
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()

    with tempfile.TemporaryDirectory() as tmp, patch('peps.collector.SafeFetcher', side_effect=AssertionError('No external acquisition in functional controls')):
        engine = Engine(tmp)
        engine.store.import_entities([{'id': 'fixture', 'name': 'Persona Sintética Permisos'}])
        news = NewsAcquirer(engine)
        config = {'search': {**news.search_config(), 'strategy': 'shared', 'execution_mode': 'extended', 'session_seconds': 1800},
                  'sources': [
                      {'id': 'fixture_api', 'source_name': 'Buscador sintético JSON', 'kind': 'searxng',
                       'search_url': 'https://search.fixture.invalid/search', 'allowed_hosts': ['search.fixture.invalid'],
                       'enabled': False, 'terms_approved': False, 'robots_required': True,
                       'download_articles': False, 'article_terms_approved': False},
                      {'id': 'fixture_public', 'source_name': 'Originales públicos sintéticos', 'kind': 'rss',
                       'discover_public_article_hosts': True, 'public_research_scope': 'personal_local_research',
                       'discovery': False, 'automated_search': False, 'allowed_hosts': [], 'robots_required': True,
                       'enabled': False, 'terms_approved': False, 'download_articles': False, 'article_terms_approved': False}
                  ]}
        news.config_path.write_text(json.dumps(config), encoding='utf-8')
        server, _ = create_server(engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        errors, external, mutations = [], [], []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=args.browser_path, headless=True, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1365, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: external.append(request.url) if not request.url.startswith(address + '/') else mutations.append(request.url) if request.method == 'POST' else None)
                page.goto(address)
                page.locator('[data-workflow-step="sources"]').click()
                api = page.locator('[data-source-id="fixture_api"]')
                public = page.locator('[data-source-id="fixture_public"]')
                lumy = page.locator('[data-source-id="lumy_general_v1"]')
                originals = page.locator('[data-source-id="public_research_articles"]')
                expect(api).to_be_visible();expect(public).to_be_visible()
                expect(lumy).to_be_visible();expect(originals).to_be_visible()
                expect(page.locator('#public-source-setup')).to_be_hidden()
                check('existing_shared_strategy_has_no_nominal_preparation_shortcut')
                check('promoted_sources_are_visible_recommendations_with_permissions_off',
                      not lumy.locator('input').is_checked() and not originals.locator('input').is_checked())
                check('new_capabilities_visible_without_exploring_full_catalogue', not page.locator('#source-show-all').is_checked())
                page.locator('#workflow-search-strategy').select_option('targeted')
                check('nominal_default_shows_only_the_two_recommended_cards',
                      page.locator('#news-sources > .source:visible').evaluate_all('rows=>rows.map(row=>row.dataset.sourceId)')
                      == ['lumy_general_v1', 'public_research_articles'])
                page.locator('#source-show-all').check()
                check('full_catalogue_is_explicit_opt_in_and_retains_every_card',
                      page.locator('#news-sources > .source:visible').count() == len(news.sources()))
                page.locator('#source-show-all').uncheck()
                page.locator('#workflow-search-strategy').select_option('shared')
                expect(api).to_be_visible();expect(public).to_be_visible()
                check('previewing_nominal_cards_does_not_change_saved_shared_settings', news.search_config()['strategy'] == 'shared')
                check('api_has_only_discovery_control_without_discovery_only_flag', api.locator('input[type=checkbox]').count() == 1)
                check('reading_only_profile_is_activatable_instead_of_manual_search', public.locator('input[type=checkbox]').count() == 1 and 'Consulta manual' not in public.inner_text())
                check('permissions_start_off_without_automatic_enrollment', not api.locator('input').is_checked() and not public.locator('input').is_checked())
                api.locator('summary').click()
                check('search_url_is_displayed_even_without_url_or_base_url', api.locator('a').get_attribute('href') == 'https://search.fixture.invalid/search')
                api.locator('summary').click()
                api.locator('input').check()
                expect(api.locator('.source-permission-badge')).to_have_text('Sólo referencias')
                stored = next(s for s in news.sources() if s['id'] == 'fixture_api')
                check('saving_api_never_grants_editorial_body_permission', stored['enabled'] and stored['terms_approved'] and not stored['download_articles'] and not stored['article_terms_approved'])
                public.locator('input').check()
                expect(public.locator('.source-permission-badge')).to_have_text('Lectura pública activada')
                expect(page.locator('#workflow-permission-summary')).to_contain_text('1 fuente de búsqueda')
                expect(page.locator('#workflow-permission-summary')).to_contain_text('lectura de originales públicos activada')
                check('reading_policy_count_is_separate_from_search_and_editorial_license')
                check('saved_shared_strategy_and_30_minute_budget_remain_unchanged', news.search_config()['strategy'] == 'shared' and news.search_config()['session_seconds'] == 1800)
                page.locator('#workflow-time-budget').select_option('600')
                with page.expect_response(lambda response: response.url == address + '/api/news-search'
                                          and response.request.method == 'POST') as saved_search:
                    page.locator('#workflow-save-search').click()
                assert saved_search.value.status == 200, saved_search.value.text()
                expect(page.locator('#workflow-time-budget')).to_have_value('600')
                check('ten_minutes_is_persisted_without_changing_strategy', news.search_config()['session_seconds'] == 600 and news.search_config()['strategy'] == 'shared')
                public.locator('input').uncheck()
                expect(public.locator('.source-permission-badge')).to_have_text('Lectura pública sin activar')
                expect(page.locator('#workflow-permission-summary')).to_contain_text('sólo referencias')
                stored = next(s for s in news.sources() if s['id'] == 'fixture_public')
                check('revoking_public_reading_revokes_all_its_own_permission_flags', not any(stored[key] for key in ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved')))
                stored_api = next(s for s in news.sources() if s['id'] == 'fixture_api')
                check('revoking_reading_keeps_independent_api_choice', stored_api['enabled'] and stored_api['terms_approved'])
                page.locator('#workflow-search-strategy').select_option('targeted')
                with page.expect_response(lambda response: response.url == address + '/api/news-search'
                                          and response.request.method == 'POST'):
                    page.locator('#workflow-save-search').click()
                expect(page.locator('#public-source-setup')).to_be_visible()
                expect(lumy.locator('strong')).to_have_text('Búsqueda API · Lumy / Yandex')
                expect(originals.locator('strong')).to_have_text('Lectura de originales públicos')
                check('nominal_search_prioritizes_two_separate_recommended_scopes',
                      page.locator('#news-sources > .source').evaluate_all('(rows)=>rows.slice(0,2).map(row=>row.dataset.sourceId)')
                      == ['lumy_general_v1', 'public_research_articles'])
                check('reading_profile_does_not_link_its_non_endpoint_placeholder', originals.locator('a').count() == 0)
                check('nominal_view_keeps_existing_selected_sources_visible',
                      page.locator('#news-sources > .source:visible').evaluate_all('rows=>rows.map(row=>row.dataset.sourceId)')
                      == ['lumy_general_v1', 'public_research_articles', 'fixture_api'])
                check('preparation_shortcut_precedes_budget_dates_and_advanced_settings',
                      page.locator('#public-source-setup').evaluate('(node)=>Boolean(node.compareDocumentPosition(document.querySelector(".search-setup"))&Node.DOCUMENT_POSITION_FOLLOWING)'))
                report['layout']['desktop_height'] = page.evaluate('document.documentElement.scrollHeight')
                page.screenshot(path=str(output / 'recommended-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                expect(api).to_be_visible();expect(public).to_be_hidden()
                expect(page.locator('#prepare-public-sources')).to_be_visible()
                check('mobile_shortcut_explains_independent_scopes_before_activation',
                      'Puedes desmarcar cada permiso por separado' in page.locator('#public-source-setup').inner_text())
                report['layout']['mobile_height_before_preparation'] = page.evaluate('document.documentElement.scrollHeight')
                page.screenshot(path=str(output / 'recommended-mobile-before.png'), full_page=True)
                before = len(mutations)
                untouched_before = {source['id']: source for source in news.sources()
                                    if source['id'] not in ('lumy_general_v1', 'public_research_articles')}
                with page.expect_response(lambda response: response.url == address + '/api/news-source'
                                          and response.request.method == 'POST') as prepared:
                    page.locator('#prepare-public-sources').click()
                assert prepared.value.status == 200, prepared.value.text()
                page.wait_for_function('()=>window.pepsSourcesPending()===0')
                assert next(s for s in news.sources() if s['id'] == 'lumy_general_v1')['enabled'], (page.locator('#message').inner_text(), mutations[before:], errors)
                expect(lumy.locator('.source-permission-badge')).to_have_text('Sólo referencias')
                expect(originals.locator('.source-permission-badge')).to_have_text('Lectura pública activada')
                expect(page.locator('#public-source-setup')).to_be_hidden()
                changed = {source['id']: source for source in news.sources()}
                check('shortcut_only_persists_two_source_choices_without_starting_search',
                      mutations[before:] == [address + '/api/news-source', address + '/api/news-source'])
                check('shortcut_keeps_other_sources_and_existing_budget',
                      all(changed[sid] == source for sid, source in untouched_before.items())
                      and news.search_config()['session_seconds'] == 600)
                check('shortcut_api_and_originals_permissions_remain_separate',
                      changed['lumy_general_v1']['enabled'] and not changed['lumy_general_v1']['download_articles']
                      and changed['public_research_articles']['download_articles'])
                page.reload()
                page.locator('#workflow-mobile-step').select_option('sources')
                expect(lumy.locator('input')).to_be_checked();expect(originals.locator('input')).to_be_checked()
                lumy.locator('input').uncheck()
                expect(lumy.locator('.source-permission-badge')).to_have_text('Sin seleccionar')
                expect(originals.locator('.source-permission-badge')).to_have_text('Lectura pública activada')
                check('saved_shortcut_choices_are_reviewable_and_api_revocation_keeps_reader')
                report['layout']['mobile_height'] = page.evaluate('document.documentElement.scrollHeight')
                check('nominal_layout_height_is_reduced_on_desktop_and_mobile',
                      report['layout']['desktop_height'] < 3400 and report['layout']['mobile_height_before_preparation'] < 5000)
                page.screenshot(path=str(output / 'recommended-mobile.png'), full_page=True)
                check('mobile_source_controls_have_no_horizontal_page_overflow', page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                check('functional_controls_never_start_acquisition_or_external_requests', not external and not any('/api/workflow-run' in url for url in mutations))
                check('no_browser_errors', not errors)
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join()
    report['passed'] = all(report['checks'].values())
    (output / 'validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
