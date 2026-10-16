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
  const labels = {complete:'Completado',ready:'Listo para continuar',blocked:'Pendiente',running:'En curso',waiting:'Esperando fuentes',preparing:'Preparando consultas',pausing:'Confirmando pausa',resumable:'Avance guardado',external_worker:'Otro proceso activo',paused:'En pausa',failed:'Requiere atención',error:'Requiere atención'};
  const state = {hooks:null,workflow:null,operation:{},acquisition:null,current:'import',initialized:false,first:true,requesting:false,recovering:false,observedOperation:null,finishedOperation:null,searchLoaded:false,searchLoading:false,searchDirty:false,searchConfig:{},searchPlan:{},searchRequest:0,phaseNodes:new Map(),taskNodes:new Map(),estimateNodes:new Map()};
  function stepFor(tab) { return steps.find(step=>step.tab===tab); }
  function serverStep(tab) { const step=stepFor(tab);return state.workflow?.steps?.find(item=>item.id===step?.id); }
  function action(name) { return state.workflow?.actions?.[name] || {enabled:false,reason:'Consultando el estado del proceso…'}; }
  function busy() { return !!state.operation.busy || state.requesting; }
  function downloading(op=state.operation){return op.kind==='download'||op.kind==='pipeline'&&['download','preparing'].includes(op.phase);}
  function cacheMode(){return $('workflow-cache-mode').value||'reuse';}
  function onlineAllowed(){return cacheMode()==='local'||$('news-online').checked;}
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
    const coverage=state.workflow?.coverage||{},op=state.operation,activity=state.workflow?.activity||coverage.activity||op.activity;
    const pending=Number(activity?.current_bank_tasks?.pending??coverage.pending_downloads??state.workflow?.counts?.pending_downloads??0);
    const workerActive=activity?!!activity.owner_active||Number(activity.active_requests||0)>0:!!coverage.worker_active||Number(coverage.active_running||0)>0;
    const result=downloading(op)?(op.last_result||op.result||{}):{};
    const stopReason=coverage.stop_reason||result.stop_reason||'';
    const attempted=['running','completed','paused','failed','closed_partial'].includes(coverage.status)||downloading(op);
    return {coverage,pending,workerActive,stopReason,attempted,activity,preparationIncomplete:!!(coverage.preparation_incomplete||result.preparation_incomplete),local:state.workflow?.mode==='local',
      canContinue:!busy()&&!workerActive&&canOpenTab('analysis')};
  }
  function retryDelay(context) {
    const coverage=context.coverage,op=state.operation,result=downloading(op)?(op.last_result||op.result||{}):{};
    const stamp=context.activity?.next_retry_at??coverage.next_retry_at??op.next_retry_at??result.next_retry_at;
    if(Number.isFinite(stamp))return Math.max(0,stamp-Date.now()/1000);
    const iso=coverage.next_retry||op.next_retry||result.next_retry;
    if(iso&&Number.isFinite(Date.parse(iso)))return Math.max(0,(Date.parse(iso)-Date.now())/1000);
    const seconds=context.activity?.next_retry_seconds??coverage.next_retry_seconds??coverage.wait_seconds??op.wait_seconds??result.wait_seconds;
    return Number.isFinite(seconds)?Math.max(0,seconds):null;
  }
  function currentActivity(context=downloadContext()) {
    if(context.activity)return context.activity;
    const op=state.operation,requests=Number(context.coverage.active_running||0),delay=retryDelay(context);
    const status=op.status==='cancel_requested'?'pausing':op.busy&&downloading()?requests?'running':delay>0?'waiting':'preparing':context.workerActive?'external_worker':context.coverage.closed_partial?'completed':context.pending?'paused':context.attempted?'completed':'idle';
    return {state:status,owner_active:!!op.busy,active_requests:requests,can_pause:!!op.busy,next_retry_seconds:delay};
  }
  function renderSearchLive() {
    if(!state.workflow)return;
    const context=downloadContext(),activity=currentActivity(context),data=state.acquisition||{},material=activity.material||{},total=Number(data.entities_total??state.workflow.counts?.entities??0),format=n=>Number(n||0).toLocaleString('es-MX');
    const labels={running:'Buscando información',waiting:'Esperando a las fuentes',preparing:'Preparando las consultas',pausing:'Confirmando la pausa',paused:'Búsqueda en pausa',resumable:'Avance guardado',completed:'Búsqueda terminada',failed:'La búsqueda necesita atención',idle:'Todo listo para buscar',external_worker:'Búsqueda abierta en otro proceso'};
    const current=activity.state||'idle',bodies=Number(material.body_articles??data.body_articles??0),candidates=Number(material.relevant_articles??data.relevant_articles??0),queried=Number(data.entities_queried||0),shared=state.searchConfig.strategy==='shared';
    $('workflow-search-live').dataset.state=current;
    setText('workflow-list-scope','LISTA ACTUAL · '+format(total)+' '+(total===1?'ENTIDAD':'ENTIDADES'));
    const analyzing=state.operation.busy&&['attribution','scoring'].includes(state.operation.phase);
    setText('workflow-download-card-title',context.local?'Tus documentos están en el equipo':analyzing?'Descarga finalizada · análisis en curso':labels[current]||activity.label||'Revisando el estado');
    const description=context.local?'Continúa con los documentos que importaste, sin consultas de red.':current==='waiting'?'El proceso sigue abierto, pero no está descargando en este instante. Las fuentes fijan cuándo puede continuar.':current==='pausing'?'La petición actual debe terminar antes de cambiar la búsqueda o iniciar el análisis. Tu avance se conserva.':current==='running'?activity.detail||'Consulta nombres y conserva sólo los resultados pertinentes. La identidad se comprueba durante el análisis.':current==='preparing'?'Preparando el siguiente trabajo. Los nombres cuentan como consultados sólo después de recibir una respuesta.':current==='paused'||current==='resumable'?'El avance está guardado. Puedes reanudar sin repetir las consultas terminadas o cambiar el enfoque.':context.pending?'Quedan '+format(context.pending)+' tareas guardadas. Revisa la cobertura antes de continuar.':'Los contadores muestran únicamente coincidencias de esta lista.';
    setText('workflow-download-note',analyzing?'La descarga ya dio paso al análisis. Puedes seguir la atribución y el cálculo en «Analizar y calcular».':description);
    setText('workflow-download-status',labels[current]||activity.label);
    const progress=$('workflow-entity-progress');progress.max=Math.max(1,total);progress.value=Math.min(total,queried);progress.setAttribute('aria-valuetext',queried+' de '+total+' nombres con consulta web ejecutada. No representa resultados encontrados.');
    setText('workflow-entity-progress-label',format(queried)+' de '+format(total)+' nombres con consulta web ejecutada'+(shared?' · el recorrido de índices se mide por separado':''));
    setText('workflow-live-elapsed',downloading()&&Number.isFinite(state.operation.elapsed_seconds)?formatTime(state.operation.elapsed_seconds)+' de sesión':'');
    const delay=retryDelay(context),queue=state.operation.task_progress?.queue||{},estimated=timingEstimate(queue,current==='paused');
    setText('workflow-live-estimate',current==='waiting'?'La espera de la fuente no estima el tiempo total':current==='paused'||current==='resumable'?'Tiempo restante: se actualizará al reanudar':['completed','idle'].includes(current)?'':['measured','estimated'].includes(estimated.kind)?estimated.text:'Duración total: aún no estimable');
    const waiting=current==='waiting'||current==='external_worker'||current==='paused'&&delay>0,wait=$('workflow-live-wait');wait.hidden=!waiting;
    const source=activity.source_label||activity.source_id,stamp=Number.isFinite(activity.next_retry_at)?new Date(activity.next_retry_at*1000):delay!==null?new Date(Date.now()+delay*1000):null;
    const retry=stamp&&Number.isFinite(stamp.getTime())?'Próximo intento permitido: '+stamp.toLocaleString('es-MX',{dateStyle:'medium',timeStyle:'medium'})+(delay>0?' · en ≈ '+formatTime(delay):''):current==='external_worker'?'Espera a que el otro proceso libere su tarea.':'La fuente no informó una hora de reintento.';
    setText('workflow-live-wait',(source?source+' · ':'')+retry+(activity.last_error?' · '+activity.last_error:''));
    const empty=!context.local&&context.attempted&&data.current_bank_index_ready!==false&&candidates===0&&bodies===0;
    $('workflow-zero-results').hidden=!empty;
    setText('workflow-zero-description',shared&&Number(data.shared_pages_read||0)>0?'Se leyeron '+format(data.shared_pages_read)+' páginas de índices, pero no aportaron coincidencias para tu lista. Busca directamente el nombre de cada persona para ampliar la cobertura.':queried?'Las consultas completadas aún no aportaron coincidencias admisibles. Puedes ampliar las fuentes o ajustar el período.':'Todavía no se ha completado una consulta web por nombre. Las comparaciones con índices y las referencias de otras búsquedas no cuentan como resultados de esta lista.');
    const recovery=$('workflow-targeted-recovery'),showRecovery=empty&&(shared||queried===0);recovery.hidden=!showRecovery;recovery.textContent=state.recovering?'Cambiando a búsqueda por nombre…':context.workerActive?'Pausar y buscar por nombre':'Buscar por nombre';
    setButton('workflow-targeted-recovery',!state.recovering&&!state.requesting&&state.searchLoaded&&cacheMode()!=='local',state.recovering?'Esperando a que termine la solicitud actual.':cacheMode()==='local'?'Cambia «Sólo caché local» para permitir consultas web.':'Al continuar, permites consultar las fuentes ya autorizadas. Conserva el período y el avance.','workflow-targeted-reason');
    setText('workflow-register-count',format(candidates)+' referencias · '+format(bodies)+' textos');
    setText('workflow-inspection-summary',format(activity.active_requests||0)+' solicitudes activas'+(context.pending?' · '+format(context.pending)+' pendientes':''));
    const pauseAnalyze=action('pause_and_analyze');$('workflow-pause-analyze').hidden=!pauseAnalyze.enabled;setButton('workflow-pause-analyze',!state.requesting&&pauseAnalyze.enabled,pauseAnalyze.reason);
    $('news-acquire').hidden=!!(state.operation.busy&&downloading());$('news-acquire').classList.toggle('primary',!showRecovery);
    if(showRecovery&&!state.operation.busy)$('news-acquire').textContent=shared?'Reanudar el índice guardado':$('news-acquire').textContent;
    if(current==='waiting')setText('workflow-download-lock','Puedes pausar la espera'+(showRecovery?' o cambiar a búsqueda por nombre':'')+'. El avance se conserva.');
    else if(current==='preparing'||current==='running')setText('workflow-download-lock','Puedes solicitar una pausa; se confirmará al terminar la petición actual.');
    else if(current==='pausing')setText('workflow-download-lock','Espera la confirmación de la pausa antes de cambiar la búsqueda.');
    if(context.preparationIncomplete&&!context.workerActive&&!context.coverage.closed_partial){
      setText('workflow-download-card-title','Falta completar la búsqueda');setText('workflow-download-note','La búsqueda aún no está completa. La sesión terminó antes de preparar todas las consultas; el avance está guardado.');
      setText('workflow-download-status','Preparación pendiente');setText('workflow-download-current','La sesión terminó antes de completar el índice o el plan de consultas.');setText('workflow-download-wait','El plan sigue incompleto; inicia otra sesión o cambia a búsqueda por nombre.');
    }
    $('workflow-download-estimate').hidden=true;
  }
  function renderDownloadActivity() {
    if(!state.workflow)return;
    const context=downloadContext(),{coverage,pending,workerActive,local,stopReason}=context;
    $('workflow-download-activity').hidden=local;
    const activity=currentActivity(context),scoped=activity.current_bank_tasks||coverage;
    const completed=scoped.done??scoped.completed;
    setText('workflow-tasks-completed',Number.isFinite(completed)?completed.toLocaleString('es-MX'):'—');
    for(const [id,key] of [['running',scoped.active_running!==undefined?'active_running':'running'],['queued','queued'],['deferred','deferred'],['failed','failed']])setText('workflow-tasks-'+id,Number(scoped[key]||0).toLocaleString('es-MX'));
    if(Number.isFinite(activity.active_requests))setText('workflow-tasks-running',activity.active_requests.toLocaleString('es-MX'));
    setText('workflow-tasks-retained',pending.toLocaleString('es-MX'));
    const reasons={time_limit:'La sesión alcanzó su límite de tiempo. El avance está guardado y puedes reanudar las tareas pendientes.',job_limit:'La sesión alcanzó su límite de tareas. Puedes continuar la descarga sin repetir las consultas terminadas.',cancelled:'La descarga se pausó por solicitud. Puedes retomarla o continuar con la información disponible.',backoff:'Las fuentes han pedido una pausa entre solicitudes. Las tareas esperan su próximo intento permitido.',scheduled:'Hay tareas programadas para un próximo intento. La información descargada permanece guardada.',external_lease:'Otro trabajador mantiene una descarga activa. Espera a que termine antes de analizar o cerrar la cobertura.'};
    const op=state.operation;
    let description=activity.detail||reasons[stopReason]||'Las tareas corresponden a la lista actual; una tarea puede contener varias referencias.';
    if(!op.busy&&!workerActive&&coverage.needs_new_search&&!context.preparationIncomplete)description='El banco o el alcance cambió. Inicia una nueva búsqueda para aplicar el plan actual; las tareas anteriores se conservarán por separado.';
    setText('workflow-download-current',description);
    setText('workflow-queue-state',activity.label||{waiting:'Esperando a las fuentes',running:'Consultando',preparing:'Preparando',paused:'En pausa',resumable:'Pendientes guardadas',completed:'Sesión terminada',pausing:'Confirmando pausa',external_worker:'Otro proceso activo'}[activity.state]||'Sin actividad');
    const delay=retryDelay(context);
    setText('workflow-download-wait',delay!==null&&delay>0?'Próximo intento permitido en aproximadamente '+formatTime(delay)+(workerActive?'. El proceso está abierto y respetará esa espera.':'. Reanuda para procesar las tareas disponibles.'):activity.state==='running'?Number(activity.active_requests||0)+' solicitudes en curso.':pending?'Hay '+pending.toLocaleString('es-MX')+' tareas pendientes guardadas.':'No hay tareas pendientes de esta lista.');
    const session=activity.session_tasks;setText('workflow-task-scope','Tareas de la lista actual. '+(Number.isFinite(session?.completed)?'Esta sesión completó '+session.completed.toLocaleString('es-MX')+' tareas; el total de arriba incluye sesiones anteriores de la misma lista. ':'')+'Las tareas son consultas o descargas; no son noticias encontradas.');
    const issues=$('workflow-download-issues');
    const notes=[];
    if(Number(scoped.deferred||0))notes.push(Number(scoped.deferred).toLocaleString('es-MX')+' tareas de esta lista esperan una pausa de la fuente o un reintento.');
    if(Number(scoped.expired_running||0))notes.push(Number(scoped.expired_running).toLocaleString('es-MX')+' tareas de esta lista tienen un turno vencido; no representan una solicitud activa.');
    if(Number(scoped.failed||0))notes.push(Number(scoped.failed).toLocaleString('es-MX')+' tareas de esta lista registran errores o bloqueos y limitan su cobertura.');
    for(const item of scoped.reasons||[])if(typeof item==='string')notes.push(item);else if(item?.reason)notes.push(item.reason+(item.count?' ('+item.count+')':''));
    const notesKey=JSON.stringify(notes);if(issues.dataset.notesKey!==notesKey){issues.dataset.notesKey=notesKey;issues.replaceChildren();for(const note of notes){const li=document.createElement('li');li.textContent=note;issues.append(li);}}
    issues.hidden=!notes.length;
  }
  function searchPayload() {
    if(!$('workflow-search-form').reportValidity())return null;
    const start=$('workflow-history-start').value,end=$('workflow-history-end').value;
    if(start>end){state.hooks.message('La fecha inicial debe ser anterior o igual a la fecha final.',true);$('workflow-history-start').focus();return null;}
    const execution_mode=$('workflow-profile-quick').checked?'quick':'extended';
    return {strategy:$('workflow-search-strategy').value,preset:$('workflow-search-preset').value,start,end,execution_mode,
      session_seconds:execution_mode==='quick'?120:Number($('workflow-time-budget').value),
      window:$('workflow-history-window').value,max_queries:Number($('workflow-search-max-queries').value),
      include_aliases:$('workflow-search-aliases').checked,use_context:$('workflow-search-context').checked};
  }
  function renderBudgetChoice() {
    const quick=$('workflow-profile-quick').checked;
    const shared=$('workflow-search-strategy').value==='shared';
    $('workflow-time-budget-field').hidden=quick;
    $('workflow-time-budget').disabled=quick||$('workflow-search-fields').disabled;
    for(const mode of ['quick','extended'])$('workflow-profile-'+mode).closest('label').classList.toggle('selected',$('workflow-profile-'+mode).checked);
    setText('workflow-budget-preview',(quick?'Hasta 2 minutos de descarga en esta sesión.':'Hasta '+formatTime(Number($('workflow-time-budget').value))+' de descarga en esta sesión.')+(shared?' Cada página se contrasta con toda la lista en tu equipo. El límite acota las páginas y los textos que se pueden leer; no garantiza recorrer todo el archivo.':' El límite no garantiza consultar toda la lista ni encontrar evidencia puntuable.')+' El avance se conserva para reanudar.');
    $('workflow-shared-strategy-note').hidden=!shared;
    window.pepsOrderNewsSources?.();
    $('workflow-search-preset').disabled=$('workflow-search-fields').disabled||['feeds','shared'].includes($('workflow-search-strategy').value);
  }
  function renderAcquisition() {
    const local=state.workflow?.mode==='local',data=state.acquisition,permissions=data?.permissions;
    const showEntitySummary=!local&&!!data&&data.current_bank_index_ready!==false&&Number.isFinite(data.entities_with_bodies);
    $('workflow-summary-entities').hidden=!showEntitySummary;
    $('workflow-summary-entities').closest('article').dataset.entityCoverage=String(showEntitySummary);
    if(showEntitySummary){setText('workflow-summary-entity-count',data.entities_with_bodies.toLocaleString('es-MX'));setText('workflow-summary-entity-total','de '+Number(data.entities_total||0).toLocaleString('es-MX'));}
    const permissionBox=$('workflow-permission-summary');permissionBox.hidden=local||!permissions;
    if(permissions){const prepared=Number(permissions.prepared_sources||0),bodies=Number(permissions.body_authorized_sources||0),discovery=Number(permissions.discovery_only_sources||0);permissionBox.dataset.bodyReady=String(bodies>0);setText('workflow-permission-summary',prepared+' '+(prepared===1?'fuente preparada':'fuentes preparadas')+' · '+bodies+' con texto autorizado'+(discovery?' · '+discovery+' sólo de descubrimiento':'')+'. '+(!prepared?'Prepara al menos una fuente y revisa sus permisos antes de continuar.':bodies?'Los permisos se comprueban de nuevo al conservar cada artículo.':'La configuración actual sólo permite descubrir referencias; no autoriza conservar cuerpos para el análisis.'));}
    const noBodies=!local&&!!permissions&&Number(permissions.prepared_sources||0)>0&&Number(permissions.body_authorized_sources||0)===0;
    $('workflow-session-permission-warning').hidden=!noBodies;
    if(noBodies)setText('workflow-session-permission-warning','No hay fuentes con texto autorizado. Esta sesión sólo puede descubrir referencias; no aportarán el cuerpo original para el score. Revisa «Fuentes y permisos» si cuentas con autorización para conservar el contenido.');
    $('workflow-entity-coverage').hidden=local||!data;
    if(data){const total=Number(data.entities_total||0),label=value=>Number.isFinite(value)?value.toLocaleString('es-MX')+' de '+total.toLocaleString('es-MX'):'Aún no disponible';setText('workflow-entities-queried',label(data.entities_queried));setText('workflow-entities-candidates',label(data.entities_with_candidates));setText('workflow-entities-bodies',label(data.entities_with_bodies));const counts=[];for(const [key,text] of [['relevant_articles','referencias pertinentes'],['body_articles','textos conservados'],['metadata_only_articles','referencias sin cuerpo']])if(Number.isFinite(data[key])&&(key!=='metadata_only_articles'||data[key]>0))counts.push(data[key].toLocaleString('es-MX')+' '+text);setText('workflow-relevant-articles',counts.join(' · '));}
    if(data?.current_bank_index_ready===false){for(const id of ['workflow-entities-queried','workflow-entities-candidates','workflow-entities-bodies'])setText(id,'Por actualizar');setText('workflow-relevant-articles','Se actualizarán las coincidencias al iniciar la búsqueda. La información histórica se conserva.');}
    const shared=state.searchConfig.strategy==='shared'||Number(state.searchPlan.shared_discovery?.sources||0)>0||Number(data?.shared_pages_read||0)>0||Number(data?.shared_pages_pending||0)>0;
    $('workflow-shared-coverage').hidden=local||!data||!shared;
    $('workflow-index-details').hidden=local||!data||!shared;
    if(data&&shared){
      const read=Number(data.shared_pages_read||0),pending=Number(data.shared_pages_pending||0),known=read+pending,total=Number(data.entities_total||0),checked=Number(data.entities_checked_locally||0);
      setText('workflow-shared-pages',read.toLocaleString('es-MX'));
      setText('workflow-shared-records',Number(data.shared_records_checked||0).toLocaleString('es-MX'));
      setText('workflow-shared-entities',checked.toLocaleString('es-MX')+' de '+total.toLocaleString('es-MX'));
      setText('workflow-shared-progress-label',read?read.toLocaleString('es-MX')+' páginas leídas · '+pending.toLocaleString('es-MX')+' pendientes conocidas':pending?pending.toLocaleString('es-MX')+' páginas pendientes conocidas':'Todavía no se han leído páginas del índice.');
      const progress=$('workflow-shared-progress');progress.max=Math.max(1,known);progress.value=read;progress.setAttribute('aria-valuetext',read+' páginas leídas y '+pending+' pendientes conocidas. No representa todo el archivo.');
      $('workflow-shared-limit-notice').hidden=Number(data.shared_pages_limited||0)===0;
    }
    const budget=$('workflow-session-budget');budget.hidden=local;
    if(local)return;
    const execution=state.searchPlan.execution||{},config=state.searchConfig,seconds=execution.session_seconds??config.session_seconds,mode=execution.mode||config.execution_mode;
    if(!Number.isFinite(seconds)){setText('workflow-session-budget','Cargando el presupuesto de la sesión…');return;}
    if(config.strategy==='shared'||Number(state.searchPlan.shared_discovery?.sources||0)>0&&!Number(state.searchPlan.entities_planned||0)){
      const index=state.searchPlan.shared_discovery||{},sources=Number(index.sources||0);
      setText('workflow-session-budget',(mode==='quick'?'Rápido':'Ampliado')+' · hasta '+formatTime(seconds)+' por sesión. Índice compartido: '+sources.toLocaleString('es-MX')+' '+(sources===1?'fuente preparada':'fuentes preparadas')+'. Cada página se filtra contra toda la lista y se reutilizan metadatos ya descargados. Sólo los candidatos pertinentes pasan a la descarga de texto; recorrer parte del índice no equivale a cubrir todo el período.');return;
    }
    const planned=state.searchPlan.entities_planned,total=state.searchPlan.entities_total,firstSession=state.searchPlan.estimated_entities_first_session;
    const estimate=Number.isFinite(firstSession)&&Number.isFinite(total)?' Primera sesión: hasta '+firstSession.toLocaleString('es-MX')+' de '+total.toLocaleString('es-MX')+' entidades según la programación; las respuestas y la descarga de texto pueden reducir ese alcance.':'';
    setText('workflow-session-budget',(mode==='quick'?'Rápido':'Ampliado')+' · hasta '+formatTime(seconds)+' por sesión.'+estimate+(Number.isFinite(planned)&&Number.isFinite(total)?' Plan completo: '+planned.toLocaleString('es-MX')+' de '+total.toLocaleString('es-MX')+' entidades.':'')+' Las tareas pendientes se guardan para reanudar; el plan no equivale a cobertura completada.');
  }
  function searchDate(value) {
    const text=String(value||'');
    if(/^\d{8,14}$/.test(text))return text.slice(6,8)+'/'+text.slice(4,6)+'/'+text.slice(0,4);
    if(/^\d{4}-\d{2}-\d{2}/.test(text))return text.slice(8,10)+'/'+text.slice(5,7)+'/'+text.slice(0,4);
    return text;
  }
  function renderSearch(response) {
    const config=response.config||{},plan=response.plan||{};
    state.searchConfig=config;state.searchPlan=plan;
    const mode=config.execution_mode||'extended';$('workflow-profile-quick').checked=mode==='quick';$('workflow-profile-extended').checked=mode!=='quick';
    const seconds=Number(config.session_seconds||900);$('workflow-time-budget').querySelector('option[value="900"]').hidden=seconds!==900;$('workflow-time-budget').value=String(mode==='quick'?1800:seconds);
    for(const [id,key] of [['workflow-search-strategy','strategy'],['workflow-search-preset','preset'],['workflow-history-start','start'],['workflow-history-end','end'],['workflow-history-window','window'],['workflow-search-max-queries','max_queries']])if(config[key]!==undefined)$(id).value=String(config[key]);
    for(const [id,key] of [['workflow-search-aliases','include_aliases'],['workflow-search-context','use_context']])if(config[key]!==undefined)$(id).checked=!!config[key];
    renderBudgetChoice();renderAcquisition();
    state.searchLoaded=true;state.searchDirty=false;
    const box=$('workflow-search-plan');box.replaceChildren();
    const summary=document.createElement('p');summary.className='search-plan-summary';
    const measures=[],sharedPlan=plan.shared_discovery||{};
    const measured={entities:plan.entities_planned??plan.entities,queries:plan.query_count??(Array.isArray(plan.queries)?plan.queries.length:plan.queries),windows:plan.windows??(Array.isArray(plan.sources)?plan.sources.reduce((sum,source)=>sum+Number(source.windows||0),0):undefined)};
    if(config.strategy==='shared'){
      const shared=plan.shared_discovery||{};
      if(Number.isFinite(shared.sources))measures.push(shared.sources.toLocaleString('es-MX')+' '+(shared.sources===1?'fuente':'fuentes')+' de índice compartido');
      if(Number.isFinite(shared.initial_pages))measures.push(shared.initial_pages.toLocaleString('es-MX')+' páginas iniciales');
      if(Number.isFinite(plan.entities_total))measures.push('Cada página se contrasta con '+plan.entities_total.toLocaleString('es-MX')+' entidades');
    }else{
      if(config.strategy!=='feeds'&&Number.isFinite(measured.entities))measures.push(measured.entities.toLocaleString('es-MX')+(Number.isFinite(plan.entities_total)?' de '+plan.entities_total.toLocaleString('es-MX'):'')+' entidades incluidas');
      for(const [key,label] of [['queries',config.strategy==='feeds'?'feeds previstos':Number(sharedPlan.sources||0)>0?'consultas y páginas previstas':'consultas'],['windows','ventanas por fuente']])if(Number.isFinite(measured[key]))measures.push(measured[key].toLocaleString('es-MX')+' '+label);
      if(Number(sharedPlan.sources||0)>0)measures.push(sharedPlan.sources.toLocaleString('es-MX')+' fuentes de índice compartido');
    }
    summary.textContent=measures.length?measures.join(' · '):'La planeación muestra el alcance de las fuentes preparadas, sin iniciar descargas.';
    box.append(summary);
    const execution=plan.execution||{};
    if(Number.isFinite(execution.session_seconds)){const note=document.createElement('p');note.className='search-plan-estimate';note.textContent='Presupuesto guardado: '+(execution.mode==='quick'?'Rápido':'Ampliado')+' · hasta '+formatTime(execution.session_seconds)+' por sesión. '+(execution.scope_note||'La cobertura de la lista depende del ritmo y las respuestas de las fuentes; el avance pendiente se puede reanudar.');box.append(note);}
    if(config.strategy==='shared'||Number(sharedPlan.sources||0)>0){const shared=sharedPlan,note=document.createElement('p');note.className='search-plan-estimate';note.textContent=(Number.isFinite(shared.max_pages_per_source)?'Límite guardado: hasta '+shared.max_pages_per_source.toLocaleString('es-MX')+' páginas por fuente y recorrido. ':'')+(shared.scope_note||'Los filtros de fechas delimitan la búsqueda. El recorrido del índice puede quedar parcial; las páginas consultadas no equivalen a todo el archivo.')+' Reutiliza metadatos ya descargados al contrastar la lista, sin tratarlos como cuerpos de noticias ni como identidad confirmada.';box.append(note);}
    if(!['shared','feeds'].includes(config.strategy)&&Number.isFinite(plan.estimated_entities_first_session)){const note=document.createElement('p');note.className='search-plan-estimate';note.textContent='Primera sesión: aproximadamente '+plan.estimated_entities_first_session.toLocaleString('es-MX')+' entidades podrían consultarse según la programación del plan. '+(plan.first_session_estimate_note||'Es una estimación de alcance, no de hallazgos ni de cobertura completa.');box.append(note);}
    if(measured.queries>0&&Number.isFinite(plan.estimated_schedule_seconds)&&plan.estimated_schedule_seconds>0){
      const estimate=document.createElement('p');estimate.className='search-plan-estimate';
      estimate.textContent='Tiempo mínimo programado entre '+(Number(sharedPlan.sources||0)>0?'solicitudes':'consultas')+': '+formatTime(plan.estimated_schedule_seconds)+'. No incluye la duración ni los reintentos de las descargas.';box.append(estimate);
    }
    const sources=Array.isArray(plan.sources)?plan.sources:[];
    if(sources.length){
      const list=document.createElement('ul');list.className='search-effective-ranges';
      for(const source of sources){const item=document.createElement('li'),name=source.source_name||source.name||source.id;item.textContent=name+': '+(source.scope==='shared_publisher_index'?'índice compartido para toda la lista'+(source.start&&source.end_exclusive?' · desde '+searchDate(source.start)+' hasta antes del '+searchDate(source.end_exclusive):''):source.start&&source.end_exclusive?'desde '+searchDate(source.start)+' hasta antes del '+searchDate(source.end_exclusive):source.scope==='current_feed_only'?'actualizaciones del feed, sin búsqueda de archivo histórico':'alcance informado por la fuente')+'.';list.append(item);}box.append(list);
    }
    const examples=(Array.isArray(plan.queries)?plan.queries:[]).filter(query=>typeof query.query==='string'&&query.query.trim()).slice(0,3);
    if(examples.length){
      const details=document.createElement('details');details.id='workflow-query-examples';details.className='search-query-examples';const title=document.createElement('summary');title.textContent='Ejemplos de búsquedas';details.append(title);const list=document.createElement('ol');
      for(const query of examples){const item=document.createElement('li'),source=sources.find(candidate=>candidate.id===query.source_id),label=document.createElement('p');label.className='query-example-source';label.textContent=(query.source_name||source?.source_name||source?.name||query.source_id||'Fuente configurada')+(query.start&&query.end?' · desde '+searchDate(query.start)+' hasta antes del '+searchDate(query.end):'');const text=document.createElement('p');text.className='query-example-text';text.textContent=query.query;item.append(label,text);list.append(item);}details.append(list);box.append(details);
    }
    const warnings=[...(plan.warnings||[])];if(plan.truncated&&!warnings.some(item=>/presupuesto|límite de consultas/i.test(typeof item==='string'?item:item.message||'')))warnings.push('El plan alcanza el límite de consultas. Amplía el límite o acota el período para revisar la cobertura.');
    if(warnings.length){const list=document.createElement('ul');for(const warning of warnings){const item=document.createElement('li');item.textContent=typeof warning==='string'?warning:warning.message||warning.reason||'';list.append(item);}box.append(list);}
    const notes=[...box.children].slice(1);if(notes.length){const detail=document.createElement('details');detail.className='search-scope-details';const title=document.createElement('summary');title.textContent='Alcance y límites';detail.append(title,...notes);box.append(detail);}
    setText('workflow-search-status','Alcance guardado. Puedes iniciar la revisión.');
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
    const units={entities:'entidades',documents:'documentos',articles:'noticias',jobs:'tareas',urls:'enlaces',pages:'páginas',records:'registros'};
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
    if(queue&&downloading()){
      const estimate=timingEstimate(queue,!state.operation.busy&&state.operation.status==='paused');
      const next=queue.next_retry_seconds,context=downloadContext();
      if(context.preparationIncomplete&&!state.operation.busy&&!context.coverage.closed_partial){setText('workflow-operation-eta','Completa el plan en otra sesión o elige analizar sólo lo disponible.');return;}
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
    const operationTab=op.kind==='pipeline'?(['attribution','scoring'].includes(op.phase)?'analysis':'news'):{import:'import',document_import:'sources',download:'news',analysis:'analysis'}[op.kind];
    const completedHere=op.status==='completed'&&(operationTab===state.current||(['analysis','pipeline'].includes(op.kind)&&state.current==='bank'));
    const visible=!!op.busy||completedHere||['paused','error','failed','stopped'].includes(op.status);
    $('workflow-operation').hidden=!visible||state.current==='news'&&downloading(op);if(!visible)return;
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
    if(downloading(op)&&op.queue_snapshot_exact===false)detail+=' · '+(Number.isFinite(op.queue_snapshot_age_seconds)?'cola medida hace '+formatTime(Math.ceil(op.queue_snapshot_age_seconds)):'recuento de cola por actualizar');
    setText('workflow-operation-detail',detail);
    progress.setAttribute('aria-valuetext',percent!==null?Math.round(percent)+' por ciento. '+detail:detail+'. Total aún no disponible.');
    setText('workflow-operation-eta',op.busy?(Number.isFinite(op.eta_seconds)?'Tiempo restante estimado: '+formatTime(op.eta_seconds):'Tiempo restante: aún no estimable'):(op.status==='completed'?'Operación terminada':op.status==='paused'?'Puedes reanudar la descarga':'Consulta el estado antes de continuar'));
    $('workflow-operation-error').hidden=!op.error;
    setText('workflow-operation-error',op.error);
    if(downloading(op)&&state.workflow){const context=downloadContext(),delay=retryDelay(context);if(!op.busy&&context.coverage.closed_partial){$('workflow-operation').dataset.status='completed';setText('workflow-operation-label','Descarga cerrada con cobertura parcial');setText('workflow-operation-eta','Las tareas pendientes se conservan para reanudar después.');}else if(!op.busy&&context.preparationIncomplete){setText('workflow-operation-label','Preparación en pausa');setText('workflow-operation-eta','Completa el plan en otra sesión o elige analizar sólo lo disponible.');}else if(!op.busy&&context.pending){setText('workflow-operation-label','Descarga en pausa · '+context.pending.toLocaleString('es-MX')+' tareas pendientes');setText('workflow-operation-eta',delay!==null&&delay>0?'Próximo intento permitido en aproximadamente '+formatTime(delay):'Reanuda la descarga o continúa con cobertura parcial.');}else if(op.busy&&delay!==null&&delay>0&&(context.coverage.waiting||op.waiting||(op.last_result||op.result||{}).waiting)){setText('workflow-operation-eta','Espera de la fuente: próximo intento en aproximadamente '+formatTime(delay));}}
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
      const hints={data:Number(counts.entities||0)>0?Number(counts.entities).toLocaleString('es-MX')+(Number(counts.entities)===1?' entidad guardada':' entidades guardadas'):'Un nombre o una lista para comenzar',sources:state.workflow?.sources_confirmed?'Origen y permisos confirmados':'Elige origen y permisos',download:state.workflow?.mode==='local'?'Recorrido sin conexión':Number(state.acquisition?.entities_with_bodies||0)>0?Number(state.acquisition.entities_with_bodies).toLocaleString('es-MX')+' entidades con texto':Number(state.acquisition?.entities_queried||0).toLocaleString('es-MX')+' nombres consultados en la web',analysis:state.workflow?.analysis_current?'Evaluación actualizada':'Atribución y cálculo local',results:action('view_results').enabled?'Expedientes disponibles':'Explicaciones y revisión'};
      setText('step-reason-'+step.id,data?.status==='blocked'?'Completa los pasos anteriores':hints[step.id]||labels[data?.status]||'Pendiente');
      button.title=data?.reason||hints[step.id]||'';
      const progress=$('workflow-step-progress-'+step.id);
      const operationStep=state.operation.kind==='pipeline'?(['attribution','scoring'].includes(state.operation.phase)?'analysis':'download'):{import:'data',document_import:'sources',download:'download',acquire:'download',analysis:'analysis',analyze:'analysis',rescore:'analysis'}[state.operation.kind];
      const isOperation=operationStep===step.id;
      const phase=state.operation.phase||'';
      const phaseLabel={saving:'guardado',download:'descarga',attribution:'atribución',scoring:'cálculo del score'}[phase]||phase||'actual';
      const operationPercent=Number.isFinite(state.operation.total)&&state.operation.total>0&&Number.isFinite(state.operation.percent)?Math.max(0,Math.min(99,state.operation.percent)):null;
      if(data?.status==='complete') {
        progress.value=100;progress.setAttribute('aria-valuetext','Paso completado.');
      } else if(step.id==='download'&&isOperation&&state.acquisition?.current_bank_index_ready!==false&&Number.isFinite(state.acquisition?.entities_queried)&&Number(state.acquisition?.entities_total)>0){
        const queried=state.acquisition.entities_queried,total=state.acquisition.entities_total;progress.value=Math.min(99,100*queried/total);progress.setAttribute('aria-valuetext',queried+' de '+total+' nombres con consulta web ejecutada. '+(labels[data?.status]||'Búsqueda preparada')+'. No indica artículos encontrados.');
      } else if(isOperation&&['running','waiting','preparing','pausing','external_worker','paused','failed','error'].includes(data?.status)) {
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
    const opBusy=busy();const savingSources=Number(window.pepsSourcesPending?.()||0)>0;const mode=state.workflow.mode||'online';const local=mode==='local';const counts=state.workflow.counts||{};
    for(const name of ['online','local']) {
      const button=$('workflow-mode-'+name);const selected=mode===name;
      button.classList.toggle('selected',selected);button.setAttribute('aria-pressed',String(selected));button.disabled=opBusy||!action('configure_sources').enabled;
    }
    $('workflow-online-sources').hidden=local;$('workflow-local-documents').hidden=!local;
    setText('workflow-mode-status',local?'Modo local: la información se importa desde tu equipo. No se descargarán noticias.':'Modo con conexión: sólo se consultarán las fuentes que habilites y autorices.');
    const sourceCount=Number(counts.sources||0);setText('workflow-source-count',sourceCount+' '+(sourceCount===1?'fuente preparada':'fuentes preparadas'));
    setText('workflow-local-document-status',Number(counts.documents||0).toLocaleString('es-MX')+' documentos conservados. La importación no calcula el score.');
    $('workflow-sources-next').textContent=local?'Continuar con mis documentos':savingSources?'Guardando selección…':$('workflow-auto-analyze').checked?'Guardar e iniciar revisión':'Guardar e iniciar descarga';
    const prepared=Number(state.acquisition?.permissions?.prepared_sources??counts.sources??0),configured=prepared>0;
    const noQueries=state.searchLoaded&&!state.searchDirty&&Number(state.searchPlan.query_count)===0;
    const runReason=savingSources?'Guardando los permisos seleccionados…':!configured&&!local?'Selecciona al menos una fuente y revisa sus condiciones.':noQueries&&!local?'El alcance no tiene consultas. Ajusta las fuentes o las fechas.':!local&&!onlineAllowed()?'Autoriza la conexión de esta sesión o elige «Sólo caché local».':local?'Continúa con los documentos que ya conservaste.':$('workflow-auto-analyze').checked?'Descargará y analizará al terminar. Si queda cobertura parcial, te pedirá cómo continuar.':'Conservará la información sin iniciar el análisis.';
    setButton('workflow-sources-next',!opBusy&&!savingSources&&action('configure_sources').enabled&&!state.searchLoading&&(local||configured&&!noQueries&&onlineAllowed()),runReason,'workflow-sources-reason');
    const runOptions=$('workflow-run-options'),runSlot=$(state.current==='sources'?'workflow-source-run-options':'workflow-news-run-options');if(runOptions.parentElement!==runSlot)runSlot.append(runOptions);
    const manual=$('workflow-manual-discovery'),manualSlot=$(state.current==='news'?'workflow-manual-news-slot':'workflow-manual-source-slot');if(manual.parentElement!==manualSlot)manualSlot.append(manual);manual.hidden=local;
    runOptions.hidden=local;for(const field of runOptions.querySelectorAll('input,select'))field.disabled=opBusy||savingSources;
    const localCache=cacheMode()==='local';$('workflow-network-consent').hidden=local||localCache;
    setText('workflow-cache-explanation',localCache?'Sin solicitudes de red. Sólo copias locales; no se comprueba su vigencia en internet.':cacheMode()==='revalidate'?'Comprueba si las fuentes cambiaron, respetando sus pausas y condiciones. No borra la evidencia conservada.':'Reutiliza información guardada y consulta la red sólo cuando haga falta. Las fechas originales se conservan.');
    for(const field of document.querySelectorAll('#news-sources input,#sources input'))field.disabled=opBusy||!action('configure_sources').enabled||field.dataset.permissionSaving==='true';
    const context=downloadContext(),{coverage,pending,workerActive}=context;
    const consent=$('workflow-network-consent'),consentSlot=state.current==='news'?document.querySelector('.download-main-actions'):$('workflow-run-options');
    if(state.current==='news'){if(consent.parentElement!==$('workflow-search-live'))consentSlot.before(consent);}else if(consent.parentElement!==consentSlot)consentSlot.append(consent);
    $('workflow-network-consent').hidden=local||cacheMode()==='local'||context.canContinue&&!pending&&state.current!=='sources';
    $('workflow-search-fields').disabled=opBusy||state.searchLoading||!action('configure_sources').enabled;
    renderBudgetChoice();renderAcquisition();
    setButton('workflow-save-search',!opBusy&&!savingSources&&!state.searchLoading&&action('configure_sources').enabled);
    setButton('workflow-news-settings',!opBusy&&!workerActive&&action('configure_sources').enabled,opBusy?'Pausa la búsqueda antes de cambiar fuentes o período.':action('configure_sources').reason);
    $('workflow-search-preset').disabled=$('workflow-search-fields').disabled||['feeds','shared'].includes($('workflow-search-strategy').value);
    setText('workflow-download-mode',local?'Documentos locales':'Fuentes autorizadas');
    setText('workflow-download-card-title',local?'Tu evidencia ya está en el equipo':context.canContinue?'La información está lista para analizar':context.preparationIncomplete?'Falta completar la preparación':pending?'Tu avance está guardado':'Descargar noticias');
    setText('workflow-download-note',local?'Este recorrido utiliza los documentos que importaste. Puedes continuar al análisis sin realizar una descarga.':context.preparationIncomplete?'La sesión terminó antes de completar el índice o el plan de consultas. La búsqueda aún no está completa; la información ya conservada sigue disponible.':coverage.needs_new_search?'Cambió el banco o el alcance de la búsqueda. Se preparará el plan nuevo y se conservarán las tareas anteriores, sin presentarlas como consultas del alcance actual.':pending?pending.toLocaleString('es-MX')+' tareas siguen pendientes. Reanuda desde ese punto o cierra la descarga con la cobertura ya disponible.':'La conexión consulta las fuentes y el período que preparaste. La identidad se revisará durante el análisis.');
    setText('workflow-news-count',Number(local?state.acquisition?.local_documents||0:state.acquisition?.relevant_articles||0).toLocaleString('es-MX'));
    setText('workflow-news-count-label',local?'documentos locales conservados':'referencias pertinentes de esta lista');
    setText('workflow-coverage-note',local?'Los documentos se analizarán en tu equipo.':Number(state.acquisition?.metadata_only_articles||0).toLocaleString('es-MX')+' referencias pertinentes aún no tienen cuerpo. Los totales históricos del banco no cuentan como resultados de esta lista.');
    const download=action('download');
    const downloadReason=workerActive?'Hay un trabajador activo. Espera a que termine o solicita la pausa.':local?'Modo local: no necesitas descargar noticias.':opBusy?'Hay una operación en curso. Puedes consultar su actividad y progreso.':context.canContinue?(coverage.accepted_partial?'Continuarás con cobertura parcial; las tareas restantes se conservan.':'La evidencia disponible está lista para analizar.'):!download.enabled?download.reason:!onlineAllowed()?'Marca el permiso de conexión para iniciar o reanudar la descarga.':context.preparationIncomplete?'Inicia otra sesión con el alcance actual para completar la preparación. Se conservan las tareas y la información guardada.':coverage.needs_new_search?'Se aplicará el plan actual y se conservará la cola anterior. Las tareas ya guardadas no se eliminan.':pending?'Se retomarán las tareas pendientes sin volver a crear las consultas terminadas.':'Sólo se consultarán las fuentes y el alcance que preparaste.';
    $('news-acquire').dataset.downloadAction=context.canContinue?'continue':context.preparationIncomplete?'prepare':coverage.needs_new_search?'start_new':pending?'resume':'start';
    $('news-acquire').textContent=context.canContinue?($('workflow-auto-analyze').checked?'Analizar y ver resultados':'Continuar al análisis'):opBusy&&downloading()?'Descarga en curso…':context.preparationIncomplete?'Completar preparación':coverage.needs_new_search?'Iniciar nueva búsqueda':pending?'Reanudar descarga':'Iniciar descarga';
    setButton('news-acquire',context.canContinue||!local&&!opBusy&&!workerActive&&download.enabled&&onlineAllowed()&&state.searchLoaded&&!state.searchLoading,state.searchLoading?'Cargando el presupuesto guardado antes de iniciar.':downloadReason,'workflow-download-lock');
    const pause=$('stop'),pauseSlot=state.operation.busy&&state.current!=='news'?$('workflow-operation-controls'):document.querySelector('.download-main-actions');if(pause.parentElement!==pauseSlot)pauseSlot.append(pause);pause.hidden=!state.operation.busy;pause.textContent=state.operation.status==='cancel_requested'?'Confirmando pausa…':'Pausar proceso';setButton('stop',!!state.operation.busy&&state.operation.status!=='cancel_requested');
    $('news-online').disabled=opBusy||workerActive;
    const canResumeLater=!local&&pending>0&&!!coverage.accepted_partial&&coverage.resume_available!==false;
    $('workflow-resume-download').hidden=!canResumeLater;
    $('workflow-resume-note').hidden=!canResumeLater;
    setButton('workflow-resume-download',canResumeLater&&!opBusy&&!workerActive&&download.enabled&&onlineAllowed(),workerActive?'Hay un trabajador activo.':!onlineAllowed()?'Marca el permiso de conexión para reanudar.':'Ampliar la cobertura requiere volver a analizar; los resultados históricos se conservan.','workflow-resume-note');
    const downloadState=serverStep('news');setText('workflow-download-status',local?'Sin descarga de red':coverage.closed_partial?'Cobertura parcial aceptada':workerActive||state.operation.busy&&downloading()?'Descarga activa':context.preparationIncomplete?'Preparación pendiente':pending?'En pausa · '+pending.toLocaleString('es-MX')+' pendientes':downloadState?.status==='ready'?'Preparado para descargar':labels[downloadState?.status]||'Esperando descarga');
    const partial=state.workflow.actions?.analyze_available||action('accept_partial'),material=context.activity?.material,hasText=Number(material?.analyzable_documents??((state.acquisition?.body_articles||0)+(state.acquisition?.local_documents||0)))>0;
    $('workflow-partial-warning').hidden=local||!hasText||!context.attempted||(!coverage.partial&&!pending)||!!coverage.accepted_partial||workerActive;
    setText('workflow-partial-description',(context.preparationIncomplete?'El plan de búsqueda quedó incompleto. ':'')+'Se analizará únicamente la información que ya está guardada. '+(pending?pending.toLocaleString('es-MX')+' tareas pendientes se conservarán para reanudar después. ':'')+'Los resultados indicarán la cobertura parcial.');
    $('workflow-accept-partial').textContent='Analizar lo disponible';
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
    renderPreflightEstimates();renderSessionMonitor();renderSearchLive();for(const retry of document.querySelectorAll('[data-task-retry]'))retry.disabled=opBusy;
    if(window.pepsImport?.syncButtons)window.pepsImport.syncButtons();
    renderPrimary();
  }
  const metricNodes=new Map();let taskOffset=0,taskRequest=0;
  function renderSessionMonitor(){
    const data=state.acquisition||{},session=data.session,box=$('workflow-session-monitor');box.hidden=state.workflow?.mode==='local';
    const metrics=$('workflow-session-metrics');
    for(const [key,label] of [['network_requests','Solicitudes de red'],['http_cache_hits','Reusos de caché HTTP'],['metadata_cache_hits','Índices reutilizados'],['bodies_new','Textos nuevos'],['bodies_reused','Textos reutilizados'],['duplicate_candidates','Candidatos repetidos'],['excluded_records','Registros ajenos omitidos']]){
      let node=metricNodes.get(key);if(!node){const cell=document.createElement('div'),title=document.createElement('dt');node=document.createElement('dd');title.textContent=label;cell.append(title,node);metrics.append(cell);metricNodes.set(key,node);}const value=session?.[key];node.textContent=typeof value==='number'&&Number.isFinite(value)?value.toLocaleString('es-MX'):'—';
    }
    const started=session?.started_at?new Date(session.started_at):null;
    const startedLabel=started&&!Number.isNaN(started.getTime())?started.toLocaleString('es-MX'):'fecha no disponible';
    const live=!!session&&!session.finished_at&&state.operation.busy&&downloading();
    setText('workflow-session-scope',!session?'Aún sin medición':live?'Sesión en curso: '+startedLabel:'Última sesión: '+startedLabel+(session.finished_at?' · finalizada':' · sin cierre registrado'));
    const localSession=session?.cache_mode==='local';
    const bankNote=data.session_matches_bank===false?'La sesión corresponde a una lista anterior. ':data.session_matches_bank===true?'La sesión corresponde a la lista actual. ':'';
    setText('workflow-session-freshness',bankNote+(localSession?'Sólo copias locales; no se comprobó su vigencia en internet. ':'')+'Los contadores corresponden a '+(session?'esa sesión':'una sesión medida')+'. «—» indica que no hay medición disponible.');
    const sources=Array.isArray(data.source_breakdown)?data.source_breakdown:Array.isArray(session?.source_breakdown)?session.source_breakdown:[];
    const container=$('workflow-source-activity'),key=JSON.stringify(sources);if(container.dataset.value===key)return;container.dataset.value=key;container.replaceChildren();
    for(const source of sources){const row=document.createElement('div');row.className='source-activity-row';const name=document.createElement('strong'),detail=document.createElement('span');name.textContent=source.label||source.source_name||source.id||'Fuente';const status={running:'En curso',waiting:'En espera',paused:'En pausa',completed:'Terminada',error:'Con error',idle:'Sin actividad'}[source.state]||source.state||'Sin actividad';detail.textContent=status+(Number.isFinite(source.pending)?' · '+source.pending+' pendientes':'')+(Number.isFinite(source.failed)&&source.failed?' · '+source.failed+' con error':'');row.append(name,detail);container.append(row);}
  }
  async function loadTasks(reset=false){
    if(reset)taskOffset=0;const request=++taskRequest,filter=$('workflow-task-filter').value;
    $('workflow-task-refresh').disabled=true;setText('workflow-task-status','Consultando tareas guardadas…');
    try{const data=await state.hooks.api('news-tasks?scope=current_bank&limit=25&offset='+taskOffset+(filter?'&status='+encodeURIComponent(filter):''));if(request!==taskRequest)return;const items=Array.isArray(data.items)?data.items:Array.isArray(data.tasks)?data.tasks:[];
      const container=$('workflow-task-list');container.replaceChildren();for(const task of items){const row=document.createElement('article');row.className='queue-task';const title=document.createElement('strong'),detail=document.createElement('p');title.textContent=task.label||task.source_id||task.kind||'Tarea';const status={running:'En curso',queued:'En cola',deferred:'En espera',blocked:'Bloqueada',dead:'Con error',done:'Terminada'}[task.status]||task.status;detail.textContent=status+' · '+Number(task.attempts||0)+' intentos'+(Number.isFinite(task.next_retry_seconds)&&task.next_retry_seconds>0?' · próximo intento en ≈ '+formatTime(task.next_retry_seconds):'');row.append(title,detail);if(task.error){const note=document.createElement('p');note.className='muted';note.textContent=task.error;row.append(note);}if(task.metadata_warning){const note=document.createElement('p');note.className='muted';note.textContent=task.metadata_warning;row.append(note);}if(['blocked','dead'].includes(task.status)){const retry=document.createElement('button');retry.type='button';retry.dataset.taskRetry=task.id;retry.textContent='Reintentar esta tarea';retry.disabled=busy();retry.onclick=()=>perform(async()=>{const retried=await state.hooks.api('news-retry',{job_ids:[task.id]});await loadTasks(false);state.hooks.message(Number(retried.requeued??retried.retried)>0?'Tarea reencolada. Reanuda para procesarla; se respetan las pausas de la fuente.':'La tarea cambió de estado y no se reencoló. Actualiza la lista antes de volver a intentarlo.');});row.append(retry);}container.append(row);}
      if(!items.length){const empty=document.createElement('p');empty.className='muted';empty.textContent='No hay tareas en este filtro.';container.append(empty);}
      setText('workflow-task-page',Math.floor(taskOffset/25)+1);$('workflow-task-previous').disabled=taskOffset===0;$('workflow-task-next').disabled=typeof data.total==='number'?taskOffset+items.length>=data.total:items.length<25;setText('workflow-task-status',items.length+' tareas mostradas. Las esperas respetan los límites de cada fuente.');
    }catch(error){if(request===taskRequest)setText('workflow-task-status','No se pudieron consultar las tareas: '+error.message);}
    finally{if(request===taskRequest)$('workflow-task-refresh').disabled=false;}
  }
  function onNavigate(tab) {
    if(state.current!==tab)$('message').hidden=true;
    state.current=tab;document.body.dataset.workflowTab=tab;renderActions();
    try { sessionStorage.setItem('peps-workflow-tab',tab); } catch {}
    renderNavigation();renderPrimary();renderOperation();
    if(tab==='sources'||tab==='news'&&!state.searchLoaded)loadSearch();
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
    if(context.local||busy()||context.workerActive||!action('download').enabled||!onlineAllowed()||!state.searchLoaded||state.searchLoading)return;
    await window.pepsWaitSources?.();
    await perform(async()=>{await state.hooks.api('workflow-run',{online:cacheMode()!=='local',resume,cache_mode:cacheMode(),analyze_after_download:$('workflow-auto-analyze').checked});state.hooks.message(resume?'Reanudando el trabajo guardado.':'Proceso iniciado. Puedes seguir las fases y la actividad de cada fuente.');});
  }
  async function directedRecovery() {
    if(state.recovering||state.requesting||cacheMode()==='local'||!state.searchLoaded)return;
    // This explicitly labeled recovery action authorizes this connection; source permissions stay unchanged.
    $('news-online').checked=true;
    state.recovering=true;renderSearchLive();
    try {
      if(downloadContext().workerActive||state.operation.busy){
        const operation_id=state.operation.operation_id||state.operation.id;
        if(!operation_id||!downloading())throw Error('Espera a que termine la operación actual antes de cambiar la búsqueda.');
        await state.hooks.api('stop',{operation_id});
        const deadline=Date.now()+45000;
        while(true){const stats=await state.hooks.api('stats');update(stats);const activity=stats.workflow?.activity||stats.operation?.activity;if(!stats.operation?.busy&&!activity?.owner_active&&!Number(activity?.active_requests||0))break;if(Date.now()>deadline)throw Error('La pausa sigue pendiente. La consulta actual debe terminar antes de cambiar el enfoque; tu avance está guardado.');await new Promise(resolve=>setTimeout(resolve,250));}
      }
      if(!action('configure_sources').enabled)throw Error(action('configure_sources').reason||'La búsqueda todavía no está disponible para cambios.');
      const config={...state.searchConfig,strategy:'targeted',preset:'identity',max_queries:Math.max(Number(state.searchConfig.max_queries||1000),Number(state.acquisition?.entities_total||0))};
      const response=await state.hooks.api('news-search',config);renderSearch(response);
      if(Number(response.plan?.query_count||0)===0){await navigate('sources');state.hooks.message('La búsqueda por nombre está preparada. Selecciona una fuente que admita consultas dirigidas y revisa sus permisos.');return;}
      await state.hooks.api('workflow-run',{online:cacheMode()!=='local',resume:false,cache_mode:cacheMode(),analyze_after_download:$('workflow-auto-analyze').checked,search:config});
      await state.hooks.refresh(true);state.hooks.message('Búsqueda por nombre iniciada. Los textos y las tareas anteriores se conservan por separado.');
    } catch(error){state.hooks.message(error.message,true);}
    finally{state.recovering=false;renderActions();}
  }
  async function confirmSources() {
    if(!action('configure_sources').enabled||state.searchLoading||busy())return;
    await window.pepsWaitSources?.();
    const config=state.searchDirty?searchPayload():null;if(state.searchDirty&&!config)return;
    await perform(async()=>{
      if(state.workflow?.mode==='local'){await state.hooks.api('workflow-confirm-sources',{});await state.hooks.refresh(true);if(canOpenTab('news'))await navigate('news');return;}
      const payload={online:cacheMode()!=='local',resume:false,cache_mode:cacheMode(),analyze_after_download:$('workflow-auto-analyze').checked};if(config)payload.search=config;
      await state.hooks.api('workflow-run',payload);state.searchDirty=false;await state.hooks.refresh(true);if(canOpenTab('news'))await navigate('news');
    });
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
    if(stats.acquisition)state.acquisition=stats.acquisition;
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
    if(op.busy&&id&&op.kind==='pipeline'&&['attribution','scoring'].includes(op.phase)&&state.current==='news'&&state.analysisNavigationOperation!==id&&canOpenTab('analysis')){
      state.analysisNavigationOperation=id;navigate('analysis').catch(error=>state.hooks.message(error.message,true));
    }
    if(id&&id!==state.finishedOperation&&!op.busy&&op.status==='completed'&&state.observedOperation===id) {
      state.finishedOperation=id;
      if(['pipeline','analysis'].includes(op.kind)&&action('view_results').enabled)navigate('bank').catch(error=>state.hooks.message(error.message,true));
      else if(state.current==='news'&&canOpenTab('analysis'))navigate('analysis').catch(error=>state.hooks.message(error.message,true));
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
    if(state.initialized)return;state.initialized=true;state.hooks=hooks;try{if(sessionStorage.getItem('peps-auto-analyze')==='false')$('workflow-auto-analyze').checked=false;const cached=sessionStorage.getItem('peps-cache-mode');if(['reuse','revalidate','local'].includes(cached))$('workflow-cache-mode').value=cached;}catch{}
    $('workflow-mobile-step').onchange=()=>navigate($('workflow-mobile-step').value).catch(error=>hooks.message(error.message,true));
    // Put the recovery decision before diagnostic counters, including on narrow screens.
    $('workflow-download-note').after($('workflow-zero-results'));
    $('workflow-primary').onclick=()=>primary().catch(error=>hooks.message(error.message,true));
    for(const mode of ['online','local'])$('workflow-mode-'+mode).onclick=()=>perform(async()=>{await hooks.api('workflow-mode',{mode});});
    $('workflow-sources-next').onclick=confirmSources;
    // This button owns the contextual download/recovery action; the legacy app handler is replaced here.
    $('news-acquire').onclick=()=>{const context=downloadContext();if(context.canContinue&&!$('workflow-auto-analyze').checked)return navigate('analysis').catch(error=>hooks.message(error.message,true));if(context.canContinue)return perform(async()=>{await hooks.api('workflow-analyze',{});if(canOpenTab('analysis'))await navigate('analysis');});return startDownload(context.pending>0&&!context.coverage.needs_new_search);};
    $('workflow-resume-download').onclick=()=>startDownload(true);
    $('workflow-news-settings').onclick=()=>navigate('sources').catch(error=>hooks.message(error.message,true));
    $('workflow-targeted-recovery').onclick=directedRecovery;
    $('workflow-pause-analyze').onclick=async()=>{if(state.requesting||!action('pause_and_analyze').enabled)return;state.requesting=true;renderActions();try{await hooks.api('workflow-pause-and-analyze-available',{operation_id:state.operation.operation_id||state.operation.id});await hooks.refresh(true);hooks.message('Pausa solicitada. El análisis comenzará cuando termine la petición actual.');}catch(error){hooks.message(error.message,true);}finally{state.requesting=false;renderActions();}};
    $('workflow-search-form').onsubmit=event=>{event.preventDefault();const config=searchPayload();if(!config||!action('configure_sources').enabled)return;perform(async()=>{renderSearch(await hooks.api('news-search',config));});};
    for(const field of document.querySelectorAll('#workflow-search-fields input,#workflow-search-fields select'))field.addEventListener('change',()=>{state.searchDirty=true;setText('workflow-search-status','Los cambios se guardarán al iniciar. Puedes revisar el alcance antes.');renderActions();});
    $('workflow-download-next').onclick=()=>navigate('analysis').catch(error=>hooks.message(error.message,true));
    $('workflow-results-next').onclick=()=>navigate('bank').catch(error=>hooks.message(error.message,true));
    $('workflow-analyze').onclick=()=>perform(async()=>{await hooks.api('workflow-analyze',{});hooks.message('Análisis iniciado. Puedes seguir el progreso de atribución y cálculo.');});
    $('workflow-accept-partial').onclick=()=>perform(async()=>{await hooks.api('workflow-analyze-available',{});await hooks.refresh(true);hooks.message('Analizando la cobertura disponible. Las tareas pendientes se conservan y los resultados mostrarán esta limitación.');if(canOpenTab('analysis'))await navigate('analysis');});
    $('news-online').onchange=()=>renderActions();$('workflow-cache-mode').onchange=()=>{try{sessionStorage.setItem('peps-cache-mode',cacheMode());}catch{}renderActions();};$('workflow-auto-analyze').onchange=()=>{try{sessionStorage.setItem('peps-auto-analyze',String($('workflow-auto-analyze').checked));}catch{}renderActions();};
    $('workflow-queue-details').ontoggle=()=>{if($('workflow-queue-details').open)loadTasks(true);};$('workflow-task-filter').onchange=()=>loadTasks(true);$('workflow-task-refresh').onclick=()=>loadTasks(false);$('workflow-task-previous').onclick=()=>{taskOffset=Math.max(0,taskOffset-25);loadTasks(false);};$('workflow-task-next').onclick=()=>{taskOffset+=25;loadTasks(false);};
    renderNavigation();renderPrimary();
  }
  return {init,update,onNavigate,canOpenTab,canAction,busy,observe};
})();
