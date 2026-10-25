"""Render URLs and verify DOM interactions with Playwright."""
import base64
import os
from urllib.parse import urlparse, urldefrag
from collections import Counter
import hashlib
import time
from contextlib import contextmanager

from .dom import clean_document, group_values, _entry

class NonPageResponse(RuntimeError):
    retryable = False
    phase = 'excluded'


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


@contextmanager
def browser_session(endpoint=None):
    if endpoint and os.environ.get('SCRAPER_DIRECT_PLAYWRIGHT') == '1':
        from ..persistent.browser_connection import SharedBrowserConnection
        with SharedBrowserConnection(endpoint) as browser:
            yield browser
    else:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect(endpoint,timeout=15000) if endpoint else p.chromium.launch(
                headless=True, **({'channel': 'chrome', 'args': ['--disable-blink-features=AutomationControlled'],
                                  'ignore_default_args': ['--enable-automation']}
                                 if os.environ.get('SCRAPER_DESKTOP_PROFILE') == '1' else {}))
            try:yield browser
            finally:browser.close()


@contextmanager
def loaded_page(url):
    """Own an isolated context and yield a loaded, live Playwright page."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    _valid_url(url)
    with browser_session(os.environ.get('SCRAPER_BROWSER_ENDPOINT')) as browser:
        endpoint = os.environ.get('SCRAPER_BROWSER_ENDPOINT')
        context = None
        try:
            context = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='es-UY',
                                          timezone_id='America/Montevideo', service_workers='block',
                                          **({'storage_state': os.environ['SCRAPER_BROWSER_STORAGE_STATE']}
                                             if os.environ.get('SCRAPER_BROWSER_STORAGE_STATE') else {}))
            token = os.environ.get('SCRAPER_BROWSER_TOKEN')
            if endpoint and token:
                import json
                context.add_init_script(f"(() => {{const mark=()=>console.timeStamp({json.dumps(token)});mark();setInterval(mark,500);}})()")
            page = context.new_page()
            if os.environ.get('SCRAPER_DESKTOP_PROFILE') == '1':
                from .browser_profile import configure_desktop_page
                configure_desktop_page(context, page)
            response = page.goto(url, wait_until='domcontentloaded',
                                 timeout=90000 if os.environ.get('SCRAPER_DESKTOP_PROFILE') == '1' else 30000)
            if response and response.status < 400:
                content_type = response.headers.get('content-type','').split(';')[0].strip().lower()
                if content_type and content_type not in ('text/html','application/xhtml+xml'):
                    raise NonPageResponse(f'Recurso excluido: el servidor respondió {content_type}, no una página HTML.')
            if response and response.status >= 400:
                raise RuntimeError(f'El sitio devolvió HTTP {response.status}; importa el HTML renderizado si abre en tu navegador.')
            try:
                page.wait_for_load_state('networkidle', timeout=5000)
            except PlaywrightTimeout:
                pass
            # Sparse shells often redirect or hydrate after the network goes idle.
            # Wait for semantic content only on those shells, with a fixed bound.
            try:
                page.wait_for_function("""() => !!document.querySelector('main, article, [role="main"], [itemscope], script[type="application/ld+json"]') || (document.body?.innerText.length || 0) > 500""", timeout=15000)
            except PlaywrightTimeout:
                pass
            # Client-side redirects can replace the execution context after
            # DOMContentLoaded. Retry this read-only observation, never a click.
            from playwright.sync_api import Error as PlaywrightError
            for observation in range(3):
                try:
                    _overlay_marker(page)
                    break
                except PlaywrightError as exc:
                    if observation == 2 or not any(text in str(exc) for text in
                            ('Execution context was destroyed', 'Cannot find context')):
                        raise
                    page.wait_for_load_state('domcontentloaded', timeout=10000)
                    page.wait_for_timeout(500)
            yield page
        finally:
            # Dispose this activity context before dropping its connection.
            # Never close another activity's browser or context.
            try:context.close()
            except Exception:pass


def capture_loaded_page(page):
    """Snapshot without closing or reloading the strategy's page."""
    return {'html': page.content(), 'url': page.url, 'title': page.title(),
            'screenshot_base64': base64.b64encode(page.screenshot(
                type='jpeg', quality=60, full_page=True)).decode('ascii')}


def capture_page(url):
    with loaded_page(url) as page:
        snapshot = capture_loaded_page(page)
        return snapshot['html'], snapshot['screenshot_base64'], snapshot['url'], snapshot['title']


