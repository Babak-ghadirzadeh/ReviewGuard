"""Bounded, restartable journal -> evidence -> daily surveillance workflow."""
import datetime as dt
import json
import re
from .domain import clean_doi, eligibility, now, parse_date, fingerprint
from .http import FetchError
from .retrieve import CROSSREF, retrieve_article, refresh_rw, check_tracked
from .extract import extract_store, parse_html
from .contributions import analyze_all, propose_ambiguous
from .report import write_reports

def valid_issn(issn):
    if not re.fullmatch(r'\d{4}-\d{3}[\dX]',issn):raise ValueError('Invalid ISSN format.')
    digits=[int(x) if x!='X' else 10 for x in issn.replace('-','')]
    if sum(n*w for n,w in zip(digits,range(8,0,-1)))%11:raise ValueError('Invalid ISSN checksum.')

def remember_work(store,issn,work):
    doi=clean_doi(work.get('DOI'))
    if not doi:return False
    # Never accept a record assigned to an unrelated journal.
    if issn not in work.get('ISSN',[]):raise ValueError('Crossref discovery record has a different or missing ISSN.')
    title=(work.get('title') or [''])[0]
    abstract=parse_html(work.get('abstract','')).text
    decision=eligibility(title,abstract)['status']
    with store.con:
        store.con.execute('''INSERT INTO journal_works VALUES(?,?,?,?,?) ON CONFLICT(issn,doi)
        DO UPDATE SET eligibility=excluded.eligibility,title=excluded.title,last_seen=excluded.last_seen''',
        (issn,doi,decision,title,now()))
    if decision=='candidate':
        store.seed([{'doi':doi,'title':title}]);store.metadata(doi,work,decision)
        store.set_setting('journal-for:'+doi,issn)
        return True
    return False

def crossref_window(store,http,issn,start,end,max_pages=4,indexed=False):
    """Complete only traversed date windows; never equate a request cap to coverage."""
    cursor='*';records=candidates=0;status='partial';page_size=200;seen_pages=set()
    dates=f'from-index-date:{start},until-index-date:{end}' if indexed else f'from-pub-date:{start},until-pub-date:{end}'
    try:
        for page in range(max_pages):
            data,_=http.get_json(f'{CROSSREF}/journals/{issn}/works',
              {'filter':dates,'cursor':cursor,'rows':page_size},cache_seconds=0 if page else 20*3600)
            message=data.get('message') or {};items=message.get('items')
            if not isinstance(items,list):raise FetchError('Crossref discovery has no item list.','invalid_response')
            page_hash=fingerprint([w.get('DOI') for w in items])
            if items and page_hash in seen_pages:raise FetchError('Discovery returned a repeated page; coverage incomplete.','invalid_response')
            seen_pages.add(page_hash)
            for work in items:
                candidates+=int(remember_work(store,issn,work));records+=1
            new_cursor=message.get('next-cursor')
            if len(items)<page_size or not new_cursor:status='complete';break
            cursor=new_cursor
        result={'status':status,'records':records,'candidate_reviews':candidates,
                'issn':issn,'start':start,'end':end,'indexed':indexed}
        if not indexed:
            with store.con:
                store.con.execute('''INSERT INTO discovery_windows VALUES(?,?,?,?,?,?)
                ON CONFLICT(issn,start_date,end_date) DO UPDATE SET status=excluded.status,
                records=excluded.records,updated_at=excluded.updated_at''',(issn,start,end,status,records,now()))
        store.check('crossref-discovery',f'{issn}:{"index" if indexed else start}',status,json.dumps(result))
        return result
    except (FetchError,ValueError,TypeError) as exc:
        store.check('crossref-discovery',f'{issn}:{"index" if indexed else start}','failed',str(exc))
        return {'status':'failed','issn':issn,'start':start,'end':end,'records':records,'detail':str(exc)}

def discover(store,http,config,today=None):
    today=today or dt.datetime.now(dt.timezone.utc).date()
    start=dt.date.fromisoformat(config['backfill_since'])
    if start>today:raise ValueError('Backfill date is in the future.')
    results=[]
    for journal in config['journals']:
        if not journal.get('enabled',True):continue
        issn=journal['issn'];valid_issn(issn)
        if config.get('focused_bootstrap',True):
            # Fast useful starting coverage while the exhaustive backfile scan
            # progresses. Ranked search is explicitly *not* complete discovery.
            try:
                data,_=http.get_json(f'{CROSSREF}/journals/{issn}/works',
                 {'query.title':'systematic review meta-analysis','rows':20,
                  'filter':f'from-pub-date:{start},until-pub-date:{today}'},cache_seconds=20*3600)
                items=(data.get('message') or {}).get('items')
                if not isinstance(items,list):raise ValueError('Focused candidate search has no item list.')
                found=sum(int(remember_work(store,issn,w)) for w in items)
                store.check('crossref-focused',issn,'partial',f'{found} candidates in {len(items)} ranked results. Not exhaustive journal recall.')
            except (FetchError,ValueError,TypeError) as exc:
                store.check('crossref-focused',issn,'failed',str(exc))
        # An overlapping indexed-date query catches newly deposited old papers.
        # Advance the watermark only after a complete query traversal.
        watermark=store.setting('index-watermark:'+issn,str(today-dt.timedelta(days=2)))
        update_start=str(dt.date.fromisoformat(watermark)-dt.timedelta(days=1))
        update=crossref_window(store,http,issn,update_start,str(today),config['discovery_pages'],indexed=True)
        results.append(update)
        if update['status']=='complete':store.set_setting('index-watermark:'+issn,str(today))
        # Daily backfile windows avoid keeping expiring provider cursors across
        # runs. Recent windows are revisited through the indexed-date query.
        completed={r['start_date'] for r in store.rows("SELECT start_date FROM discovery_windows WHERE issn=? AND status='complete'",(issn,))}
        day=today;processed=0
        while day>=start and processed<config['backfill_days_per_run']:
            value=str(day);day-=dt.timedelta(days=1)
            if value in completed:continue
            result=crossref_window(store,http,issn,value,value,config['discovery_pages'])
            results.append(result);processed+=1
            if result['status']=='failed':break
    return results

