#!/usr/bin/env python3
"""Bounded, isolated Playwright collection. Agents receive file receipts/short JSON.

No CUA/MCP, model requests, playback, HTTP scraping, inferred media URLs, covers,
ACKs or production writes. Formal collect/discover preserve the original owner.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from app_server_control import hidden_options

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
SELECTORS = {'title':'#video','episodes':'#list a[data-web]',
             'groups':'[class*="playList"]','lines':'label[for]'}
SCOPES = {'01':['酷播','天堂'], '02':['非凡'], '03':['量子','高清5'],
          '04':['动漫O','日漫M','高清7'],
          'main':['酷播','天堂','非凡','量子','高清5','动漫O','日漫M','高清7']}
SOURCE_REFRESH_SECONDS = 0


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def node(action, **kwargs):
    binary = shutil.which('node.exe' if os.name == 'nt' else 'node')
    if not binary:
        raise RuntimeError('Existing Node runtime is unavailable')
    result = subprocess.run([binary, str(HERE/'native-detail-capture.mjs')],
        input=json.dumps({'action':action, **kwargs}, ensure_ascii=False),
        text=True, encoding='utf-8', capture_output=True, timeout=30, **hidden_options())
    if result.returncode:
        raise RuntimeError('DOM adapter rejected result: '+result.stderr[-1200:])
    return json.loads(result.stdout)


def list_reader():
    result = subprocess.run([shutil.which('node'),str(HERE/'playwright-dom.mjs'),'reader'],
        text=True, encoding='utf-8', capture_output=True, check=True, timeout=10,
        **hidden_options())
    return json.loads(result.stdout)['code']


def public_url(value, detail=False):
    u = urlsplit(value)
    if (u.scheme != 'https' or u.hostname not in {'xxmanmi.com','www.xxmanmi.com'}
            or u.username or u.password or u.fragment or (detail and u.query)
            or (detail and not re.fullmatch(r'/video\d+/[^/]+\.html',u.path))):
        raise ValueError('Expected an actually observed public xxmanmi URL')
    return value


def route_guard():
    import collection_runtime
    collection_runtime.require_write()
    if collection_runtime.production():
        return collection_runtime.native_route()
    sys.path.insert(0,str(PROJECT/'web'))
    import cover_direct
    config = cover_direct._api('/configs')
    prefix = set()
    for rule in cover_direct._api('/rules')['rules']:
        if rule.get('proxy') != 'DIRECT':
            break
        if rule.get('type') == 'Domain':
            prefix.add(rule.get('payload'))
    if (config.get('mode') != 'rule' or not cover_direct._persisted_tun()
            or not {'xxmanmi.com','www.xxmanmi.com'} <= prefix):
        raise RuntimeError('Required persistent TUN/exact site DIRECT rules unavailable')
    return {'rule_mode':True,'persistent_tun':True,'exact_site_direct_rules':True}


@contextmanager
def browser(profile=None):
    from playwright.sync_api import sync_playwright
    route_guard()
    with sync_playwright() as p:
        import collection_runtime
        options = dict(headless=True,channel='chromium' if collection_runtime.production() else 'chrome',timeout=30000,
            args=['--no-proxy-server','--autoplay-policy=user-gesture-required'])
        if profile:
            profile = Path(profile)
            if profile.is_symlink():
                raise ValueError('Unsafe private browser profile')
            context = p.chromium.launch_persistent_context(str(profile),**options)
            instance = None
        else:
            instance = p.chromium.launch(**options)
            context = instance.new_context(accept_downloads=False)
        page = context.new_page()
        page.set_default_timeout(15000)
        try:
            yield page
        finally:
            context.close()
            if instance:
                instance.close()


class PublicPageUnavailable(RuntimeError):
    """An explicit public page failure; skip it without saving a DOM payload."""


def observe_detail(page, item, reader, *, owner, batch, job_id):
    """Capture a whole page without handing code or segments through a model."""
    url = public_url(item['url'],True)
    response = page.goto(url,wait_until='domcontentloaded',timeout=30000)
    if response is not None and response.status >= 400:
        raise PublicPageUnavailable('Public detail HTTP status '+str(response.status))
    page.locator('#video').wait_for(state='visible',timeout=15000)
    header = page.evaluate(reader['code'],{'selectors':reader['selectors']})
    for _ in range(12):
        page.wait_for_timeout(250)
        fresh = page.evaluate(reader['code'],{'selectors':reader['selectors']})
        if (fresh == header and fresh.get('episodeDomCount',0) == 0
                and re.search(r'获取失败|暂无资源|已下架|已删除', fresh.get('title',''))):
            raise PublicPageUnavailable('Public page reports unavailable resources')
        if fresh == header and fresh.get('episodeDomCount',0)>0:
            break
        header = fresh
    else:
        raise RuntimeError('Public detail DOM empty or not stable; no journal written')
    if header['url'] != url:
        raise ValueError('Public detail navigation changed the observed URL')
    count = header['episodeDomCount']
    if type(count) is not int or not 1 <= count <= 100000:
        raise ValueError('Invalid/bounded DOM entry count')
    def wrap(observation,start):
        return {'owner':owner,'batch':batch,'expectedUrl':url,'start':start,
                'observedAt':datetime.now(timezone.utc).isoformat(),'observation':observation}
    evidence = {'schema':'xxmanmi-playwright-detail-evidence-v1','jobId':job_id,
        'owner':owner,'batch':batch,'expectedUrl':url,'engine':'playwright/chrome/headless',
        'segmentSize':100,'readerSha256':hashlib.sha256(reader['code'].encode()).hexdigest(),
        'metadata':wrap(header,None),'segments':[],'sourceCalls':[]}
    for offset in range(0,count,100):
        part = page.evaluate(reader['code'],{'selectors':reader['selectors'],'start':offset,'limit':100})
        if any(part.get(k)!=header[k] for k in ['url','title','fingerprint','episodeDomCount']):
            raise ValueError('Public DOM changed during capture; no journal written')
        if any(value and not re.fullmatch(r'(?:undefined|null|nan)',value,re.I)
               and not re.match(r'^(?:https?://|//)',value,re.I)
               for entry in part.get('entries',[])
               for value in [entry.get('dataWeb','').strip()]):
            raise PublicPageUnavailable('Unresolved public resource identifiers; skip without persistence')
        evidence['segments'].append(wrap(part,offset))
    final = page.evaluate(reader['code'],{'selectors':reader['selectors']})
    if final != header:
        raise ValueError('Public DOM changed after capture; no journal written')
    validate_evidence(evidence,item,owner,batch)
    return evidence


def usable_preview(evidence, item, work):
    """Reject identifiers in memory, before creating any journal/evidence file."""
    preview = node('preview', evidence=evidence, item=item, work=str(work))
    if preview.get('outcome') != 'captured' or preview.get('mediaCount', 0) < 1:
        return {'ok': True, 'outcome': 'skipped', 'journaled': 0,
                'reason': 'No complete supported public media links; skip without persistence'}
    return None


def validate_evidence(e, item, owner, batch):
    if (e.get('schema') != 'xxmanmi-playwright-detail-evidence-v1'
            or e.get('owner') != owner or e.get('batch') != batch
            or e.get('expectedUrl') != item['url'] or e.get('segmentSize') != 100
            or not re.fullmatch(r'[0-9a-f]{32}',e.get('jobId',''))):
        raise ValueError('Playwright receipt identity/claim mismatch')
    header = e['metadata']['observation']
    count = header.get('episodeDomCount')
    if type(count) is not int or count < 1 or not header.get('fingerprint'):
        raise ValueError('Incomplete Playwright metadata')
    if len(e['segments']) != (count+99)//100:
        raise ValueError('Missing/extra Playwright segments')
    for index, payload in enumerate([e['metadata'],*e['segments']]):
        start = None if index==0 else (index-1)*100
        if any(payload.get(k)!=v for k,v in {'owner':owner,'batch':batch,
                'expectedUrl':item['url'],'start':start}.items()):
            raise ValueError('Playwright segment binding mismatch')
        observed = payload['observation']
        if any(observed.get(k)!=header.get(k) for k in ['url','title','episodeDomCount','fingerprint']):
            raise ValueError('Inconsistent Playwright segment metadata')
        if start is not None:
            entries = observed.get('entries',[])
            if (observed.get('start')!=start or len(entries)!=min(100,count-start)
                    or [x.get('index') for x in entries]!=list(range(start,start+len(entries)))):
                raise ValueError('Truncated or reordered Playwright segment')
            if any(any(type(x.get(k)) is not str for k in ['label','group','dataWeb']) for x in entries):
                raise ValueError('Incomplete public DOM entry')
    if header.get('url') != item['url']:
        raise ValueError('Playwright detail URL mismatch')
    previous = None
    for part in [e['metadata'],*e['segments']]:
        at = datetime.fromisoformat(part['observedAt'].replace('Z','+00:00'))
        if at.tzinfo is None or (previous and at < previous):
            raise ValueError('Playwright observation clock/order mismatch')
        previous = at


def owner_context(owner):
    import workflowctl as workflow
    import recoveryctl
    import coordctl
    if owner != os.environ.get('CODEX_THREAD_ID'):
        raise ValueError('Only the original collector owner may run its formal worker')
    slot,binding = recoveryctl.slot_for(owner)
    state = workflow.read_state(workflow.DEFAULT_STATE)
    coordctl.require_collection_running(state)
    _,work = workflow.owner_record(workflow.DEFAULT_STATE,owner)
    if Path(binding['work']).resolve() != work or not work.is_dir() or work.is_symlink():
        raise ValueError('Original collection work binding changed')
    return slot,work


def collect(owner, expected_batch=None):
    import workflowctl as workflow
    import coordctl
    _,work = owner_context(owner)
    current = coordctl.current_batch(workflow.DEFAULT_STATE,owner)
    if expected_batch and current.get('active_batch') != expected_batch:
        raise ValueError('Requested batch is not the original active claim; no new claim created')
    claim = workflow.claim_detail_task(workflow.DEFAULT_STATE,owner,limit=1)
    if expected_batch and claim.get('batch')!=expected_batch:
        raise ValueError('Requested original batch differs from actual active claim')
    if claim.get('remaining',0)==0:
        return claim
    reader = node('reader')
    job_id = uuid.uuid4().hex
    job_path = work/f"{claim['batch']}-playwright-job.json"
    with workflow.work_lock(work):
        state = workflow.read_state(workflow.DEFAULT_STATE)
        batch = state['batches'][claim['batch']]
        _,book = workflow._detail_book(workflow.DEFAULT_STATE,owner,claim['batch'],work,batch,
                                     workflow.DEFAULT_TASKBOOKS)
        found = workflow._detail_observations(work,batch,book)
        job = {'schema':'xxmanmi-playwright-job-v1','jobId':job_id,'owner':owner,
            'batch':claim['batch'],'pid':os.getpid(),'started_at':workflow.utc_now(),
            'status':'running','taskbook_sha256':digest(book),'results':[]}
        workflow.atomic_json(job_path,job)
        try:
            with browser(work/'playwright-browser-profile') as page:
                for url in batch['item_urls']:
                    if url in found:
                        continue
                    fresh = workflow.read_state(workflow.DEFAULT_STATE)
                    coordctl.require_collection_running(fresh)
                    if (fresh['threads'][owner].get('active_batch')!=claim['batch']
                            or fresh['batches'][claim['batch']]['state']!='claimed'):
                        raise ValueError('Original live claim changed; worker stopped')
                    item = fresh['items'][url]['item']
                    try:
                        evidence = observe_detail(page,item,reader,owner=owner,batch=claim['batch'],job_id=job_id)
                    except PublicPageUnavailable as error:
                        job['results'].append({'url':url,'ok':True,'outcome':'skipped',
                                               'journaled':0,'reason':str(error)})
                        workflow.atomic_json(job_path,job)
                        continue
                    skipped = usable_preview(evidence,item,work)
                    if skipped:
                        job['results'].append({'url':url,**skipped})
                        workflow.atomic_json(job_path,job)
                        continue
                    # Persist authentic observations first; the model never supplies this payload.
                    evidence_path = work/f"{claim['batch']}-playwright-{digest(evidence)[:24]}.json"
                    with evidence_path.open('x',encoding='utf-8',newline='\n') as f:
                        json.dump(evidence,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
                    with coordctl.StateLock(workflow.DEFAULT_STATE):
                        final_state = workflow.read_state(workflow.DEFAULT_STATE)
                        coordctl.require_collection_running(final_state)
                        if (final_state['threads'][owner].get('active_batch')!=claim['batch']
                                or final_state['batches'][claim['batch']]!=fresh['batches'][claim['batch']]):
                            raise ValueError('Claim/gate changed before journaling; raw receipt retained')
                        evidence['evidencePath'] = str(evidence_path)
                        result = node('journal',evidence=evidence,item=item,work=str(work))
                    job['results'].append({'url':url,**result,'evidence':str(evidence_path),
                                           'evidence_sha256':hashlib.sha256(evidence_path.read_bytes()).hexdigest()})
                    workflow.atomic_json(job_path,job)
            job['status']='complete';job['completed_at']=workflow.utc_now()
            workflow.atomic_json(job_path,job)
        except Exception as error:
            job.update(status='failed',error=str(error)[:1200],finished_at=workflow.utc_now())
            workflow.atomic_json(job_path,job)
            raise
    remaining = workflow.claim_detail_task(workflow.DEFAULT_STATE,owner,limit=1)
    return {**remaining,'engine':'playwright','receipt':str(job_path),
            'results':job['results'],'browser_closed':True}


def settled_list(page, reader):
    previous = None
    for _ in range(30):
        value = page.evaluate(reader)
        key = digest({k:value[k] for k in ['url','view','source','items','radios','loading','explicitEmpty']})
        if (not value['loading'] and value['readyState']=='complete'
                and value['panelVisible'] and key==previous
                and (value['items'] or value['explicitEmpty'])):
            return value
        previous = key
        page.wait_for_timeout(300)
    if not value['loading'] and value['readyState']=='complete' and value['panelVisible']:
        return value  # No empty indicator: caller records uncertain, never zero-success.
    raise RuntimeError('Public source view did not settle; navigation checkpoint retained')


def latest_list(page, reader, sources):
    """Restore the visible latest tab after a persistent search session."""
    snapshot = settled_list(page, reader)
    if snapshot['view'] != 'latest':
        tab = next((t for t in snapshot.get('tabs', []) if t['value'] == 'like'), None)
        if tab is None:
            raise ValueError('Actual visible latest navigation unavailable')
        control = page.locator('.weui-tabbar [data-id='+json.dumps(tab['value'])+']')
        if control.count() != 1 or not control.is_visible():
            raise ValueError('Observed latest navigation is not uniquely visible')
        control.click()
        snapshot = settled_list(page, reader)
    visible_sources = {r['text'] for r in snapshot['radios'] if r['name'] == 'newNum'}
    if snapshot['view'] != 'latest' or not set(sources) <= visible_sources:
        raise ValueError('Latest source controls unavailable; round cannot be completed')
    return snapshot


def discover(owner, *, max_views=8, sources=None):
    import workflowctl as workflow
    import coordctl
    slot,work = owner_context(owner)
    current = coordctl.current_batch(workflow.DEFAULT_STATE,owner)
    if current.get('active_batch'):
        raise ValueError('Resume original claimed batch before source discovery')
    checkpoint = work/'playwright-discovery.json'
    sources = sources or SCOPES[slot]
    reader = list_reader()
    def current_source_views(saved):
        views={}
        for value in saved['seen'].values():
            receipt=Path(value)
            if receipt.is_symlink() or not receipt.resolve().is_relative_to(work):
                raise ValueError('Source coverage receipt escaped original work')
            proof=json.loads(receipt.read_text('utf-8'))
            snapshot=proof.get('snapshot',{})
            if snapshot.get('view')=='latest':
                source=proof.get('requested_source',snapshot.get('source'))
                if source in sources:
                    views[source]=proof
        return views
    def navigation_sources(saved):
        if slot!='main':
            return [s for s in sources if s in {'高清5','高清7','日漫M','动漫O'}]
        views=current_source_views(saved)
        # Daily incremental polling uses each actual latest list. Exact-title
        # search is only a bounded fallback when that source lacks a usable list.
        return [s for s in sources if views.get(s,{}).get('status') not in {'observed','explicit_empty'}]
    def navigation_titles(saved, source):
        # A source whose latest control falls back can still be reached through
        # its actual visible search button. Use names already observed in this
        # same bounded public round; no model terms or historical keyword queue.
        if slot!='main':
            return list(saved['observed_titles'].get(source,{}))
        views=current_source_views(saved)
        titles=[item['title'] for s in sources for item in
                views.get(s,{}).get('snapshot',{}).get('items',[]) if item.get('title')]
        return list(dict.fromkeys(titles))[:2]
    with workflow.work_lock(work):
        saved = json.loads(checkpoint.read_text('utf-8')) if checkpoint.exists() else {
            'schema':'xxmanmi-playwright-discovery-v1','owner':owner,'pending':[],
            'seen':{},'sources':sources,'observed_titles':{},'navigation_seen':[],'completed_at':None}
        if saved.get('owner')!=owner or saved.get('sources')!=sources:
            raise ValueError('Original discovery owner/source allocation changed')
        if saved.get('action_inflight'):
            raise ValueError('Previous navigation outcome is unknown; retain checkpoint for main diagnosis')
        if saved.get('completed_at') and not saved['seen']:
            # An empty receipt map proves no source coverage, even if an older
            # version incorrectly recorded completion while the search tab was active.
            saved.setdefault('invalidated_rounds', []).append({
                'completed_at':saved['completed_at'], 'invalidated_at':workflow.utc_now(),
                'reason':'no_source_view_receipts'})
            saved['completed_at'] = None
            workflow.atomic_json(checkpoint, saved)
        if not saved['pending'] and saved.get('completed_at'):
            elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['completed_at'].replace('Z','+00:00'))).total_seconds()
            if elapsed < SOURCE_REFRESH_SECONDS:
                return {'ok':True,'status':'waiting_for_public_change','retry_after_seconds':int(SOURCE_REFRESH_SECONDS-elapsed)+1,
                        'receipt':str(checkpoint),'next_action':{'kind':'wait_for_public_change'},'browser_started':False}
            # Keep prior coverage receipts; a new bounded refresh must actually observe new DOM.
            saved['seen']={};saved['observed_titles']={};saved['navigation_seen']=[];saved['completed_at']=None
        reports=[];added=0
        with browser(work/'playwright-browser-profile') as page:
            page.goto('https://www.xxmanmi.com/',wait_until='domcontentloaded',timeout=30000)
            initial = latest_list(page,reader,sources)
            if not saved['pending'] and not saved['seen']:
                saved['pending']=[{'controls':[r['id']],'pages':0} for r in initial['radios']
                    if r['name']=='newNum' and r['text'] in sources]
            for _ in range(max_views):
                if not saved['pending']:
                    break
                action = saved['pending'][0]
                path = action['controls']
                requested_source=next((r['text'] for r in initial['radios']
                    if r['name']=='newNum' and r['id']==path[0]),None)
                if requested_source not in sources:
                    raise ValueError('Source path lacks its original visible source binding')
                page.goto('https://www.xxmanmi.com/',wait_until='domcontentloaded',timeout=30000)
                snapshot = latest_list(page,reader,sources)
                for control_id in path:
                    radio = next((r for r in snapshot['radios'] if r['id']==control_id),None)
                    if not radio:
                        raise ValueError('Observed source control no longer visible: '+control_id)
                    if not radio['checked']:
                        saved['action_inflight']={'path':path,'control':control_id,'at':workflow.utc_now()}
                        workflow.atomic_json(checkpoint,saved)
                        label = page.locator('label[for='+json.dumps(control_id)+']')
                        if not label.is_visible():
                            raise ValueError('Observed source label not currently visible')
                        label.click();page.wait_for_timeout(1500);snapshot=settled_list(page,reader)
                        saved.pop('action_inflight',None)
                        workflow.atomic_json(checkpoint,saved)
                for _page in range(action.get('pages',0)):
                    pager=next((p for p in snapshot['pagers'] if not p['disabled']),None)
                    if not pager:
                        raise ValueError('Previously observed public pagination is no longer available')
                    saved['action_inflight']={'path':path,'pager':pager,'at':workflow.utc_now()}
                    workflow.atomic_json(checkpoint,saved)
                    controls=page.get_by_text(pager['text'],exact=True)
                    choices=[controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible()]
                    if len(choices)!=1:
                        raise ValueError('Public pagination control is not uniquely visible')
                    prior=digest(snapshot['items']);choices[0].click();snapshot=settled_list(page,reader)
                    if digest(snapshot['items'])==prior:
                        raise ValueError('Public pagination did not produce a changed settled list')
                    saved.pop('action_inflight',None);workflow.atomic_json(checkpoint,saved)
                view_key=digest({'source':snapshot['source'],
                    'filters':[(r['name'],r['id']) for r in snapshot['radios'] if r['checked']],
                    'page':action.get('pages',0)})
                if snapshot['source'] not in sources or snapshot['view']!='latest':
                    raise ValueError('Actual visible source differs from allocated public view')
                if snapshot['source']!=requested_source:
                    proof={'status':'source_fallback','requested_source':requested_source,
                        'actual_source':snapshot['source'],'observed_at':workflow.utc_now(),
                        'snapshot':snapshot}
                    receipt=work/f'playwright-discovery-{digest(proof)[:24]}.json'
                    workflow.atomic_json(receipt,proof)
                    saved['seen'][digest({'requested_source':requested_source,'status':'source_fallback'})]=str(receipt)
                    reports.append({'source':requested_source,'status':'source_fallback',
                        'actual_source':snapshot['source'],'added':0,'receipt':str(receipt)})
                    saved['pending'].pop(0);workflow.atomic_json(checkpoint,saved)
                    page.wait_for_timeout(1500)
                    continue
                if view_key not in saved['seen']:
                    status='observed' if snapshot['items'] else 'explicit_empty' if snapshot['explicitEmpty'] else 'uncertain'
                    view={'status':status,'observed_at':workflow.utc_now(),'snapshot':snapshot}
                    view_path=work/f'playwright-discovery-{digest(view)[:24]}.json'
                    workflow.atomic_json(view_path,view)
                    coordctl.require_collection_running(workflow.read_state(workflow.DEFAULT_STATE))
                    enqueue=coordctl.enqueue_items(workflow.DEFAULT_STATE,owner,snapshot['items'],
                        refresh_evidence=view_path if slot=='main' and status=='observed' else None)
                    added+=enqueue['added'];saved['seen'][view_key]=str(view_path)
                    reports.append({'source':snapshot['source'],'status':status,'observed':len(snapshot['items']),
                                    'added':enqueue['added'],'receipt':str(view_path)})
                    for item in snapshot['items']:
                        if item['title']:
                            saved['observed_titles'].setdefault(snapshot['source'],{})[item['title']]=item['url']
                    for r in snapshot['radios']:
                        if r['name']=='newNum' or r['checked']:
                            continue
                        checked=[x['id'] for x in snapshot['radios'] if x['checked']
                                 and (x['name']=='newNum' or (x['name']=='newType1' and r['name']!='newType1'))]
                        candidate={'controls':[*checked,r['id']],'pages':0}
                        if candidate not in saved['pending']:
                            saved['pending'].append(candidate)
                    if any(not p['disabled'] for p in snapshot['pagers']) and action.get('pages',0)<100:
                        saved['pending'].append({'controls':path,'pages':action.get('pages',0)+1})
                saved['pending'].pop(0)
                workflow.atomic_json(checkpoint,saved)
                page.wait_for_timeout(1500)
            # Sources without a complete browseable list may require exact observed-title navigation.
            # Only engine-observed public titles are eligible. No model words or legacy search claims.
            needed_sources=navigation_sources(saved)
            if not saved['pending'] and needed_sources and len(reports)<max_views:
                observed_tab=next((t for t in initial.get('tabs',[]) if t['value']=='search'),None)
                if observed_tab is None:
                    raise RuntimeError('Actual visible search tab unavailable; discovery checkpoint retained')
                search_tab=page.locator('.weui-tabbar [data-id='+json.dumps(observed_tab['value'])+']')
                if search_tab.count()!=1 or not search_tab.is_visible():
                    raise RuntimeError('Observed search navigation no longer uniquely visible')
                if observed_tab is not None:
                    search_tab.click();page.locator('#searchInput').wait_for(state='visible')
                    for source in needed_sources:
                        for title in navigation_titles(saved,source):
                            key=digest({'source':source,'title':title})
                            if key in saved['navigation_seen']:
                                continue
                            if len(reports)>=max_views:
                                break
                            saved['action_inflight']={'source':source,'observed_title':title,'at':workflow.utc_now()}
                            workflow.atomic_json(checkpoint,saved)
                            box=page.locator('#searchInput');box.fill(title)
                            buttons=page.locator('#search botton, #search button').filter(has_text=re.compile('^'+re.escape(source)+'$'))
                            if buttons.count()!=1 or not buttons.is_visible():
                                raise ValueError('Engine-observed source search button is not uniquely visible')
                            buttons.click();page.wait_for_timeout(1500);snapshot=settled_list(page,reader)
                            if snapshot['source']!=source or snapshot['view']!='search' or snapshot['items'] and any(i['query']!=title for i in snapshot['items']):
                                raise ValueError('Visible source navigation binding mismatch')
                            status='observed' if snapshot['items'] else 'explicit_empty' if snapshot['explicitEmpty'] else 'uncertain'
                            proof={'status':status,'snapshot':snapshot,'observed_at':workflow.utc_now()}
                            receipt=work/f'playwright-navigation-{digest(proof)[:24]}.json'
                            workflow.atomic_json(receipt,proof)
                            enqueue=coordctl.enqueue_items(workflow.DEFAULT_STATE,owner,snapshot['items']);added+=enqueue['added']
                            reports.append({'source':source,'status':status,'observed':len(snapshot['items']),
                                            'added':enqueue['added'],'receipt':str(receipt)})
                            saved['navigation_seen'].append(key);saved.pop('action_inflight',None)
                            workflow.atomic_json(checkpoint,saved)
            unvisited=any(digest({'source':s,'title':t}) not in saved['navigation_seen']
                          for s in navigation_sources(saved)
                          for t in navigation_titles(saved,s))
            if not saved['pending'] and not unvisited:
                if not saved['seen']:
                    raise ValueError('No actual source receipts; discovery round is incomplete')
                saved['completed_at']=workflow.utc_now();workflow.atomic_json(checkpoint,saved)
        return {'ok':True,'engine':'playwright','command':'discover','views':reports,'added':added,
                'remaining_views':len(saved['pending']),'remaining_navigation':unvisited,'receipt':str(checkpoint),'browser_closed':True,
                'next_action':{'kind':'claim_detail' if added else 'discover' if saved['pending'] or unvisited else 'wait_for_public_change'},
                'waiting_condition':'bounded observed source views only; source/site exhaustion not claimed'}


def probe(url):
    reader=node('reader');started=time.monotonic()
    with browser() as page:
        evidence=observe_detail(page,{'url':public_url(url,True)},reader,
            owner='read-only-probe',batch='test-only',job_id=uuid.uuid4().hex)
    entries=[x for s in evidence['segments'] for x in s['observation']['entries']]
    media=[x for x in entries if re.match(r'^(https?://|//)',x['dataWeb'])]
    return {'ok':True,'test_only':True,'title':evidence['metadata']['observation']['title'],
        'entries':len(entries),'url_attributes':len(media),'opaque_entries':len(entries)-len(media),
        'segments':len(evidence['segments']),'complete_indices':True,'stable_dom':True,
        'seconds':round(time.monotonic()-started,3),'browser_closed':True,'database_writes':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('probe');p.add_argument('--url',required=True)
    for command in ['collect','discover','next']:
        p=sub.add_parser(command);p.add_argument('--thread',required=True)
        if command=='collect':p.add_argument('--batch')
        else:
            p.add_argument('--max-views',type=int,choices=range(1,21),default=8)
            p.add_argument('--source',action='append')
    args=parser.parse_args()
    try:
        if args.command=='probe':result=probe(args.url)
        elif args.command=='collect':result=collect(args.thread,args.batch)
        elif args.command=='discover':result=discover(args.thread,max_views=args.max_views,sources=args.source)
        else:
            result=collect(args.thread)
            if result.get('status')=='empty':
                result=discover(args.thread,max_views=args.max_views,sources=args.source)
                if result.get('added',0):
                    result={'discovery_receipt':result['receipt'],**collect(args.thread)}
        print(json.dumps(result,ensure_ascii=False),flush=True)
    except Exception as error:
        if args.command!='probe':
            try:
                import recoveryctl
                recoveryctl.record(args.thread,str(error),command='playwrightctl '+args.command,
                                   batch=getattr(args,'batch',None))
            except Exception:
                pass
        print(json.dumps({'ok':False,'error_type':type(error).__name__,'error':str(error)[:1800]},ensure_ascii=False),flush=True)
        raise SystemExit(1)


if __name__=='__main__':
    main()
