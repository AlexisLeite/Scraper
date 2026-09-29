"""Local commercial page analysis using Jev and DOM evidence."""
import base64
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import jev_connector
from extract import candidate_groups, clean_document, group_values

ROOT = Path(__file__).resolve().parent
MAX_CAPTURE_CHARS = 8_000_000
_jobs = {}
_jobs_lock = Lock()
_log = logging.getLogger('markup_studio')
app = FastAPI(title='Markup Studio', docs_url='/api/docs')

class CaptureRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    objective: str = Field(default='', max_length=1000)

class HtmlRequest(CaptureRequest):
    html: str = Field(min_length=1, max_length=MAX_CAPTURE_CHARS)

class ClassifyRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)
    classification_schema: dict = Field(alias='schema')

class GenerateRequest(BaseModel):
    instructions: str = Field(min_length=8, max_length=4000)


def validate_questions(schema):
    questions = schema.get('questions') if isinstance(schema, dict) else None
    if not isinstance(questions, dict) or not 1 <= len(questions) <= 12:
        raise ValueError("El esquema necesita entre 1 y 12 preguntas en 'questions'.")
    for key, question in questions.items():
        if not isinstance(key, str) or not key or len(key) > 64 or not isinstance(question, dict):
            raise ValueError('Cada pregunta necesita un identificador y una definición válidos.')
        kind = question.get('type')
        if kind not in ('choice', 'score', 'noul'):
            raise ValueError(f'Tipo inválido en {key}: usa choice, score o noul.')
        if not isinstance(question.get('instructions'), str) or not question['instructions'].strip():
            raise ValueError(f'Faltan instrucciones en {key}.')
        criteria = question.get('criteria')
        if kind == 'choice' and (not isinstance(criteria, dict) or not 2 <= len(criteria) <= 12):
            raise ValueError(f'{key} necesita de 2 a 12 opciones en criteria.')
        if kind == 'score' and (not isinstance(criteria, list) or not 2 <= len(criteria) <= 12):
            raise ValueError(f'{key} necesita de 2 a 12 niveles en criteria.')
    return questions


def classify(content, schema):
    return jev_connector.predict(content, validate_questions(schema))


def _valid_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError('Pega una URL http o https válida, sin credenciales.')


def _overlay_marker(page):
    page.evaluate('''() => {
      const area = innerWidth * innerHeight;
      for (const el of document.body.querySelectorAll('*')) {
        const style = getComputedStyle(el), rect = el.getBoundingClientRect();
        const box = Math.max(0, Math.min(rect.width, innerWidth)) * Math.max(0, Math.min(rect.height, innerHeight));
        if (!el.closest('header,nav,main') && (style.position === 'fixed' || el.matches('[role="dialog"],[aria-modal="true"]')) &&
            box > area * .08 && rect.height > innerHeight * .12 && (el.querySelector('button,a,input') || box > area * .5))
          el.setAttribute('data-studio-overlay', '');
      }
    }''')


def capture_page(url):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    _valid_url(url)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 900}, locale='es-UY',
                                    timezone_id='America/Montevideo', service_workers='block')
            response = page.goto(url, wait_until='domcontentloaded', timeout=30000)
            if response and response.status >= 400:
                raise RuntimeError(f'El sitio devolvió HTTP {response.status}; importa el HTML renderizado si abre en tu navegador.')
            try:
                page.wait_for_load_state('networkidle', timeout=5000)
            except PlaywrightTimeout:
                pass
            _overlay_marker(page)
            html = page.content()
            screenshot = base64.b64encode(page.screenshot(type='jpeg', quality=60)).decode('ascii')
            return html, screenshot, page.url, page.title()
        finally:
            browser.close()


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
    kind, kind_confidence = _choice(page_result.get('answers', {}).get('page_kind'), page_q['page_kind']['criteria'])
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
        answers = predict(context, questions).get('answers', {})
        for g in batch:
            role, confidence = _choice(answers.get(g['id']), ROLE_OPTIONS)
            decisions.append({**_decision(role, confidence, [g['id']]), 'group_id': g['id'],
                              'heading': g['heading'], 'region': g['region'], 'shape': g['shape']})
    return _decision(kind, kind_confidence, ['page:headings', 'page:text', 'jsonld'], 'Jev sobre DOM'), decisions


def _select_roles(groups, decisions, roles):
    lookup = {g['id']: g for g in groups}
    return [(lookup[d['group_id']], d) for d in decisions if d['value'] in roles]


def choose_primary_group(soup, url, title, groups, decisions, predict):
    candidates = [g for g in groups if g['region'] == 'main' and g['shape'] == 'repeated_siblings'
                  and len(g['entries']) >= 3 and len(group_values(g, soup, url)['links']) >= 3]
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


