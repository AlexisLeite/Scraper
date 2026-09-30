"""Classify structural DOM groups with Jev and assemble evidenced results."""
import json
from urllib.parse import urlparse

from . import jev_connector
from .dom import candidate_groups, clean_document, group_values
from .uri_to_html import _valid_url, capture_page, verify_action

MAX_CAPTURE_CHARS = 8_000_000

def _choice(answer, options):
    value = answer.get('choice') if isinstance(answer, dict) else None
    probabilities = answer.get('probabilities', {}) if isinstance(answer, dict) else {}
    confidence = answer.get('confidence') if isinstance(answer, dict) else None
    if confidence is None and isinstance(probabilities, dict):
        confidence = probabilities.get(value)
    return (value, confidence) if value in options else ('other', None)

ROLE_OPTIONS = {
    'primary_products': 'Colección principal de tarjetas de productos que corresponde a la página.',
    'secondary_products': 'Carrusel, recomendaciones o productos repetidos fuera de la colección principal.',
    'categories': 'Enlaces de categorías de productos.',
    'breadcrumb': 'Migas de pan que muestran ancestros de la página.',
    'reviews': 'Reseñas publicadas, valoraciones o widget de reseñas.',
    'pagination': 'Posible control para avanzar el listado principal, pendiente de prueba de interacción.',
    'attributes': 'Atributos de la ficha: beneficios, ingredientes, uso, tamaño, precio u otros detalles.',
    'site_menu': 'Navegación global del sitio.',
    'other': 'Otro contenido o evidencia insuficiente.'
}


def _decision(value, confidence, evidence_ids, verification='modelo'):
    return {'value': value, 'evidence_ids': evidence_ids, 'model': 'Jev',
            'confidence': confidence, 'verification': verification}


def _model_group(group):
    def item(entry):
        attrs = entry.get('attrs', {})
        return {'text': entry.get('text'), 'url': entry.get('url'),
                'attrs': {key: value[:3] if isinstance(value, list) else value
                          for key, value in attrs.items() if key in ('id', 'class', 'role', 'itemprop', 'aria-label', 'rel')}}
    return {'id': group['id'], 'shape': group['shape'], 'region': group['region'],
            'heading': group['heading'], 'text': group['text'], 'attrs': group['attrs'],
            'members': [item(x) for x in group['entries']],
            'links': [{'text': x['text'], 'url': x['url']} for x in group['links']],
            'controls': [item(x) for x in group['controls']]}


def decide_groups(title, url, soup, structured, groups, predict=jev_connector.predict):
    state = {'title': title, 'url': url,
             'headings': [x.get_text(' ', strip=True) for x in soup.select('h1,h2,h3')][:15],
             'jsonld': [{k: v for k, v in item.items() if k in ('@type', 'name', 'url', 'aggregateRating', 'review')}
                        for item in structured[:15]],
             'main_text': (soup.find('main') or soup.body or soup).get_text(' ', strip=True)[:3500]}
    page_q = {'page_kind': {'type': 'choice', 'instructions': 'Función principal de la página, según el contenido visible. Puede ser otra.',
                           'criteria': {'product': 'Ficha de un producto individual', 'listing': 'Listado de varios productos',
                                        'other': 'Otra página o evidencia insuficiente'}}}
    page_result = predict(state, page_q)
    page_answer = page_result.get('answers', {}).get('page_kind') or {}
    kind, kind_confidence = _choice(page_answer, page_q['page_kind']['criteria'])
    decisions = []
    batches = []
    current = []
    for group in groups:
        candidate = current + [group]
        context_size = len(json.dumps([_model_group(x) for x in candidate], ensure_ascii=False))
        if current and (len(candidate) > 8 or context_size > 16000):
            batches.append(current)
            current = [group]
        else:
            current = candidate
    if current:
        batches.append(current)
    for batch in batches:
        questions = {g['id']: {'type': 'choice',
                    'instructions': f"Clasifica solo el grupo {g['id']} completo en el contexto de la página. Abstente si no basta la evidencia.",
                    'criteria': ROLE_OPTIONS} for g in batch}
        context = {'page': state, 'page_kind': kind,
                   'groups': [_model_group(g) for g in batch]}
        response = predict(context, questions)
        answers = response.get('answers', {})
        for g in batch:
            role, confidence = _choice(answers.get(g['id']), ROLE_OPTIONS)
            decisions.append({**_decision(role, confidence, [g['id']]),
                              'model': response.get('model', 'Jev'),
                              'probabilities': (answers.get(g['id']) or {}).get('probabilities', {}),
                              'group_id': g['id'],
                              'heading': g['heading'], 'region': g['region'], 'shape': g['shape']})
    page_decision = _decision(kind, kind_confidence,
                              ['page:headings', 'page:text', 'jsonld'], 'Jev sobre DOM')
    page_decision['model'] = page_result.get('model', 'Jev')
    page_decision['probabilities'] = page_answer.get('probabilities', {})
    return page_decision, decisions


