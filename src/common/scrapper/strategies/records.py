"""Generic collection membership and navigation hierarchy from document structure."""
from urllib.parse import urldefrag
from bs4 import Tag
from ...dom import css_path
from .navigation import REGION


def source_path(tag):
    return tag.get('data-semantic-origin') or css_path(tag)


def record_image(card, href, base):
    """Prefer the image linking to the record over unrelated badges/icons.

    aria-hidden describes accessibility, not visual visibility. Read images
    from the original card, even when its text representation omits them.
    """
    from .semantic import url_value
    if card is None:
        return None
    images = [image for image in card.select('img[src]') if url_value(image.get('src'), base)
              and not image.has_attr('hidden') and not image.find_parent(attrs={'hidden': True})]
    def score(image):
        anchor = image.find_parent('a', href=True)
        destination = url_value(anchor.get('href'), base) if anchor else None
        return (bool(destination and urldefrag(destination)[0] == urldefrag(href)[0]),
                bool(image.get('alt', '').strip()))
    return max(images, key=score) if images else None


def collection_members(soup, documents, references, base):
    from .semantic import nodes, schema_term, schema_vocabulary, url_value
    def resolved(value):
        if isinstance(value, dict):
            return {**references.get(value.get('@id'), {}), **value}
        return value
    def destination(value):
        if isinstance(value, str):
            return url_value(value, base)
        if isinstance(value, dict):
            return url_value(value.get('url') or value.get('@id'), base)
    for selector, data in documents:
        for node in nodes(data):
            types = node.get('@type', [])
            types = types if isinstance(types,list) else [types]
            context=node.get('@context',data.get('@context') if isinstance(data,dict) else None)
            if not any(schema_term(t,allow_bare=schema_vocabulary(context)) == 'ItemList' for t in types):
                continue
            values = node.get('itemListElement', [])
            values = values if isinstance(values,list) else [values]
            members = []
            for position, member in enumerate(values, 1):
                member = resolved(member)
                item = resolved(member.get('item',member)) if isinstance(member,dict) else member
                href = destination(item) or destination(member)
                if href:
                    members.append((urldefrag(href)[0], member, item, position))
            targets = {m[0] for m in members}
            anchors = {}
            for anchor in soup.select('a[href]'):
                href = url_value(anchor.get('href'),base)
                if href and urldefrag(href)[0] in targets:
                    anchors.setdefault(urldefrag(href)[0], []).append(anchor)
            for href, member, item, position in members:
                choices = anchors.get(href, [])
                anchor = next((a for a in choices if a.find(['h1','h2','h3','h4','h5','h6'])), choices[0] if choices else None)
                card = anchor
                if anchor:
                    for parent in anchor.parents:
                        if not isinstance(parent, Tag) or parent.name in ('html','body','main','nav','header','footer') or parent.get('role') in ('main','navigation'):
                            break
                        linked = {urldefrag(u)[0] for a in parent.select('a[href]') if (u:=url_value(a.get('href'),base)) and urldefrag(u)[0] in targets}
                        if linked != {href}:
                            break
                        # A whole collection with a single member is not a card.
                        if len(targets)==1 and not parent.find(['h1','h2','h3','h4','h5','h6']):
                            break
                        card = parent
                heading = card.find(['h1','h2','h3','h4','h5','h6']) if card else None
                declared_name = item.get('name') if isinstance(item,dict) else None
                if not declared_name and isinstance(member,dict):
                    declared_name = member.get('name')
                name = heading.get_text(' ',strip=True) if heading else declared_name or (anchor.get_text(' ',strip=True) if anchor else href)
                image = record_image(card, href, base)
                description = '\n'.join(p.get_text(' ',strip=True) for p in card.select('p') if p.get_text(' ',strip=True)) if card else ''
                yield {'url':href,'name':name,'selector':source_path(card) if card else selector,
                       'heading_selector':source_path(heading) if heading else selector,
                       'image':url_value(image.get('src'),base) if image else None,
                       'image_selector':source_path(image) if image else selector,'description':description,
                       'declared':member,'position':member.get('position',position) if isinstance(member,dict) else position,
                       'collection_selector':selector,'dom_html':str(card)[:24000] if card else None,
                       'dom_html_truncated':bool(card and len(str(card))>24000)}


