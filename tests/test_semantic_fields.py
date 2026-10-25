"""Unannotated commerce values stay scoped to exact DOM samples."""
from dataclasses import replace
import json

from src.common.scrapper import PageCapture, SemanticMetadataStrategy
from src.common.scrapper.strategies.semantic_fields import enrich_fields
from src.common.scrapper.strategies.semantic_fields import monetary_choice


class API:
    def __init__(self):self.expenses=[]
    def report_expense(self,expense):self.expenses.append(expense)


def fixture_result(html):
    capture=PageCapture('https://neutral.test/','https://neutral.test/','Neutral page',html)
    return capture,SemanticMetadataStrategy().extract_capture(capture)


def predictor(calls):
    def predict(state,questions):
        calls.append((state,questions))
        assert len(questions)<=32
        assert len(json.dumps({'model':'jev-latest','state':state,'questions':questions},ensure_ascii=False).encode())<=60000
        if 'recognized_blocks' in state:
            return {'answers':{'page_kind':{'choice':'listing' if any(b['collection'] for b in state['recognized_blocks']) else 'product','confidence':.99}}}
        answers={}
        for sample in state['samples']:
            sid=sample['id']
            answers[sid+':role']={'choice':'product','confidence':.99}
            # All questions for this sample must share this one context.
            assert {key.split(':')[0] for key in questions if key.startswith(sid+':')}=={sid}
            for key in [q for q in questions if q.startswith(sid+':') and q!=sid+':role']:
                field=key.split(':')[1]
                text={'price':'EUR 18.50','previous_price':'EUR 25.00','rating':'4.7 / 5',
                      'rating_count':'142 reviews','name':'Neutral lotion','description':'Daily moisturizer'}.get(field)
                value=next((v['id'] for v in sample['values'] if v['text']==text),None) if text else None
                if field=='image':value=next((v['id'] for v in sample['values'] if v['tag']=='img'),None)
                answers[key]={'choice':value or 'other','confidence':.99}
        return {'model':'mock-semantic','answers':answers}
    return predict


def test_cards_preserve_prices_without_using_shipping_or_a_neighbour():
    capture,result=fixture_result('''<main><section>
      <div><a href="/one"><h2>First lotion</h2></a><p>EUR 18.50</p><s>EUR 25.00</s><p>Shipping EUR 5.00</p><p>EUR 0.92 / ml</p><p>4.7 / 5</p><p>142 reviews</p></div>
      <div><a href="/two"><h2>Second lotion</h2></a><p>Choose options</p></div>
      </section></main>''')
    calls=[];enriched=enrich_fields(result,capture,None,API(),predict=predictor(calls))
    first=next(e for e in enriched.entities if e.links and e.links[0]['url'].endswith('/one'))
    prices=[v['text'] for p in first.parts if p['class']=='price' for v in p['values']]
    assert prices==['EUR 18.50']
    second=next(e for e in enriched.entities if e.links and e.links[0]['url'].endswith('/two'))
    assert not any(p['class']=='price' for p in second.parts)
    assert enriched.page_type=='listing'
    assert first.attributes['semantic_field_model']=='mock-semantic'


def test_individual_detail_without_schema_becomes_an_evidenced_product():
    capture,result=fixture_result('''<main><h1>Neutral lotion</h1><img aria-hidden="true" src="/bottle.jpg">
      <p>Daily moisturizer</p><div>EUR 18.50</div><s>EUR 25.00</s><p>Shipping EUR 5.00</p>
      <p>4.7 / 5</p><span>142 reviews</span><p>Sign in to see individual reviews.</p></main>''')
    calls=[];enriched=enrich_fields(result,capture,None,API(),predict=predictor(calls))
    product=next(e for e in enriched.entities if e.role=='product')
    assert enriched.page_type=='product'
    fields={p['class']:p['values'][0] for p in product.parts}
    assert fields['name']['text']=='Neutral lotion'
    assert fields['price']['text']=='EUR 18.50'
    assert fields['previous_price']['text']=='EUR 25.00'
    assert fields['image']['src']=='https://neutral.test/bottle.jpg'
    assert fields['rating_count']['text']=='142 reviews'
    assert not any(e.role=='reviews' for e in enriched.entities)
    assert all(v['selector'] for v in fields.values())


