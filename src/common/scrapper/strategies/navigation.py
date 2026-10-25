"""Bounded observation of navigation controls, without clicks or document navigation."""
from __future__ import annotations

import base64
import hashlib
import time

from ...dom import css_path
from bs4 import BeautifulSoup

REGION = 'header, nav, [role="navigation"], [role="menubar"]'
CONTROLS = ', '.join(f'{region} {control}' for region in REGION.split(', ') for control in (
    'button[aria-expanded]', '[aria-haspopup="true"]', '[aria-haspopup="menu"]', '[role="menuitem"][aria-expanded]'))


def controls(html):
    soup = BeautifulSoup(html, 'html.parser')
    result = []
    for tag in soup.select(CONTROLS):
        result.append({'selector': css_path(tag), 'label': tag.get_text(' ', strip=True) or tag.get('aria-label', ''),
                       'depth': len(list(tag.parents))})
    return sorted(result, key=lambda item: item['depth'])


def destinations(html, base):
    from .semantic import url_value
    soup = BeautifulSoup(html, 'html.parser')
    return {url for tag in soup.select('a[href]') if (url := url_value(tag['href'], base))}


def observe_navigation(page, capture, *, max_controls=32, timeout_seconds=30, on_snapshot=None):
    """Return snapshots plus explicit coverage. Hovers never consume a navigation hop.

    These observations expose navigation in the same document, not collection
    expansions. No clicks, submissions, or navigation links are executed.
    """
    report = {'supported': hasattr(page, 'hover'), 'attempted': 0, 'observed': 0,
              'failed': 0, 'skipped_invisible': 0, 'omitted_controls': 0, 'blocked_navigations': [],
              'max_controls': max_controls, 'timeout_seconds': timeout_seconds,
              'complete': False, 'initial_url': capture.final_uri}
    if not report['supported']:
        report['reason'] = 'Snapshot-only input; interactive navigation was not observed.'
        return [capture], [], report
    from ..models import PageCapture
    states, actions, tried = [capture], [], set()
    invisible, active_region = set(), None
    deadline = time.monotonic() + timeout_seconds
    initial_url = page.url
    # Visual overlays are suppressed only while observing menus. No consent or
    # other state-changing button is clicked. Preserve the initial capture intact.
    overlay_styles = page.locator('[data-studio-overlay]').evaluate_all('''els => els.map(el => {
        const style = el.getAttribute('style'); el.style.setProperty('display','none','important');
        return {selector: '', style};
    })''')
    # Restore the marked overlays after observation; the original capture is intact.
    report['temporarily_hidden_overlays'] = len(overlay_styles)
    def guard(route):
        if route.request.is_navigation_request():
            report['blocked_navigations'].append(route.request.url)
            route.fulfill(status=204, body='')
        else:
            route.continue_()
    page.context.route('**/*', guard)
    try:
        while len(tried) < max_controls and time.monotonic() < deadline:
            candidates = controls(page.content())
            pending = [item for item in candidates if item['selector'] not in tried]
            if active_region:
                pending.sort(key=lambda item:not item['selector'].startswith(active_region+' > '))
            chosen = None
            for candidate in pending:
                locator = page.locator(candidate['selector'])
                if locator.count()==1 and locator.is_visible():
                    chosen = candidate
                    break
                invisible.add(candidate['selector'])
            if chosen is None:
                break
            item = chosen
            tried.add(item['selector'])
            if not active_region or not item['selector'].startswith(active_region+' > '):
                active_region = item['selector'].rsplit(' > ',1)[0]
            target = page.locator(item['selector'])
            report['attempted'] += 1
            before = PageCapture(capture.requested_uri,page.url,page.title(),page.content(),interactive=True)
            before_urls = destinations(before.html, initial_url)
            action = {'kind': 'navigation_observation', 'method': 'hover', 'control': item,
                      'before_url': initial_url, 'status': 'unchanged', 'added_links': [], 'removed_links': []}
            try:
                # There is exactly one hover attempt per control, including failures.
                target.hover(timeout=700)
                # Wait for a bounded quiet DOM window, including async lazy menus.
                page.evaluate('''() => {window.__semanticObservationStart=window.__semanticMutationAt=performance.now();
                    window.__semanticObserver?.disconnect();
                    window.__semanticObserver=new MutationObserver(()=>window.__semanticMutationAt=performance.now());
                    window.__semanticObserver.observe(document.documentElement,{childList:true,subtree:true});}''')
                page.wait_for_function('performance.now()-window.__semanticObservationStart>400 && performance.now()-window.__semanticMutationAt>180', timeout=1300)
                html = page.content()
                if page.url != initial_url:
                    raise RuntimeError('Document URL changed while observing navigation.')
                after_urls = destinations(html, initial_url)
                action.update(added_links=sorted(after_urls-before_urls), removed_links=sorted(before_urls-after_urls))
                # A different control can expose groups without adding a destination.
                signature = lambda value: hashlib.sha256(BeautifulSoup(value,'html.parser').get_text(' ',strip=True).encode()).hexdigest()
                changed = after_urls != before_urls or signature(html) != signature(before.html)
                if changed:
                    shot = base64.b64encode(page.screenshot(type='jpeg', quality=45, full_page=False)).decode()
                    after = PageCapture(capture.requested_uri, page.url, page.title(), html, shot, interactive=True)
                    if on_snapshot:
                        on_snapshot(after)
                    states.append(after)
                    action.update(status='observed', captures={
                        'before': {'url':before.final_uri,'title':before.title,'html':before.html,'screenshot_base64':before.screenshot_base64},
                        'after': {'url':after.final_uri,'title':after.title,'html':after.html,'screenshot_base64':after.screenshot_base64}})
                    report['observed'] += 1
                actions.append(action)
            except Exception as exc:
                report['failed'] += 1
                action.update(status='failed',error=f'{type(exc).__name__}: {exc}')
                actions.append(action)
        remaining = [item for item in controls(page.content()) if item['selector'] not in tried]
        eligible = [item for item in remaining if page.locator(item['selector']).count()==1 and page.locator(item['selector']).is_visible()]
        report['omitted_controls'] = len(eligible)
        report['skipped_invisible'] = len(invisible-tried)
        report['complete'] = not eligible and not report['failed'] and not report['blocked_navigations']
        report['limit_reached'] = bool(eligible)
        report['final_url'] = page.url
    finally:
        page.context.unroute('**/*', guard)
        page.evaluate('''styles => {document.querySelectorAll('[data-studio-overlay]').forEach((el,i)=>{
            if(!styles[i])return; if(styles[i].style===null)el.removeAttribute('style');else el.setAttribute('style',styles[i].style);
        });window.__semanticObserver?.disconnect();delete window.__semanticObserver;delete window.__semanticMutationAt;}''', overlay_styles)
    return states, actions, report