def navigation_groups(soup, base):
    from .semantic import url_value
    selector = 'button[aria-expanded], [aria-haspopup="true"], [aria-haspopup="menu"], [role="menuitem"][aria-expanded]'
    region_tags = {id(tag) for tag in soup.select(REGION)}
    for button in soup.select(selector):
        if not any(id(parent) in region_tags for parent in button.parents):
            continue
        group = None
        controlled = button.get('aria-controls','').split()
        if controlled:
            group = soup.find(id=controlled[0])
        if group is None:
            group = button.find_parent('li')
        if group is None:
            # Custom navigation components often use a control followed by a panel.
            for parent in list(button.parents)[:3]:
                controls = parent.select(selector)
                if len(controls)==1 and parent.select_one('a[href]'):
                    group=parent;break
        if group is None or not group.select_one('a[href]'):
            continue
        name = button.get_text(' ',strip=True) or button.get('aria-label','')
        if not name:
            continue
        parents = []
        for ancestor in reversed(list(button.parents)):
            if ancestor.name != 'li':
                continue
            owner = next((b for b in ancestor.select(selector) if b.find_parent('li') is ancestor),None)
            if owner:
                title = owner.get_text(' ',strip=True) or owner.get('aria-label','')
                if title and (not parents or parents[-1]!=title):
                    parents.append(title)
        if not parents or parents[-1]!=name:
            parents.append(name)
        links=[];seen=set()
        for anchor in group.select('a[href]'):
            href=url_value(anchor.get('href'),base)
            if href and href not in seen:
                seen.add(href)
                links.append({'url':href,'text':anchor.get_text(' ',strip=True) or anchor.get('aria-label') or href,
                              'selector':source_path(anchor)})
        yield {'name':name,'selector':source_path(button),'group_selector':source_path(group),
               'path':parents,'links':links,'expanded':button.get('aria-expanded')=='true'}


def structural_members(soup, base, known, *, original=None):
    """Repeated sibling cards remain neutral list items without commercial guesses."""
    from .semantic import url_value
    seen=set(known)
    originals = {source_path(tag):tag for tag in original.find_all(True)} if original is not None else {}
    candidates={}
    for heading in soup.select('h2, h3, h4'):
        if heading.find_parent(['nav','header','footer','aside']):
            continue
        anchor=heading.find_parent('a',href=True) or heading.find('a',href=True)
        if not anchor:
            continue
        # Collection boundaries can sit above arbitrarily many layout wrappers.
        # Ascend to document landmarks instead of imposing a layout depth.
        for ancestor in anchor.parents:
            if ancestor.name in ('body','html','main','nav','header','footer','aside') or ancestor.get('role') in ('main','navigation'):
                break
            if ancestor.parent:
                candidates[id(ancestor.parent)]=ancestor.parent
    for parent in candidates.values():
        rows=[]
        for child in parent.find_all(recursive=False):
            linked=[]
            for heading in child.select('h2,h3,h4'):
                anchor=heading.find_parent('a',href=True) or heading.find('a',href=True)
                if not anchor or (anchor is not child and child not in anchor.parents):
                    continue
                href=url_value(anchor.get('href'),base)
                if href:
                    linked.append((heading,urldefrag(href)[0]))
            # A panel containing several records is not itself a record. Extra
            # unlinked headings (badges, disclosures) do not hide the title.
            if len({href for _,href in linked})==1:
                heading,href=linked[0]
                rows.append((child,heading,href))
        if len({href for _,_,href in rows})<2 or len({child.name for child,_,_ in rows})!=1:
            continue
        for position,(card,heading,href) in enumerate(rows,1):
            if href in seen:
                continue
            seen.add(href)
            wrappers=[]
            while True:
                children=card.find_all(True,recursive=False)
                own_text=''.join(card.find_all(string=True,recursive=False)).strip()
                if len(children)!=1 or own_text or children[0].name in ('a','h2','h3','h4'):
                    break
                wrappers.append({'selector':source_path(card),'attributes':dict(card.attrs)})
                card=children[0]
            evidence_card=originals.get(source_path(card),card)
            image=record_image(evidence_card,href,base)
            yield {'url':href,'name':heading.get_text(' ',strip=True),'selector':source_path(card),
                   'heading_selector':source_path(heading),'image':url_value(image.get('src'),base) if image else None,
                   'image_selector':source_path(image) if image else source_path(card),
                   'description':'\n'.join(p.get_text(' ',strip=True) for p in card.select('p')),
                   'declared':None,'position':position,'collection_selector':source_path(parent),
                   'wrappers':wrappers,
                   'dom_html':str(evidence_card)[:24000],'dom_html_truncated':len(str(evidence_card))>24000}
