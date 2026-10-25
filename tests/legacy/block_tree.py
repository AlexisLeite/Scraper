"""Compact structural samples and local field classification with exact DOM evidence."""
import json
from collections import Counter
from urllib.parse import urljoin
from bs4 import Tag
from src.common.dom import css_path
from src.common.taxonomy import FIELDS, FIELD_CLASSES


def signature(tag):
    return (tag.name, tuple((x.name, len(x.find_all(True, recursive=False)))
                           for x in tag.find_all(True)[:60]))


def representative_members(group, soup, max_variants=4):
    root = soup.select_one(group['selector'])
    if root is None:
        return [], 0
    members = root.find_all(True, recursive=False) if group['shape'] == 'repeated_siblings' else [root]
    variants = {}
    for member in members:
        variants.setdefault(signature(member), []).append(member)
    return list(variants.values())[:max_variants], max(0, len(variants) - max_variants)


def compact_group(group, soup):
    variants, omitted = representative_members(group, soup)
    samples = []
    for members in variants:
        node = members[0]
        samples.append({'count': len(members), 'text': node.get_text(' ', strip=True)[:1600],
                        'html': str(node)[:1800], 'selector': css_path(node)})
    # The complete group stays in the audit; only its representative description is sent.
    return {'id': group['id'], 'shape': group['shape'], 'region': group['region'],
            'heading': group['heading'], 'attrs': group['attrs'],
            'structural_aliases': group.get('structural_aliases', []),
            'text': ' | '.join(s['text'] for s in samples), 'samples': samples,
            'members_count': sum(len(v) for v in variants), 'omitted_variants': omitted,
            'links': [{k: x.get(k) for k in ('text', 'url')} for x in group['links'][:6]],
            'controls': [{k: x.get(k) for k in ('text', 'url')} for x in group['controls'][:8]]}


def field_nodes(root):
    candidates = []
    for node in [root, *root.find_all(True)]:
        if node.name in ('img', 'a', 'time', 'h1', 'h2', 'h3', 'h4', 'p', 'dt', 'dd', 'li') or not node.find(True):
            if node.get_text(' ', strip=True) or node.get('src') or node.get('href'):
                candidates.append(node)
    return candidates[:32], max(0, len(candidates) - 32)


def relative_path(root, node):
    path = []
    while node is not root:
        parent = node.parent
        path.append(parent.find_all(True, recursive=False).index(node))
        node = parent
    return list(reversed(path))


def resolve(root, path):
    node = root
    for index in path:
        children = node.find_all(True, recursive=False)
        if index >= len(children):
            return None
        node = children[index]
    return node


def value(node, url):
    result = {'text': node.get_text(' ', strip=True), 'selector': css_path(node)}
    for attr in ('href', 'src', 'alt', 'title', 'datetime', 'content', 'itemprop', 'aria-label'):
        if node.has_attr(attr):
            result[attr] = urljoin(url, node[attr]) if attr in ('href', 'src') else node[attr]
    return result


def classify_parts(groups, decisions, soup, url, predict, choice):
    lookup = {group['id']: group for group in groups}
    eligible = [decision for decision in decisions if decision['value'] in FIELD_CLASSES]
    contexts, jobs, plans, references = {}, {}, [], {}
    for decision in eligible:
        group = lookup[decision['group_id']]
        root = soup.select_one(group['selector'])
        if root is None or decision['confidence'] is None or decision['confidence'] < 0.65 or any(
            other is not decision and other['value'] == decision['value'] and
            other['confidence'] is not None and other['confidence'] >= 0.65 and
            (child := soup.select_one(lookup[other['group_id']]['selector'])) is not None and
            root in child.parents for other in eligible):
            continue
        variants, omitted = representative_members(group, soup)
        decision['parts'] = []
        decision['omitted_variants'] = omitted
        for variant, members in enumerate(variants):
            sample = members[0]
            nodes, omitted_fields = field_nodes(sample)
            criteria = {key: FIELDS[key] for key in (*FIELD_CLASSES[decision['value']], 'other')}
            context_id = f"{group['id']}:v{variant}"
            contexts[context_id] = {'block_class': decision['value'], 'heading': group['heading'],
                'sample_text': sample.get_text(' ', strip=True)[:1800]}
            decision.setdefault('coverage', []).append({'variant': variant, 'members': len(members),
                'omitted_fields': omitted_fields})
            for index, node in enumerate(nodes):
                path = relative_path(sample, node)
                exact = [value(resolved, url) for member in members
                         if (resolved := resolve(member, path)) is not None]
                key = (css_path(node), tuple(sorted(criteria)))
                if key in references:
                    job_id = references[key]
                    decision['reused_field_predictions'] = decision.get('reused_field_predictions', 0) + 1
                else:
                    job_id = f'f{len(jobs)}'
                    field = {'id': job_id, 'context_id': context_id, 'tag': node.name,
                        **{key: (val[:350] if isinstance(val, str) else val)
                           for key, val in value(node, url).items() if key != 'selector'}}
                    jobs[job_id] = {'field': field, 'criteria': criteria}
                    references[key] = job_id
                for extracted in exact:
                    references.setdefault((extracted['selector'], tuple(sorted(criteria))), job_id)
                plans.append((decision, variant, index, path, exact, job_id))

    def state(batch):
        context_ids = {job['field']['context_id'] for job in batch}
        classes = {key for job in batch for key in job['criteria']}
        return {'fields': [job['field'] for job in batch],
                'block_contexts': {key: contexts[key] for key in sorted(context_ids)},
                'class_definitions': {key: FIELDS[key] for key in sorted(classes)}}

    # Keep every question about one sample in the same call. A sample has at
    # most 32 fields; the payload limit groups samples, never splits one.
    bundles = {}
    for job in jobs.values():
        bundles.setdefault(job['field']['context_id'], []).append(job)
    batches, current = [], []
    for bundle in bundles.values():
        proposed = current + bundle
        if current and (len(proposed) > 32 or len(json.dumps(state(proposed), ensure_ascii=False)) > 16000):
            batches.append(current)
            current = list(bundle)
        else:
            current = proposed
    if current:
        batches.append(current)
    answers = {}
    for batch in batches:
        questions = {job['field']['id']: {'type': 'choice', 'instructions':
            f"Clasifica exclusivamente {job['field']['id']} (etiqueta {job['field']['tag']}, texto: {job['field'].get('text', '')[:100]}) usando su block_contexts[context_id]. Otro permite descartarla.",
            'criteria': {key: key.replace('_', ' ') for key in job['criteria']}} for job in batch}
        response = predict(state(batch), questions)
        for job in batch:
            job_id = job['field']['id']
            answers[job_id] = (response.get('answers', {}).get(job_id), response.get('model', 'Jev'))
    for decision, variant, index, path, exact, job_id in plans:
        answer, model = answers[job_id]
        label, confidence = choice(answer, jobs[job_id]['criteria'])
        decision['parts'].append({'id': f"{decision['group_id']}:v{variant}:f{index}",
            'class': label, 'confidence': confidence, 'model': model,
            'template_path': path, 'evidence_ids': [item['selector'] for item in exact],
            'values': exact, 'variant': variant, 'prediction_id': job_id})


