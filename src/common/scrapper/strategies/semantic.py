"""Zero-cost extraction from declared metadata and standard HTML semantics only."""
from __future__ import annotations

from .discovery import discover_links
from .navigation import observe_navigation
from .records import collection_members, navigation_groups, structural_members
from dataclasses import replace

import json
from html import unescape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ...dom import clean_document, css_path
from ..models import PageAnalysis, StrategyExpense, WebPageEntities, WebPageEntity

# Standard vocabularies, never site classes, text patterns or URL paths.
SCHEMA_ROLES = {
    'Product': 'product', 'IndividualProduct': 'product', 'ProductGroup': 'product',
    'Offer': 'offer', 'AggregateOffer': 'offer', 'Review': 'reviews',
    'AggregateRating': 'reviews', 'Article': 'editorial', 'NewsArticle': 'editorial',
    'BlogPosting': 'editorial', 'TechArticle': 'editorial', 'Recipe': 'editorial',
    'DiscussionForumPosting': 'discussion', 'QAPage': 'qa', 'Question': 'qa',
    'BreadcrumbList': 'breadcrumb', 'ItemList': 'semantic_collection',
}
SCHEMA_FIELDS = {
    'name': 'name', 'headline': 'name', 'brand': 'brand', 'image': 'image',
    'description': 'description', 'articleBody': 'description', 'reviewBody': 'review_body',
    'reviewTitle': 'review_title', 'author': 'author', 'datePublished': 'date',
    'acceptedAnswer': 'answer', 'suggestedAnswer': 'answer',
    'sku': 'sku', 'productID': 'product_id', 'mpn': 'mpn', 'gtin': 'gtin',
    'gtin13': 'gtin', 'gtin14': 'gtin', 'dateModified': 'date', 'price': 'price', 'lowPrice': 'price', 'highPrice': 'price',
    'availability': 'availability', 'ratingValue': 'rating', 'ratingCount': 'rating_count',
    'reviewCount': 'review_count', 'url': 'product_link', 'recipeIngredient': 'ingredients',
}
NAVIGATION_SELECTOR = 'nav a[href], [role="navigation"] a[href], main a[href], article a[href], table a[href], [role="main"] a[href], a[rel~="next"], link[rel~="next"]'


def schema_term(value, allow_bare=True):
    """Resolve schema.org URLs or the standard schema prefix, not arbitrary vocabularies."""
    if not isinstance(value, str):
        return ''
    parsed = urlsplit(value)
    if parsed.hostname in {'schema.org', 'www.schema.org'}:
        return parsed.path.strip('/').split('/')[-1]
    if allow_bare and value.startswith('schema:'):
        return value.partition(':')[2]
    return value if allow_bare and ':' not in value and '/' not in value else ''


def schema_vocabulary(context):
    if isinstance(context, list):
        return any(schema_vocabulary(item) for item in context)
    if isinstance(context, dict):
        return any(schema_vocabulary(value) for value in context.values())
    return isinstance(context, str) and urlsplit(context).hostname in {'schema.org', 'www.schema.org'}


def dom_type(root, value):
    if root.has_attr('itemtype'):
        return schema_term(value, allow_bare=False)
    vocabulary = root if root.has_attr('vocab') else root.find_parent(attrs={'vocab': True})
    if ':' not in value and schema_vocabulary(vocabulary.get('vocab') if vocabulary else None):
        return schema_term(value)
    owner = root if root.has_attr('prefix') else root.find_parent(attrs={'prefix': True})
    if owner and ':' in value:
        declarations = owner.get('prefix', '').split()
        namespaces = dict(zip(declarations[::2], declarations[1::2]))
        prefix, term = value.split(':', 1)
        if schema_vocabulary(namespaces.get(prefix + ':')):
            return term
    return schema_term(value, allow_bare=False)


def source_selector(tag):
    return tag.get('data-semantic-origin') or css_path(tag)


