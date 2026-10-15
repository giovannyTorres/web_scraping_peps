'use strict';
// Navigation and operation state are derived from the local server, never from a timer.
window.pepsWorkflow = (() => {
  const $ = id => document.getElementById(id);
  const steps = [
    {id:'data',tab:'import',title:'Carga tu lista de entidades',description:'Agrega un nombre o una lista. Revisa los datos antes de continuar.'},
    {id:'sources',tab:'sources',title:'Prepara tus fuentes de información',description:'Elige fuentes públicas autorizadas o incorpora documentos de tu equipo.'},
    {id:'download',tab:'news',title:'Conserva la información que vas a revisar',description:'Descarga noticias de las fuentes preparadas o continúa con tus documentos locales.'},
    {id:'analysis',tab:'analysis',title:'Relaciona la evidencia y calcula el score',description:'El análisis se realiza en tu equipo y conserva las razones de cada resultado.'},
    {id:'results',tab:'bank',title:'Revisa los resultados con contexto',description:'Consulta la evidencia original y comprueba las coincidencias antes de tomar decisiones.'}
  ];
  const labels = {complete:'Completado',ready:'Listo para continuar',blocked:'Pendiente',running:'En curso',paused:'En pausa',error:'Requiere atención'};
  const state = {hooks:null,workflow:null,operation:{},current:'import',initialized:false,first:true,requesting:false,observedOperation:null,finishedOperation:null};
  function stepFor(tab) { return steps.find(step=>step.tab===tab); }
  function serverStep(tab) { const step=stepFor(tab);return state.workflow?.steps?.find(item=>item.id===step?.id); }
  function action(name) { return state.workflow?.actions?.[name] || {enabled:false,reason:'Consultando el estado del proceso…'}; }
  function busy() { return !!state.operation.busy || state.requesting; }
  function canOpenTab(tab,announce=false) {
    if(tab==='audit')return true;
    const step=serverStep(tab);
    const confirmed=state.workflow?.sources_confirmed;
    const allowed=step ? step.status!=='blocked'&&(tab!=='news'||confirmed) : tab==='import';
    if(!allowed&&announce)state.hooks.message(step?.reason||'Completa el paso anterior para continuar.',true);
    return allowed;
  }
  function navigate(tab) { return state.hooks.switchTab(tab); }
  function setText(id,text) { if($(id))$(id).textContent=text||''; }
  function setButton(id,enabled,reason,reasonId) {
    const button=$(id);if(!button)return;
    button.disabled=!enabled;
    if(reasonId) { setText(reasonId,reason);button.setAttribute('aria-describedby',reasonId); }
    if(!enabled&&reason)button.title=reason;else button.removeAttribute('title');
  }
  function formatTime(value) {
    const seconds=Math.max(0,Math.round(Number(value)));
    if(seconds<60)return seconds+' s';
    if(seconds<3600)return Math.floor(seconds/60)+' min'+(seconds%60?' '+seconds%60+' s':'');
    return Math.floor(seconds/3600)+' h '+Math.floor(seconds%3600/60)+' min';
  }
  function renderOperation() {
    const op=state.operation;
    const operationTab={import:'import',document_import:'sources',download:'news',analysis:'analysis'}[op.kind];
    const completedHere=op.status==='completed'&&(operationTab===state.current||(op.kind==='analysis'&&state.current==='bank'));
    const visible=!!op.busy||completedHere||['paused','error','failed','stopped'].includes(op.status);
    $('workflow-operation').hidden=!visible;if(!visible)return;
    $('workflow-operation').dataset.status=op.busy?'running':op.status==='failed'?'error':op.status;
    const phaseLabels={saving:'Guardando información',download:'Descargando noticias',attribution:'Relacionando entidades y noticias',scoring:'Calculando scores y explicaciones'};
    setText('workflow-operation-label',op.label||phaseLabels[op.phase]||'Procesando información');
    const progress=$('workflow-progress');
    const total=Number.isFinite(op.total)&&op.total>0?op.total:null;
    const completed=Number.isFinite(op.completed)?op.completed:null;
    const percent=Number.isFinite(op.percent)?Math.min(100,Math.max(0,op.percent)):(total!==null&&completed!==null?Math.min(100,100*completed/total):null);
    if(percent!==null) { progress.value=percent;setText('workflow-operation-percent',Math.round(percent)+' %'); }
    else { progress.removeAttribute('value');setText('workflow-operation-percent',op.busy?'En curso':labels[op.status]||'Estado conservado'); }
    const units={entities:'entidades',documents:'documentos',articles:'noticias',jobs:'tareas',urls:'enlaces'};
    let detail=completed!==null?completed.toLocaleString('es-MX')+(total!==null?' de '+total.toLocaleString('es-MX'):'')+' '+(units[op.unit]||op.unit||'elementos'):'Preparando la operación';
    if(Number.isFinite(op.elapsed_seconds))detail+=' · '+formatTime(op.elapsed_seconds)+' transcurridos';
    setText('workflow-operation-detail',detail);
    progress.setAttribute('aria-valuetext',percent!==null?Math.round(percent)+' por ciento. '+detail:detail+'. Total aún no disponible.');
    setText('workflow-operation-eta',op.busy?(Number.isFinite(op.eta_seconds)?'Tiempo restante estimado: '+formatTime(op.eta_seconds):'Tiempo restante: aún no estimable'):(op.status==='completed'?'Operación terminada':op.status==='paused'?'Puedes reanudar la descarga':'Consulta el estado antes de continuar'));
    $('workflow-operation-error').hidden=!op.error;
    setText('workflow-operation-error',op.error);
  }
  function renderNavigation() {
    for(const step of steps) {
      const data=serverStep(step.tab);
      const button=document.querySelector('[data-workflow-step="'+step.id+'"]');
      const current=state.current===step.tab;
      button.dataset.status=data?.status||'blocked';
      button.disabled=!canOpenTab(step.tab);
      button.classList.toggle('active',current);
      if(current)button.setAttribute('aria-current','step');else button.removeAttribute('aria-current');
      button.querySelector('.step-number').textContent=data?.status==='complete'?'✓':String(steps.indexOf(step)+1).padStart(2,'0');
      setText('step-reason-'+step.id,data?.reason||labels[data?.status]||'Pendiente');
      const progress=$('workflow-step-progress-'+step.id);
      const operationStep={import:'data',document_import:'sources',download:'download',acquire:'download',analysis:'analysis',analyze:'analysis',rescore:'analysis'}[state.operation.kind];
      const isOperation=operationStep===step.id;
      const phase=state.operation.phase||'';
      const phaseLabel={saving:'guardado',download:'descarga',attribution:'atribución',scoring:'cálculo del score'}[phase]||phase||'actual';
      const operationPercent=Number.isFinite(state.operation.total)&&state.operation.total>0&&Number.isFinite(state.operation.percent)?Math.max(0,Math.min(99,state.operation.percent)):null;
      if(data?.status==='complete') {
        progress.value=100;progress.setAttribute('aria-valuetext','Paso completado.');
      } else if(isOperation&&['running','paused','error'].includes(data?.status)) {
        if(operationPercent===null)progress.removeAttribute('value');else progress.value=operationPercent;
        progress.setAttribute('aria-valuetext',(labels[data.status]||'En curso')+'. Fase de '+phaseLabel+': '+(operationPercent===null?'total aún no disponible.':Math.round(operationPercent)+' por ciento de esta fase.')+(step.id==='analysis'?' El porcentaje corresponde a la fase actual, no a la duración total del análisis.':''));
      } else {
        progress.value=0;progress.setAttribute('aria-valuetext',data?.status==='ready'?'Paso preparado para comenzar.':'Paso pendiente. '+(data?.reason||'Completa los pasos anteriores.'));
      }
      const option=$('workflow-mobile-step').querySelector('option[value="'+step.tab+'"]');
      option.disabled=!canOpenTab(step.tab);
      option.textContent=(steps.indexOf(step)+1)+'. '+button.querySelector('strong').textContent+(data?.status==='complete'?' · completado':'');
    }
    $('workflow-mobile-step').value=state.current==='audit'?stepFor(state.current)?.tab||'import':state.current;
    const current=stepFor(state.current);
    if(current) {
      const index=steps.indexOf(current)+1;
      setText('workflow-step-kicker','PASO '+String(index).padStart(2,'0')+' DE 05');
      setText('workflow-step-title',current.title);
      setText('workflow-step-description',current.description);
      setText('workflow-status-label',labels[serverStep(state.current)?.status]||'Consultando estado');
    } else {
      setText('workflow-step-kicker','INTEGRIDAD Y RESPALDOS');setText('workflow-step-title','Conserva una revisión verificable');
      setText('workflow-step-description','Comprueba la bitácora y conserva respaldos externos de tu información.');
      setText('workflow-status-label','Procesamiento local');
    }
  }
  function renderPrimary() {
    const button=$('workflow-primary');let label,enabled=true,reason='';
    const opBusy=busy();
    if(state.current==='import') {
      const sources=serverStep('sources');
      label=sources&&sources.status!=='blocked'?'Continuar con las fuentes':'Cargar mi lista';
      enabled=!opBusy;reason=opBusy?'Espera a que termine la operación en curso.':'';
    } else if(state.current==='sources') {
      label=state.workflow?.mode==='local'?'Continuar con mis documentos':'Confirmar fuentes y continuar';
      enabled=action('configure_sources').enabled&&!opBusy;
      reason=enabled?'Revisa el origen y los permisos antes de continuar.':action('configure_sources').reason;
    } else if(state.current==='news') {
      const next=canOpenTab('analysis');
      label=next?'Continuar al análisis':state.workflow?.mode==='local'?'Preparar documentos':'Descargar noticias';
      enabled=next||(!opBusy&&action('download').enabled&&$('news-online').checked);
      reason=next?'La evidencia disponible se analizará en tu equipo.':$('workflow-download-lock').textContent;
    } else if(state.current==='analysis') {
      label=action('view_results').enabled?'Revisar resultados':'Analizar y calcular score';
      enabled=(action('view_results').enabled||action('analyze').enabled)&&!opBusy;
      reason=enabled?'Consulta el detalle de la operación y su cobertura.':action('analyze').reason;
    } else if(state.current==='bank') {
      label='Consultar evidencia';enabled=true;reason='Selecciona una entidad para abrir su expediente.';
    } else { label='Volver al proceso';enabled=true; }
    button.replaceChildren(document.createTextNode(label));
    const arrow=document.createElementNS('http://www.w3.org/2000/svg','svg');arrow.setAttribute('viewBox','0 0 24 24');arrow.setAttribute('fill','none');arrow.setAttribute('stroke','currentColor');arrow.setAttribute('stroke-width','1.8');arrow.setAttribute('aria-hidden','true');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d','M5 12h14m-6-6 6 6-6 6');arrow.append(path);button.append(arrow);
    setButton('workflow-primary',enabled,reason,'workflow-primary-reason');
  }
  function renderActions() {
    if(!state.workflow)return;
    const opBusy=busy();const mode=state.workflow.mode||'online';const local=mode==='local';const counts=state.workflow.counts||{};
    for(const name of ['online','local']) {
      const button=$('workflow-mode-'+name);const selected=mode===name;
      button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));button.disabled=opBusy||!action('configure_sources').enabled;
    }
    $('workflow-online-sources').hidden=local;$('workflow-local-documents').hidden=!local;
    setText('workflow-mode-status',local?'Modo local: la información se importa desde tu equipo. No se descargarán noticias.':'Modo con conexión: sólo se consultarán las fuentes que habilites y autorices.');
    const sourceCount=Number(counts.sources||0);setText('workflow-source-count',sourceCount+' '+(sourceCount===1?'fuente preparada':'fuentes preparadas'));
    setText('workflow-local-document-status',Number(counts.documents||0).toLocaleString('es-MX')+' documentos conservados. La importación no calcula el score.');
    $('workflow-sources-next').textContent=local?'Continuar con mis documentos':'Confirmar fuentes y continuar';
    setButton('workflow-sources-next',!opBusy&&action('configure_sources').enabled,action('configure_sources').reason||'Comprueba el origen de la evidencia antes de continuar.','workflow-sources-reason');
    for(const field of document.querySelectorAll('#news-sources input,#sources input'))field.disabled=opBusy||!action('configure_sources').enabled;
    $('workflow-network-consent').hidden=local;
    setText('workflow-download-mode',local?'Documentos locales':'Fuentes autorizadas');
    setText('workflow-download-card-title',local?'Tu evidencia ya está en el equipo':'Descargar noticias');
    setText('workflow-download-note',local?'Este recorrido utiliza los documentos que importaste. Puedes continuar al análisis sin realizar una descarga.':'La conexión sólo consulta las fuentes habilitadas y autorizadas. La identidad se revisará durante el análisis.');
    setText('workflow-news-count',Number(local?counts.documents||0:counts.articles||0).toLocaleString('es-MX'));
    setText('workflow-news-count-label',local?'documentos locales conservados':'noticias conservadas');
    const download=action('download');
    const downloadReason=local?'Modo local: no necesitas descargar noticias.':opBusy?'Hay una operación en curso. Puedes consultar su progreso arriba.':!download.enabled?download.reason:!$('news-online').checked?'Marca el permiso de conexión para iniciar la descarga.':'Sólo se consultarán las fuentes preparadas en el paso anterior.';
    setButton('news-acquire',!local&&!opBusy&&download.enabled&&$('news-online').checked,downloadReason,'workflow-download-lock');
    setButton('stop',!!state.operation.busy&&/download|acquir|collect/.test(state.operation.kind||state.operation.phase||''));
    $('news-online').disabled=opBusy;
    const downloadState=serverStep('news');setText('workflow-download-status',local?'Sin descarga de red':labels[downloadState?.status]||'Esperando descarga');
    const partial=action('accept_partial');
    $('workflow-partial-warning').hidden=local||!partial.enabled||!state.workflow.coverage?.partial||!!state.workflow.coverage?.accepted_partial;
    setButton('workflow-accept-partial',!opBusy&&partial.enabled,partial.reason);
    const canAnalyze=canOpenTab('analysis');
    setButton('workflow-download-next',canAnalyze&&!opBusy,canAnalyze?'La información conservada está lista para el análisis.':serverStep('analysis')?.reason||action('analyze').reason,'workflow-download-reason');
    setText('workflow-analysis-title',state.operation.busy&&['attribution','scoring'].includes(state.operation.phase)?'Análisis en curso':state.workflow.analysis_current?'El análisis está actualizado':action('analyze').enabled?'Todo listo para analizar':'Completa la preparación');
    setButton('workflow-analyze',!opBusy&&action('analyze').enabled,action('analyze').reason||'Se calcularán las entidades y se conservará una evaluación explicable.','workflow-analysis-reason');
    setButton('news-attribute',!opBusy&&action('analyze').enabled,action('analyze').reason);
    setButton('score',!opBusy&&action('analyze').enabled,action('analyze').reason);
    setButton('workflow-results-next',!opBusy&&action('view_results').enabled,action('view_results').enabled?'Los resultados conservan sus fuentes y razones.':action('view_results').reason,'workflow-results-reason');
    setButton('export',!opBusy&&action('export').enabled,action('export').reason);
    const resultsCurrent=!!action('view_results').enabled;
    $('workflow-results-stale').hidden=resultsCurrent;
    setText('workflow-results-stale','Resultados pendientes de actualizar. '+(action('view_results').reason||'Completa el análisis antes de abrir o revisar una evaluación actual.'));
    for(const field of document.querySelectorAll('#detail .review-form input,#detail .review-form select,#detail .review-form textarea,#detail .review-form button,#detail .identity-span'))field.disabled=!resultsCurrent;
    setButton('collect',!opBusy&&!local&&download.enabled,downloadReason);
    setButton('retry',!opBusy,opBusy?'Espera a que termine la operación.':'');
    setButton('import-documents',!opBusy&&Number(counts.entities||0)>0,Number(counts.entities||0)>0?'':'Primero carga una lista de entidades.');
    for(const id of ['doc-text','doc-file','entity-text','entity-file'])if($(id))$(id).disabled=opBusy;
    if(window.pepsImport?.syncButtons)window.pepsImport.syncButtons();
    renderPrimary();
  }
  function onNavigate(tab) {
    if(state.current!==tab)$('message').hidden=true;
    state.current=tab;
    try { sessionStorage.setItem('peps-workflow-tab',tab); } catch {}
    renderNavigation();renderPrimary();renderOperation();
    window.scrollTo({top:0,behavior:'instant'});
    const panel=document.querySelector('[data-panel="'+tab+'"]');
    const heading=panel?.querySelector('h2');if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}
  }
  async function perform(fn) {
    if(busy())return;
    state.requesting=true;renderActions();
    try { await fn();await state.hooks.refresh(true); }
    catch(error) { state.hooks.message(error.message,true); }
    finally { state.requesting=false;renderActions(); }
  }
  async function confirmSources() {
    if(!action('configure_sources').enabled)return;
    await perform(async()=>{await state.hooks.api('workflow-confirm-sources',{});await state.hooks.refresh(true);if(canOpenTab('news'))await navigate('news');else state.hooks.message(serverStep('news')?.reason||'Completa la configuración de las fuentes.',true);});
  }
  async function primary() {
    if(state.current==='import') { if(canOpenTab('sources'))await navigate('sources');else{window.pepsImport?.open('csv');$('csv-file').focus();} }
    else if(state.current==='sources')await confirmSources();
    else if(state.current==='news') { if(canOpenTab('analysis'))await navigate('analysis');else if(state.workflow?.mode==='local')await navigate('sources');else $('news-acquire').click(); }
    else if(state.current==='analysis') { if(action('view_results').enabled)await navigate('bank');else $('workflow-analyze').click(); }
    else if(state.current==='bank') { $('search').focus();document.querySelector('.workspace')?.scrollIntoView({block:'start',behavior:'smooth'}); }
    else await navigate(steps.find(step=>step.id===state.workflow?.recommended_step)?.tab||'import');
  }
  function update(stats) {
    state.operation=stats.operation||{};
    if(stats.workflow)state.workflow=stats.workflow;
    renderOperation();if(!state.workflow)return;
    renderNavigation();renderActions();
    if(state.first) {
      state.first=false;let saved;try{saved=sessionStorage.getItem('peps-workflow-tab');}catch{}
      const recommended=steps.find(step=>step.id===state.workflow.recommended_step)?.tab||'import';
      navigate(saved&&canOpenTab(saved)?saved:recommended).catch(error=>state.hooks.message(error.message,true));
    }
    const op=state.operation;const id=op.operation_id||op.id;
    if(op.busy&&id)state.observedOperation=id;
    if(id&&id!==state.finishedOperation&&!op.busy&&op.status==='completed'&&state.observedOperation===id) {
      state.finishedOperation=id;
      if(state.current==='news'&&canOpenTab('analysis'))navigate('analysis').catch(error=>state.hooks.message(error.message,true));
      else if(state.current==='analysis'&&action('view_results').enabled)navigate('bank').catch(error=>state.hooks.message(error.message,true));
    }
  }
  function canAction(id) {
    const names={collect:'download','news-acquire':'download','news-attribute':'analyze','import-entities':'import','import-documents':'import',export:'export',score:'analyze'};
    if(!names[id])return true;
    const value=action(names[id]);
    if(busy()||!value.enabled){state.hooks.message(busy()?'Espera a que termine la operación en curso.':value.reason||'Completa el paso anterior.',true);return false;}
    return true;
  }
  function observe(response) { if(response?.operation_id)state.observedOperation=response.operation_id; }
  function init(hooks) {
    if(state.initialized)return;state.initialized=true;state.hooks=hooks;
    $('workflow-mobile-step').onchange=()=>navigate($('workflow-mobile-step').value).catch(error=>hooks.message(error.message,true));
    $('workflow-primary').onclick=()=>primary().catch(error=>hooks.message(error.message,true));
    for(const mode of ['online','local'])$('workflow-mode-'+mode).onclick=()=>perform(async()=>{await hooks.api('workflow-mode',{mode});});
    $('workflow-sources-next').onclick=confirmSources;
    $('workflow-download-next').onclick=()=>navigate('analysis').catch(error=>hooks.message(error.message,true));
    $('workflow-results-next').onclick=()=>navigate('bank').catch(error=>hooks.message(error.message,true));
    $('workflow-analyze').onclick=()=>perform(async()=>{await hooks.api('workflow-analyze',{});hooks.message('Análisis iniciado. Puedes seguir el progreso de atribución y cálculo.');});
    $('workflow-accept-partial').onclick=()=>perform(async()=>{await hooks.api('workflow-accept-partial',{});await hooks.refresh(true);hooks.message('Se continuará con la cobertura disponible. Esta limitación quedará señalada.');if(canOpenTab('analysis'))await navigate('analysis');});
    $('news-online').onchange=()=>renderActions();
    renderNavigation();renderPrimary();
  }
  return {init,update,onNavigate,canOpenTab,canAction,busy,observe};
})();
