const $ = (id) => document.getElementById(id);
const sampleSchema = {
  questions: {
    departamento: {
      type: 'choice',
      instructions: '¿Qué equipo debe atender este mensaje?',
      criteria: {
        facturacion: 'Cobros, facturas y reembolsos',
        soporte: 'Errores técnicos y problemas de acceso',
        ventas: 'Precios, planes y compras',
        otro: 'Cualquier otro asunto'
      }
    },
    urgente: {type: 'noul', instructions: '¿El mensaje requiere atención urgente?'}
  }
};
const STORE_KEY = 'markup-studio:v1';
const DRAFT_KEY = 'markup-studio:draft:v1';
function upgradeMoodSchema(raw) {
  let schema;
  try { schema = JSON.parse(raw); }
  catch { return raw; }
  const questions = schema?.questions;
  if (!questions || Object.keys(questions).sort().join(',') !== 'emocion,intensidad,ironia,valencia' ||
      questions.valencia?.type !== 'score' || questions.emocion?.type !== 'choice' ||
      questions.intensidad?.type !== 'score' || questions.ironia?.type !== 'noul' ||
      !Array.isArray(questions.valencia.criteria) ||
      questions.valencia.criteria.join('|') !==
        'Muy negativa|Negativa|Levemente negativa|Neutra o indeterminada|Levemente positiva|Positiva|Muy positiva' ||
      !Array.isArray(questions.intensidad.criteria) ||
      questions.intensidad.criteria.join('|') !== 'Ausente|Sutil|Moderada|Fuerte|Extrema' ||
      Object.keys(questions.emocion.criteria || {}).sort().join(',') !==
        'Alegría,Ansiedad,Desprecio,Enojo,Esperanza,Gratitud,Miedo,Mixta,Neutra,Tristeza') return raw;
  return JSON.stringify({questions: {
    emocion: {
      type: 'choice',
      instructions: 'Identifica la emoción predominante en el mensaje. Si aparecen varias, elige la más intensa; si no hay señales suficientes, elige «indeterminada».',
      criteria: {
        alegría: 'Expresa felicidad, entusiasmo o satisfacción.',
        tristeza: 'Expresa pena, desánimo o pérdida.',
        enojo: 'Expresa ira, irritación o frustración.',
        miedo: 'Expresa temor o sensación de amenaza.',
        ansiedad: 'Expresa preocupación, nerviosismo o incertidumbre.',
        sorpresa: 'Expresa asombro ante algo inesperado.',
        calma: 'Expresa tranquilidad o serenidad.',
        indeterminada: 'No hay señales suficientes para identificar una emoción predominante.'
      }
    },
    intensidad: {
      type: 'score',
      instructions: 'Valora la intensidad de la emoción predominante según el lenguaje del mensaje.',
      criteria: ['Nula o no identificable', 'Leve', 'Moderada', 'Intensa', 'Muy intensa']
    }
  }}, null, 2);
}
function readStore() {
  try { const value = JSON.parse(localStorage.getItem(STORE_KEY)); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  catch { return {}; }
}
const store = readStore();
function readDraft() {
  try { const value = JSON.parse(localStorage.getItem(DRAFT_KEY)); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  catch { return {}; }
}
const draft = readDraft();
store.schemas = Array.isArray(store.schemas) ? store.schemas : [];
store.recent = Array.isArray(store.recent) ? store.recent : [];
const schemaDraft = draft.schema ?? store.draftSchema ?? JSON.stringify(sampleSchema, null, 2);
$('schema-input').value = upgradeMoodSchema(schemaDraft);
$('url-input').value = draft.url ?? store.url ?? '';
$('objective-input').value = draft.objective ?? store.objective ?? '';
$('content-input').value = draft.content ?? store.content ?? $('content-input').value;
$('magic-instructions').value = draft.magicInstructions ?? '';
delete store.draftSchema;
delete store.url;
delete store.objective;
delete store.content;
if ($('schema-input').value !== schemaDraft) {
  delete store.lastClassification;
  try { localStorage.setItem(STORE_KEY, JSON.stringify(store)); }
  catch { /* The stale result is hidden in the current tab. */ }
  draft.selectedSchema = '';
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify({...draft, schema: $('schema-input').value})); }
  catch { /* The edited schema is still available in the current tab. */ }
}
let capturedHtml = '';
let cleanedHtml = '';
let toastTimer;

function saveStore() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(store)); }
  catch {
    while (store.recent.length) {
      store.recent.pop();
      try { localStorage.setItem(STORE_KEY, JSON.stringify(store)); return true; }
      catch { /* Try with less history. */ }
    }
    toast('El almacenamiento local está lleno; no se pudo guardar este resultado.', true);
    return false;
  }
  return true;
}

