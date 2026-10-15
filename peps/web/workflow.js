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
  const state = {hooks:null,workflow:null,operation:{},current:'import',initialized:false,first:true,requesting:false,observedOperation:null,finishedOperation:null,searchLoaded:false,searchLoading:false,searchDirty:false,searchRequest:0,phaseNodes:new Map(),taskNodes:new Map(),estimateNodes:new Map()};
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
  function setText(id,text) { const element=$(id),value=text??'';if(element&&element.textContent!==String(value))element.textContent=String(value); }
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
  function downloadContext() {
    const coverage=state.workflow?.coverage||{},op=state.operation;
    const pending=Number(coverage.pending_downloads??state.workflow?.counts?.pending_downloads??0);
    const workerActive=!!coverage.worker_active||Number(coverage.active_running||0)>0;
    const result=op.kind==='download'?(op.last_result||op.result||{}):{};
    const stopReason=coverage.stop_reason||result.stop_reason||'';
    const attempted=['running','completed','paused','failed','closed_partial'].includes(coverage.status)||op.kind==='download';
    return {coverage,pending,workerActive,stopReason,attempted,local:state.workflow?.mode==='local',
      canContinue:!busy()&&!workerActive&&canOpenTab('analysis')};
  }
  function retryDelay(context) {
    const coverage=context.coverage,op=state.operation,result=op.kind==='download'?(op.last_result||op.result||{}):{};
    const stamp=coverage.next_retry_at??op.next_retry_at??result.next_retry_at;
    if(Number.isFinite(stamp))return Math.max(0,stamp-Date.now()/1000);
    const iso=coverage.next_retry||op.next_retry||result.next_retry;
    if(iso&&Number.isFinite(Date.parse(iso)))return Math.max(0,(Date.parse(iso)-Date.now())/1000);
    const seconds=coverage.next_retry_seconds??coverage.wait_seconds??op.wait_seconds??result.wait_seconds;
    return Number.isFinite(seconds)?Math.max(0,seconds):null;
  }
  function renderDownloadActivity() {
    if(!state.workflow)return;
    const context=downloadContext(),{coverage,pending,workerActive,local,stopReason}=context;
    $('workflow-download-activity').hidden=local;
    const completed=coverage.done??coverage.completed;
    setText('workflow-tasks-completed',Number.isFinite(completed)?completed.toLocaleString('es-MX'):'—');
    for(const [id,key] of [['running',coverage.active_running!==undefined?'active_running':'running'],['queued','queued'],['deferred','deferred'],['failed','failed']])setText('workflow-tasks-'+id,Number(coverage[key]||0).toLocaleString('es-MX'));
    setText('workflow-tasks-retained',pending.toLocaleString('es-MX'));
    const reasons={time_limit:'La sesión alcanzó su límite de tiempo. El avance está guardado y puedes reanudar las tareas pendientes.',job_limit:'La sesión alcanzó su límite de tareas. Puedes continuar la descarga sin repetir las consultas terminadas.',cancelled:'La descarga se pausó por solicitud. Puedes retomarla o continuar con la información disponible.',backoff:'Las fuentes han pedido una pausa entre solicitudes. Las tareas esperan su próximo intento permitido.',scheduled:'Hay tareas programadas para un próximo intento. La información descargada permanece guardada.',external_lease:'Otro trabajador mantiene una descarga activa. Espera a que termine antes de analizar o cerrar la cobertura.'};
    const op=state.operation;
    let activity=op.busy&&op.kind==='download'?(op.label||'Consultando las fuentes autorizadas…'):workerActive?reasons.external_lease:coverage.closed_partial?'Descarga cerrada con cobertura parcial. Las tareas pendientes se conservan para reanudar después.':reasons[stopReason]||(['paused','failed'].includes(coverage.status)?'La sesión se detuvo con tareas pendientes. Puedes reanudar o cerrar esta descarga con lo disponible.':coverage.status==='completed'?'La sesión de descarga terminó. Revisa la cobertura antes de continuar.':'Todavía no se ha iniciado una descarga.');
    if(!op.busy&&!workerActive&&coverage.needs_new_search)activity='El banco o el alcance cambió. Inicia una nueva búsqueda para aplicar el plan actual; las tareas anteriores se conservarán por separado en la cola.';
    setText('workflow-download-current',activity);
    setText('workflow-queue-state',workerActive||op.busy&&op.kind==='download'?'Descarga activa':coverage.closed_partial?'Cobertura parcial aceptada':pending?'Pendientes guardadas':context.attempted?'Sesión terminada':'Sin actividad');
    const delay=retryDelay(context);
    setText('workflow-download-wait',pending?(workerActive?'Hay trabajo activo; el análisis seguirá bloqueado hasta que termine.':delay!==null&&delay>0?'Próximo intento permitido en aproximadamente '+formatTime(delay)+'. Reanuda la descarga para que procese las tareas cuando estén disponibles.':Number(coverage.queued||0)>0?'Hay tareas en cola listas para reanudar.':'No hay una espera estimable para las tareas restantes. Puedes reanudar o continuar con cobertura parcial.'):'No hay tareas pendientes de descarga.');
    const issues=$('workflow-download-issues');
    const notes=[];
    if(Number(coverage.deferred||0))notes.push(Number(coverage.deferred).toLocaleString('es-MX')+' tareas esperan una pausa de la fuente, un reintento o su programación.');
    if(Number(coverage.expired_running||0))notes.push(Number(coverage.expired_running).toLocaleString('es-MX')+' tareas tienen un turno vencido; no representan un trabajador activo.');
    if(Number(coverage.failed||0))notes.push(Number(coverage.failed).toLocaleString('es-MX')+' tareas registran errores o bloqueos. Su ausencia se señalará como límite de cobertura.');
    for(const item of coverage.reasons||[])if(typeof item==='string')notes.push(item);else if(item?.reason)notes.push(item.reason+(item.count?' ('+item.count+')':''));
    const notesKey=JSON.stringify(notes);if(issues.dataset.notesKey!==notesKey){issues.dataset.notesKey=notesKey;issues.replaceChildren();for(const note of notes){const li=document.createElement('li');li.textContent=note;issues.append(li);}}
    issues.hidden=!notes.length;
  }
  function searchPayload() {
    if(!$('workflow-search-form').reportValidity())return null;
    const start=$('workflow-history-start').value,end=$('workflow-history-end').value;
    if(start>end){state.hooks.message('La fecha inicial debe ser anterior o igual a la fecha final.',true);$('workflow-history-start').focus();return null;}
    return {strategy:$('workflow-search-strategy').value,preset:$('workflow-search-preset').value,start,end,
      window:$('workflow-history-window').value,max_queries:Number($('workflow-search-max-queries').value),
      include_aliases:$('workflow-search-aliases').checked,use_context:$('workflow-search-context').checked};
  }
  function searchDate(value) {
    const text=String(value||'');
    if(/^\d{8,14}$/.test(text))return text.slice(6,8)+'/'+text.slice(4,6)+'/'+text.slice(0,4);
    if(/^\d{4}-\d{2}-\d{2}/.test(text))return text.slice(8,10)+'/'+text.slice(5,7)+'/'+text.slice(0,4);
    return text;
  }
  function renderSearch(response) {
    const config=response.config||{},plan=response.plan||{};
    for(const [id,key] of [['workflow-search-strategy','strategy'],['workflow-search-preset','preset'],['workflow-history-start','start'],['workflow-history-end','end'],['workflow-history-window','window'],['workflow-search-max-queries','max_queries']])if(config[key]!==undefined)$(id).value=String(config[key]);
    for(const [id,key] of [['workflow-search-aliases','include_aliases'],['workflow-search-context','use_context']])if(config[key]!==undefined)$(id).checked=!!config[key];
    state.searchLoaded=true;state.searchDirty=false;
    const box=$('workflow-search-plan');box.replaceChildren();
    const summary=document.createElement('p');summary.className='search-plan-summary';
    const measures=[];
    const measured={entities:plan.entities_planned??plan.entities,queries:plan.query_count??(Array.isArray(plan.queries)?plan.queries.length:plan.queries),windows:plan.windows??(Array.isArray(plan.sources)?plan.sources.reduce((sum,source)=>sum+Number(source.windows||0),0):undefined)};
    if(config.strategy!=='feeds'&&Number.isFinite(measured.entities))measures.push(measured.entities.toLocaleString('es-MX')+(Number.isFinite(plan.entities_total)?' de '+plan.entities_total.toLocaleString('es-MX'):'')+' entidades incluidas');
    for(const [key,label] of [['queries',config.strategy==='feeds'?'feeds previstos':'consultas'],['windows','ventanas por fuente']])if(Number.isFinite(measured[key]))measures.push(measured[key].toLocaleString('es-MX')+' '+label);
    summary.textContent=measures.length?measures.join(' · '):'La planeación muestra el alcance de las fuentes preparadas, sin iniciar descargas.';
    box.append(summary);
    if(measured.queries>0&&Number.isFinite(plan.estimated_schedule_seconds)){
      const estimate=document.createElement('p');estimate.className='search-plan-estimate';
      estimate.textContent='Tiempo mínimo programado entre consultas: '+formatTime(plan.estimated_schedule_seconds)+'. No incluye la duración ni los reintentos de las descargas.';box.append(estimate);
    }
    const sources=Array.isArray(plan.sources)?plan.sources:[];
    if(sources.length){
      const list=document.createElement('ul');list.className='search-effective-ranges';
      for(const source of sources){const item=document.createElement('li'),name=source.source_name||source.name||source.id;item.textContent=name+': '+(source.start&&source.end_exclusive?'desde '+searchDate(source.start)+' hasta antes del '+searchDate(source.end_exclusive):source.scope==='current_feed_only'?'actualizaciones del feed, sin búsqueda de archivo histórico':'alcance informado por la fuente')+'.';list.append(item);}box.append(list);
    }
    const examples=(Array.isArray(plan.queries)?plan.queries:[]).filter(query=>typeof query.query==='string'&&query.query.trim()).slice(0,3);
    if(examples.length){
      const details=document.createElement('details');details.id='workflow-query-examples';details.className='search-query-examples';const title=document.createElement('summary');title.textContent='Ejemplos de búsquedas';details.append(title);const list=document.createElement('ol');
      for(const query of examples){const item=document.createElement('li'),source=sources.find(candidate=>candidate.id===query.source_id),label=document.createElement('p');label.className='query-example-source';label.textContent=(query.source_name||source?.source_name||source?.name||query.source_id||'Fuente configurada')+(query.start&&query.end?' · desde '+searchDate(query.start)+' hasta antes del '+searchDate(query.end):'');const text=document.createElement('p');text.className='query-example-text';text.textContent=query.query;item.append(label,text);list.append(item);}details.append(list);box.append(details);
    }
    const warnings=[...(plan.warnings||[])];if(plan.truncated&&!warnings.some(item=>/presupuesto|límite de consultas/i.test(typeof item==='string'?item:item.message||'')))warnings.push('El plan alcanza el límite de consultas. Amplía el límite o acota el período para revisar la cobertura.');
    if(warnings.length){const list=document.createElement('ul');for(const warning of warnings){const item=document.createElement('li');item.textContent=typeof warning==='string'?warning:warning.message||warning.reason||'';list.append(item);}box.append(list);}
    setText('workflow-search-status','Alcance preparado. Confirma las fuentes para continuar.');
  }
  async function loadSearch() {
    if(state.searchLoading||state.searchDirty)return;
    const request=++state.searchRequest;state.searchLoading=true;renderActions();
    try {const response=await state.hooks.api('news-search');if(request===state.searchRequest&&!state.searchDirty)renderSearch(response);}
    catch(error){setText('workflow-search-status','No se pudo cargar la búsqueda: '+error.message);}
    finally{if(request===state.searchRequest){state.searchLoading=false;renderActions();}}
  }
  function timingEstimate(item,paused=false) {
    const kind=item?.eta_kind||'unknown',seconds=item?.eta_seconds;
    if(kind==='completed'||['completed','complete','done'].includes(item?.status))return {text:'Terminada',kind:'completed',basis:'Trabajo completado'};
    if(kind==='waiting')return {text:Number.isFinite(seconds)?'Próximo intento en ≈ '+formatTime(seconds):'Esperando a la fuente',kind,basis:'Espera programada; no es tiempo de descarga'};
    if(paused)return {text:'Estimación en pausa',kind:'paused',basis:'Reanuda para actualizar los tiempos'};
    if(Number.isFinite(seconds)&&seconds>=0&&['measured','estimated'].includes(kind))return {text:'Restante estimado: ≈ '+formatTime(seconds),kind,basis:kind==='measured'?'Calculado con el trabajo de esta sesión':'Estimado con observaciones previas'};
    return {text:'Calculando estimación',kind:'unknown',basis:'Aún no hay trabajo observado suficiente'};
  }
  function timingCard(kind) {
    const element=document.createElement('article');element.className=kind==='phase'?'phase-progress-card':'task-timing-row';
    const heading=document.createElement('div');heading.className='timing-card-heading';
    const title=document.createElement(kind==='phase'?'h4':'strong'),status=document.createElement('span');status.className='timing-status';heading.append(title,status);
    const source=document.createElement('p');source.className='timing-source';
    const progress=document.createElement('progress');progress.max=100;progress.className='timing-progress';
    const counts=document.createElement('p');counts.className='timing-counts';
    const meta=document.createElement('div');meta.className='timing-meta';
    const elapsed=document.createElement('span'),eta=document.createElement('span');eta.className='timing-eta';meta.append(elapsed,eta);
    const basis=document.createElement('p');basis.className='timing-basis';element.append(heading,source,progress,counts,meta,basis);
    return {element,title,status,source,progress,counts,elapsed,eta,basis};
  }
  function updateTimingCard(card,item,kind) {
    const put=(element,value)=>{const text=String(value??'');if(element.textContent!==text)element.textContent=text;};
    const paused=kind!=='estimate'&&!state.operation.busy&&['paused','cancel_requested','failed'].includes(state.operation.status);
    const rawStatus=item.status||'running',status=paused&&['running','waiting'].includes(rawStatus)?'paused':rawStatus;
    const stageLabels={fetch:'Descargando',fetching:'Descargando',discovery:'Revisando resultados',process:'Conservando documento',download:'Descargando',robots:'Comprobando acceso',extract:'Extrayendo texto',extracting:'Extrayendo texto',parse:'Leyendo contenido',parsing:'Leyendo contenido',saving:'Guardando',waiting:'En espera',attribution:'Relacionando entidades',scoring:'Calculando score'};
    put(card.title,item.label||{saving:'Guardar información',download:'Descargar noticias',attribution:'Relacionar entidades',scoring:'Calcular scores'}[item.id]||'Tarea en curso');
    put(card.status,{running:'En curso',waiting:'En espera',paused:'En pausa',completed:'Terminada',complete:'Terminada',pending:'Pendiente',ready:'Preparada',error:'Con error',failed:'Con error'}[status]||'En curso');
    card.element.dataset.status=status;card.element.dataset[kind==='task'?'taskId':'phase']=String(item.id||'');
    const description=[item.source,kind==='task'?(stageLabels[item.stage]||item.stage):''].filter(Boolean).join(' · ');put(card.source,description);card.source.hidden=!description;
    const total=Number.isFinite(item.total)&&item.total>0?item.total:null,completed=Number.isFinite(item.completed)?item.completed:null;
    const finished=['completed','complete','done'].includes(status);
    if(finished)card.progress.value=100;else if(['ready','pending','paused','failed'].includes(status)&&total===null)card.progress.value=0;else if(total!==null&&completed!==null)card.progress.value=Math.min(99,Math.max(0,100*completed/total));else card.progress.removeAttribute('value');
    card.progress.setAttribute('aria-label','Progreso de '+card.title.textContent);
    const units={entities:'entidades',documents:'documentos',articles:'noticias',jobs:'tareas',urls:'enlaces'};
    const countText=completed!==null?completed.toLocaleString('es-MX')+(total!==null?' de '+total.toLocaleString('es-MX'):'')+' '+(units[item.unit]||item.unit||'elementos'):'';
    put(card.counts,countText);card.counts.hidden=!countText;
    card.progress.setAttribute('aria-valuetext',finished?'Trabajo terminado':countText||'Avance aún no cuantificable; se muestra la actividad real');
    put(card.elapsed,kind==='estimate'?'Aún no iniciada':kind==='task'&&item.eta_scope==='current_stage'&&Number.isFinite(item.stage_elapsed_seconds)?'Tarea: '+formatTime(item.elapsed_seconds||0)+' · etapa: '+formatTime(item.stage_elapsed_seconds):Number.isFinite(item.elapsed_seconds)?formatTime(item.elapsed_seconds)+' transcurridos':'Tiempo aún no disponible');
    const estimate=timingEstimate(item,paused);const notStarted=kind==='estimate'||['ready','pending'].includes(status),currentStage=kind==='task'&&item.eta_scope==='current_stage';let etaText=notStarted&&estimate.kind==='unknown'?'Se estimará al iniciar':currentStage?estimate.text.replace('Restante estimado','Esta etapa: restante estimado'):estimate.text;let basisText=notStarted&&estimate.kind==='unknown'?'Primera ejecución: se necesita trabajo observado':estimate.basis;if(currentStage&&['estimated','measured'].includes(estimate.kind))basisText+=' de esta etapa; no es el tiempo total de la noticia';if(item.eta_basis==='observation_exceeded'){etaText='Reajustando estimación';basisText='Está tardando más que las observaciones previas; la actividad sigue en curso';}put(card.eta,etaText);put(card.basis,basisText);card.eta.dataset.kind=estimate.kind;
    if(item.eta_basis)card.basis.title=String(item.eta_basis);
  }
  function renderTiming() {
    const data=state.operation.task_progress,phases=Array.isArray(data?.phases)?data.phases:[],tasks=Array.isArray(data?.active)?data.active:[];
    const phaseBox=$('workflow-phase-progress'),details=$('workflow-task-timing'),taskBox=$('workflow-active-tasks');
    const compact=state.current==='bank'&&!state.operation.busy&&state.operation.status==='completed';
    if(compact){if(phaseBox.parentNode!==details)details.insertBefore(phaseBox,taskBox);}else if(phaseBox.parentNode===details)details.before(phaseBox);
    if(details.dataset.compact!==String(compact)){details.dataset.compact=String(compact);if(compact)details.open=false;}
    phaseBox.hidden=!phases.length;
    for(const [box,items,nodes,kind] of [[phaseBox,phases,state.phaseNodes,'phase'],[$('workflow-active-tasks'),tasks,state.taskNodes,'task']]){
      const keys=new Set();
      items.forEach((item,index)=>{const key=String(item.id??index);keys.add(key);let card=nodes.get(key);if(!card){card=timingCard(kind);nodes.set(key,card);box.append(card.element);}updateTimingCard(card,item,kind);});
      for(const [key,card] of nodes)if(!keys.has(key)){card.element.remove();nodes.delete(key);}
    }
    const queue=data?.queue;
    details.hidden=!data;
    setText('workflow-task-timing-title',compact?'Ver tiempos de las fases terminadas':tasks.length?'Ver tiempos de las tareas ('+tasks.length+' activas)':'Ver tiempos y esperas');
    $('workflow-task-empty').hidden=!!tasks.length||compact;
    if(queue&&state.operation.kind==='download'){
      const estimate=timingEstimate(queue,!state.operation.busy&&state.operation.status==='paused');
      const next=queue.next_retry_seconds,context=downloadContext();
      setText('workflow-operation-eta',context.coverage.closed_partial?'Cobertura parcial guardada; las tareas restantes se conservan.':!state.operation.busy&&state.operation.status==='completed'&&Number(queue.pending||0)===0?'Operación terminada':!state.operation.busy&&state.operation.status==='failed'?'Operación detenida; consulta el error.':estimate.kind==='waiting'&&Number.isFinite(next)?'Próximo intento de la cola en ≈ '+formatTime(next):'Cola · '+estimate.text);
    } else if(phases.length){
      const current=phases.find(phase=>phase.id===state.operation.phase)||phases.find(phase=>phase.status==='running');
      if(current){const estimate=timingEstimate(current,!state.operation.busy&&state.operation.status==='paused');setText('workflow-operation-eta',estimate.text);}
    }
  }
  function renderPreflightEstimates() {
    const estimates=state.operation.task_progress?.estimates;
    const items=Array.isArray(estimates)?estimates:Object.values(estimates||{});
    const analysis=items.filter(item=>['attribution','scoring'].includes(item.id));
    const box=$('workflow-analysis-estimates'),show=analysis.length&&!state.operation.busy&&!state.workflow?.analysis_current;
    box.hidden=!show;
    const keys=new Set();
    for(const item of analysis){const key=String(item.id);keys.add(key);let card=state.estimateNodes.get(key);if(!card){card=timingCard('phase');state.estimateNodes.set(key,card);box.append(card.element);}updateTimingCard(card,{...item,status:'ready',completed:null,total:null},'estimate');}
    for(const [key,card] of state.estimateNodes)if(!keys.has(key)){card.element.remove();state.estimateNodes.delete(key);}
    const download=items.find(item=>item.id==='download');
    if(!download||state.operation.busy||downloadContext().local){$('workflow-download-estimate').hidden=true;return;}
    $('workflow-download-estimate').hidden=false;
    const estimate=timingEstimate(download);
    setText('workflow-download-estimate',estimate.kind==='waiting'?(Number.isFinite(download.eta_seconds)?'Próximo intento permitido en ≈ '+formatTime(download.eta_seconds)+'. La espera no es una estimación de descarga.':'Hay una espera programada por la fuente.'):['measured','estimated'].includes(estimate.kind)&&Number.isFinite(download.eta_seconds)?'Tiempo estimado de descarga: ≈ '+formatTime(download.eta_seconds)+'. Basado en trabajo observado; puede cambiar con las fuentes.':'Tiempo de descarga: se estimará al observar las primeras tareas. Las pausas de las fuentes se muestran por separado.');
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
    progress.hidden=!op.busy&&percent===null;
    if(percent!==null) { progress.value=percent;setText('workflow-operation-percent',Math.round(percent)+' % de la fase'); }
    else { progress.removeAttribute('value');setText('workflow-operation-percent',op.busy?'En curso':labels[op.status]||'Estado conservado'); }
    const units={entities:'entidades',documents:'documentos',articles:'noticias',jobs:'tareas',urls:'enlaces'};
    let detail=completed!==null?completed.toLocaleString('es-MX')+(total!==null?' de '+total.toLocaleString('es-MX'):'')+' '+(units[op.unit]||op.unit||'elementos'):'Preparando la operación';
    if(Number.isFinite(op.elapsed_seconds))detail+=' · '+formatTime(op.elapsed_seconds)+' transcurridos';
    setText('workflow-operation-detail',detail);
    progress.setAttribute('aria-valuetext',percent!==null?Math.round(percent)+' por ciento. '+detail:detail+'. Total aún no disponible.');
    setText('workflow-operation-eta',op.busy?(Number.isFinite(op.eta_seconds)?'Tiempo restante estimado: '+formatTime(op.eta_seconds):'Tiempo restante: aún no estimable'):(op.status==='completed'?'Operación terminada':op.status==='paused'?'Puedes reanudar la descarga':'Consulta el estado antes de continuar'));
    $('workflow-operation-error').hidden=!op.error;
    setText('workflow-operation-error',op.error);
    if(op.kind==='download'&&state.workflow){const context=downloadContext(),delay=retryDelay(context);if(!op.busy&&context.coverage.closed_partial){$('workflow-operation').dataset.status='completed';setText('workflow-operation-label','Descarga cerrada con cobertura parcial');setText('workflow-operation-eta','Las tareas pendientes se conservan para reanudar después.');}else if(!op.busy&&context.pending){setText('workflow-operation-label','Descarga en pausa · '+context.pending.toLocaleString('es-MX')+' tareas pendientes');setText('workflow-operation-eta',delay!==null&&delay>0?'Próximo intento permitido en aproximadamente '+formatTime(delay):'Reanuda la descarga o continúa con cobertura parcial.');}else if(op.busy&&delay!==null&&delay>0&&(context.coverage.waiting||op.waiting||(op.last_result||op.result||{}).waiting)){setText('workflow-operation-eta','Espera de la fuente: próximo intento en aproximadamente '+formatTime(delay));}}
    renderTiming();
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
      const counts=state.workflow?.counts||{};
      const hints={data:Number(counts.entities||0)>0?Number(counts.entities).toLocaleString('es-MX')+(Number(counts.entities)===1?' entidad guardada':' entidades guardadas'):'Un nombre o una lista para comenzar',sources:state.workflow?.sources_confirmed?'Origen y permisos confirmados':'Elige origen y permisos',download:state.workflow?.mode==='local'?'Recorrido sin conexión':Number(counts.pending_downloads||0)>0?Number(counts.pending_downloads).toLocaleString('es-MX')+' tareas pendientes':'Noticias y fuentes conservadas',analysis:state.workflow?.analysis_current?'Evaluación actualizada':'Atribución y cálculo local',results:action('view_results').enabled?'Expedientes disponibles':'Explicaciones y revisión'};
      setText('step-reason-'+step.id,data?.status==='blocked'?'Completa los pasos anteriores':hints[step.id]||labels[data?.status]||'Pendiente');
      button.title=data?.reason||hints[step.id]||'';
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
    const button=$('workflow-primary');document.querySelector('.workflow-overview').hidden=state.current==='news';button.closest('.workflow-overview-action').hidden=['import','sources','news','analysis'].includes(state.current);let label,enabled=true,reason='';
    const opBusy=busy();
    if(state.current==='import') {
      const sources=serverStep('sources');
      label=sources&&sources.status!=='blocked'?'Continuar con las fuentes':'Cargar mi lista';
      enabled=!opBusy;reason=opBusy?'Espera a que termine la operación en curso.':'';
    } else if(state.current==='sources') {
      label=state.workflow?.mode==='local'?'Continuar con mis documentos':'Confirmar fuentes y continuar';
      enabled=action('configure_sources').enabled&&!opBusy&&!state.searchLoading;
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
    if(button.dataset.label!==label){button.dataset.label=label;button.replaceChildren(document.createTextNode(label));
    const arrow=document.createElementNS('http://www.w3.org/2000/svg','svg');arrow.setAttribute('viewBox','0 0 24 24');arrow.setAttribute('fill','none');arrow.setAttribute('stroke','currentColor');arrow.setAttribute('stroke-width','1.8');arrow.setAttribute('aria-hidden','true');
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('d','M5 12h14m-6-6 6 6-6 6');arrow.append(path);button.append(arrow);}
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
    const context=downloadContext(),{coverage,pending,workerActive}=context;
    $('workflow-network-consent').hidden=local||context.canContinue&&!pending;
    $('workflow-search-fields').disabled=opBusy||state.searchLoading||!action('configure_sources').enabled;
    setButton('workflow-save-search',!opBusy&&!state.searchLoading&&action('configure_sources').enabled);
    $('workflow-search-preset').disabled=$('workflow-search-fields').disabled||$('workflow-search-strategy').value==='feeds';
    setText('workflow-download-mode',local?'Documentos locales':'Fuentes autorizadas');
    setText('workflow-download-card-title',local?'Tu evidencia ya está en el equipo':context.canContinue?'La información está lista para analizar':pending?'Tu avance está guardado':'Descargar noticias');
    setText('workflow-download-note',local?'Este recorrido utiliza los documentos que importaste. Puedes continuar al análisis sin realizar una descarga.':coverage.needs_new_search?'Cambió el banco o el alcance de la búsqueda. Se preparará el plan nuevo y se conservarán las tareas anteriores, sin presentarlas como consultas del alcance actual.':pending?pending.toLocaleString('es-MX')+' tareas siguen pendientes. Reanuda desde ese punto o cierra la descarga con la cobertura ya disponible.':'La conexión consulta las fuentes y el período que preparaste. La identidad se revisará durante el análisis.');
    setText('workflow-news-count',Number(local?counts.documents||0:counts.articles||0).toLocaleString('es-MX'));
    setText('workflow-news-count-label',local?'documentos locales conservados':'noticias conservadas');
    setText('workflow-coverage-note',local?'Los documentos se analizarán en tu equipo.':Number(coverage.unfetched_articles||0)?Number(coverage.unfetched_articles).toLocaleString('es-MX')+' noticias conservan sólo la referencia o el resumen; todavía no tienen un cuerpo descargado.':'La cantidad de noticias indica cobertura, no riesgo ni una identidad confirmada.');
    const download=action('download');
    const downloadReason=workerActive?'Hay un trabajador activo. Espera a que termine o solicita la pausa.':local?'Modo local: no necesitas descargar noticias.':opBusy?'Hay una operación en curso. Puedes consultar su actividad y progreso.':context.canContinue?(coverage.accepted_partial?'Continuarás con cobertura parcial; las tareas restantes se conservan.':'La evidencia disponible está lista para analizar.'):!download.enabled?download.reason:!$('news-online').checked?'Marca el permiso de conexión para iniciar o reanudar la descarga.':coverage.needs_new_search?'Se aplicará el plan actual y se conservará la cola anterior. Las tareas ya guardadas no se eliminan.':pending?'Se retomarán las tareas pendientes sin volver a crear las consultas terminadas.':'Sólo se consultarán las fuentes y el alcance que preparaste.';
    $('news-acquire').dataset.downloadAction=context.canContinue?'continue':coverage.needs_new_search?'start_new':pending?'resume':'start';
    $('news-acquire').textContent=context.canContinue?'Continuar al análisis':opBusy&&state.operation.kind==='download'?'Descarga en curso…':coverage.needs_new_search?'Iniciar nueva búsqueda':pending?'Reanudar descarga':'Iniciar descarga';
    setButton('news-acquire',context.canContinue||!local&&!opBusy&&!workerActive&&download.enabled&&$('news-online').checked,downloadReason,'workflow-download-lock');
    $('stop').hidden=!state.operation.busy||!(/download|acquir|collect/.test(state.operation.kind||state.operation.phase||''));
    setButton('stop',!!state.operation.busy&&/download|acquir|collect/.test(state.operation.kind||state.operation.phase||''));
    $('news-online').disabled=opBusy||workerActive;
    const canResumeLater=!local&&pending>0&&!!coverage.accepted_partial;
    $('workflow-resume-download').hidden=!canResumeLater;
    $('workflow-resume-note').hidden=!canResumeLater;
    setButton('workflow-resume-download',canResumeLater&&!opBusy&&!workerActive&&download.enabled&&$('news-online').checked,workerActive?'Hay un trabajador activo.':!$('news-online').checked?'Marca el permiso de conexión para reanudar.':'Ampliar la cobertura requiere volver a analizar; los resultados históricos se conservan.','workflow-resume-note');
    const downloadState=serverStep('news');setText('workflow-download-status',local?'Sin descarga de red':coverage.closed_partial?'Cobertura parcial aceptada':workerActive||state.operation.busy&&state.operation.kind==='download'?'Descarga activa':pending?'En pausa · '+pending.toLocaleString('es-MX')+' pendientes':labels[downloadState?.status]||'Esperando descarga');
    const partial=action('accept_partial');
    $('workflow-partial-warning').hidden=local||!context.attempted||(!coverage.partial&&!pending)||!!coverage.accepted_partial;
    setText('workflow-partial-description','Se analizará únicamente la información que ya está guardada. '+(pending?pending.toLocaleString('es-MX')+' tareas pendientes se conservarán para reanudar después. ':'')+'Los resultados indicarán la cobertura parcial.');
    setButton('workflow-accept-partial',!opBusy&&!workerActive&&partial.enabled,workerActive?'No puedes cerrar la cobertura mientras haya un trabajador activo.':opBusy?'Espera a que termine o se pause la operación actual.':partial.reason||'Esta decisión no elimina ni marca como completadas las tareas pendientes.','workflow-partial-reason');
    $('workflow-download-footer').hidden=true;
    setButton('workflow-download-next',context.canContinue,context.canContinue?'La información conservada está lista para el análisis.':serverStep('analysis')?.reason||action('analyze').reason,'workflow-download-reason');
    renderDownloadActivity();
    setText('workflow-analysis-title',state.operation.busy&&['attribution','scoring'].includes(state.operation.phase)?'Análisis en curso':state.workflow.analysis_current?'El análisis está actualizado':action('analyze').enabled?'Todo listo para analizar':'Completa la preparación');
    const resultsReady=!!action('view_results').enabled;
    $('workflow-analyze').classList.toggle('primary',!resultsReady);
    if($('workflow-analyze').dataset.currentLabel!==String(resultsReady)){$('workflow-analyze').dataset.currentLabel=String(resultsReady);$('workflow-analyze').textContent=resultsReady?'Volver a analizar':'Analizar y calcular score';}
    $('workflow-results-next').classList.toggle('primary',resultsReady);
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
    if(state.current==='sources'&&state.searchLoading){setButton('workflow-sources-next',false,'Cargando la configuración de búsqueda…','workflow-sources-reason');}
    renderPreflightEstimates();
    if(window.pepsImport?.syncButtons)window.pepsImport.syncButtons();
    renderPrimary();
  }
  function onNavigate(tab) {
    if(state.current!==tab)$('message').hidden=true;
    state.current=tab;
    try { sessionStorage.setItem('peps-workflow-tab',tab); } catch {}
    renderNavigation();renderPrimary();renderOperation();
    if(tab==='sources')loadSearch();
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
  async function startDownload(resume=false) {
    const context=downloadContext();
    if(context.local||busy()||context.workerActive||!action('download').enabled||!$('news-online').checked)return;
    await perform(async()=>{await state.hooks.api('news-acquire',{online:true,resume});state.hooks.message(resume?'Reanudando las tareas pendientes. El avance anterior se conserva.':'Descarga iniciada con el alcance preparado.');});
  }
  async function confirmSources() {
    if(!action('configure_sources').enabled||state.searchLoading)return;
    const config=state.searchDirty?searchPayload():null;if(state.searchDirty&&!config)return;
    await perform(async()=>{if(config)renderSearch(await state.hooks.api('news-search',config));await state.hooks.api('workflow-confirm-sources',{});await state.hooks.refresh(true);if(canOpenTab('news'))await navigate('news');else state.hooks.message(serverStep('news')?.reason||'Completa la configuración de las fuentes.',true);});
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
    // This button owns the contextual download/recovery action; the legacy app handler is replaced here.
    $('news-acquire').onclick=()=>{const context=downloadContext();if(context.canContinue)return navigate('analysis').catch(error=>hooks.message(error.message,true));return startDownload(context.pending>0&&!context.coverage.needs_new_search);};
    $('workflow-resume-download').onclick=()=>startDownload(true);
    $('workflow-search-form').onsubmit=event=>{event.preventDefault();const config=searchPayload();if(!config||!action('configure_sources').enabled)return;perform(async()=>{renderSearch(await hooks.api('news-search',config));});};
    for(const field of document.querySelectorAll('#workflow-search-fields input,#workflow-search-fields select'))field.addEventListener('change',()=>{state.searchDirty=true;setText('workflow-search-status','Hay cambios sin aplicar. Guárdalos o confirma las fuentes para continuar.');renderActions();});
    $('workflow-download-next').onclick=()=>navigate('analysis').catch(error=>hooks.message(error.message,true));
    $('workflow-results-next').onclick=()=>navigate('bank').catch(error=>hooks.message(error.message,true));
    $('workflow-analyze').onclick=()=>perform(async()=>{await hooks.api('workflow-analyze',{});hooks.message('Análisis iniciado. Puedes seguir el progreso de atribución y cálculo.');});
    $('workflow-accept-partial').onclick=()=>perform(async()=>{await hooks.api('workflow-accept-partial',{});await hooks.refresh(true);hooks.message('Descarga cerrada con cobertura parcial. Las tareas pendientes se conservan y los resultados mostrarán esta limitación.');if(canOpenTab('analysis'))await navigate('analysis');});
    $('news-online').onchange=()=>renderActions();
    renderNavigation();renderPrimary();
  }
  return {init,update,onNavigate,canOpenTab,canAction,busy,observe};
})();
