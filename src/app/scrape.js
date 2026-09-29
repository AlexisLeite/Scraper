/* Browser for durable, breadth-first scraping history. */
(() => {
  const el = (id) => document.getElementById(id);
  const KEY = 'markup-studio:scrape:v1';
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY)) || {}; } catch { /* Ignore old drafts. */ }
  el('scrape-url').value = saved.url || '';
  el('scrape-limit').value = saved.maxSites || 10;
  el('scrape-hops').value = saved.maxHops ?? 2;
  let currentRun = null;
  let currentIndex = -1;
  let requestSerial = 0;

  function remember(changes = {}) {
    saved = {...saved, url: el('scrape-url').value, maxSites: Number(el('scrape-limit').value),
      maxHops: Number(el('scrape-hops').value), ...changes};
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch { /* History is on the server. */ }
  }
  ['scrape-url', 'scrape-limit', 'scrape-hops'].forEach((id) => el(id).addEventListener('change', () => remember()));

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
  function statusText(status) {
    return ({done: 'Analizada', error: 'Falló', running: 'En curso'})[status] || status;
  }
  function edgeText(disposition) {
    return ({scheduled: 'Visitada', queued: 'En cola', max_hops: 'Límite de saltos',
      max_sites: 'Límite de sitios', duplicate: 'Duplicada', doubtful_page: 'Página dudosa',
      doubtful_entity: 'Entidad dudosa', invalid_uri: 'URL inválida',
      outside_seed_hosts: 'Fuera de los hosts iniciales'})[disposition] || disposition;
  }

  async function refreshRuns() {
    try {
      const runs = await json('/api/scrapes');
      const target = el('scrape-runs'); target.replaceChildren();
      if (!runs.length) target.append(node('p', 'no-evidence', 'Todavía no hay recorridos guardados.'));
      runs.forEach((run) => {
        const button = node('button', 'saved-result-row'); button.type = 'button';
        const root = run.config?.initial_uris?.[0] || run.id;
        const summary = run.summary;
        button.append(node('strong', '', root),
          node('small', '', `${new Date(run.started_at).toLocaleString('es')} · ${summary ? `${summary.succeeded}/${summary.scheduled} páginas analizadas` : 'Recorrido en curso o interrumpido'} · límite ${run.config?.max_sites ?? '?'}`));
        button.addEventListener('click', () => openRun(run.id));
        target.append(button);
      });
    } catch (error) { el('scrape-runs').replaceChildren(node('p', 'no-evidence', error.message)); }
  }

  async function openRun(runId, preferredVisitId) {
    const serial = ++requestSerial;
    const data = await json(`/api/scrapes/${encodeURIComponent(runId)}`);
    if (serial !== requestSerial) return;
    currentRun = data;
    remember({runId});
    const visits = data.visits;
    const run = data.run;
    el('scrape-explorer').classList.remove('hidden');
    el('scrape-run-title').textContent = run.config?.initial_uris?.[0] || 'Recorrido';
    el('scrape-run-meta').textContent = `${visits.length} páginas visitadas · límite ${run.config?.max_sites ?? '?'} · ${run.finished_at ? 'Finalizado' : 'En curso o interrumpido'} · cola por niveles`;
    const timeline = el('scrape-timeline'); timeline.replaceChildren();
    visits.forEach((visit, index) => {
      const item = node('button', 'scrape-visit'); item.type = 'button';
      item.style.marginLeft = `${Math.min(visit.depth, 6) * 8}px`;
      item.append(node('span', 'scrape-visit-number', String(index + 1).padStart(2, '0')),
        node('span', 'scrape-visit-title', visit.title || visit.requested_uri),
        node('small', '', `Salto ${visit.depth} · ${statusText(visit.status)}${visit.doubtful ? ' · Dudosa' : ''}`));
      item.addEventListener('click', () => showVisit(index));
      timeline.append(item);
    });
    if (!visits.length) {
      timeline.append(node('p', 'no-evidence', 'Aún no hay páginas visitadas.'));
      el('scrape-detail').classList.add('hidden');
      return;
    }
    el('scrape-detail').classList.remove('hidden');
    const index = Math.max(0, visits.findIndex((visit) => visit.id === preferredVisitId));
    await showVisit(index);
  }

  async function showVisit(index) {
    if (!currentRun || index < 0 || index >= currentRun.visits.length) return;
    currentIndex = index;
    const visit = currentRun.visits[index];
    document.querySelectorAll('.scrape-visit').forEach((item, i) => item.classList.toggle('selected', i === index));
    el('scrape-prev').disabled = index === 0;
    el('scrape-next').disabled = index === currentRun.visits.length - 1;
    const runId = currentRun.run.id;
    const base = `/api/scrapes/${encodeURIComponent(runId)}/visits/${encodeURIComponent(visit.id)}`;
    const detail = await json(base);
    if (currentRun?.run.id !== runId || currentIndex !== index) return;
    el('scrape-page-position').textContent = `PÁGINA ${index + 1} / ${currentRun.visits.length} · DESCUBRIMIENTO ${visit.discovery_order}`;
    el('scrape-page-title').textContent = detail.title || detail.requested_uri;
    const url = el('scrape-page-url'); url.textContent = detail.final_uri || detail.requested_uri;
    url.href = detail.final_uri || detail.requested_uri;
    el('scrape-page-meta').textContent = `Salto ${detail.depth} · ${statusText(detail.status)} · ${detail.page_type || 'Tipo desconocido'} · confianza ${confidence(detail.page_confidence)}${detail.doubtful ? ' · Página dudosa: no se siguieron sus enlaces' : ''}${detail.error ? ` · ${detail.error}` : ''}`;
    const shot = el('scrape-shot');
    shot.classList.toggle('hidden', !detail.screenshot_path);
    el('scrape-no-shot').classList.toggle('hidden', !!detail.screenshot_path);
    shot.removeAttribute('src');
    if (detail.screenshot_path) shot.src = `${base}/screenshot`;
    const download = el('scrape-html-download'); download.href = `${base}/html`;
    download.classList.toggle('hidden', !detail.html_path);
    renderEntities(detail);
    renderEdges(detail);
  }

  function renderEntities(detail) {
    const target = el('scrape-entities'); target.replaceChildren();
    const result = detail.result;
    if (!result) {
      target.append(node('p', 'no-evidence', detail.status === 'running' ? 'La página se está analizando.' : 'No hay extracciones; consulta el error y los intentos en el resultado completo.'));
      el('scrape-result-json').textContent = JSON.stringify({attempts: detail.attempts, error: detail.error}, null, 2);
      return;
    }
    const entities = result.entities || [];
    const identified = entities.filter((item) => item.role !== 'other');
    target.append(node('p', 'coverage-note', `${identified.length} grupos identificados · ${entities.length - identified.length} con evidencia insuficiente · modelo ${result.model || 'Jev'}`));
    identified.forEach((entity) => {
      const card = node('article', 'scrape-entity');
      const title = node('div', 'scrape-entity-head');
      title.append(node('strong', '', entity.role.replaceAll('_', ' ')),
        node('span', '', confidence(entity.confidence)));
      card.append(title);
      if (entity.heading) card.append(node('h3', '', entity.heading));
      card.append(node('p', '', `${entity.region || 'Región desconocida'} · ${entity.selector || 'sin selector'} · evidencia: ${(entity.evidence_ids || []).join(', ') || 'sin identificador'}`));
      if (entity.text) {
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
      if (entity.controls?.length) card.append(node('p', '', `Controles: ${entity.controls.map((control) => control.text || control.selector).join(' · ')}`));
      target.append(card);
    });
    if (!identified.length) target.append(node('p', 'no-evidence', 'Jev no identificó grupos de interés con evidencia suficiente.'));
    el('scrape-result-json').textContent = JSON.stringify({result, attempts: detail.attempts, events: detail.events}, null, 2);
  }

  function renderEdges(detail) {
    const target = el('scrape-outgoing'); target.replaceChildren();
    if (!detail.outgoing_edges.length) {
      target.append(node('p', 'no-evidence', 'No se descubrieron enlaces de navegación en esta página.'));
      return;
    }
    detail.outgoing_edges.forEach((edge) => {
      const row = node('div', 'scrape-edge');
      const route = node('span', '', `#${edge.discovery_order} · ${edgeText(edge.disposition)} · confianza ${confidence(edge.confidence)}`);
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
      job.status === 'error' ? 'Recorrido interrumpido' : 'Recorriendo páginas por niveles…';
    el('scrape-progress-count').textContent = `${finished} / ${job.scheduled || 0} visitadas`;
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
        body: JSON.stringify({url: el('scrape-url').value.trim(), max_sites: Number(el('scrape-limit').value),
          max_hops: Number(el('scrape-hops').value)})});
      remember({jobId: job.job_id});
      await pollJob(job.job_id);
    } catch (error) { el('scrape-progress-title').textContent = error.message; }
    finally { button.disabled = false; }
  });
  el('scrape-refresh').addEventListener('click', refreshRuns);
  el('scrape-prev').addEventListener('click', () => showVisit(currentIndex - 1));
  el('scrape-next').addEventListener('click', () => showVisit(currentIndex + 1));
  refreshRuns().then(() => {
    if (saved.jobId) pollJob(saved.jobId).catch(() => remember({jobId: null}));
    else if (saved.runId) openRun(saved.runId).catch(() => {});
  });
})();