function saveDraft() {
  const fields = {
    url: $('url-input').value,
    objective: $('objective-input').value,
    content: $('content-input').value,
    schema: $('schema-input').value,
    magicInstructions: $('magic-instructions').value,
    selectedSchema: $('saved-schema-select').value
  };
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(fields)); return true; }
  catch {
    store.recent = [];
    delete store.lastClassification;
    saveStore();
    try { localStorage.setItem(DRAFT_KEY, JSON.stringify(fields)); return true; }
    catch { toast('El almacenamiento local está lleno; no se pudieron guardar los campos.', true); return false; }
  }
}
['url-input', 'objective-input', 'content-input', 'schema-input', 'magic-instructions'].forEach((id) => {
  $(id).addEventListener('input', saveDraft);
  $(id).addEventListener('change', saveDraft);
});
window.addEventListener('pagehide', saveDraft);

function renderSavedSchemas() {
  const select = $('saved-schema-select');
  const selected = select.value || draft.selectedSchema;
  select.replaceChildren(new Option('Esquemas guardados…', ''));
  store.schemas.forEach((entry) => select.add(new Option(entry.name, entry.id)));
  select.value = store.schemas.some((entry) => entry.id === selected) ? selected : '';
}

function renderHistory() {
  const panel = $('saved-results');
  panel.classList.toggle('hidden', store.recent.length === 0);
  const list = $('saved-results-list'); list.replaceChildren();
  store.recent.forEach((entry, index) => {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'saved-result-row';
    const title = document.createElement('strong'); title.textContent = entry.result.title || entry.url;
    const detail = document.createElement('small'); detail.textContent = `${entry.url} · ${new Date(entry.savedAt).toLocaleString('es')}`;
    button.append(title, detail);
    button.addEventListener('click', () => {
      $('url-input').value = entry.url; $('objective-input').value = entry.objective || '';
      saveDraft(); renderCapture(entry.result, true);
      toast('Resultado recuperado del almacenamiento local. Vuelve a analizar para descargar el HTML.');
    });
    list.append(button);
  });
}
renderSavedSchemas();
renderHistory();

function toast(message, isError = false) {
  const element = $('toast');
  element.textContent = message;
  element.classList.toggle('error', isError);
  element.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove('show'), 5000);
}

async function post(path, body) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `Error HTTP ${response.status}`);
  return data;
}

function busy(button, state, label) {
  if (state) button.dataset.label = button.innerHTML;
  button.disabled = state;
  button.innerHTML = state ? label : button.dataset.label;
}

document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab === button));
  document.querySelectorAll('.page').forEach((page) => page.classList.toggle('active', page.id === `page-${button.dataset.tab}`));
  $('breadcrumb-current').textContent = ({web: 'ANALIZAR SITIO', classify: 'CLASIFICAR CONTENIDO',
    scrape: 'RECORRER SITIO'})[button.dataset.tab];
}));

async function refreshHealth() {
  try {
    const response = await fetch('/api/health');
    const health = await response.json();
    $('codex-status').textContent = health.codex_cli ? 'Disponible' : 'No encontrado';
    $('jev-status').textContent = health.jev?.configured ? 'Configurado' : 'Sin clave';
    $('status-dot').classList.toggle('off', !health.jev?.configured);
  } catch {
    $('jev-status').textContent = 'Sin conexión';
    $('status-dot').classList.add('off');
  }
}
refreshHealth();

function linkList(targetId, items, emptyText, includeScore = false) {
  const container = $(targetId); container.replaceChildren();
  if (!items.length) { const empty = document.createElement('p'); empty.className = 'no-evidence'; empty.textContent = emptyText; container.append(empty); return; }
  items.slice(0, 14).forEach((item) => {
    const row = document.createElement('a'); row.className = 'evidence-link';
    row.href = item.url; row.target = '_blank'; row.rel = 'noopener noreferrer';
    const label = document.createElement('strong'); label.textContent = item.label || item.url;
    const url = document.createElement('small'); url.textContent = item.url;
    row.append(label, url);
    if (includeScore && typeof item.relevance === 'number') {
      const score = document.createElement('span'); score.className = 'relevance-score';
      score.textContent = `${Math.round(item.relevance * 100)}%`; row.append(score);
    }
    container.append(row);
  });
}

