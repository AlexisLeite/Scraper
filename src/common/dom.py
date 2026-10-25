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
    """Structural discovery only; labels and paths remain source evidence."""
    groups, seen = [], set()
    roots = {}
    def add(tag, shape, members):
        aliases = []
        # A wrapper chain has one child and no text of its own. Keep the
        # innermost div/span boundary without changing the saved DOM.
        while tag.name not in ('a', 'button', 'summary', 'details') and tag.get('role') not in ('button', 'link'):
            children = tag.find_all(True, recursive=False)
            own_text = ''.join(str(text) for text in tag.find_all(string=True, recursive=False)).strip()
            if len(children) != 1 or children[0].name not in ('div', 'span') or own_text:
                break
            aliases.append({'selector': css_path(tag), 'shape': shape, 'region': _region(tag),
                'heading': _near_heading(tag), 'attrs': {key: tag.get(key) for key in USEFUL_ATTRS if tag.has_attr(key)}})
            tag = children[0]
        selector = css_path(tag)
        if selector in seen:
            existing = next(group for group in groups if group['selector'] == selector)
            existing.setdefault('structural_aliases', []).extend(alias for alias in aliases
                if alias['selector'] not in {a['selector'] for a in existing.get('structural_aliases', [])})
            return
        if aliases:
            members = tag.find_all(True, recursive=False) or [tag]
        entries = [_entry(x, base_url) for x in members if isinstance(x, Tag)]
        if not entries:
            return
        seen.add(selector)
        roots[selector] = tag
        link_entries = [_entry(x, base_url) for x in tag.find_all('a', href=True)]
        unique_links = list({(x['text'], x['url']): x for x in link_entries}.values())[:100]
        groups.append({'id': f'g{len(groups)+1}', 'shape': shape, 'selector': selector,
                       'region': _region(tag), 'heading': _near_heading(tag),
                       'text': tag.get_text(' ', strip=True)[:6000],
                       'attrs': {key: tag.get(key) for key in USEFUL_ATTRS if tag.has_attr(key)},
                       'entries': entries[:80], 'links': unique_links, 'structural_aliases': aliases,
                       'controls': [_entry(x, base_url) for x in tag.find_all(['button', 'summary', 'a'])
                                    if x.name != 'a' or x.get('role') == 'button'][:20]})
    root = soup.body or soup
    main = root.find('main')
    ordered = (list(main.find_all(True)) + [main] + [tag for tag in root.find_all(True) if tag is not main and main not in tag.parents]) if main else root.find_all(True)
    for tag in ordered:
        children = tag.find_all(True, recursive=False)
        if len(children) >= 2:
            counts = Counter(child.name for child in children)
            repeated = [child for child in children if counts[child.name] >= 2]
            if len(repeated) >= 2 and any(child.name in ('a', 'button', 'summary') or child.find('a', href=True) or child.find(['button', 'h2', 'h3']) for child in repeated):
                add(tag, 'repeated_siblings', repeated)
        if tag.name in ('nav', 'section', 'article', 'aside', 'main', 'header', 'footer', 'details') or tag.get('role') in ('navigation', 'list', 'region'):
            members = children or [tag]
            if tag.get_text(' ', strip=True) or tag.find('a', href=True):
                add(tag, 'section', members)
        if len(tag.find_all('a', href=True, recursive=False)) >= 2:
            add(tag, 'link_bar', tag.find_all('a', href=True, recursive=False))
        if tag.name in ('button', 'summary'):
            add(tag, 'control', [tag])
    # Repeated sibling controls are represented by their containing group,
    # retaining each target for nomination and audit instead of classifying each
    # button again as an independent block.
    repeated_roots = [roots[group['selector']] for group in groups if group['shape'] == 'repeated_siblings']
    compacted = []
    for group in groups:
        tag = roots[group['selector']]
        parent = next((parent for parent in tag.parents if any(parent is root for root in repeated_roots)), None)
        if group['shape'] == 'control' and parent is not None:
            controls = parent.find_all(tag.name)
            if len(controls) >= 2:
                owner = next(other for other in groups if roots[other['selector']] is parent)
                owner.setdefault('grouped_controls', []).append(group['selector'])
                continue
        compacted.append(group)
    groups = compacted
    # Collapse only nested candidates with exactly the same DOM evidence. Equal
    # text in distinct cards, regions or controls is not enough to merge them.
    def evidence(tag):
        targets = [node for node in [tag, *tag.find_all(['a', 'button', 'summary', 'img', 'input', 'select', 'textarea'])]
                   if node.name in ('a', 'button', 'summary', 'img', 'input', 'select', 'textarea')]
        return (tag.get_text(' ', strip=True), tuple(id(node) for node in targets))
    kept = []
    by_evidence = {}
    for group in sorted(groups, key=lambda group: len(list(roots[group['selector']].parents)), reverse=True):
        tag = roots[group['selector']]
        key = (group['region'], evidence(tag))
        descendants = [other for other in by_evidence.get(key, [])
                       if any(parent is tag for parent in roots[other['selector']].parents)]
        if descendants:
            representative = descendants[0]
            representative.setdefault('structural_aliases', []).append({
                key: group[key] for key in ('selector', 'shape', 'region', 'heading', 'attrs')})
            representative['structural_aliases'].extend(group.get('structural_aliases', []))
            representative.setdefault('grouped_controls', []).extend(group.get('grouped_controls', []))
        else:
            kept.append(group)
            by_evidence.setdefault(key, []).append(group)
    # Retain original discovery order, then apply the cap after wrapper removal.
    kept.sort(key=lambda group: int(group['id'][1:]))
    kept = kept[:limit]
    for index, group in enumerate(kept, 1):
        group['id'] = f'g{index}'
        group['structural_aliases'] = list({alias['selector']: alias for alias in group.get('structural_aliases', [])
            if alias['selector'] != group['selector']}.values())
    return kept


def group_values(group, soup, base_url):
    tag = soup.select_one(group['selector'])
    if tag is None:
        return {'text': '', 'links': [], 'controls': []}
    links = [_entry(x, base_url) for x in tag.find_all('a', href=True)]
    controls = [_entry(x, base_url) for x in ([tag] if tag.name in ('button', 'summary') else tag.find_all(['button', 'summary']))]
    return {'text': tag.get_text(' ', strip=True), 'links': links, 'controls': controls,
            'heading': group['heading'], 'selector': group['selector']}
