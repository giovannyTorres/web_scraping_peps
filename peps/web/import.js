'use strict';
// The import wizard uses only the authenticated, same-origin local API.
window.pepsImport = (() => {
  const $ = id => document.getElementById(id);
  const kinds = {person: 'Persona', company: 'Empresa', organization: 'Organización o institución'};
  const fields = [
    ['ignore', 'Omitir esta columna'], ['name', 'Nombre completo o razón social'],
    ['given_name', 'Nombre(s) de la persona'], ['first_surname', 'Primer apellido'],
    ['second_surname', 'Segundo apellido'], ['kind', 'Tipo de entidad'],
    ['id', 'Identificador propio'], ['aliases', 'Otros nombres o alias'],
    ['role', 'Cargo o actividad'], ['organization', 'Organización relacionada'],
    ['state', 'Estado'], ['municipality', 'Municipio'], ['city', 'Ciudad'],
    ['country', 'País'], ['rfc', 'RFC'], ['age', 'Edad'],
    ['birth_date', 'Fecha de nacimiento'], ['legal_name', 'Razón social adicional'],
    ['trade_names', 'Nombres comerciales'], ['abbreviations', 'Siglas'],
    ['context_note', 'Contexto o notas de identidad']
  ];
  const fieldNames = new Set(fields.map(([value]) => value));
  let hooks, initialized = false;
  const state = {generation: 0, request: 0, reading: false, columns: null, preview: null,
    kindValues: Object.create(null), bytes: null, fileName: '', fileRead: 0,
    singleRevision: 0, singleBusy: false, operation: null, channel: null};
  function node(tag, value, className) {
    const element = document.createElement(tag);
    if (value !== undefined && value !== null) element.textContent = String(value);
    if (className) element.className = className;
    return element;
  }
  function option(value, label) {
    const element = node('option', label); element.value = value; return element;
  }
  function showStatus(channel, text, error = false) {
    const box = $(channel === 'single' ? 'single-status' : 'csv-status');
    if (!box.hidden && box.dataset.statusText === text && box.dataset.statusError === String(error) && box.childElementCount === 1) return box;
    box.dataset.statusText = text; box.dataset.statusError = String(error);
    box.hidden = false; box.className = 'import-status' + (error ? ' import-error' : '');
    box.replaceChildren(node('p', text)); return box;
  }
  function inform(text, error = false) { hooks.message(text, error); }
  async function openEntity(entity) {
    try { await hooks.switchTab('bank'); await hooks.showDetail(entity); }
    catch (error) { inform('La entidad sigue en el banco. No se pudo cargar su expediente: ' + error.message, true); }
  }
  function setStep(step) {
    for (let index = 1; index <= 3; index++) {
      const element = $('csv-step-' + index);
      if (step === index) element.setAttribute('aria-current', 'step');
      else element.removeAttribute('aria-current');
      element.classList.toggle('complete', index < step);
    }
  }
  function open(mode = 'single') {
    hooks.switchTab('import');
    const single = mode !== 'csv';
    for (const [id, active] of [['import-single-tab', single], ['import-csv-tab', !single]]) {
      $(id).setAttribute('aria-selected', String(active)); $(id).tabIndex = active ? 0 : -1;
    }
    $('import-single-panel').hidden = !single; $('import-csv-panel').hidden = single;
    const target = $(single ? 'single-name' : 'csv-file');
    if (!target.disabled) target.focus({preventScroll: true});
  }
  function clearPreview() {
    state.generation++; state.preview = null;
    $('csv-confirm-reviewed').checked = false; $('csv-confirm').disabled = true;
    $('csv-preview-section').hidden = true;
    if (!state.operation) $('csv-status').hidden = true;
  }
  function updateButtons() {
    $('csv-read').disabled = state.reading || state.singleBusy || !!state.operation;
    $('csv-validate').disabled = state.reading || state.singleBusy || !!state.operation || !state.columns || !$('csv-mapping-reviewed').checked;
    $('csv-confirm').disabled = state.singleBusy || !!state.operation || !state.preview?.ready || !state.preview?.preview_hash || !$('csv-confirm-reviewed').checked || Number(state.preview.counts?.accepted || 0) < 1;
    $('single-save').disabled = state.reading || state.singleBusy || !!state.operation;
  }
  function invalidateMapping(clearKinds = false) {
    clearPreview(); $('csv-mapping-reviewed').checked = false;
    if (clearKinds) { state.kindValues = Object.create(null); $('csv-kind-section').hidden = true; $('csv-kind-mapping').replaceChildren(); }
    setStep(state.columns ? 2 : 1); updateButtons();
  }
  function clearCSV() {
    if (state.operation) return;
    state.fileRead++; state.bytes = null; state.fileName = '';
    $('csv-file').value = ''; $('csv-text').value = '';
    $('csv-encoding').value = 'auto'; $('csv-delimiter').value = 'auto';
    $('csv-header').checked = true; $('csv-skip-rows').value = '0';
    $('csv-skip-invalid').checked = false; $('csv-update-existing').checked = false;
    $('csv-aliases-separator').value = '|'; $('csv-date-format').value = 'auto';
    $('csv-file-info').textContent = 'La lista se eliminó del formulario. Las entidades ya guardadas permanecen en el banco.';
    $('csv-mapping').replaceChildren(); $('csv-preview').replaceChildren();
    invalidateSource(); $('csv-file').focus();
  }
  function invalidateSource() {
    clearPreview(); state.columns = null; state.kindValues = Object.create(null);
    $('csv-mapping-section').hidden = true; $('csv-kind-section').hidden = true;
    $('csv-mapping-reviewed').checked = false; $('csv-default-kind').value = '';
    $('csv-read-status').textContent = 'Pulsa «Leer columnas» para revisar el contenido actual.';
    setStep(1); updateButtons();
  }
  function delimiter() { return $('csv-delimiter').value === 'tab' ? '\t' : $('csv-delimiter').value; }
  function csvPayload(withMapping) {
    return {mode: 'csv', text: $('csv-text').value, delimiter: delimiter(),
      has_header: $('csv-header').checked, skip_rows: Number($('csv-skip-rows').value || 0),
      mapping: withMapping ? Array.from($('csv-mapping').querySelectorAll('select[data-column]'), element => element.value) : null,
      default_kind: $('csv-default-kind').value || null, kind_values: {...state.kindValues},
      skip_invalid: $('csv-skip-invalid').checked, existing_policy: $('csv-update-existing').checked ? 'update' : 'skip',
      aliases_separator: $('csv-aliases-separator').value, date_format: $('csv-date-format').value};
  }
  function decode(bytes, selected) {
    const view = new Uint8Array(bytes); let encoding = selected;
    if (encoding === 'auto') {
      if (view[0] === 0xff && view[1] === 0xfe) encoding = 'utf-16le';
      else if (view[0] === 0xfe && view[1] === 0xff) encoding = 'utf-16be';
      else {
        const sample = view.slice(0, 512); let even = 0, odd = 0;
        for (let index = 0; index < sample.length; index++) if (sample[index] === 0) { if (index % 2) odd++; else even++; }
        if (sample.length && odd > sample.length / 5) encoding = 'utf-16le';
        else if (sample.length && even > sample.length / 5) encoding = 'utf-16be';
        else {
          try { new TextDecoder('utf-8', {fatal: true}).decode(bytes); encoding = 'utf-8'; }
          catch { encoding = 'windows-1252'; }
        }
      }
    }
    const text = new TextDecoder(encoding, {fatal: true}).decode(bytes).replace(/^\uFEFF/, '');
    return {text, encoding};
  }
  function decodeFile() {
    if (!state.bytes) return;
    invalidateSource();
    try {
      const result = decode(state.bytes, $('csv-encoding').value);
      $('csv-text').value = result.text;
      $('csv-file-info').textContent = `${state.fileName} · ${(state.bytes.byteLength / 1024).toLocaleString('es-MX', {maximumFractionDigits: 1})} KB · ${result.encoding.toUpperCase()}. Revisa los acentos en las muestras.`;
    } catch {
      $('csv-text').value = '';
      $('csv-read-status').textContent = 'No se pudo leer con esta codificación. Elige otra en «Ajustes de lectura del archivo».';
      inform('La codificación elegida no corresponde al archivo. Cambia el ajuste y vuelve a leer sus columnas.', true);
    }
  }
  async function chooseFile() {
    const sequence = ++state.fileRead;
    const file = $('csv-file').files[0];
    if (!file) return;
    invalidateSource(); state.bytes = null; state.fileName = file.name; $('csv-text').value = '';
    if (file.size > 16000000) { $('csv-file-info').textContent = 'El archivo supera 16 MB. Divide la lista en archivos más pequeños.'; inform('El archivo supera 16 MB. Divide la lista en archivos más pequeños.', true); return; }
    $('csv-file-info').textContent = 'Leyendo archivo en tu equipo…';
    try {
      const bytes = await file.arrayBuffer();
      if (sequence !== state.fileRead) return;
      state.bytes = bytes; decodeFile();
    } catch (error) { if (sequence === state.fileRead) inform('No se pudo abrir el archivo: ' + error.message, true); }
  }
  function rawRowsTable(data) {
    const wrapper = node('div', undefined, 'import-table-scroll');
    const table = node('table'); table.className = 'import-preview-table';
    const head = node('tr'); head.append(node('th', 'Fila de origen'));
    for (const column of data.columns || state.columns || []) head.append(node('th', `${column.index + 1}. ${column.header || 'Sin título'}`));
    const thead = node('thead'); thead.append(head); const tbody = node('tbody');
    for (const row of data.rows || []) {
      const tr = node('tr'); tr.append(node('td', row.line_start || row.row_number));
      for (const column of data.columns || state.columns || []) {
        const value = row.values?.[column.index];
        tr.append(node('td', (value || '—') + (row.values_truncated?.includes(column.index) ? '… [muestra abreviada]' : '')));
      }
      tbody.append(tr);
    }
    table.append(thead, tbody); wrapper.append(table); return wrapper;
  }
  function renderMapping(data) {
    state.columns = data.columns || [];
    const container = $('csv-mapping'); container.replaceChildren();
    for (const column of state.columns) {
      const card = node('div', undefined, 'import-column');
      card.append(node('span', `COLUMNA ${column.index + 1}`, 'eyebrow'), node('h3', column.header || 'Sin título'));
      const examples = node('ul', undefined, 'import-samples');
      for (const example of (column.examples || []).slice(0, 3)) examples.append(node('li', example === '' ? '(celda vacía)' : example));
      if (!examples.childElementCount) examples.append(node('li', 'Sin valores en la muestra'));
      card.append(examples);
      const label = node('label', '¿Qué dato contiene?', 'import-field');
      const select = node('select'); select.dataset.column = String(column.index); select.id = 'csv-column-' + column.index;
      select.setAttribute('aria-label', `Qué dato contiene la columna ${column.index + 1}: ${column.header || 'sin título'}`);
      for (const [value, text] of fields) select.append(option(value, text));
      const suggested = column.suggested_field || data.suggested_mapping?.[column.index] || 'ignore';
      select.value = fieldNames.has(suggested) ? suggested : 'ignore';
      select.addEventListener('change', () => invalidateMapping(true));
      label.append(select); card.append(label);
      if (suggested !== 'ignore' && fieldNames.has(suggested)) card.append(node('small', 'Sugerencia: comprueba que coincide con tus datos.', 'muted'));
      container.append(card);
    }
    const details = node('details', undefined, 'import-raw-details'); details.append(node('summary', 'Ver muestras de las filas originales'), rawRowsTable(data)); container.append(details);
    const info = node('p', `Separador: ${data.delimiter === '\t' ? 'tabulación' : data.delimiter || 'detectado'} · ${data.counts?.total || 0} filas de datos. Se muestran hasta 20 filas de muestra.`, 'muted'); container.append(info);
    $('csv-mapping-section').hidden = false; $('csv-mapping-reviewed').checked = false;
    renderKindValues(data.kind_values, data.kind_suggestions);
    setStep(2); updateButtons();
  }
  function renderKindValues(values, suggestions = {}) {
    const container = $('csv-kind-mapping'); container.replaceChildren();
    $('csv-kind-section').hidden = !values?.length;
    for (const value of values || []) {
      const raw = String(typeof value === 'object' ? value.value ?? value.raw ?? '' : value);
      const label = node('label', `«${raw || '(vacío)'}» significa:`, 'import-field');
      const select = node('select'); select.dataset.kindValue = raw; select.setAttribute('aria-label', `Tipo de entidad para el valor ${raw || 'vacío'}`);
      select.append(option('', 'Identifica este tipo…'));
      for (const [key, text] of Object.entries(kinds)) select.append(option(key, text));
      select.value = state.kindValues[raw] || suggestions[raw] || '';
      select.addEventListener('change', () => {
        if (select.value) state.kindValues[raw] = select.value; else delete state.kindValues[raw];
        invalidateMapping();
      });
      label.append(select); container.append(label);
    }
  }
  function warningList(values, className) {
    const list = node('ul', undefined, className);
    for (const value of values || []) list.append(node('li', typeof value === 'string' ? value : value.message || String(value)));
    return list;
  }
  function renderPreview(data) {
    state.preview = data;
    const container = $('csv-preview'); container.replaceChildren();
    const counts = data.counts || {};
    const summary = node('div', undefined, 'import-summary');
    for (const [label, value, style] of [
      ['Filas de la lista', counts.total, ''], ['Listas para guardar', counts.accepted, ' good'],
      ['Nuevas', counts.new, ''], ['Actualizaciones', counts.update, ''],
      ['Ya existentes / sin cambios', counts.unchanged, ''], [$('csv-skip-invalid').checked && !counts.conflicting_ids ? 'Filas con errores que se omitirán' : 'Con errores', counts.invalid, counts.invalid ? ' warn' : ''],
      ['Duplicadas en la lista', counts.duplicates, ''], ['Omitidas por ID existente', counts.skipped, '']
    ]) { const card = node('div', undefined, 'import-summary-item' + style); card.append(node('strong', Number(value || 0).toLocaleString('es-MX')), node('span', label)); summary.append(card); }
    container.append(summary);
    if (data.ready && Number(counts.accepted || 0) > 0) container.append(node('p', `${counts.accepted} ${counts.accepted === 1 ? 'entidad lista' : 'entidades listas'} para guardar. ${$('csv-update-existing').checked ? 'Se actualizarán las coincidencias identificadas en el resumen.' : 'Las entidades que ya existen se conservarán sin cambios.'}`, 'import-notice import-success'));
    else if (data.ready && !Number(counts.accepted || 0) && !Number(counts.invalid || 0)) container.append(node('p', 'No hay entidades nuevas ni cambios para guardar. Las coincidencias existentes y los duplicados se conservan sin volver a importarse.', 'import-notice'));
    else container.append(node('p', 'Revisa los errores y los tipos de entidad antes de guardar. Si lo decides, puedes omitir explícitamente las filas con errores y validar de nuevo.', 'import-notice import-warning'));
    if ($('csv-skip-invalid').checked && counts.invalid && !counts.conflicting_ids) container.append(node('p', `${counts.invalid === 1 ? 'Se omitirá 1 fila con errores. No se guardará' : `Se omitirán ${counts.invalid} filas con errores. No se guardarán`} en el banco.`, 'import-notice import-warning'));
    if (counts.conflicting_ids) container.append(node('p', 'Hay identificadores en conflicto. Corrige la lista antes de continuar; omitir filas con errores no autoriza sobrescribir otra identidad.', 'import-notice import-warning'));
    if (data.warnings?.length) container.append(warningList(data.warnings, 'import-warnings'));
    if (data.errors?.length) {
      const details = node('details', undefined, 'import-issues'); details.open = true;
      details.append(node('summary', `Errores y datos pendientes (${data.errors.length}${counts.invalid > data.errors.length ? ' mostrados' : ''})`));
      const list = node('ul'); for (const error of data.errors) list.append(node('li', `${error.line_start || error.row_number ? `Fila ${error.line_start || error.row_number}: ` : ''}${error.message || error}`));
      details.append(list); container.append(details);
    }
    const wrapper = node('div', undefined, 'import-table-scroll'); const table = node('table'); table.className = 'import-preview-table';
    const thead = node('thead'); const header = node('tr'); for (const label of ['Fila', 'Nombre que se guardará', 'Tipo', 'Acción', 'Observaciones']) header.append(node('th', label)); thead.append(header);
    const tbody = node('tbody');
    const statuses = {unmapped: 'Identificar columnas', conflict: 'Conflicto de ID', new: 'Crear', update: 'Actualizar', unchanged: 'Conservar existente', existing: 'Conservar existente', skipped: 'Omitir', invalid: 'Corregir', duplicate: 'Duplicada', error: 'Corregir', valid: 'Guardar'};
    for (const row of data.rows || []) {
      const tr = node('tr'); const entity = row.entity || {};
      tr.append(node('td', row.line_start || row.row_number), node('td', entity.name || '—'), node('td', kinds[entity.kind] || 'Sin identificar'), node('td', row.status === 'invalid' && $('csv-skip-invalid').checked ? 'Omitir por error' : statuses[row.status] || row.status || 'Revisar'));
      const notes = [...(row.errors || []), ...(row.warnings || [])].map(value => typeof value === 'string' ? value : value.message || String(value));
      const cell = node('td', notes.join(' · ') || '—'); if (row.errors?.length) cell.className = 'import-error-text'; tr.append(cell); tbody.append(tr);
    }
    table.append(thead, tbody); wrapper.append(table); container.append(wrapper, node('p', `Vista previa: hasta 20 filas. El resumen y la validación incluyen las ${counts.total || 0} filas de la lista. Las muestras de celdas largas se abrevian; la validación usa el contenido completo. Cambiar cualquier dato invalida esta confirmación.`, 'muted'));
    $('csv-preview-section').hidden = false;
    $('csv-confirmation').hidden = !data.ready || Number(counts.accepted || 0) < 1;
    $('csv-confirm-reviewed').checked = false;
    if (data.kind_values?.length) renderKindValues(data.kind_values, data.kind_suggestions);
    setStep(3); updateButtons();
  }
  async function requestCSV(validate) {
    if (state.reading || state.singleBusy || state.operation) return;
    if (!$('csv-text').value.trim()) { $('csv-paste-details').open = true; $('csv-text').focus(); inform('Selecciona un archivo o pega una tabla antes de leer sus columnas.', true); return; }
    if (validate && !$('csv-mapping-reviewed').checked) { inform('Revisa qué contiene cada columna y marca la casilla de revisión.', true); return; }
    const skipRows = $('csv-skip-rows'); if (!skipRows.checkValidity()) { skipRows.reportValidity(); return; }
    const payload = csvPayload(validate); const generation = state.generation; const request = ++state.request;
    state.reading = true; updateButtons();
    $('csv-read-status').textContent = validate ? 'Validando todas las filas en tu equipo…' : 'Leyendo columnas y muestras…';
    try {
      const data = await hooks.api('import-preview', payload);
      if (request !== state.request || generation !== state.generation) return;
      if (validate) {
        renderPreview(data);
        if (data.kind_values?.length && !data.ready) { $('csv-mapping-reviewed').checked = false; updateButtons(); }
        $('csv-preview-section').scrollIntoView({block: 'start', behavior: 'smooth'});
      } else { renderMapping(data); $('csv-mapping-section').scrollIntoView({block: 'start', behavior: 'smooth'}); }
      $('csv-read-status').textContent = validate ? 'Validación terminada. Comprueba el resumen.' : 'Columnas leídas. Ahora identifica los datos.';
    } catch (error) { if (request === state.request && generation === state.generation) { $('csv-read-status').textContent = 'No se pudo completar la lectura.'; inform(error.message, true); } }
    finally { if (request === state.request) { state.reading = false; updateButtons(); } }
  }
  function lockOperation(id, channel) {
    state.operation = id; state.channel = channel;
    for (const fieldset of ['single-fields', 'csv-input-fields', 'csv-mapping-fields']) $(fieldset).disabled = true;
    $('csv-confirm-reviewed').disabled = true; updateButtons();
  }
  function unlockOperation() {
    state.operation = null; state.channel = null; state.singleBusy = false;
    for (const fieldset of ['single-fields', 'csv-input-fields', 'csv-mapping-fields']) $(fieldset).disabled = false;
    $('csv-confirm-reviewed').disabled = false; updateButtons();
  }
  async function finishImport(data, channel) {
    const result = data.result || data; const counts = result.counts || {};
    state.preview = null; $('csv-confirm-reviewed').checked = false; $('csv-confirmation').hidden = true;
    unlockOperation();
    const imported = Number(result.imported ?? (Number(counts.new || 0) + Number(counts.update || 0)));
    const box = showStatus(channel, imported ? `${imported === 1 ? 'Se guardó 1 entidad' : `Se guardaron ${imported.toLocaleString('es-MX')} entidades`} en tu banco.` : 'La revisión terminó. Las entidades existentes se conservaron sin cambios.');
    box.classList.add('import-success');
    if (counts.skipped || counts.duplicates || counts.unchanged || counts.invalid) box.append(node('p', `Conservadas sin cambios: ${counts.unchanged || 0} · Duplicadas: ${counts.duplicates || 0} · Filas con errores: ${counts.invalid || 0}${counts.invalid_skipped ? ' (omitidas)' : ''} · Omitidas por ID existente: ${counts.skipped || 0}.`, 'muted'));
    box.append(node('p', result.scoring_status === 'pending' ? `Las entidades están guardadas. El score sigue pendiente${result.scoring_error ? ': ' + result.scoring_error : '; puedes actualizarlo desde el banco'}.` : 'El expediente permite consultar el score, su evidencia y «¿Por qué obtuvo este score?».', 'muted'));
    const actions = node('div', undefined, 'actions');
    const bank = node('button', 'Ir al banco'); bank.type = 'button'; bank.onclick = async () => { try { await (hooks.openBank ? hooks.openBank() : hooks.switchTab('bank')); } catch (error) { inform(error.message, true); } }; actions.append(bank);
    const samples = result.sample_entities || [];
    const first = samples[0];
    if (first) {
      const consult = node('button', channel === 'single' ? 'Consultar expediente' : 'Consultar primera entidad', 'primary'); consult.type = 'button';
      consult.onclick = () => openEntity(first); actions.append(consult);
    }
    box.append(actions);
    if (samples.length > 1) {
      const details = node('details'); details.append(node('summary', 'Consultar entidades de esta importación'));
      const links = node('div', undefined, 'import-entity-links');
      for (const entity of samples) {
        const button = node('button', entity.name, 'text-button'); button.type = 'button';
        button.onclick = () => openEntity(entity); links.append(button);
      }
      details.append(links); box.append(details);
    }
    box.append(node('p', `Comprobante local · Operación ${data.id || data.operation_id || 'completada'}`, 'muted'));
    try { await hooks.refresh(); } catch { inform('La importación terminó. Actualiza el banco para cargar sus resultados.', true); }
    inform(imported ? `Importación terminada: ${imported === 1 ? '1 entidad guardada' : `${imported} entidades guardadas`}.` : 'Importación terminada: se conservaron las coincidencias existentes.');
    if (channel === 'single' && first) {
      $('single-form').reset(); state.singleRevision++;
      await openEntity(first);
    }
  }
  async function watchOperation(id, channel) {
    let errors = 0;
    while (state.operation === id) {
      try {
        const data = await hooks.api('import-status?id=' + encodeURIComponent(id));
        if (state.operation !== id) return;
        errors = 0;
        if (data.status === 'completed') { await finishImport(data, channel); return; }
        if (data.status === 'error') {
          unlockOperation(); state.preview = null; $('csv-confirm').disabled = true; $('csv-confirmation').hidden = true;
          const box = showStatus(channel, data.error || 'No se pudo completar la importación. Valida la lista de nuevo antes de reintentar.', true);
          if (data.retry_preview) box.append(node('p', 'El banco o la lista cambiaron. No se guardó una importación con una vista previa vencida; vuelve a validar los datos.', 'muted'));
          inform(data.error || 'La importación no se completó.', true); return;
        }
        showStatus(channel, data.phase === 'scoring' ? 'Nombres guardados. Preparando el score y sus explicaciones locales…' : data.status === 'queued' ? 'Importación en espera. Puedes seguir consultando el banco.' : 'Guardando las entidades y su trazabilidad…');
      } catch (error) {
        if (error.status === 404) {
          unlockOperation(); state.preview = null;
          $('csv-confirm-reviewed').checked = false;
          $('csv-confirmation').hidden = true; $('csv-confirm').disabled = true;
          const text = 'El comprobante ya no está disponible. Revisa el banco antes de volver a importar: los datos podrían haberse guardado.';
          const box = showStatus(channel, text, true);
          const bank = node('button', 'Ir al banco'); bank.type = 'button';
          bank.onclick = async () => { try { await (hooks.openBank ? hooks.openBank() : hooks.switchTab('bank')); } catch (failure) { inform(failure.message, true); } };
          box.append(bank); inform(text, true); return;
        }
        errors++;
        if (errors >= 3) {
          const box = showStatus(channel, 'Se perdió la conexión con la aplicación local. La operación puede seguir en curso; consulta su estado antes de volver a importar.', true);
          const retry = node('button', 'Consultar estado de la importación'); retry.type = 'button';
          retry.onclick = () => { retry.disabled = true; watchOperation(id, channel); }; box.append(retry); return;
        }
      }
      await new Promise(resolve => setTimeout(resolve, 700));
    }
  }
  async function commit(payload, channel) {
    if (state.operation) return;
    // Freeze before sending: a second click can never submit a second job.
    lockOperation('submitting', channel); showStatus(channel, 'Confirmando la importación en tu equipo…');
    try {
      const result = await hooks.api('import-confirm', payload);
      if (!result.operation_id) throw Error('La aplicación no devolvió un identificador de operación. Revisa el banco antes de repetir la importación.');
      lockOperation(result.operation_id, channel); await watchOperation(result.operation_id, channel);
    } catch (error) {
      unlockOperation(); state.preview = null; $('csv-confirm').disabled = true;
      $('csv-confirmation').hidden = true;
      showStatus(channel, error.status === 409 ? 'La lista o el banco cambiaron. Valida otra vez antes de guardar.' : error.message, true); inform(error.message, true);
    }
  }
  async function submitSingle(event) {
    event.preventDefault(); if (state.singleBusy || state.reading || state.operation || !$('single-form').reportValidity()) return;
    state.singleBusy = true; updateButtons();
    const revision = state.singleRevision;
    const context = {};
    for (const [key, id] of [['role', 'single-role'], ['organization', 'single-organization'], ['state', 'single-state'], ['city', 'single-city'], ['rfc', 'single-rfc'], ['birth_date', 'single-birth-date'], ['context_note', 'single-context']]) {
      if ($(id).value.trim()) context[key] = $(id).value.trim();
    }
    const payload = {mode: 'single', entity: {name: $('single-name').value.trim(), kind: $('single-kind').value,
      aliases: $('single-aliases').value.split('|').map(value => value.trim()).filter(Boolean), context}, existing_policy: 'skip'};
    showStatus('single', 'Revisando el nombre y los datos de identidad…');
    try {
      const data = await hooks.api('import-preview', payload);
      if (revision !== state.singleRevision) { showStatus('single', 'El formulario cambió mientras se revisaba. Comprueba tus datos y pulsa «Guardar y consultar» de nuevo.'); return; }
      if (!data.ready) {
        const box = showStatus('single', 'Revisa los datos antes de guardar.', true);
        box.append(warningList(data.errors?.map(error => error.message || error) || data.warnings || ['El nombre o su tipo requieren revisión.'])); return;
      }
      if (!Number(data.counts?.accepted || 0)) {
        const box = showStatus('single', 'Esta entidad ya existe. Sus datos se conservaron sin cambios.');
        const existing = data.rows?.find(row => row.entity)?.entity;
        if (existing) { const button = node('button', 'Consultar entidad existente', 'primary'); button.type = 'button'; button.onclick = () => openEntity(existing); box.append(button); }
        return;
      }
      await commit({...payload, preview_hash: data.preview_hash}, 'single');
    } catch (error) { showStatus('single', error.message, true); inform(error.message, true); }
    finally { state.singleBusy = false; updateButtons(); }
  }
  function init(options) {
    if (initialized) return; initialized = true; hooks = options;
    for (const button of document.querySelectorAll('[data-import-open]')) button.addEventListener('click', () => open(button.dataset.importOpen));
    $('import-single-tab').onclick = () => open('single'); $('import-csv-tab').onclick = () => open('csv');
    for (const id of ['import-single-tab', 'import-csv-tab']) $(id).onkeydown = event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault(); const next = event.key === 'Home' ? 'single' : event.key === 'End' ? 'csv' : id === 'import-single-tab' ? 'csv' : 'single'; open(next); $('import-' + next + '-tab').focus();
      }
    };
    $('single-form').onsubmit = submitSingle; $('single-form').oninput = () => { state.singleRevision++; $('single-status').hidden = true; };
    $('single-kind').onchange = () => { state.singleRevision++; $('single-status').hidden = true; };
    $('csv-file').onchange = chooseFile; $('csv-clear').onclick = clearCSV;
    $('csv-text').oninput = () => { state.fileRead++; state.bytes = null; $('csv-file-info').textContent = 'Se usará el contenido pegado o editado debajo.'; invalidateSource(); };
    $('csv-encoding').onchange = () => { if (state.bytes) decodeFile(); else invalidateSource(); };
    for (const id of ['csv-delimiter', 'csv-header', 'csv-skip-rows']) $(id).addEventListener(id === 'csv-skip-rows' ? 'input' : 'change', invalidateSource);
    for (const id of ['csv-default-kind', 'csv-aliases-separator', 'csv-date-format', 'csv-skip-invalid', 'csv-update-existing']) $(id).addEventListener('change', () => invalidateMapping());
    $('csv-mapping-reviewed').onchange = () => { clearPreview(); updateButtons(); };
    $('csv-confirm-reviewed').onchange = updateButtons;
    $('csv-read').onclick = () => requestCSV(false); $('csv-validate').onclick = () => requestCSV(true);
    $('csv-confirm').onclick = () => {
      if (state.operation || !state.preview?.ready || !state.preview.preview_hash || !$('csv-confirm-reviewed').checked) return;
      commit({...csvPayload(true), preview_hash: state.preview.preview_hash}, 'csv');
    };
    setStep(1); updateButtons();
  }
  return {init, open};
})();