def test_untrusted_unknown_or_low_confidence_values_cannot_be_published():
    capture,result=fixture_result('<main><h1>Neutral lotion</h1><p>EUR 18.50</p></main>')
    def predict(state,questions):
        return {'answers':{key:{'choice':('product' if key.endswith(':role') else 'n999999'),'confidence':.99} for key in questions}}
    enriched=enrich_fields(result,capture,None,API(),predict=predict)
    assert enriched.entities==result.entities


def test_model_failure_preserves_snapshot_and_reports_partial_coverage():
    capture,result=fixture_result('<main><h1>Neutral lotion</h1><p>EUR 18.50</p></main>')
    def fail(*args):raise RuntimeError('Model unavailable')
    enriched=enrich_fields(result,capture,None,API(),predict=fail)
    assert enriched.entities==result.entities
    assert not enriched.classification['coverage']['field_interpretation']['complete']
    assert enriched.classification['coverage']['field_interpretation']['errors']


def test_large_samples_keep_questions_together_within_provider_limits():
    html='<main><h1>Neutral lotion</h1><p>EUR 18.50</p>'+''.join(f'<p>{n} '+('Long specification '*16)+'</p>' for n in range(400))+'</main>'
    capture,result=fixture_result(html);calls=[]
    enriched=enrich_fields(result,capture,None,API(),predict=predictor(calls))
    assert enriched.classification['coverage']['field_interpretation']['omitted_values']>0
    assert len(calls[0][1])==13
    assert next(p for e in enriched.entities if e.role=='product' for p in e.parts if p['class']=='price')['values'][0]['text']=='EUR 18.50'


def test_identical_amount_aliases_share_confidence_without_absorbing_shipping():
    values=[{'id':'a','text':'EUR 18.50'},{'id':'b','text':'Price EUR 18.50'},
            {'id':'c','text':'Shipping EUR 5.00'},{'id':'d','text':'EUR 18.50 with 5% discount'}]
    answer={'choice':'a','confidence':.51,'probabilities':{'a':.51,'b':.23,'c':.2,'d':.06}}
    selected,confidence=monetary_choice(answer,{'a','b','c','d','other'},values)
    assert selected=='a' and confidence==.74
    answer={'choice':'c','confidence':.2,'probabilities':{'a':.51,'b':.23,'c':.2,'d':.06}}
    assert monetary_choice(answer,{'a','b','c','d','other'},values)[0]=='other'


def test_injected_predictor_usage_is_reported_at_the_input_only_rate():
    capture,result=fixture_result('<main><h1>Neutral lotion</h1><p>EUR 18.50</p></main>')
    api=API()
    def predict(state,questions):
        return {'model':'test-metered','answers':{},'usage':{'input_tokens':1_000_000,'output_tokens':2_000_000}}
    enrich_fields(result,capture,None,api,predict=predict)
    assert len(api.expenses)==1
    assert str(api.expenses[0].amount_usd)=='0.042'
    assert api.expenses[0].usage['output_cost_usd']=='0'


def test_product_image_can_be_linked_by_its_explicit_alt_identity():
    capture,result=fixture_result('''<main><h1>Neutral lotion</h1>
      <img src="/wrong.jpg" alt="Another product"><img src="/right.jpg" alt="Neutral lotion frontal photo">
      <p>EUR 18.50</p></main>''')
    base=predictor([])
    def predict(state,questions):
        response=base(state,questions)
        for key in response['answers']:
            if key.endswith(':image'):response['answers'][key]={'choice':'other','confidence':.9}
        return response
    enriched=enrich_fields(result,capture,None,API(),predict=predict)
    image=next(p for e in enriched.entities if e.role=='product' for p in e.parts if p['class']=='image')
    assert image['values'][0]['src']=='https://neutral.test/right.jpg'
