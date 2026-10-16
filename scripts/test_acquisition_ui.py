"""Offline browser regression for acquisition budgets, relevance and score coverage.

Run: python scripts/test_acquisition_ui.py [--browser-path /path/to/chromium]
Requires playwright==1.62.0 and Chromium. Only loopback HTTP is permitted.
The real SQLite fixture has 300 fictional entities, 235 retained references,
24 observed searches, eight metadata candidates and no acquired article bodies.
Two separately imported official documents exercise a genuine zero and a
positive score; an entity without evidence must show an em dash, not a zero.
Reports and screenshots default to ignored runtime/acquisition-ui-validation/.
Use --plan-only without Playwright; --output changes the JSON report path.
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

SCENARIOS = [
    'A real 300-entity SQLite bank distinguishes 235 old references from eight relevant candidates and zero acquired bodies.',
    'Legacy extended/900-second scope is preserved; 30-minute, one-hour, two-hour and quick/120-second budgets persist.',
    'Saving quick mode never enables a source or grants text permissions; the missing-text warning is visible before download.',
    'The download panel separates planned entities, observed searches, candidates and bodies, including after reload.',
    'Explicit local analysis preserves a numeric zero for neutral admissible text, a positive score for adverse admissible text and an em dash for insufficient evidence.',
    'Desktop and 390-pixel mobile views retain readable score coverage and have no page overflow, browser errors or external requests.',
]


def seed_case(engine):
    """Seed recorded synthetic observations, never fabricated scores or API JSON."""
    from peps.news import NewsAcquirer
    from peps.store import canonical, now

    entities = [{'id': f'acquisition-{i:03d}', 'name': f'Entidad Sintética de Prueba {i:03d}',
                 'kind': 'company'} for i in range(300)]
    engine.store.import_entities(entities)
    config = json.loads((ROOT / 'peps/defaults/news_sources.json').read_text(encoding='utf-8'))
    config['search'].update(execution_mode='extended', session_seconds=900)
    source = {'id': 'synthetic_search', 'source_name': 'Archivo enteramente sintético de pruebas',
              'kind': 'rss', 'search_kind': 'wordpress_rss', 'document_kind': 'news',
              'url': 'https://fixture.invalid/feed/', 'base_url': 'https://fixture.invalid/',
              'search_url': 'https://fixture.invalid/', 'allowed_hosts': ['fixture.invalid'],
              'enabled': True, 'terms_approved': True, 'download_articles': False,
              'article_terms_approved': False, 'max_records': 10, 'min_interval_seconds': 5,
              'review_note': 'Configuración sintética sin descargas externas; sólo se valida la interfaz.'}
    config['sources'] = [source]
    (engine.data_dir / 'news_sources.json').write_text(json.dumps(config), encoding='utf-8')
    news = NewsAcquirer(engine)
    with engine.store.connection() as con:
        con.execute('BEGIN IMMEDIATE')
        for i in range(235):
            article_id, url = f'synthetic-article-{i}', f'https://fixture.invalid/article-{i}'
            title = entities[i]['name'] + ': referencia sintética' if i < 8 else f'Referencia ajena sintética {i}'
            con.execute('INSERT INTO news_articles(id,canonical_url,publisher_id,source_id,title,summary,discovered_at,scope,status) '
                        'VALUES(?,?,?,?,?,?,?,?,?)', (article_id, url, 'fixture.invalid', source['id'], title,
                        'Resumen sintético sin el cuerpo original del editor.', now(), 'feed_metadata_only', 'discovered'))
            con.execute('INSERT INTO news_observations(id,article_id,source_id,discovery_url,observed_url,retrieved_at,details) '
                        'VALUES(?,?,?,?,?,?,?)', (f'observation-{i}', article_id, source['id'], source['url'], url, now(),
                        canonical({'retrieval_relevance': {'relevant': i < 8,
                            'entity_ids': [entities[i]['id']] if i < 8 else [], 'identity_verified': False}})))
        engine.store._audit(con, 'news_discovery_completed', {'synthetic_fixture': True, 'records': 235})
    for i in range(24):
        job_id = engine.store.enqueue('news_query', {'entity_ids': [entities[i]['id']]}, key=f'synthetic-query-{i}')
        with engine.store.connection() as con:
            con.execute('INSERT INTO news_discovery_runs VALUES(?,?,?,?,?,?,?,?)',
                        (job_id, source['id'], 'news_query', 'https://fixture.invalid/search', 235 if i == 0 else 0,
                         canonical({'candidate_filter': {'excluded_records': 223 if i == 0 else 0}}), 'synthetic-hash', now()))
    while job := engine.store.claim(kind_prefix='news_'):
        engine.store.finish(job)
    # Full official text is independent of publisher-body permissions and of
    # the 235 acquired metadata records. Scores are produced by the real scorer.
    for index, ending in [(9, 'inauguró una biblioteca pública.'), (10, 'fue condenada por fraude.')]:
        entity = entities[index]
        engine.import_documents([{'url': f'https://official.fixture.invalid/document-{index}',
            'title': 'Boletín enteramente ficticio de prueba', 'source_kind': 'official',
            'published_at': datetime.now(timezone.utc).date().isoformat(),
            'entity_ids': [entity['id']], 'text': entity['name'] + ' ' + ending}])
    return news, entities


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()
    if args.plan_only:
        print(json.dumps({'kind': 'offline_acquisition_ui_plan', 'scenarios': SCENARIOS}, ensure_ascii=False, indent=2))
        return

    from playwright.sync_api import expect, sync_playwright
    from peps.app import create_server
    from peps.engine import Engine
    from test_workflow_ui import api, wait_until

    artifacts = ROOT / 'runtime/acquisition-ui-validation'
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {'kind': 'offline_acquisition_budget_relevance_and_assessment',
              'processed_at': datetime.now(timezone.utc).isoformat(), 'scenarios': SCENARIOS,
              'checks': {}, 'screenshots': [], 'fixture': {'entities': 300, 'retained_references': 235,
                  'observed_searches': 24, 'candidate_entities': 8, 'acquired_bodies': 0,
                  'independent_local_documents': 2, 'excluded_records': 223, 'real_sqlite': True}}

    def checked(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    def screenshot(page, name, locator=None):
        if locator:
            locator.scroll_into_view_if_needed()
            locator.evaluate("element=>window.scrollTo({top:Math.max(0,window.scrollY+element.getBoundingClientRect().top-18),behavior:'instant'})")
        else:
            page.evaluate('window.scrollTo(0,0)')
        page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
        page.screenshot(path=str(artifacts / (name + '.png')), full_page=False)
        report['screenshots'].append('runtime/acquisition-ui-validation/' + name + '.png')

    def permissions(page):
        return {source['id']: {key: bool(source.get(key)) for key in
                ('enabled', 'terms_approved', 'download_articles', 'article_terms_approved', 'body_terms_approved')}
                for source in api(page, 'news')['body']['sources']}

    errors, requests = [], []
    page = None
    try:
        with tempfile.TemporaryDirectory(prefix='webscope-acquisition-ui-') as tmp:
            engine = Engine(tmp)
            news, entities = seed_case(engine)
            checked('fixture_does_not_precompute_any_scores', engine.store.stats()['scores'] == 0)
            # A regression must fail before opening any backend network transport.
            with patch('peps.collector.SafeFetcher', side_effect=AssertionError('External transport is forbidden in this test')):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=args.browser_path,
                            headless=True, args=['--no-sandbox'])
                        context = browser.new_context(viewport={'width': 1512, 'height': 1100})
                        context.on('request', lambda request: requests.append(request.url))
                        context.route('**/*', lambda route: route.continue_()
                                      if route.request.url.startswith(address + '/') else route.abort())
                        page = context.new_page()
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        try:
                            page.goto(address)
                            page.evaluate('document.fonts.ready')
                            expect(page.locator('[data-panel="sources"]')).to_be_visible()
                            expect(page.locator('#workflow-profile-extended')).to_be_checked()
                            expect(page.locator('#workflow-time-budget')).to_have_value('900')
                            checked('legacy_900_second_budget_is_preserved')
                            before = permissions(page)
                            expect(page.locator('#workflow-permission-summary')).to_contain_text('0 con texto autorizado')
                            source = page.locator('#news-sources .source').filter(has_text='Archivo enteramente sintético de pruebas')
                            expect(source.locator('input[type=checkbox]').nth(1)).not_to_be_checked()
                            assert source.locator('input[type=checkbox]').count() == 2
                            checked('metadata_permissions_are_explicit_and_initially_unchecked')
                            for seconds in (1800, 3600, 7200):
                                page.locator('#workflow-time-budget').select_option(str(seconds))
                                page.locator('#workflow-save-search').click()
                                wait_until(page, lambda: api(page, 'news-search')['body']['config']['session_seconds'] == seconds,
                                           'The extended budget was not saved')
                                saved = api(page, 'news-search')['body']
                                checked(f'extended_{seconds}_seconds_saved_through_ui', saved['config']['execution_mode'] == 'extended')
                            page.locator('#workflow-profile-quick').check()
                            page.locator('#workflow-save-search').click()
                            wait_until(page, lambda: api(page, 'news-search')['body']['config']['execution_mode'] == 'quick',
                                       'The quick mode was not saved')
                            saved = api(page, 'news-search')['body']
                            checked('quick_budget_is_120_seconds_in_config_and_plan', saved['config']['session_seconds'] == 120 and
                                    saved['plan']['execution']['session_seconds'] == 120)
                            checked('planned_entities_are_not_reported_as_completed_queries', saved['plan']['entities_planned'] == 300 and
                                    saved['plan']['estimated_entities_first_session'] < 300 and
                                    api(page, 'stats')['body']['acquisition']['entities_queried'] == 24)
                            checked('saving_budgets_does_not_auto_enable_or_approve_any_source', permissions(page) == before)
                            expect(page.locator('#workflow-time-budget-field')).to_be_hidden()
                            screenshot(page, 'desktop-budget', page.locator('.execution-budget'))
                            page.locator('#workflow-auto-analyze').uncheck()
                            assert api(page, 'workflow-confirm-sources', {})['status'] == 200
                            page.locator('[data-tab="news"]').click()
                            expect(page.locator('[data-panel="news"]')).to_be_visible()
                            expect(page.locator('#workflow-session-budget')).to_contain_text('Rápido · hasta 2 min')
                            expect(page.locator('#workflow-session-budget')).to_contain_text('24 de 300')
                            expect(page.locator('#workflow-session-permission-warning')).to_be_visible()
                            checked('budget_and_no_body_warning_are_visible_before_start')
                            coverage = api(page, 'stats')['body']['acquisition']
                            checked('real_sqlite_coverage_has_expected_300_235_distinctions',
                                    coverage['entities_total'] == 300 and coverage['entities_queried'] == 24 and
                                    coverage['entities_with_candidates'] == 8 and coverage['entities_with_bodies'] == 0 and
                                    coverage['relevant_articles'] == 8 and coverage['metadata_only_articles'] == 8 and
                                    coverage['body_articles'] == 0 and coverage['excluded_records'] == 223 and news.stats()['articles'] == 235)
                            for suffix, text in [('queried', '24 de 300'), ('candidates', '8 de 300'), ('bodies', '0 de 300')]:
                                expect(page.locator('#workflow-entities-' + suffix)).to_have_text(text)
                            expect(page.locator('#workflow-news-count')).to_have_text('235')
                            expect(page.locator('#workflow-summary-entity-count')).to_have_text('0')
                            checked('rendered_coverage_prioritizes_entities_over_raw_reference_count')
                            expect(page.locator('#news-articles .evidence')).to_have_count(8)
                            checked('news_register_omits_227_unrelated_retained_references')
                            screenshot(page, 'desktop-coverage')
                            screenshot(page, 'desktop-coverage-detail', page.locator('#workflow-entity-coverage'))
                            page.reload()
                            expect(page.locator('[data-panel="news"]')).to_be_visible()
                            expect(page.locator('#workflow-session-budget')).to_contain_text('Rápido · hasta 2 min')
                            checked('quick_budget_survives_reload_without_starting_a_worker',
                                    not api(page, 'stats')['body']['operation']['busy'] and permissions(page) == before)
                            page.set_viewport_size({'width': 390, 'height': 844})
                            checked('mobile_coverage_has_no_page_overflow', page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
                            screenshot(page, 'mobile-coverage')
                            screenshot(page, 'mobile-coverage-detail', page.locator('#workflow-entity-coverage'))
                            page.locator('#workflow-mobile-step').select_option('sources')
                            expect(page.locator('[data-panel="sources"]')).to_be_visible()
                            screenshot(page, 'mobile-budget', page.locator('.execution-budget'))
                            page.locator('#workflow-mode-local').click()
                            expect(page.locator('#workflow-local-documents')).to_be_visible()
                            page.locator('#workflow-sources-next').click()
                            expect(page.locator('[data-panel="news"]')).to_be_visible()
                            page.locator('#news-acquire').click()
                            expect(page.locator('[data-panel="analysis"]')).to_be_visible()
                            page.locator('#workflow-analyze').click()
                            wait_until(page, lambda: api(page, 'workflow')['body']['analysis_current'],
                                       'Explicit local analysis did not finish', timeout=45)
                            expect(page.locator('[data-panel="bank"]')).to_be_visible()
                            checked('explicit_local_analysis_saved_300_actual_scores', engine.store.stats()['scores'] == 300)
                            cases = [(8, 'insufficient_evidence'), (9, 'evaluated'), (10, 'evaluated')]
                            assessments = {}
                            for index, expected_state in cases:
                                entity = entities[index]
                                row = page.get_by_role('row', name='Consultar ' + entity['name'], exact=True)
                                row.click()
                                expect(page.locator('#detail>h2')).to_have_text(entity['name'])
                                wait_until(page, lambda: not page.locator('#detail').get_attribute('aria-busy'), 'Explanation did not finish')
                                explanation = api(page, 'explanation?id=' + entity['id'])['body']
                                assessment = explanation['assessment']
                                assessments[entity['id']] = assessment
                                score = explanation['result']['score']
                                checked(f'assessment_{index}_uses_real_trace_state', assessment['state'] == expected_state)
                                if index == 8:
                                    checked('missing_evidence_has_internal_zero_but_no_display_score', score == 0 and assessment['display_score'] is None)
                                    expect(row.locator('.score')).to_have_text('—')
                                    expect(page.locator('.score-number strong')).to_have_text('—')
                                    expect(page.locator('.score-unavailable')).to_be_visible()
                                    expect(page.get_by_role('heading', name='Qué falta para evaluar', exact=True)).to_be_visible()
                                    screenshot(page, 'mobile-insufficient-evidence', page.locator('#detail'))
                                elif index == 9:
                                    checked('admissible_neutral_text_preserves_numeric_zero', score == 0 and assessment['display_score'] == 0)
                                    expect(row.locator('.score')).to_have_text('0')
                                    expect(page.locator('.score-number strong')).to_have_text('0')
                                    expect(page.locator('.score-unavailable')).to_have_count(0)
                                else:
                                    checked('admissible_adverse_text_has_bounded_positive_score', 0 < score <= 95 and assessment['display_score'] == score)
                                    expect(row.locator('.score')).to_have_text(str(score))
                                    expect(page.locator('.score-number strong')).to_have_text(str(score))
                                    ranges = explanation['explanation']['interpretation']['ranges']
                                    selected = [band for band in ranges if band['min'] <= score <= band['max']]
                                    checked('positive_score_uses_exactly_one_published_band', len(selected) == 1)
                                    band = selected[0]
                                    value = str(band['min']) if band['min'] == band['max'] else f"{band['min']}–{band['max']}"
                                    expect(page.locator('.active-range')).to_have_text(value + ': ' + band['label'])
                                    report['positive_control'] = {'score': score, 'band': band}
                                checked(f'mobile_assessment_{index}_has_no_page_overflow', page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
                            screenshot(page, 'mobile-positive-evidence', page.locator('#detail'))
                            page.set_viewport_size({'width': 1512, 'height': 1100})
                            screenshot(page, 'desktop-results')
                            checked('permissions_remain_unchanged_after_analysis', permissions(page) == before)
                            checked('no_external_requests_or_browser_errors', not errors and all(url.startswith(address + '/') for url in requests))
                            checked('audit_chain_is_valid', engine.store.verify_audit()['ok'])
                            report.update(coverage=coverage, assessments=assessments, page_errors=errors,
                                          network_request_count=len(requests), browser_version=browser.version)
                        except Exception:
                            if page is not None and not page.is_closed():
                                screenshot(page, 'failure')
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
