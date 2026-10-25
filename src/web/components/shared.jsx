export function JsonView({value, title='Resultado y evidencias completas'}) {
  return <details className="vector-details"><summary>{title}</summary><pre>{JSON.stringify(value,null,2)}</pre></details>;
}
export function Link({url, children}) { return /^https?:\/\//i.test(url||'') ? <a href={url} target="_blank" rel="noopener noreferrer">{children||url}</a> : <span>{children||url}</span>; }
export function Heading({eyebrow, title, subtitle}) { return <div className="page-heading"><div className="eyebrow"><span className="eyebrow-line"/>{eyebrow}</div><h1>{title}</h1><p>{subtitle}</p></div>; }
export function Panel({title, children, className=''}) { return <div className={`panel studio-panel ${className}`}><div className="panel-head"><h2>{title}</h2></div>{children}</div>; }
export function Progress({title, detail, percent}) { return <div className="progress-panel" role="status" aria-live="polite"><div className="progress-top"><strong>{title}</strong><span>{Math.round(percent||0)}%</span></div><div className="progress-track"><div style={{width:`${Math.min(100,percent||0)}%`}}/></div><span>{detail}</span></div>; }
