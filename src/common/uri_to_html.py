"""Render URLs and verify DOM interactions with Playwright."""
import base64
from urllib.parse import urlparse

from .dom import clean_document, _entry

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


def _items_at(soup, selector, url):
    container = soup.select_one(selector)
    if not container:
        return []
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
