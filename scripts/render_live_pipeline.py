"""Publish counts from a real pipeline receipt, without bodies or personal scores."""
from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime
from html import escape
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def table(rows, columns):
    return '<table><thead><tr>' + ''.join('<th>' + escape(label) + '</th>' for _, label in columns) + '</tr></thead><tbody>' + ''.join(
        '<tr>' + ''.join('<td>' + escape(str(row[key]) if row.get(key) is not None else '—') + '</td>' for key, _ in columns) + '</tr>' for row in rows) + '</tbody></table>'


def rate(value):
    return 'No estimable' if value['value'] is None else f'{100*value["value"]:.1f}% ({value["numerator"]}/{value["denominator"]})'


def render(receipt, output, baseline=None, supplements=(), intermediates=(), derived=None):
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Use un destino vacío: no se sobrescriben informes ni archivos existentes.')
    raw = json.loads(receipt.read_text(encoding='utf-8'))
    cold = json.loads(baseline.read_text(encoding='utf-8')) if baseline else None
    middle = [(path, json.loads(path.read_text(encoding='utf-8'))) for path in intermediates]
    bridge = json.loads(derived.read_text(encoding='utf-8')) if derived else None
    if bridge:
        bm = bridge['method']
        if (bm.get('origin_report_sha256') != hashlib.sha256(receipt.read_bytes()).hexdigest()
                or bm.get('real_transport') is not True or bm.get('production_scoring') is not True
                or bm.get('new_search_queries') is not False or bm.get('gold_urls_injected') is not False
                or {(p['entity_id'], p['name']) for p in bridge['people_results']} !=
                   {(p['entity_id'], p['name']) for p in raw['people_results']}
                or bridge['summary']['unique_original_documents_before'] != raw['summary']['unique_original_documents']):
            raise ValueError('La ampliación debe derivar exactamente del banco final y conservar sus personas.')
    if any(previous['frozen_inputs'][key] != raw['frozen_inputs'][key]
           for previous in ([cold] if cold else []) + [value for _, value in middle]
           for key in ('roster_sha256', 'people_sha256', 'plan_sha256', 'effective_config_sha256')):
        raise ValueError('El frío y la reanudación deben conservar personas, catálogo y plan.')
    method = raw['method']
    if not all(method.get(key) is True for key in ('real_transport', 'real_clock', 'production_pipeline')) or any(
        method.get(key) is not False for key in ('response_substitution', 'synthetic_documents', 'gold_urls_used_for_search', 'automated_human_confirmation')):
        raise ValueError('El informe exige una corrida real, sin sustitución de respuestas ni entradas sembradas.')
    reserved = {'index.html', 'README.md', 'entities.csv', 'sources.csv', 'originals.csv',
                'queries.csv', 'summary.json', 'manifest.json', 'font-licenses.txt', 'cold-attempt-summary.json'}
    names = [path.name for path in supplements]
    if len(set(names)) != len(names) or reserved.intersection(names):
        raise ValueError('Los suplementos públicos requieren nombres distintos de los archivos generados.')
    if any(not path.is_file() or path.suffix not in {'.md', '.json', '.csv', '.html', '.txt'} for path in supplements):
        raise ValueError('Cada suplemento debe ser un archivo público revisado en formato de texto.')
    output.mkdir(parents=True, exist_ok=True)
    summary = dict(raw['summary'])
    if bridge:
        summary.update(people_with_original_text=bridge['summary']['people_with_original_text_after'],
                       unique_original_documents=bridge['summary']['unique_original_documents_after'],
                       current_candidate_urls=bridge['summary']['current_candidate_urls'],
                       production_scores_computed=bridge['summary']['production_scores_computed'],
                       assessment_states=bridge['summary']['assessment_states'])
        # Group breakdowns and pair counts from the base are not final-derived metrics.
        for key in ('groups', 'original_entity_text_pairs', 'current_candidate_entity_pairs', 'unique_original_body_hashes'):
            summary.pop(key, None)
        summary.pop('people_with_fetch_attempt_this_session', None)
        summary.update(people_with_current_candidates=sum(bool(p['current_candidate_urls']) for p in bridge['people_results']),
                       actual_http_requests_cumulative=bridge['summary']['inherited_actual_http_requests'] + bridge['summary']['new_actual_http_requests'],
                       actual_http_requests_this_session=bridge['summary']['new_actual_http_requests'],
                       http_status_counts_this_session=bridge['summary']['http_status_counts'],
                       fetch_outcome_counts_this_session=bridge['summary']['fetch_outcome_counts'],
                       pending_jobs=bridge['summary']['pending_jobs'],
                       job_status_counts=bridge['summary']['job_status_counts'])
        summary['scope'] = 'Latest derived publisher copy; original nominal search counts inherited, not repeated.'
    public_people = []
    for person in (bridge or raw)['people_results']:
        public_people.append({key: person.get(key) for key in ('entity_id', 'name', 'entity_group', 'queries_planned',
            'queries_completed', 'query_actual_http_requests_cumulative', 'current_candidate_urls', 'original_documents')} |
            {'assessment_state': person['assessment']['state'], 'identity_confirmations_automated': False})
    source_counts = {}
    for query in raw['query_results']:
        row = source_counts.setdefault(query['source_id'], {'source_id': query['source_id'], 'planned': 0, 'completed': 0, 'pending': 0, 'blocked_failed': 0})
        row['planned'] += 1
        row['completed'] += int(query['discovery_receipt_saved'])
        row['pending'] += int(query['status'] in ('queued', 'running', 'deferred'))
        row['blocked_failed'] += int(query['status'] in ('blocked', 'failed', 'dead'))
    for metrics in raw.get('source_results', []):
        row = source_counts.get(metrics['source_id'])
        if row is not None:
            row.update(http=metrics['actual_http_this_session'], originals=metrics['original_documents_cumulative'],
                       http_p95_resume_seconds=metrics['transport_latency']['p95_seconds'],
                       http_p95_cold_seconds=None)
    if cold:
        for metrics in cold.get('source_results', []):
            row = source_counts.get(metrics['source_id'])
            if row is not None:
                row['http'] += metrics['actual_http_this_session']
                row['http_p95_cold_seconds'] = metrics['transport_latency']['p95_seconds']
    middle_latency_fields = []
    for index, (_, previous) in enumerate(middle, 1):
        field = f'http_p95_resume_{index}_seconds'
        middle_latency_fields.append(field)
        for metrics in previous.get('source_results', []):
            row = source_counts.get(metrics['source_id'])
            if row is not None:
                row['http'] += metrics['actual_http_this_session']
                row[field] = metrics['transport_latency']['p95_seconds']
    sources = list(source_counts.values())
    original_fields = ('article_id', 'document_id', 'canonical_url', 'published_at', 'body_chars',
                       'body_sha256', 'raw_snapshot_sha256', 'raw_snapshot_hash_valid')
    originals = [{key: body.get(key) for key in original_fields} | {'article_id': body['id']}
                 for body in (bridge or raw)['original_bodies']]
    query_rows = [{key: row.get(key) for key in ('id', 'source_id', 'status', 'attempts', 'query_url', 'discovery_receipt_saved')}
                  for row in raw['query_results']]
    public = {'schema_version': 1, 'version': '2.9.5', 'generated_at': raw['generated_at'], 'method': method,
              'started_at': raw['run']['started_at'], 'completed_at': raw['run']['completed_at'],
              'online_e2e_seconds': raw['run']['online_e2e_seconds'],
              'first_original_body_seconds': raw['run']['first_original_body_seconds'],
              'timings_seconds': raw['timings_seconds'], 'frozen_inputs': raw['frozen_inputs'],
              'summary': summary, 'checks': raw['operational_checks'], 'partial_reasons': raw.get('partial_reasons', []),
              'source_sha256': raw['source_hashes_before'], 'original_receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
              'limitations': ['La muestra es de conveniencia; no representa la prevalencia nacional.',
                'Una URL o hash distinto no garantiza una historia independiente.',
                'Un score calculado puede tener estado insuficiente o identidad pendiente: no acredita ausencia de riesgo.',
                'Los documentos y riesgos personales no se publican. No se crearon revisiones humanas automáticas.',
                'La ejecución real no demuestra precisión o recall sin verdad documental revisada independiente.',
                'Las fuentes consultadas, sus topes, las esperas y la cobertura parcial limitan el resultado.']}
    # Frozen inputs contain hashes/counts, not private input paths or article text.
    public['frozen_inputs'] = {key: value for key, value in raw['frozen_inputs'].items()
                               if key.endswith('sha256') or key in ('people', 'groups', 'plan_queries', 'maximum_http_total', 'body_budget', 'per_entity_body_limit')}
    public['public_supplements'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in supplements}
    if bridge:
        public['base_summary'] = raw['summary']
        public['derived_attempt'] = {'receipt_sha256': hashlib.sha256(derived.read_bytes()).hexdigest(),
            'started_at': bridge['run']['started_at'], 'completed_at': bridge['run']['completed_at'],
            'total_seconds': bridge['run']['total_seconds'], 'summary': bridge['summary'],
            'timings_seconds': bridge['timings_seconds'], 'checks': bridge['operational_checks'],
            'partial_reasons': bridge['partial_reasons'], 'source_sha256': bridge['method']['source_hashes']}
    public['intermediate_attempts'] = [
        {'receipt_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
         'started_at': value['run']['started_at'], 'completed_at': value['run']['completed_at'],
         'online_e2e_seconds': value['run']['online_e2e_seconds'], 'timings_seconds': value['timings_seconds'],
         'summary': value['summary'], 'checks': value['operational_checks'],
         'partial_reasons': value['partial_reasons']}
        for path, value in middle]
    public['online_e2e_seconds_across_attempts'] = sum(value['run']['online_e2e_seconds'] for value in
        ([cold] if cold else []) + [value for _, value in middle] + [raw])
    public['measured_attempt_total_seconds'] = sum(value['timings_seconds']['total_until_report'] for value in
        ([cold] if cold else []) + [value for _, value in middle] + [raw]) + (bridge['run']['total_seconds'] if bridge else 0)
    for path in supplements:
        (output / path.name).write_bytes(path.read_bytes())
    if cold:
        public['cold_attempt'] = {'receipt_sha256':hashlib.sha256(baseline.read_bytes()).hexdigest(),
            'started_at':cold['run']['started_at'], 'completed_at':cold['run']['completed_at'],
            'online_e2e_seconds':cold['run']['online_e2e_seconds'], 'timings_seconds':cold['timings_seconds'],
            'summary':cold['summary'], 'checks':cold['operational_checks'],
            'partial_reasons':cold['partial_reasons']}
        public['elapsed_between_attempts_seconds'] = (
            datetime.fromisoformat(raw['run']['completed_at'].replace('Z','+00:00')) -
            datetime.fromisoformat(cold['run']['started_at'].replace('Z','+00:00'))).total_seconds()
        public['time_scope'] = 'Cold and corrected reuse are separate real attempts; final counts are cumulative, not sums of their corpora.'
        (output / 'cold-attempt-summary.json').write_text(json.dumps(public['cold_attempt'],ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_csv(output / 'entities.csv', public_people, tuple(public_people[0]))
    write_csv(output / 'sources.csv', sources, ('source_id', 'planned', 'completed', 'pending', 'blocked_failed', 'http', 'originals', 'http_p95_cold_seconds', *middle_latency_fields, 'http_p95_resume_seconds'))
    write_csv(output / 'originals.csv', originals, original_fields)
    write_csv(output / 'queries.csv', query_rows, ('id', 'source_id', 'status', 'attempts', 'query_url', 'discovery_receipt_saved'))
    (output / 'summary.json').write_text(json.dumps(public, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    fonts = ''
    for family, filename in [('Lato', 'Lato-Regular.woff2'), ('Source Serif 4', 'SourceSerif4-Roman.woff2')]:
        encoded = base64.b64encode((ROOT / 'peps/web/fonts' / filename).read_bytes()).decode()
        fonts += f"@font-face{{font-family:'{family}';src:url(data:font/woff2;base64,{encoded}) format('woff2');font-display:swap;}}"
    cards = [('Personas reales', summary['people']), ('Consultadas por red', summary['people_with_actual_query_http']),
             ('Con al menos una consulta completada', summary['people_with_completed_nominal_search']),
             ('Con texto original', summary['people_with_original_text']),
             ('Artículos con cuerpo conservado', summary['unique_original_documents']),
             ('Scores procesados', summary['production_scores_computed'])]
    cards_html = ''.join(f'<div class="card"><b>{escape(str(value))}</b><span>{escape(label)}</span></div>' for label, value in cards)
    phase_labels = {'import': 'Importar y congelar la lista', 'import_and_freeze': 'Importar y congelar la lista', 'report': 'Preparar el informe', 'search_and_original_download': 'Buscar y descargar originales',
                    'identity_attribution': 'Resolver identidad y atribuir evidencia', 'production_scoring': 'Calcular resultados',
                    'replay_and_explanations': 'Reproducir scores y generar explicaciones', 'production_export': 'Exportar resultados',
                    'actual_local_cache_reuse': 'Comprobar la caché real en copia', 'total_until_report': 'Total hasta el informe'}
    state_labels = {'evaluated': 'Evaluado', 'pending_identity': 'Identidad pendiente', 'insufficient_text': 'Falta texto original',
                    'insufficient_evidence': 'Evidencia insuficiente', 'not_computed': 'Sin cálculo'}
    phases = [{'phase': phase_labels.get(key,key), 'seconds': f'{value:.3f}'} for key, value in raw['timings_seconds'].items()]
    if cold:
        phases = ([{'phase':'Frío · '+phase_labels.get(key,key), 'seconds':f'{value:.3f}'} for key,value in cold['timings_seconds'].items()] +
                  [row for index, (_, previous) in enumerate(middle, 1) for row in
                   [{'phase':f'Reanudación {index} · '+phase_labels.get(key,key), 'seconds':f'{value:.3f}'}
                    for key,value in previous['timings_seconds'].items()]] +
                  [{'phase':'Reanudación final · '+row['phase'], 'seconds':row['seconds']} for row in phases])
    states = [{'state': state_labels.get(key,key), 'people': value} for key, value in summary['assessment_states'].items()]
    failed = [name for name, passed in raw['operational_checks'].items() if not passed]
    status = 'Comprobaciones técnicas aprobadas' if not failed else 'Hay comprobaciones técnicas fallidas'
    partial = raw.get('partial_reasons', [])
    details = '<ul>' + ''.join('<li>' + escape(str(value)) + '</li>' for value in partial + failed) + '</ul>' if partial or failed else '<p>No quedaron límites operativos pendientes registrados.</p>'
    page = '''<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>WebScope · Pipeline real de 300 personas</title><style>'''+ fonts + '''
    :root{color-scheme:light;--navy:#072146;--blue:#004481;--aqua:#028484;--muted:#536476;--line:#d5e0e9}*{box-sizing:border-box}body{margin:0;background:#f3f7fa;color:var(--navy);font:16px/1.6 Lato,sans-serif}main{max-width:1180px;margin:auto;padding:48px 24px 80px}header{border-top:5px solid var(--aqua);padding:28px 0}h1,h2{font-family:'Source Serif 4',serif;line-height:1.2;font-weight:400}h1{font-size:clamp(2rem,5vw,3.7rem);max-width:850px;margin:12px 0}h2{font-size:1.8rem;margin-top:0}.eyebrow{font-size:.78rem;letter-spacing:.15em;text-transform:uppercase;color:var(--muted)}.lead{max-width:850px;color:var(--muted);font-size:1.1rem}.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:24px 0}.card,section{background:white;border:1px solid var(--line);border-radius:14px;padding:24px}.card b{font-size:2.6rem;line-height:1.2;display:block}.card span{color:var(--muted)}section{margin:20px 0}.scroll{overflow-x:auto}table{border-collapse:collapse;min-width:100%;font-size:.9rem}th{text-align:left;color:var(--muted);font-weight:400}th,td{padding:12px;border-bottom:1px solid var(--line);vertical-align:top}td{overflow-wrap:anywhere}input,select{font:inherit;padding:10px 14px;border:1px solid #93a9bd;border-radius:8px;min-height:44px}input{width:min(100%,380px)}a{color:var(--blue);text-underline-offset:4px}.notice{border-left:4px solid #a95b00;padding-left:18px}.files{display:flex;gap:20px;flex-wrap:wrap}.small{font-size:.86rem;color:var(--muted)}@media(max-width:650px){main{padding:24px 16px}.cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.card,section{padding:18px}.card b{font-size:2rem}}@media print{body{background:white}section{break-inside:avoid}input{display:none}.scroll{overflow:visible}}
    </style><main><header><div class="eyebrow">WebScope 2.9.5 · Evaluación real · 17 octubre 2026</div><h1>De 300 nombres al resultado documental.</h1><p class="lead">Búsquedas, respuestas HTTP, textos originales, atribución, scoring, explicaciones y exportación ejecutados con el pipeline de producción. Sin documentos ni respuestas simuladas.</p></header>'''
    page += '<div class="cards">' + cards_html + '</div>'
    if bridge:
        bs = bridge['summary']
        page += '<section><h2>Resultado final con la ampliación de editores</h2><p>Las tarjetas, las 300 filas y el CSV de originales muestran la copia ampliada: <b>' + str(bs['unique_original_documents_after']) + ' artículos y ' + str(bs['people_with_original_text_after']) + ' personas con texto</b>. Se agregaron ' + str(bs['new_unique_original_documents']) + ' artículos de nueve editores entre enlaces ya descubiertos, con ' + str(bs['new_actual_http_requests']) + ' HTTP nuevos y cero búsquedas nominales adicionales.</p><p>Este brazo tardó ' + f'{bridge["run"]["total_seconds"]:.2f}' + ' segundos completos y se mide aparte del banco base. <a href="publisher-bridge-summary.json">Ver tiempos, alcance y 28 comprobaciones</a>.</p><p class="notice">Tener texto candidato no implica identidad confirmada ni evidencia suficiente para evaluar. La cobertura sigue siendo parcial.</p></section>'
    supplied = {path.name: path for path in supplements}
    if 'quality-metrics.json' in supplied and 'reference-recall.json' in supplied:
        quality = json.loads(supplied['quality-metrics.json'].read_text(encoding='utf-8'))
        references = json.loads(supplied['reference-recall.json'].read_text(encoding='utf-8'))
        qr = next(row for row in quality['runs'] if row['id'] == 'publisher_bridge_derived')
        rr = next(row for row in references['runs'] if row['id'] == 'publisher_bridge_derived')
        observed = qr['quality']['fixed_pair_probability_sample']['observed_detector']
        measured = [{'metric':label, 'value':rate(observed[key])} for key,label in
                    [('precision','Precisión del detector de papel adverso'),('recall','Recall del detector'),
                     ('accuracy_penalizing_abstention','Accuracy penalizando abstenciones'),('f1','F1 del detector')]]
        for key,label in [('exact_reference_url_recall_any_searched_entity','Recuperación de URLs conocidas · exacta'),
                          ('verified_redirect_reference_url_recall_any_searched_entity','Recuperación de URLs conocidas · con redirecciones verificadas')]:
            measured.append({'metric':label,'value':rate(rr['stages']['original_texts'][key])})
        page += '<section><h2>Calidad medida: la recuperación sigue siendo limitada</h2><p>Medición del banco ampliado antes de las correcciones posteriores a la auditoría. El detector se evaluó sobre pares persona/documento de una muestra probabilística; las etiquetas fueron realizadas por agentes y bloqueadas antes de abrir predicciones individuales. La precisión se presenta junto a sus denominadores, recall y abstenciones.</p>' + table(measured,[('metric','Medición'),('value','Resultado observado')]) + '<p class="notice">Cero de los 44 documentos adversos revisados llegó a un componente provisional positivo por las restricciones de identidad y selección. La accuracy de un score numérico y la identidad legal no se estiman aquí. Las referencias conocidas pertenecen a los 60 notarios reutilizados; no miden recall de toda la web ni de los 240 adicionales.</p><p>Los estratos, ponderaciones, desconocidos, marcos rechazados y límites de ceguera se conservan en <a href="quality-metrics.json">las métricas completas</a> y <a href="reference-recall.json">el recall de referencia</a>. Las correcciones posteriores sobre estos mismos textos son desarrollo retrospectivo.</p></section>'
    if 'post-audit-comparison.json' in supplied:
        comparison = json.loads(supplied['post-audit-comparison.json'].read_text(encoding='utf-8'))['headline']
        if comparison.get('retrospective') is not True or comparison.get('held_out') is not False:
            raise ValueError('La comparación posterior debe declararse retrospectiva y sin evaluación fuera de muestra.')
        pairs = [{'metric':label,'before':rate(comparison[key+'_before']), 'after':rate(comparison[key+'_after'])}
                 for key,label in [('recall','Recall del detector'),('accuracy','Accuracy penalizando abstenciones')]]
        page += '<section><h2>Corrección posterior: comparación sobre los mismos textos</h2><p>Tras abrir las etiquetas se corrigieron dos patrones explícitos y se ejecutaron de nuevo la atribución y los 300 cálculos en una copia de los 401 artículos reales. Esta es una medición de desarrollo retrospectivo; no es otra búsqueda ni una validación independiente.</p>' + table(pairs,[('metric','Medición'),('before','NLP 1.0.4'),('after','NLP 1.0.5')]) + '<p>Los falsos positivos observados permanecieron en cero. La recuperación de referencias continúa en 6/97 y la identidad sigue pendiente; el cambio no crea confirmaciones humanas ni evaluaciones positivas.</p><p><a href="post-audit-comparison.json">Comparación pareada completa</a> · <a href="post-audit-processing-summary.json">43 comprobaciones del reprocesamiento real</a></p></section>'
    if cold:
        timing_text = f'<b>{cold["run"]["online_e2e_seconds"]/60:.2f} min de ejecución fría</b>. ' + ' '.join(f'Reanudación {index}: <b>{previous["run"]["online_e2e_seconds"]/60:.2f} min</b>.' for index,(_,previous) in enumerate(middle,1)) + f' Reanudación final: <b>{raw["run"]["online_e2e_seconds"]/60:.2f} min</b>. Tiempo transcurrido entre inicio y cierre final: {public["elapsed_between_attempts_seconds"]/60:.2f} min, incluidas comprobaciones y el intervalo para corregir. Las reanudaciones usan las noticias realmente descargadas; su duración no representa otra búsqueda fría.'
        first_body = cold['run']['first_original_body_seconds']
    else:
        timing_text = f'<b>{raw["run"]["online_e2e_seconds"]/60:.2f} minutos</b> desde el inicio hasta el cierre online.'
        first_body = raw['run']['first_original_body_seconds']
    page += '<section><h2>Tiempo real y alcance</h2><p>' + timing_text + '</p><p>Primer original conservado en frío: <b>' + escape(str(first_body)) + ' segundos</b>.</p><p class="notice">Las búsquedas terminadas y los scores procesados no equivalen a identidades confirmadas ni a personas sin incidentes. El detalle de estados está separado.</p><div class="scroll">' + table(phases, [('phase','Etapa'),('seconds','Segundos observados')]) + '</div><p class="small">Las pausas de cada fuente forman parte del tiempo observado. La comprobación de caché usa los textos reales de este banco y se muestra por separado.</p></section>'
    page += '<section><h2>Qué resultado pudo producirse</h2>' + table(states,[('state','Estado documental'),('people','Personas')]) + '<p class="small">pending_identity exige resolver la identidad; insufficient_text e insufficient_evidence no se convierten en un cero evaluado. Los valores numéricos individuales permanecen en el banco local.</p></section>'
    page += '<section><h2>Fuentes y tareas de la búsqueda original</h2><p>Estos conteos corresponden al banco base y sus once fuentes, antes de los nueve editores adicionales.</p><div class="scroll">' + table(sources,[('source_id','Fuente'),('planned','Consultas'),('completed','Con recibo'),('pending','Pendientes'),('blocked_failed','Bloqueadas/error'),('http','HTTP reales'),('originals','Originales'),('http_p95_cold_seconds','HTTP p95 frío · s'), *[(field,f'HTTP p95 reanudación {index} · s') for index,field in enumerate(middle_latency_fields,1)],('http_p95_resume_seconds','HTTP p95 final · s')]) + '</div><p class="small">p95 mide el transporte de cada intento por separado; las pausas de cortesía se incluyen en el tiempo de adquisición. Los originales son URLs con texto, no historias independientes.</p></section>'
    page += '<section><h2>' + escape(status) + '</h2>' + details + '<p class="small">Una ejecución puede conservar resultados válidos y tener cobertura parcial. Los fallos no se eliminan del informe.</p></section>'
    if cold:
        earlier = [name for name,passed in cold['operational_checks'].items() if not passed]
        page += '<section><h2>La primera ejecución también se conserva</h2><p>' + escape(str(len(earlier))) + ' comprobaciones fallidas antes de la corrección. Sus conteos y tiempos originales están en <a href="cold-attempt-summary.json">el recibo frío</a>.</p><ul>' + ''.join('<li>'+escape(name)+'</li>' for name in earlier) + '</ul></section>'
    for index, (_, previous) in enumerate(middle, 1):
        failures = [name for name,passed in previous['operational_checks'].items() if not passed]
        note = ('El índice de candidatos todavía no estaba publicado de forma completa y la comparación estricta de caché detectó la diferencia. '
                if previous.get('phase_results', {}).get('local_cache', {}).get('checks', {}).get('candidate_projection_stable') is False else '')
        page += '<section><h2>Reanudación ' + str(index) + (': el fallo intermedio permanece visible' if failures else ': comprobaciones aprobadas') + '</h2><p>' + str(len(failures)) + ' comprobaciones fallidas en este intento. ' + note + 'La ejecución final conserva los recibos previos.</p><ul>' + ''.join('<li>'+escape(name)+'</li>' for name in failures) + '</ul><p><a href="summary.json">Conteos, tiempos y comprobaciones originales del intento intermedio</a>.</p></section>'
    html_people = [row | {'entity_group': {'notary': 'Notario', 'municipal_president': 'Presidencia municipal'}.get(row['entity_group'], row['entity_group']),
                         'assessment_state': state_labels.get(row['assessment_state'], row['assessment_state'])} for row in public_people]
    page += '<section><h2>Volumen por persona</h2><label for="filter">Filtrar por nombre o grupo</label><p><input id="filter" type="search" placeholder="Nombre, notario, municipal…" autocomplete="off"></p><p id="matches" role="status" aria-live="polite"></p><div class="scroll" id="entities">' + table(html_people,[('name','Persona'),('entity_group','Grupo'),('queries_completed','Consultas terminadas'),('original_documents','Originales vinculados como candidatos'),('assessment_state','Estado')]) + '</div></section>'
    page += '<section><h2>Datos y límites auditables</h2><div class="files">' + ''.join('<a href="' + name + '">' + label + '</a>' for name,label in [('entities.csv','300 personas · CSV'),('sources.csv','Fuentes · CSV'),('originals.csv','Originales · CSV'),('queries.csv','Consultas · CSV'),('summary.json','Recibo público · JSON'),('manifest.json','Hashes · JSON')]) + '</div><ul>' + ''.join('<li>' + escape(value) + '</li>' for value in public['limitations']) + '</ul></section>'
    if supplements:
        page += '<section><h2>Auditoría y correcciones observadas</h2><p>Los suplementos distinguen revisión documental, recuperación de referencias conocidas y ampliaciones posteriores de cobertura. Sus mediciones conservan el alcance y el intento de origen.</p><ul>' + ''.join('<li><a href="'+escape(path.name, quote=True)+'">'+escape(path.name)+'</a></li>' for path in supplements) + '</ul></section>'
    page += '''<script>const input=document.getElementById('filter'),rows=[...document.querySelectorAll('#entities tbody tr')],count=document.getElementById('matches');function filter(){let visible=0;const term=input.value.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();for(const row of rows){row.hidden=!row.textContent.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().includes(term);if(!row.hidden)visible++;}count.textContent=visible+' de '+rows.length+' personas';}input.addEventListener('input',filter);filter();</script></main></html>'''
    (output / 'index.html').write_text(page, encoding='utf-8')
    readme = '# Pipeline masivo real · WebScope 2.9.5\n\nAbra [index.html](index.html) desde la carpeta extraída para consultar el informe interactivo. Los CSV y [summary.json](summary.json) contienen mediciones reales y estados documentales, sin cuerpos ni puntuaciones individuales.\n\n'
    readme += '| Resultado final | Conteo |\n|---|---:|\n' + ''.join(f'| {label} | {value} |\n' for label,value in cards)
    readme += '\nEntrada: 300 personas públicas, con 150 notarios y 150 registros de directorios municipales. Los cargos son pistas de entrada según sus fuentes y fechas, sin certificación de vigencia individual. Los 60 notarios usados en correcciones anteriores son desarrollo; los comparadores sin notas no son negativos certificados. Consulte [la procedencia](roster-provenance.json).\n\n'
    if cold:
        readme += f'La adquisición fría tomó {cold["timings_seconds"]["search_and_original_download"]/60:.2f} minutos. Los tiempos de cada reanudación y de la ampliación se muestran separados. La suma medida del banco base y del puente, incluidos caché e informes, es {public["measured_attempt_total_seconds"]/60:.2f} minutos; excluye los intervalos para corregir código y el reprocesamiento retrospectivo posterior, informado aparte. La duración de una lectura local no se presenta como una nueva búsqueda fría.\n\n'
    readme += 'La recuperación de referencias conocidas sigue siendo baja y no se presenta como cobertura equivalente a un buscador general. La precisión del detector requiere leerse junto con recall, abstenciones, estados de identidad y sus denominadores. Ningún cero se interpreta como persona sin incidentes.\n\n'
    readme += 'Los intentos originales, sus fallos, bloqueos y cobertura parcial permanecen en el informe. Las correcciones posteriores al desbloqueo de etiquetas son desarrollo retrospectivo.\n\n'
    if supplements:
        readme += 'Suplementos públicos:\n\n' + ''.join(f'- [{path.name}]({path.name})\n' for path in supplements)
    (output / 'README.md').write_text(readme, encoding='utf-8')
    licenses = '\n\n'.join((ROOT / 'peps/licenses' / name).read_text(encoding='utf-8') for name in ('FONT_SOURCES.txt','LATO_OFL.txt','SOURCE_SERIF_4_OFL.txt'))
    (output / 'font-licenses.txt').write_text(licenses, encoding='utf-8')
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(output.iterdir()) if path.is_file() and path.name != 'manifest.json'}
    (output / 'manifest.json').write_text(json.dumps({'receipt_sha256':public['original_receipt_sha256'],'public_files_sha256':files},indent=2)+'\n',encoding='utf-8')
    return public


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipt', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--supplement', type=Path, action='append', default=[], help='Archivo público previamente revisado; no usar originales ni explicaciones personales.')
    parser.add_argument('--intermediate', type=Path, action='append', default=[], help='Intento intermedio real del mismo banco, conservado entre el frío y el cierre final.')
    parser.add_argument('--derived', type=Path, help='Ampliación real del banco final; presenta sus personas y originales, conservando los intentos base separados.')
    args = parser.parse_args()
    result = render(args.receipt, args.output, args.baseline, args.supplement, args.intermediate, args.derived)
    print(json.dumps({'output':str(args.output),'people':result['summary']['people']},ensure_ascii=False))
