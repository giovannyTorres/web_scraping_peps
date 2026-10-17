'use strict';
const token=document.querySelector('meta[name=peps-token]').content;
const $=id=>document.getElementById(id);
let offset=0, selected=null, entities=[], activeTab='import', refreshing=null;
async function api(path,data){const r=await fetch('/api/'+path,{method:data?'POST':'GET',headers:{'X-PEPS-Token':token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined});if(!r.ok){let e=await r.json();const error=Error(e.error||r.statusText);error.status=r.status;throw error;}const value=await r.json();if(data&&window.pepsWorkflow?.observe)window.pepsWorkflow.observe(value);return value;}
function message(text,error=false){if(pollStopped)return;$('message').hidden=false;$('message').textContent=text;$('message').className=error?'error':'';}
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function action(id,fn){$(id).onclick=async()=>{if(window.pepsWorkflow&&!window.pepsWorkflow.canAction(id))return;$(id).disabled=true;try{await fn();await refresh(true);}catch(e){message(e.message,true);}finally{if(!['previous','next'].includes(id))$(id).disabled=false;if(window.pepsWorkflow)window.pepsWorkflow.update(lastStats);}};}
const kinds={person:'Persona',company:'Empresa',organization:'Organismo'};
let detailRequest=0, detailState=null, lastStats={}, renderedEntitiesKey=null, renderedNewsArticlesKey=null;
const sourceSaves=new Set();
window.pepsSourcesPending=()=>sourceSaves.size;
window.pepsWaitSources=()=>Promise.allSettled([...sourceSaves]);
const roleLabels={accused:'Posible sujeto de acusación',victim:'Posible víctima',complainant:'Posible denunciante',incidental:'Mención incidental',negated:'Afirmación negada',resolved:'Resolución reportada',indirect:'Relación indirecta',insufficient:'Evidencia contextual insuficiente'};
const relevanceLabels={direct_adverse_candidate:'Candidato a revisión por señal directa',victim_context:'Contexto de víctima',reporter_context:'Contexto de denunciante',incidental:'Mención incidental',negated_claim:'Afirmación negada',reported_resolution:'Resolución reportada',insufficient:'Sin conclusión contextual',indirect_only:'Sólo relación indirecta'};
const methodLabels={discriminative:'Clasificador discriminativo local',advanced:'NLP local con dependencias y NER',enriched:'Clasificador con extracciones NLP',hybrid:'Acuerdo experimental de clasificador y NLP'};
function codepoints(text){return Array.from(String(text||''));}
function sourceLink(value,label){const link=el('a',label||value||'Fuente sin URL');try{const url=new URL(value);if(['https:','http:'].includes(url.protocol)){link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';}}catch{}return link;}
function list(items,cls){const ul=el('ul',undefined,cls);for(const item of items||[])ul.append(el('li',item));return ul;}
function jsonDetails(title,value){const detail=el('details',undefined,'technical');detail.append(el('summary',title));detail.ontoggle=()=>{if(detail.open&&!detail.dataset.rendered){detail.append(el('pre',JSON.stringify(value,null,2)));detail.dataset.rendered='true';}};return detail;}
function dateLabel(value){return value?new Date(value).toLocaleString('es-MX',{dateStyle:'medium',timeStyle:'short'}):'sin fecha';}
function numberLabel(value){return Number(value||0).toLocaleString('es-MX',{maximumFractionDigits:3});}
function assessmentOf(item){return item.assessment||item.score?.explainability?.assessment||null;}
function renderEntities(){
  const key=JSON.stringify([offset,selected,window.pepsWorkflow?.canOpenTab('bank'),entities.map(item=>[item.id,item.name,item.kind,item.score?.score,item.score?.status,item.score_available,assessmentOf(item)])]);
  if(key===renderedEntitiesKey)return;renderedEntitiesKey=key;
  const body=$('entities');body.replaceChildren();$('empty').hidden=entities.length>0;
  if(!entities.length){const searching=!!$('search').value.trim();$('empty').querySelector('h3').textContent=searching?'No hay coincidencias':'Empieza con un nombre o una lista';$('empty').querySelector('p').textContent=searching?'Prueba con otro nombre, una parte del apellido o borra la búsqueda.':'Agrega personas, empresas u organizaciones para consultar su evidencia y entender su score.';$('empty').querySelector('.actions').hidden=searching;}
  for(const item of entities){
    const assessment=assessmentOf(item),value=item.score?(assessment?assessment.display_score:item.score.score):null;
    const tr=el('tr',undefined,'entity-row'+(selected===item.id?' selected':''));tr.tabIndex=0;
    tr.setAttribute('aria-label',`Consultar ${item.name}`);tr.setAttribute('aria-selected',String(selected===item.id));
    tr.setAttribute('aria-disabled',String(window.pepsWorkflow?!window.pepsWorkflow.canOpenTab('bank'):false));
    tr.append(el('td',item.name),el('td',kinds[item.kind]||item.kind));
    const score=el('td');score.append(el('span',value===null?'—':String(value),'score'+(value>=50?' urgent':'')));
    if(assessment)score.title=assessment.label;tr.append(score);
    const status=el('td'),label=item.score?(assessment&&assessment.state!=='evaluated'?assessment.label:({validated:'Revisado',provisional:'Provisional',no_evidence:'Sin señales puntuables'}[item.score.status]||item.score.status)):(item.score_available?'Pendiente de actualizar':'Sin evaluar');
    status.append(el('span',label,'badge'));tr.append(status);
    const open=()=>showDetail(item);tr.onclick=open;tr.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};body.append(tr);
  }
  $('page').textContent=String(offset/100+1);$('previous').disabled=offset===0;$('next').disabled=entities.length<100;
}
// Persisted evaluations supply both scores and their evidence; list rows are only a cache.
async function showDetail(item,runId=null){
  if(!runId&&window.pepsWorkflow&&!window.pepsWorkflow.canOpenTab('bank',true))return;
  selected=item.id;renderEntities();const request=++detailRequest;detailState=null;
  const card=$('detail');card.setAttribute('aria-busy','true');card.replaceChildren(el('p','EXPEDIENTE','eyebrow'),el('h2',item.name),el('p','Cargando evaluación y sus evidencias…','muted'));
  try{
    const data=await api('explanation?id='+encodeURIComponent(item.id)+(runId?'&run_id='+encodeURIComponent(runId):''));
    if(request!==detailRequest)return;
    const state={data,request,notes:new Map(),noteRequests:new Map(),shownEvidence:0};detailState=state;
    card.replaceChildren(el('p','EXPEDIENTE','eyebrow'),el('h2',data.entity.name),el('p',`${kinds[data.entity.kind]||data.entity.kind} · ID ${data.entity.id}`,'muted'));
    renderHistory(card,state);renderPublicSearch(card,state);renderDocumentReview(card,state);renderExplanation(card,state);renderAuxiliary(card,state);renderEvidenceIndex(card,state);renderTechnical(card,state);
    const row=entities.find(entity=>entity.id===item.id);if(row&&!runId){row.score=data.result;row.assessment=data.assessment;renderEntities();}
  }catch(error){if(request===detailRequest){card.replaceChildren(el('h2','No se pudo abrir la evaluación'),el('p',error.message,'error'));message(error.message,true);}}
  finally{if(request===detailRequest)card.removeAttribute('aria-busy');}
}
function renderDocumentReview(card,state){
  const review=state.data.document_review;if(!review)return;
  const details=el('details',undefined,'document-review');details.setAttribute('aria-label','Señales documentales para revisión');
  const count=Number(review.documents_with_leads||0),total=Number(review.total_leads||0);
  const summary=el('summary',undefined,'document-review-summary'),overview=el('span',undefined,'document-review-overview');
  overview.append(el('span','Lectura documental','document-review-label'),el('span',`${count} ${count===1?'nota':'notas'} · ${total} ${total===1?'fragmento':'fragmentos'} para revisar`,'document-review-caption'));
  const toggle=el('span','Ver fragmentos','document-review-toggle');summary.append(el('span',String(count),'document-review-count'),overview,toggle);details.append(summary);
  const content=el('div',undefined,'document-review-content');content.append(el('p',review.note,'notice'));
  if(review.reanalyze_required)content.append(el('p',`${review.reanalyze_required} documentos requieren volver a analizarse con las reglas actuales. La evaluación histórica permanece conservada.`,'muted'));
  const items=Array.isArray(review.items)?review.items:[],rows=el('div',undefined,'document-review-rows');
  const pagination=el('div',undefined,'document-review-pagination'),position=el('p','','muted'),more=el('button','Mostrar más fragmentos','secondary');more.type='button';position.setAttribute('aria-live','polite');pagination.append(position,more);let shown=0;
  function append(){
    for(const item of items.slice(shown,shown+10)){
      const resolutions=Array.isArray(item.resolution_context)?item.resolution_context:[];
      const mixed=item.adverse_alert===false||item.chronology_state==='mixed_requires_review'||resolutions.length>0;
      const row=el('article',undefined,'document-lead'+(mixed?' document-lead-resolution':''));
      row.append(el('h4',item.title),el('p',`${item.category_label} · ${item.stage_label}${item.uncertain?' · formulación incierta':''}`,'citation-label'));
      if(mixed)row.append(el('p','Acusación y resolución · cronología por revisar','document-chronology-label'),el('p','Lee ambas citas antes de interpretar esta nota. No se presenta como una alerta adversa concluyente.','muted'));
      row.append(el('blockquote',item.quote));
      const actions=el('div',undefined,'document-lead-actions');actions.append(citationButton(state,item.evidence_id,item,'Revisar fragmento en el original'),sourceLink(item.source_url,'Consultar fuente'));row.append(actions);
      for(const resolution of resolutions){const block=el('div',undefined,'document-resolution');block.append(el('p',`Resolución reportada · ${resolution.stage_label}`,'citation-label'),el('blockquote',resolution.quote),citationButton(state,item.evidence_id,resolution,'Revisar resolución en el original'));row.append(block);}
      row.append(el('p',`${item.published_at||'Sin fecha'} · Identidad: ${{matched:'corroboración documental',candidate:'pendiente de resolver',rejected:'vínculo rechazado'}[item.identity_status]||'pendiente de resolver'}`,'document-lead-meta'));
      rows.append(row);
    }
    shown=Math.min(shown+10,items.length);position.textContent=`${shown} de ${total} fragmentos${review.truncated?' · vista limitada':''}`;more.hidden=shown>=items.length;
  }
  more.onclick=append;content.append(rows,pagination);
  if(!items.length){content.append(el('p','No hay fragmentos adversos directos admisibles en esta lectura. Consulta la cobertura y los textos; este estado no acredita ausencia de incidentes.','muted'));pagination.hidden=true;}
  if(review.truncated)content.append(el('p',`Esta vista contiene ${items.length} de ${total} fragmentos. Consulta el expediente completo para revisar el resto.`,'muted'));
  details.append(content);details.ontoggle=()=>{toggle.textContent=details.open?'Cerrar':'Ver fragmentos';if(details.open&&!shown&&items.length)append();};card.append(details);
}
function renderPublicSearch(card,state){
  const details=el('details',undefined,'public-search-links');details.append(el('summary','Ampliar búsqueda en la web y redes públicas'));
  const body=el('div');details.append(body);let loaded=false;
  details.ontoggle=async()=>{if(!details.open||loaded)return;loaded=true;body.replaceChildren(el('p','Preparando enlaces de búsqueda…','muted'));
    try{const data=await api('public-search?id='+encodeURIComponent(state.data.entity.id));if(detailState!==state)return;
      body.replaceChildren(el('p',data.note||'Estos enlaces abren búsquedas manuales. No conectan cuentas ni incorporan resultados automáticamente.','muted'));
      const links=Array.isArray(data.links)?data.links:[];for(const item of links){if(!item||!/^https?:\/\//i.test(item.url||''))continue;const row=el('p');row.append(sourceLink(item.url,item.label||item.channel||'Abrir búsqueda'));body.append(row);}
      if(!links.length)body.append(el('p','No hay enlaces públicos disponibles para esta entidad.','muted'));
    }catch(error){loaded=false;body.replaceChildren(el('p','No se pudieron preparar los enlaces: '+error.message,'muted'));}
  };card.append(details);
}
function renderHistory(card,state){
  const {data}=state;const historical=data.run.historical;
  const controls=el('div',undefined,'evaluation-controls');const label=el('label','Evaluación consultada');const select=el('select');select.className='evaluation-select';select.setAttribute('aria-label','Evaluación consultada');
  const current=el('option','Actual · resultado y revisión vigentes');current.value='';select.append(current);
  for(const run of data.history||[]){const option=el('option',`${dateLabel(run.created_at)} · ${run.score}/100 · ${run.id.slice(0,8)}${run.current?' · actual':''}`);option.value=run.id;select.append(option);}
  select.value=historical?data.run.id:'';select.onchange=()=>showDetail(data.entity,select.value||null);label.append(select);controls.append(label);const reload=el('button','Actualizar evaluación','text-button');reload.type='button';reload.onclick=()=>showDetail(data.entity);controls.append(reload);card.append(controls);
  card.append(el('p',`${historical?'Histórico inmutable · revisión deshabilitada':'Evaluación actual'} · Procesada: ${dateLabel(data.run.created_at)}`,'muted'));
  if(historical)card.append(el('p','Se muestran el texto, la identidad y la revisión conservados en esta evaluación. Selecciona «Actual» para registrar una revisión.','notice'));
}
function citationButton(state,id,citation,label='Ver fragmento en la nota'){
  const button=el('button',label,'text-button');button.type='button';button.dataset.evidenceId=id;button.onclick=async()=>{button.disabled=true;try{const note=await loadNote(state,id,citation);if(detailState!==state||!note)return;note.open=true;const target=note.querySelector('mark.active-evidence')||note.querySelector('.note-body');(target||note).scrollIntoView({block:'center',behavior:'smooth'});if(target){target.tabIndex=-1;target.focus({preventScroll:true});}}catch(error){message(error.message,true);}finally{button.disabled=false;}};return button;
}
function renderExplanation(card,state){
  const {result,explanation}=state.data;const interpretation=explanation.interpretation||{};
  const assessment=state.data.assessment||explanation.assessment,display=assessment?assessment.display_score:result.score;
  const section=el('section',undefined,'score-explanation');section.setAttribute('aria-label','Explicación del scoring');section.append(el('h3','¿Por qué obtuvo este score?','explanation-title'));
  const summary=el('div',undefined,'score-summary'+(display===null?' score-unavailable':''));const number=el('div',undefined,'score-number');number.append(el('strong',display===null?'—':String(display)),el('span',display===null?'Sin evaluación suficiente':' / 100'));const meaning=el('div');meaning.append(el('h4',display===null?assessment.label:interpretation.title||`Prioridad ${result.label}`),el('p',interpretation.meaning,'muted'));summary.append(number,meaning);section.append(summary);
  if(assessment?.zero_reasons?.length){const reasons=el('div',undefined,'notice');reasons.append(el('h4',display===null?'Qué falta para evaluar':'Por qué no hay puntos'),list(assessment.zero_reasons.map(reason=>reason.label)));section.append(reasons);}
  const branches=el('div',undefined,'score-branches');for(const [label,value]of [['Provisional',result.provisional_score],['Validada por revisión humana',result.validated_score]]){const branch=el('div');branch.append(el('span',label),el('strong',`${value}/100`));branches.append(branch);}if(display!==null)section.append(branches,el('p',interpretation.branches,'muted'));
  const scale=el('details',undefined,'scale-details');scale.append(el('summary','Cómo interpretar la escala'),el('p',`Escala mostrada sobre ${interpretation.scale?.display_denominator??100}; máximo alcanzable: ${interpretation.scale?.maximum??95}. Fecha del cálculo: ${interpretation.as_of||'sin fecha'}.`,'muted'));
  const ranges=el('div',undefined,'score-ranges');for(const range of interpretation.ranges||[]){const rangeLabel=range.min===range.max?String(range.min):`${range.min}–${range.max}`;ranges.append(el('span',`${rangeLabel}: ${range.label}`,'badge'+(result.score>=range.min&&result.score<=range.max?' active-range':'')));}scale.append(ranges,el('p',interpretation.comparison,'muted'));section.append(scale);
  section.append(el('h4','Factores que participaron en el cálculo'));
  if(!explanation.factors?.length)section.append(el('p','No hay eventos con aportación puntuable. Revisa la cobertura y las exclusiones; esto no acredita ausencia de riesgo.','muted'));
  for(const factor of explanation.factors||[]){
    const block=el('article',undefined,'score-factor');const heading=el('div',undefined,'factor-heading');heading.append(el('h4',factor.title),el('span',`${factor.score_delta_when_removed>0?'−':''}${numberLabel(factor.score_delta_when_removed)} al retirarlo`,'factor-delta'));block.append(heading,el('p',`Sin este evento: ${factor.score_without_event}/100. Peso del evento: provisional ${numberLabel(factor.provisional_points)}; validado ${numberLabel(factor.validated_points)}.`,'muted'));
    if(factor.resolution)block.append(el('p',`Resolución aplicada: ${factor.resolution}${factor.resolution_review_required?' · pendiente de revisión humana':''}.`,'notice'));
    for(const citation of factor.evidence||[]){const excerpt=el('div',undefined,'factor-citation');let role=citation.selected_for_score?'Seleccionada para el score final':citation.resolution_applied?'Resolución aplicada al evento':citation.provisional_selected||citation.validated_selected?'Seleccionada en una rama del cálculo':'Apoyo del evento; no suma de nuevo';excerpt.append(el('p',`${role} · ${citation.category_label} · ${citation.stage_label}`,'citation-label'),el('blockquote',citation.quote),citationButton(state,citation.evidence_id,citation));block.append(excerpt);}
    block.append(jsonDetails('Reglas, pesos y selección de este evento',{event_group:factor.event_group,aggregation:factor.aggregation,source_support:factor.source_support,evidence:factor.evidence}));section.append(block);
  }
  section.append(el('p','El cambio al retirar un evento usa el cálculo real. Estos cambios no se suman: el score combina eventos, redondea y tiene un límite.','muted'));
  const coverage=explanation.coverage||{};section.append(el('h4','Cobertura y límites'),el('p',`${coverage.documents??0} notas · ${coverage.event_groups??0} grupos de eventos · ${coverage.reviewed_documents??0} notas revisadas · ${coverage.excluded_documents??0} notas con exclusiones.`,'muted'));
  renderAcquisitionCoverage(section,result.provenance?.acquisition);
  if(explanation.warnings?.length){const warnings=el('div',undefined,'explanation-warnings');warnings.append(el('h4','Advertencias para la revisión'),list(explanation.warnings));section.append(warnings);}
  if(explanation.exclusions?.length){const excluded=el('details');excluded.append(el('summary','Señales y documentos excluidos del cálculo'),list(explanation.exclusions.map(item=>`${item.label}: ${item.count}`)));section.append(excluded);}
  const limitations=el('details');limitations.append(el('summary','Limitaciones de la interpretación'),list(explanation.limitations));section.append(limitations);card.append(section);
}
function renderAcquisitionCoverage(section,coverage){
  if(!coverage)return;
  const block=el('div',undefined,coverage.partial?'notice acquisition-coverage':'acquisition-coverage');
  block.append(el('h4','Cobertura de la descarga usada en esta evaluación'));
  if(coverage.mode==='local')block.append(el('p','Evaluación de los documentos locales disponibles. No se realizó una búsqueda web para este análisis.'));
  else{
    if(coverage.accepted_partial)block.append(el('p',`Descarga cerrada con cobertura parcial: ${coverage.pending_tasks??0} tareas quedaron pendientes y ${coverage.failed_tasks??0} terminaron con error. Los resultados corresponden a la información disponible al cerrar.`));
    const search=coverage.search||{};
    if(search.start&&search.end)block.append(el('p',`Periodo solicitado: ${search.start} a ${search.end}. Es un rango de búsqueda; no acredita cobertura completa de todos esos años.`,'muted'));
    else block.append(el('p','El período de las descargas anteriores no quedó registrado. No se les atribuye el rango de búsqueda configurado después.','muted'));
    const multimedia=Number(coverage.multimedia_reference_articles||0),profiles=Number(coverage.profile_reference_articles||0),duplicates=Number(coverage.duplicate_reference_articles||0),pending=Number(coverage.pending_text_articles??Math.max(0,Number(coverage.metadata_only_articles||0)-multimedia-profiles-duplicates));
    const references=[`${coverage.full_articles??0} textos disponibles`,`${pending} referencias pendientes de texto`];
    if(multimedia)references.push(`${multimedia} referencias de audio/vídeo sin transcripción`);if(profiles)references.push(`${profiles} perfiles o directorios`);if(duplicates)references.push(`${duplicates} réplicas de textos ya conservados`);
    block.append(el('p',`${coverage.articles??0} referencias de esta lista: ${references.join(' · ')}. Las referencias sin texto disponible no puntúan como evidencia del artículo.`,'muted'));
  }
  const observed=coverage.observed;
  if(observed)block.append(el('p',`Cobertura observada de la lista: ${observed.entities_queried??0} de ${observed.entities_total??0} entidades consultadas; ${observed.entities_with_bodies??0} con texto conservado. Las consultas planeadas no cuentan como ejecutadas.`,'muted'));
  if(observed&&(Number(observed.shared_pages_read||0)>0||Number(observed.entities_checked_locally||0)>0))block.append(el('p',`Índice compartido observado: ${observed.shared_pages_read??0} páginas leídas, ${observed.shared_records_checked??0} registros contrastados y ${observed.entities_checked_locally??0} de ${observed.entities_total??0} entidades contrastadas localmente. Esto no acredita consultas dirigidas ni cobertura completa del archivo.`,'muted'));
  const selection=coverage.last_session_limits||{};
  if(Number.isFinite(selection.per_entity_body_limit))block.append(el('p',`Selección de la última sesión: hasta ${selection.per_entity_body_limit} cuerpos por entidad, con ${selection.body_skipped_budget??0} candidatos aplazados por ese límite. La selección no acredita cobertura exhaustiva.`,'muted'));
  const limits=Array.isArray(coverage.limitations)?coverage.limitations.filter(item=>typeof item==='string'):[];
  const warnings=Array.isArray(coverage.planning_warnings)?coverage.planning_warnings.filter(item=>typeof item==='string'):[];
  if(limits.length||warnings.length)block.append(list([...new Set([...limits,...warnings])]));
  section.append(block);
}
// Auxiliary hypotheses stay outside the numerical scoring explanation.
function renderAuxiliary(card,state){
  const details=el('details',undefined,'contextual-section');details.append(el('summary','Lectura contextual auxiliar · IA y NLP local'),el('p','Estas hipótesis no modifican el score y no son razones de su cálculo. El clasificador se entrenó con ejemplos sintéticos y aún no está validado con prensa real; puede omitir acusaciones. Requieren revisión humana.','notice'));
  const analyses=state.data.result.contextual_analysis||[];const container=el('div');const more=el('button','Mostrar más análisis contextuales');more.type='button';details.append(container,more);let shown=0;
  function appendPage(){if(!analyses.length)container.append(el('p','No hay análisis contextual conservado.','muted'));const page=analyses.slice(shown,shown+20);shown+=page.length;more.hidden=shown>=analyses.length;
  for(const {evidence_id:id,analysis}of page){const block=el('article',undefined,'auxiliary-note');block.append(el('h4',roleLabels[analysis.role]||analysis.role||'Sin conclusión'),el('p',`${methodLabels[analysis.method]||analysis.method} · ${analysis.model_version||'sin versión'} · ${analysis.abstained?'Se abstiene de concluir':relevanceLabels[analysis.relevance]||analysis.relevance}`,'muted'),citationButton(state,id,null,'Abrir nota de esta hipótesis'));
    for(const finding of analysis.findings||[]){const excerpt=el('div',undefined,'factor-citation');excerpt.append(el('p',`${roleLabels[finding.role]||finding.role}${finding.abstained?' · sin conclusión':''}`,'citation-label'),el('blockquote',finding.quote),citationButton(state,id,finding));block.append(excerpt);}
    const entities=analysis.entities||[],relations=analysis.relations||[],events=analysis.events||[];
    if(entities.length)block.append(el('h5','Entidades detectadas · identidad sin verificar'),list(entities.map(entity=>`${entity.text} · ${entity.label||'sin tipo'}`)));
    if(relations.length)block.append(el('h5','Relaciones propuestas'),list(relations.map(relation=>`${relation.mention?.text||'Entidad'} → ${relation.event?.text||relation.event?.lemma||'evento'} · ${roleLabels[relation.role]||relation.role||'sin papel'} · ${relation.status||''}`)));
    if(events.length)block.append(el('h5','Eventos detectados'),list(events.map(event=>`${event.text||event.lemma||'Evento'}${event.negated?' · negado':''}${event.hypothetical?' · hipotético':''} · ${event.status||'sin estado'}`)));
    if(!entities.length&&!relations.length&&!events.length)block.append(el('p','Esta evaluación no conserva extracción de entidades, relaciones ni eventos. El modo predeterminado clasifica papeles; esas extracciones requieren el NLP opcional instalado.','muted'));
    if(analysis.warnings?.length)block.append(list(analysis.warnings,'auxiliary-warnings'));
    block.append(jsonDetails('Método, fragmentos y limitaciones del análisis auxiliar',analysis));container.append(block);
  }}more.onclick=appendPage;details.ontoggle=()=>{if(details.open&&!details.dataset.rendered){details.dataset.rendered='true';appendPage();}};card.append(details);
}
function renderEvidenceIndex(card,state){
  const section=el('section',undefined,'evidence-section');section.append(el('h3',`Notas de esta evaluación (${state.data.evidence_count})`),el('p','Abre una nota para consultar el texto íntegro conservado, sus fragmentos resaltados y la revisión de identidad. Las fuentes externas se abren sólo al pulsar su enlace.','muted'));state.evidenceContainer=el('div');section.append(state.evidenceContainer);state.moreEvidence=el('button','Mostrar más notas');state.moreEvidence.onclick=()=>appendEvidencePage(state);section.append(state.moreEvidence);card.append(section);appendEvidencePage(state);
}
function appendEvidencePage(state){const audits=state.data.result.evidence_audit||[];const next=Math.min(audits.length,state.shownEvidence+50);for(let i=state.shownEvidence;i<next;i++)createNoteShell(state,audits[i]);state.shownEvidence=next;state.moreEvidence.hidden=next>=audits.length;}
function createNoteShell(state,audit){
  if(state.notes.has(audit.evidence_id))return state.notes.get(audit.evidence_id);
  const note=el('details',undefined,'evidence note');note.id='evidence-'+audit.evidence_id;note.dataset.evidenceId=audit.evidence_id;const summary=el('summary');summary.append(el('span',`Nota ${state.notes.size+1} · ${audit.source_kind||'sin tipo'}`),el('span',audit.evidence_id.slice(0,12),'muted'));note.append(summary);const body=el('div',undefined,'note-content');body.append(el('p','Texto íntegro disponible en la evaluación conservada.','muted'));note.append(body);note._content=body;state.notes.set(audit.evidence_id,note);state.evidenceContainer.append(note);note.ontoggle=()=>{if(note.open&&!note._loaded)loadNote(state,audit.evidence_id).catch(error=>message(error.message,true));};return note;
}
async function loadNote(state,id,citation=null){
  if(detailState!==state)return null;
  const audit=(state.data.result.evidence_audit||[]).find(item=>item.evidence_id===id);if(!audit)throw Error('La nota no pertenece a esta evaluación.');
  const note=state.notes.get(id)||createNoteShell(state,audit);
  if(!note._loaded){
    if(!state.noteRequests.has(id)){note._content.replaceChildren(el('p','Cargando texto original…','muted'));const request=api('run-evidence?run_id='+encodeURIComponent(state.data.run.id)+'&id='+encodeURIComponent(id));state.noteRequests.set(id,request);}
    try{const evidence=await state.noteRequests.get(id);if(detailState!==state)return null;if(!note._loaded){note._evidence=evidence;renderNote(state,note,evidence,audit);note._loaded=true;}}catch(error){state.noteRequests.delete(id);note._content.replaceChildren(el('p',error.message,'error'));throw error;}
  }
  if(citation){const valid=renderNoteText(note,state,id,citation);if(!valid)throw Error('El fragmento no coincide con el texto y sus offsets. No se resaltó como evidencia.');}
  return note;
}
// Python offsets count Unicode code points. Verify source text before adding any mark.
function renderNoteText(note,state,id,active=null){
  const text=String(note._evidence.text||''),chars=codepoints(text);const valid=span=>Number.isInteger(span.start)&&Number.isInteger(span.end)&&span.start>=0&&span.end>span.start&&span.end<=chars.length&&chars.slice(span.start,span.end).join('')===(span.quote??span.text);
  if(active&&!valid(active))return false;
  const candidates=(state.data.explanation.factors||[]).flatMap(factor=>(factor.evidence||[]).filter(citation=>citation.evidence_id===id));if(active)candidates.push(active);
  const spans=candidates.filter(valid);const boundaries=new Set([0,chars.length]);for(const span of spans){boundaries.add(span.start);boundaries.add(span.end);}const positions=Array.from(boundaries).sort((a,b)=>a-b),fragment=document.createDocumentFragment();
  for(let i=0;i<positions.length-1;i++){const start=positions[i],end=positions[i+1],value=chars.slice(start,end).join('');const matching=spans.filter(span=>span.start<=start&&span.end>=end);if(matching.length){const mark=el('mark',value);mark.dataset.start=String(start);mark.dataset.end=String(end);if(active&&active.start<=start&&active.end>=end)mark.className='active-evidence';fragment.append(mark);}else fragment.append(document.createTextNode(value));}
  note._text.replaceChildren(fragment);return true;
}
function renderNote(state,note,ev,audit){
  const block=note._content;block.replaceChildren(el('h4',ev.title||'Documento sin título'),sourceLink(ev.url||ev.source_url),el('p',`${ev.source_kind} · Publicado: ${ev.published_at||'sin fecha'} · Recuperado: ${ev.retrieved_at||'sin fecha'}`,'muted'));
  if(audit.exclusions?.length){const labels=new Map((state.data.explanation.exclusions||[]).map(item=>[item.code,item.label]));block.append(el('p','Motivos de exclusión de esta nota','citation-label'),list(audit.exclusions.map(code=>labels.get(code)||code)));}
  note._text=el('div',undefined,'note-body');note._text.setAttribute('aria-label','Texto original con evidencia resaltada');block.append(note._text);renderNoteText(note,state,ev.id);
  block.append(jsonDetails('Identificadores, hash, offsets y análisis conservado',{id:ev.id,document_id:ev.document_id,run_id:ev.run_id,content_hash:ev.content_hash,identity_resolution:ev.identity_resolution,analysis:ev.analysis,review:ev.review}));
  if(state.data.run.historical){block.append(el('p','Revisión conservada en el histórico. Para cambiarla, abre la evaluación actual.','muted'));return;}
  renderIdentity(block,ev);renderReview(block,state,ev);
}
function renderReview(block,state,ev){
  const form=el('form',undefined,'review-form');form.append(el('h4','Revisión humana de vínculo y evidencia'));
  const select=el('select');select.setAttribute('aria-label','Estado de revisión');for(const [value,label]of [['pending','Pendiente'],['confirmed','Confirmar vínculo y evidencia'],['rejected','Rechazar vínculo o evidencia']]){const option=el('option',label);option.value=value;select.append(option);}select.value=ev.review?.status||'pending';
  const actor=el('input');actor.setAttribute('aria-label','Revisor');actor.placeholder='Revisor';actor.value=localStorage.getItem('peps-reviewer')||'';actor.required=true;
  const reason=el('textarea');reason.setAttribute('aria-label','Motivo y comprobación de identidad');reason.placeholder='Motivo y comprobación de identidad';reason.rows=3;reason.required=true;const save=el('button','Guardar revisión');save.type='submit';form.append(select,actor,reason,save);
  form.onsubmit=async event=>{event.preventDefault();save.disabled=true;try{await api('review',{id:ev.id,expected_run_id:state.data.run.id,status:select.value,actor:actor.value,reason:reason.value,identity_spans:reviewIdentitySpans(block,ev)});localStorage.setItem('peps-reviewer',actor.value);await refresh();if(detailState===state)await showDetail(state.data.entity);message('Revisión guardada. Se creó una evaluación con trazabilidad; el histórico anterior se conserva.');}catch(error){message((error.status===409||(error.status===400&&error.message.includes('La evaluación cambió')))?'La evaluación cambió. Pulsa «Actualizar evaluación» y comprueba la evidencia vigente antes de guardar otra revisión. No se guardó la revisión.':error.message,true);}finally{save.disabled=false;}};block.append(form);
}
function renderTechnical(card,state){
  const details=el('details',undefined,'technical scoring-technical');details.append(el('summary','Desglose técnico auditable y reproducción'),el('p',`Evaluación ${state.data.run.id} · ${state.data.result.model_version}`,'muted'));const replay=el('button','Reproducir cálculo conservado');replay.type='button';const outcome=el('div');outcome.setAttribute('role','status');replay.onclick=async()=>{replay.disabled=true;outcome.replaceChildren(el('p','Reproduciendo cálculo local…','muted'));try{const result=await api('replay',{run_id:state.data.run.id});if(detailState!==state)return;outcome.replaceChildren(el('p',result.matched?'El cálculo y la traza coinciden con la evaluación conservada.':result.status==='unsupported'?'Esta versión no puede reproducir el cálculo histórico.':'El cálculo difiere de la evaluación conservada.',result.matched?'replay-ok':'error'),el('p',result.reason,'muted'),el('pre',JSON.stringify(result,null,2)));}catch(error){outcome.replaceChildren(el('p',error.message,'error'));}finally{replay.disabled=false;}};
  details.append(replay,outcome,jsonDetails('Resultado íntegro, parámetros y versiones',state.data.result));card.append(details);
}
function bindSourceChecks(checks,path,source,confirmation,onSaved=()=>{}){
  let saved=Object.fromEntries(Object.keys(checks).map(key=>[key,!!source[key]]));
  for(const input of Object.values(checks))input.onchange=async()=>{
    const values=Object.fromEntries(Object.entries(checks).map(([key,field])=>[key,field.checked]));
    for(const field of Object.values(checks)){field.dataset.permissionSaving='true';field.disabled=true;}
    const task=(async()=>{try{await api(path,{id:source.id,...values});saved=values;onSaved(values);message(confirmation);await refresh(true);}
      catch(error){for(const [key,field]of Object.entries(checks))field.checked=saved[key];message(error.message,true);}})();
    sourceSaves.add(task);window.pepsWorkflow?.update(lastStats);
    try{await task;}finally{sourceSaves.delete(task);for(const field of Object.values(checks)){delete field.dataset.permissionSaving;field.disabled=false;}window.pepsWorkflow?.update(lastStats);}
  };
}
async function loadSources(){const sources=await api('sources');if(pollStopped)return;const container=$('sources');container.replaceChildren();for(const source of sources){const row=el('div',undefined,'source');const info=el('div');info.append(el('strong',source.name||source.id),el('p',source.base_url||source.url||''),el('p',`Tipo: ${source.kind} · Pausa: ${source.min_interval_seconds||5}s · ${source.discovery?'Descubrimiento':'Contenido / importación'}`));const choices=el('div');const enabled=el('input');enabled.type='checkbox';enabled.checked=source.enabled;const terms=el('input');terms.type='checkbox';terms.checked=source.terms_approved;const l1=el('label');l1.append(enabled,document.createTextNode('Habilitada'));const l2=el('label');l2.append(terms,document.createTextNode('Condiciones de uso revisadas'));choices.append(l1,l2);bindSourceChecks({enabled,terms_approved:terms},'source',source,'Configuración de fuente guardada.');row.append(info,choices);container.append(row);}}
// Keep polling serial and fetch only the visible view. A summary never opens a score run.
let refreshRequested=false, pollTimer=null, pollStopped=false, pollFailures=0;
let loadedEntitiesKey=null, loadedNewsKey=null;
function schedulePoll(){
  clearTimeout(pollTimer);
  if(pollStopped)return;
  const base=document.hidden?15000:lastStats.operation?.busy?1000:3000;
  const delay=Math.min(document.hidden?60000:15000,base*Math.pow(2,Math.min(pollFailures,3)));
  pollTimer=setTimeout(()=>refresh().catch(()=>{}),delay);
}
async function loadEntities(stats,force=false){
  const query=$('search').value, pageOffset=offset;
  const key=JSON.stringify([query,pageOffset,stats.entities,stats.scores,stats.audit,stats.workflow?.analysis_current]);
  if(!force&&key===loadedEntitiesKey)return;
  const rows=await api('entities?summary=1&q='+encodeURIComponent(query)+'&offset='+pageOffset);
  if(pollStopped)return;
  if(activeTab!=='bank'||query!==$('search').value||pageOffset!==offset){
    if(activeTab==='bank')refreshRequested=true;
    return;
  }
  entities=rows;loadedEntitiesKey=key;renderEntities();
}
function refresh(force=false){
  if(refreshing){if(force)refreshRequested=true;return refreshing;}
  clearTimeout(pollTimer);
  refreshing=(async()=>{
    let reload=force;
    do{
      refreshRequested=false;
      const stats=await api('stats');if(pollStopped)return;lastStats=stats;pollFailures=0;
      if(window.pepsWorkflow)window.pepsWorkflow.update(stats);
      for(const key of ['entities','documents','evidence'])$('m-'+key).textContent=stats[key].toLocaleString('es-MX');
      $('m-jobs').textContent=String((stats.jobs.queued||0)+(stats.jobs.deferred||0)+(stats.jobs.running||0));
      const acquisitionPhase=stats.operation.kind==='download'||stats.operation.kind==='pipeline'&&['preparing','download'].includes(stats.operation.phase);
      $('operation').textContent=acquisitionPhase&&stats.workflow?.activity?.label?stats.workflow.activity.label:stats.operation.busy?(stats.operation.label||'Procesando…'):'Listo para continuar';
      $('mode').textContent=stats.online?'Red autorizada':'Modo cerrado';
      $('jobs').textContent=JSON.stringify(stats.jobs);
      $('errors').textContent=stats.recent_errors.map(e=>`${e.status}: ${e.error}`).join('\n');
      $('capacity').textContent=JSON.stringify(stats.capacity,null,2);
      if(stats.operation.error&&stats.operation.status==='failed')message(stats.operation.error,true);
      if(activeTab==='bank')await loadEntities(stats,reload);
      if(activeTab==='news'){
        const key=JSON.stringify([stats.documents,stats.audit,stats.workflow?.counts?.articles,stats.jobs]);
        if(reload||key!==loadedNewsKey){await loadNews(false);loadedNewsKey=key;}
      }
      reload=refreshRequested;
    }while(refreshRequested&&!pollStopped);
  })().catch(error=>{
    pollFailures++;
    if(!pollStopped)$('operation').textContent='Reconectando con la aplicación local…';
    throw error;
  }).finally(()=>{refreshing=null;schedulePoll();});
  return refreshing;
}
async function switchTab(tab){
  if(window.pepsWorkflow&&!window.pepsWorkflow.canOpenTab(tab,true))return;
  activeTab=tab;
  for(const b of document.querySelectorAll('[data-tab]'))b.classList.toggle('active',b.dataset.tab===tab);
  for(const p of document.querySelectorAll('[data-panel]'))p.hidden=p.dataset.panel!==tab;
  if(window.pepsWorkflow)window.pepsWorkflow.onNavigate(tab);
  if(tab==='sources')await Promise.all([loadSources(),loadNews()]);
  if(tab==='news')await loadNews(false);
  if(tab==='bank')await refresh(true);
}
for(const btn of document.querySelectorAll('[data-tab]'))btn.onclick=()=>switchTab(btn.dataset.tab).catch(error=>message(error.message,true));
let debounce;$('search').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(()=>{offset=0;refresh(true).catch(e=>message(e.message,true));},250);};
action('previous',async()=>{offset=Math.max(0,offset-100);});action('next',async()=>{offset+=100;});
action('score',async()=>{await api('workflow-analyze',{});await switchTab('analysis');message('Análisis iniciado. Puedes seguir las fases y consultar los resultados al terminar.');});
action('import-entities',async()=>{await api('import-entities',{text:$('entity-text').value});message('Importación en curso.');});
action('import-documents',async()=>{await api('import-documents',{text:$('doc-text').value});message('Guardando documentos. El análisis y el score se ejecutarán en el paso 4.');});
for(const [input,text]of [['entity-file','entity-text'],['doc-file','doc-text']])$(input).onchange=async()=>{const f=$(input).files[0];if(f){if(f.size>16000000){message('Archivo mayor de 16 MB; use la CLI para esta carga.',true);return;}$(text).value=await f.text();}};
action('collect',async()=>{await api('collect',{online:$('online').checked});message('Buscando en fuentes permitidas. Puedes seguir consultando el banco.');});
action('stop',async()=>{await api('stop',{operation_id:lastStats.operation?.operation_id});message('Pausa solicitada. Las solicitudes en curso terminarán con su timeout.');});
action('retry',async()=>{await api('retry',{});message('Tareas fallidas reencoladas; los circuitos de dominio conservan su pausa.');});
action('audit-check',async()=>{$('audit-result').textContent=JSON.stringify(await api('audit'),null,2);});
action('backup',async()=>{$('audit-result').textContent=JSON.stringify(await api('backup',{}),null,2);});
action('export',async()=>{const result=await api('export-ticket',{});const a=el('a');a.href=result.url;a.download='webscope-export.jsonl';a.click();message('Exportación por registros iniciada; el navegador la guardará en disco.');});
$('shutdown').onclick=async()=>{try{await api('shutdown',{});document.body.replaceChildren(el('main','WebScope se cerró. Puedes cerrar esta pestaña.'));pollStopped=true;clearTimeout(pollTimer);}catch(e){message(e.message,true);}};

const identityLabels={matched:'Coincidencia corroborada por reglas; requiere revisión',candidate:'Candidato sin identidad confirmada',ambiguous:'Homónimo o identidad ambigua',rejected:'Incompatible con las anclas verificadas'};
function renderIdentity(block,ev){const resolution=ev.identity_resolution;if(!resolution)return;block.append(el('h3',identityLabels[resolution.status]||resolution.status));for(const reason of resolution.reasons||[])block.append(el('p',reason,'muted'));const spans=(resolution.mentions||[]).slice(0,100);if(spans.length){block.append(el('p','Selecciona las menciones que comprobaste que corresponden a esta entidad.','muted'));for(const m of spans){const label=el('label',undefined,'check');const checkbox=el('input');checkbox.type='checkbox';checkbox.className='identity-span';checkbox.dataset.start=String(m.start);checkbox.dataset.end=String(m.end);checkbox.checked=Array.isArray(ev.review?.identity_spans)?ev.review.identity_spans.some(span=>span.start===m.start&&span.end===m.end):!!m.identity_supported||spans.length===1;const chars=codepoints(ev.text);const context=chars.slice(Math.max(0,m.start-45),Math.min(chars.length,m.end+100)).join('');label.append(checkbox,document.createTextNode(context));block.append(label);}}}
function reviewIdentitySpans(block,ev){if(!ev.identity_resolution)return undefined;return Array.from(block.querySelectorAll('.identity-span:checked')).map(e=>({start:Number(e.dataset.start),end:Number(e.dataset.end)}));}
// Reorder the existing cards: toggling strategy must preserve permission fields,
// their focus and any in-flight save rather than rebuilding the form.
function orderNewsSources(){
  const container=$('news-sources'),shared=$('workflow-search-strategy')?.value==='shared';
  const rows=Array.from(container.children),ordered=rows.slice().sort((a,b)=>
    (shared?Number(b.dataset.sharedIndex==='true')-Number(a.dataset.sharedIndex==='true'):0)||
    Number(a.dataset.setupPriority)-Number(b.dataset.setupPriority)||
    Number(a.dataset.catalogueOrder)-Number(b.dataset.catalogueOrder));
  for(const row of rows){const note=row.querySelector('.source-shared-omitted');if(note)note.hidden=!shared;}
  if(ordered.some((row,index)=>container.children[index]!==row))container.append(...ordered);
  const setupRows=['lumy_general_v1','public_research_articles'].map(id=>rows.find(row=>row.dataset.sourceId===id));
  $('public-source-setup').hidden=$('workflow-search-strategy')?.value!=='targeted'||setupRows.some(row=>!row)||setupRows.every(row=>row.dataset.selected==='true');
  const summary=$('workflow-shared-source-summary');summary.hidden=!shared;
  if(shared){const count=rows.filter(row=>row.dataset.sharedIndex==='true').length,text='Primero aparecen '+count+' fuentes con índice compartido. Las demás no generan consultas en este modo. La lectura de originales se configura por separado y no amplía el índice. Habilita y autoriza expresamente las fuentes que quieras utilizar.';if(summary.textContent!==text)summary.textContent=text;}
  filterNewsSources();
}
window.pepsOrderNewsSources=orderNewsSources;
action('prepare-public-sources',async()=>{
  const fields=Array.from(document.querySelectorAll('[data-source-id="lumy_general_v1"] input,[data-source-id="public_research_articles"] input'));
  for(const field of fields){field.disabled=true;field.dataset.permissionSaving='true';}
  const task=(async()=>{
    await api('news-source',{id:'lumy_general_v1',enabled:true,terms_approved:true,download_articles:false,article_terms_approved:false});
    await api('news-source',{id:'public_research_articles',enabled:true,terms_approved:true,download_articles:true,article_terms_approved:true});
  })();
  sourceSaves.add(task);window.pepsWorkflow?.update(lastStats);
  try{await task;message('Búsqueda API y lectura pública preparadas. Puedes revisar ambos permisos antes de iniciar.');}
  finally{try{await loadNews();await window.pepsWorkflow?.sourcesChanged();await refresh(true);}finally{sourceSaves.delete(task);for(const field of fields)delete field.dataset.permissionSaving;window.pepsWorkflow?.update(lastStats);}}
});
$('news-register-scope').onchange=()=>loadNews(false).catch(error=>message(error.message,true));
function filterNewsSources(){
  const query=($('source-filter')?.value||'').trim().toLocaleLowerCase('es'),selectedOnly=!!$('source-selected-only')?.checked,showAll=!!$('source-show-all')?.checked;
  const nominal=!['shared','feeds'].includes($('workflow-search-strategy')?.value);
  let visible=0,total=0;for(const row of $('news-sources').children){total++;const selected=row.dataset.selected==='true',recommended=nominal?Number(row.dataset.setupPriority)<2:row.dataset.recommended==='true';const show=(!query||row.dataset.searchText.includes(query))&&(!selectedOnly||selected)&&(!!query||showAll||selectedOnly||selected||recommended);row.hidden=!show;if(show)visible++;}
  $('source-filter-count').textContent=visible+' de '+total+' fuentes';
}
async function loadNews(includeSources=true){
  const scope=$('news-register-scope').value,query='&scope='+encodeURIComponent(scope);
  const data=await api((includeSources?'news?relevant=1':'news?summary=1&relevant=1')+query);if(pollStopped)return;
  $('news-register-status').textContent=data.articles.length.toLocaleString('es-MX')+' referencias mostradas'+(data.articles.length===100?' · las 100 más recientes de este filtro':'')+'.';
  $('news-summary').textContent=JSON.stringify(data.stats,null,2);if(data.plan!==undefined)$('news-plan').textContent=JSON.stringify(data.plan,null,2);
  const articlesKey=JSON.stringify(data.articles||[]);
  if(articlesKey!==renderedNewsArticlesKey){renderedNewsArticlesKey=articlesKey;const articles=$('news-articles');articles.replaceChildren();
    for(const article of data.articles){
      const multimedia=article.scope==='multimedia_reference'||article.status==='multimedia_reference',profile=article.scope==='profile_reference',duplicate=article.scope==='duplicate_reference',hasText=article.scope==='full_article'&&!!article.document_id;
      const row=el('div',undefined,'evidence');row.dataset.articleScope=article.scope||'';row.dataset.hasText=String(hasText);
      const scope=profile?'Perfil o directorio; no es una noticia':duplicate?'Réplica de un texto ya conservado':multimedia?'Referencia de audio/vídeo; falta transcripción':hasText?'Texto útil conservado':'Referencia sin cuerpo';
      const status=profile||duplicate||multimedia?'Captura original conservada':({fetched:hasText?'Texto conservado':'Referencia conservada',discovered:'Pendiente de texto',failed:'Con error'}[article.status]||article.status);
      row.append(el('strong',article.title||article.canonical_url),el('p',article.publisher_id,'muted'),el('span',scope,'badge'),el('p',status,'muted'));
      if(multimedia)row.append(el('p','El reproductor no aporta una transcripción. Esta referencia se conserva para revisión y no se utiliza como texto del artículo ni para el score.','muted'));
      if(profile)row.append(el('p','Esta ficha se conserva como referencia. No cuenta como noticia ni queda pendiente de descarga.','muted'));
      if(duplicate)row.append(el('p','La procedencia se conserva para auditoría. El texto se cuenta una sola vez y no duplica evidencias ni puntuación.','muted'));
      row.append(sourceLink(article.canonical_url,'Abrir fuente original'));articles.append(row);
    }
    if(!data.articles.length)articles.append(el('p','No hay referencias coincidentes para la lista actual. Los documentos de búsquedas anteriores se conservan en el banco.','muted'));
  }
  if(!includeSources)return;
  const container=$('news-sources');container.replaceChildren();
  for(const source of data.sources){
    const row=el('div',undefined,'source compact-source'),info=el('div',undefined,'compact-source-info'),controls=el('div',undefined,'compact-source-controls');
    const name=source.source_name||source.id,publicSearch=['mwmbl','duckduckgo_html','duckduckgo_lite','searxng','lemmy_search','bing_news_rss'].includes(source.kind);
    const publicBodies=source.discover_public_article_hosts===true&&source.public_research_scope==='personal_local_research';
    const aggregator=source.discovery_only===true||publicSearch||source.kind==='gdelt'||source.search_kind==='google_news_rss'||!!source.documented_api_permission,sharedIndex=['wordpress_api','sitemap'].includes(source.kind);
    const sourceUrl=source.url||source.search_url||source.api_url||source.base_url||'';
    const lumy=source.id==='lumy_general_v1';
    row.dataset.recommended=String(publicBodies||['wordpress_api','mwmbl','duckduckgo_html','searxng'].includes(source.kind));row.dataset.sourceId=source.id;row.dataset.sharedIndex=String(sharedIndex);row.dataset.catalogueOrder=String(container.children.length);row.dataset.setupPriority=String(lumy?0:source.id==='public_research_articles'?1:2);row.dataset.searchText=(name+' '+sourceUrl).toLocaleLowerCase('es');
    info.append(el('strong',lumy?'Búsqueda API · Lumy / Yandex':publicBodies?'Lectura de originales públicos':name));if(sharedIndex)row.classList.add('shared-source');
    const badge=el('span',undefined,'badge source-permission-badge');info.append(badge);
    const details=el('details',undefined,'source-conditions');details.append(el('summary','Alcance y condiciones'));
    if(sourceUrl&&!publicBodies)details.append(sourceLink(sourceUrl,sourceUrl));
    if(lumy){details.append(sourceLink(source.documented_api_permission?.operator_documentation_url,'Documentación pública del operador'),sourceLink(source.documented_api_permission?.api_documentation_url,'Contrato de la API JSON'));}
    details.append(el('p',source.review_note||'Revisa las condiciones públicas de esta fuente antes de autorizar su consulta o conservar su texto.'));
    if(sharedIndex)details.append(el('p',source.search_kind==='wordpress_api_search'?'Búsqueda por nombre · también admite índice compartido':'Índice compartido · una página para toda la lista','source-shared-kind'));
    if(aggregator)details.append(el('p','Sólo enlaces y extractos. El texto se obtiene de editores autorizados por separado.'));
    if(publicSearch)info.append(el('p',lumy?'Busca cada nombre con Yandex mediante la API JSON de Lumy. Conserva enlaces y extractos; el permiso de consulta no autoriza el texto de los editores.':'Busca los nombres y conserva referencias. Su permiso de consulta no autoriza el contenido de los editores.','muted'));
    if(publicBodies)info.append(el('p','Lee originales públicos de enlaces pertinentes ya encontrados. Uso personal local, con robots y sin iniciar sesión ni superar suscripciones. No concede derechos comerciales del editor.','muted'));
    if(!sharedIndex){const note=el('p',publicBodies?'Esta modalidad lee originales ya encontrados; no busca nombres ni recorre índices.':aggregator?'En índice compartido este buscador no genera consultas. La lectura de sus enlaces se autoriza por separado.':'En índice compartido no genera consultas. Puede aportar texto autorizado de los enlaces encontrados.','source-shared-omitted');note.hidden=true;details.append(note);}
    info.append(details);
    let saved={enabled:!!source.enabled,terms_approved:!!source.terms_approved,download_articles:!!source.download_articles,article_terms_approved:!!(source.article_terms_approved||source.body_terms_approved)};
    const consult=el('input'),body=el('input');consult.type=body.type='checkbox';consult.checked=saved.enabled&&saved.terms_approved;body.checked=saved.download_articles&&saved.article_terms_approved;
    consult.setAttribute('aria-label','Consultar '+name+'; condiciones revisadas');body.setAttribute('aria-label','Conservar texto de '+name+'; uso revisado');
    const choice=(input,label)=>{const field=el('label',undefined,'source-consent');field.append(input,document.createTextNode(label));controls.append(field);};
    if(publicBodies)choice(body,'Conservar originales públicos · uso personal local');else{choice(consult,'Consultar · condiciones revisadas');if(!aggregator)choice(body,'Conservar texto · uso revisado');}
    body.setAttribute('aria-label',publicBodies?'Conservar originales públicos de enlaces encontrados; uso personal local':'Conservar texto de '+name+'; uso revisado');
    const update=()=>{const active=saved.enabled&&saved.terms_approved,text=active&&!aggregator&&saved.download_articles&&saved.article_terms_approved;row.dataset.selected=String(active);badge.textContent=publicBodies?(text?'Lectura pública activada':'Lectura pública sin activar'):source.automated_search===false?'Consulta manual':text?'Con texto autorizado':active?'Sólo referencias':'Sin seleccionar';row.classList.toggle('source-selected',active);filterNewsSources();};
    const save=async()=>{
      const fields=publicBodies?[body]:aggregator?[consult]:[consult,body],values={enabled:publicBodies?body.checked:consult.checked,terms_approved:publicBodies?body.checked:consult.checked,download_articles:!aggregator&&body.checked,article_terms_approved:!aggregator&&body.checked};
      for(const field of fields){field.disabled=true;field.dataset.permissionSaving='true';}
      const task=(async()=>{try{await api('news-source',{id:source.id,...values});saved=values;update();await window.pepsWorkflow?.sourcesChanged();await refresh(true);}catch(error){consult.checked=saved.enabled&&saved.terms_approved;body.checked=saved.download_articles&&saved.article_terms_approved;message(error.message,true);}})();
      sourceSaves.add(task);window.pepsWorkflow?.update(lastStats);
      try{await task;}finally{sourceSaves.delete(task);for(const field of fields){delete field.dataset.permissionSaving;field.disabled=false;}window.pepsWorkflow?.update(lastStats);}
    };
    consult.onchange=save;body.onchange=save;if(source.automated_search===false&&!publicBodies)controls.replaceChildren(el('p','Consulta manual disponible en los expedientes.','muted'));row.append(info,controls);container.append(row);update();
  }
  orderNewsSources();filterNewsSources();
}
let manualEntityRequest=0,manualLinksRequest=0,manualTimer;
function resetManualLinks(){manualLinksRequest++;$('manual-search-results').replaceChildren();$('manual-entity-select').value='';}
async function loadManualEntities(){
  const request=++manualEntityRequest;const status=$('manual-search-status'),select=$('manual-entity-select');resetManualLinks();select.disabled=true;status.textContent='Buscando en tu lista…';
  try{const entities=await api('entities?summary=1&limit=100&q='+encodeURIComponent($('manual-entity-query').value));if(request!==manualEntityRequest||pollStopped)return;select.replaceChildren();const placeholder=el('option','Elige una entidad…');placeholder.value='';select.append(placeholder);for(const entity of entities){const option=el('option',entity.name);option.value=entity.id;select.append(option);}status.textContent=entities.length?entities.length+' entidades mostradas. Escribe parte del nombre para filtrar la lista.':'No hay coincidencias en tu lista.';}
  catch(error){if(request===manualEntityRequest){select.replaceChildren();status.textContent='No se pudo consultar la lista: '+error.message;}}
  finally{if(request===manualEntityRequest)select.disabled=false;}
}
$('workflow-manual-discovery').ontoggle=()=>{if($('workflow-manual-discovery').open){loadManualEntities();}};
$('manual-entity-query').oninput=()=>{manualEntityRequest++;resetManualLinks();$('manual-entity-select').disabled=true;clearTimeout(manualTimer);manualTimer=setTimeout(loadManualEntities,250);};
$('manual-entity-select').onchange=async()=>{const id=$('manual-entity-select').value,request=++manualLinksRequest,box=$('manual-search-results');box.replaceChildren();if(!id)return;box.append(el('p','Preparando enlaces de búsqueda…','muted'));try{const data=await api('public-search?id='+encodeURIComponent(id));if(request!==manualLinksRequest||pollStopped)return;box.replaceChildren(el('p',data.note||'Sólo búsquedas manuales en sitios públicos.','muted'));for(const item of data.links||[]){if(!/^https?:\/\//i.test(item.url||''))continue;const row=el('p');row.append(sourceLink(item.url,item.label||item.channel||'Abrir búsqueda'));box.append(row);}}catch(error){if(request===manualLinksRequest)box.replaceChildren(el('p','No se pudieron preparar los enlaces: '+error.message,'muted'));}};
$('source-filter').oninput=filterNewsSources;$('source-selected-only').onchange=filterNewsSources;$('source-show-all').onchange=filterNewsSources;
action('news-acquire',async()=>{await api('news-acquire',{online:$('news-online').checked});message('Adquisición en curso: fuentes compartidas y enlaces únicos.');});
action('news-attribute',async()=>{await api('workflow-analyze',{});message('Atribución y cálculo iniciados. Las ambigüedades conservan su estado.');});

if(window.pepsWorkflow)window.pepsWorkflow.init({api,refresh,switchTab,message});
if(window.pepsImport)window.pepsImport.init({api,refresh,switchTab,message,onImported:async()=>{
  await refresh(true);await switchTab('sources');
},openBank:async()=>{
  await switchTab('bank');$('search').value='';offset=0;await refresh(true);
},showDetail:async item=>{
  await switchTab('bank');$('search').value=item.name||'';offset=0;await refresh(true);await showDetail(item);
}});

document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!pollStopped)refresh(true).catch(()=>{});else schedulePoll();});
refresh().catch(e=>message(e.message,true));
