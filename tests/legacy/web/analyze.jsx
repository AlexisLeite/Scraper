'use client';
import {useEffect,useRef,useState} from 'react';
import {api,post,pause,read,write,label} from '../lib/api';
import {Heading,Panel,Progress,JsonView,Link} from './shared';
const DRAFT='markup-studio:draft:v1',STORE='markup-studio:v1';
function download(content,name) {const url=URL.createObjectURL(new Blob([content],{type:'text/html;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function Links({items=[]}) {return <div className="link-list">{items.map((item,index)=><div className="evidence-link" key={index}><Link url={item.url}>{item.label||item.text||item.url}</Link>{typeof item.relevance==='number'&&<span className="relevance-score">{Math.round(item.relevance*100)}%</span>}</div>)}{!items.length&&<p className="no-evidence">Sin enlaces identificados.</p>}</div>;}
export default function Analyze() {
  const [url,setUrl]=useState(''),[objective,setObjective]=useState(''),[html,setHtml]=useState('');
  const [result,setResult]=useState(null),[recent,setRecent]=useState([]),[job,setJob]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const mounted=useRef(true);
  useEffect(()=>{mounted.current=true;const draft=read(DRAFT),store=read(STORE);setUrl(draft.url??store.url??'');setObjective(draft.objective??store.objective??'');setRecent(store.recent||[]);return()=>{mounted.current=false;};},[]);
  function draft(changes) {write(DRAFT,{...read(DRAFT),url,objective,...changes});}
  async function start(imported=false) {
    setError('');setBusy(true);draft({});setJob({percent:0,message:'Preparando análisis…',phase:'queued'});
    try {
      const response=await post(imported?'/api/analyze-html':'/api/analyze',{url:url.trim(),objective:objective.trim(),...(imported?{html}: {})});
      let progress;
      do {await pause(650);progress=await api(`/api/jobs/${response.job_id}`);if(!mounted.current)return;setJob(progress);} while(progress.status==='running');
      if(progress.status==='error') throw new Error(progress.message);
      const value=progress.result;setResult(value);
      const compact={...value,html:undefined,clean_html:undefined,screenshot:value.screenshot?.length<200000?value.screenshot:undefined};
      const entries=[{url:value.url,objective,savedAt:Date.now(),result:compact},...recent.filter(entry=>entry.url!==value.url||entry.objective!==objective)].slice(0,5);
      setRecent(entries);write(STORE,{...read(STORE),recent:entries});
    } catch(exc){if(mounted.current)setError(exc.message);}finally{if(mounted.current)setBusy(false);}
  }
  const analysis=result?.analysis||{},stats=result?.stats||{},reviews=analysis.reviews||{};
  return <>
    <Heading eyebrow="EXPLORACIÓN WEB" title={<>Entiende lo que hay<br/><em>detrás de una página.</em></>} subtitle="Renderiza una URL y encuentra productos, reseñas y rutas de navegación relevantes para tu objetivo."/>
    <Panel title="URL de la página" className="input-panel"><form className="url-form" onSubmit={event=>{event.preventDefault();start();}}><label className="sr-only" htmlFor="url-input">URL</label><span className="url-prefix">↗</span><input id="url-input" type="url" required autoComplete="url" value={url} onChange={event=>{setUrl(event.target.value);draft({url:event.target.value});}} placeholder="https://ejemplo.com/pagina"/><button className="primary-button" disabled={busy}>{busy?'Analizando…':'Analizar página →'}</button></form><label className="objective-label" htmlFor="objective-input">OBJETIVO DE EXPLORACIÓN · OPCIONAL</label><input className="objective-input" id="objective-input" maxLength={1000} value={objective} onChange={event=>{setObjective(event.target.value);draft({objective:event.target.value});}} placeholder="Ej.: encontrar zapatillas de running"/><p className="input-note">Esta herramienta analiza la página con Jev. Para usar extracción gratuita, selecciona la estrategia semántica en «Recorrer sitio».</p></Panel>
    {error&&<p role="alert" className="no-evidence">{error}</p>}
    {job&&<Progress title={job.message} percent={job.percent} detail={job.phase==='actions'?'Playwright · verificación':'Chromium · DOM · Jev'}/>}
    <details className="panel import-panel"><summary>Importar HTML del navegador</summary><p>Si un sitio bloquea la captura, pega su HTML renderizado e indica la URL arriba.</p><textarea aria-label="HTML renderizado" value={html} onChange={event=>setHtml(event.target.value)} placeholder="Pega aquí el HTML renderizado…"/><button className="secondary-button" disabled={busy||!html.trim()||!url.trim()} onClick={()=>start(true)}>Analizar HTML del navegador</button></details>
    {recent.length>0&&<Panel title="Análisis guardados en este navegador">{recent.map((entry,index)=><button className="saved-result-row" key={`${entry.url}:${index}`} onClick={()=>{setResult(entry.result);setUrl(entry.url);setObjective(entry.objective||'');}}><strong>{entry.result.title||entry.url}</strong><small>{entry.url} · {new Date(entry.savedAt).toLocaleString('es')}</small></button>)}</Panel>}
    {!result?<div className="empty-state"><span className="empty-glyph">⌁</span><h3>Tu análisis aparecerá aquí</h3><p>Introduce una URL pública para empezar.</p></div>:<div className="results" id="capture-result">
      <div className="result-heading"><div><span className="overline">RESULTADO / JEV + DOM</span><h2>{result.title||'Página sin título'}</h2><Link url={result.url}/></div><div className="download-actions"><button className="secondary-button" disabled={!result.clean_html} onClick={()=>download(result.clean_html,'pagina-limpia.html')}>↓ HTML extraído</button><button className="secondary-button" disabled={!result.html} onClick={()=>download(result.html,'pagina-renderizada.html')}>↓ HTML original</button></div></div>
      <div className="metrics">{[[stats.elements,'ELEMENTOS'],[stats.candidate_groups,'GRUPOS CANDIDATOS'],[stats.decided_groups,'GRUPOS IDENTIFICADOS'],[stats.content_chars,'CARACTERES']].map(([value,title])=><div className="metric" key={title}><span>{title}</span><strong>{Number(value||0).toLocaleString('es')}</strong></div>)}</div>
      <div className="analysis-summary panel"><div><span className="overline">TIPO DE PÁGINA</span><h3>{label(result.classification?.value)}</h3><p>Jev · confianza {result.classification?.confidence==null?'sin registro':`${Math.round(result.classification.confidence*100)}%`}</p></div><div><span className="overline">SEÑALES DETECTADAS</span><p>{analysis.groups?.length||0} grupos · {stats.links||0} enlaces · {analysis.primary_group||'Sin colección principal confirmada'}</p></div></div>
      <div className="insight-grid"><Panel title="Productos similares"><Links items={analysis.related_products}/></Panel><Panel title="Reseñas"><p>{reviews.state?.value||'Estado incierto'} · recorrido {reviews.mode?.value||'desconocido'}</p><Links items={reviews.links}/></Panel><Panel title="Categorías a recorrer"><Links items={analysis.categories}/></Panel><Panel title="Navegación del listado"><Links items={analysis.product_links}/>{(analysis.actions||[]).map((action,index)=><div className="navigation-step" key={index}><strong>{action.effect}: {action.before_products??'?'} → {action.after_products??'?'} productos</strong><p>{action.control?.text||action.control?.selector}</p><JsonView value={action} title="Evidencia de la interacción"/></div>)}</Panel></div>
      <JsonView value={{classification:result.classification,analysis:result.analysis}} title="Decisiones y evidencias de Jev"/>
      <div className="result-grid"><Panel title="Vista renderizada" className="preview-panel">{result.screenshot?<img src={`data:image/jpeg;base64,${result.screenshot}`} alt="Captura de la página renderizada"/>:<p>Sin captura disponible en este resultado guardado.</p>}</Panel><Panel title="Contenido detectado"><h3>Encabezados</h3><ul>{(result.headings||[]).map((heading,index)=><li key={index}>{heading}</li>)}</ul><h3>Extracto de texto</h3><p>{result.text_preview||'No se encontró texto visible.'}</p></Panel></div>
    </div>}
  </>;
}
