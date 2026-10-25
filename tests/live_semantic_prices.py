"""Replay or re-extract the two reported pages; expectations stay in tests."""
import argparse
from contextlib import contextmanager
import json
import re
from decimal import Decimal
from bs4 import BeautifulSoup
from pathlib import Path
import sys
import time
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.common.scrapper import AuditStore, PageCapture, Scrapper, ScrapperConfig, SemanticMetadataStrategy

SOURCES={'detail':'afd0173db09e4f619c7852047ca27971','listing':'ef5aedede8784835a4c53a29245f60c3'}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--only',choices=SOURCES)
    parser.add_argument('--api',default='http://100.111.171.20:8787');args=parser.parse_args()
    root=Path('output/semantic-price-regression');root.mkdir(exist_ok=True)
    def request(path,body=None):
        req=urllib.request.Request(args.api+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
    for kind,source in SOURCES.items():
        if args.only and args.only!=kind:continue
        source_visit=request('/api/scrapes/'+source)['visits'][0];url=source_visit['requested_uri']
        if args.live:
            job=request('/api/extract',{'url':url})
            for _ in range(300):
                job_state=request('/api/scrapes/jobs/'+job['job_id'])
                if job_state['status']!='running':break
                time.sleep(1)
            assert job_state['status']=='done',job_state
            run_id=job_state['run_id'];run=request('/api/scrapes/'+run_id)
            assert len(run['visits'])==1
            visit=request('/api/scrapes/'+run_id+'/visits/'+run['visits'][0]['id'])
        else:
            with urllib.request.urlopen(args.api+'/api/scrapes/'+source+'/visits/'+source_visit['id']+'/html') as response:html=response.read().decode()
            @contextmanager
            def loader(uri):yield PageCapture(uri,uri,source_visit['title'],html)
            scraper=Scrapper(ScrapperConfig(max_sites=1,max_hops=0,max_retries=0,page_loader=loader,strategy=SemanticMetadataStrategy(follow_navigation=False)),audit=AuditStore(root/'audit'))
            summary=scraper.run([url]);run_id=summary.run_id
            visit=scraper.audit.get_visit(scraper.audit.list_visits(run_id)[0]['id'])
        result=visit.get('result') or {};items=[e for e in result.get('entities',[]) if e['role'] in ('product','semantic_item')]
        priced=[e for e in items if any(p['class']=='price' for p in e['parts'])]
        report={'kind':kind,'mode':'live' if args.live else 'replay','run_id':run_id,'items':len(items),'priced':len(priced),
                'fields':[{'role':e['role'],'parts':e['parts']} for e in items], 'page_type':result.get('page_type'),
                'coverage':result.get('classification',{}).get('coverage'),'usage':visit['usage'],'costs':visit['costs'],
                'url':args.api+'/?view=extract&extract_run='+run_id if args.live else None}
        (root/f'{kind}-{report["mode"]}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print({k:v for k,v in report.items() if k not in ('fields','coverage','costs')},flush=True)
        assert visit['status']=='done',visit.get('error')
        assert priced,report['coverage']
        if kind=='detail':
            assert len(items)==1 and result['page_type']=='product'
            fields={p['class']:p['values'][0]['text'] for p in items[0]['parts']}
            assert '192.08' in ''.join(fields['price'].split()),fields
            assert '312.38' in ''.join(fields['previous_price'].split()),fields
            assert '4.6' in fields['rating'] and '23,351' in fields['rating_count'],fields
            assert fields.get('image'),fields
            assert not any(e['role']=='reviews' for e in result['entities'])
        else:
            assert len(items)>=40 and len(priced)>=25
            assert result['page_type']=='listing'
            # Independent, site-specific oracle: never used by production rules.
            if args.live:
                with urllib.request.urlopen(args.api+'/api/scrapes/'+run_id+'/visits/'+visit['id']+'/html') as response:
                    original=BeautifulSoup(response.read().decode(),'html.parser')
            else:
                original=BeautifulSoup(scraper.audit.read_html(visit['id']),'html.parser')
            def amount(text):
                value=re.search(r'\d[\d.,\s]*',text).group(0)
                return Decimal(re.sub(r'[,\s]','',value))
            checked=0
            for entity in items:
                card=original.select_one(entity['selector'])
                expected=card.select_one('.a-price:not(.a-text-price) .a-offscreen') if card else None
                if not expected:continue
                captured=next((v['text'] for p in entity['parts'] if p['class']=='price' for v in p['values']),None)
                assert captured and amount(captured)==amount(expected.get_text(' ',strip=True)),entity['selector']
                checked+=1
            assert checked==len(priced)

if __name__=='__main__':main()