def _select_roles(groups, decisions, roles):
    lookup = {g['id']: g for g in groups}
    return [(lookup[d['group_id']], d) for d in decisions if d['value'] in roles]


def choose_primary_group(soup, url, title, groups, decisions, predict):
    candidates = [g for g in groups if g['region'] == 'main' and g['shape'] == 'repeated_siblings'
                  and len(g['entries']) >= 2 and len(group_values(g, soup, url)['links']) >= 2]
    if not candidates:
        return []
    winners = []
    for start in range(0, len(candidates), 9):
        batch = candidates[start:start+9]
        criteria = {g['id']: f"Grupo estructural {g['id']}, {len(g['entries'])} elementos hermanos" for g in batch}
        criteria['other'] = 'Ninguno es la colección principal de productos.'
        answer = predict({'title': title, 'page_kind': 'listing', 'groups': [_model_group(g) for g in batch]},
                         {'primary_group': {'type': 'choice',
                           'instructions': 'Elige la colección principal de tarjetas de producto; descarta navegación, filtros y carruseles secundarios. Puedes abstenerte.',
                           'criteria': criteria}}).get('answers', {}).get('primary_group')
        choice, confidence = _choice(answer, criteria)
        if choice != 'other':
            winners.append((next(g for g in batch if g['id'] == choice), confidence))
    if not winners:
        return []
    if len(winners) > 1:
        criteria = {g['id']: f"Grupo {g['id']} con {len(g['entries'])} elementos" for g, _ in winners}
        criteria['other'] = 'Ninguno.'
        answer = predict({'title': title, 'groups': [_model_group(g) for g, _ in winners]},
                         {'primary_group': {'type': 'choice', 'instructions': 'Elige una sola colección principal.',
                                            'criteria': criteria}}).get('answers', {}).get('primary_group')
        choice, confidence = _choice(answer, criteria)
        winners = [(g, confidence) for g, _ in winners if g['id'] == choice]
    return [(g, _decision('primary_products', confidence, [g['id']], 'Jev sobre grupos DOM')) for g, confidence in winners[:1]]


def _specific_pairs(soup, pairs):
    selected = []
    for group, decision in pairs:
        tag = soup.select_one(group['selector'])
        if tag and any(other['id'] != group['id'] and
                       (child := soup.select_one(other['selector'])) is not None and tag in child.parents
                       for other, _ in pairs):
            continue
        selected.append((group, decision))
    return selected or pairs


def reconcile_primary(decisions, primary):
    """Keep the initial label as evidence, including when Jev declines a main group."""
    chosen = primary[0][0]['id'] if primary else None
    for decision in decisions:
        if decision['value'] == 'primary_products' and decision['group_id'] != chosen:
            decision['original_decision'] = dict(decision)
            decision['value'] = 'other'
            decision['confidence'] = None
            decision['verification'] = ('Reemplazada por selección de colección principal' if chosen
                                        else 'Jev no confirmó una colección principal')
        elif decision['group_id'] == chosen:
            decision['original_decision'] = dict(decision)
            decision.update(primary[0][1])
            decision['group_id'] = chosen


def _links_for(soup, url, pairs):
    result, seen = [], set()
    for group, decision in _specific_pairs(soup, pairs):
        for entry in group_values(group, soup, url)['links']:
            target = entry['url']
            if not target or target in seen or urlparse(target).hostname != urlparse(url).hostname:
                continue
            seen.add(target)
            result.append({'label': entry['text'] or target, 'url': target,
                           'evidence_ids': [group['id']], 'confidence': decision['confidence'],
                           'model': 'Jev', 'verification': 'DOM'})
    return result


def _review_decision(soup, url, groups, decisions, predict):
    pairs = _select_roles(groups, decisions, {'reviews'})
    if not pairs:
        return _decision('unknown', None, [], 'Sin grupo identificado'), {'value': 'unknown', 'evidence_ids': []}
    context = {'groups': [{'id': g['id'], 'heading': g['heading'], 'values': group_values(g, soup, url)} for g, _ in pairs[:4]]}
    questions = {
        'state': {'type': 'choice', 'instructions': 'Distingue reseñas publicadas del widget vacío. Solo cuenta registros visibles; una puntuación agregada no basta.',
                  'criteria': {'published': 'Hay registros de reseñas individuales visibles', 'empty_widget': 'Widget presente pero sin reseñas publicadas visibles', 'unknown': 'No hay evidencia suficiente'}},
        'mode': {'type': 'choice', 'instructions': 'Cómo se recorren las reseñas de este grupo, si hay evidencia.',
                 'criteria': {'load_more': 'Control para cargar más reseñas', 'pages': 'Páginas de reseñas', 'all_visible': 'Todas visibles', 'unknown': 'No se puede determinar'}}}
    answers = predict(context, questions).get('answers', {})
    state, conf = _choice(answers.get('state'), questions['state']['criteria'])
    mode, mode_conf = _choice(answers.get('mode'), questions['mode']['criteria'])
    ids = [g['id'] for g, _ in pairs]
    return _decision(state, conf, ids, 'Jev sobre registros DOM'), _decision(mode, mode_conf, ids, 'Acción pendiente de verificar')


