"""Unclassified link discovery. Admission belongs to the queue, not a model."""
from bs4 import BeautifulSoup
from ..page_urls import is_page_url
from urllib.parse import urljoin, urlsplit


def discover_links(capture, api):
    soup = BeautifulSoup(capture.html, 'html.parser')
    base_tag = soup.find('base', href=True)
    base = urljoin(capture.final_uri,base_tag['href']) if base_tag else capture.final_uri
    seen = set()
    for tag in soup.select('a[href], area[href], link[href]'):
        if tag.has_attr('download'):
            continue
        if tag.name == 'link':
            relations = set(tag.get('rel',[]))
            if not relations & {'next','prev','canonical','alternate'}:
                continue
            if tag.get('as') or tag.get('type','').split(';')[0].lower() not in ('','text/html','application/xhtml+xml'):
                continue
        try:
            target = urljoin(base,tag['href'])
            if not is_page_url(target) or target in seen:
                continue
        except ValueError:
            continue
        seen.add(target)
        api.enqueue(target,via='dom-links')
    return seen