function stepList(targetId, items, emptyText) {
  const container = $(targetId); container.replaceChildren();
  if (!items.length) { const empty = document.createElement('p'); empty.className = 'no-evidence'; empty.textContent = emptyText; container.append(empty); return; }
  items.forEach((item, index) => {
    const row = document.createElement('div'); row.className = 'navigation-step';
    const number = document.createElement('span'); number.textContent = String(index + 1).padStart(2, '0');
    const detail = document.createElement('div');
    const title = document.createElement('strong'); title.textContent = typeof item === 'string' ? item : item.label;
    detail.append(title);
    if (item.url) { const anchor = document.createElement('a'); anchor.href = item.url; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer'; anchor.textContent = item.url; detail.append(anchor); }
    if (item.control) { const hint = document.createElement('small'); hint.textContent = item.control.selector || item.control.label; detail.append(hint); }
    row.append(number, detail); container.append(row);
  });
}

function renderAnalysis(result) {
  const classification = result.classification || {};
  const analysis = result.analysis || {};
  const names = {product: 'Página de producto', listing: 'Listado de productos', other: 'Otra página'};
  $('page-kind').textContent = names[classification.value] || classification.value || 'Sin decisión';
  $('page-kind-source').textContent = `Jev · confianza ${classification.confidence == null ? 'no disponible' : Math.round(classification.confidence * 100) + '%'} · evidencia: ${(classification.evidence_ids || []).join(', ')}`;
  $('page-signals').textContent = `${analysis.groups?.length || 0} grupos clasificados · ${analysis.primary_group ? 'colección principal: ' + analysis.primary_group : 'sin colección principal confirmada'} · ${result.stats.links} enlaces en el DOM`;
  linkList('related-links', analysis.related_products || [], 'Jev no identificó productos relacionados.');
  const review = analysis.reviews || {};
  const reviewNames = {published: 'Reseñas publicadas visibles', empty_widget: 'Widget de reseñas vacío', unknown: 'Estado de reseñas incierto'};
  $('reviews-status').textContent = `${reviewNames[review.state?.value] || 'Estado incierto'} · ${review.state?.evidence_ids?.join(', ') || 'sin evidencia'} · confianza ${review.state?.confidence == null ? 'no disponible' : Math.round(review.state.confidence * 100) + '%'}; recorrido: ${review.mode?.value || 'desconocido'}.`;
  stepList('reviews-steps', (analysis.actions || []).filter(x => x.effect === 'loaded_reviews').map(x => ({label: `Verificado: ${x.before_review_nodes ?? x.before_reviews} → ${x.after_review_nodes ?? x.after_reviews} registros DOM`, control: x.control})), 'No se verificó carga de reseñas.');
  linkList('reviews-links', review.links || [], 'Sin enlaces de reseñas identificados por Jev.');
  $('categories-intro').textContent = result.objective ? `Rutas confirmadas por Jev; prioridad para “${result.objective}” estimada aparte.` : 'Rutas que Jev identificó como categorías o migas de pan.';
  linkList('category-links', analysis.categories || [], 'Jev no identificó categorías.', true);
  $('pagination-intro').textContent = 'Los controles se muestran con el efecto observado en Playwright; una etiqueta no confirma avance.';
  stepList('pagination-steps', (analysis.actions || []).map(x => ({label: `${x.effect}: ${x.before_products ?? '?'} → ${x.after_products ?? '?'} productos; ${x.before_review_nodes ?? x.before_reviews ?? '?'} → ${x.after_review_nodes ?? x.after_reviews ?? '?'} registros DOM`, control: x.control})), 'Sin acción verificada.');
  linkList('product-links', analysis.product_links || [], 'Jev no identificó productos principales.');
}

function updateProgress(job) {
  $('analysis-progress').classList.remove('hidden');
  $('progress-message').textContent = job.message;
  $('progress-percent').textContent = `${job.percent}%`;
  $('progress-fill').style.width = `${job.percent}%`;
  $('progress-phase').textContent = job.phase === 'actions' ? 'Playwright · verificación' : 'Chromium · DOM · Jev';
}

function renderCapture(result, restored = false) {
  capturedHtml = restored ? '' : result.html;
  cleanedHtml = restored ? '' : result.clean_html;
  $('download-html').disabled = restored;
  $('download-clean-html').disabled = restored;
  $('capture-empty').classList.add('hidden');
  $('capture-result').classList.remove('hidden');
  $('result-title').textContent = result.title || 'Página sin título';
  $('result-url').textContent = result.url;
  $('result-url').href = result.url;
  $('stat-elements').textContent = result.stats.elements.toLocaleString('es');
  $('stat-main-blocks').textContent = result.stats.candidate_groups.toLocaleString('es');
  $('stat-other-blocks').textContent = result.stats.decided_groups.toLocaleString('es');
  $('stat-content-chars').textContent = result.stats.content_chars.toLocaleString('es');
  const summary = result.classification?.summary;
  $('coverage-note').textContent = summary ? `Jev dedujo el tipo de página del resumen de ${summary.blocks.length} bloques reconocidos. ${summary.excluded_count ?? summary.discarded_count} bloques quedaron excluidos. Los campos conservan sus valores del DOM; las acciones se verifican por separado.` : 'Jev clasificó grupos estructurales; los valores y enlaces se extrajeron del DOM. Las acciones requieren verificación independiente.';
  $('site-shot').src = result.screenshot ? `data:image/jpeg;base64,${result.screenshot}` : '';
  $('headings-list').replaceChildren(...(result.headings.length ? result.headings : ['Sin encabezados visibles']).map((item) => {
    const li = document.createElement('li'); li.textContent = item; return li;
  }));
  $('text-preview').textContent = result.text_preview || 'No se encontró texto visible.';
  renderAnalysis(result);
  $('analysis-json').textContent = JSON.stringify({classification: result.classification, analysis: result.analysis}, null, 2);
  $('capture-result').scrollIntoView({behavior: 'smooth', block: 'start'});
}

async function runPageAnalysis(endpoint, payload, button) {
  busy(button, true, 'Analizando…');
  try {
    $('analysis-progress').classList.remove('hidden');
    updateProgress({percent: 0, phase: 'queued', message: 'Preparando análisis…'});
    const {job_id: jobId} = await post(endpoint, payload);
    let job;
    do {
      await new Promise((resolve) => setTimeout(resolve, 650));
      const response = await fetch(`/api/jobs/${jobId}`);
      job = await response.json();
      if (!response.ok) throw new Error(job.detail || 'No se pudo consultar el progreso.');
      updateProgress(job);
    } while (job.status === 'running');
    if (job.status === 'error') throw new Error(job.message);
    const result = job.result;
    renderCapture(result);
    refreshHealth();
    const compact = {...result, html: undefined, clean_html: undefined,
      screenshot: result.screenshot?.length < 200000 ? result.screenshot : undefined};
    const objective = $('objective-input').value.trim();
    store.recent = store.recent.filter((entry) => entry.url !== result.url || entry.objective !== objective);
    store.recent.unshift({url: result.url, objective, savedAt: Date.now(), result: compact});
    store.recent = store.recent.slice(0, 5);
    const saved = saveStore(); renderHistory();
    toast(saved ? 'Página analizada. El resultado quedó guardado en este navegador.' : 'Página analizada.', !saved);
  } catch (error) {
    toast(error.message, true);
    $('analysis-progress').classList.add('hidden');
    if (error.message.includes('HTTP 403') || error.message.includes('HTTP 429') ||
        error.message.includes('verificación de seguridad')) $('manual-import').open = true;
  }
  finally { busy(button, false); }
}

$('capture-form').addEventListener('submit', (event) => {
  event.preventDefault();
  saveDraft();
  runPageAnalysis('/api/analyze', {url: $('url-input').value.trim(), objective: $('objective-input').value.trim()},
    event.currentTarget.querySelector('button'));
});

$('import-html-button').addEventListener('click', () => {
  const html = $('html-input').value.trim();
  const url = $('url-input').value.trim();
  if (!url || !html) return toast('Indica la URL y pega el HTML renderizado.', true);
  runPageAnalysis('/api/analyze-html', {url, objective: $('objective-input').value.trim(), html},
    $('import-html-button'));
});

function downloadHtml(content, filename) {
  if (!content) return;
  const href = URL.createObjectURL(new Blob([content], {type: 'text/html;charset=utf-8'}));
  const anchor = document.createElement('a');
  anchor.href = href; anchor.download = filename; anchor.click();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
}
$('download-html').addEventListener('click', () => downloadHtml(capturedHtml, 'pagina-renderizada.html'));
$('download-clean-html').addEventListener('click', () => downloadHtml(cleanedHtml, 'pagina-limpia.html'));

$('content-input').addEventListener('input', () => $('content-count').textContent = $('content-input').value.length.toLocaleString('es'));
$('content-input').dispatchEvent(new Event('input'));
$('format-schema').addEventListener('click', () => {
  try { $('schema-input').value = JSON.stringify(JSON.parse($('schema-input').value), null, 2); saveDraft(); toast('JSON formateado.'); }
  catch { toast('El esquema no es JSON válido.', true); }
});
$('save-schema').addEventListener('click', () => {
  try { JSON.parse($('schema-input').value); }
  catch { return toast('El esquema no es JSON válido.', true); }
  const name = window.prompt('Nombre del esquema', `Esquema ${store.schemas.length + 1}`);
  if (!name?.trim()) return;
  const id = String(Date.now());
  store.schemas.unshift({id, name: name.trim().slice(0, 80), value: $('schema-input').value});
  store.schemas = store.schemas.slice(0, 20);
  const saved = saveStore(); renderSavedSchemas(); $('saved-schema-select').value = id; saveDraft();
  toast(saved ? 'Esquema guardado en este navegador.' : 'No se pudo guardar el esquema.', !saved);
});
$('saved-schema-select').addEventListener('change', () => {
  const entry = store.schemas.find((item) => item.id === $('saved-schema-select').value);
  if (entry) $('schema-input').value = entry.value;
  saveDraft();
});
$('delete-schema').addEventListener('click', () => {
  const id = $('saved-schema-select').value;
  if (!id) return;
  store.schemas = store.schemas.filter((item) => item.id !== id);
  saveStore(); renderSavedSchemas(); saveDraft(); toast('Esquema eliminado.');
});

function addProbabilityRows(card, probabilities) {
  Object.entries(probabilities).sort((a, b) => b[1] - a[1]).forEach(([name, value]) => {
    const row = document.createElement('div'); row.className = 'prob-row';
    const label = document.createElement('span'); label.textContent = name;
    const track = document.createElement('div'); track.className = 'prob-track';
    const fill = document.createElement('div'); fill.className = 'prob-fill'; fill.style.width = `${Math.max(0, Math.min(100, value * 100))}%`; track.append(fill);
    const percent = document.createElement('strong'); percent.textContent = `${Math.round(value * 100)}%`;
    row.append(label, track, percent); card.append(row);
  });
}

function renderClassification(result) {
    $('classify-result').classList.remove('hidden');
    $('jev-model').textContent = result.routing?.model || result.model || 'Jev';
    $('classify-result').querySelector('.overline').textContent = 'RESULTADO / JEV';
    $('classification-json').textContent = JSON.stringify(result, null, 2);
    const container = $('answer-cards'); container.replaceChildren();
    Object.entries(result.answers || {}).forEach(([id, answer]) => {
      const card = document.createElement('div'); card.className = 'answer-card';
      const label = document.createElement('div'); label.className = 'answer-label'; label.textContent = id.replaceAll('_', ' ');
      const value = document.createElement('div'); value.className = 'answer-value';
      value.textContent = answer.type === 'choice' ? answer.choice : answer.type === 'noul' ? `${Math.round(answer.noul * 100)}% sí` : Number(answer.score).toFixed(2);
      const sub = document.createElement('div'); sub.className = 'answer-sub'; sub.textContent = answer.type === 'score' ? 'Nivel estimado' : answer.type === 'noul' ? 'Probabilidad afirmativa' : 'Categoría más probable';
      card.append(label, value, sub);
      if (answer.probabilities) addProbabilityRows(card, answer.probabilities);
      container.append(card);
    });
}

if (store.lastClassification) renderClassification(store.lastClassification);

$('classify-button').addEventListener('click', async () => {
  const button = $('classify-button');
  saveDraft();
  let schema;
  try { schema = JSON.parse($('schema-input').value); }
  catch { return toast('El esquema no es JSON válido.', true); }
  busy(button, true, 'Clasificando…');
  try {
    const result = await post('/api/classify', {content: $('content-input').value.trim(), schema});
    renderClassification(result);
    store.lastClassification = result;
    saveStore();
    $('classify-result').scrollIntoView({behavior: 'smooth', block: 'start'});
    toast('Clasificación completada con Jev.');
  } catch (error) { toast(error.message, true); }
  finally { busy(button, false); }
});

const modal = $('magic-modal');
function closeModal() { modal.classList.add('hidden'); }
$('magic-button').addEventListener('click', () => { modal.classList.remove('hidden'); $('magic-instructions').focus(); });
document.querySelectorAll('[data-close-modal]').forEach((item) => item.addEventListener('click', closeModal));
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeModal(); });
$('generate-button').addEventListener('click', async () => {
  const button = $('generate-button');
  const instructions = $('magic-instructions').value.trim();
  if (instructions.length < 8) return toast('Describe con más detalle el esquema que necesitas.', true);
  busy(button, true, '✦ Codex está creando el esquema…');
  try {
    const schema = await post('/api/generate-schema', {instructions});
    $('schema-input').value = JSON.stringify(schema, null, 2);
    saveDraft();
    closeModal(); toast('Esquema generado. Revísalo antes de clasificar.');
  } catch (error) { toast(error.message, true); }
  finally { busy(button, false); }
});
