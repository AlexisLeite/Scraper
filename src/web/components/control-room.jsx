'use client';

import {Children, memo, useEffect, useRef, useState} from 'react';

const names = {completed_with_errors:'Sin pendientes · con errores',idle:'idle',queued:'En cola',running:'En curso',paused:'Pausada',retry_wait:'Espera de reintento',done:'Finalizada',error:'Error',excluded:'Excluida',starting:'Preparando navegador',loading:'Cargando página',capture:'Guardando captura',extracting:'Extrayendo contenido',saving:'Guardando resultados',interaction:'Expandiendo contenido'};
const sections = {dashboard:'Análisis',extract:'Extraer',active:'Actividad',seeds:'Dominios y páginas',settings:'Configuración',activities:'Historial',failures:'Fallidas',suspects:'Dominios marcados'};
const emptySeed = {url:'',enabled:true,max_urls_per_minute:null,max_pages:null,rule_mode:'black',allowed_domains:[],blocked_domains:[],allowed_url_patterns:[],blocked_url_patterns:[]};
const ruleNames = {allowed_domains:'Dominios permitidos',blocked_domains:'Dominios bloqueados',allowed_url_patterns:'Regex permitidas',blocked_url_patterns:'Regex bloqueadas'};
const date = value => value ? new Date(typeof value==='number'?value*1000:value).toLocaleString('es-UY') : '—';
const label = value => names[value] || value;
async function api(path,body,method='POST') {
  const response=await fetch(path.startsWith('/api/')?path:'/api/service'+path,{cache:'no-store',...(body!==undefined?{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});
  const text=await response.text();
  let data;
  try{data=JSON.parse(text);}catch{throw new Error(response.ok?'El servidor devolvió una respuesta inválida.':`No se pudo completar la solicitud (HTTP ${response.status}).`);}
  if(!response.ok) throw new Error(Array.isArray(data.detail)?data.detail.map(x=>x.msg).join(' · '):data.detail||'No se pudo conectar con el servicio.');
  return data;
}
function routeHref(changes,reset=false){const query=new URLSearchParams(!reset&&typeof window!=='undefined'?window.location.search:'');for(const [key,value] of Object.entries(changes)){if(value===null||value==='')query.delete(key);else query.set(key,String(value));}return '?'+query.toString();}
function goTo(href){if(href===window.location.search)return;history.pushState(null,'',href);window.dispatchEvent(new PopStateEvent('popstate'));window.scrollTo({top:0,left:0,behavior:'instant'});}
function RouteLink({href,children,disabled=false,className='',...props}){return <a {...props} className={className+(disabled?' disabled-link':'')} href={disabled?undefined:href} aria-disabled={disabled||undefined} onClick={event=>{if(disabled){event.preventDefault();return;}if(event.button===0&&!event.metaKey&&!event.ctrlKey&&!event.shiftKey&&!event.altKey){event.preventDefault();goTo(href);}}}>{children}</a>;}
function useUrlState(key,fallback){const [value,setValue]=useState(fallback);useEffect(()=>{function sync(){const raw=new URLSearchParams(location.search).get(key);setValue(typeof fallback==='number'?Math.max(1,Number.parseInt(raw||fallback,10)||fallback):raw??fallback);}sync();window.addEventListener('popstate',sync);return()=>window.removeEventListener('popstate',sync);},[key,fallback]);return [value,next=>goTo(routeHref({[key]:next}))];}
function useData(path,interval=0) {
  const [data,setData]=useState(null),[error,setError]=useState(''),[version,setVersion]=useState(0);
  const currentPath=useRef(path);
  useEffect(()=>{let alive=true,timer; if(currentPath.current!==path){setData(null);currentPath.current=path;} const load=async()=>{try{const result=await api(path);if(alive){setData(old=>JSON.stringify(old)===JSON.stringify(result)?old:result);setError('');}}catch(e){if(alive)setError(e.message);}finally{if(alive&&interval)timer=setTimeout(load,interval);}};load();return()=>{alive=false;clearTimeout(timer);};},[path,interval,version]);
  return [data,error,()=>setVersion(x=>x+1)];
}
function Status({value}) {return <span className={'status '+value}>{label(value)}</span>;}
function Empty({children}) {return <div className="empty">{children}</div>;}
function Pager({data,page,param='page'}) {
  return <div className="pager"><span>{data.total} resultados · 20 por página</span><RouteLink className="button-link" disabled={page<=1} href={routeHref({[param]:page-1})}>Anterior</RouteLink><span>{page} / {Math.max(1,Math.ceil(data.total/20))}</span><RouteLink className="button-link" disabled={page*20>=data.total} href={routeHref({[param]:page+1})}>Siguiente</RouteLink></div>;
}
function TaskProgress({activity}) {
  const percent=activity.progress??(activity.status==='done'?100:0);
  return <div className={'task-progress '+activity.status}><div className="progress-label"><span>{label(activity.stage)}</span><span>{percent}%</span></div><div className="progress-track" role="progressbar" aria-label={'Progreso de '+activity.url} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} aria-valuetext={`${label(activity.stage)}: ${percent}% por etapas`} title="Progreso estimado por etapas"><div className="progress-fill" style={{width:percent+'%'}}/></div><small>Intento {activity.attempt||'pendiente'}</small></div>;
}
function ActivityTable({items,onOpen,onRetry,retrying}) {return items.length?<table><thead><tr><th>Página</th><th>Estado</th><th>Tarea / intento</th><th>Inicio</th><th/></tr></thead><tbody>{items.map(a=><tr key={a.id}><td className="url"><RouteLink className="text-link" href={routeHref({activity:a.id},true)}>{a.url}</RouteLink><small>{a.domain}</small>{a.error&&<small className="failure-reason">{a.error}</small>}</td><td><Status value={a.status}/></td><td><TaskProgress activity={a}/></td><td className="nowrap">{date(a.started||a.created)}</td><td><div className="inline"><RouteLink className="button-link" href={routeHref({activity:a.id},true)}>Ver actividad</RouteLink>{a.status==='error'&&onRetry&&<button disabled={retrying===a.id} onClick={()=>onRetry(a.id)}>{retrying===a.id?'Encolando…':'Reintentar'}</button>}</div></td></tr>)}</tbody></table>:<Empty>No hay actividades en esta vista.</Empty>;}
function ChartPopup({item,series}) {
  if(!item)return null;
  return <div className="chart-popup" role="tooltip"><strong>{new Date(item.label).toLocaleString('es-UY',{timeZone:'America/Montevideo',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false})}</strong>{series.map(([key,name,color])=><div key={key}><i style={{background:color}}/><span>{name}</span><b>{item[key]||0}</b></div>)}</div>;
}
function Chart({title,items,daily=false}) {
  const [active,setActive]=useState(null),max=Math.max(1,...items.map(x=>x.count));
  return <section className="chart"><h3>{title}</h3><div className="chart-plot" onMouseLeave={()=>setActive(null)}><ChartPopup item={items[active]} series={[["count","Extraídas","#337f9b"]]}/><div className="bars">{items.map((item,i)=><div className="bar-column" key={item.label} tabIndex={0} aria-label={`${date(item.label)}: ${item.count} extracciones`} onMouseEnter={()=>setActive(i)} onFocus={()=>setActive(i)} onBlur={()=>setActive(null)} onKeyDown={e=>{if(e.key==='Escape')setActive(null);}}><span>{item.count||''}</span><div className="bar" style={{height:Math.max(2,item.count/max*100)+'px'}}/><small>{daily?new Date(item.label).toLocaleDateString('es-UY',{weekday:'short',timeZone:'America/Montevideo'}):i%3===0?new Date(item.label).getHours()+'h':''}</small></div>)}</div></div></section>;
}
function HourlyChart({items}) {
  const [active,setActive]=useState(null);
  const series=[['explored','Exploradas','#336eaf'],['extracted','Extraídas','#16816b'],['errors','Errores','#bf4953']];
  const max=Math.max(2,Math.ceil(Math.max(0,...items.flatMap(item=>series.map(([key])=>item[key]||0)))/2)*2);
  const x=i=>44+i*624/Math.max(1,items.length-1),y=value=>190-value/max*160;
  const curve=key=>items.map((item,i)=>{const px=x(i),py=y(item[key]||0);if(!i)return `M ${px} ${py}`;const mid=(x(i-1)+px)/2;return `C ${mid} ${y(items[i-1][key]||0)}, ${mid} ${py}, ${px} ${py}`;}).join(' ');
  const hour=value=>new Date(value).toLocaleTimeString('es-UY',{hour:'2-digit',minute:'2-digit',timeZone:'America/Montevideo'});
  return <section className="chart hourly-chart"><h3>Extracciones por hora · últimas 24 horas</h3><div className="chart-legend">{series.map(([key,name,color])=><span key={key} title={key==='explored'?'Intentos iniciados, incluidos reintentos':key==='extracted'?'Extracciones finalizadas correctamente':'Fallos registrados, incluidos reintentos'}><i style={{background:color}}/>{name}<strong>{items.reduce((sum,item)=>sum+(item[key]||0),0)}</strong></span>)}</div><div className="chart-plot" onMouseLeave={()=>setActive(null)}><ChartPopup item={items[active]} series={series}/><svg viewBox="0 0 690 226" aria-label="Exploradas, Extraídas y Errores por hora"><title>Actividad por hora, zona horaria de Montevideo</title>{[0,.5,1].map(ratio=><g key={ratio}><line x1="44" x2="668" y1={y(max*ratio)} y2={y(max*ratio)} stroke="#dce6ec"/><text x="35" y={y(max*ratio)+4} textAnchor="end">{Math.round(max*ratio)}</text></g>)}{items.map((item,i)=>i%4===0||i===items.length-1?<text key={item.label} x={x(i)} y="215" textAnchor="middle">{hour(item.label)}</text>:null)}{series.map(([key,name,color],index)=><g key={key} className="chart-series" data-series={key}><path d={curve(key)} fill="none" stroke={color} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></g>)}{items.map((item,i)=><rect key={item.label} tabIndex={0} aria-label={hour(item.label)} onMouseEnter={()=>setActive(i)} onFocus={()=>setActive(i)} onBlur={()=>setActive(null)} onKeyDown={e=>{if(e.key==='Escape')setActive(null);}} x={x(i)-13.6} y="25" width="27.2" height="170" fill="transparent"/>)}</svg></div></section>;
}
function Monitor({data}) {
  const active=data.workers.filter(x=>x.status==='running').length;
  return <><div className="overview"><div><span>Extracciones finalizadas</span><strong>{data.counts.done||0}</strong></div><div><span>Threads activos</span><strong>{active}<em> / {data.settings.threads}</em></strong></div><div><span>En cola / reintento</span><strong>{(data.counts.queued||0)+(data.counts.retry_wait||0)}</strong></div><div><span>Errores</span><strong>{data.counts.error||0}</strong></div><div><span>Dominios investigados</span><strong>{data.domains}</strong></div></div>
    <div className="chart-grid"><HourlyChart items={data.hourly}/><Chart title="Extracciones por día · esta semana" items={data.daily} daily/></div>
    <div className="resource-strip"><strong>Recursos del motor</strong><span>Memoria: {data.daemon.resources?.memory_mb??'—'} / {data.settings.memory_mb} MB</span><span>CPU máxima: {data.settings.cpu_percent}% <small>(100% = 1 núcleo)</small></span></div></>;
}
function SlotTable({slots,onOpen}) {
  return <table className="slot-table"><thead><tr><th>Slot</th><th>Estado</th><th>Página</th><th>Tarea actual</th><th>Actividad</th></tr></thead><tbody>{[...slots].sort((a,b)=>Number(!!b.activity)-Number(!!a.activity)||a.slot-b.slot).map(({slot,status,activity:a})=><tr key={slot} className={a?'':'idle-slot'}><td><strong>#{String(slot).padStart(2,'0')}</strong></td><td><Status value={status}/></td><td className="url">{a?<RouteLink className="text-link" href={routeHref({activity:a.id},true)}>{a.url}</RouteLink>:<span>Sin actividad asignada</span>}</td><td>{a?<><TaskProgress activity={a}/></>:'—'}</td><td>{a?<RouteLink className="button-link" href={routeHref({activity:a.id},true)}>Ver actividad</RouteLink>:'—'}</td></tr>)}</tbody></table>;
}
function LiveActivities({onOpen}) {
  const [data,error]=useData('/dashboard',2000);
  return <>{data&&<div className="overview activity-counters">{[["pages_per_hour","Páginas por hora"],["queued","Encoladas"],["running","En curso"],["errors_per_hour","Errores por hora"]].map(([key,title])=><div key={key}><span>{title}</span><strong>{data.activity_counters?.[key]??0}</strong></div>)}<div><span>RAM</span><strong>{data.daemon.resources?.memory_mb==null?'—':Math.round(data.daemon.resources.memory_mb).toLocaleString('es-UY')}<em> / {data.settings.memory_mb.toLocaleString('es-UY')} MB</em></strong></div><div><span title="100 % equivale a un núcleo">CPU</span><strong>{data.daemon.resources?.cpu_used_percent==null?'—':Math.round(data.daemon.resources.cpu_used_percent).toLocaleString('es-UY')}<em> / {data.settings.cpu_percent.toLocaleString('es-UY')} %</em></strong></div></div>}<section className="panel"><div className="section-heading"><div><h2>Actividad por slot</h2></div>{data&&<span>{data.slots.filter(s=>s.activity).length} ocupados / {data.slots.length} slots</span>}</div>{error&&<p role="alert">{error}</p>}{data&&<SlotTable slots={data.slots} onOpen={onOpen}/>}</section></>;
}
function History({onOpen,failedOnly=false}) {
  const [page,setPage]=useUrlState('page',1),[status,setStatus]=useUrlState('status',failedOnly?'error':''),[retrying,setRetrying]=useState(''),[retryError,setRetryError]=useState('');
  async function retry(id){setRetrying(id);setRetryError('');try{const result=await api(`/activities/${id}/retry`,{});onOpen(result.activity_id);}catch(e){setRetryError(e.message);}finally{setRetrying('');}}
  const [data,error]=useData(`/activities?page=${page}&status=${status}`,2000);
  return <section className="panel"><div className="section-heading"><h2>{failedOnly?'Actividades fallidas':'Todas las actividades'}</h2>{!failedOnly&&<label>Estado<select value={status} onChange={e=>goTo(routeHref({status:e.target.value,page:1}))}><option value="">Todos</option>{['queued','running','paused','retry_wait','done','error','excluded'].map(s=><option key={s} value={s}>{label(s)}</option>)}</select></label>}</div>{error&&<p role="alert">{error}</p>}{data&&<div className="page-transition" key={data.page}><ActivityTable items={data.items} onOpen={onOpen} onRetry={retry} retrying={retrying}/>{retryError&&<p className="error-banner" role="alert">{retryError}</p>}<Pager data={data} page={page} setPage={setPage}/></div>}</section>;
}
function SettingsForm({initial,onSaved}) {
  const [form,setForm]=useState(initial),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
  async function save(e){e.preventDefault();setBusy(true);try{await api('/settings',form,'PUT');setMessage('Configuración guardada. Se aplicará también al motor activo.');onSaved();}catch(e){setMessage(e.message);}finally{setBusy(false);}}
  return <form className="panel form-panel" onSubmit={save}><h2>Funcionamiento del motor</h2><label className="checkbox"><input type="checkbox" checked={form.enabled} onChange={e=>setForm({...form,enabled:e.target.checked})}/>Scraping activado</label><div className="form-grid">{[['threads','Threads simultáneos',1,50],['attempts','Intentos por actividad',1,10],['freshness_hours','Vigencia de la extracción (horas)',1,8760],['retry_delay','Tiempo entre reintentos (segundos)',1,86400],['memory_mb','Umbral global para iniciar (MB)',256,262144],['activity_memory_mb','Umbral de memoria por actividad (MB)',128,262144],['cpu_percent','CPU máxima (%) · 100% = 1 núcleo',1,10000],['suspicious_pages','Umbral de URL por dominio',2,1000000]].map(([key,title,min,max])=><label key={key}>{title}<input type="number" min={min} max={max} required value={form[key]} onChange={e=>setForm({...form,[key]:Number(e.target.value)})}/></label>)}<p>Extractor semántico · procesamiento local sin llamadas a modelos.</p></div><AnimatedDetails><summary>Memoria y reintentos</summary><div className="detail-body">El umbral global pospone tareas nuevas. El límite por actividad interrumpe solo esa actividad. Los reintentos automáticos requieren que no haya contenido emitido ni interacciones iniciadas.</div></AnimatedDetails><button className="primary" disabled={busy}>{busy?'Guardando…':'Guardar configuración'}</button><p role="status">{message}</p></form>;
}
function RuleList({kind,title,items,onChange,draft,setDraft,onEditing}) {
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[editing,setEditing]=useState(null);
  function edit(index){setEditing(index);onEditing(true);setDraft(items[index]);setError('');}
  function cancel(){setEditing(null);onEditing(false);setDraft('');setError('');}
  async function add(){
    if(busy)return;
    setBusy(true);setError('');
    try{
      const result=await api('/rules/validate',{kind,value:draft});
      if(items.some((item,index)=>index!==editing&&item===result.value))throw new Error('Este elemento ya está en la lista.');
      onChange(editing===null?[...items,result.value]:items.map((item,index)=>index===editing?result.value:item));cancel();
    }catch(e){setError(e.message);}finally{setBusy(false);}
  }
  const field=(isEdit)=><input autoFocus={isEdit} aria-label={(isEdit?'Editar en ':'Agregar a ')+title.toLowerCase()} aria-invalid={!!error} aria-describedby={'error-'+kind} disabled={busy||(!isEdit&&items.length>=100)} value={draft} maxLength={2048} placeholder={kind==='domain'?'ejemplo.com':'https://ejemplo[.]com/productos/.*'} onChange={e=>{setDraft(e.target.value);setError('');}} onKeyDown={e=>{if(e.key==='Enter'){e.preventDefault();add();}if(isEdit&&e.key==='Escape'){e.preventDefault();cancel();}}}/>;
  return <fieldset className="rule-list"><legend>{title}</legend><ul>{items.map((item,i)=><li key={item+i}>{editing===i?<>{field(true)}<button type="button" disabled={busy||!draft.trim()} onClick={add}>{busy?'Validando…':'Aplicar cambio'}</button><button type="button" disabled={busy} onClick={cancel}>Cancelar edición</button></>:<><code>{item}</code><span className="rule-valid">Válido</span><button type="button" disabled={busy||editing!==null} aria-label={'Editar '+item} onClick={()=>edit(i)}>Editar</button><button type="button" disabled={busy||editing!==null} aria-label={'Quitar '+item} onClick={()=>onChange(items.filter((_,index)=>index!==i))}>Quitar</button></>}</li>)}</ul>{!items.length&&<p className="muted">Todavía no hay elementos.</p>}{editing===null&&<div className="inline">{field(false)}<button type="button" disabled={busy||!draft.trim()||items.length>=100} onClick={add}>{busy?'Validando…':'Agregar'}</button></div>}{error&&<p id={'error-'+kind} className="rule-error" role="alert">{error}</p>}<small>{kind==='domain'?'Sin protocolo ni ruta. Incluye sus subdominios.':'Sin delimitadores; las / no necesitan escape.'}</small></fieldset>;
}
function RegexTester({patterns,seedId,initialUrl}) {
  const [pattern,setPattern]=useState(patterns[0]||''),[urls,setUrls]=useState(initialUrl||''),[result,setResult]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function known(){try{const values=await api(`/seeds/${seedId}/urls`);setUrls([...new Set([initialUrl,...values].filter(Boolean))].join('\n'));setResult(null);}catch(e){setError(e.message);}}
  async function test(){setBusy(true);setError('');setResult(null);try{const data=await api('/rules/test',{pattern,urls:urls.split('\n').map(s=>s.trim()).filter(Boolean)});setResult(data.results);}catch(e){setError(e.message);}finally{setBusy(false);}}
  return <section className="regex-tester"><h3>Probar una expresión regular</h3>{patterns.length>0&&<label>Usar una regex de la lista<select value="" onChange={e=>{setPattern(e.target.value);setResult(null);}}><option value="">Seleccionar patrón…</option>{patterns.map(p=><option key={p} value={p}>{p}</option>)}</select></label>}<label>Patrón a probar<input value={pattern} onChange={e=>{setPattern(e.target.value);setResult(null);}} placeholder="https://ejemplo[.]com/productos/.*"/></label><label>URL de prueba · una por línea<textarea aria-label="URL de prueba · una por línea" rows={6} value={urls} onChange={e=>{setUrls(e.target.value);setResult(null);}}/></label><div className="inline"><button type="button" disabled={busy||!pattern||!urls.trim()} onClick={test}>{busy?'Evaluando…':'Probar regex'}</button>{seedId&&<button type="button" onClick={known}>Cargar URL conocidas</button>}</div>{error&&<p role="alert" className="rule-error">{error}</p>}{result&&<><p role="status">{result.filter(r=>r.matched).length} coinciden · {result.filter(r=>!r.matched&&!r.error).length} no coinciden · {result.filter(r=>r.error).length} URL inválidas</p><table><thead><tr><th>URL</th><th>Resultado</th></tr></thead><tbody>{result.map((row,i)=><tr key={i}><td className="url">{row.url}{row.normalized!==row.url&&row.normalized&&<small>Evaluada: {row.normalized}</small>}</td><td><span className={'status '+(row.error?'error':row.matched?'done':'queued')}>{row.error?'URL inválida':row.matched?'Coincide':'No coincide'}</span>{row.error&&<small>{row.error}</small>}</td></tr>)}</tbody></table></>}</section>;
}
function SeedEditor({seed,onBack,onSaved,listHref}) {
  const [form,setForm]=useState({...seed,rule_mode:seed.rule_mode||(seed.allowed_domains.length||seed.allowed_url_patterns.length?'white':'black')}),[message,setMessage]=useState(''),[busy,setBusy]=useState(false),[drafts,setDrafts]=useState({}),[editingRules,setEditingRules]=useState({});
  const rules=Object.keys(ruleNames),prefix=form.rule_mode==='white'?'allowed':'blocked',activeKeys=['allowed_domains',prefix+'_url_patterns'];
  const pending=activeKeys.some(key=>drafts[key]?.trim()||editingRules[key]);
  async function save(e){e.preventDefault();if(pending)return;setBusy(true);try{
    const body={url:form.url,enabled:form.enabled,max_urls_per_minute:form.max_urls_per_minute??null,max_pages:form.max_pages??null,rule_mode:form.rule_mode,...Object.fromEntries(rules.map(key=>[key,activeKeys.includes(key)?form[key]:[]]))};
    await api('/seeds'+(seed.id?'/'+seed.id:''),body,seed.id?'PUT':'POST');onSaved();
  }catch(e){setMessage(e.message);}finally{setBusy(false);}}
  return <div className="seed-editor section-transition"><div className="editor-heading"><div><h2>{seed.id?'Editar URL inicial':'Nueva URL inicial'}</h2><p>{seed.id?seed.url:'Configura el origen y sus reglas de exploración.'}</p></div><RouteLink className="button-link" href={listHref}>Volver al listado</RouteLink></div><form className="panel form-panel" onSubmit={save}><label>URL<input type="url" required value={form.url} placeholder="https://ejemplo.com/" onChange={e=>setForm({...form,url:e.target.value})}/></label><label className="checkbox"><input type="checkbox" checked={form.enabled} onChange={e=>setForm({...form,enabled:e.target.checked})}/>Exploración activa</label><label>Límite de páginas (opcional)<input type="number" min="1" step="1" value={form.max_pages??''} placeholder="Sin límite" onChange={e=>setForm({...form,max_pages:e.target.value===''?null:Number(e.target.value)})}/><small>Páginas distintas, incluida la URL inicial. Se cuentan al iniciar su extracción, aunque falle. Los reintentos no suman otra página. Vacío: sin límite.</small></label><label>Máximo de URL por minuto<input type="number" min="1" max="60000" step="1" value={form.max_urls_per_minute??''} placeholder="Sin límite" onChange={e=>setForm({...form,max_urls_per_minute:e.target.value===''?null:Number(e.target.value)})}/><small>Incluye reintentos. Vacío: sin límite.</small></label><fieldset className="rule-mode"><legend>Modo de exploración</legend>{[['white','Lista blanca'],['black','Lista negra']].map(([value,title])=><label className="checkbox" key={value}><input type="radio" name="rule-mode" value={value} checked={form.rule_mode===value} onChange={()=>{setForm({...form,rule_mode:value});setDrafts({});setEditingRules({});}}/>{title}</label>)}</fieldset><p>{form.rule_mode==='white'?'Si defines dominios y regex, la URL debe coincidir con un dominio y una regex. Si defines solo un grupo, basta una coincidencia de ese grupo. La URL inicial es el punto de entrada explícito.':'Se exploran los dominios indicados, excluyendo las URL que coincidan con alguna regex de la lista negra.'} Una URL admitida por otro origen también puede extraerse.</p>{activeKeys.map((key,i)=><RuleList key={key} kind={i===0?'domain':'regex'} title={i===0?'Dominios':'Expresiones regulares'} items={form[key]} onChange={items=>setForm(f=>({...f,[key]:items}))} draft={drafts[key]||''} setDraft={value=>setDrafts(d=>({...d,[key]:value}))} onEditing={value=>setEditingRules(current=>({...current,[key]:value}))}/>)}<RegexTester patterns={form[prefix+'_url_patterns']} seedId={seed.id} initialUrl={form.url}/>{pending&&<p role="status">Aplica o cancela la edición, o agrega o borra el elemento pendiente antes de guardar.</p>}<div className="inline"><button className="primary" disabled={busy||pending}>{busy?'Guardando…':'Guardar URL inicial'}</button><RouteLink className="button-link" href={listHref}>Cancelar</RouteLink></div><p role="alert">{message}</p></form></div>;
}
function StartSeed({seed,onStarted}) {
  const [busy,setBusy]=useState(''),[error,setError]=useState('');
  async function action(kind){setBusy(kind);setError('');try{const result=await api(`/seeds/${seed.id}/${kind}`,{});onStarted(kind==='pause'?'Origen pausado.':result.engine_enabled?'Exploración solicitada.':'Exploración encolada. Activa el scraping global para ejecutarla.');}catch(e){setError(e.message);}finally{setBusy('');}}
  return <div><div className="inline"><button className="seed-toggle" type="button" aria-busy={!!busy} disabled={!!busy} onClick={()=>action(seed.enabled?'pause':'start')} aria-label={(seed.enabled?'Pausar':'Iniciar')+' exploración de '+seed.url} title={seed.enabled?'Pausar este origen':'Iniciar exploración'}>{seed.enabled?'⏸ Pausar':'▶ Iniciar'}</button></div>{error&&<p role="alert">{error}</p>}</div>;
}
function ClearSeedHistory({seed,onCleared,compact=false}) {
  const [confirm,setConfirm]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const dialog=useRef(null),closing=useRef(false);
  async function closeModal(after){if(closing.current)return;closing.current=true;const node=dialog.current;if(node&&!window.matchMedia('(prefers-reduced-motion: reduce)').matches){await node.animate([{opacity:1,transform:'translateY(0) scale(1)'},{opacity:0,transform:'translateY(8px) scale(.98)'}],{duration:160,easing:'ease-in',fill:'forwards'}).finished.catch(()=>{});}setConfirm(false);closing.current=false;after?.();}
  useEffect(()=>{if(confirm){dialog.current?.getAnimations().forEach(a=>a.cancel());dialog.current?.showModal();}else dialog.current?.close();},[confirm]);
  async function clear(){setBusy(true);setError('');try{const result=await api(`/seeds/${seed.id}/history`,{},'DELETE');if(result.file_errors?.length){setError('El historial se eliminó, pero algunos archivos no pudieron borrarse. Vuelve a intentarlo.');}else{await closeModal(onCleared);}}catch(e){setError(e.message);}finally{setBusy(false);}}
  return <section className={compact?'history-delete':'panel'}>{!compact&&<h3>Historial de este origen</h3>}<button type="button" aria-label={'Eliminar historial de '+seed.url} title="Eliminar historial" onClick={()=>{setError('');setConfirm(true);}}>{compact?<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></svg>:'Eliminar historial'}</button><dialog ref={dialog} className="confirm-dialog" aria-label="Eliminar historial de exploración" onCancel={e=>{e.preventDefault();if(!busy)closeModal();}} onClick={e=>{if(e.target!==dialog.current||busy)return;const r=dialog.current.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeModal();}}><h2>Eliminar historial de exploración</h2><p className="dialog-url">{seed.url}</p><p>Se cancelarán las tareas activas y se borrarán las actividades, resultados y capturas exclusivos de este origen. Los compartidos con otros orígenes se conservan.</p><p>La configuración se conserva. Después podrás pulsar <strong>Iniciar</strong> para explorar nuevamente.</p>{error&&<p role="alert" className="error-banner">{error}</p>}<div className="dialog-actions"><button type="button" autoFocus disabled={busy} onClick={()=>closeModal()}>Cancelar</button><button className="danger-button" type="button" disabled={busy} onClick={clear}>{busy?'Eliminando…':'Confirmar eliminación del historial'}</button></div></dialog></section>;
}
function Seeds({onOpen}) {
  const [page,setPage]=useUrlState('page',1),[editId]=useUrlState('edit',''),[originId]=useUrlState('origin',''),[message,setMessage]=useState('');
  const selected=editId||originId;
  const [data,error,reload]=useData(selected&&selected!=='new'?'/seeds/'+selected:'/seeds?page='+page,3000);
  const editing=editId==='new'?emptySeed:editId&&data?.id===editId?data:null,viewing=originId&&data?.id===originId?data:null;
  const listHref=routeHref({edit:null,origin:null,items_page:null,q:null});
  if(selected&&!editing&&!viewing)return <Empty>{error||'Cargando origen…'}</Empty>;
  if(viewing)return <div className="section-transition"><div className="editor-heading origin-heading"><div><h2>Páginas alcanzadas</h2><p className="url"><ExternalLink url={viewing.url}/></p></div><div className="origin-actions"><StartSeed seed={viewing} onStarted={message=>{setMessage(message);reload();}}/><ClearSeedHistory compact seed={viewing} onCleared={()=>{goTo(listHref);setMessage('Historial eliminado. Pulsa Iniciar para explorar nuevamente.');reload();}}/><RouteLink className="button-link" href={listHref}>Volver al listado</RouteLink></div></div>{message&&<p role="status">{message}</p>}<DomainPages seedId={viewing.id} domain="" title="Páginas" onOpen={onOpen}/></div>;
  if(editing)return <SeedEditor key={editing.id||'new'} seed={editing} onBack={()=>goTo(listHref)} listHref={listHref} onSaved={()=>{goTo(listHref);setMessage('URL inicial guardada.');reload();}}/>;
  return <section className="panel seed-list section-transition"><div className="section-heading"><div><h2>Orígenes de exploración</h2></div><RouteLink className="button-link primary" href={routeHref({edit:'new',origin:null})}>Nueva URL inicial</RouteLink></div>{error&&<p role="alert">{error}</p>}{message&&<p role="status" className="list-message">{message}</p>}{data?.items&&<div className="page-transition" key={data.page}><table><thead><tr><th>URL inicial</th><th>Estado</th><th>Páginas alcanzadas</th><th>Reglas</th><th>Acciones</th></tr></thead><tbody>{data.items.map(seed=><tr key={seed.id}><td className="url"><div className="inline"><RouteLink className="text-link" href={routeHref({edit:seed.id,origin:null})}>{seed.url}</RouteLink><ExternalLink url={seed.url} aria-label={'Abrir sitio externo '+seed.url} title="Abrir sitio en otra pestaña"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M14 3h7v7M21 3 10 14M10 3H3v18h18v-7"/></svg></ExternalLink></div></td><td><Status value={seed.status}/><small>{seed.activity_counts?.done||0} finalizadas · {seed.activity_counts?.error||0} con error</small></td><td><RouteLink className="text-link" href={routeHref({origin:seed.id,edit:null,items_page:1,q:null})}>{seed.page_count}</RouteLink></td><td>{Object.keys(ruleNames).reduce((sum,key)=>sum+seed[key].length,0)}</td><td><div className="inline"><RouteLink className="button-link edit-pencil" aria-label={'Editar '+seed.url} title="Editar URL inicial" href={routeHref({edit:seed.id,origin:null})}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="m16 3 5 5L8 21H3v-5Z M13 6l5 5"/></svg></RouteLink><RouteLink className="button-link edit-pencil" aria-label={'Ver páginas de '+seed.url} title="Ver páginas alcanzadas" href={routeHref({origin:seed.id,edit:null,items_page:1,q:null})}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></RouteLink><StartSeed seed={seed} onStarted={message=>{setMessage(message);reload();}}/><ClearSeedHistory compact seed={seed} onCleared={()=>{setMessage('Historial eliminado. Pulsa Iniciar para explorar nuevamente.');reload();}}/></div></td></tr>)}</tbody></table>{!data.items.length&&<Empty>Agrega una URL inicial para configurar la exploración.</Empty>}<Pager data={data} page={page} setPage={setPage}/></div>}</section>;
}
function DomainPages({domain,onOpen,seedId,title}) {
  const [status]=useUrlState('page_status',''),[search]=useUrlState('q',''),[page]=useUrlState('items_page',1),[message,setMessage]=useState(''),[query,setQuery]=useState(search),[requesting,setRequesting]=useState([]);
  useEffect(()=>setQuery(search),[search]);
  useEffect(()=>{if(query===search)return;const t=setTimeout(()=>goTo(routeHref({q:query,items_page:1})),350);return()=>clearTimeout(t);},[query,search]);
  const [data,error,reload]=useData(seedId?`/seeds/${seedId}/pages?query=${encodeURIComponent(search)}&page=${page}&status=${encodeURIComponent(status)}`:`/pages?domain=${encodeURIComponent(domain)}&query=${encodeURIComponent(search)}&page=${page}&status=${encodeURIComponent(status)}`,3000);
  async function rescrape(id){if(requesting.includes(id))return;setRequesting(current=>[...current,id]);setMessage('');try{await api(`/pages/${id}/rescrape`,{});reload();}catch(e){setMessage(e.message);}finally{setRequesting(current=>current.filter(value=>value!==id));}}
  return <section className="panel"><div className="section-heading pages-heading"><div><h2>{title||domain}</h2></div><label>Estado<select aria-label="Estado de las páginas" value={status} onChange={e=>goTo(routeHref({page_status:e.target.value,items_page:1}))}><option value="">Todos</option>{['queued','running','paused','retry_wait','done','error','excluded'].map(value=><option key={value} value={value}>{label(value)}</option>)}</select></label><label>Buscar URL<input type="search" placeholder="Búsqueda aproximada…" value={query} onChange={e=>setQuery(e.target.value)}/></label></div>{(error||message)&&<p role="alert">{error||message}</p>}{data&&<div className="page-transition" key={data.page}><table><thead><tr><th>URL descubierta</th><th>Última actividad</th><th/></tr></thead><tbody>{data.items.map(p=><tr key={p.id}><td className="url"><RouteLink className="text-link" href={routeHref({activity:p.activity_id},true)}>{p.url}</RouteLink></td><td><Status value={p.status}/></td><td><button disabled={requesting.includes(p.id)} onClick={()=>rescrape(p.id)}>{requesting.includes(p.id)?'Solicitando…':'Re-scrap'}</button></td></tr>)}</tbody></table>{!data.items.length&&<Empty>No hay páginas que coincidan con la búsqueda.</Empty>}<Pager data={data} page={page} param="items_page"/></div>}</section>;
}
function Domains({onOpen,suspects=false}) {
  const [data,error,reload]=useData('/domains',3000),[domain,setDomain]=useUrlState('domain',''),[message,setMessage]=useState(''),[limits,setLimits]=useState({});
  async function action(d,resume){try{await api('/domains/action',{domain:d.name,approved_limit:resume?Number(limits[d.name]||Math.max(d.pages+100,d.pages*2)):null});setMessage(resume?'Dominio reanudado.':'Dominio pausado.');reload();}catch(e){setMessage(e.message);}}
  const items=(data||[]).filter(d=>!suspects||d.suspicious);
  return <><section className="panel"><div className="section-heading"><div><h2>{suspects?'Revisión de dominios pausados':'Dominios descubiertos'}</h2></div></div>{(error||message)&&<p role="status">{error||message}</p>}{items.length?<table><thead><tr><th>Dominio</th><th>URL encontradas</th><th>Estado</th><th>Control</th></tr></thead><tbody>{items.map(d=><tr key={d.name}><td><RouteLink className="text-link" href={routeHref({domain:d.name,items_page:1,q:null})}>{d.name}</RouteLink>{d.reason&&<small>{d.reason}</small>}</td><td>{d.pages}</td><td><Status value={d.suspicious?'paused':'running'}/></td><td>{d.suspicious?<div className="inline"><input aria-label={'Nuevo límite para '+d.name} type="number" min={d.pages+1} value={limits[d.name]||Math.max(d.pages+100,d.pages*2)} onChange={e=>setLimits({...limits,[d.name]:e.target.value})}/><button onClick={()=>action(d,true)}>Aprobar y reanudar</button></div>:<button onClick={()=>action(d,false)}>Pausar dominio</button>}</td></tr>)}</tbody></table>:<Empty>{suspects?'No hay dominios pendientes de revisión.':'Los dominios aparecerán al agregar URL iniciales.'}</Empty>}</section>{domain&&<DomainPages key={domain} domain={domain} onOpen={onOpen}/>}</>;
}
function AnimatedDetails({children,open=false}) {
  const node=useRef(null),animation=useRef(null),target=useRef(open);
  const [summary,...body]=Children.toArray(children);
  useEffect(()=>()=>animation.current?.cancel(),[]);
  function toggle(event){
    event.preventDefault();
    const el=node.current,start=el.getBoundingClientRect().height;
    target.current=!target.current;
    const expanding=target.current;
    if(animation.current){animation.current.onfinish=null;animation.current.cancel();}
    if(window.matchMedia('(prefers-reduced-motion: reduce)').matches){el.open=expanding;el.style.height='';el.style.overflow='';return;}
    el.open=true;
    el.style.height='auto';
    const end=expanding?el.getBoundingClientRect().height:el.querySelector('summary').getBoundingClientRect().height+parseFloat(getComputedStyle(el).borderTopWidth||0)+parseFloat(getComputedStyle(el).borderBottomWidth||0);
    el.style.height=start+'px';el.style.overflow='hidden';
    const current=el.animate([{height:start+'px'},{height:end+'px'}],{duration:240,easing:'cubic-bezier(.2,0,0,1)',fill:'both'});
    animation.current=current;
    current.onfinish=()=>{el.open=expanding;el.style.height='';el.style.overflow='';current.cancel();animation.current=null;};
  }
  return <details className="animated-details" ref={node} open={open}><summary {...summary?.props} onClick={toggle}>{summary?.props.children}</summary><div className="accordion-body">{body}</div></details>;
}
const fieldLabels={name:'Nombre',brand:'Marca',description:'Descripción',price:'Precio',previous_price:'Precio anterior',availability:'Disponibilidad',rating:'Puntuación',rating_count:'Valoraciones',review_body:'Opinión',author:'Autor',date:'Fecha',ingredients:'Ingredientes',usage:'Modo de uso',size:'Tamaño',product_link:'Página',image:'Imagen',answer:'Respuesta',question:'Pregunta'};
const roleLabels={semantic_article:'Contenido',semantic_content:'Texto de la página',semantic_disclosure:'Contenido desplegable',semantic_table:'Tabla',editorial:'Artículo',qa:'Pregunta',metadata:'Metadatos',semantic_collection:'Colección',semantic_item:'Elemento del listado',categories:'Categorías',breadcrumb:'Ruta de navegación',site_menu:'Enlaces',reviews:'Reseña',product:'Producto',offer:'Oferta'};
const cleanText=value=>String(value??'').replace(/\s+/g,' ').trim();
function entityFields(entity){const result={};for(const part of entity.parts||[])for(const value of part.values||[]){const text=cleanText(value.text||value.value||value.href||value.src);if(text)(result[part.class]??=[]).push({...value,text});}return result;}
function externalUrl(value){try{const url=new URL(value);return ['http:','https:'].includes(url.protocol)?url.href:null;}catch{return null;}}
function ExternalLink({url,children,...props}){const href=externalUrl(url);return href?<a href={href} target="_blank" rel="noopener noreferrer" {...props}>{children||url}</a>:<span>{children||url}</span>;}
function ReadableValue({value}){const href=externalUrl(value.href);return href?<ExternalLink url={href}>{value.text}</ExternalLink>:<>{value.text}{value.currency?' '+value.currency:''}</>;}
function TextContent({text}){if(!text)return null;return text.length>600?<><p>{text.slice(0,450)}…</p><AnimatedDetails><summary>Leer contenido completo</summary><p className="full-text">{text}</p></AnimatedDetails></>:<p className="full-text">{text}</p>;}
function Evidence({entity}){
  const source=(entity.attributes?.declared&&entity.attributes?.dom_html?{declared:entity.attributes.declared,html:entity.attributes.dom_html}:entity.attributes?.declared)??{text:entity.text,attributes:entity.attributes,parts:entity.parts,links:entity.links};
  return <AnimatedDetails><summary>Ver fuente</summary><div className="source-evidence"><span>{entity.attributes?.source==='jsonld'?'Datos estructurados de la página':entity.attributes?.source==='head'?'Metadatos del documento':'Contenido del DOM'}</span>{entity.attributes?.disclosure_open===false&&<p>El contenido estaba en el DOM, dentro de un bloque cerrado.</p>}<code>{entity.selector}</code><pre>{JSON.stringify(source,null,2)}</pre></div></AnimatedDetails>;
}
function EntityLinks({links=[]}){
  const items=links.filter(link=>externalUrl(link.url));
  return items.length?<ul className="entity-links">{items.map((link,i)=><li key={link.url+i}><ExternalLink url={link.url}>{link.text||link.label||link.url}</ExternalLink></li>)}</ul>:null;
}
function ContentCard({entity}){
  const fields=entityFields(entity),title=fields.name?.[0]?.text||entity.heading||roleLabels[entity.role]||'Contenido capturado';
  let text=cleanText(fields.description?.map(v=>v.text).join(' ')||entity.text);
  if(text.startsWith(cleanText(title)))text=text.slice(cleanText(title).length).trim();
  const picture=fields.image?.find(v=>externalUrl(v.src||v.text)),price=fields.price||[];
  return <article className="content-card">{picture&&<img className="content-image" src={externalUrl(picture.src||picture.text)} alt={title} loading="lazy" referrerPolicy="no-referrer"/>}<h3>{title}</h3>{entity.attributes?.navigation_path?.length>1&&<p className="navigation-path">{entity.attributes.navigation_path.slice(0,-1).join(' › ')}</p>}{price.length>0&&<div className="captured-price">{price.map((v,i)=><span key={i}><ReadableValue value={v}/></span>)}</div>}{!price.length&&entity.attributes?.semantic_field_status?.price==='other'&&<p className="muted">Sin precio confirmado en esta captura.</p>}<TextContent text={text}/><dl>{Object.entries(fields).filter(([key])=>!['name','description','image','price'].includes(key)).map(([key,values])=><div className="captured-field" key={key}><dt>{fieldLabels[key]||key.replaceAll('_',' ')}</dt><dd>{values.map((v,i)=><span key={i}><ReadableValue value={v}/></span>)}</dd></div>)}</dl><EntityLinks links={entity.links}/><Evidence entity={entity}/></article>;
}
function QuestionCard({entity}){
  const fields=entityFields(entity),declared=entity.attributes?.declared||{};
  const question=fields.question?.[0]?.text||fields.name?.[0]?.text||declared.name||entity.heading||roleLabels[entity.role]||'Contenido desplegable';
  const declaredAnswers=[declared.acceptedAnswer,declared.suggestedAnswer].flat().filter(Boolean).map(x=>typeof x==='string'?x:x.text).filter(Boolean);
  const answer=fields.answer?.map(v=>v.text).join('\n\n')||declaredAnswers.join('\n\n')||entity.text;
  return <article className="question-card"><h3>{question}</h3>{answer?<TextContent text={answer}/>:<p className="muted">No se capturó una respuesta para esta pregunta.</p>}<Evidence entity={entity}/></article>;
}
function ContentGroup({items,questions=false}){
  const Card=questions?QuestionCard:ContentCard;
  return <><div className={questions?'question-list':'content-grid'}>{items.slice(0,6).map((entity,i)=><Card key={entity.id||i} entity={entity}/>)}</div>{items.length>6&&<AnimatedDetails><summary>Ver {items.length-6} resultados más</summary><div className={questions?'question-list':'content-grid'}>{items.slice(6).map((entity,i)=><Card key={entity.id||i} entity={entity}/>)}</div></AnimatedDetails>}</>;
}
const Results=memo(function Results({visits,sourceUrl}) {
  const [selected,setSelected]=useUrlState('visit','');
  const visit=visits.find(v=>v.id===selected)||visits.at(-1);
  if(!visit)return <Empty>La actividad todavía no guardó una captura. Las etapas aparecerán durante la ejecución.</Empty>;
  const entities=visit.result?.entities||[],used=new Set();
  const groups=[['Productos y listados',e=>/product|offer|attributes|semantic_item/.test(e.role)],['Contenido principal',e=>/article|editorial|review|table/.test(e.role)],['Preguntas y respuestas',e=>e.role==='qa'],['Contenido desplegable',e=>e.role==='semantic_disclosure'],['Texto completo de la página',e=>e.role==='semantic_content'],['Categorías y navegación',e=>['categories','site_menu','breadcrumb'].includes(e.role)],['Metadatos',e=>e.role==='metadata'],['Enlaces y otros datos',()=>true]].map(([name,filter])=>{
    let items=entities.filter((e,i)=>{if(used.has(i)||!filter(e))return false;used.add(i);return true;});
    if(name==='Preguntas y respuestas'){
      const unique=new Map();for(const e of items){const f=entityFields(e),key=cleanText(f.question?.[0]?.text||f.name?.[0]?.text||e.heading).toLowerCase()||e.id;const prior=unique.get(key);if(!prior||f.answer?.length)unique.set(key,e);}items=[...unique.values()];
    }
    return {name,items};
  }).filter(g=>g.items.length);
  const primary=groups.filter(g=>['Productos y listados','Contenido principal','Preguntas y respuestas'].includes(g.name));
  const coverage=visit.result?.classification?.coverage;
  const observed=coverage?.navigation_observation;
  return <div className="results"><div className="result-summary"><div><h2>{visit.title||'Resultados de la página'}</h2>{sourceUrl&&<p className="result-source-url"><ExternalLink url={sourceUrl}/></p>}<div className="result-verdict"><Status value={visit.status}/><span>{entities.length?`${entities.length} bloques capturados`:'Sin contenido capturado'}</span></div></div>{visit.screenshot_path&&<ExternalLink url={typeof window!=='undefined'?new URL(`/api/scrapes/${visit.run_id}/visits/${visit.id}/screenshot`,window.location.origin).href:''}>Ver captura</ExternalLink>}</div><div className="result-counts">{primary.map(g=><span key={g.name}><strong>{g.items.length}</strong> {g.name.toLowerCase()}</span>)}<span>Gasto: US${visit.costs?.amount_usd??'—'}{visit.costs&&!visit.costs.exact?' (parcial)':''}</span></div>{visits.length>1&&<label>Captura / intento<select value={visit.id} onChange={e=>setSelected(e.target.value)}>{visits.map((v,i)=><option value={v.id} key={v.id}>{i+1}. {v.kind==='expansion'?'Expansión':'Página'} · {label(v.status)} · {date(v.started_at)}</option>)}</select></label>}{observed&&<p className="navigation-coverage note">{observed.supported?`Menús: ${observed.attempted} controles explorados · ${observed.observed} estados nuevos. ${observed.complete?'Se exploraron los controles visibles disponibles.':`Cobertura parcial: ${observed.failed||0} fallos y ${observed.omitted_controls||0} controles pendientes.`}`:'Entrada estática: no se pudo explorar el comportamiento de los menús.'}</p>}{coverage?.field_interpretation?.errors?.length>0&&<p className="note">No se pudieron interpretar algunos campos. Se conserva el contenido recibido y su evidencia.</p>}{coverage?.omitted_entities>0&&<p className="note">Se omitieron {coverage.omitted_entities} bloques por el límite de captura.</p>}{groups.map((group,i)=><AnimatedDetails key={visit.id+group.name} open={primary.length?primary.includes(group):i===0}><summary>{group.name}<span>{group.items.length}</span></summary><div className="detail-body"><ContentGroup items={group.items} questions={['Preguntas y respuestas','Contenido desplegable'].includes(group.name)}/></div></AnimatedDetails>)}{!entities.length&&<Empty>{visit.status==='running'?'Esperando contenido capturado…':'No se encontró contenido estructurado en esta captura.'}</Empty>}{visit.result?.actions?.some(action=>action.kind==='navigation_observation')&&<AnimatedDetails><summary>Exploración de menús y evidencia</summary><div className="detail-body">{visit.result.actions.map((action,i)=><div className="menu-observation" key={i}><strong>{action.control?.label||action.control?.selector}</strong><p>Hover · {action.status} · {action.added_links?.length||0} enlaces nuevos</p>{action.error&&<p>{action.error}</p>}{action.evidence&&<div className="inline">{['before','after'].filter(phase=>action.evidence[phase]).map(phase=><a key={phase} target="_blank" rel="noopener noreferrer" href={`/api/scrapes/${visit.run_id}/visits/${visit.id}/actions/${i}/${phase}/html`}>HTML {phase==='before'?'anterior':'posterior'}</a>)}</div>}</div>)}</div></AnimatedDetails>}<AnimatedDetails><summary>Auditoría y evidencia técnica</summary><div className="detail-body">{visit.html_path&&<a href={`/api/scrapes/${visit.run_id}/visits/${visit.id}/html`}>Descargar HTML</a>}<pre>{JSON.stringify({error:visit.error,expenses:visit.expenses,result:visit.result},null,2)}</pre></div></AnimatedDetails></div>;
},(previous,next)=>previous.sourceUrl===next.sourceUrl&&JSON.stringify(previous.visits)===JSON.stringify(next.visits));
function Extract() {
  const [url,setUrl]=useState(''),[submitting,setSubmitting]=useState(false),[state,setState]=useState({status:'idle',error:'',visit:null});
  const [jobId]=useUrlState('extract_job',''),[runId]=useUrlState('extract_run','');
  useEffect(()=>{
    if(!jobId&&!runId){setState({status:'idle',error:'',visit:null});return;}
    let alive=true,timer;
    setState({status:'running',error:'',visit:null});
    async function load(){
      try{
        let job=null;
        if(jobId){try{job=await api('/api/scrapes/jobs/'+jobId);}catch(e){if(!runId)throw e;}}
        const id=job?.run_id||runId;
        if(!alive)return;
        // Keep the saved result addressable after reload or an API restart.
        if(id&&id!==runId){goTo(routeHref({extract_run:id}));return;}
        let visit=null,run=null;
        if(id){
          const result=await api('/api/scrapes/'+id);
          run=result.run;
          const captured=result.visits.at(-1);
          if(captured)visit=await api(`/api/scrapes/${id}/visits/${captured.id}`);
        }
        if(!alive)return;
        const status=job?.status||(run?.finished_at?'done':'running');
        setState({status,error:status==='error'?(job?.message||'No se pudo completar la extracción.'):visit?.error||'',visit});
        if(status==='running')timer=setTimeout(load,1500);
      }catch(e){if(alive)setState(previous=>({...previous,status:'error',error:e.message}));}
    }
    load();
    return()=>{alive=false;clearTimeout(timer);};
  },[jobId,runId]);
  const busy=submitting||state.status==='running';
  async function start(event){
    event.preventDefault();if(busy)return;
    setSubmitting(true);setState({status:'idle',error:'',visit:null});
    try{
      const result=await api('/api/extract',{url:url.trim()});
      goTo(routeHref({view:'extract',extract_job:result.job_id},true));
    }catch(e){setState({status:'error',error:e.message,visit:null});}
    finally{setSubmitting(false);}
  }
  return <>
    <form className="panel form-panel" onSubmit={start}>
      <p>Analiza una página y extrae su contenido, metadatos y datos estructurados. Solo se visita la URL indicada, sin seguir enlaces ni ampliar listados.</p>
      <label>URL de la página<input type="url" required maxLength={2048} placeholder="https://ejemplo.com/pagina" value={url} onChange={e=>setUrl(e.target.value)}/></label>
      <div><button className="primary" disabled={busy}>{busy?'Extrayendo…':'Extraer página'}</button></div>
      {busy&&<p role="status">Analizando la página. Los resultados aparecerán aquí.</p>}
      {state.error&&<p className="error-banner" role="alert">{state.error}</p>}
      {state.status==='done'&&!state.error&&<p role="status">Extracción finalizada.</p>}
    </form>
    {state.visit&&<section className="panel activity-content"><Results visits={[state.visit]} sourceUrl={state.visit.final_uri||state.visit.requested_uri}/></section>}
  </>;
}
function RequestedScrape({id}) {
  const [data,error]=useData('/activities/'+id,2000);
  return <p role="status">Re-scrap solicitado. {data&&<Status value={data.status}/>} <RouteLink href={routeHref({activity:id},true)}>Ver actividad solicitada</RouteLink>{error&&<span role="alert">{error}</span>}</p>;
}
function Activity({id,onOpen}) {
  const [data,error,reload]=useData('/activities/'+id,2000),[message,setMessage]=useState(''),[requesting,setRequesting]=useState(false),[requested,setRequested]=useState('');
  async function rescrape(){if(requesting)return;setRequesting(true);setMessage('');try{const result=await api(data.status==='error'?`/activities/${data.id}/retry`:`/pages/${data.page_id}/rescrape`,{});setRequested(result.activity_id);reload();}catch(e){setMessage(e.message);}finally{setRequesting(false);}}
  return <>{error&&<p className="error-banner" role="alert">{error}</p>}{data&&<div className="activity-detail">
    <div className="activity-heading"><div><h1>Actividad de extracción</h1><p className="url activity-original-url"><ExternalLink url={data.url}/></p></div><button disabled={requesting} onClick={rescrape}>{requesting?'Solicitando…':data.status==='error'?'Reintentar':'Solicitar re-scrap'}</button></div>
    <section className="activity-overview panel"><div className="activity-summary-row"><div className="activity-meta"><Status value={data.status}/><span>{data.strategy==='semantic'?'Semántica · US$0':data.strategy==='jev'?'Jev':'Estrategia pendiente'}</span><span>Intento {data.attempt}</span></div><TaskProgress activity={data}/></div>{data.error&&<div className="activity-failure" role="alert">{data.error.replace(/^\w+Error: /,'').replace('; importa el HTML renderizado si abre en tu navegador.','.')}</div>}<div className="activity-dates"><span>Inicio <time>{date(data.started)}</time></span><span>Fin <time>{date(data.finished)}</time></span></div></section>
    {message&&<p className="error-banner" role="alert">{message}</p>}{requested&&<RequestedScrape key={requested} id={requested}/>}
    <section className="panel activity-content"><Results visits={data.visits}/></section>
    <section className="panel activity-support">
      <AnimatedDetails><summary>Procedencia del enlace<span>{data.referrers?.length||data.origins?.length||0}</span></summary><div className="detail-body"><div className="activity-origins"><strong>URL iniciales</strong>{data.origins?.length?data.origins.map(origin=><RouteLink key={origin.id} href={routeHref({view:'seeds',edit:origin.id},true)}>{origin.url}</RouteLink>):<span>Sin orígenes asociados</span>}</div>{data.referrers?.length>0?<section className="activity-referrers"><h2>Enlace encontrado en</h2><ul>{data.referrers.map(source=><li key={source.visit_id}><ExternalLink url={source.url}/>{source.activity_id&&<RouteLink className="text-link" href={routeHref({activity:source.activity_id,visit:source.visit_id},true)}>Ver actividad de origen</RouteLink>}</li>)}</ul></section>:<p className="muted">{data.origins?.some(origin=>origin.url===data.url)?'Punto de entrada de la exploración.':'Origen del enlace no registrado.'}</p>}</div></AnimatedDetails>
      {data.errors?.length>0&&<AnimatedDetails><summary>Errores de esta actividad<span>{data.errors.length}</span></summary><div className="detail-body"><table><thead><tr><th>Fecha</th><th>Error</th></tr></thead><tbody>{data.errors.map((e,i)=><tr key={i}><td>{date(e.at)}</td><td>{e.error}</td></tr>)}</tbody></table></div></AnimatedDetails>}
      <AnimatedDetails><summary>Etapas y tiempos<span>{data.stages.length}</span></summary><div className="detail-body timeline">{data.stages.filter((s,i,all)=>i===0||s.stage!==all[i-1].stage||s.detail).map(s=><div key={s.id}><strong>{label(s.stage)}</strong><time>{date(s.at)}</time>{s.detail&&<p>{s.detail}</p>}</div>)}</div></AnimatedDetails>
    </section>
  </div>}</>;

}
export default function ControlRoom() {
  const [section,setSection]=useState('dashboard'),[activity,setActivity]=useState(''),[message,setMessage]=useState('');
  const [data,error,reload]=useData('/dashboard',2000);
  useEffect(()=>{function sync(){const q=new URLSearchParams(location.search);setActivity(q.get('activity')||'');setSection(q.get('view')||'dashboard');}sync();window.addEventListener('popstate',sync);return()=>window.removeEventListener('popstate',sync);},[]);
  function navigate(view){goTo(routeHref({view},true));}
  function open(id){if(id)goTo(routeHref({activity:id},true));}
  async function toggle(){try{await api('/settings',{...data.settings,enabled:!data.settings.enabled},'PUT');reload();setMessage('');}catch(e){setMessage(e.message);}}
  return <><div className="shell"><aside className="sidebar"><div className="brand"><svg width="30" height="30" viewBox="0 0 30 30" aria-hidden="true"><path d="M5 5H25V12H12V25H5Z" fill="currentColor"/><path d="M17 17H25V25H17Z" fill="#7bacba"/></svg><span>Scrapper</span></div><nav>{Object.entries(sections).map(([key,title],i)=><RouteLink className={!activity&&section===key?'active':''} key={key} href={'?view='+key}><span className="nav-icon" aria-hidden="true">{['◉','↓','≡','◎','↗','⚙','!','⚑'][i]}</span>{title}</RouteLink>)}</nav><div className="sidebar-bottom"><span className={'dot '+(data?.daemon.online?'online':'')}/>{data?.daemon.online?'Motor conectado':'Motor sin conexión'}</div></aside><main><header className="topbar"><div><span>{activity?'Historial':sections[section]||'Análisis'}</span></div><div className="inline">{data&&<><Status value={data.settings.enabled?'running':'paused'}/><button className={data.settings.enabled?'':'primary'} onClick={toggle}>{data.settings.enabled?'Pausar scraping':'Activar scraping'}</button></>}</div></header><div className="workspace">{(error||message)&&<p className="error-banner" role="alert">{error||message}</p>}{data?.daemon.error&&<p className="error-banner">{data.daemon.error}</p>}{data&&!data.daemon.online&&<p className="error-banner">El motor no está conectado. Puedes guardar configuración; las tareas se ejecutarán cuando vuelva a estar disponible.</p>}<div className="section-transition" key={activity||section}>{activity?<Activity key={activity} id={activity} onOpen={open}/>:<><div className="page-heading"><h1>{sections[section]||'Análisis'}</h1></div>{data&&section==='dashboard'&&<Monitor data={data}/>} {section==='extract'&&<Extract/>} {section==='active'&&<LiveActivities onOpen={open}/>} {section==='activities'&&<History onOpen={open}/>} {section==='failures'&&<History onOpen={open} failedOnly/>} {section==='seeds'&&<Seeds onOpen={open}/>} {section==='settings'&&data&&<SettingsForm initial={data.settings} onSaved={reload}/>} {section==='domains'&&<Domains onOpen={open}/>} {section==='suspects'&&<Domains onOpen={open} suspects/>}</>}</div></div></main></div></>;
}
