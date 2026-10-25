/* Browser for durable scraping history and discovery decisions. */
(() => {
  const el = (id) => document.getElementById(id);
  const KEY = 'markup-studio:scrape:v1';
  const linkedRun = new URLSearchParams(location.search).get('run');
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY)) || {}; } catch { /* Ignore old drafts. */ }
  el('scrape-url').value = saved.url || '';
  el('scrape-limit').value = saved.maxSites || 10;
  el('scrape-hops').value = saved.maxHops ?? 2;
  el('scrape-strategy').value = saved.strategy || 'jev';
  let currentRun = null;
  let currentIndex = -1;
  let requestSerial = 0;
  let visitSerial = 0;
  let commercialKey = null;

  function remember(changes = {}) {
    saved = {...saved, url: el('scrape-url').value, maxSites: Number(el('scrape-limit').value),
      maxHops: Number(el('scrape-hops').value), strategy: el('scrape-strategy').value, ...changes};
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch { /* History is on the server. */ }
  }
  ['scrape-url', 'scrape-limit', 'scrape-hops', 'scrape-strategy'].forEach((id) => el(id).addEventListener('change', () => remember()));

  async function json(path, options) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || `Error HTTP ${response.status}`);
    return data;
  }
  function node(tag, className, content) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (content != null) item.textContent = content;
    return item;
  }
  function safeLink(url, label) {
    const link = node('a', '', label || url);
    if (/^https?:\/\//i.test(url || '')) {
      link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    }
    return link;
  }
  function confidence(value) {
    return typeof value === 'number' ? `${Math.round(value * 100)}%` : 'sin confianza';
  }
  function cost(usage, costs) {
    if (costs) return `${costs.exact ? '' : 'Al menos '}US$${Number(costs.amount_usd).toFixed(6)}`;
    if (!usage) return 'Sin registro';
    return `${usage.exact ? '' : 'Al menos '}US$${(Number(usage.input_tokens || 0) * 0.042 / 1e6).toFixed(6)}`;
  }
  const names = {listing: 'Listado de productos', product: 'Producto',
    commercial_landing: 'Página comercial', other: 'Sin clasificación suficiente',
    primary_products: 'Productos principales', secondary_products: 'Productos secundarios',
    products: 'Productos', attributes: 'Características', offer: 'Oferta y precio',
    reviews: 'Reseñas', categories: 'Categorías', breadcrumb: 'Ruta de navegación',
    collection_control: 'Controles de la colección', site_menu: 'Menú del sitio',
    header: 'Cabecera', footer: 'Pie de página', promotion: 'Promoción',
    filters: 'Filtros', qa: 'Preguntas y respuestas', discussion: 'Debate', editorial: 'Contenido editorial',
    search: 'Buscador', brand: 'Marca', service: 'Servicios', metadata: 'Metadatos', semantic_collection: 'Colección declarada', semantic_content: 'Contenido principal', semantic_article: 'Artículo HTML', semantic_table: 'Tabla HTML'};
  function name(value) { return names[value] || value || 'Sin clasificar'; }
  function tokenUsage(usage, costs) {
    if (costs) {
      const tokens = usage?.calls ? ` · Modelos: ${usage.calls} llamadas · ${Number(usage.input_tokens || 0).toLocaleString('es')} tokens de entrada · ${Number(usage.output_tokens || 0).toLocaleString('es')} de salida${usage.exact ? '' : ' · Tokens parciales'}` : '';
      const providers = [...new Set(costs.breakdown.map(item => item.provider).filter(Boolean))].join(', ');
      return `${cost(usage, costs)} · ${costs.reports} reportes de gastos${providers ? ` · ${providers}` : ''}${tokens}${costs.exact ? '' : ' · Gasto parcial'}${costs.unreported_expenses ? ` · ${costs.unreported_expenses} importes sin informar` : ''}${costs.recording_errors ? ' · Falló el registro' : ''}`;
    }
    if (!usage) return 'Uso de Jev: sin registro en este recorrido anterior';
    const input = Number(usage.input_tokens || 0).toLocaleString('es');
    const output = Number(usage.output_tokens || 0).toLocaleString('es');
    const qualifier = usage.exact ? '' : ' al menos';
    return `${cost(usage)} · Jev: ${usage.calls} llamadas registradas ·${qualifier} ${input} tokens de entrada · ${output} de salida${usage.unreported_calls ? ` · ${usage.unreported_calls} llamadas sin cifra` : ''}${usage.recording_errors ? ' · Medición incompleta: falló el registro' : ''}`;
  }
  function statusText(status) {
    return ({done: 'Analizada', error: 'Falló', running: 'En curso'})[status] || status;
  }
  function edgeText(disposition) {
    return ({scheduled: 'Visitada', queued: 'En cola', max_hops: 'Límite de saltos',
      max_sites: 'Límite de visitas', duplicate: 'Duplicada', doubtful_page: 'Página dudosa',
      doubtful_entity: 'Entidad dudosa', invalid_uri: 'URL inválida',
      outside_seed_hosts: 'Fuera de los hosts iniciales'})[disposition] || disposition;
  }

  async function refreshRuns() {
    try {
      const runs = await json('/api/scrapes');
      const target = el('scrape-runs'); target.replaceChildren();
      if (!runs.length) target.append(node('p', 'no-evidence', 'Todavía no hay recorridos guardados.'));
      runs.forEach((run) => {
        const button = node('button', 'saved-result-row'); button.type = 'button'; button.dataset.runId = run.id;
        const root = run.config?.initial_uris?.[0] || run.id;
        const summary = run.summary;
        button.append(node('strong', '', root),
          node('small', '', `${new Date(run.started_at).toLocaleString('es')} · ${summary ? `${summary.scheduled} visitas · ${summary.succeeded} análisis completados · ${summary.failed} fallidas` : 'Recorrido en curso o interrumpido'} · límite ${run.config?.max_sites ?? '?'} · ${cost(run.usage, run.costs)}`));
        button.addEventListener('click', () => openRun(run.id).then(() => el('scrape-run-stats').scrollIntoView({block: 'start'})).catch((error) => { el('scrape-runs').prepend(node('p', 'no-evidence', error.message)); }));
        target.append(button);
      });
      return runs;
    } catch (error) { el('scrape-runs').replaceChildren(node('p', 'no-evidence', error.message)); }
  }

  async function openRun(runId, preferredVisitId) {
    const serial = ++requestSerial;
    const data = await json(`/api/scrapes/${encodeURIComponent(runId)}`);
    if (serial !== requestSerial) return;
    const selectedVisitId = currentRun?.run.id === runId ? currentRun.visits[currentIndex]?.id : undefined;
    currentRun = data;
    remember({runId});
    const url = new URL(location.href); url.searchParams.set('run', runId); history.replaceState(null, '', url);
    document.querySelectorAll('#scrape-runs [data-run-id]').forEach((button) => button.classList.toggle('selected', button.dataset.runId === runId));
    const visits = data.visits;
    const run = data.run;
    const strategy = run.config?.strategy;
    const strategyLabel = strategy?.endsWith('.SemanticMetadataStrategy') ? 'Semántica' :
      strategy?.endsWith('.JevStrategy') ? 'Jev' : strategy ? strategy.split('.').pop() : 'Sin registrar';
    el('scrape-explorer').classList.remove('hidden');
    el('scrape-run-title').textContent = run.config?.initial_uris?.[0] || 'Recorrido';
    el('scrape-run-meta').textContent = `${visits.length} visitas (páginas y expansiones) · estrategia ${strategyLabel} · límite ${run.config?.max_sites ?? '?'} · ${run.finished_at ? 'Finalizado' : 'En curso o interrumpido'} · ${run.config?.traversal === "balanced" ? "recorrido mixto: amplitud y profundidad" : "cola por niveles"}`;
    el('scrape-run-usage').textContent = tokenUsage(run.usage, run.costs);
    renderRunSummary(data);
    renderTimeline();
    if (!visits.length) {
      ++visitSerial;
      currentIndex = -1;
      commercialKey = null;
      el('scrape-detail').classList.add('hidden');
      return;
    }
    el('scrape-detail').classList.remove('hidden');
    const index = Math.max(0, visits.findIndex((visit) => visit.id === (selectedVisitId || preferredVisitId)));
    await showVisit(index);
  }

  function renderRunSummary(data) {
    const {run, visits, edges} = data;
    const complete = visits.filter((visit) => visit.status === 'done').length;
    const failed = visits.filter((visit) => visit.status === 'error').length;
    const expansions = visits.filter((visit) => visit.kind === 'expansion').length;
    const stats = el('scrape-run-stats'); stats.replaceChildren();
    const metrics = [
      [String(visits.length), 'Visitas iniciadas', `${visits.length - expansions} páginas + ${expansions} expansiones`],
      [String(complete), 'Análisis completados', 'Incluye los estados posteriores a expandir'],
      [String(failed), 'Visitas fallidas', 'Selecciona «Fallidas» para revisar el motivo'],
      [cost(run.usage, run.costs), 'Gasto de estrategias', run.costs ? (run.finished_at && complete ? `${run.costs.exact ? '' : 'Al menos '}US$${(Number(run.costs.amount_usd) / complete).toFixed(6)} por análisis completado` : 'Importes reportados por la estrategia') : 'Jev: US$0,042 / millón de entrada · salida gratis']
    ];
    metrics.forEach(([value, label, description]) => {
      const card = node('article', 'scrape-stat');
      card.append(node('span', '', label), node('strong', '', value), node('small', '', description));
      stats.append(card);
    });
    const pending = visits.filter((visit) => visit.status === 'running').length;
    const blocked = edges.filter((edge) => edge.disposition === 'max_sites').length;
    const state = run.finished_at ? (blocked ? 'Finalizó al alcanzar el límite de visitas.' : 'Recorrido finalizado.') :
      `${pending} visitas en proceso. Si el recorrido fue interrumpido, este historial conserva lo ya obtenido.`;
    el('scrape-run-outcome').textContent = `${state} ${complete} análisis completados no garantizan que todas las extracciones sean correctas: revisa su evidencia. ${(run.costs ? !run.costs.exact : run.usage && !run.usage.exact) ? 'El gasto es parcial: faltan reportes o hay importes sin informar.' : ''}`;
  }

  function renderTimeline() {
    const visits = currentRun.visits;
    const filter = el('scrape-visit-filter').value;
    const timeline = el('scrape-timeline'); timeline.replaceChildren();
    visits.forEach((visit, index) => {
      if (filter === 'error' && visit.status !== 'error' || filter === 'expansion' && visit.kind !== 'expansion' || filter === 'doubtful' && !visit.doubtful) return;
      const item = node('button', 'scrape-visit'); item.type = 'button';
      item.dataset.index = index;
      item.classList.toggle('selected', index === currentIndex);
      item.style.marginLeft = `${Math.min(visit.depth, 6) * 8}px`;
      item.append(node('span', 'scrape-visit-number', String(index + 1).padStart(2, '0')),
        node('span', 'scrape-visit-title', visit.title || visit.requested_uri),
        node('small', '', `${visit.kind === 'expansion' ? 'Expansión' : 'Página'} · ${name(visit.page_type)} · Salto ${visit.depth} · ${statusText(visit.status)}${visit.doubtful ? ' · Dudosa' : ''}`));
      item.addEventListener('click', () => showVisit(index));
      timeline.append(item);
    });
    if (!timeline.children.length) timeline.append(node('p', 'no-evidence', 'No hay visitas de este tipo.'));
  }

  async function showVisit(index) {
    if (!currentRun || index < 0 || index >= currentRun.visits.length) return;
    const serial = ++visitSerial;
    const previousVisitId = currentRun.visits[currentIndex]?.id;
    currentIndex = index;
    const visit = currentRun.visits[index];
    document.querySelectorAll('.scrape-visit').forEach((item) => item.classList.toggle('selected', Number(item.dataset.index) === index));
    el('scrape-prev').disabled = index === 0;
    el('scrape-next').disabled = index === currentRun.visits.length - 1;
    const runId = currentRun.run.id;
    const base = `/api/scrapes/${encodeURIComponent(runId)}/visits/${encodeURIComponent(visit.id)}`;
    if (previousVisitId !== visit.id || !commercialKey?.startsWith(`${base}\n`)) {
      commercialKey = null;
      el('scrape-commercial').replaceChildren(node('p', 'no-evidence', 'Preparando los datos extraídos…'));
    }
    const detail = await json(base);
    if (serial !== visitSerial || currentRun?.run.id !== runId || currentRun.visits[currentIndex]?.id !== visit.id) return;
    el('scrape-page-position').textContent = `VISITA ${index + 1} / ${currentRun.visits.length} · DESCUBRIMIENTO ${visit.discovery_order}`;
    el('scrape-page-title').textContent = detail.title || detail.requested_uri;
    const url = el('scrape-page-url'); url.textContent = detail.final_uri || detail.requested_uri;
    url.href = detail.final_uri || detail.requested_uri;
    el('scrape-page-meta').textContent = `${detail.kind === 'expansion' ? 'Expansión · ' : ''}Salto ${detail.depth} · ${statusText(detail.status)} · ${name(detail.page_type)} · confianza ${confidence(detail.page_confidence)}${detail.doubtful ? ' · Página dudosa: no se siguieron sus enlaces' : ''}${detail.error ? ` · ${detail.error}` : ''}\n${tokenUsage(detail.usage, detail.costs)}`;
    const shot = el('scrape-shot');
    shot.classList.toggle('hidden', !detail.screenshot_path);
    el('scrape-no-shot').classList.toggle('hidden', !!detail.screenshot_path);
    shot.removeAttribute('src');
    if (detail.screenshot_path) shot.src = `${base}/screenshot`;
    el('scrape-shot-open').href = `${base}/screenshot`;
    const parent = currentRun.visits.findIndex((item) => item.id === detail.parent_visit_id);
    const summary = el('scrape-page-summary');
    summary.textContent = `${detail.kind === 'expansion' ? 'Estado después de expandir' : 'Página visitada'}${parent >= 0 ? ` · Descubierta desde la visita ${parent + 1}` : ' · URL inicial'}. ${detail.status === 'error' ? `Falló: ${detail.error}` : `Tipo de página: ${name(detail.page_type).toLowerCase()}.`}`;
    if (parent >= 0) {
      const button = node('button', 'secondary-button', `Volver a visita ${parent + 1}`);
      button.type = 'button'; button.addEventListener('click', () => showVisit(parent)); summary.append(button);
    }
    const download = el('scrape-html-download'); download.href = `${base}/html`;
    download.classList.toggle('hidden', !detail.html_path);
    renderEntities(detail);
    renderEdges(detail);
    await renderCommercial(detail, base, serial);
  }

  async function renderCommercial(detail, base, serial) {
    const key = `${base}\n${JSON.stringify([detail.result, detail.html_path, detail.status, detail.error])}`;
    if (commercialKey === key) return;
    const target = el('scrape-commercial');
    target.replaceChildren(node('p', 'no-evidence', 'Preparando los datos extraídos…'));
    let dom = null;
    if (detail.html_path) {
      try {
        const response = await fetch(`${base}/html?clean=true`);
        if (response.ok) dom = new DOMParser().parseFromString(await response.text(), 'text/html');
      } catch { /* Show fields separately when source membership cannot be checked. */ }
    }
    if (serial !== visitSerial || currentRun?.visits[currentIndex]?.id !== detail.id) return;
    target.replaceChildren();
    if (dom) renderEntities(detail, dom);
    if (!detail.result) {
      target.append(node('p', 'no-evidence', detail.status === 'error' ? 'No se extrajeron datos: la visita falló.' : 'El análisis todavía no tiene resultados.'));
      commercialKey = key;
      return;
    }
    window.ScrapeCommercial.render(target, detail, dom);
    commercialKey = key;
  }

  function renderEntities(detail, dom = null) {
    const target = el('scrape-entities'); target.replaceChildren();
    const result = detail.result;
    if (!result) {
      target.append(node('p', 'no-evidence', detail.status === 'running' ? 'La página se está analizando.' : 'No hay extracciones; consulta el error y los intentos en el resultado completo.'));
      el('scrape-result-json').textContent = JSON.stringify({attempts: detail.attempts,
        model_calls: detail.model_calls, usage: detail.usage, expenses: detail.expenses, costs: detail.costs, error: detail.error}, null, 2);
      return;
    }
    const entities = result.entities || [];
    const synthesis = result.classification?.summary;
    if (synthesis) {
      target.append(node('p', 'coverage-note', `Síntesis de la extracción: ${name(result.page_type)} · ${Object.entries(synthesis.counts || {}).map(([kind, count]) => `${count} ${name(kind)}`).join(' · ')} · ${synthesis.excluded_count ?? synthesis.discarded_count ?? 0} bloques excluidos del resumen`));
    }
    const recognized = entities.filter((item) => item.role !== 'other');
    const represented = new Set();
    if (dom) {
      for (const entity of recognized) {
        let root; try { root = dom.querySelector(entity.selector); } catch { continue; }
        if (!root) continue;
        for (const child of recognized) {
          if (child === entity || child.role !== entity.role) continue;
          let nested; try { nested = dom.querySelector(child.selector); } catch { continue; }
          if (nested && nested !== root && root.contains(nested) &&
              (entity.text === child.text || !entity.parts?.length && child.parts?.length)) { represented.add(entity.id);break; }
        }
      }
    }
    const identified = recognized.filter((entity) => !represented.has(entity.id));
    const contentGroups = new Map();
    const unexecuted = node('details', 'scrape-supporting');
    unexecuted.append(node('summary', '', 'Controles propuestos que no se ejecutaron'));
    if (result.reviews?.state) {
      const labels = {published: 'Reseñas publicadas', empty_widget: 'Widget sin reseñas publicadas', unknown: 'Reseñas: evidencia insuficiente'};
      target.append(node('p', 'coverage-note', `${labels[result.reviews.state.value] || result.reviews.state.value} · ${confidence(result.reviews.state.confidence)}${result.reviews.mode?.value === 'load_more' ? ' · Se recorren con «Cargar más»' : ''}`));
    }
    const effects = {added_products: 'Agregó productos', replaced_products: 'Reemplazó productos',
      loaded_reviews: 'Cargó reseñas', replaced_reviews: 'Reemplazó reseñas', moved_carousel: 'Movió un carrusel',
      changed_url: 'Cambió la URL', no_effect: 'Sin efecto observado', unverified: 'Sin verificar', planned: 'Pendiente de visita y presupuesto', not_found: 'Control no encontrado', not_interactable: 'Control no visible o deshabilitado'};
    (result.actions || []).forEach((action, index) => {
      const card = node('article', 'scrape-entity');
      card.append(node('strong', '', `${action.control?.text || action.control?.selector || 'Control'}: ${effects[action.effect] || action.effect}`),
        node('p', '', [action.verification, action.after_products > 0 ? `Productos: ${action.before_products} → ${action.after_products}` : '', action.after_reviews > 0 ? `Reseñas: ${action.before_reviews} → ${action.after_reviews}` : ''].filter(Boolean).join(' · ')));
      if (action.budget_disposition) {
        card.append(node('p', '', `Decisión del recorrido: ${edgeText(action.budget_disposition)}`));
      }
      if (action.child_visit_id) {
        const childIndex = currentRun.visits.findIndex((visit) => visit.id === action.child_visit_id);
        if (childIndex >= 0) {
          const button = node('button', 'secondary', 'Ver visita de expansión');
          button.type = 'button'; button.addEventListener('click', () => showVisit(childIndex)); card.append(button);
        }
      }
      if (action.network) {
        const network = node('details', 'scrape-entity-text');
        network.append(node('summary', '', `Red: ${action.network.length} solicitudes de interacción · ${action.network_settled ? 'finalizadas' : 'pendientes'}`), node('pre', '', JSON.stringify(action.network, null, 2)));
        card.append(network);
      }
      const identities = action.product_identities;
      if (identities) {
        card.append(node('p', '', `Destinos de producto: ${identities.added.length} agregados · ${identities.removed.length} retirados. Se comparan enlaces, no contadores ni cambios de texto.`));
        const changes = node('details', 'scrape-entity-text');
        changes.append(node('summary', '', 'Ver destinos antes y después'), node('pre', '', JSON.stringify(identities, null, 2)));
        card.append(changes);
      }
      ['before', 'after'].forEach((phase) => {
        const evidence = action.evidence?.[phase];
        if (!evidence) return;
        const row = node('div', 'scrape-entity-links');
        const base = `/api/scrapes/${currentRun.run.id}/visits/${detail.id}/actions/${index}/${phase}`;
        const label = phase === 'before' ? 'Antes' : 'Después';
        if (evidence.screenshot_path) {
          const shot = node('a', '', `${label}: captura completa`);
          shot.href = `${base}/screenshot`; shot.target = '_blank'; shot.rel = 'noopener'; row.append(shot);
        }
        const html = node('a', '', `${label}: descargar HTML`); html.href = `${base}/html`; row.append(html); card.append(row);
      });
      const text = [action.after_content?.products?.text, action.after_content?.reviews?.text].filter(Boolean).join('\n\n');
      if (text) {
        const content = node('details', 'scrape-entity-text');
        content.append(node('summary', '', 'Contenido exacto después de la acción'), node('pre', '', text)); card.append(content);
      }
      (['planned', 'unverified'].includes(action.effect) && !action.child_visit_id ? unexecuted : target).append(card);
    });
    if (unexecuted.children.length > 1) target.append(unexecuted);
    target.append(node('p', 'coverage-note', `${identified.length} bloques mostrados · ${represented.size} contenedores anidados agrupados · ${entities.length - recognized.length} descartados · extractor ${result.model || 'sin registrar'}`));
    const supporting = node('details', 'scrape-supporting');
    supporting.append(node('summary', '', 'Ver navegación, categorías y otros bloques identificados'));
    const priority = {primary_products: 0, product: 1, attributes: 2, offer: 3, reviews: 4};
    [...identified].sort((a, b) => (priority[a.role] ?? 10) - (priority[b.role] ?? 10)).forEach((entity) => {
      const card = node('article', 'scrape-entity');
      const title = node('div', 'scrape-entity-head');
      title.append(node('strong', '', name(entity.role)),
        node('span', '', confidence(entity.confidence)));
      card.append(title);
      if (entity.source_action != null) card.append(node('p', 'coverage-note', `Contenido posterior a la acción ${entity.source_action + 1}; consulta su captura después de la interacción.${entity.unclassified_members ? ` ${entity.unclassified_members} elementos con estructura nueva requieren clasificación.` : ''}`));
      if (entity.heading) card.append(node('h3', '', entity.heading));
      const evidence = node('details', 'scrape-entity-text');
      evidence.append(node('summary', '', 'Ubicación y evidencia de esta extracción'), node('p', '', `${entity.region || 'Región desconocida'} · ${entity.selector || 'sin selector'} · evidencia: ${(entity.evidence_ids || []).join(', ') || 'sin identificador'}`));
      card.append(evidence);
      if (entity.text) {
        if (entity.purposes?.includes('content')) card.append(node('p', 'scrape-extracted-preview', entity.text.slice(0, 420) + (entity.text.length > 420 ? '…' : '')));
        const text = node('details', 'scrape-entity-text');
        text.append(node('summary', '', `Texto extraído (${entity.text.length} caracteres)`), node('pre', '', entity.text));
        card.append(text);
      }
      const links = entity.links || [];
      if (links.length) {
        const list = node('div', 'scrape-entity-links');
        links.forEach((link) => list.append(safeLink(link.url, link.text || link.url)));
        card.append(list);
      }
      const parts = (entity.parts || []).filter((part) => part.class !== 'other');
      if (parts.length) {
        const fields = node('details', 'scrape-entity-text');
        fields.append(node('summary', '', `Campos extraídos (${parts.length})`));
        parts.forEach((part) => {
          fields.append(node('strong', '', `${part.class} · confianza ${confidence(part.confidence)}`));
          fields.append(node('pre', '', JSON.stringify(part.values, null, 2)));
        });
        card.append(fields);
      }
      if (entity.controls?.length) card.append(node('p', '', `Controles: ${entity.controls.map((control) => control.text || control.selector).join(' · ')}`));
      if (entity.purposes?.includes('content')) {
        if (!contentGroups.has(entity.role)) contentGroups.set(entity.role, []);
        contentGroups.get(entity.role).push(card);
      } else supporting.append(card);
    });
    contentGroups.forEach((cards, role) => {
      const group = node('details', 'scrape-content-group');
      group.open = ['primary_products', 'product', 'attributes', 'offer', 'reviews'].includes(role);
      group.append(node('summary', '', `${name(role)} · ${cards.length} bloques identificados`));
      cards.forEach((card) => group.append(card));
      target.append(group);
    });
    if (supporting.children.length > 1) target.append(supporting);
    if (!identified.length) target.append(node('p', 'no-evidence', 'No se identificaron bloques de interés con evidencia suficiente.'));
    el('scrape-result-json').textContent = JSON.stringify({result, attempts: detail.attempts,
      model_calls: detail.model_calls, events: detail.events}, null, 2);
  }

  function renderEdges(detail) {
    const target = el('scrape-outgoing'); target.replaceChildren();
    if (!detail.outgoing_edges.length) {
      target.append(node('p', 'no-evidence', 'No se descubrieron enlaces de navegación en esta página.'));
      return;
    }
    detail.outgoing_edges.forEach((edge) => {
      const row = node('div', 'scrape-edge');
      const route = node('span', '', `#${edge.discovery_order} · ${edge.via_entity_id?.startsWith('action:') ? 'Expansión · ' : ''}${edgeText(edge.disposition)} · confianza ${confidence(edge.confidence)}`);
      row.append(route, safeLink(edge.target_uri, edge.target_uri));
      const visited = currentRun.visits.findIndex((visit) => visit.id === edge.to_visit_id);
      if (visited >= 0) {
        const button = node('button', '', `Abrir página ${visited + 1} →`);
        button.type = 'button'; button.addEventListener('click', () => showVisit(visited)); row.append(button);
      }
      target.append(row);
    });
  }

  function renderProgress(job) {
    el('scrape-progress').classList.remove('hidden');
    const finished = (job.succeeded || 0) + (job.failed || 0);
    const max = Number(el('scrape-limit').value) || 1;
    el('scrape-progress-title').textContent = job.status === 'done' ? 'Recorrido finalizado' :
      job.status === 'error' ? 'Recorrido interrumpido' : 'Explorando páginas y expandiendo contenido…';
    el('scrape-progress-count').textContent = `${finished} terminadas · ${job.scheduled || 0} iniciadas / ${max} permitidas`;
    el('scrape-progress-fill').style.width = job.status === 'done' ? '100%' : `${Math.min(100, 100 * finished / max)}%`;
    el('scrape-progress-detail').textContent = `${job.succeeded || 0} analizadas · ${job.failed || 0} fallidas · ${job.doubtful || 0} dudosas · límite ${max}`;
  }

  async function pollJob(jobId) {
    while (true) {
      const job = await json(`/api/scrapes/jobs/${encodeURIComponent(jobId)}`);
      renderProgress(job);
      if (job.run_id) {
        const previousVisit = currentRun?.run.id === job.run_id ? currentRun.visits[currentIndex]?.id : undefined;
        await openRun(job.run_id, previousVisit);
      }
      if (job.status !== 'running') {
        remember({jobId: null, runId: job.run_id || saved.runId});
        await refreshRuns();
        if (job.status === 'error') throw new Error(job.message);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }

  el('scrape-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = el('scrape-form').querySelector('button');
    button.disabled = true;
    remember();
    try {
      const job = await json('/api/scrapes', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({urls: el('scrape-url').value.split(/\r?\n/).map(url => url.trim()).filter(Boolean), strategy: el('scrape-strategy').value, max_sites: Number(el('scrape-limit').value),
          max_hops: Number(el('scrape-hops').value)})});
      remember({jobId: job.job_id});
      await pollJob(job.job_id);
    } catch (error) { el('scrape-progress-title').textContent = error.message; }
    finally { button.disabled = false; }
  });
  el('scrape-refresh').addEventListener('click', refreshRuns);
  el('scrape-prev').addEventListener('click', () => showVisit(currentIndex - 1));
  el('scrape-next').addEventListener('click', () => showVisit(currentIndex + 1));
  el('scrape-visit-filter').addEventListener('change', () => { if (currentRun) renderTimeline(); });
  if (linkedRun) document.querySelector('[data-tab="scrape"]').click();
  refreshRuns().then((runs) => {
    if (linkedRun) openRun(linkedRun).then(() => (location.hash === '#scrape-commercial' ? el('scrape-commercial') : el('scrape-run-stats')).scrollIntoView({block: 'start'})).catch((error) => { el('scrape-runs').prepend(node('p', 'no-evidence', error.message)); });
    else if (saved.jobId) pollJob(saved.jobId).catch(() => remember({jobId: null}));
    else if (saved.runId || runs?.[0]) openRun(saved.runId || runs[0].id).catch(() => {});
  });
})();