def analyze_interactions(soup, url, page, groups, decisions, primary, predict,
                         verify=verify_action, *, interactive=False, capture_evidence=False,
                         min_confidence=0.65, max_actions=4):
    """Jev nominates blocks and controls; Playwright reports their observed effects."""
    state, mode = _review_decision(soup, url, groups, decisions, predict)
    reviews = _select_roles(groups, decisions, {'reviews'})
    review_roots = [soup.select_one(g['selector']) for g, _ in reviews]
    record_candidates = []
    if state['value'] == 'published':
        for group in groups:
            tag = soup.select_one(group['selector'])
            if (tag is not None and group['shape'] == 'repeated_siblings' and
                any(root is tag or root in tag.parents for root in review_roots if root is not None)):
                record_candidates.append(group)
    review_selector = None
    records_decision = _decision('other', None, [], 'No hay lista de reseñas confirmada')
    if record_candidates:
        criteria = {g['id']: f"Grupo estructural {g['id']}" for g in record_candidates}
        criteria['other'] = 'Ninguno contiene una lista de reseñas individuales.'
        response = predict({'page_kind': page['value'],
                            'groups': [_model_group(g) for g in record_candidates]},
                           {'records': {'type': 'choice',
                            'instructions': 'Elige el grupo cuyos hijos directos son reseñas individuales publicadas. Excluye estadísticas, filtros y botones. Abstente si no hay uno.',
                            'criteria': criteria}})
        choice, confidence = _choice(response.get('answers', {}).get('records'), criteria)
        records_decision = _decision(choice, confidence, [choice] if choice != 'other' else [], 'Jev sobre registros DOM')
        if choice != 'other' and confidence is not None and confidence >= min_confidence:
            review_selector = next(g['selector'] for g in record_candidates if g['id'] == choice)
    roles = {'pagination', 'reviews', 'secondary_products'}
    candidates = {}
    for group, decision in _select_roles(groups, decisions, roles):
        values = group_values(group, soup, url)
        items = values['controls'] + (values['links'] if decision['value'] == 'pagination' else [])
        for item in items:
            candidate = {**item, 'group_id': group['id'], 'group_role': decision['value'],
                         'collection_selector': group['selector'], 'context': _model_group(group)}
            # Prefer the containing block's context over a lone button candidate.
            if item['selector'] not in candidates or group['shape'] != 'control':
                candidates[item['selector']] = candidate
    nominees = []
    values = list(candidates.values())
    criteria = {'advance_products': 'Agrega o reemplaza productos en la colección principal',
                'load_reviews': 'Carga más reseñas individuales publicadas',
                'carousel': 'Mueve un carrusel secundario',
                'other': 'Ordenar, filtrar, otra acción o evidencia insuficiente'}
    for start in range(0, len(values), 4):
        batch = values[start:start + 4]
        questions = {f'control_{i}': {'type': 'choice',
                     'instructions': f'Clasifica la acción del control {i} con su grupo completo. Un texto Siguiente o un número no prueba paginación. Abstente si falta evidencia.',
                     'criteria': criteria} for i in range(len(batch))}
        response = predict({'page_kind': page['value'], 'controls': batch,
                            'primary_group': _model_group(primary[0][0]) if primary else None,
                            'review_state': state, 'review_mode': mode}, questions)
        for i, control in enumerate(batch):
            role, confidence = _choice(response.get('answers', {}).get(f'control_{i}'), criteria)
            if confidence is None or confidence < min_confidence:
                continue
            if (role == 'carousel' or (role == 'advance_products' and primary) or
                (role == 'load_reviews' and review_selector)):
                nominees.append({**{k: v for k, v in control.items() if k != 'context'},
                                 'predicted_role': role, 'confidence': confidence,
                                 'evidence_ids': [control['group_id']], 'model': response.get('model', 'Jev')})
    nominees.sort(key=lambda item: item['confidence'], reverse=True)
    checks = []
    for control in nominees[:max_actions]:
        if not interactive:
            checks.append({'effect': 'unverified', 'verification': 'HTML; interacción no disponible',
                           'control': control})
            continue
        try:
            kwargs = {'capture_evidence': True} if capture_evidence else {}
            check = verify(url, control, primary[0][0]['selector'] if primary else None,
                           review_selector, **kwargs)
            checks.append(check)
            if check['effect'] == 'loaded_reviews':
                mode = {**mode, 'value': 'load_more', 'verification': 'Playwright: agregó reseñas'}
        except Exception as exc:
            checks.append({'effect': 'unverified', 'verification': 'Playwright',
                           'control': control, 'reason': type(exc).__name__})
    return {'state': state, 'mode': mode, 'records': records_decision,
            'selector': review_selector}, checks