def recognized_summary(groups, decisions, soup, min_confidence=0.65):
    lookup = {g['id']: g for g in groups}
    blocks = []
    for decision in decisions:
        if decision['value'] == 'other' or decision['confidence'] is None or decision['confidence'] < min_confidence:
            continue
        group = lookup[decision['group_id']]
        node = soup.select_one(group['selector'])
        if node is not None and any(other is not decision and other['value'] == decision['value'] and
            other['confidence'] is not None and other['confidence'] >= min_confidence and
            (child := soup.select_one(lookup[other['group_id']]['selector'])) is not None and
            node in child.parents for other in decisions):
            continue
        parent_ids = [other['group_id'] for other in decisions if other is not decision and
                      other['value'] != 'other' and node is not None and
                      (parent := soup.select_one(lookup[other['group_id']]['selector'])) is not None and
                      parent in node.parents]
        fields = [part for part in decision.get('parts', []) if part['class'] != 'other' and
                  part['confidence'] is not None and part['confidence'] >= min_confidence]
        blocks.append({'id': group['id'], 'class': decision['value'],
            'confidence': decision['confidence'], 'region': group['region'], 'heading': group['heading'],
            'shape': group['shape'], 'parent_ids': parent_ids,
            'members_count': len(node.find_all(True, recursive=False)) if node is not None else 0,
            'excerpt': node.get_text(' ', strip=True)[:420] if node is not None else '',
            'fields': [{'class': p['class'], 'confidence': p['confidence'],
                        'sample': [{k: (v[:160] if isinstance(v, str) else v) for k, v in p['values'][0].items() if k != 'selector'}] if p['values'] else [], 'count': len(p['values'])} for p in fields],
            'coverage': decision.get('coverage', []), 'omitted_variants': decision.get('omitted_variants', 0)})
    included_ids = {block['id'] for block in blocks}
    for block in blocks:
        block['parent_ids'] = [parent for parent in block['parent_ids'] if parent in included_ids]
    return {'counts': dict(Counter(block['class'] for block in blocks)), 'blocks': blocks,
            'discarded_count': sum(d['value'] == 'other' for d in decisions),
            'excluded_count': len(decisions) - len(blocks)}


def refresh_parts(parts, group, before_soup, after_soup, url):
    """Reuse only structurally identical, model-classified templates after an action."""
    variants, _ = representative_members(group, before_soup)
    after_root = after_soup.select_one(group['selector'])
    if after_root is None:
        return [], 0
    members = after_root.find_all(True, recursive=False) if group['shape'] == 'repeated_siblings' else [after_root]
    known_shapes = {signature(variant[0]) for variant in variants}
    refreshed = []
    for part in parts:
        variant = part.get('variant')
        if variant is None or variant >= len(variants):
            continue
        shape = signature(variants[variant][0])
        exact = [value(node, url) for member in members if signature(member) == shape
                 and (node := resolve(member, part['template_path'])) is not None]
        refreshed.append({**part, 'values': exact, 'evidence_ids': [v['selector'] for v in exact],
                          'verification': 'DOM posterior; patrón estructural clasificado por Jev'})
    return refreshed, sum(signature(member) not in known_shapes for member in members)
