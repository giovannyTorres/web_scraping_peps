"""Real browser integration checks with synthetic, local-only documents.

Optional developer dependency: playwright==1.62.0; no model or browser download
occurs here. Install Chromium separately or pass --browser-path. Run from any
folder: python scripts/test_explainability_ui.py [--browser-path /path/to/chromium] [--output report.json]
Screenshots are written under ignored runtime/ui-validation. The JSON report
measures UI behavior and one local render latency; it is not model-quality evidence.
"""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import shutil
from pathlib import Path
import sys
import tempfile
import threading
import time
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from peps.engine import Engine
from peps.app import create_server
from peps.news import NewsAcquirer
from peps.workflow import GuidedWorkflow

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--browser-path',default=shutil.which('chromium') or shutil.which('chromium-browser'))
parser.add_argument('--output',type=Path,help='Optional report destination; defaults to runtime/ui-validation/validation.json')
args=parser.parse_args()
artifacts=root/'runtime'/'ui-validation'
artifacts.mkdir(parents=True,exist_ok=True)
results={'kind':'synthetic_offline_browser_validation','processed_at':datetime.now(timezone.utc).isoformat(),'browser':'Chromium','playwright_version':importlib.metadata.version('playwright'),'fixtures':'211 synthetic documents, 4 synthetic entities; no real notes','checks':{},'screenshots':[]}
with tempfile.TemporaryDirectory() as tmp:
    engine=Engine(tmp)
    engine.store.import_entities([{'id':'aurora','name':'Entidad Sintética Aurora','kind':'company'}, {'id':'low','name':'Empresa Sintética Serena','kind':'company'}, {'id':'emoji','name':'Empresa Sintética Águila','kind':'company'}, {'id':'identity','name':'Empresa Sintética Identidad','kind':'company'}])
    now=time.strftime('%Y-%m-%d',time.gmtime())
    engine.import_documents([
        {'url':'https://fixture.invalid/a','title':'<img src=x onerror="window.injected=1"> · Documento sintético','source_kind':'official','entity_ids':['aurora'],'text':'🦅 Boletín sintético de prueba. '+'Información introductoria. '*100+'Entidad Sintética Aurora fue condenada por fraude.','published_at':now},
        {'url':'https://fixture.invalid/b','title':'Condena sintética de prueba','source_kind':'official','entity_ids':['aurora'],'text':'Entidad Sintética Aurora fue condenada por corrupción.','published_at':now},
        {'url':'https://fixture.invalid/c','title':'Documento sintético de prueba','source_kind':'official','entity_ids':['aurora'],'text':'Entidad Sintética Aurora fue condenada por lavado de dinero.','published_at':now},
        {'url':'https://fixture.invalid/low','title':'Boletín sin señales','source_kind':'official','entity_ids':['low'],'text':'Empresa Sintética Serena abrió una sucursal en Puebla.','published_at':now},
        {'url':'https://fixture.invalid/emoji','title':'Texto Unicode','source_kind':'official','entity_ids':['emoji'],'text':'🦅 Empresa Sintética Águila fue acusada de fraude.','published_at':now},
        {'url':'https://fixture.invalid/identity','title':'Selección de identidad sintética','source_kind':'news','entity_ids':['identity'],'text':'Empresa Sintética Identidad fue acusada de fraude.','published_at':now}
    ])
    engine.import_documents([{'url':f'https://fixture.invalid/bulk-{i}','title':f'Nota incidental sintética {i}','source_kind':'official','entity_ids':['aurora'],'text':f'Comunicado sintético número {i}, sin mención nominal de la entidad.','published_at':now} for i in range(205)])
    engine.rescore()
    workflow=GuidedWorkflow(engine.store,sources_provider=NewsAcquirer(engine).sources)
    workflow.set_mode('local')
    workflow.confirm_sources()
    workflow.mark_analysis()
    server,token=create_server(engine)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    address=f'http://127.0.0.1:{server.server_port}'
    try:
      with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=args.browser_path,headless=True,args=['--no-sandbox'])
        results['browser_version']=browser.version
        page=browser.new_page(viewport={'width':1512,'height':1100},device_scale_factor=1)
        page_errors=[];page.on('pageerror',lambda e:page_errors.append(str(e)))
        network=[];page.on('request',lambda r:network.append(r.url))
        page.goto(address)
        started=time.perf_counter();page.get_by_role('row',name='Consultar Entidad Sintética Aurora').click()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        results['entity_open_latency_ms']=(time.perf_counter()-started)*1000
        assert page.locator('.score-number strong').inner_text()==str(engine.explanation('aurora')['result']['score'])
        results['checks']['actual_score_and_scale']=True
        assert page.locator('.score-number strong').inner_text()!='0'
        assert page.locator('.score-factor').count()>=3
        results['checks']['real_factors_and_nonadditive_delta']=page.get_by_text('Estos cambios no se suman',exact=False).count()==1
        fraud=page.locator('.score-factor').filter(has_text='por fraude.').first
        fraud.locator('.text-button').first.click()
        page.locator('.note[open] .note-body mark.active-evidence').first.wait_for()
        marks=page.locator('.note[open] .note-body mark.active-evidence')
        quote=fraud.locator('.factor-citation blockquote').first.inner_text()
        assert ''.join(marks.all_inner_texts())==quote
        text=page.locator('.note[open] .note-body').inner_text()
        assert text.startswith('🦅') and len(text)>1800 and 'fue condenada por fraude' in quote
        results['checks']['unicode_exact_citation_after_1800_chars']=True
        opened_id=page.locator('.note[open]').first.get_attribute('data-evidence-id')
        assert opened_id not in {item['id'] for item in engine.store.evidence('aurora',200)}
        results['checks']['citation_loads_evidence_beyond_200_notes']=True
        assert page.locator('#detail img').count()==0 and page.evaluate('window.injected') is None
        assert '<img src=x' in page.locator('.note[open] h4').first.inner_text()
        results['checks']['xss_rendered_as_plain_text']=True
        page.locator('.note[open] mark.active-evidence').first.evaluate("element=>element.scrollIntoView({block:'center',behavior:'instant'})")
        page.screenshot(path=str(artifacts/'desktop-citation.png'),full_page=False);results['screenshots'].append('runtime/ui-validation/desktop-citation.png')
        page.locator('.contextual-section>summary').click()
        assert page.get_by_text('Estas hipótesis no modifican el score',exact=False).count()==1
        results['checks']['auxiliary_separate_from_factors']=True
        page.locator('.scoring-technical>summary').click()
        page.get_by_role('button',name='Reproducir cálculo conservado').click()
        page.get_by_text('El cálculo y la traza coinciden con la evaluación conservada.',exact=True).wait_for()
        results['checks']['replay_matches']=True
        page.locator('#detail').evaluate("element=>{element.scrollTo({top:0,behavior:'instant'});window.scrollTo({top:element.getBoundingClientRect().top+window.scrollY-20,behavior:'instant'})}")
        heading_box=page.locator('.explanation-title').bounding_box()
        assert 0<heading_box['y']<700
        results['checks']['desktop_explanation_heading_visible']=True
        page.screenshot(path=str(artifacts/'desktop-explanation.png'),full_page=False);results['screenshots'].append('runtime/ui-validation/desktop-explanation.png')
        previous_run=engine.explanation('aurora')['run']['id']
        note=page.locator('.note[open]').first
        note.get_by_label('Estado de revisión',exact=True).select_option('confirmed')
        note.get_by_label('Revisor',exact=True).fill('revisor-sintético')
        note.get_by_label('Motivo y comprobación de identidad',exact=True).fill('Prueba sintética de formulario y trazabilidad.')
        note.get_by_role('button',name='Guardar revisión',exact=True).click()
        page.get_by_text('Revisión guardada. Se creó una evaluación',exact=False).wait_for()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        assert engine.explanation('aurora')['run']['id']!=previous_run
        results['checks']['current_review_creates_new_evaluation']=True
        page.get_by_label('Evaluación consultada',exact=True).select_option(previous_run)
        page.get_by_text('Se muestran el texto, la identidad y la revisión conservados',exact=False).wait_for()
        page.locator('.score-factor .text-button').first.click()
        page.locator('.note[open] .note-body').wait_for()
        assert page.get_by_role('button',name='Guardar revisión',exact=True).count()==0
        assert page.locator('.note[open]').get_by_text('Revisión conservada en el histórico.',exact=False).count()==1
        results['checks']['historical_notes_readonly']=True
        page.get_by_label('Evaluación consultada',exact=True).select_option('')
        page.get_by_text('Evaluación actual · Procesada:',exact=False).wait_for()
        results['checks']['return_to_current']=True
        page.locator('.score-factor .text-button').first.click()
        stale_note=page.locator('.note[open]').first
        stale_id=stale_note.get_attribute('data-evidence-id')
        engine.store.review(stale_id,'pending','other-session','Cambio sintético desde otra sesión.')
        engine.explanation('aurora')
        workflow.mark_analysis()
        stale_note.get_by_label('Estado de revisión',exact=True).select_option('rejected')
        stale_note.get_by_label('Revisor',exact=True).fill('stale-ui-reviewer')
        stale_note.get_by_label('Motivo y comprobación de identidad',exact=True).fill('Esta revisión obsoleta no debe persistirse.')
        stale_note.get_by_role('button',name='Guardar revisión',exact=True).click()
        page.get_by_text('La evaluación cambió. Pulsa «Actualizar evaluación»',exact=False).wait_for()
        current_note=next(note for note in engine.store.evidence('aurora') if note['id']==stale_id)
        assert current_note['review']['actor']=='other-session'
        results['checks']['stale_review_rejected_without_overwriting']=True
        page.get_by_role('button',name='Actualizar evaluación',exact=True).click()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        page.get_by_role('row',name='Consultar Empresa Sintética Serena').focus()
        page.keyboard.press('Enter')
        page.get_by_role('heading',name='Empresa Sintética Serena',exact=True).wait_for()
        page.get_by_text('No hay eventos con aportación puntuable.',exact=False).wait_for()
        assert page.locator('.score-number strong').inner_text()=='0'
        results['checks']['zero_is_insufficient_not_risk_absence']=True
        page.get_by_role('row',name='Consultar Empresa Sintética Águila').click()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        page.locator('.score-factor .text-button').first.click()
        assert page.locator('.note[open] .note-body').inner_text().startswith('🦅')
        assert page.locator('.note[open] .note-body mark').first.inner_text()==page.locator('.score-factor blockquote').first.inner_text()
        results['checks']['unicode_astral_offsets']=True
        page.get_by_role('row',name='Consultar Empresa Sintética Identidad').click()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        page.locator('.note>summary').first.click()
        identity_note=page.locator('.note[open]').first
        checkbox=identity_note.locator('.identity-span').first
        checkbox.wait_for()
        assert checkbox.is_checked()
        checkbox.uncheck()
        identity_note.get_by_label('Estado de revisión',exact=True).select_option('confirmed')
        identity_note.get_by_label('Revisor',exact=True).fill('revisor-identidad-sintético')
        identity_note.get_by_label('Motivo y comprobación de identidad',exact=True).fill('Sin menciones confirmadas para esta entidad sintética.')
        identity_note.get_by_role('button',name='Guardar revisión',exact=True).click()
        page.get_by_text('Revisión guardada. Se creó una evaluación',exact=False).wait_for()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        page.locator('.note>summary').first.click()
        restored_checkbox=page.locator('.note[open] .identity-span').first
        restored_checkbox.wait_for()
        assert not restored_checkbox.is_checked()
        assert engine.store.evidence('identity')[0]['review']['identity_spans']==[]
        assert page.locator('.score-number strong').inner_text()=='—'
        assert 'Identidad pendiente' in page.locator('.score-summary').inner_text()
        results['checks']['explicit_empty_identity_selection_stays_unchecked_after_save']=True
        original_latest_score_run=engine.store.latest_score_run
        def delayed_latest_score_run(entity_id):
            if entity_id=='low':time.sleep(.25)
            return original_latest_score_run(entity_id)
        engine.store.latest_score_run=delayed_latest_score_run
        page.evaluate("Promise.all([showDetail({id:'low',name:'Serena'}),showDetail({id:'emoji',name:'Águila'})])")
        assert page.locator('#detail>h2').inner_text()=='Empresa Sintética Águila'
        results['checks']['entity_selection_race_does_not_replace_latest']=True
        engine.store.latest_score_run=original_latest_score_run
        page.get_by_role('row',name='Consultar Entidad Sintética Aurora').click()
        page.get_by_role('heading',name='¿Por qué obtuvo este score?').wait_for()
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        page.locator('.explanation-title').evaluate("element=>element.scrollIntoView({block:'start'})")
        page.screenshot(path=str(artifacts/'mobile.png'),full_page=False);results['screenshots'].append('runtime/ui-validation/mobile.png')
        results['checks']['mobile_no_horizontal_page_overflow']=True
        assert not [url for url in network if not url.startswith(address)]
        results['checks']['no_external_requests']=True
        assert not page_errors,page_errors
        results['checks']['no_browser_errors']=True
        results['page_errors']=page_errors
        browser.close()
    finally:
      server.shutdown();server.server_close();thread.join()
results['passed']=all(results['checks'].values())
print(json.dumps(results,ensure_ascii=False,indent=2))
output=args.output or artifacts/'validation.json'
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
