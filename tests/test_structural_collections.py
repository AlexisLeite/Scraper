"""Layout depth and auxiliary headings must not decide collection membership."""
from bs4 import BeautifulSoup

from src.common.scrapper import PageCapture, SemanticMetadataStrategy


def wrapped_card(key, depth, *, heading_inside_link=True):
    title = f'<h2>Record {key}</h2>'
    link = f'<a href="/record/{key}">{title}</a>' if heading_inside_link else f'<h2><a href="/record/{key}">Record {key}</a></h2>'
    content = f'<section><h3>Auxiliary badge</h3><img src="/{key}.jpg">{link}<p>Description {key}</p></section>'
    for n in range(depth):
        content = f'<div data-layout="{n}">{content}</div>'
    return content


def test_deep_layout_wrappers_keep_individual_members_and_source_evidence():
    cards = ''.join(wrapped_card(key, depth) for key, depth in [('a', 12), ('b', 7), ('c', 1)])
    html = f'<body><nav><div>{wrapped_card("nav1", 3)}{wrapped_card("nav2", 3)}</div></nav><div><div><div hidden>Hidden layout sibling</div>{cards}</div></div></body>'
    result = SemanticMetadataStrategy().extract_capture(PageCapture('https://neutral.test/', 'https://neutral.test/', 'Records', html))
    items = [e for e in result.entities if e.role == 'semantic_item']
    assert len(items) == 3
    assert {e.parts[0]['values'][0]['text'] for e in items} == {'Record a', 'Record b', 'Record c'}
    assert result.classification['value'] == 'listing'
    assert all(e.attributes['dom_html'].startswith('<section') for e in items)
    assert all(e.attributes['wrappers'] for e in items)
    original = BeautifulSoup(html, 'html.parser')
    for item in items:
        assert original.select_one(item.selector).name == 'section'
        assert original.select_one(item.attributes['wrappers'][0]['selector'])['data-layout'] is not None
        assert len(item.links) == 1


def test_panels_with_multiple_destinations_are_not_additional_records():
    panels = ''.join(f'<section>{wrapped_card(str(n)+"a", 5, heading_inside_link=False)}{wrapped_card(str(n)+"b", 5)}</section>' for n in range(2))
    result = SemanticMetadataStrategy().extract_capture(PageCapture('https://neutral.test/', 'https://neutral.test/', 'Records', f'<main>{panels}</main>'))
    items = [e for e in result.entities if e.role == 'semantic_item']
    assert len(items) == 4
    assert len({e.links[0]['url'] for e in items}) == 4
    assert not any(e.role == 'product' for e in items)


def test_accessibility_hidden_product_photos_survive_text_cleaning():
    cards = ''.join(f'''<div><img src="/badge.png" alt="Certification">
      <a href="/{key}"><img src="/{key}.jpg" aria-hidden="true" alt=""></a>
      <div><a href="/{key}"><h2>Record {key}</h2></a></div></div>''' for key in ('a','b'))
    html=f'<main><section>{cards}</section></main>'
    result=SemanticMetadataStrategy().extract_capture(PageCapture('https://neutral.test/', 'https://neutral.test/', 'Records', html))
    items=[e for e in result.entities if e.role=='semantic_item']
    assert len(items)==2
    original=BeautifulSoup(html,'html.parser')
    for item in items:
        image=next(p['values'][0] for p in item.parts if p['class']=='image')
        assert image['src']==item.links[0]['url']+'.jpg'
        assert original.select_one(image['selector'])['aria-hidden']=='true'
        assert 'aria-hidden="true"' in item.attributes['dom_html']
