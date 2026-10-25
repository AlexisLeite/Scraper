"""Classify structural DOM groups with Jev and assemble evidenced results."""
import json
from contextvars import ContextVar
from contextlib import contextmanager
from urllib.parse import urlparse

from tests.legacy import jev_connector
from src.common.dom import candidate_groups, clean_document, group_values
from src.common.uri_to_html import _valid_url, capture_page, verify_action
from src.common.taxonomy import BLOCKS, VERSION
from tests.legacy.block_tree import compact_group, classify_parts, recognized_summary

MAX_CAPTURE_CHARS = 8_000_000
_defer_interactions = ContextVar('defer_scrape_interactions', default=False)


@contextmanager
def plan_interactions():
    """Nominate controls without clicking; the page strategy budgets interactions."""
    token = _defer_interactions.set(True)
    try:
        yield
    finally:
        _defer_interactions.reset(token)


def _choice(answer, options):
    value = answer.get('choice') if isinstance(answer, dict) else None
    probabilities = answer.get('probabilities', {}) if isinstance(answer, dict) else {}
    confidence = answer.get('confidence') if isinstance(answer, dict) else None
    if confidence is None and isinstance(probabilities, dict):
        confidence = probabilities.get(value)
    return (value, confidence) if value in options else ('other', None)

ROLE_OPTIONS = BLOCKS


def _decision(value, confidence, evidence_ids, verification='modelo'):
    return {'value': value, 'evidence_ids': evidence_ids, 'model': 'Jev',
            'confidence': confidence, 'verification': verification}


def _model_group(group):
    if group.get('model_context') is not None:
        context = group['model_context']
        return {**context, 'samples': [{'count': x['count'], 'text': x['text']} for x in context['samples']]}
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
    """Classify locally, extract parts, then ask Jev to interpret recognized evidence."""
    decisions = []
    compact = [compact_group(group, soup) for group in groups]
    for group, context in zip(groups, compact):
        group['model_context'] = context
    batches, current = [], []
    for group in compact:
        candidate = current + [group]
        if current and (len(candidate) > 8 or len(json.dumps(candidate, ensure_ascii=False)) > 16000):
            batches.append(current)
            current = [group]
        else:
            current = candidate
    if current:
        batches.append(current)
    for batch in batches:
        questions = {g['id']: {'type': 'choice',
            'instructions': f"Clasifica el bloque {g['id']} por su contenido local. No presupongas el tipo de página ni si es principal. Usa Otro para descartar.",
            'criteria': {key: key.replace('_', ' ') for key in ROLE_OPTIONS}} for g in batch}
        response = predict({'groups': batch, 'taxonomy_version': VERSION,
                            'class_definitions': ROLE_OPTIONS}, questions)
        for group in batch:
            answer = response.get('answers', {}).get(group['id']) or {}
            role, confidence = _choice(answer, ROLE_OPTIONS)
            decisions.append({**_decision(role, confidence, [group['id']]),
                'model': response.get('model', 'Jev'), 'probabilities': answer.get('probabilities', {}),
                'group_id': group['id'], 'heading': group['heading'],
                'region': group['region'], 'shape': group['shape'], 'local_class': role})
    classify_parts(groups, decisions, soup, url, predict, _choice)
    summary = recognized_summary(groups, decisions, soup)
    product_blocks = [block for block in summary['blocks'] if block['class'] == 'products']
    primary_options = {block['id']: f"Colección reconocida {block['id']}" for block in product_blocks}
    primary_options['other'] = 'Otro: ninguna colección principal confirmada o evidencia insuficiente.'
    questions = {
        'page_kind': {'type': 'choice',
            'instructions': 'Deduce la función principal de la página SOLO de este resumen de bloques y partes reconocidos. Las cantidades no determinan el tipo. Puedes elegir Otro.',
            'criteria': {'product': 'Ficha de producto', 'listing': 'Listado de productos',
                'commercial_landing': 'Presentación comercial o campaña', 'editorial': 'Artículo o guía',
                'reviews_page': 'Página dedicada a reseñas', 'discussion': 'Debate',
                'institutional': 'Información institucional', 'other': 'Otro o evidencia insuficiente'}},
        'primary_group': {'type': 'choice',
            'instructions': 'Identifica la colección principal de productos a partir de los bloques reconocidos y sus relaciones. Descarta recomendaciones y contenedores generales. Otro si ninguna está confirmada.',
            'criteria': primary_options}}
    response = predict({'summary': summary, 'taxonomy_version': VERSION}, questions)
    answers = response.get('answers', {})
    kind, confidence = _choice(answers.get('page_kind'), questions['page_kind']['criteria'])
    chosen, primary_confidence = _choice(answers.get('primary_group'), primary_options)
    if primary_confidence is None or primary_confidence < 0.65:
        chosen = 'other'
    summarized_products = {block['id'] for block in product_blocks}
    for decision in decisions:
        if decision['value'] == 'products':
            decision['original_decision'] = dict(decision)
            decision['value'] = ('primary_products' if decision['group_id'] == chosen else
                                 'secondary_products' if decision['group_id'] in summarized_products else 'other')
            if decision['group_id'] == chosen:
                decision['confidence'] = primary_confidence
            decision['verification'] = 'Jev sobre resumen de bloques reconocidos'
    page = _decision(kind, confidence, [block['id'] for block in summary['blocks']],
                     'Jev sobre resumen de bloques reconocidos')
    page.update(model=response.get('model', 'Jev'), summary=summary, taxonomy_version=VERSION,
                candidate_deduplication={'sent_to_model': len(groups),
                    'controls_grouped': sum(len(g.get('grouped_controls', [])) for g in groups),
                    'reused_field_predictions': sum(d.get('reused_field_predictions', 0) for d in decisions),
                    'nested_wrappers_collapsed': sum(len(g.get('structural_aliases', [])) for g in groups),
                    'aliases': {g['id']: g['structural_aliases'] for g in groups if g.get('structural_aliases')}},
                primary_decision=_decision(chosen, primary_confidence, [chosen] if chosen != 'other' else []),
                probabilities=(answers.get('page_kind') or {}).get('probabilities', {}))
    return page, decisions