def _items_at(soup, selector, url):
    container = soup.select_one(selector)
    if not container:
        return []
    from extract import _entry
    children = container.find_all(True, recursive=False)
    return [(_entry(child.find('a', href=True) or child, url)['url'] or child.get_text(' ', strip=True)[:100]) for child in children]


def verify_action(url, control, primary_selector=None, review_selector=None):
    """Replay one Jev-nominated control and compare collection state before/after."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(service_workers='block')
            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            try:
                page.wait_for_load_state('networkidle', timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            _overlay_marker(page)
            page.evaluate("document.querySelectorAll('[data-studio-overlay]').forEach(el => el.remove())")
            before, _ = clean_document(page.content())
            old_url = page.url
            before_items = _items_at(before, primary_selector, url) if primary_selector else []
            before_reviews = _items_at(before, review_selector, url) if review_selector else []
            locator = page.locator(control['selector']).first
            if locator.count() == 0:
                return {'effect': 'not_found', 'verification': 'Playwright', 'control': control}
            product_nodes = page.locator(primary_selector + ' > *').count() if primary_selector else 0
            review_nodes = page.locator(review_selector + ' > *').count() if review_selector else 0
            locator.click(timeout=8000)
            try:
                page.wait_for_function('''({products, reviews, productCount, reviewCount, oldUrl}) =>
                    (products && (document.querySelector(products)?.children.length || 0) !== productCount) ||
                    (reviews && (document.querySelector(reviews)?.children.length || 0) !== reviewCount) ||
                    location.href !== oldUrl''',
                    arg={'products': primary_selector, 'reviews': review_selector,
                         'productCount': product_nodes, 'reviewCount': review_nodes,
                         'oldUrl': old_url}, timeout=9000)
            except Exception:
                pass
            page.wait_for_timeout(500)
            after, _ = clean_document(page.content())
            after_items = _items_at(after, primary_selector, page.url) if primary_selector else []
            after_reviews = _items_at(after, review_selector, page.url) if review_selector else []
            if len(after_items) > len(before_items) and set(before_items).issubset(after_items):
                effect = 'added_products'
            elif before_items and after_items and before_items != after_items:
                effect = 'replaced_products'
            elif len(after_reviews) > len(before_reviews) and set(before_reviews).issubset(after_reviews):
                effect = 'loaded_reviews'
            elif before_reviews and after_reviews and before_reviews != after_reviews:
                effect = 'replaced_reviews'
            elif page.url != old_url:
                effect = 'changed_url'
            elif before_items and after_items and before_items != after_items:
                effect = 'moved_carousel'
            else:
                effect = 'no_effect'
            return {'effect': effect, 'verification': 'Playwright', 'control': control,
                    'before_products': len(before_items), 'after_products': len(after_items),
                    'before_reviews': len(before_reviews), 'after_reviews': len(after_reviews),
                    'before_review_nodes': review_nodes,
                    'after_review_nodes': page.locator(review_selector + ' > *').count() if review_selector else 0,
                    'before_url': old_url, 'after_url': page.url}
        finally:
            browser.close()


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
    if primary:
        chosen = primary[0][0]['id']
        for decision in decisions:
            if decision['value'] == 'primary_products' and decision['group_id'] != chosen:
                decision['value'] = 'other'
                decision['verification'] = 'Reemplazada por selección de colección principal'
            elif decision['group_id'] == chosen:
                decision.update(primary[0][1])
                decision['group_id'] = chosen

    categories = _select_roles(groups, decisions, {'categories', 'breadcrumb'})
    secondary = _select_roles(groups, decisions, {'secondary_products'})
    reviews = _select_roles(groups, decisions, {'reviews'})
    review_state, review_mode = _review_decision(soup, final_url, groups, decisions, predict)
    attributes = [{'group_id': g['id'], 'heading': g['heading'], 'text': group_values(g, soup, final_url)['text'],
                   'confidence': d['confidence']} for g, d in _select_roles(groups, decisions, {'attributes'})]
    update('actions', 75, 'Comprobando acciones con Playwright')
    possible_controls = []
    role_pairs = _select_roles(groups, decisions, {'pagination', 'reviews'})
    for group in groups:
        if group['shape'] != 'control':
            continue
        tag = soup.select_one(group['selector'])
        if tag is None:
            continue
        ancestors = [(len(list(parent.parents)), decision['value']) for parent_group, decision in role_pairs
                     if (parent := soup.select_one(parent_group['selector'])) is not None and parent in tag.parents]
        own = next((d['value'] for d in decisions if d['group_id'] == group['id']), None)
        parent_role = max(ancestors)[1] if ancestors else None
        if own not in ('pagination', 'reviews') and parent_role not in ('pagination', 'reviews'):
            continue
        for item in group_values(group, soup, final_url)['controls']:
            possible_controls.append({'group_id': group['id'], 'group_role': own if own in ('pagination', 'reviews') else parent_role,
                                      'heading': group['heading'], 'region': group['region'], **item})
    seen_control_selectors = {item['selector'] for item in possible_controls}
    for group, _ in _select_roles(groups, decisions, {'pagination'}):
        for item in group_values(group, soup, final_url)['links'][:20]:
            if item['selector'] not in seen_control_selectors:
                possible_controls.append({'group_id': group['id'], 'group_role': 'pagination',
                                          'heading': group['heading'], 'region': group['region'], **item})
                seen_control_selectors.add(item['selector'])
    controls = []
    for item in possible_controls:
        question = {'control_role': {'type': 'choice',
                    'instructions': 'Clasifica la acción probable de este único control. Selecciona otro si no hay evidencia; filtros y ordenar no cargan más registros.',
                    'criteria': {'advance_products': 'Agrega o reemplaza productos en la colección principal',
                                 'load_reviews': 'Agrega más reseñas individuales a la lista actual',
                                 'carousel': 'Mueve un carrusel secundario',
                                 'other': 'Filtro, orden, otra acción o evidencia insuficiente'}}}
        answer = predict({'page_kind': page['value'], 'control': item,
                          'review_mode': review_mode, 'main_group': primary[0][0]['id'] if primary else None},
                         question).get('answers', {}).get('control_role')
        role, confidence = _choice(answer, question['control_role']['criteria'])
        if ((page['value'] == 'product' and role == 'load_reviews') or
            (page['value'] == 'listing' and primary and role == 'advance_products')):
            controls.append({**item, 'predicted_role': role, 'confidence': confidence})
    if len(controls) > 1:
        winners = []
        for start in range(0, len(controls), 9):
            batch = controls[start:start+9]
            criteria = {f'c{i}': f"Control {i}: {item['text']} (grupo {item['group_id']})" for i, item in enumerate(batch)}
            criteria['other'] = 'Ninguno tiene evidencia suficiente.'
            answer = predict({'page_kind': page['value'], 'controls': batch,
                              'task': 'Elegir una sola acción que agregue elementos a la colección actual sin filtrar, ordenar ni mover un carrusel.'},
                             {'best_control': {'type': 'choice', 'instructions': 'Elige el control que probablemente agregue registros nuevos a la colección; abstente si no hay uno claro.',
                                               'criteria': criteria}}).get('answers', {}).get('best_control')
            choice, _ = _choice(answer, criteria)
            if choice != 'other':
                winners.append(batch[int(choice[1:])])
        controls = winners
        if len(controls) > 1:
            criteria = {f'c{i}': f"{item['text']} ({item['group_id']})" for i, item in enumerate(controls)}
            criteria['other'] = 'Ninguno.'
            answer = predict({'controls': controls, 'page_kind': page['value']},
                             {'best_control': {'type': 'choice', 'instructions': 'Selecciona solo el control que agregue elementos nuevos, sin filtrar ni ordenar.',
                                               'criteria': criteria}}).get('answers', {}).get('best_control')
            choice, _ = _choice(answer, criteria)
            controls = [controls[int(choice[1:])]] if choice != 'other' else []
    checks = []
    review_candidates = []
    for group, _ in reviews:
        tag = soup.select_one(group['selector'])
        if tag is None or group['shape'] != 'repeated_siblings':
            continue
        children = tag.find_all(True, recursive=False)
        if len(children) >= 3:
            review_candidates.append(group)
    review_selector = None
    if review_candidates:
        batch = review_candidates[:9]
        criteria = {g['id']: f"Grupo {g['id']} con {len(soup.select_one(g['selector']).find_all(True, recursive=False))} registros" for g in batch}
        criteria['other'] = 'Ninguno contiene registros individuales.'
        answer = predict({'groups': batch, 'task': 'Encontrar la lista de reseñas individuales, no estadísticas ni filtros.'},
                         {'records': {'type': 'choice', 'instructions': 'Elige el grupo cuyos hijos directos son reseñas individuales publicadas.',
                                      'criteria': criteria}}).get('answers', {}).get('records')
        choice, _ = _choice(answer, criteria)
        if choice != 'other':
            review_selector = next(g['selector'] for g in batch if g['id'] == choice)
    if captured is None:
        for control in controls[:4]:
            try:
                checks.append(verify(final_url, control,
                                     primary[0][0]['selector'] if primary else None,
                                     review_selector))
            except Exception as exc:
                checks.append({'effect': 'unverified', 'verification': 'Playwright', 'control': control,
                               'reason': type(exc).__name__})
    elif controls:
        checks = [{'effect': 'unverified', 'verification': 'HTML importado; interacción no disponible', 'control': x} for x in controls[:4]]
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


def generate_schema(instructions):
    codex = shutil.which('codex.cmd' if os.name == 'nt' else 'codex')
    if not codex:
        raise RuntimeError('Codex CLI no está instalado o no aparece en PATH.')
    prompt = ("Genera un esquema de clasificación para Jev según las instrucciones del usuario. "
              "Responde únicamente JSON válido, sin markdown: un objeto con clave 'questions', "
              "que contiene un mapa de IDs cortos a preguntas. Cada pregunta tiene 'type' "
              "('choice', 'score' o 'noul'), 'instructions' y, para choice, 'criteria' como "
              "objeto de 2 a 12 etiquetas con descripciones; para score, una lista ordenada "
              "de 2 a 12 niveles. Usa noul para preguntas de sí/no. Crea de 1 a 5 preguntas. "
              "No ejecutes herramientas ni escribas archivos.\n\nInstrucciones del usuario:\n" + instructions)
    with tempfile.TemporaryDirectory(prefix='jev-schema-') as directory:
        output = Path(directory) / 'schema.json'
        command = [codex, 'exec', '--ephemeral', '--sandbox', 'read-only', '--skip-git-repo-check',
                   '--output-last-message', str(output), '-']
        try:
            result = subprocess.run(command, input=prompt, capture_output=True, text=True,
                                    encoding='utf-8', cwd=directory, timeout=150, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError('Codex CLI tardó demasiado en generar el esquema.') from exc
        if result.returncode != 0 or not output.exists():
            raise RuntimeError('Codex CLI no pudo generar el esquema. Revisa su sesión y conexión.')
        raw = output.read_text(encoding='utf-8').strip()
        if raw.startswith('```'):
            raw = raw.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        try:
            schema = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError('Codex CLI devolvió un esquema inválido.') from exc
        validate_questions(schema)
        return schema


def _update_job(job_id, **changes):
    with _jobs_lock:
        _jobs[job_id].update(changes)


def _run_job(job_id, url, objective, captured=None):
    try:
        result = inspect_url(url, objective, lambda phase, percent, message:
                             _update_job(job_id, phase=phase, percent=percent, message=message), captured)
        _update_job(job_id, status='done', phase='done', percent=100, message='Análisis completo', result=result)
    except (ValueError, RuntimeError) as exc:
        _update_job(job_id, status='error', message=str(exc))
    except Exception:
        _log.exception('Page analysis failed')
        _update_job(job_id, status='error', message='No se pudo analizar la página; revisa el servidor.')

@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/app.css')
def styles():
    return FileResponse(ROOT / 'app.css', media_type='text/css')

@app.get('/app.js')
def script():
    return FileResponse(ROOT / 'app.js', media_type='text/javascript')

@app.get('/api/health')
def health():
    return {'codex_cli': bool(shutil.which('codex.cmd' if os.name == 'nt' else 'codex')),
            'jev': {'configured': jev_connector.configured()}}

@app.post('/api/capture')
async def capture(request: CaptureRequest):
    try:
        return await run_in_threadpool(inspect_url, request.url, request.objective)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _start_job(url, objective, captured=None):
    with _jobs_lock:
        now = time.time()
        for key in list(_jobs):
            if _jobs[key]['status'] != 'running' and now - _jobs[key]['created'] > 900:
                del _jobs[key]
        if sum(job['status'] == 'running' for job in _jobs.values()) >= 3:
            raise HTTPException(503, 'Hay demasiados análisis activos; espera a que termine uno.')
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {'status': 'running', 'phase': 'queued', 'percent': 0,
                         'message': 'En cola', 'created': now, 'result': None}
    Thread(target=_run_job, args=(job_id, url, objective, captured), daemon=True).start()
    return {'job_id': job_id}

@app.post('/api/analyze', status_code=202)
def start_analysis(request: CaptureRequest):
    _valid_url(request.url)
    return _start_job(request.url, request.objective)

@app.post('/api/analyze-html', status_code=202)
def start_html_analysis(request: HtmlRequest):
    _valid_url(request.url)
    return _start_job(request.url, request.objective, (request.html, '', request.url, ''))

@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, 'Análisis no encontrado o vencido.')
        return {key: value for key, value in job.items() if key != 'created'}

@app.post('/api/classify')
async def classify_endpoint(request: ClassifyRequest):
    try:
        return await run_in_threadpool(classify, request.content, request.classification_schema)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

@app.post('/api/generate-schema')
async def generate_endpoint(request: GenerateRequest):
    try:
        return await run_in_threadpool(generate_schema, request.instructions)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc

if __name__ == '__main__':
    import uvicorn
    try:
        result = subprocess.run(['tailscale', 'ip', '-4'], capture_output=True, text=True,
                                timeout=5, check=True)
        host = str(ipaddress.IPv4Address(result.stdout.strip()))
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired,
            ipaddress.AddressValueError) as exc:
        raise SystemExit('Tailscale no tiene una dirección IPv4 disponible.') from exc
    uvicorn.run(app, host=host, port=8787)
