from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from .domain import Notice,parse_date,clean_doi,now
from .store import Store
from .http import Http,FetchError
from .retrieve import retrieve_all,discover_journal,refresh_rw,check_tracked
from .extract import extract_store
from .report import write_reports,serve

PROJECT=Path(__file__).resolve().parent.parent

def default_data():
    local=PROJECT/'config/local-runtime.json'
    if os.getenv('RG_DATA_DIR'):return os.environ['RG_DATA_DIR']
    if local.exists():return json.loads(local.read_text())['data_dir']
    return str(PROJECT/'data' if (PROJECT/'config').is_dir() else Path.cwd()/'reviewguard-data')

def default_articles():
    source=PROJECT/'config/articles.json'
    if source.exists():return str(source)
    from importlib.resources import files
    return str(files('reviewguard').joinpath('resources/articles.json'))

@contextmanager
def writer_lock(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    with (root/'writer.lock').open('a+') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise RuntimeError('Another ReviewGuard job is running; this attempt is deferred.') from exc
        try:yield
        finally:fcntl.flock(f,fcntl.LOCK_UN)

def import_file(store,doi,path,kind='local_fulltext',licence='User-supplied; redistribution rights unverified'):
    doi=clean_doi(doi);store.review(doi)
    path=Path(path).resolve()
    if not path.is_file() or path.stat().st_size>80_000_000:raise ValueError('Local source is missing or exceeds the 80MB import limit.')
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    extension=path.suffix.lower()
    if extension not in {'.pdf','.docx','.xml','.html','.txt'}:raise ValueError('Unsupported local import format.')
    dest=store.root/'documents'/(sha+extension);dest.parent.mkdir(exist_ok=True)
    if not dest.exists():shutil.copyfile(path,dest)
    store.add_document(doi,kind,dest,'user-supplied local file','',licence)
    store.attempt(doi,'local-import',kind,'user-supplied local file','success','Imported as a source, not as verified inclusion data.')
    return {'review_doi':doi,'document_sha':sha,'path':str(dest)}

def demo(root):
    # Always isolated from the actual review database.
    demo_root=Path(root)/'synthetic-demo'
    s=Store(demo_root)
    try:
        review='10.1000/reviewguard-demo';report='10.1000/reviewguard-demo-report'
        s.seed([{'id':'SYNTHETIC DEMO','doi':review,'title':'Synthetic systematic review — demonstration only'}])
        s.metadata(review,{'DOI':review,'title':['Synthetic systematic review — demonstration only'],'reference':[{'key':'demo-ref','DOI':report,'article-title':'Synthetic trial'}]},'candidate')
        text=demo_root/'demo-evidence.xml'
        text.write_text(f'''<article><front><article-meta><article-id pub-id-type="doi">{review}</article-id></article-meta></front>
        <body><sec><title>Results</title><p id="selection">We included 2 studies in the final synthesis
        [<xref ref-type="bibr" rid="r1">1</xref>, <xref ref-type="bibr" rid="r2">2</xref>].</p></sec></body>
        <back><ref-list><ref id="r1"><label>1</label><pub-id pub-id-type="doi">{report}</pub-id></ref>
        <ref id="r2"><label>2</label><pub-id pub-id-type="doi">10.1000/reviewguard-demo-report2</pub-id></ref></ref-list></back></article>''')
        s.add_document(review,'synthetic_evidence',text,'synthetic demonstration only')
        extract_store(s)
        from .contributions import analyze_review
        analysis=analyze_review(s,review,force=True)
        e=Notice('synthetic','notice-1',report,'retraction',parse_date('2026-10-01'),'10.1000/reviewguard-demo-notice','Synthetic data',{'demo':True})
        s.ingest_notices([e]);result=s.reconcile();result.update(write_reports(s,demo_root/'reports'))
        return {'synthetic_demo_only':True,'database':str(demo_root),'automatic_analysis':analysis,'result':result}
    finally:s.close()

def main(argv=None):
    p=argparse.ArgumentParser(description='ReviewGuard: automatic journal retraction surveillance (research alpha).')
    p.add_argument('--data-dir',default=default_data(),help='Database, cache and source evidence directory.')
    p.add_argument('--max-requests',type=int,default=250,help='Hard per-run HTTP request cap; no paid service fallback.')
    p.add_argument('--refresh',action='store_true',help='Refresh HTTP responses instead of using fresh cached data.')
    sub=p.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init',help='Import the six article seeds without network requests.');init.add_argument('--articles',default=default_articles())
    sub.add_parser('retrieve',help='Retrieve metadata, accessible sources and observed supplement files.')
    local=sub.add_parser('import-file',help='Register a user-supplied source file.');local.add_argument('--doi',required=True);local.add_argument('--path',required=True);local.add_argument('--kind',choices=['local_fulltext','supplement'],default='local_fulltext')
    sub.add_parser('extract',help='Extract retrieved sources locally; preserve incomplete/OCR warnings.')
    disc=sub.add_parser('discover-journal',help='Bounded journal discovery with explicit coverage status.');disc.add_argument('--issn',required=True);disc.add_argument('--since',default='2026-01-01');disc.add_argument('--max-works',type=int,default=100);disc.add_argument('--max-pages',type=int,default=2)
    disc.add_argument('--query',help='Optional focused candidate search; this does not establish complete journal recall.')
    rw=sub.add_parser('import-notices',help='Import a local Retraction Watch CSV transactionally.');rw.add_argument('path');rw.add_argument('--complete-snapshot',action='store_true')
    sub.add_parser('refresh-notices',help='Fetch the full free RW snapshot with streaming size limits.')
    sub.add_parser('check',help='Check DOI-addressable bibliography reports; preserve inclusion uncertainty.')
    daily=sub.add_parser('daily',help='Refresh notices and check tracked reports; safe to schedule daily.');daily.add_argument('--force',action='store_true')
    daily.add_argument('--config',help='Journal monitor configuration JSON; saved for subsequent daily jobs.')
    monitor=sub.add_parser('monitor',help='Discover journal reviews, retrieve/analyze, and check contributing reports.')
    monitor.add_argument('--config',help='Journal monitor configuration JSON; saved for subsequent daily jobs.')
    monitor.add_argument('--force',action='store_true',help='Revisit content even within the refresh interval.')
    analyze=sub.add_parser('analyze',help='Automatically register source-backed contributing reports from extracted content.')
    analyze.add_argument('--force',action='store_true')
    sub.add_parser('doctor',help='Check Python, PDF support, local model, and data directory without external requests.')
    sched=sub.add_parser('schedule',help='Install or remove this Mac user\'s daily journal job.')
    sched.add_argument('--hour',type=int,default=9);sched.add_argument('--remove',action='store_true')
    decide=sub.add_parser('decide-reference',help='Record a human inclusion/identity decision with source evidence.')
    for flag in ['review','ref-key','role','evidence','locator','actor','document-sha']:decide.add_argument('--'+flag,required=True)
    decide.add_argument('--report-doi')
    case=sub.add_parser('decide-case',help='Acknowledge or update a case without erasing its history.');case.add_argument('id',type=int);case.add_argument('--state',choices=['open','under_review','closed'],required=True);case.add_argument('--note',required=True);case.add_argument('--actor',required=True)
    assess=sub.add_parser('assess',help='Ask local Ollama for an unverified inclusion proposal from source evidence.')
    for flag in ['review','ref-key','evidence-file','locator','document-sha']:assess.add_argument('--'+flag,required=True)
    assess.add_argument('--model',default='qwen3:4b')
    old=sub.add_parser('migrate-legacy',help='Read old flags into explicitly unverified legacy candidates.');old.add_argument('path')
    report=sub.add_parser('report',help='Write atomic HTML/JSON/CSV reports without marking cases read.');report.add_argument('--out')
    sub.add_parser('status',help='Print current coverage and case status.')
    server=sub.add_parser('serve',help='Open the read-only dashboard server on this Mac.');server.add_argument('--port',type=int,default=8765)
    sub.add_parser('demo',help='Run an isolated synthetic end-to-end example.')
    args=p.parse_args(argv)
    if args.max_requests<1:p.error('--max-requests must be positive.')
    if args.command=='serve':
        if not 1024<=args.port<=65535:p.error('Choose a port between 1024 and 65535.')
        serve(args.data_dir,args.port);return 0
    if args.command=='demo':
        with writer_lock(args.data_dir):print(json.dumps(demo(args.data_dir),indent=2))
        return 0
    try:
        with writer_lock(args.data_dir):
            store=Store(args.data_dir)
            try:
                http=Http(store.root/'cache',max_requests=args.max_requests,refresh=args.refresh)
                cmd=args.command
                if cmd=='init':result={'reviews_imported':len(entries:=json.loads(Path(args.articles).read_text()))};store.seed(entries)
                elif cmd=='retrieve':result=retrieve_all(store,http)
                elif cmd=='import-file':result=import_file(store,args.doi,args.path,args.kind)
                elif cmd=='extract':result=extract_store(store)
                elif cmd=='discover-journal':
                    if args.max_works<1 or args.max_pages<1:raise ValueError('Discovery limits must be positive.')
                    result=discover_journal(store,http,args.issn,args.since,args.max_works,args.max_pages,args.query)
                elif cmd=='import-notices':result=store.import_rw(args.path,args.complete_snapshot);result.update(store.reconcile())
                elif cmd=='refresh-notices':result=refresh_rw(store,http);result.update(store.reconcile())
                elif cmd=='check':result=check_tracked(store,http)
                elif cmd in {'daily','monitor'}:
                    from .pipeline import run_pipeline,load_config
                    config=load_config(args.config) if args.config else store.setting('monitor-config',load_config())
                    if args.config:store.set_setting('monitor-config',config)
                    last=store.rows("SELECT finished_at,status FROM runs WHERE kind='monitor' AND status='completed_with_coverage_gaps' ORDER BY id DESC LIMIT 1")
                    import datetime as dt
                    if cmd=='daily' and last and not args.force and (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(last[0]['finished_at'])).total_seconds()<20*3600:
                        result={'status':'not_due','last_success':last[0]['finished_at']}
                    else:
                        result=run_pipeline(store,http,config,args.force)
                elif cmd=='analyze':
                    from .contributions import analyze_all
                    result={'analysis':analyze_all(store,args.force),**store.reconcile()}
                elif cmd=='doctor':
                    from .setup import doctor
                    result=doctor(store)
                elif cmd=='schedule':
                    from .setup import schedule
                    result=schedule(store.root,args.hour,args.remove)
                elif cmd=='decide-reference':
                    store.decide_reference(args.review,args.ref_key,args.role,args.evidence,args.locator,args.actor,args.document_sha,args.report_doi);result=store.reconcile()
                elif cmd=='decide-case':store.case_decision(args.id,args.state,args.note,args.actor);result={'case':args.id,'state':args.state}
                elif cmd=='assess':
                    from .ai import assess
                    result=assess(store,clean_doi(args.review),args.ref_key,Path(args.evidence_file).read_text(),args.locator,args.document_sha,args.model)
                elif cmd=='migrate-legacy':result=store.migrate_legacy(args.path)
                elif cmd=='status':result=store.snapshot()
                elif cmd=='report':result=write_reports(store,args.out or store.root/'reports')
                if cmd not in {'report','status'}:write_reports(store,store.root/'reports')
                print(json.dumps(result,ensure_ascii=False,indent=2))
                if cmd in {'daily','monitor'} and result.get('status')=='incomplete':return 2
                return 0
            finally:store.close()
    except (ValueError,OSError,RuntimeError) as exc:
        print('ReviewGuard: '+str(exc),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
