/* Taxonomy views of model decisions. Association uses source DOM, never row order. */
(() => {
  const labels = {name:'Producto',brand:'Marca',image:'Imagen',product_link:'Ficha',description:'Descripción',benefit:'Beneficios',ingredients:'Ingredientes',usage:'Modo de uso',size:'Tamaño / variante',price:'Precio',previous_price:'Precio anterior',availability:'Disponibilidad',rating:'Puntuación',rating_count:'Valoraciones anunciadas',review_title:'Título',review_body:'Opinión',author:'Autor',date:'Fecha',question:'Pregunta',answer:'Respuesta',post:'Mensaje'};
  const roles = {primary_products:'Lista de productos principales',secondary_products:'Lista de productos secundarios',products:'Lista de productos',product:'Fichas y tarjetas de producto',attributes:'Características de producto',offer:'Ofertas',reviews:'Reseñas',qa:'Preguntas y respuestas',discussion:'Debates',categories:'Categorías',breadcrumb:'Ruta de navegación',site_menu:'Menú del sitio',collection_control:'Controles de colección',search:'Búsqueda',filters:'Filtros',header:'Cabecera',footer:'Pie de página',brand:'Marca',promotion:'Promociones',service:'Servicios',editorial:'Contenido editorial'};
  const productRoles = ['primary_products','secondary_products','products','product','attributes','offer'];
  function node(tag,cls,text) { const n=document.createElement(tag);n.className=cls||'';if(text!=null)n.textContent=text;return n; }
  function select(dom,selector) {try{return dom?.querySelector(selector)||null;}catch{return null;}}
  function link(url,text) {const n=node('a','',text||url);if(/^https?:\/\//i.test(url||'')){n.href=url;n.target='_blank';n.rel='noopener noreferrer';}return n;}
  function evidence(container,value) {
    const d=node('details','scrape-entity-text');d.append(node('summary','','Fuente'),node('p','',`${value.selector} · confianza ${value.confidence==null?'sin registro':Math.round(value.confidence*100)+'%'} · bloques ${(value.entityIds||[]).join(', ')}`));container.append(d);
  }
  function field(container,values) {
    for(const value of values||[]) {
      const row=node('div','commercial-value');
      if(value.src && /^https?:\/\//i.test(value.src)) {const img=node('img','commercial-product-image');img.src=value.src;img.alt=value.text||'Imagen extraída';img.loading='lazy';img.referrerPolicy='no-referrer';row.append(img);}
      else row.append(value.href?link(value.href,value.text):node('span','',value.text));
      if(value.confidence==null||value.confidence<.65)row.append(node('small','commercial-uncertain','Clasificación por revisar'));
      evidence(row,value);container.append(row);
    }
  }
  function records(detail,dom) {
    const entities=(detail.result?.entities||[]).filter(e=>e.role!=='other');
    const roots=entities.map(entity=>({entity,root:select(dom,entity.selector)}));
    const products=roots.filter(e=>e.entity.role==='product'&&e.root);
    const collections=roots.filter(e=>['primary_products','secondary_products','products'].includes(e.entity.role)&&e.root);
    // A collection inside an individual product cannot split that product's fields.
    const conflicts=collections.filter(c=>products.some(p=>p.root!==c.root&&p.root.contains(c.root)));
    const usable=collections.filter(c=>!conflicts.includes(c));
    const groups=new Map(),isolated=[];
    const ids=new WeakMap();let serial=0;
    const id=root=>{if(!ids.has(root))ids.set(root,++serial);return ids.get(root);};
    const outer=(entries)=>entries.find(a=>!entries.some(b=>b.root!==a.root&&b.root.contains(a.root)))||entries[0];
    const inner=(entries)=>entries.find(a=>!entries.some(b=>b.root!==a.root&&a.root.contains(b.root)))||entries[0];
    for(const {entity,root} of roots) {
      if(!productRoles.includes(entity.role)&&!['reviews','qa','discussion'].includes(entity.role))continue;
      for(const part of entity.parts||[])for(const value of part.values||[]) {
        if(!labels[part.class])continue;
        const text=value.text||value.href||value.src||value.datetime;if(!text)continue;
        const source=select(dom,value.selector);
        let member=null,owner=null;
        if(source&&root?.contains(source)) {
          if(productRoles.includes(entity.role)) {
            const product=outer(products.filter(p=>p.root.contains(source)));
            owner=inner(usable.filter(c=>c.root.contains(product?.root||source)));
            if(product)member=product.root;
            else if(['product','attributes','offer'].includes(entity.role))member=root;
          } else owner={entity,root};
          if(!member&&Array.isArray(part.template_path)) {
            member=source;for(const _ of part.template_path)member=member?.parentElement;
            if(!member||!root.contains(member))member=null;
          }
          if(owner&&member&&member!==owner.root&&owner.root.contains(member)) {
            while(member.parentElement&&member.parentElement!==owner.root)member=member.parentElement;
          }
        }
        const item={...value,text,confidence:part.confidence,entityIds:[entity.id]};
        if(!member){isolated.push({class:part.class,value:item});continue;}
        const role=owner?.entity.role||entity.role;
        // Fields from a nested collection resolve to their enclosing product view.
        const displayRole=productRoles.includes(entity.role)&&products.some(p=>p.root===member)&&!owner?'product':role;
        const groupKey=displayRole==='reviews'?'reviews':owner?`owner:${owner.entity.id}`:displayRole;
        if(!groups.has(groupKey))groups.set(groupKey,{role:displayRole,entity:owner?.entity,records:new Map()});
        const group=groups.get(groupKey),key=id(member);
        if(!group.records.has(key))group.records.set(key,{fields:{},entityIds:new Set()});
        const record=group.records.get(key);record.entityIds.add(entity.id);
        const values=record.fields[part.class]||=[];
        const old=values.find(v=>v.selector===item.selector&&v.text===item.text);
        if(old){old.confidence=Math.max(old.confidence||0,item.confidence||0);if(!old.entityIds.includes(entity.id))old.entityIds.push(entity.id);}
        else values.push(item);
      }
    }
    return {groups,isolated,conflicts,roots};
  }
  function render(target,detail,dom) {
    target.replaceChildren();
    const data=records(detail,dom);
    target.append(node('p','coverage-note','Bloques organizados según los tipos y campos extraídos. Cada elemento conserva sus propios campos y fuentes. Los campos ausentes no se inventan.'));
    for(const group of data.groups.values()) {
      const section=node('section','commercial-taxonomy-group');section.dataset.role=group.role;
      section.append(node('h3','',`${roles[group.role]||group.role} · ${group.records.size}`));
      if(group.entity?.heading)section.append(node('p','coverage-note',group.entity.heading));
      const list=node('div',productRoles.includes(group.role)?'commercial-products':'commercial-items');
      for(const record of group.records.values()) {
        const isReview=group.role==='reviews'&&['review_body','review_title','author','date'].some(key=>record.fields[key]);
        const card=node('article',productRoles.includes(group.role)?'commercial-product':isReview?'commercial-review':'commercial-description');
        card.dataset.blocks=[...record.entityIds].join(',');
        const title=record.fields.name?.[0]?.text||record.fields.review_title?.[0]?.text;
        card.append(node('h4','',title||(productRoles.includes(group.role)?'Producto sin nombre extraído':group.role==='reviews'&&!isReview?'Estadísticas de valoraciones':'Elemento extraído')));
        if(productRoles.includes(group.role)&&!title)card.append(node('p','commercial-uncertain','Extracción incompleta: revisar la fuente de este bloque.'));
        const fields=node('dl','commercial-fields');
        for(const [key,values] of Object.entries(record.fields)) {
          const dt=node('dt','',labels[key]),dd=node('dd');field(dd,values);fields.append(dt,dd);
        }
        card.append(fields);list.append(card);
      }
      section.append(list);target.append(section);
    }
    const represented=new Set();for(const group of data.groups.values())for(const record of group.records.values())for(const id of record.entityIds)represented.add(id);
    for(const role of Object.keys(roles)) {
      const entries=data.roots.filter(e=>e.entity.role===role&&!represented.has(e.entity.id)&&!data.conflicts.includes(e)&&
        !data.roots.some(p=>p!==e&&p.entity.role===role&&p.root&&e.root&&p.root.contains(e.root)&&p.entity.text===e.entity.text));
      if(!entries.length)continue;
      const section=node('section','commercial-taxonomy-group');section.dataset.role=role;section.append(node('h3','',roles[role]));
      for(const {entity} of entries) {
        const card=node('article','commercial-description');if(entity.heading)card.append(node('h4','',entity.heading));
        card.append(node('p','',entity.text||'Sin contenido textual extraído.'));
        const links=node('ul');for(const item of entity.links||[]) {const li=node('li');li.append(link(item.url,item.text));links.append(li);}if(links.children.length)card.append(links);
        evidence(card,{selector:entity.selector,confidence:entity.confidence,entityIds:[entity.id]});section.append(card);
      }
      target.append(section);
    }
    if(data.conflicts.length) {
      const notice=node('details','scrape-content-group');notice.append(node('summary','','Clasificaciones de colecciones por revisar'));
      for(const {entity} of data.conflicts)notice.append(node('p','',`${entity.id}: La extracción identificó ${roles[entity.role]} dentro de un bloque de producto. Sus campos se muestran en ese producto; la decisión original permanece en la auditoría.`));target.append(notice);
    }
    if(data.isolated.length) {
      const section=node('details','scrape-content-group');section.append(node('summary','','Campos sin asociación DOM verificada'));
      for(const item of data.isolated){section.append(node('strong','',labels[item.class]));field(section,[item.value]);}target.append(section);
    }
    if(detail.result?.reviews?.state?.value==='empty_widget')target.append(node('p','no-evidence','Se identificó un widget vacío: no hay reseñas publicadas extraídas.'));
    return data;
  }
  window.ScrapeCommercial={render,records};
})();