def load_config(path=None):
    from importlib.resources import files
    config=json.loads(open(path).read()) if path else json.loads(files('reviewguard').joinpath('resources/monitor.json').read_text())
    required={'journals','backfill_since','discovery_pages','backfill_days_per_run','reviews_per_run','refresh_days','ai','crossref_reports_per_run'}
    if not required.issubset(config):raise ValueError('Monitor configuration has missing fields.')
    if parse_date(config['backfill_since']).precision!='day':raise ValueError('Backfill start must be YYYY-MM-DD.')
    if not isinstance(config['journals'],list) or not config['journals']:raise ValueError('At least one journal is required.')
    for journal in config['journals']:valid_issn(journal['issn'])
    for key in ['discovery_pages','backfill_days_per_run','reviews_per_run','refresh_days','crossref_reports_per_run']:
        if not isinstance(config[key],int) or not 1<=config[key]<=10000:raise ValueError(f'Invalid positive limit: {key}')
    if not isinstance(config['ai'],dict):raise ValueError('AI configuration must be an object.')
    limit=config['ai'].get('proposals_per_run',3)
    if not isinstance(limit,int) or not 0<=limit<=20:raise ValueError('AI proposals per run must be between 0 and 20.')
    return config

def run_pipeline(store,http,config,force=False):
    rid=store.start_run('monitor');result={'errors':[],'research_alpha':True};problems=False
    def step(name,fn):
        nonlocal problems
        try:
            value=fn();result[name]=value;return value
        except (FetchError,ValueError,OSError,RuntimeError,TypeError) as exc:
            problems=True;result['errors'].append({'stage':name,'detail':str(exc)})
            return None
    try:
        # Known contributing reports get notice checks even when discovery fails.
        step('notice_snapshot',lambda:refresh_rw(store,http))
        tracked=step('tracked_reports',lambda:check_tracked(store,http,only_included=True,max_reports=config['crossref_reports_per_run']))
        discoveries=step('discovery',lambda:discover(store,http,config))
        if discoveries and any(d['status']!='complete' for d in discoveries):problems=True
        if tracked and tracked['incomplete']:problems=True
        reviews=store.rows("""SELECT r.doi, max(a.checked_at) AS retrieved FROM reviews r
          LEFT JOIN attempts a ON a.review_doi=r.doi AND a.provider='pipeline' AND a.kind='refresh'
          WHERE r.eligibility IN ('candidate','unclassified') GROUP BY r.doi
          ORDER BY coalesce(max(a.checked_at),''),r.doi""")
        cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=config['refresh_days'])).isoformat()
        due=[r for r in reviews if force or not r['retrieved'] or r['retrieved']<cutoff]
        result['reviews_deferred']=max(0,len(due)-config['reviews_per_run'])
        result['retrieval']=[]
        for row in due[:config['reviews_per_run']]:
            doi=row['doi'];retrieved=step('retrieval_current',lambda:retrieve_article(store,http,doi))
            if retrieved:result['retrieval'].append(retrieved)
            store.attempt(doi,'pipeline','refresh','','success' if retrieved and retrieved['metadata'] else 'failed',
                          'Attempt recorded for fair retry ordering; failed sources remain visible.')
        result.pop('retrieval_current',None)
        step('extraction',lambda:extract_store(store))
        analysis=step('analysis',lambda:analyze_all(store,force=force))
        if config['ai'].get('enabled'):
            ai=step('local_ai',lambda:propose_ambiguous(store,config['ai'].get('model','qwen3:1.7b'),config['ai'].get('proposals_per_run',3)))
            if ai and any(a.get('status')=='unavailable_or_rejected' for a in ai):problems=True
        step('reconciliation',store.reconcile)
        # New links match the snapshot immediately. Crossref checks for newly
        # discovered reports run on the next job, keeping this run bounded.
        result['coverage_complete']=False
        result['backfill_pending']=any(
            store.con.execute("SELECT count(*) FROM discovery_windows WHERE issn=? AND status='complete'",(j['issn'],)).fetchone()[0]
            <(dt.datetime.now(dt.timezone.utc).date()-dt.date.fromisoformat(config['backfill_since'])).days+1
            for j in config['journals'] if j.get('enabled',True))
        problems=problems or result['reviews_deferred']>0
        result['status']='incomplete' if problems else 'completed_with_coverage_gaps'
        store.finish_run(rid,result['status'],result)
        result.update(write_reports(store,store.root/'reports'))
        return result
    except BaseException as exc:
        store.finish_run(rid,'interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',{'detail':str(exc)})
        raise