def _items_at(soup, selector, url, *, reviews=False):
    """Stable identity within a model-selected collection; controls are not records."""
    container = soup.select_one(selector)
    if not container:
        return []
    identities = []
    for child in container.find_all(True, recursive=False):
        if child.name in ('button', 'summary') or child.get('role') == 'button':
            continue
        if reviews:
            # Links and helpful-vote controls are not the identity of a review.
            clone = clean_document(str(child))[0]
            for control in clone.select('button, select, input, [role="button"]'):
                control.decompose()
            text = ' '.join(clone.get_text(' ', strip=True).split())
            if text:
                identities.append(hashlib.sha256(text.encode()).hexdigest())
        else:
            anchor = child if child.name == 'a' and child.get('href') else child.find('a', href=True)
            identity = _entry(anchor, url)['url'] if anchor is not None else None
            if identity:
                identities.append(urldefrag(identity)[0])
    return identities


def collection_change(before, after, added, replaced):
    old, new = Counter(before), Counter(after)
    if not old or not new:
        return None
    # Membership, including multiplicity, matters; order and display text do not.
    if old == new:
        return None
    if sum(new.values()) > sum(old.values()) and not (old - new):
        return added
    return replaced


def verify_action(url, control, primary_selector=None, review_selector=None, *, capture_evidence=False, timeout_ms=12000):
    """Compatibility entry point for standalone verification."""
    with loaded_page(url) as page:
        return verify_loaded_action(page, control, primary_selector, review_selector,
                                    capture_evidence=capture_evidence, timeout_ms=timeout_ms)