def _select_roles(groups, decisions, roles):
    lookup = {g['id']: g for g in groups}
    return [(lookup[d['group_id']], d) for d in decisions if d['value'] in roles and
            d['confidence'] is not None and d['confidence'] >= 0.65]




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
    context = {'groups': [_model_group(g) for g, _ in pairs[:4]]}
    questions = {
        'state': {'type': 'choice', 'instructions': 'Distingue reseñas publicadas del widget vacío. Solo cuenta registros visibles; una puntuación agregada no basta.',
                  'criteria': {'published': 'Hay registros de reseñas individuales visibles', 'empty_widget': 'Widget presente pero sin reseñas publicadas visibles', 'unknown': 'No hay evidencia suficiente', 'other': 'Otro; no se puede determinar'}},
        'mode': {'type': 'choice', 'instructions': 'Cómo se recorren las reseñas de este grupo, si hay evidencia.',
                 'criteria': {'load_more': 'Control para cargar más reseñas', 'pages': 'Páginas de reseñas', 'all_visible': 'Todas visibles', 'unknown': 'No se puede determinar', 'other': 'Otro modo o evidencia insuficiente'}}}
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
    roles = {'collection_control', 'reviews', 'secondary_products', 'primary_products', 'product'}
    candidates = {}
    for group, decision in _select_roles(groups, decisions, roles):
        values = group_values(group, soup, url)
        items = values['controls'] + (values['links'] if decision['value'] == 'collection_control' else [])
        for item in items:
            candidate = {**item, 'group_id': group['id'], 'group_role': decision['value'],
                         'collection_selector': group['selector'], 'context': _model_group(group),
                         '_context_depth': len(list(soup.select_one(group['selector']).parents)) if group['shape'] != 'control' else -1}
            # Prefer the containing block's context over a lone button candidate.
            if (item['selector'] not in candidates or candidate['_context_depth'] >
                candidates[item['selector']]['_context_depth']):
                candidates[item['selector']] = candidate
    nominees = []
    values = list(candidates.values())
    criteria = {'advance_products': 'Agrega o reemplaza productos en la colección principal',
                'load_reviews': 'Carga más reseñas individuales publicadas',
                'carousel': 'Mueve un carrusel o galería; no avanza una colección de productos',
                'other': 'Ordenar, filtrar, otra acción o evidencia insuficiente'}
    control_bundles = {}
    for control in values:
        control_bundles.setdefault(control['group_id'], []).append(control)
    control_batches, current = [], []
    for bundle in control_bundles.values():
        proposed = current + bundle
        shared_contexts = {control['group_id']: control['context'] for control in proposed}
        if current and (len(proposed) > 32 or len(json.dumps(shared_contexts, ensure_ascii=False)) > 16000):
            control_batches.append(current)
            current = list(bundle)
        else:
            current = proposed
    if current:
        control_batches.append(current)
    for batch in control_batches:
        questions = {f'control_{i}': {'type': 'choice',
                     'instructions': f"Clasifica exclusivamente el control control_{i}, texto '{batch[i]['text']}', en group_contexts[group_id] y con el estado de la colección. Otro permite descartar. Una etiqueta no prueba el efecto, que se verificará después.",
                     'criteria': criteria} for i in range(len(batch))}
        model_controls = [{'id': f'control_{i}', **{k: v for k, v in control.items()
                           if k in ('text', 'url', 'group_id', 'group_role')}}
                          for i, control in enumerate(batch)]
        contexts = {control['group_id']: control['context'] for control in batch}
        primary_id = primary[0][0]['id'] if primary else None
        if primary_id and primary_id not in contexts:
            contexts[primary_id] = _model_group(primary[0][0])
        response = predict({'page_kind': page['value'], 'controls': model_controls,
                            'group_contexts': contexts,
                            'primary_group_id': primary_id,
                            'review_state': state, 'review_mode': mode}, questions)
        for i, control in enumerate(batch):
            role, confidence = _choice(response.get('answers', {}).get(f'control_{i}'), criteria)
            if confidence is None or confidence < min_confidence:
                continue
            if ((role == 'carousel' and control['group_role'] in {'secondary_products', 'product', 'collection_control'}) or
                (role == 'advance_products' and primary) or
                (role == 'load_reviews' and review_selector)):
                nominees.append({**{k: v for k, v in control.items() if k not in ('context', '_context_depth')},
                                 'predicted_role': role, 'confidence': confidence,
                                 'evidence_ids': [control['group_id']], 'model': response.get('model', 'Jev')})
    priority = {'load_reviews': 0, 'advance_products': 1, 'carousel': 2}
    nominees.sort(key=lambda item: (priority[item['predicted_role']], -item['confidence']))
    checks = []
    seen_collections = set()
    selected = []
    for control in nominees:
        collection_key = (control['predicted_role'],
                          review_selector if control['predicted_role'] == 'load_reviews' else
                          primary[0][0]['selector'] if control['predicted_role'] == 'advance_products' else
                          control['collection_selector'])
        if collection_key not in seen_collections:
            selected.append(control)
            seen_collections.add(collection_key)
    for control in selected[:max_actions]:
        if interactive and _defer_interactions.get():
            remote = control['predicted_role'] in {'load_reviews', 'advance_products'}
            checks.append({'effect': 'planned' if remote else 'unverified',
                'verification': 'Pendiente de salto y presupuesto de la estrategia' if remote else 'Control local no recorrido',
                'control': control, 'primary_selector': primary[0][0]['selector'] if primary else None,
                'review_selector': review_selector})
            continue
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
    primary = _select_roles(groups, decisions, {'primary_products'})

    categories = _select_roles(groups, decisions, {'categories', 'breadcrumb'})
    secondary = _select_roles(groups, decisions, {'secondary_products'})
    reviews = _select_roles(groups, decisions, {'reviews'})
    attributes = [{'group_id': g['id'], 'heading': g['heading'], 'text': group_values(g, soup, final_url)['text'],
                   'confidence': d['confidence']} for g, d in _select_roles(groups, decisions, {'attributes', 'product', 'offer'})]
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
