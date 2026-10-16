"""Browser validation of individual names and reviewed CSV import, entirely local.

Developer dependency: playwright==1.62.0 with Chromium installed separately.
Run: python scripts/test_import_ui.py [--browser-path /path/to/chromium] [--output report.json]
Only fictional names are used. Screenshots go to ignored runtime/import-ui-validation.
The runtime JSON reports measured UI checks and timings, not NLP quality.
"""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from peps.app import create_server
from peps.engine import Engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--browser-path', default=shutil.which('chromium') or shutil.which('chromium-browser'))
    parser.add_argument('--output', type=Path, help='Optional report destination; defaults to runtime/import-ui-validation/validation.json')
    args = parser.parse_args()
    artifacts = ROOT / 'runtime' / 'import-ui-validation'
    artifacts.mkdir(parents=True, exist_ok=True)
    report = {
        'kind': 'synthetic_offline_import_browser_validation',
        'processed_at': datetime.now(timezone.utc).isoformat(),
        'playwright_version': importlib.metadata.version('playwright'),
        'fixtures': 'Fictional single name, CSV with duplicate/blank headings, custom entity kinds, invalid row, CP1252 and UTF-16 files',
        'checks': {}, 'timings_ms': {}, 'screenshots': [],
    }

    def checked(name, condition=True):
        assert condition, name
        report['checks'][name] = True

    def analyze_local_bank(page):
        """The browser workflow requires an explicit analysis after every import."""
        if not page.locator('[data-panel="sources"]').is_visible():
            if page.locator('#workflow-mobile-step').is_visible():
                page.locator('#workflow-mobile-step').select_option('sources')
            else:
                page.locator('[data-workflow-step="sources"]').click()
        page.locator('[data-panel="sources"]').wait_for()
        page.locator('#workflow-auto-analyze').uncheck()
        page.locator('#workflow-mode-local').click()
        page.locator('#workflow-sources-next').click()
        page.locator('[data-panel="news"]').wait_for()
        page.locator('#news-acquire').click()
        page.locator('[data-panel="analysis"]').wait_for()
        page.locator('#workflow-analyze').click()
        page.locator('[data-panel="bank"]').wait_for()
        page.locator('#search').fill('')

    with tempfile.TemporaryDirectory() as tmp:
        engine = Engine(tmp)
        server, _ = create_server(engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        address = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(executable_path=args.browser_path, headless=True, args=['--no-sandbox'])
                report['browser_version'] = browser.version
                page = browser.new_page(viewport={'width': 1512, 'height': 1100}, device_scale_factor=1)
                page_errors, network = [], []
                page.on('pageerror', lambda exc: page_errors.append(str(exc)))
                page.on('request', lambda request: network.append(request.url))
                page.goto(address)
                page.locator('[data-panel="import"]').wait_for()
                checked('empty_workflow_starts_with_name_and_csv_entry_points',
                        page.locator('#import-single-tab').is_visible() and page.locator('#import-csv-tab').is_visible())
                page.locator('#single-name').fill('Persona Sintética Estrella')
                page.locator('#single-save').click()
                checked('individual_type_is_required_without_silent_person_default',
                        not page.locator('#single-kind').evaluate('element=>element.checkValidity()') and
                        engine.store.stats()['entities'] == 0)
                page.locator('#single-kind').select_option('person')
                page.locator('#single-aliases').fill('Estrella de prueba | E. Sintética')
                page.locator('#import-single-panel details summary').click()
                page.locator('#single-city').fill('Toluca')
                page.locator('#single-role').fill('Docente sintética')
                start = time.perf_counter()
                page.locator('#single-save').click()
                page.locator('[data-panel="sources"]').wait_for()
                report['timings_ms']['single_save_to_source_step'] = (time.perf_counter() - start) * 1000
                entity = next(engine.store.iter_entities())
                checked('single_identity_hints_and_aliases_persist',
                        entity['context']['city'] == 'Toluca' and entity['context']['role'] == 'Docente sintética' and
                        entity['aliases'] == ['Estrella de prueba', 'E. Sintética'])
                checked('individual_save_preserves_explicit_analysis_step',
                        engine.store.latest_score_run(entity['id']) is None and engine.store.get_score(entity['id']) is None)
                analyze_local_bank(page)
                page.get_by_role('row', name='Consultar Persona Sintética Estrella', exact=True).click()
                page.get_by_role('heading', name='¿Por qué obtuvo este score?').wait_for()
                checked('explicit_analysis_opens_real_auditable_explanation',
                        page.locator('.score-number strong').inner_text() == '—' and
                        engine.store.get_score(entity['id'])['score'] == 0 and
                        'No hay evidencia atribuida' in page.locator('#detail').inner_text() and
                        'esto no acredita ausencia de riesgo' in page.locator('#detail').inner_text())

                page.locator('.toolbar [data-import-open="csv"]').click()
                cp1252_text = 'Nombre;Ciudad\r\nPersona Sintética José;Mérida\r\n'
                page.locator('#csv-file').set_input_files({
                    'name': 'lista-sintetica-cp1252.csv', 'mimeType': 'text/csv',
                    'buffer': cp1252_text.encode('cp1252')})
                page.locator('#csv-read').click()
                page.locator('#csv-mapping-section').wait_for()
                checked('windows1252_upload_decodes_accents', 'José' in page.locator('#csv-mapping').inner_text())
                # A response started before a text edit must never restore an old preview.
                def stale_preview(route):
                    response = route.fetch()
                    page.evaluate("""()=>{const text=document.getElementById('csv-text');
                      text.value='Nombre;Ciudad\\nPersona Sintética Nueva;Toluca';
                      text.dispatchEvent(new Event('input',{bubbles:true}));}""")
                    route.fulfill(response=response)
                page.route('**/api/import-preview', stale_preview)
                page.locator('#csv-read').click()
                expect(page.locator('#csv-read')).to_be_enabled()
                checked('late_preview_cannot_restore_source_edited_during_request',
                        page.locator('#csv-mapping-section').is_hidden() and page.locator('#csv-preview-section').is_hidden())
                page.unroute('**/api/import-preview', stale_preview)
                utf16_text = 'Nombre\tCiudad\r\nPersona Sintética Águila\tMérida\r\n'
                page.locator('#csv-file').set_input_files({
                    'name': 'lista-sintetica-utf16.tsv', 'mimeType': 'text/tab-separated-values',
                    'buffer': utf16_text.encode('utf-16')})
                page.locator('#csv-read').click()
                page.locator('#csv-mapping-section').wait_for()
                expect(page.locator('#csv-mapping')).to_contain_text('Águila')
                checked('utf16_upload_and_tab_detection_preserve_unicode')

                csv_text = ('Reporte sintético local\r\n'
                            'Código;Etiqueta;Etiqueta;;Clase;Dato libre\r\n'
                            'csv-brisa;Empresa Sintética Brisa;Brisa|E. Brisa;Toluca;Sociedad;<img src=x onerror=window.injected=1>\r\n'
                            'csv-jose;Persona Sintética José;José de Prueba;Puebla;Individuo;no almacenar\r\n'
                            'csv-invalida;;Sin nombre;Mérida;Individuo;no almacenar\r\n')
                page.locator('#csv-file').set_input_files({
                    'name': 'lista-sintetica-irregular.csv', 'mimeType': 'text/csv', 'buffer': csv_text.encode('utf-8')})
                # Reading controls are inside a details element, so open it before editing.
                reading = page.locator('#csv-encoding').locator('xpath=ancestor::details')
                if not reading.evaluate('element=>element.open'):
                    reading.locator('summary').click()
                page.locator('#csv-skip-rows').fill('1')
                page.locator('#csv-delimiter').select_option(';')
                start = time.perf_counter()
                page.locator('#csv-read').click()
                page.locator('#csv-mapping-section').wait_for()
                expect(page.locator('#csv-mapping select')).to_have_count(6)
                report['timings_ms']['csv_parse_to_mapping'] = (time.perf_counter() - start) * 1000
                checked('duplicate_and_blank_headers_get_distinct_mapping_controls')
                for index, field in enumerate(['id', 'name', 'aliases', 'city', 'kind', 'ignore']):
                    page.locator('#csv-mapping select').nth(index).select_option(field)
                checked('mapping_requires_user_review', not page.locator('#csv-mapping-reviewed').is_checked())
                checked('uploaded_cells_render_as_text_without_html_execution',
                        page.locator('#csv-mapping img').count() == 0 and page.evaluate('window.injected') is None)
                page.locator('#csv-mapping-reviewed').check()
                page.locator('#csv-validate').click()
                page.locator('#csv-kind-section').wait_for()
                checked('unknown_type_values_explicitly_prompt_for_user_identification',
                        page.locator('#csv-kind-mapping select').count() == 2)
                for select in page.locator('#csv-kind-mapping select').all():
                    label_text = select.locator('xpath=..').inner_text()
                    select.select_option('company' if 'Sociedad' in label_text else 'person')
                page.locator('#csv-mapping-reviewed').check()
                page.locator('#csv-validate').click()
                page.locator('#csv-preview-section').wait_for()
                checked('invalid_row_blocks_confirmation', page.locator('#csv-confirm').is_disabled())
                page.locator('#csv-skip-invalid').check()
                page.locator('#csv-mapping-reviewed').check()
                page.locator('#csv-validate').click()
                page.locator('#csv-preview-section').wait_for()
                expect(page.locator('#csv-preview')).to_contain_text('Empresa Sintética Brisa')
                checked('preview_exposes_names_kinds_and_invalid_row',
                        'Persona Sintética José' in page.locator('#csv-preview').inner_text())
                page.locator('#csv-confirm-reviewed').check()
                expect(page.locator('#csv-confirm')).to_be_enabled()

                page.locator('#csv-mapping select').nth(3).select_option('state')
                checked('changing_column_mapping_invalidates_confirmation',
                        page.locator('#csv-confirm').is_disabled() and not page.locator('#csv-confirm-reviewed').is_checked())
                page.locator('#csv-mapping select').nth(3).select_option('city')
                page.locator('#csv-mapping-reviewed').check()
                page.locator('#csv-validate').click()
                page.locator('#csv-kind-section').wait_for()
                for select in page.locator('#csv-kind-mapping select').all():
                    label_text = select.locator('xpath=..').inner_text()
                    select.select_option('company' if 'Sociedad' in label_text else 'person')
                page.locator('#csv-mapping-reviewed').check()
                page.locator('#csv-validate').click()
                page.locator('#csv-preview-section').wait_for()
                page.locator('#csv-confirm-reviewed').check()
                page.locator('#csv-preview-section').screenshot(path=str(artifacts / 'desktop-preview.png'))
                report['screenshots'].append('runtime/import-ui-validation/desktop-preview.png')
                start = time.perf_counter()
                confirmations_before = len([url for url in network if url.endswith('/api/import-confirm')])
                page.locator('#csv-confirm').evaluate('element=>{element.click();element.click();}')
                deadline = time.monotonic() + 10
                while engine.store.stats()['entities'] != 3 and time.monotonic() < deadline:
                    page.wait_for_timeout(10)
                assert engine.store.stats()['entities'] == 3
                report['timings_ms']['csv_confirmation_to_persisted_entities'] = (time.perf_counter() - start) * 1000
                brisa, jose = engine.store.get_entity('csv-brisa'), engine.store.get_entity('csv-jose')
                checked('csv_custom_kind_values_and_optional_identity_hints_saved',
                        brisa['kind'] == 'company' and brisa['context']['city'] == 'Toluca' and
                        jose['kind'] == 'person' and jose['context']['city'] == 'Puebla')
                checked('invalid_row_omitted_only_after_explicit_choice', engine.store.get_entity('csv-invalida') is None)
                checked('ignored_column_is_not_persisted',
                        'no almacenar' not in json.dumps(list(engine.store.iter_entities()), ensure_ascii=False) and
                        '<img' not in json.dumps(list(engine.store.iter_entities()), ensure_ascii=False))

                page.locator('[data-panel="sources"]').wait_for()
                checked('csv_save_does_not_run_implicit_scoring', engine.store.latest_score_run('csv-brisa') is None)
                analyze_local_bank(page)
                page.get_by_role('row', name='Consultar Empresa Sintética Brisa', exact=True).wait_for()
                checked('explicit_analysis_reveals_imported_list_without_previous_filter',
                        page.locator('#search').input_value() == '')
                page.get_by_role('row', name='Consultar Empresa Sintética Brisa', exact=True).click()
                page.get_by_role('heading', name='¿Por qué obtuvo este score?').wait_for()
                saved = engine.store.latest_score_run('csv-brisa')
                shown = saved['result']['explainability']['assessment']['display_score']
                checked('analyzed_imported_list_links_to_existing_score_audit',
                        page.locator('.score-number strong').inner_text() == ('—' if shown is None else str(shown)) and
                        saved['entity_snapshot']['name'] in page.locator('#detail').inner_text())
                checked('double_confirmation_click_submits_one_operation',
                        len([url for url in network if url.endswith('/api/import-confirm')]) == confirmations_before + 1)

                page.locator('.toolbar [data-import-open="csv"]').click()
                page.set_viewport_size({'width': 390, 'height': 844})
                checked('mobile_import_has_no_horizontal_page_overflow',
                        page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))
                page.locator('#csv-mapping-section').evaluate("element=>element.scrollIntoView({block:'start',behavior:'instant'})")
                page.screenshot(path=str(artifacts / 'mobile-mapping.png'), full_page=False)
                report['screenshots'].append('runtime/import-ui-validation/mobile-mapping.png')
                page.locator('#csv-clear').click()
                checked('clear_import_releases_file_and_preview_after_receipt',
                        page.locator('#csv-text').input_value() == '' and
                        page.locator('#csv-file').evaluate('element=>element.files.length') == 0 and
                        page.locator('#csv-mapping-section').is_hidden() and page.locator('#csv-preview-section').is_hidden())
                page.locator('#csv-paste-details summary').click()
                page.locator('#csv-text').fill('Persona Sintética\tLuna\tPrueba\n')
                page.locator('#csv-skip-rows').fill('0')
                page.locator('#csv-header').uncheck()
                page.locator('#csv-delimiter').select_option('tab')
                page.locator('#csv-read').click()
                page.locator('#csv-mapping-section').wait_for()
                for index, field in enumerate(['given_name', 'first_surname', 'second_surname']):
                    page.locator('#csv-mapping select').nth(index).select_option(field)
                page.locator('#csv-default-kind').select_option('person')
                page.locator('#csv-mapping-reviewed').check()
                page.locator('#csv-validate').click()
                expect(page.locator('#csv-preview')).to_contain_text('Persona Sintética Luna Prueba')
                checked('headerless_split_names_compose_full_name_from_user_mapping')
                page.locator('#csv-confirm-reviewed').check()
                expect(page.locator('#csv-confirm')).to_be_enabled()

                # A missing receipt is terminal tracking loss, not a broken connection.
                # Simulate the documented TTL/receipt eviction response without waiting 30 minutes.
                def missing_receipt(route):
                    route.fulfill(status=404, content_type='application/json', body=json.dumps({
                        'error': 'Seguimiento no disponible. Consulte el banco para comprobar si los datos ya se guardaron.'}))
                page.route('**/api/import-status?id=*', missing_receipt)
                page.locator('#import-single-tab').click()
                page.locator('#single-name').fill('Persona Sintética Comprobante')
                page.locator('#single-kind').select_option('person')
                confirmations_before = len([url for url in network if url.endswith('/api/import-confirm')])
                tracking_before = len([url for url in network if '/api/import-status?' in url])
                page.locator('#single-save').click()
                expect(page.locator('#single-status')).to_contain_text('El comprobante ya no está disponible.')
                checked('missing_receipt_unlocks_inputs_with_honest_uncertainty',
                        page.locator('#single-name').is_enabled() and page.locator('#single-save').is_enabled() and
                        'los datos podrían haberse guardado' in page.locator('#single-status').inner_text())
                checked('missing_receipt_invalidates_previous_csv_confirmation',
                        page.locator('#csv-confirm').is_disabled() and page.locator('#csv-confirmation').is_hidden() and
                        not page.locator('#csv-confirm-reviewed').is_checked())
                page.wait_for_timeout(100)
                checked('missing_receipt_never_auto_reimports_or_retries_tracking',
                        len([url for url in network if url.endswith('/api/import-confirm')]) == confirmations_before + 1 and
                        len([url for url in network if '/api/import-status?' in url]) == tracking_before + 1 and
                        page.get_by_role('button', name='Consultar estado de la importación', exact=True).count() == 0)
                deadline = time.monotonic() + 10
                while engine.store.stats()['entities'] != 4 and time.monotonic() < deadline:
                    page.wait_for_timeout(10)
                assert engine.store.stats()['entities'] == 4
                analyze_local_bank(page)
                page.get_by_role('row', name='Consultar Persona Sintética Comprobante', exact=True).wait_for()
                checked('missing_receipt_explicit_workflow_can_verify_saved_entities', page.locator('#search').input_value() == '')
                page.unroute('**/api/import-status?id=*', missing_receipt)
                checked('no_external_requests', not [url for url in network if not url.startswith(address)])
                checked('no_browser_errors', not page_errors)
                checked('import_preserves_audit_chain', engine.store.verify_audit()['ok'])
                report['page_errors'] = page_errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    report['passed'] = all(report['checks'].values())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    output = args.output or artifacts / 'validation.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding='utf-8')
    print(rendered)


if __name__ == '__main__':
    main()