def verify_loaded_action(page, control, primary_selector=None, review_selector=None, *, capture_evidence=False, timeout_ms=12000):
    """Observe one click on the strategy's live page, preserving its session."""
    page.wait_for_timeout(1000)
    _overlay_marker(page)
    page.evaluate("document.querySelectorAll('[data-studio-overlay]').forEach(el => el.remove())")
    def snapshot():
        return capture_loaded_page(page)
    def unavailable(effect, reason):
        result = {'effect': effect, 'verification': 'Playwright', 'control': control, 'reason': reason}
        if capture_evidence:
            capture = snapshot()
            result['captures'] = {'before': capture, 'after': capture}
        return result
    locator = page.locator(control['selector']).first
    if locator.count() == 0:
        return unavailable('not_found', 'Control no encontrado.')
    if not locator.is_visible():
        return unavailable('not_interactable', 'El control no está visible.')
    # Scrolling can initialize lazy scripts. Wait before the single verified click.
    locator.scroll_into_view_if_needed(timeout=8000)
    try:
        page.wait_for_load_state('networkidle', timeout=2500)
    except Exception:
        pass
    page.wait_for_timeout(1500)
    if not locator.is_enabled():
        return unavailable('not_interactable', 'El control está deshabilitado.')
    before_capture = snapshot() if capture_evidence else None
    before, _ = clean_document(page.content())
    old_url = page.url
    before_items = _items_at(before, primary_selector, old_url) if primary_selector else []
    before_reviews = _items_at(before, review_selector, old_url, reviews=True) if review_selector else []
    product_nodes = page.locator(primary_selector + ' > *').count() if primary_selector else 0
    review_nodes = page.locator(review_selector + ' > *').count() if review_selector else 0
    carousel_selector = control.get('collection_selector') if control.get('predicted_role') == 'carousel' else None
    def region_signature(selector):
        if not selector:
            return None
        return page.evaluate('''selector => {
            const node = document.querySelector(selector);
            if (!node) return null;
            const origin = node.getBoundingClientRect();
            return [...node.children].map(child => {
                const rect = child.getBoundingClientRect();
                return [child.textContent, rect.x - origin.x, rect.y - origin.y, child.getAttribute('aria-hidden'),
                    getComputedStyle(child).transform];
            });
        }''', selector)
    before_carousel = region_signature(carousel_selector)
    network = []
    pending = set()
    requests_by_object = {}
    last_network_event = time.monotonic()
    def request_started(request):
        nonlocal last_network_event
        if request.resource_type not in ('xhr', 'fetch', 'document'):
            return
        item = {'id': len(network) + 1, 'method': request.method,
                'type': request.resource_type,
                'url': urlparse(request.url)._replace(query='', fragment='').geturl(),
                'status': 'pending', 'http_status': None, '_started': time.monotonic()}
        network.append(item)
        requests_by_object[request] = item
        pending.add(request)
        last_network_event = time.monotonic()
    def response_received(response):
        item = requests_by_object.get(response.request)
        if item is not None:
            item['http_status'] = response.status
    def request_completed(request, failed=False):
        nonlocal last_network_event
        item = requests_by_object.get(request)
        if item is not None:
            item['status'] = ('failed' if failed else 'http_error' if
                item['http_status'] is not None and item['http_status'] >= 400 else 'finished')
            item['duration_ms'] = round((time.monotonic() - item.pop('_started')) * 1000)
            if failed:
                item['failure'] = request.failure
            pending.discard(request)
            last_network_event = time.monotonic()
    page.on('request', request_started)
    page.on('response', response_received)
    page.on('requestfinished', request_completed)
    def request_failed(request):
        request_completed(request, failed=True)
    page.on('requestfailed', request_failed)
    try:
        locator.click(timeout=8000)
        deadline = time.monotonic() + timeout_ms / 1000
        def current_items(selector, *, reviews=False):
            if not selector or not page.locator(selector).count():
                return []
            fragment, _ = clean_document('<section id="watched-collection">' +
                page.locator(selector).first.inner_html() + '</section>')
            return _items_at(fragment, '#watched-collection', page.url, reviews=reviews)
        while time.monotonic() < deadline:
            products_now = current_items(primary_selector)
            reviews_now = current_items(review_selector, reviews=True)
            changed = (page.url != old_url or
                collection_change(before_items, products_now, 'added', 'replaced') or
                collection_change(before_reviews, reviews_now, 'added', 'replaced') or
                (carousel_selector and before_carousel != region_signature(carousel_selector)))
            network_quiet = not pending and time.monotonic() - last_network_event >= 0.5
            if changed and network_quiet:
                break
            page.wait_for_timeout(300)
        page.wait_for_timeout(500)
        after_capture = snapshot() if capture_evidence else None
        after, _ = clean_document(page.content())
        after_items = _items_at(after, primary_selector, page.url) if primary_selector else []
        after_reviews = _items_at(after, review_selector, page.url, reviews=True) if review_selector else []
        product_effect = collection_change(before_items, after_items, 'added_products', 'replaced_products')
        review_effect = collection_change(before_reviews, after_reviews, 'loaded_reviews', 'replaced_reviews')
        if product_effect:
            effect = product_effect
        elif review_effect:
            effect = review_effect
        elif page.url != old_url:
            effect = 'changed_url'
        elif carousel_selector and before_carousel != region_signature(carousel_selector):
            effect = 'moved_carousel'
        else:
            effect = 'no_effect'
        def exact_values(document, selector, base_url):
            return group_values({'selector': selector, 'heading': ''}, document, base_url) if selector else None
        result = {'effect': effect, 'verification': 'Playwright', 'control': control,
                'before_products': len(before_items), 'after_products': len(after_items),
                'before_reviews': len(before_reviews), 'after_reviews': len(after_reviews),
                'before_product_nodes': product_nodes,
                'after_product_nodes': page.locator(primary_selector + ' > *').count() if primary_selector else 0,
                'product_identities': {'before': before_items, 'after': after_items,
                    'added': list((Counter(after_items) - Counter(before_items)).elements()),
                    'removed': list((Counter(before_items) - Counter(after_items)).elements())},
                'before_review_nodes': review_nodes,
                'after_review_nodes': page.locator(review_selector + ' > *').count() if review_selector else 0,
                'before_url': old_url, 'after_url': page.url,
                'network': [{k: v for k, v in item.items() if k != '_started'} for item in network],
                'network_settled': not pending, 'observation_timeout_ms': timeout_ms,
                'before_content': {'products': exact_values(before, primary_selector, old_url),
                                   'reviews': exact_values(before, review_selector, old_url)},
                'after_content': {'products': exact_values(after, primary_selector, page.url),
                                  'reviews': exact_values(after, review_selector, page.url)}}
        if effect == 'no_effect':
            result['reason'] = f'No se observó cambio de identidades en {timeout_ms} ms.'
        if pending:
            result['network_note'] = 'La observación terminó con solicitudes pendientes.'
        if capture_evidence:
            result['captures'] = {'before': before_capture, 'after': after_capture}
        return result
    finally:
        page.remove_listener('request', request_started)
        page.remove_listener('response', response_received)
        page.remove_listener('requestfinished', request_completed)
        page.remove_listener('requestfailed', request_failed)
