'use client';
import {memo,useEffect,useRef,useState} from 'react';
import {records} from '../lib/commercial';
import {label,strategyName,usageLabel} from '../lib/api';
import {JsonView,Link} from './shared';
const productRoles=['primary_products','secondary_products','products','product','attributes','offer'];
const navigationRoles=['categories','breadcrumb','site_menu','header','footer','filters','search','collection_control','semantic_collection'];
const fieldNames={name:'Nombre',brand:'Marca',image:'Imagen',product_link:'Enlace',description:'Descripción',benefit:'Beneficios',ingredients:'Ingredientes',usage:'Modo de uso',size:'Tamaño',price:'Precio',previous_price:'Precio anterior',availability:'Disponibilidad',rating:'Puntuación',rating_count:'Valoraciones',review_count:'Reseñas',sku:'SKU',product_id:'ID de producto',mpn:'Código del fabricante',gtin:'GTIN',review_title:'Título',review_body:'Opinión',author:'Autor',date:'Fecha',question:'Pregunta',answer:'Respuesta'};
const knownAvailability={InStock:'Disponible',OutOfStock:'Agotado',PreOrder:'Reserva',Discontinued:'Descontinuado'};
function valueText(value,kind) {const text=value.text||value.href||value.src||value.datetime||'';if(kind==='availability')return knownAvailability[String(text).split('/').pop()]||text;return kind==='price'&&value.currency?`${text} ${value.currency}`:text;}
function Section({id,title,note,count,children,initialOpen=false}) {
  return <details className="page-result-section" id={id} open={initialOpen}><summary><span className="result-section-title">{title}{count!=null&&<span className="result-count">{count}</span>}</span><span className="result-section-note">{note}</span></summary><div className="result-section-body">{children}</div></details>;
}
function Fields({fields}) {
  const order=['name','price','previous_price','availability','brand','size','rating','rating_count','description','image','product_link'];
  const rank=kind=>order.includes(kind)?order.indexOf(kind):order.length;
  const sorted=Object.entries(fields).sort(([a],[b])=>rank(a)-rank(b));
  return <dl className="result-fields">{sorted.map(([kind,values])=><div className={`result-field field-${kind}`} key={kind}><dt>{fieldNames[kind]||kind}</dt><dd>{values.map((value,index)=><div key={index}>
    {kind==='image'&&/^https?:\/\//i.test(value.src||value.text||'')?<img className="result-product-image" src={value.src||value.text} alt="Imagen declarada del producto" loading="lazy" referrerPolicy="no-referrer"/>:kind==='product_link'?<Link url={value.href||value.text}>{valueText(value,kind)}</Link>:<span>{valueText(value,kind)}</span>}
  </div>)}</dd></div>)}</dl>;
}
function entityFields(entity) {
  const fields={};
  for(const part of entity.parts||[]) {
    if(part.class==='other') continue;
    const values=(part.values||[]).filter(v=>valueText(v,part.class));
    if(!values.length)continue;
    const entries=fields[part.class]||=[];
    for(const value of values)if(!entries.some(v=>valueText(v,part.class)===valueText(value,part.class)))entries.push(value);
  }
  return fields;
}
function ExtractedBlock({entity}) {
  const fields=entityFields(entity);
  const source=entity.attributes?.source;
  return <article className="result-block"><h3>{entity.heading||label(entity.role)}</h3>{source&&<span className="result-source">{source==='jsonld'||source==='head'?'Metadatos declarados':'HTML semántico'}</span>}
    {!!Object.keys(fields).length&&<Fields fields={fields}/>}
    {entity.text&&<p className="result-content-preview">{entity.text.slice(0,320)}{entity.text.length>320?'…':''}</p>}
    {entity.text?.length>320&&<details className="result-text"><summary>Leer contenido completo ({entity.text.length.toLocaleString('es')} caracteres)</summary><p>{entity.text}</p></details>}
    {entity.attributes?.rows?.length>0&&<details className="result-text"><summary>Ver tabla ({entity.attributes.rows.length} filas capturadas)</summary><div className="result-table"><table><tbody>{entity.attributes.rows.map((row,i)=><tr key={i}>{row.cells.map((cell,j)=><td key={j}>{cell}</td>)}</tr>)}</tbody></table></div></details>}
    {!Object.keys(fields).length&&!entity.text&&<p className="result-empty">Sin campos ni texto extraídos en este bloque.</p>}
    {!!entity.links?.length&&<details className="result-text"><summary>Enlaces del bloque ({entity.links.length})</summary><ul>{entity.links.map((link,i)=><li key={i}><Link url={link.url}>{link.text||link.url}</Link></li>)}</ul></details>}
  </article>;
}
function ResultContents({detail,base,run,select,visits,index}) {
  const [dom,setDom]=useState(null),[sourceState,setSourceState]=useState(detail.html_path?'loading':'missing');
  useEffect(()=>{
    let active=true;const controller=new AbortController();setDom(null);setSourceState(detail.html_path?'loading':'missing');
    if(detail.html_path)fetch(`${base}/html?clean=true`,{signal:controller.signal}).then(async response=>{if(!response.ok)throw new Error();const html=await response.text();if(active){setDom(new DOMParser().parseFromString(html,'text/html'));setSourceState('ready');}}).catch(()=>{if(active)setSourceState('unavailable');});
    return()=>{active=false;controller.abort();};
  },[base,detail.html_path]);
  const entities=(detail.result?.entities||[]).filter(e=>e.role!=='other');
  const associated=records(detail,dom),products=[],reviews=[],covered=new Set();
  for(const group of associated.groups.values())for(const record of group.records.values()) {
    const item={role:group.role,record};
    if(productRoles.includes(group.role))products.push(item);
    else if(['reviews','qa','discussion'].includes(group.role))reviews.push(item);
    for(const id of record.entityIds)covered.add(id);
  }
  const productBlocks=entities.filter(e=>productRoles.includes(e.role)&&!covered.has(e.id));
  const reviewBlocks=entities.filter(e=>['reviews','qa','discussion'].includes(e.role)&&!covered.has(e.id));
  const content=entities.filter(e=>!productRoles.includes(e.role)&&!['reviews','qa','discussion','metadata'].includes(e.role)&&!navigationRoles.includes(e.role));
  const metadata=entities.filter(e=>e.role==='metadata');
  const navigation=entities.filter(e=>navigationRoles.includes(e.role));
  const meaningfulProducts=products.length||productBlocks.some(e=>Object.keys(entityFields(e)).length);
  const hasContent=content.some(e=>e.text||Object.keys(entityFields(e)).length);
  const hasFields=entities.some(e=>Object.keys(entityFields(e)).length||e.text);
  const heading=detail.status==='error'?'La extracción falló':detail.status==='running'?'Extracción en curso':!detail.result?'Visita terminada sin resultado':meaningfulProducts?'Se capturaron datos de productos':hasContent?'Contenido capturado; sin productos identificados':hasFields?'Solo se capturaron metadatos y datos auxiliares':'No se capturaron datos útiles';
  const coverage=detail.result?.classification?.coverage||{};
  const omissions=Number(coverage.omitted_entities||0)+Number(coverage.omitted_navigation_links||0);
  const warnings=[detail.error,detail.doubtful?'La visita quedó marcada para revisión.':null,
    metadata.length?'Los metadatos son declaraciones del sitio; no confirman que el dato esté visible.':null,
    omissions?`${[coverage.omitted_entities?`${coverage.omitted_entities} bloques`:null,coverage.omitted_navigation_links?`${coverage.omitted_navigation_links} enlaces`:null].filter(Boolean).join(' y ')} omitidos por los límites de extracción.`:null,
    coverage.jsonld_errors?.length?`${coverage.jsonld_errors.length} documentos de metadatos no se pudieron interpretar.`:null,
    sourceState==='unavailable'?'No se pudo cargar el HTML de respaldo. Los campos se muestran sin comprobar su asociación en el DOM.':null].filter(Boolean);
  const fieldCount=entities.reduce((sum,e)=>sum+(e.parts||[]).filter(p=>p.class!=='other'&&p.values?.length).length,0);
  const actions=detail.result?.actions||[],edges=detail.outgoing_edges||[];
  function Record({item}) {const {record,role}=item;return <article className={productRoles.includes(role)?'result-block commercial-product':'result-block commercial-review'}><h3>{record.fields.name?.[0]?.text||record.fields.review_title?.[0]?.text||label(role)}</h3><span className="result-source">Campos asociados en el HTML capturado</span><Fields fields={record.fields}/><details className="result-text"><summary>Fuente de los campos</summary><p>Bloques: {[...record.entityIds].join(', ')}</p></details></article>;}
  return <div className="page-result" id="scrape-page-result">
    <header className={`page-verdict verdict-${detail.status}`}><div className="visit-context">Visita {index+1} de {visits.length} · {detail.kind==='expansion'?'Estado después de expandir':'Página'} · {strategyName(run)}</div><h2>{detail.title||detail.requested_uri}</h2><Link url={detail.final_uri||detail.requested_uri}/><div className="visit-result-heading"><span className="result-status">{detail.status==='done'?'Finalizada':detail.status==='error'?'Falló':'En curso'}</span><h3>{heading}</h3></div>
      <div className="visit-facts"><span><strong>{fieldCount}</strong> campos extraídos</span><span><strong>{entities.length}</strong> bloques reconocidos</span><span>{usageLabel(detail.usage,detail.costs)}</span></div>
      {warnings.length>0&&<ul className="result-caveats">{warnings.map((warning,i)=><li key={i}>{warning}</li>)}</ul>}
      <div className="visit-toolbar"><button className="secondary-button" disabled={index<=0} onClick={()=>select(visits[index-1]?.id)}>Anterior</button><button className="secondary-button" disabled={index>=visits.length-1} onClick={()=>select(visits[index+1]?.id)}>Siguiente</button>{detail.parent_visit_id&&<button className="secondary-button" onClick={()=>select(detail.parent_visit_id)}>Página de origen</button>}{detail.html_path&&<a href={`${base}/html`} download>Descargar HTML</a>}</div>
    </header>
    <div id="scrape-commercial">
      <Section id="result-products" title="Productos y ofertas" count={products.length+productBlocks.length} note={products.length?`${products.length} elementos con campos asociados`:'Campos comerciales capturados'} initialOpen={!!meaningfulProducts}>
        {sourceState==='loading'&&<p role="status">Comprobando la asociación de los campos…</p>}
        {products.map((item,i)=><Record key={i} item={item}/>)}{productBlocks.map(e=><ExtractedBlock key={e.id} entity={e}/>)}
        {!products.length&&!productBlocks.length&&<p className="result-empty">No se identificaron productos ni ofertas. El contenido y los metadatos disponibles se muestran abajo.</p>}
      </Section>
      <Section id="result-reviews" title="Reseñas y preguntas" count={reviews.length+reviewBlocks.length} note="Opiniones, valoraciones y respuestas">
        {reviews.map((item,i)=><Record key={i} item={item}/>)}{reviewBlocks.map(e=><ExtractedBlock key={e.id} entity={e}/>)}{!reviews.length&&!reviewBlocks.length&&<p className="result-empty">No se extrajeron reseñas ni preguntas.</p>}
      </Section>
      <Section id="result-content" title="Contenido de la página" count={content.length} note="Texto, artículos y tablas" initialOpen={!meaningfulProducts&&hasContent}>
        {content.map(e=><ExtractedBlock key={e.id} entity={e}/>)}{!content.length&&<p className="result-empty">No se reconocieron bloques de contenido.</p>}
      </Section>
      <Section id="result-metadata" title="Metadatos" count={metadata.length} note="Título y descripción declarados" initialOpen={!meaningfulProducts&&!hasContent&&metadata.length>0}>
        {metadata.map(e=><ExtractedBlock key={e.id} entity={e}/>)}{!metadata.length&&<p className="result-empty">No se extrajeron metadatos.</p>}
      </Section>
    </div>
    <Section id="result-navigation" title="Navegación y enlaces" count={edges.length} note="Rutas descubiertas y decisiones del recorrido">
      <details className="result-text"><summary>Menús y rutas de la página ({navigation.length} bloques)</summary>{navigation.map(e=><ExtractedBlock key={e.id} entity={e}/>)}</details>
      <div className="scrape-outgoing">{edges.map(edge=><div key={edge.id||edge.discovery_order} className="scrape-edge"><span>{({scheduled:'Visitada',queued:'En cola',max_sites:'Límite de visitas',max_hops:'Límite de saltos',duplicate:'Duplicada',blocked_url:'Excluida por lista negra',outside_allowlist:'Fuera de la lista blanca'})[edge.disposition]||edge.disposition}</span><Link url={edge.target_uri}/>{edge.to_visit_id&&visits.some(v=>v.id===edge.to_visit_id)&&<button onClick={()=>select(edge.to_visit_id)}>Abrir visita</button>}</div>)}</div>
      {!edges.length&&<p className="result-empty">No se descubrieron enlaces en esta visita.</p>}
    </Section>
    <Section id="result-capture" title="Captura de la página" note="Comparar la extracción con lo que mostró el navegador">
      {detail.screenshot_path?<a href={`${base}/screenshot`} target="_blank" rel="noopener"><img className="result-screenshot" src={`${base}/screenshot`} alt="Captura completa de la página visitada" loading="lazy"/></a>:<p className="result-empty">No hay captura disponible.</p>}
    </Section>
    <Section id="result-audit" title="Evidencias y diagnóstico" note="Selectores, llamadas, gastos e interacciones">
      <p>Tipo de página: {label(detail.page_type)}. Extractor: {detail.result?.model||'sin registrar'}. Profundidad: {detail.depth}.</p>
      {actions.map((action,i)=><article className="result-block" key={i}><h3>{action.control?.text||action.control?.selector||'Interacción'}: {action.effect}</h3><p>{action.verification}</p>{action.child_visit_id&&<button onClick={()=>select(action.child_visit_id)}>Ver visita de expansión</button>}{['before','after'].map(phase=>action.evidence?.[phase]&&<div key={phase}><a href={`${base}/actions/${i}/${phase}/screenshot`} target="_blank" rel="noopener">{phase==='before'?'Antes':'Después'}: captura</a><a href={`${base}/actions/${i}/${phase}/html`} download>HTML</a></div>)}</article>)}
      <div id="scrape-entities">{entities.map(e=><details className="result-text" key={e.id}><summary>{label(e.role)} · {e.selector}</summary><JsonView title="Campos y evidencia del bloque" value={e}/></details>)}</div>
      <JsonView value={{result:detail.result,attempts:detail.attempts,model_calls:detail.model_calls,events:detail.events,expenses:detail.expenses,costs:detail.costs}}/>
    </Section>
  </div>;
}
export default memo(ResultContents,(a,b)=>a.base===b.base&&a.index===b.index&&a.visits.length===b.visits.length&&JSON.stringify(a.detail)===JSON.stringify(b.detail)&&strategyName(a.run)===strategyName(b.run));
