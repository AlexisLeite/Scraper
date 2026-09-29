"""Build DOM evidence and structural candidates without assigning commercial roles."""
import json
from collections import Counter
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, Tag

SKIP_TAGS = ('script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe')
USEFUL_ATTRS = ('id', 'class', 'role', 'itemprop', 'itemtype', 'aria-label', 'data-testid', 'rel', 'name')


def _walk(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, dict):
        yield value
        for item in value.values():
            if isinstance(item, (dict, list)):
                yield from _walk(item)


def clean_document(html):
    soup = BeautifulSoup(html, 'html.parser')
    structured = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            structured.extend(_walk(json.loads(tag.string or tag.get_text())))
        except (ValueError, TypeError):
            pass
    for tag in list(soup.find_all(SKIP_TAGS)):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.parent is None:
            continue
        if tag.has_attr('hidden') or tag.get('aria-hidden') == 'true' or tag.has_attr('data-studio-overlay'):
            tag.decompose()
            continue
        if tag.name == 'details':
            tag['open'] = ''
        for attr in list(tag.attrs):
            if attr == 'style' or attr.lower().startswith('on'):
                del tag[attr]
    return soup, structured


def css_path(tag):
    parts = []
    while isinstance(tag, Tag) and tag.name not in ('[document]', 'html'):
        if tag.get('id') and (tag['id'][0].isalpha() or tag['id'][0] == '_') and all(c.isalnum() or c in '_-' for c in tag['id']):
            parts.append('#' + tag['id'])
            break
        siblings = [x for x in tag.parent.find_all(tag.name, recursive=False)] if isinstance(tag.parent, Tag) else []
        parts.append(f'{tag.name}:nth-of-type({siblings.index(tag)+1})' if len(siblings) > 1 else tag.name)
        tag = tag.parent
    return ' > '.join(reversed(parts))


def _near_heading(tag):
    for node in [tag, *list(tag.parents)[:3]]:
        if not isinstance(node, Tag):
            continue
        heading = node.find(['h1', 'h2', 'h3', 'h4'], recursive=False)
        if heading:
            return heading.get_text(' ', strip=True)[:180]
        previous = node.find_previous(['h1', 'h2', 'h3', 'h4'])
        if previous:
            return previous.get_text(' ', strip=True)[:180]
    return ''


def _region(tag):
    for parent in [tag, *tag.parents]:
        if isinstance(parent, Tag) and parent.name in ('main', 'article', 'nav', 'header', 'footer', 'aside'):
            return parent.name
    return 'body'


def _entry(tag, base_url):
    label = tag.get_text(' ', strip=True)[:240]
    if not label:
        label = tag.get('aria-label') or tag.get('title') or ''
    href = tag.get('href')
    url = urljoin(base_url, href) if href else None
    if url and urlparse(url).scheme not in ('http', 'https'):
        url = None
    return {'text': label, 'url': url, 'selector': css_path(tag),
            'attrs': {key: tag.get(key) for key in USEFUL_ATTRS if tag.has_attr(key)}}


def candidate_groups(soup, base_url, title='', limit=80):
    """Structural discovery only; labels and paths remain evidence for Jev."""
    groups, seen = [], set()
    def add(tag, shape, members):
        selector = css_path(tag)
        if selector in seen or len(groups) >= limit:
            return
        entries = [_entry(x, base_url) for x in members if isinstance(x, Tag)]
        if not entries:
            return
        seen.add(selector)
        link_entries = [_entry(x, base_url) for x in tag.find_all('a', href=True)]
        unique_links = list({(x['text'], x['url']): x for x in link_entries}.values())[:100]
        groups.append({'id': f'g{len(groups)+1}', 'shape': shape, 'selector': selector,
                       'region': _region(tag), 'heading': _near_heading(tag),
                       'text': tag.get_text(' ', strip=True)[:6000],
                       'attrs': {key: tag.get(key) for key in USEFUL_ATTRS if tag.has_attr(key)},
                       'entries': entries[:80], 'links': unique_links,
                       'controls': [_entry(x, base_url) for x in tag.find_all(['button', 'summary', 'a'])
                                    if x.name != 'a' or x.get('role') == 'button'][:20]})
    root = soup.body or soup
    main = root.find('main')
    ordered = (list(main.find_all(True)) + [main] + [tag for tag in root.find_all(True) if tag is not main and main not in tag.parents]) if main else root.find_all(True)
    for tag in ordered:
        if len(groups) >= limit:
            break
        children = tag.find_all(True, recursive=False)
        if len(children) >= 2:
            counts = Counter(child.name for child in children)
            repeated = [child for child in children if counts[child.name] >= 2]
            if len(repeated) >= 2 and any(child.name == 'a' or child.find('a', href=True) or child.find(['button', 'h2', 'h3']) for child in repeated):
                add(tag, 'repeated_siblings', repeated)
        if tag.name in ('nav', 'section', 'article', 'aside', 'main') or tag.get('role') in ('navigation', 'list', 'region'):
            members = children or [tag]
            if tag.get_text(' ', strip=True) or tag.find('a', href=True):
                add(tag, 'section', members)
        if len(tag.find_all('a', href=True, recursive=False)) >= 2:
            add(tag, 'link_bar', tag.find_all('a', href=True, recursive=False))
        if tag.name in ('button', 'summary'):
            add(tag, 'control', [tag])
    return groups


def group_values(group, soup, base_url):
    tag = soup.select_one(group['selector'])
    if tag is None:
        return {'text': '', 'links': [], 'controls': []}
    links = [_entry(x, base_url) for x in tag.find_all('a', href=True)]
    controls = [_entry(x, base_url) for x in ([tag] if tag.name in ('button', 'summary') else tag.find_all(['button', 'summary']))]
    return {'text': tag.get_text(' ', strip=True), 'links': links, 'controls': controls,
            'heading': group['heading'], 'selector': group['selector']}
