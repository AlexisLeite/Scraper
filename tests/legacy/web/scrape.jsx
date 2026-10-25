'use client';
import {useEffect, useRef, useState} from 'react';
import {api,post,pause,read,write,strategyName,cost,usageLabel,label} from '../lib/api';
import PageResult from './page-result';
import {Heading,Panel,Progress} from './shared';
const KEY='markup-studio:scrape:v1';
const ruleFields=[['allowedDomains','Dominios permitidos','ejemplo.com'],['blockedDomains','Dominios excluidos','cuentas.ejemplo.com'],['allowedPatterns','URL permitidas (regex)','/catalogo/'],['blockedPatterns','URL excluidas (regex)','[?&]orden=']];
const lines=value=>value.split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
const status = value => ({done:'Analizada',error:'Falló',running:'En curso'})[value]||value;
export default function Scrape() {
  const [form,setForm]=useState({url:'',maxSites:10,maxHops:2,maxThreads:3,strategy:'jev',allowedDomains:'',blockedDomains:'',allowedPatterns:'',blockedPatterns:''});
  const [runs,setRuns]=useState([]),[data,setData]=useState(null),[detail,setDetail]=useState(null);
  const [selected,setSelected]=useState(null),[filter,setFilter]=useState('all');
  const [job,setJob]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const formRef=useRef(form),selection=useRef(null),runRef=useRef(null),detailSerial=useRef(0),runSerial=useRef(0),pollSerial=useRef(0),mounted=useRef(true);
  function remember(changes={}) {write(KEY,{...read(KEY),...formRef.current,...changes});}
  async function refresh() { const list=await api('/api/scrapes'); if(mounted.current) setRuns(list); return list; }
  async function loadDetail(runId,id) {
    const serial=++detailSerial.current;
    const value=await api(`/api/scrapes/${encodeURIComponent(runId)}/visits/${encodeURIComponent(id)}`);
    if(!mounted.current||serial!==detailSerial.current||runRef.current!==runId||selection.current!==id) return;
    setDetail(previous=>JSON.stringify(previous)===JSON.stringify(value)?previous:value);
  }
  function select(id) {
    if(!id||!runRef.current) return;
    if(selection.current!==id) setDetail(null);
    selection.current=id;setSelected(id);
    loadDetail(runRef.current,id).catch(exc=>setError(exc.message));
  }
  async function openRun(id) {
    const serial=++runSerial.current;
    const value=await api(`/api/scrapes/${encodeURIComponent(id)}`);
    if(!mounted.current||serial!==runSerial.current) return;
    const visit=runRef.current===id&&value.visits.some(v=>v.id===selection.current)?selection.current:value.visits[0]?.id;
    if(runRef.current!==id||selection.current!==visit) {++detailSerial.current;setDetail(null);}
    runRef.current=id;selection.current=visit;setSelected(visit);setData(value);
    remember({runId:id});
    const url=new URL(location.href);url.searchParams.set('run',id);history.replaceState(null,'',url);
    if(visit) await loadDetail(id,visit);
  }
  async function poll(id) {
    const serial=++pollSerial.current;setBusy(true);
    try {
      while(mounted.current&&serial===pollSerial.current) {
        const progress=await api(`/api/scrapes/jobs/${encodeURIComponent(id)}`);
        if(!mounted.current||serial!==pollSerial.current) return;
        setJob(progress);
        if(progress.run_id) await openRun(progress.run_id);
        if(progress.status!=='running') {remember({jobId:null});await refresh();if(progress.status==='error') throw new Error(progress.message);return;}
        await pause(1200);
      }
    } catch(exc) {if(mounted.current) {setError(exc.message);remember({jobId:null});}}
    finally {if(mounted.current&&serial===pollSerial.current) setBusy(false);}
  }
  useEffect(()=>{
    let active=true;
    mounted.current=true;
    const saved=read(KEY),values={url:saved.url||'',maxSites:saved.maxSites||10,maxHops:saved.maxHops??2,maxThreads:saved.maxThreads??3,strategy:saved.strategy||'jev',allowedDomains:saved.allowedDomains||'',blockedDomains:saved.blockedDomains||'',allowedPatterns:saved.allowedPatterns||'',blockedPatterns:saved.blockedPatterns||''};
    formRef.current=values;setForm(values);
    const linked=new URLSearchParams(location.search).get('run');
    refresh().then(list=>{
      if(!active) return;
      if(linked) return openRun(linked);
      if(saved.jobId) return poll(saved.jobId);
      if(saved.runId) return openRun(saved.runId);
      if(list[0]) return openRun(list[0].id);
    }).catch(exc=>{if(mounted.current)setError(exc.message);});
    return ()=>{active=false;mounted.current=false;++pollSerial.current;++runSerial.current;++detailSerial.current;};
  },[]);
  function change(name,value) {const next={...formRef.current,[name]:value};formRef.current=next;setForm(next);remember();}
  async function start(event) {
    event.preventDefault();setError('');setBusy(true);remember();
    try {const response=await post('/api/scrapes',{urls:form.url.split(/\r?\n/).map(url=>url.trim()).filter(Boolean),strategy:form.strategy,max_sites:Number(form.maxSites),max_hops:Number(form.maxHops),max_threads:Number(form.maxThreads),allowed_domains:lines(form.allowedDomains),blocked_domains:lines(form.blockedDomains),allowed_url_patterns:lines(form.allowedPatterns),blocked_url_patterns:lines(form.blockedPatterns)});remember({jobId:response.job_id});await poll(response.job_id);}
    catch(exc){setError(exc.message);setBusy(false);}
  }
  const visits=data?.visits||[],index=visits.findIndex(visit=>visit.id===selected),run=data?.run;
  const base=run&&detail?`/api/scrapes/${encodeURIComponent(run.id)}/visits/${encodeURIComponent(detail.id)}`:'';
  let runTitle=run?.config?.initial_uris?.[0]||'Recorrido';
  try {runTitle=new URL(runTitle).hostname;} catch {}
  const completed=visits.filter(v=>v.status==='done').length,failed=visits.filter(v=>v.status==='error').length;
  return <>
    {!data&&<Heading eyebrow="RECORRIDO / ESTRATEGIAS" title={<>Explora un sitio<br/><em>paso a paso.</em></>} subtitle="Indica las URL iniciales, una estrategia y un límite. Revisa el contenido capturado, su evidencia y los gastos del recorrido."/>}
    <details className="run-tools" open={!data}><summary>Nuevo recorrido</summary><Panel title="Iniciar recorrido" className="input-panel"><form id="scrape-form" className="scrape-form" onSubmit={start}>
      <label htmlFor="scrape-url">URL iniciales (una por línea)</label><textarea id="scrape-url" required rows={3} value={form.url} onChange={e=>change('url',e.target.value)} placeholder="https://ejemplo.com/catalogo"/>
      <label htmlFor="scrape-strategy">Estrategia</label><select id="scrape-strategy" value={form.strategy} onChange={e=>change('strategy',e.target.value)}><option value="jev">Jev</option><option value="semantic">Semántica · US$0</option></select>
      <label htmlFor="scrape-limit">Máximo de visitas (páginas y expansiones)</label><input id="scrape-limit" type="number" min={1} max={100} required value={form.maxSites} onChange={e=>change('maxSites',e.target.value)}/>
      <label htmlFor="scrape-hops">Máximo de saltos</label><input id="scrape-hops" type="number" min={0} max={8} required value={form.maxHops} onChange={e=>change('maxHops',e.target.value)}/><label htmlFor="scrape-threads">Visitas simultáneas</label><input id="scrape-threads" type="number" min={1} max={50} required value={form.maxThreads} onChange={e=>change('maxThreads',e.target.value)}/><details className="url-rules"><summary>Filtrar URL: listas blanca y negra</summary><p>Una regla por línea. Los dominios incluyen sus subdominios. Las regex se buscan en la URL normalizada completa. La lista negra tiene prioridad. Si la lista blanca tiene reglas, basta coincidir con una; vacía permite todas las URL. También se filtran las URL iniciales.</p><div className="url-rule-fields">{ruleFields.map(([name,title,placeholder])=><div className="url-rule-field" key={name}><label htmlFor={`scrape-${name}`}>{title}</label><textarea id={`scrape-${name}`} value={form[name]} onChange={e=>change(name,e.target.value)} placeholder={placeholder} rows={3} spellCheck={false}/></div>)}</div></details><button className="primary-button" disabled={busy}>{busy?'Recorriendo…':'Iniciar scraping →'}</button>
    </form><p className="input-note">Cada página y cada expansión consume una visita. Las URL iniciales están en el salto 0. Hasta 50 visitas simultáneas; el límite de visitas es compartido por todos los hilos.</p></Panel></details>
    {error&&<p role="alert" className="no-evidence">{error}</p>}
    {job&&<Progress title={job.status==='done'?'Recorrido finalizado':job.status==='error'?'Recorrido interrumpido':'Explorando páginas…'} detail={`${job.succeeded||0} analizadas · ${job.failed||0} fallidas · ${job.scheduled||0} iniciadas / ${form.maxSites} permitidas`} percent={job.status==='done'?100:100*((job.succeeded||0)+(job.failed||0))/Number(form.maxSites)}/>}
    <details className="run-tools" open={!data}><summary>Otros recorridos ({runs.length})</summary><Panel title="Historial de recorridos" className="scrape-history"><button className="secondary-button" id="scrape-refresh" onClick={()=>refresh().catch(exc=>setError(exc.message))}>Actualizar</button><div id="scrape-runs" className="scrape-runs">{runs.map(item=><button type="button" key={item.id} data-run-id={item.id} className={`saved-result-row ${run?.id===item.id?'selected':''}`} onClick={()=>openRun(item.id).catch(exc=>setError(exc.message))}><strong>{item.config?.initial_uris?.[0]||item.id}</strong><small>{new Date(item.started_at).toLocaleString('es')} · {strategyName(item)} · {item.summary?`${item.summary.scheduled} visitas · ${item.summary.failed} fallidas`:'En curso o interrumpido'} · {cost(item.usage,item.costs)}</small></button>)}{!runs.length&&<p className="no-evidence">Todavía no hay recorridos guardados.</p>}</div></Panel></details>
    {data&&<div id="scrape-explorer" className="results"><div className="result-heading run-heading"><div><span className="run-caption">Resultados del recorrido</span><h2 id="scrape-run-title">{runTitle}</h2><p id="scrape-run-meta" className="coverage-note">{visits.length} visitas · estrategia {strategyName(run)} · {run.config?.max_threads??'?'} hilos · límite {run.config?.max_sites} · {run.finished_at?'Finalizado':'En curso o interrumpido'} · {run.config?.traversal==='balanced'?'recorrido mixto: amplitud y profundidad':'cola por niveles'}</p><p className="run-cost">{usageLabel(run.usage,run.costs)}</p></div></div>
      <details className="run-summary"><summary>Resumen del recorrido: {visits.length} visitas · {completed} completadas · {failed} fallidas</summary><div className="scrape-stats" id="scrape-run-stats">{[[visits.length,'Visitas iniciadas'],[completed,'Análisis completados'],[failed,'Visitas fallidas'],[cost(run.usage,run.costs),'Gasto de estrategias']].map(([value,title])=><article className="scrape-stat" key={title}><span>{title}</span><strong>{value}</strong></article>)}</div>
      <p className="scrape-outcome">{run.finished_at?'Recorrido finalizado.':'Recorrido en curso o interrumpido.'} Los análisis completados no garantizan que todas las extracciones sean correctas: revisa su evidencia.</p><div className="run-policy">{[['allowed_domains','Dominios permitidos'],['blocked_domains','Dominios excluidos'],['allowed_url_patterns','Regex permitidas'],['blocked_url_patterns','Regex excluidas']].map(([key,title])=>run.config?.[key]?.length>0&&<div key={key}><strong>{title}</strong><pre>{run.config[key].join('\n')}</pre></div>)}</div></details>
      <div className="scrape-explorer-grid"><aside className="panel scrape-timeline-panel"><h2>Visitas en orden de descubrimiento</h2><label htmlFor="scrape-visit-filter" className="scrape-filter-label">Mostrar visitas</label><select id="scrape-visit-filter" value={filter} onChange={e=>setFilter(e.target.value)}><option value="all">Todas</option><option value="product">Productos</option><option value="error">Fallidas</option><option value="expansion">Expansiones</option><option value="doubtful">Dudosas</option></select><div className="scrape-timeline">{visits.map((visit,i)=>(filter==='all'||filter==='product'&&visit.page_type==='product'||filter==='error'&&visit.status==='error'||filter==='expansion'&&visit.kind==='expansion'||filter==='doubtful'&&visit.doubtful)?<button key={visit.id} type="button" className={`scrape-visit ${selected===visit.id?'selected':''}`} style={{marginLeft:Math.min(visit.depth,6)*8}} onClick={()=>select(visit.id)}><span className="scrape-visit-number">{String(i+1).padStart(2,'0')}</span><span className="scrape-visit-title">{visit.title||visit.requested_uri}</span><small>{visit.kind==='expansion'?'Expansión':'Página'} · {label(visit.page_type)} · Salto {visit.depth} · {status(visit.status)}</small></button>:null)}</div></aside>
      <div id="scrape-detail" className="scrape-detail">{!detail?<p role="status">{selected?'Cargando visita…':data.edges?.some(edge=>['blocked_url','outside_allowlist'].includes(edge.disposition))?'No se inició ninguna visita: las URL iniciales quedaron excluidas por las listas blanca o negra.':'Todavía no hay visitas.'}</p>:<>
        <PageResult key={base} detail={detail} base={base} run={run} visits={visits} index={index} select={select}/>
      </>}</div></div>
    </div>}
  </>;
}
