from src.common.dom import candidate_groups, clean_document
from tests.legacy.jev_extractor import decide_groups


def test_local_parts_precede_summary_and_other_is_excluded():
    soup, structured = clean_document('''<body><header>Mi tienda</header><main>
    <div id="cards"><article><a href="/a"><img src="/a.jpg"><h3>Uno</h3></a><span>10 EUR</span></article>
    <article><a href="/b"><img src="/b.jpg"><h3>Dos</h3></a><span>20 EUR</span></article></div>
    <section id="discard">SECRETO DESCARTADO</section></main></body>''')
    groups = candidate_groups(soup, 'https://shop.test/')
    calls = []
    def predict(state, questions):
        assert all('other' in q['criteria'] for q in questions.values())
        calls.append(state)
        answers = {}
        for key in questions:
            if key.startswith('f') and key[1:].isdigit():
                assert key in questions[key]['instructions']
            if key == 'page_kind':
                assert any('fields' in s for s in calls[:-1])
                assert 'SECRETO DESCARTADO' not in str(state)
                assert 'groups' not in state and 'main_text' not in state
                assert state['summary']['counts'] == {'products': 1, 'header': 1}
                choice = 'listing'
            elif key == 'primary_group':
                choice = next(b['id'] for b in state['summary']['blocks'] if b['class'] == 'products')
            elif key.startswith('f'):
                item = next(f for f in state['fields'] if f['id'] == key)
                choice = {'a': 'product_link', 'img': 'image', 'h3': 'name', 'span': 'price'}.get(item['tag'], 'other')
            else:
                assert 'page_kind' not in state and 'page' not in state
                item = next(g for g in state['groups'] if g['id'] == key)
                choice = 'products' if item['attrs'].get('id') == 'cards' else 'header' if item['region'] == 'header' else 'other'
            answers[key] = {'choice': choice, 'confidence': .95}
        return {'model': 'fixture', 'answers': answers}
    page, decisions = decide_groups('', 'https://shop.test/', soup, structured, groups, predict)
    assert page['value'] == 'listing'
    products = next(d for d in decisions if d['value'] == 'primary_products')
    names = next(p for p in products['parts'] if p['class'] == 'name')
    assert [v['text'] for v in names['values']] == ['Uno', 'Dos']
    links = next(p for p in products['parts'] if p['class'] == 'product_link')
    assert [v['href'] for v in links['values']] == ['https://shop.test/a', 'https://shop.test/b']
    assert page['summary']['discarded_count'] > 0
    assert not any(p['class'] == 'other' for b in page['summary']['blocks'] for p in b['fields'])


def test_page_can_abstain_when_all_blocks_are_discarded():
    soup, structured = clean_document('<main><section>Contenido ambiguo</section></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    def predict(state, questions):
        if 'summary' in state:
            assert state['summary']['blocks'] == []
        return {'answers': {key: {'choice': 'other', 'confidence': .9} for key in questions}}
    page, decisions = decide_groups('No usar para clasificar', '', soup, structured, groups, predict)
    assert page['value'] == 'other'
    assert all(d['value'] == 'other' for d in decisions)


def test_repetitions_are_sampled_with_structural_variants():
    from tests.legacy.block_tree import compact_group
    soup, _ = clean_document('''<main><div id="cards">
    <article><h3>Uno</h3><a href=/a>A</a></article>
    <article><h3>Dos</h3><a href=/b>B</a></article>
    <article><h3>Tres</h3><a href=/c>C</a><span>Agotado</span></article>
    </div></main>''')
    group = next(g for g in candidate_groups(soup, 'https://shop.test/') if g['selector'] == '#cards')
    compact = compact_group(group, soup)
    assert len(compact['samples']) == 2
    assert sorted(s['count'] for s in compact['samples']) == [1, 2]
    assert 'Agotado' in compact['text']


def test_low_confidence_and_redundant_ancestors_do_not_enter_summary():
    from tests.legacy.block_tree import recognized_summary
    soup, _ = clean_document('<main><section><div id="cards"><a href=/a>A</a><a href=/b>B</a></div></section><button>?</button></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    decisions = [{'group_id': g['id'], 'value': 'products' if g['shape'] != 'control' else 'collection_control',
                  'confidence': .95 if g['shape'] != 'control' else .4} for g in groups]
    summary = recognized_summary(groups, decisions, soup)
    assert summary['counts'] == {'products': 1}
    assert summary['blocks'][0]['id'] == next(g['id'] for g in groups if g['selector'] == '#cards')
    assert summary['excluded_count'] == len(groups) - 1


def test_refresh_parts_reuses_known_shapes_and_marks_new_shapes():
    from tests.legacy.block_tree import refresh_parts
    before, _ = clean_document('<div id="reviews"><article><p>A</p></article><article><p>B</p></article></div>')
    after, _ = clean_document('<div id="reviews"><article><p>A</p></article><article><p>B</p></article><article><p>C</p></article><article><p>D</p><aside>Nuevo</aside></article></div>')
    group = {'selector': '#reviews', 'shape': 'repeated_siblings'}
    parts = [{'class': 'review_body', 'variant': 0, 'template_path': [0], 'confidence': .99}]
    refreshed, unknown = refresh_parts(parts, group, before, after, 'https://fixture.test/')
    assert [v['text'] for v in refreshed[0]['values']] == ['A', 'B', 'C']
    assert unknown == 1