def inspect_url(url, objective='', progress=None, captured=None, predict=jev_connector.predict, verify=verify_action):
    _valid_url(url)
    def update(phase, percent, message):
        if progress:
            progress(phase, percent, message)
    update('capture', 5, 'Renderizando la página' if captured is None else 'Importando HTML')
    html, screenshot, final_url, title = capture_page(url) if captured is None else captured
    if len(html) > MAX_CAPTURE_CHARS:
        raise ValueError('El HTML supera el límite permitido.')
    update('dom', 25, 'Preparando DOM y abriendo detalles en una copia')
    soup, structured = clean_document(html)
    title = title or (soup.title.get_text(' ', strip=True) if soup.title else final_url)
    groups = candidate_groups(soup, final_url, title, limit=100)
    update('jev', 45, f'Jev: clasificando {len(groups)} grupos')
    page, decisions = decide_groups(title, final_url, soup, structured, groups, predict)
    primary = choose_primary_group(soup, final_url, title, groups, decisions, predict) if page['value'] == 'listing' else []
    if page['value'] == 'listing':
        reconcile_primary(decisions, primary)

    categories = _select_roles(groups, decisions, {'categories', 'breadcrumb'})
    secondary = _select_roles(groups, decisions, {'secondary_products'})
    reviews = _select_roles(groups, decisions, {'reviews'})
    attributes = [{'group_id': g['id'], 'heading': g['heading'], 'text': group_values(g, soup, final_url)['text'],
                   'confidence': d['confidence']} for g, d in _select_roles(groups, decisions, {'attributes'})]
    update('actions', 75, 'Comprobando acciones con Playwright')
    review_analysis, checks = analyze_interactions(
        soup, final_url, page, groups, decisions, primary, predict, verify,
        interactive=captured is None)
    review_state, review_mode = review_analysis['state'], review_analysis['mode']
    product_links = _links_for(soup, final_url, primary)
    category_links = _links_for(soup, final_url, categories)
    related_links = _links_for(soup, final_url, secondary)
    review_links = _links_for(soup, final_url, reviews)
    if objective and category_links:
        update('objective', 88, 'Jev: valorando rutas confirmadas para el objetivo')
        for start in range(0, len(category_links), 10):
            batch = category_links[start:start+10]
            questions = {f'route_{i}': {'type': 'noul', 'instructions':
                         f"Dado el objetivo '{objective}', ¿merece recorrerse la ruta confirmada '{x['label']}' ({x['url']})?"}
                         for i, x in enumerate(batch)}
            answers = predict({'objective': objective, 'confirmed_categories': batch}, questions).get('answers', {})
            for i, item in enumerate(batch):
                item['relevance'] = answers.get(f'route_{i}', {}).get('noul')
    update('done', 96, 'Preparando evidencia')
    main = soup.find('main') or soup.body or soup
    clean_html = str(soup)
    return {'url': final_url, 'title': title, 'html': html, 'clean_html': clean_html,
            'screenshot': screenshot, 'stats': {'elements': len(soup.find_all(True)),
            'candidate_groups': len(groups), 'decided_groups': sum(d['value'] != 'other' for d in decisions),
            'content_chars': len(main.get_text(' ', strip=True)), 'links': len(soup.find_all('a', href=True))},
            'headings': [h.get_text(' ', strip=True) for h in soup.select('h1,h2,h3')][:12],
            'text_preview': main.get_text(' ', strip=True)[:1200],
            'classification': page, 'analysis': {'groups': decisions, 'primary_group': primary[0][0]['id'] if primary else None,
            'product_links': product_links, 'categories': category_links, 'breadcrumbs': _links_for(soup, final_url, _select_roles(groups, decisions, {'breadcrumb'})),
            'related_products': related_links, 'reviews': {'state': review_state, 'mode': review_mode, 'links': review_links},
            'attributes': attributes, 'actions': checks, 'jsonld': structured[:20]}, 'objective': objective}
