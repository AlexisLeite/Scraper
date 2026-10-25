"""One-page regression. Site-specific expectations belong here, never in production."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.common.scrapper import AuditStore, Scrapper, ScrapperConfig, SemanticMetadataStrategy

URL='https://www.laroche-posay.com.uy/vitamina-c?gad_campaignid=11303071409&gad_source=1&gbraid=0AAAAAC2oEQLGm1_iSWaf8CMDtKLLisr_u'
CATEGORIES=['ALERGIAS DE LA PIEL','piel dañada','DERMATITIS Y DERMATITIS ATÓPICA','piel sensible','Exposición al sol','Situaciones específicas de la vida','PIEL CON TENDENCIA AL ACNÉ']
PRODUCTS=['PURE VITAMIN C OJOS','PURE VITAMIN C PIEL NORMAL A MIXTA','PURE VITAMIN C UV']

def text(value):return ' '.join(value.split()).casefold()
def request(base,path,body=None):
    req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--local',action='store_true')
    parser.add_argument('--api',default='http://100.111.171.20:8787')
    parser.add_argument('--report',default='output/semantic-menu-regression/verified.json')
    args=parser.parse_args()
    if args.local:
        audit=AuditStore('output/semantic-menu-regression')
        scraper=Scrapper(ScrapperConfig(max_sites=1,max_hops=0,max_threads=1,max_retries=0,
            strategy=SemanticMetadataStrategy(follow_navigation=False)),audit=audit)
        summary=asdict(scraper.run([URL]));run_id=summary['run_id'];visits=audit.list_visits(run_id)
        details=[audit.get_visit(v['id']) for v in visits]
    else:
        job=request(args.api,'/api/extract',{'url':URL})
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            status=request(args.api,'/api/scrapes/jobs/'+job['job_id'])
            if status['status']!='running':break
            time.sleep(1)
        assert status['status']=='done',status
        summary=status['summary'];run_id=status['run_id']
        run=request(args.api,'/api/scrapes/'+run_id);visits=run['visits']
        details=[request(args.api,f'/api/scrapes/{run_id}/visits/{v["id"]}') for v in visits]
    assert len(details)==1 and summary['succeeded']==1 and summary['failed']==0,summary
    detail=details[0];result=detail['result'];entities=result['entities']
    def name(entity):return next((p['values'][0]['text'] for p in entity['parts'] if p['class']=='name' and p['values']),entity.get('heading',''))
    items=[e for e in entities if e['role'] in ('semantic_item','product')]
    groups=[e for e in entities if e['role']=='categories']
    names={text(name(e)) for e in items};category_names={text(name(e)) for e in groups}
    assert all(text(value) in names for value in PRODUCTS),sorted(names)
    assert all(text(value) in category_names for value in CATEGORIES),sorted(category_names)
    assert all(e['links'] for e in items)
    navigation=result['classification']['coverage']['navigation_observation']
    assert navigation['initial_url']==navigation['final_url']
    assert not navigation['blocked_navigations'],navigation
    actions=result['actions']
    assert actions and all(a['method']=='hover' and a['kind']=='navigation_observation' for a in actions)
    assert any(a.get('evidence',{}).get('after',{}).get('html_path') for a in actions)
    assert detail['usage']['exact'] and detail['costs']['exact']
    if detail['usage']['calls']==0:assert detail['costs']['amount_usd']=='0'
    report={'at':datetime.now(timezone.utc).isoformat(),'url':URL,'run_id':run_id,'summary':summary,
        'products':[{'name':name(e),'links':e['links']} for e in items],
        'categories':[{'name':name(e),'path':e['attributes'].get('navigation_path',[]),'links':e['links']} for e in groups],
        'coverage':result['classification']['coverage'],
        'no_follow_evidence':{'visits':len(visits),'hops':0,'clicks':0,'methods':sorted({a['method'] for a in actions}),
            'document_url_unchanged':True,'blocked_navigation_requests':navigation['blocked_navigations']},
        'result_url':f'{args.api}/?view=extract&extract_run={run_id}' if not args.local else None}
    path=Path(args.report);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'run_id':run_id,'products':len(items),'category_groups':len(groups),'required_categories':len(CATEGORIES),
        'visits':len(visits),'navigation':navigation,'result_url':report['result_url'],'report':str(path)},ensure_ascii=False))

if __name__=='__main__':main()