def test_nested_identical_wrappers_are_classified_once_and_keep_aliases():
    soup, _ = clean_document('<main><section id="outer"><section id="inner"><p>Nombre</p><a href=/x>Ficha</a></section></section></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    assert len(groups) == 1
    assert groups[0]['selector'] == '#inner'
    assert {a['selector'] for a in groups[0]['structural_aliases']} == {'#outer', 'main'}


def test_equal_text_in_separate_regions_or_different_links_is_not_collapsed():
    soup, _ = clean_document('<header><section>Marca</section></header><main><section><a href=/a>Uno</a></section><section><a href=/b>Uno</a></section></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    destinations = {link['url'] for group in groups for link in group['links']}
    assert {'https://shop.test/a','https://shop.test/b'} <= destinations
    assert any(group['region'] == 'header' for group in groups)


def test_repeated_controls_keep_all_targets_without_individual_block_calls():
    soup, _ = clean_document('<main><div id="controls"><button id="prev">Anterior</button><button id="next">Siguiente</button></div></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    assert not any(group['shape'] == 'control' for group in groups)
    group = next(group for group in groups if group['selector'] == '#controls')
    assert group['grouped_controls'] == ['#prev', '#next']
    assert [control['text'] for control in group['controls']] == ['Anterior', 'Siguiente']


def test_exact_dom_fields_are_reused_across_overlapping_classified_blocks():
    from tests.legacy.block_tree import classify_parts
    from tests.legacy.jev_extractor import _choice
    soup, _ = clean_document('<div id="cards"><article id="a"><h3>Uno</h3><a href=/a>Ficha</a></article><article id="b"><h3>Dos</h3><a href=/b>Ficha</a></article></div>')
    groups = [{'id':'g1','selector':'#cards','shape':'repeated_siblings','heading':''},
              {'id':'g2','selector':'#a','shape':'section','heading':''}]
    decisions = [{'group_id':'g1','value':'products','confidence':.95},
                 {'group_id':'g2','value':'product','confidence':.95}]
    calls=[]
    def predict(state, questions):
        calls.append(state)
        return {'model':'fixture','answers':{field['id']:{'choice':'name' if field['tag']=='h3' else 'product_link' if field['tag']=='a' else 'other','confidence':.95} for field in state['fields']}}
    classify_parts(groups, decisions, soup, 'https://shop.test/', predict, _choice)
    assert len(calls)==1
    assert decisions[1]['reused_field_predictions'] > 0
    assert next(p for p in decisions[1]['parts'] if p['class']=='name')['values'][0]['text']=='Uno'


def test_single_child_wrapper_chain_is_one_leaf_block_with_attributes_audited():
    soup, _ = clean_document('<main><div id="outer" class="gallery"><div role="region"><div><div id="leaf">Imagen</div></div></div></div></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    assert len(groups) == 1 and groups[0]['selector'] == '#leaf'
    aliases = groups[0]['structural_aliases']
    assert len({a['selector'] for a in aliases}) == len(aliases)
    assert any(a['attrs'].get('class') == ['gallery'] for a in aliases)
    assert soup.select_one('#outer') is not None  # Source evidence remains intact.


def test_wrapper_with_own_text_or_multiple_branches_is_not_unwrapped():
    soup, _ = clean_document('<main><section id="text">Título<div><div>Contenido</div></div></section><section id="branches"><div>A</div><div>B</div></section></main>')
    groups = candidate_groups(soup, 'https://shop.test/')
    assert any(g['selector'] == '#text' for g in groups)
    assert any(g['selector'] == '#branches' for g in groups)


def test_all_questions_about_each_sample_share_one_call():
    from tests.legacy.block_tree import classify_parts
    from tests.legacy.jev_extractor import _choice
    soup, _ = clean_document('<section id="one">' + ''.join(f'<p>A{i}</p>' for i in range(20)) + '</section><section id="two">' + ''.join(f'<p>B{i}</p>' for i in range(20)) + '</section>')
    groups = [{'id':f'g{i}','selector':selector,'shape':'section','heading':''} for i,selector in enumerate(['#one','#two'])]
    decisions = [{'group_id':g['id'],'value':'attributes','confidence':.95} for g in groups]
    seen={}
    def predict(state, questions):
        call=len(seen)+1
        for field in state['fields']:
            seen.setdefault(field['context_id'],set()).add(call)
            assert field['context_id'] in state['block_contexts']
        return {'answers':{key:{'choice':'description','confidence':.95} for key in questions}}
    classify_parts(groups,decisions,soup,'https://shop.test/',predict,_choice)
    assert len(seen)==2 and all(len(calls)==1 for calls in seen.values())
    assert all(len(decision['parts'])==20 for decision in decisions)
