export async function api(path, options = {}) {
  const response = await fetch(path, {cache:'no-store', ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message=typeof data.detail==='string'?data.detail:Array.isArray(data.detail)?data.detail.map(issue=>issue.msg?.replace(/^Value error, /,'')).filter(Boolean).join('; '):'';
    throw new Error(message||`Error HTTP ${response.status}`);
  }
  return data;
}
export const post = (path, body) => api(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
export const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
export function read(key, fallback = {}) { try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch { return fallback; } }
export function write(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* The server retains extraction history. */ } }
export function strategyName(run) {
  const name=run?.config?.strategy;
  return name?.endsWith('.SemanticMetadataStrategy') ? 'Semántica' : name?.endsWith('.JevStrategy') ? 'Jev' : name?.split('.').pop() || 'Sin registrar';
}
export function cost(usage, costs) {
  if (costs) return `${costs.exact?'':'Al menos '}US$${Number(costs.amount_usd).toFixed(6)}`;
  if (!usage) return 'Sin registro';
  return `${usage.exact?'':'Al menos '}US$${(Number(usage.input_tokens||0)*0.042/1e6).toFixed(6)}`;
}
export function usageLabel(usage, costs) {
  return `${cost(usage,costs)}${usage?.calls ? ` · ${usage.calls} llamadas · ${usage.input_tokens} tokens de entrada · ${usage.output_tokens} de salida` : ' · Sin llamadas a modelos'}${costs&&!costs.exact ? ' · Gasto parcial' : ''}`;
}
export const names = {listing:'Listado de productos', product:'Producto', other:'Sin clasificación suficiente', primary_products:'Productos principales', secondary_products:'Productos secundarios', products:'Productos', attributes:'Características', offer:'Oferta y precio', reviews:'Reseñas', categories:'Categorías', breadcrumb:'Ruta de navegación', collection_control:'Controles de la colección', site_menu:'Menú del sitio', header:'Cabecera', footer:'Pie de página', promotion:'Promoción', filters:'Filtros', qa:'Preguntas y respuestas', discussion:'Debate', editorial:'Contenido editorial', search:'Buscador', brand:'Marca', service:'Servicios', metadata:'Metadatos', semantic_collection:'Colección declarada', semantic_content:'Contenido principal', semantic_article:'Artículo HTML', semantic_table:'Tabla HTML'};
export const label = value => names[value] || value || 'Sin clasificar';