def url_value(value, base):
    if not isinstance(value, str):
        return None
    try:
        result = urljoin(base, value)
        parsed = urlsplit(result)
        parsed.port  # Validate malformed ports as well as malformed hostnames.
        return result if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None



def scalar_values(value, preferred=('@value', 'name', 'url', 'contentUrl', '@id')):
    if isinstance(value, list):
        return [part for item in value for part in scalar_values(item, preferred)]
    if isinstance(value, dict):
        for key in preferred:
            if key in value:
                return scalar_values(value[key], preferred)
        return []
    return [unescape(str(value))] if value is not None and not isinstance(value, bool) else []


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


class SemanticMetadataStrategy:
    """Extract declared fields; unannotated articles remain articles, never guessed products."""

    def __init__(self, *, follow_navigation=True, max_entities=200, max_links=300, field_predictor=None):
        if max_entities < 1 or max_links < 1:
            raise ValueError('Los límites de entidades y enlaces deben ser positivos.')
        self.follow_navigation = follow_navigation
        self.max_entities = max_entities
        self.max_links = max_links
        self.field_predictor = field_predictor

    def process(self, page, api):
        api.report_expense(StrategyExpense(0, provider='local', operation='semantic_metadata'))
        capture = api.capture
        results = [self.extract_capture(capture,page)]
        states, actions, navigation_coverage = observe_navigation(page,capture,
            on_snapshot=lambda state:results.append(self.extract_capture(state,page)))
        merged = {}
        for index, result in enumerate(results):
            for entity in result.entities:
                identity = ((entity.role,tuple(v.casefold() for v in entity.attributes['navigation_path']))
                            if entity.role=='categories' else (entity.role,entity.selector))
                previous = merged.get(identity)
                links = {link['url']:link for old in ([previous] if previous else []) for link in old.links}
                links.update({link['url']:link for link in entity.links})
                observed = list(previous.attributes.get('observed_states', [])) if previous else []
                observed.append(index)
                merged[identity] = replace(entity, links=tuple(links.values()),
                    attributes={**entity.attributes, 'observed_states':observed})
        entities = tuple(replace(entity,id=f's{i+1}') for i,entity in enumerate(merged.values()))
        omitted = max(0,len(entities)-self.max_entities)
        # Prefer extracted collection members and navigation groups over duplicate
        # generic text evidence when the configured output limit is reached.
        entities = tuple(sorted(entities,key=lambda e:e.role not in {'product','semantic_item','categories'}))[:self.max_entities]
        coverage = {**results[0].classification['coverage'],
                    'omitted_entities':omitted + sum(r.classification['coverage']['omitted_entities'] for r in results),
                    'navigation_observation':navigation_coverage,
                    'snapshots_analyzed':len(states),
                    'omitted_navigation_links':sum(r.classification['coverage']['omitted_navigation_links'] for r in results)}
        result = replace(results[0],entities=entities,actions=tuple(actions),
            classification={**results[0].classification,'coverage':coverage})
        from .semantic_fields import enrich_fields
        result = enrich_fields(result,capture,page,api,predict=self.field_predictor)
        api.emit_content(PageAnalysis(result,capture=capture,
            doubtful=not any(e.role not in {'metadata','site_menu','breadcrumb'} for e in entities)))
        for entity in entities:
            if entity.is_content:
                api.emit_content(entity)
        if self.follow_navigation:
            for state in states:
                discover_links(state,api)

    def extract_capture(self, capture, page=None):
        """Parse an immutable snapshot; never mutate or navigate the live document."""
        original = BeautifulSoup(capture.html, 'html.parser')
        # Work on a copy: keep selectors relative to the original capture even
        # when cleaning removes hidden siblings. Visibility uses standard CSS.
        for tag in original.find_all(True):
            tag['data-semantic-origin'] = css_path(tag)
        soup, _ = clean_document(str(original))
        selectors = [source_selector(tag) for tag in original.select(
            'main, [role="main"], article, [role="article"], table, h1, h2, h3, [itemscope], [typeof], [itemprop], [property]')]
        rendered = {}
        if hasattr(page, 'evaluate') and selectors:
            observations = page.evaluate('''selectors => selectors.map(selector => {
                const node = document.querySelector(selector);
                if (!node) return [selector, null];
                const style = getComputedStyle(node);
                const visible = !!node.getClientRects().length && style.visibility !== 'hidden' && style.display !== 'none';
                return [selector, {visible, text: node.innerText || ''}];
            })''', selectors)
            rendered = {selector: data for selector, data in observations if data is not None}
        def dom_text(tag):
            observation = rendered.get(source_selector(tag))
            return observation['text'].strip() if observation else tag.get_text(' ', strip=True)

        entities = []
        structured = []
        errors = []
        metadata = {}
        base = capture.final_uri
        base_tag = original.find('base', href=True)
        if base_tag:
            base = url_value(base_tag['href'], base) or base

        def add(role, source, selector, text='', parts=(), links=(), attributes=None):
            entity = WebPageEntity(f's{len(entities)+1}', role, ('content',), 1.0,
                (selector,), selector, 'metadata' if source in {'jsonld', 'head'} else 'main', '', text,
                links=tuple(links), attributes={'source': source, 'visible': rendered.get(selector, {}).get('visible'), **(attributes or {})},
                verification=('Metadatos declarados; no prueban visibilidad' if source in {'jsonld', 'head'}
                              else 'Propiedades y elementos semánticos del DOM; visibilidad registrada por separado'), parts=tuple(parts))
            entities.append(entity)
            return entity

        def field(label, text, selector, **attrs):
            return {'id': f'{selector}:{label}', 'class': label, 'confidence': 1.0,
                'model': 'semantic-v3', 'evidence_ids': [selector],
                'values': [{'text': text, 'selector': selector, 'visible': rendered.get(selector, {}).get('visible'), **attrs}]}

        for tag in original.select('meta[name][content], meta[property][content]'):
            key = tag.get('property') or tag.get('name')
            metadata.setdefault(key, []).append(tag['content'])
        for relation in ('canonical', 'alternate', 'next', 'prev'):
            values = [url_value(tag.get('href'), base) for tag in original.select(f'link[rel~="{relation}"][href]')]
            if any(values):
                metadata[f'link:{relation}'] = [value for value in values if value]
        if original.title:
            metadata['title'] = [original.title.get_text(' ', strip=True)]
        metadata_parts = []
        if original.title:
            metadata_parts.append(field('name', original.title.get_text(' ', strip=True), 'head > title', source='head'))
        for tag in original.select('meta[name="description"][content], meta[property="og:description"][content]'):
            metadata_parts.append(field('description', unescape(tag['content']), source_selector(tag), source='head'))
        add('metadata', 'head', 'head', parts=metadata_parts, attributes={'metadata': metadata})

        documents = []
        for script in original.select('script[type="application/ld+json"]'):
            selector = source_selector(script)
            try:
                data = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                errors.append({'selector': selector, 'error': 'JSON-LD inválido'})
                continue
            structured.extend(nodes(data))
            documents.append((selector, data))
        references = {}
        for node in structured:
            if isinstance(node.get('@id'), str) and len(node) > len(references.get(node['@id'], {})):
                references[node['@id']] = node
        def referenced(value):
            if isinstance(value, list):
                return [referenced(item) for item in value]
            if isinstance(value, dict) and isinstance(value.get('@id'), str):
                return {**references.get(value['@id'], {}), **value}
            return value
        declared_types = set()
        for selector, data in documents:
            for index, node in enumerate(nodes(data)):
                types = node.get('@type', [])
                types = types if isinstance(types, list) else [types]
                terms = {schema_term(kind, allow_bare=schema_vocabulary(node.get('@context', data.get('@context') if isinstance(data, dict) else None))) for kind in types}
                declared_types.update(terms)
                role = next((SCHEMA_ROLES[term] for term in terms if term in SCHEMA_ROLES), None)
                if role is None:
                    continue
                source = f'{selector}::jsonld[{index}]'
                parts = []
                links = []
                # Product prices and ratings may be nested, but other products must
                # keep their own identity. Only standardized property edges are flattened.
                field_sources = [node]
                for prop in ('offers', 'aggregateRating', 'reviewRating'):
                    child = referenced(node.get(prop))
                    field_sources.extend(child if isinstance(child, list) else [child] if isinstance(child, dict) else [])
                for obj in field_sources:
                    if not isinstance(obj, dict):
                        continue
                    for prop, label in SCHEMA_FIELDS.items():
                        preferred = (('text', '@value', 'name') if label == 'answer' else
                                     ('url', 'contentUrl', '@id') if label == 'image' else
                                     ('url', '@id') if label == 'product_link' else
                                     ('@value', 'name', 'url', 'contentUrl', '@id'))
                        for text in scalar_values(referenced(obj.get(prop)), preferred):
                            attrs = {'source': 'jsonld', 'property': prop}
                            if obj.get('priceCurrency'):
                                attrs['currency'] = obj['priceCurrency']
                            if label in {'image', 'product_link'}:
                                destination = url_value(text, base)
                                if destination:
                                    attrs['src' if label == 'image' else 'href'] = destination
                                    if label == 'product_link':
                                        links.append({'url': destination, 'text': text})
                            parts.append(field(label, text, source, **attrs))
                add(role, 'jsonld', source, parts=parts, links=links,
                    attributes={'schema_types': types, 'declared': node})

        members=list(collection_members(original,documents,references,base))
        members.extend(structural_members(soup,base,{m['url'] for m in members},original=original))
        for member in members:
            if rendered.get(member['heading_selector'], {}).get('visible') is False:
                continue
            parts = [field('name',member['name'],member['heading_selector']),
                     field('product_link',member['url'],member['selector'],href=member['url'])]
            if member['image']:
                parts.append(field('image',member['image'],member['image_selector'],src=member['image']))
            if member['description']:
                parts.append(field('description',member['description'],member['selector']))
            add('semantic_item','collection_dom',member['selector'],member['description'],parts=parts,
                links=[{'url':member['url'],'text':member['name']}],attributes={
                    'declared':member['declared'],'collection_selector':member['collection_selector'],
                    'position':member['position'],'dom_html':member['dom_html'],
                    'wrappers':member.get('wrappers',[]),
                    'dom_html_truncated':member['dom_html_truncated']})

        for group in navigation_groups(original,base):
            add('categories','navigation_dom',group['selector'],parts=[field('name',group['name'],group['selector'])],
                links=group['links'],attributes={'navigation_path':group['path'],
                    'group_selector':group['group_selector'],'expanded':group['expanded']})

        for root in soup.select('[itemscope][itemtype], [typeof]'):
            types = (root.get('itemtype') or root.get('typeof') or '').split()
            role = next((SCHEMA_ROLES[dom_type(root, kind)] for kind in types
                         if dom_type(root, kind) in SCHEMA_ROLES), None)
            if not role:
                continue
            parts, links = [], []
            for tag in [root, *root.select('[itemprop], [property]')]:
                owners = [ancestor for ancestor in tag.parents if ancestor is not root and root in ancestor.parents
                          and (ancestor.has_attr('itemscope') or ancestor.has_attr('typeof'))]
                if any(not set((owner.get('itemprop') or owner.get('property') or '').split())
                       & {'offers', 'aggregateRating', 'reviewRating'} for owner in owners):
                    continue
                if tag is not root and (tag.has_attr('itemscope') or tag.has_attr('typeof')):
                    if not set((tag.get('itemprop') or tag.get('property') or '').split()) & {'brand', 'author'}:
                        continue
                for prop in (tag.get('itemprop') or tag.get('property') or '').split():
                    label = SCHEMA_FIELDS.get(schema_term(prop) if tag.has_attr('itemprop') else dom_type(tag, prop))
                    if label is None:
                        continue
                    value = next((tag.get(attr) for attr in ('content', 'datetime', 'href', 'src')
                                  if tag.has_attr(attr)), tag.get_text(' ', strip=True))
                    attrs = {'source': 'semantic_dom', 'property': prop}
                    if label in {'image', 'product_link'}:
                        destination = url_value(value, base)
                        if destination:
                            attrs['src' if label == 'image' else 'href'] = destination
                            if label == 'product_link':
                                links.append({'url': destination, 'text': tag.get_text(' ', strip=True)})
                    parts.append(field(label, value, source_selector(tag), **attrs))
            add(role, 'semantic_dom', source_selector(root), root.get_text(' ', strip=True),
                parts=parts, links=links, attributes={'schema_types': types})

        # Standard article elements offer content even without structured metadata.
        for article in soup.select('article, [role="article"]'):
            if rendered.get(source_selector(article), {}).get('visible') is False:
                continue
            if article.find_parent(['article']) or any(
                entity.selector == source_selector(article) and entity.attributes['source'] == 'semantic_dom'
                for entity in entities):
                continue
            heading = article.find(['h1', 'h2', 'h3'])
            parts = [field('name', dom_text(heading), source_selector(heading))] if heading else []
            add('semantic_article', 'semantic_dom', source_selector(article), dom_text(article), parts=parts)

        # List cards are semantic records even without Product annotations.
        # Preserve their title, link and text without inferring prices or types.
        for item in soup.select('main li, [role="main"] li'):
            if item.find_parent(['nav', 'aside', 'article']) or item.select_one('[itemscope], [typeof]'):
                continue
            heading = item.find(['h2', 'h3', 'h4'])
            anchor = heading.find('a', href=True) if heading else None
            if anchor is None or heading.find_parent('li') is not item:
                continue
            if rendered.get(source_selector(heading), {}).get('visible') is False:
                continue
            destination = url_value(anchor['href'], base)
            parts = [field('name', dom_text(heading), source_selector(heading))]
            links = []
            if destination:
                parts.append(field('product_link', anchor.get_text(' ', strip=True),
                                   source_selector(anchor), href=destination, source='semantic_dom'))
                links.append({'url': destination, 'text': dom_text(heading)})
            add('semantic_article', 'semantic_dom', source_selector(item), item.get_text(' ', strip=True),
                parts=parts, links=links, attributes={'list_member': True})

        # Landmarks expose visible content without asserting its commercial purpose.
        for main in soup.select('main, [role="main"]'):
            headings = [tag for tag in main.select('h1, h2, h3') if rendered.get(source_selector(tag), {}).get('visible') is not False]
            parts = [field('name', dom_text(heading), source_selector(heading)) for heading in headings[:80]]
            pairs = []
            for dt in main.select('dl dt'):
                dd = dt.find_next_sibling('dd')
                if dd:
                    pairs.append({'label': dt.get_text(' ', strip=True), 'value': dd.get_text(' ', strip=True),
                                  'label_selector': source_selector(dt), 'value_selector': source_selector(dd)})
            add('semantic_content', 'semantic_dom', source_selector(main), dom_text(main),
                parts=parts, attributes={'description_lists': pairs, 'omitted_headings': max(0, len(headings)-80)})

        declared_questions = {' '.join(v['text'].split()).casefold()
            for entity in entities if entity.role == 'qa' for part in entity.parts
            if part['class'] in ('name','question') for v in part['values']}
        # Collapsed disclosure content is already evidence. Read the original
        # DOM, not the visibility-cleaned copy; never spend a hop just to read it.
        for disclosure in original.select('details'):
            summary = disclosure.find('summary', recursive=False)
            if summary is None:
                continue
            question = summary.get_text(' ',strip=True)
            answer = ' '.join(child.get_text(' ',strip=True) if hasattr(child,'get_text') else str(child).strip()
                              for child in disclosure.children if child is not summary).strip()
            if not answer:
                continue
            selector = source_selector(disclosure)
            is_question = ' '.join(question.split()).casefold() in declared_questions
            parts = [field('question' if is_question else 'name',question,source_selector(summary),source='semantic_dom'),
                     field('answer',answer,selector,source='semantic_dom',disclosure_open=disclosure.has_attr('open'))]
            if not disclosure.has_attr('open'):
                parts[1]['values'][0]['visible'] = False
            add('qa' if is_question else 'semantic_disclosure','semantic_dom',selector,parts=parts,
                attributes={'disclosure_open':disclosure.has_attr('open'),'content_in_dom':True})
        for control in original.select('[aria-expanded][aria-controls]'):
            target_ids = control.get('aria-controls','').split()
            regions = [original.find(id=target_id) for target_id in target_ids]
            answer = ' '.join(region.get_text(' ',strip=True) for region in regions if region is not None).strip()
            if not answer:
                continue
            question = control.get_text(' ',strip=True) or control.get('aria-label','') or control.get('title','')
            is_question = ' '.join(question.split()).casefold() in declared_questions
            parts = [field('question' if is_question else 'name',question,source_selector(control),source='semantic_dom')]
            for region in regions:
                if region is not None:
                    item = field('answer',region.get_text(' ',strip=True),source_selector(region),source='semantic_dom')
                    if control.get('aria-expanded') == 'false':
                        item['values'][0]['visible'] = False
                    parts.append(item)
            add('qa' if is_question else 'semantic_disclosure','semantic_dom',source_selector(control),parts=parts,
                attributes={'disclosure_open':control.get('aria-expanded')=='true','content_in_dom':True})

        for table in soup.select('table'):
            if rendered.get(source_selector(table), {}).get('visible') is False:
                continue
            rows = table.find_all('tr')
            records = [{'cells': [cell.get_text(' ', strip=True) for cell in row.find_all(['th', 'td'], recursive=False)],
                        'selector': source_selector(row)} for row in rows[:200]]
            add('semantic_table', 'semantic_dom', source_selector(table), table.get_text(' ', strip=True),
                attributes={'rows': records, 'omitted_rows': max(0, len(rows)-200)})

        navigation = []
        for tag in original.select(NAVIGATION_SELECTOR):
            destination = url_value(tag.get('href'), base)
            if destination and destination not in {link['url'] for link in navigation}:
                navigation.append({'url': destination, 'text': tag.get_text(' ', strip=True), 'selector': source_selector(tag)})
        coverage = {'omitted_entities': max(0, len(entities) - self.max_entities),
                    'omitted_navigation_links': max(0, len(navigation) - self.max_links),
                    'jsonld_errors': errors}
        entities = entities[:self.max_entities]
        navigation = navigation[:self.max_links]
        if navigation:
            entities.append(WebPageEntity('semantic-navigation', 'site_menu', ('navigation',), 1.0,
                ('semantic-links',), NAVIGATION_SELECTOR, 'main', '', '', links=tuple(navigation),
                verification='Enlaces en regiones semánticas; sin interpretación de texto'))
        page_type = ('listing' if declared_types & {'SearchResultsPage', 'CollectionPage', 'ItemList'} else
                     'product' if declared_types & {'Product', 'IndividualProduct', 'ProductGroup'} else
                     'editorial' if declared_types & {'Article', 'NewsArticle', 'BlogPosting', 'TechArticle', 'Recipe'} or
                     'article' in metadata.get('og:type', []) else
                     'listing' if any(entity.role=='semantic_item' for entity in entities) else 'other')
        result = WebPageEntities(page_type, 1.0, 'semantic-v3', tuple(entity.id for entity in entities),
            tuple(entities), jsonld=tuple(structured), classification={'value': page_type,
                'model': 'semantic-v3', 'verification': 'Tipos declarados y elementos semánticos',
                'metadata': metadata, 'coverage': coverage})
        return result
