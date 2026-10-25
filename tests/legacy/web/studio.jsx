'use client';
import {useEffect, useState} from 'react';
import {api} from '../lib/api';
import Scrape from './scrape';
import Analyze from './analyze';
import Classify from './classify';
const tabs=[['web','⌘','Analizar sitio'],['classify','▦','Clasificar contenido'],['scrape','↝','Recorrer sitio']];
export default function Studio() {
  const [tab,setTab]=useState('web');
  const [health,setHealth]=useState(null);
  const [desktop,setDesktop]=useState(null);
  useEffect(()=>{
    if(new URLSearchParams(location.search).has('run')) setTab('scrape');
    const viewport=window.matchMedia('(min-width: 1024px)');
    const update=()=>setDesktop(viewport.matches);
    update();viewport.addEventListener('change',update);
    return ()=>viewport.removeEventListener('change',update);
  },[]);
  useEffect(()=>{
    if(desktop) api('/api/health').then(setHealth).catch(()=>setHealth({offline:true}));
  },[desktop]);
  if(desktop===null) return <main className="desktop-notice" role="status">Cargando el portal…</main>;
  if(!desktop) return <main className="desktop-notice"><h1>Solo disponible en escritorio</h1><p>Abre el portal en una ventana de al menos 1024 píxeles de ancho.</p></main>;
  return <div className="shell">
    <aside className="sidebar"><a className="brand" href="/" aria-label="Markup Studio"><span className="brand-mark">M<span>↗</span></span><span><strong>MARKUP</strong><small>STUDIO</small></span></a>
      <div className="sidebar-label">WORKSPACE</div><nav className="tabs" aria-label="Herramientas">{tabs.map(([id,icon,title],index)=><button key={id} type="button" data-tab={id} className={`tab ${tab===id?'active':''}`} onClick={()=>setTab(id)}><span className="tab-icon">{icon}</span><span>{title}</span><span className="tab-index">0{index+1}</span></button>)}</nav>
      <div className="sidebar-spacer"/><div className="system-card"><div className="system-title"><span className="pulse"/> ESTADO DEL SISTEMA</div><div className="system-row"><span>Codex CLI</span><strong>{health?(health.codex_cli?'Disponible':'No encontrado'):'Comprobando'}</strong></div><div className="system-row"><span>Jev</span><strong>{health?.offline?'Sin conexión':health?(health.jev?.configured?'Configurado':'Sin clave'):'Comprobando'}</strong></div></div><div className="sidebar-foot">LOCAL FIRST <span>·</span> Next.js</div>
    </aside>
    <main className="main"><header className="topbar"><div><span className="breadcrumb">WORKSPACE</span><span className="slash">/</span><span>{tabs.find(item=>item[0]===tab)[2].toUpperCase()}</span></div><span className="topbar-right">MARKUP STUDIO <span className="tiny-dot"/></span></header>
      <section className={`page ${tab==='web'?'active':''}`} id="page-web"><Analyze/></section>
      <section className={`page ${tab==='classify'?'active':''}`} id="page-classify"><Classify/></section>
      <section className={`page ${tab==='scrape'?'active':''}`} id="page-scrape"><Scrape/></section>
    </main>
  </div>;
}
