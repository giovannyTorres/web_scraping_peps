"""Measure local UI polling with synthetic documents; timings are observations.

No latency threshold depends on the runner. Reports include response bytes,
SQLite statement counts, browser requests and whether unchanged rows survive.
Requires the browser dependencies used by scripts/test_workflow_ui.py.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def source_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
        'peps/app.py', 'peps/store.py', 'peps/engine.py', 'peps/workflow.py',
        'peps/web/app.js', 'peps/web/workflow.js')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--documents', type=int, default=1000)
    parser.add_argument('--entities', type=int, default=20)
    parser.add_argument('--polls', type=int, default=4)
    parser.add_argument('--observe-seconds', '--duration', type=float, default=0)
    parser.add_argument('--tab', choices=('bank', 'sources'), default='bank')
    parser.add_argument('--assert-optimized', action='store_true')
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/ui-polling-validation/validation.json')
    args = parser.parse_args()
    assert args.documents >= args.entities >= 2 and args.polls >= 2
    from playwright.sync_api import sync_playwright
    from peps.app import create_server
    from peps.engine import Engine
    from peps.news import NewsAcquirer
    from peps.workflow import GuidedWorkflow
    from test_workflow_ui import wait_until

    report = {'kind': 'synthetic_local_http_browser_polling', 'checks': {},
              'processed_at': datetime.now(timezone.utc).isoformat(),
              'fixture': {'documents': args.documents, 'entities': args.entities},
              'source_hashes_before': source_hashes(),
              'limits': ['Synthetic closed-mode corpus; no external acquisition.',
                         'Observed timings are not an SLA or pass/fail latency threshold.']}

    def checked(name, condition):
        assert condition, name
        report['checks'][name] = True

    try:
        with tempfile.TemporaryDirectory(prefix='webscope-ui-polling-') as directory:
            engine = Engine(directory)
            entities = [{'id': f'polling-{index}', 'name': f'Compañía Ficticia de Prueba {index}',
                         'kind': 'company'} for index in range(args.entities)]
            engine.store.import_entities(entities)
            marker = 'SYNTHETIC_DOCUMENT_BODY_NEVER_NEEDED_BY_POLLING'
            documents = ({'url': f'https://fixture.invalid/polling/{index}', 'source_kind': 'official',
                'entity_ids': [entities[index % args.entities]['id']], 'title': f'Boletín sintético {index}',
                'text': entities[index % args.entities]['name'] + ' fue acusada de fraude. ' + marker +
                        ' Documento inventado sin relación con personas reales. ' * 8,
                'published_at': '2026-10-10'} for index in range(args.documents))
            engine.import_documents(documents)
            workflow = GuidedWorkflow(engine.store, sources_provider=NewsAcquirer(engine).sources)
            workflow.set_mode('local')
            workflow.confirm_sources()
            engine.rescore()
            workflow.mark_analysis()
            original_audit_count = engine.store.stats()['audit']
            original_connection = engine.store.connection
            statements = []

            @contextmanager
            def traced_connection(*values, **options):
                with original_connection(*values, **options) as connection:
                    connection.set_trace_callback(statements.append)
                    yield connection

            with patch.object(engine.store, 'connection', traced_connection):
                server, _ = create_server(engine)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                address = f'http://127.0.0.1:{server.server_port}'
                try:
                    with urlopen(address, timeout=20) as response:
                        import re
                        token = re.search(r'<meta name="peps-token" content="([^"]+)"',
                                          response.read().decode())[1]
                    headers = {'X-PEPS-Token': token}
                    report['http'] = {}
                    for endpoint in ('stats', 'entities', 'entities?summary=1', 'news?summary=1'):
                        samples, payloads, query_counts = [], [], []
                        last = b''
                        for _ in range(3):
                            statements.clear()
                            started = time.perf_counter()
                            with urlopen(Request(address + '/api/' + endpoint, headers=headers), timeout=30) as response:
                                last = response.read()
                            samples.append((time.perf_counter() - started) * 1000)
                            payloads.append(len(last))
                            query_counts.append(len(statements))
                        report['http'][endpoint] = {'bytes': payloads, 'sqlite_statements': query_counts,
                            'elapsed_ms': samples, 'median_ms': statistics.median(samples)}
                        checked(endpoint.replace('?', '_') + '_does_not_return_document_bodies', marker.encode() not in last)
                        if endpoint == 'entities?summary=1' and args.assert_optimized:
                            rows = json.loads(last)
                            checked('compact_entity_rows_omit_explanation_and_provenance', all(
                                not row.get('score') or not {'explainability', 'provenance'}.intersection(row['score'])
                                for row in rows))
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(executable_path=args.browser_path,
                            headless=True, args=['--no-sandbox'])
                        context = browser.new_context(viewport={'width': 1366, 'height': 768})
                        page = context.new_page()
                        requests, errors, sizes = [], [], []
                        active_requests, maximum_inflight = set(), [0]

                        def record_request(request):
                            requests.append(request.url)
                            if urlsplit(request.url).path in ('/api/stats', '/api/entities'):
                                active_requests.add(request)
                                maximum_inflight[0] = max(maximum_inflight[0], len(active_requests))

                        context.on('request', record_request)
                        context.on('requestfinished', lambda request: active_requests.discard(request))
                        context.on('requestfailed', lambda request: active_requests.discard(request))
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        context.on('response', lambda response: sizes.append((response.url,
                            int(response.headers.get('content-length', 0)))))
                        context.route('**/*', lambda route: route.continue_()
                                      if route.request.url.startswith(address + '/') else route.abort())
                        page.goto(address)
                        page.locator('[data-panel="bank"]').wait_for()
                        page.locator('#entities tr').first.wait_for()
                        if args.tab != 'bank':
                            page.locator('[data-tab="' + args.tab + '"]').click()
                            page.locator('[data-panel="' + args.tab + '"]').wait_for()
                        # A panel becomes visible before its navigation fetches
                        # finish. Exclude those responses from the polling window.
                        page.wait_for_load_state('networkidle')
                        page.evaluate("""()=>{
                          window.pollingRow=document.querySelector('#entities tr');
                          window.pollingRow.focus();window.pollingMutations=0;
                          window.pollingObserver=new MutationObserver(records=>{
                            window.pollingMutations+=records.filter(record=>record.type==='childList').length;
                          });window.pollingObserver.observe(document.querySelector('#entities'),{childList:true,subtree:true});
                        }""")
                        requests.clear()
                        sizes.clear()
                        statements.clear()
                        started = time.perf_counter()
                        deadline = time.monotonic() + 45
                        def observed():
                            return (time.perf_counter() - started >= args.observe_seconds if args.observe_seconds else
                                sum(url.split('?', 1)[0].endswith('/api/stats') for url in requests) >= args.polls)
                        while not observed():
                            assert time.monotonic() < deadline, 'Browser did not perform the expected polling cycles'
                            page.wait_for_timeout(50)
                        # Complete the last async refresh without starting an extra one.
                        page.evaluate('async()=>{if(typeof refreshing!=="undefined"&&refreshing)await refreshing;}')
                        report['browser'] = page.evaluate("""()=>({
                          unchanged_row_retained:window.pollingRow===document.querySelector('#entities tr'),
                          keyboard_focus_retained:document.querySelector('[data-panel="bank"]').hidden?null:document.activeElement===window.pollingRow,
                          entity_childlist_mutations:window.pollingMutations,
                          horizontal_overflow:document.documentElement.scrollWidth>innerWidth
                        })""")
                        report['browser'].update(elapsed_ms=(time.perf_counter() - started) * 1000,
                            tab=args.tab,
                            request_counts=dict(Counter(url.removeprefix(address) for url in requests)),
                            response_bytes=sum(size for _, size in sizes),
                            response_bytes_by_path={path: sum(size for url, size in sizes if urlsplit(url).path == path)
                                for path in {urlsplit(url).path for url, _ in sizes}},
                            sqlite_statements=len(statements))
                        checked('browser_polling_stays_on_loopback_and_has_no_script_errors',
                                not errors and all(url.startswith(address + '/') for url in requests))
                        if args.tab == 'bank':
                            checked('unchanged_rows_and_keyboard_focus_survive_polling',
                                    report['browser']['unchanged_row_retained'] and report['browser']['keyboard_focus_retained'])
                        if args.assert_optimized and args.tab != 'bank':
                            checked('inactive_results_tab_never_fetches_entities_during_polling',
                                    not any('/api/entities' in url for url in requests))
                        checked('desktop_bank_has_no_horizontal_overflow', not report['browser']['horizontal_overflow'])
                        if args.assert_optimized:
                            page.locator('[data-tab="bank"]').click()
                            page.locator('[data-panel="bank"]').wait_for()
                            page.evaluate("""async()=>{
                              if(typeof refreshing!=='undefined'&&refreshing)await refreshing;
                              window.pollingRow=document.querySelector('#entities tr');window.pollingRow.focus();
                              await refresh();await refresh();
                            }""")
                            checked('unchanged_results_keep_the_same_dom_row_and_keyboard_focus', page.evaluate(
                                "window.pollingRow===document.querySelector('#entities tr')&&document.activeElement===window.pollingRow"))
                            report['browser']['visible_results_row_and_focus_retained'] = True
                            held_routes = []
                            old_query, new_query = 'Prueba 0', f'Prueba {args.entities - 1}'

                            def hold_old_search(route):
                                query = parse_qs(urlsplit(route.request.url).query).get('q', [''])[0]
                                if query == old_query and not held_routes:
                                    held_routes.append(route)
                                else:
                                    route.continue_()

                            context.route('**/api/entities*', hold_old_search)
                            page.locator('#search').fill(old_query)
                            wait_until(page, lambda: bool(held_routes), 'The controlled old search did not start')
                            page.locator('#search').fill(new_query)
                            page.wait_for_timeout(400)  # Let the UI's search debounce request its refresh.
                            held_routes[0].continue_()
                            wait_until(page, lambda: page.locator('#entities tr').count() == 1 and
                                new_query in page.locator('#entities tr').first.inner_text(),
                                'A late old response replaced the latest search', timeout=30)
                            checked('latest_search_wins_after_a_controlled_late_response', True)
                            report['browser']['maximum_stats_or_entities_requests_inflight'] = maximum_inflight[0]
                            checked('stats_and_entities_refreshes_do_not_overlap', maximum_inflight[0] <= 1)
                            # A second page owns all its timers from before load.
                            # Retain a real stats response until the UI is closed,
                            # then advance browser time to expose orphaned polls.
                            page.close()
                            page = context.new_page()
                            page.on('pageerror', lambda error: errors.append(str(error)))
                            page.clock.install()
                            page.goto(address)
                            page.wait_for_function('Boolean(lastStats.workflow)')
                            saved_stats = page.evaluate('lastStats')
                            held_stats = []
                            page.route('**/api/stats', lambda route: held_stats.append(route))
                            page.evaluate('void refresh()')
                            wait_until(page, lambda: bool(held_stats), 'The controlled stats response did not start')
                            page.locator('#shutdown').click()
                            page.get_by_text('WebScope se cerró. Puedes cerrar esta pestaña.', exact=True).wait_for()
                            requests.clear()
                            held_stats[0].fulfill(status=200, content_type='application/json', body=json.dumps(saved_stats))
                            page.evaluate('async()=>{if(refreshing)await refreshing;}')
                            page.clock.fast_forward(30000)
                            checked('shutdown_tolerates_a_late_stats_response_without_page_errors', not errors)
                            checked('shutdown_stops_polling_even_after_thirty_seconds_of_browser_time',
                                    page.evaluate('pollStopped===true&&refreshing===null') and not requests)
                            report['shutdown'] = {'controlled_late_response': True, 'browser_seconds_advanced': 30,
                                                  'requests_after_close': list(requests), 'page_errors': list(errors)}
                        browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
            checked('polling_preserves_all_data_and_audit_integrity', engine.store.stats()['documents'] == args.documents and
                    engine.store.stats()['entities'] == args.entities and engine.store.verify_audit()['ok'])
            checked('read_only_polling_does_not_append_audit_events', engine.store.stats()['audit'] == original_audit_count)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        report['source_hashes_after'] = source_hashes()
        report['source_unchanged_during_measurement'] = report['source_hashes_before'] == report['source_hashes_after']
        report['passed'] = 'error' not in report and bool(report['checks']) and all(report['checks'].values())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'output': str(args.output)}))


if __name__ == '__main__':
    main()
