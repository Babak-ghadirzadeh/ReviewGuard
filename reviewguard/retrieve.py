"""DOI -> metadata -> publisher/repository -> observed supplement links.

Scholar links are preserved as user provenance; no Scholar scraping, guessed
download names, authentication bypass or paid OA locator is required.
"""
from pathlib import Path
import json
import re
import shutil
import urllib.parse
from .domain import clean_doi, crossref_notices, eligibility, abstract_text, now, parse_date
from .extract import parse_html, extract_jats
from .http import FetchError

CROSSREF='https://api.crossref.org'
EPMC='https://www.ebi.ac.uk/europepmc/webservices/rest'
RW_CSV='https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv'

def save_response(store,doi,kind,response,licence='',ext=None):
    with response.path.open('rb') as f:head=f.read(800)
    mime=response.content_type
    if head.startswith(b'%PDF-'):extension='.pdf'
    elif head.startswith(b'PK'):extension='.docx' if mime.endswith('wordprocessingml.document') or response.url.lower().split('?')[0].endswith('.docx') else '.zip'
    elif b'<html' in head.lower() or mime=='text/html':extension='.html'
    elif mime in {'application/xml','text/xml'} or head.lstrip().startswith(b'<?xml'):extension='.xml'
    else:extension=ext or '.bin'
    dest=store.root/'documents'/(response.sha256+extension)
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists():shutil.copyfile(response.path,dest)
    store.add_document(doi,kind,dest,response.url,mime,licence)
    return dest

def record_failure(store,doi,provider,kind,url,exc):
    store.attempt(doi,provider,kind,url,getattr(exc,'status','failed'),str(exc))

def retrieve_article(store,http,doi):
    doi=clean_doi(doi)
    if not doi:raise ValueError('Invalid article DOI.')
    review=store.review(doi)
    result={'doi':doi,'id':review['sample_id'],'metadata':False,'publisher_page':False,'repository_xml':False,
            'supplement_files':0,'fulltext_files':0,'notes':[]}
    msg={};publisher=None
    crossref_url=CROSSREF+'/works/'+urllib.parse.quote(doi,safe='/')
    try:
        data,response=http.get_json(crossref_url,cache_seconds=86400)
        msg=data.get('message') or {}
        if clean_doi(msg.get('DOI'))!=doi:raise FetchError('Crossref DOI mismatch.','identity_mismatch')
        title=(msg.get('title') or [''])[0]
        store.metadata(doi,msg,eligibility(title,parse_html(msg.get('abstract','')).text)['status'])
        store.ingest_notices(crossref_notices(msg))
        save_response(store,doi,'metadata_crossref',response,ext='.json')
        store.check('crossref',doi,'success','Article metadata and notice relations retrieved; reference list is not the included-study list.')
        store.attempt(doi,'crossref','metadata',crossref_url,'success',f"{len(msg.get('reference') or [])} bibliography entries")
        result['metadata']=True
        publisher=(msg.get('resource') or {}).get('primary',{}).get('URL') or msg.get('URL')
    except (FetchError,ValueError,TypeError) as exc:
        record_failure(store,doi,'crossref','metadata',crossref_url,exc)
        store.check('crossref',doi,'failed',str(exc))
        result['notes'].append('Metadata incomplete: '+str(exc))

    # Record all supplied licences. Only an active Creative Commons grant is
    # treated as an OA hint for automatic full-text PDF/XML downloads.
    licences=[l.get('URL','') for l in msg.get('license') or []]
    licence='; '.join(licences) or 'No reuse licence established from metadata; local use only.'
    today=now()[:10]
    oa=any('creativecommons.org/licenses/' in l.get('URL','') and
           (not (start:=parse_date((((l.get('start') or {}).get('date-parts')) or [[]])[0])).start or start.start<=today)
           for l in msg.get('license') or [])
    supplement_links=[]
    if publisher:
        try:
            response=http.get(publisher)
            raw=response.path.read_text(encoding='utf-8',errors='replace')
            parsed=parse_html(raw,response.url)
            if len(parsed.text)<500 or re.search(r'just a moment|verify you are human|access denied',parsed.text[:1500],re.I):
                raise FetchError('Publisher page is a challenge/access response.','access_blocked')
            page_doi=clean_doi(parsed.meta.get('citation_doi'))
            if page_doi and page_doi!=doi:raise FetchError('Publisher page DOI mismatch.','identity_mismatch')
            save_response(store,doi,'publisher_html',response,licence)
            result['publisher_page']=True
            supplement_links.extend(parsed.supplements)
            store.attempt(doi,'publisher','landing_page',publisher,'success',f'{len(parsed.supplements)} observed supplement links; full-text completeness unverified.')
        except (FetchError,OSError,ValueError,TypeError) as exc:
            record_failure(store,doi,'publisher','landing_page',publisher,exc)

    # Retrieve JATS only through the repository's official OA API, with DOI
    # verification. A repository 'N' flag does not override a publisher CC grant.
    search_url=EPMC+'/search'
    try:
        data,_=http.get_json(search_url,{'query':'DOI:'+doi,'format':'json','resultType':'core','pageSize':5})
        matches=[r for r in (data.get('resultList') or {}).get('result',[]) if clean_doi(r.get('doi'))==doi]
        store.attempt(doi,'europepmc','doi_search',search_url,'success' if matches else 'not_indexed',f'{len(matches)} exact DOI records')
        for record in matches:
            pmcid=record.get('pmcid') or ''
            if record.get('isOpenAccess')!='Y' or not re.fullmatch(r'PMC\d+',pmcid):continue
            xml_url=EPMC+'/'+pmcid+'/fullTextXML'
            try:
                response=http.get(xml_url,max_bytes=20_000_000)
                extracted=extract_jats(response.path.read_bytes(),doi)
                save_response(store,doi,'fulltext_xml',response,licence,ext='.xml')
                result['repository_xml']=True;result['fulltext_files']+=1
                store.attempt(doi,'europepmc','fulltext_xml',xml_url,'success',f"{len(extracted['references'])} references; inclusion not adjudicated.")
            except (FetchError,ValueError,TypeError) as exc:
                record_failure(store,doi,'europepmc','fulltext_xml',xml_url,exc)
    except (FetchError,ValueError,TypeError) as exc:
        record_failure(store,doi,'europepmc','doi_search',search_url,exc)

    # Only observed supplement URLs, never filenames guessed from a DOI.
    unique={x['url']:x for x in supplement_links}
    for url,link in list(unique.items())[:12]:
        try:
            response=http.get(url,max_bytes=60_000_000,cache_seconds=7*86400)
            with response.path.open('rb') as f:head=f.read(20)
            if not (head.startswith(b'PK') or head.startswith(b'%PDF-')):
                raise FetchError('Supplement link did not return a supported document; retained as unavailable.','unsupported_response')
            save_response(store,doi,'supplement',response,licence)
            result['supplement_files']+=1
            store.attempt(doi,'publisher','supplement',url,'success',link['label'])
        except (FetchError,ValueError,OSError) as exc:
            record_failure(store,doi,'publisher','supplement',url,exc)
    if not unique:
        store.attempt(doi,'publisher','supplement_discovery',publisher or '', 'unresolved',
                      'No supported supplement link discovered. Not evidence that no supplement exists.')

    if oa and not result['repository_xml']:
        seen=set()
        for entry in msg.get('link') or []:
            url=entry.get('URL');mime=entry.get('content-type','')
            if not url or url in seen or mime not in {'application/pdf','application/xml','text/xml'}:continue
            seen.add(url)
            try:
                response=http.get(url,max_bytes=60_000_000,cache_seconds=7*86400)
                with response.path.open('rb') as f:head=f.read(40)
                if mime=='application/pdf' and not head.startswith(b'%PDF-'):
                    raise FetchError('PDF link returned an access page or unsupported response.','invalid_response')
                if 'xml' in mime:extract_jats(response.path.read_bytes(),doi)
                save_response(store,doi,'fulltext_pdf' if mime=='application/pdf' else 'fulltext_xml',response,licence)
                result['fulltext_files']+=1
                store.attempt(doi,'publisher','fulltext',url,'success','Inclusion assessment still required.')
                break
            except (FetchError,ValueError,OSError) as exc:
                record_failure(store,doi,'publisher','fulltext',url,exc)
    if not oa:
        result['notes'].append('No Creative Commons grant established in Crossref; automatic publisher PDF download skipped. Public landing/supplement links and local imports remain available.')
    result['assessment']='incomplete: included-report identities and evidence are unadjudicated'
    result['scholar_url_preserved']=bool(review['scholar_url'])
    return result

def retrieve_all(store,http):
    rid=store.start_run('article-retrieval');results=[]
    try:
        for review in store.rows('SELECT doi FROM reviews ORDER BY sample_id,doi'):
            try:result=retrieve_article(store,http,review['doi'])
            except Exception as exc:
                store.attempt(review['doi'],'pipeline','article','','failed',f'{type(exc).__name__}: {exc}')
                result={'doi':review['doi'],'fulltext_files':0,'supplement_files':0,'assessment':'incomplete',
                        'error':f'{type(exc).__name__}: {exc}'}
            results.append(result)
            print(json.dumps(result,ensure_ascii=False),flush=True)
        status='complete_with_gaps' if any(not r['fulltext_files'] or not r['supplement_files'] for r in results) else 'retrieved_unadjudicated'
        store.finish_run(rid,status,results)
        return results
    except Exception as exc:
        store.finish_run(rid,'failed',{'error':str(exc),'completed':results})
        raise

def discover_journal(store,http,issn,since='2025-01-01',max_works=200,max_pages=2,query=None):
    if not re.fullmatch(r'\d{4}-\d{3}[\dX]',issn):raise ValueError('Invalid ISSN format.')
    digits=[int(x) if x!='X' else 10 for x in issn.replace('-','')]
    if sum(x*w for x,w in zip(digits,range(8,0,-1)))%11:raise ValueError('Invalid ISSN checksum.')
    from .domain import parse_date
    date=parse_date(since)
    if date.precision!='day':raise ValueError('Discovery start date must be a full YYYY-MM-DD date.')
    cursor='*';articles=candidates=unidentified=0;status='partial';detail='Bounded discovery, not a complete journal backfile.'
    try:
        for page in range(max_pages):
            if page and not http.openalex_key:
                status='incomplete_key_required';detail='A free OpenAlex key is needed to continue cursor pagination.';break
            params={'filter':f'primary_location.source.issn:{issn},from_publication_date:{since},type:article|review',
                    'per-page':min(200,max_works-articles),'cursor':cursor,
                    'select':'id,doi,title,publication_date,type,referenced_works,abstract_inverted_index,is_retracted,open_access,primary_location'}
            if query:params['search']=query
            data,_=http.get_json('https://api.openalex.org/works',params,cache_seconds=86400)
            if 'results' not in data:raise FetchError('OpenAlex result list is missing.','invalid_response')
            for work in data['results']:
                articles+=1;doi=clean_doi(work.get('doi'))
                if not doi:unidentified+=1;continue
                decision=eligibility(work.get('title'),abstract_text(work))
                if decision['status']=='candidate':
                    candidates+=1;store.seed([{'doi':doi,'title':work.get('title','')}])
                    with store.con:store.con.execute('UPDATE reviews SET eligibility=?,journal=? WHERE doi=?',('candidate',issn,doi))
                    if not work.get('referenced_works'):
                        store.attempt(doi,'openalex','reference_coverage','','incomplete','Candidate retained despite missing indexed references.')
            cursor=(data.get('meta') or {}).get('next_cursor')
            if not cursor or not data['results']:status='complete';detail='All records in this query traversed; index coverage remains provider-dependent.';break
            if articles>=max_works:break
        result={'issn':issn,'query':query,'articles_returned':articles,'candidate_reviews':candidates,'works_without_doi':unidentified,'status':status,'detail':detail}
        store.check('openalex',issn,'success' if status=='complete' else status,json.dumps(result))
        return result
    except (FetchError,ValueError,TypeError) as exc:
        store.check('openalex',issn,'failed',str(exc));raise

def refresh_rw(store,http):
    try:
        response=http.get(RW_CSV,max_bytes=256_000_000,cache_seconds=20*3600)
        return store.import_rw(response.path,complete_snapshot=True)
    except Exception as exc:
        store.check('retraction-watch','snapshot','failed',str(exc));raise

def check_tracked(store,http,only_included=False,max_reports=None):
    """Check DOI-addressable bibliography reports without labelling them included."""
    results=[]
    # Oldest/never checked reports first: a daily request cap must not starve
    # references that sort later in the bibliography.
    queue=store.rows('''SELECT DISTINCT r.report_doi FROM
      (SELECT report_doi FROM reference_candidates WHERE report_doi IS NOT NULL
       '''+("AND verified=1 AND role IN ('included_quantitative','included_qualitative')" if only_included else '')+'''
       UNION SELECT report_doi FROM automatic_links WHERE active=1) r
      LEFT JOIN source_checks c ON c.provider='crossref-report' AND c.target=r.report_doi
      WHERE r.report_doi IS NOT NULL ORDER BY coalesce(c.succeeded_at,''),r.report_doi''')
    for row in queue:
        if max_reports is not None and len(results)>=max_reports:break
        doi=row['report_doi'];url=CROSSREF+'/works/'+urllib.parse.quote(doi,safe='/')
        try:
            data,_=http.get_json(url,cache_seconds=20*3600)
            message=data.get('message') or {}
            if clean_doi(message.get('DOI'))!=doi:raise FetchError('Crossref report DOI mismatch.','identity_mismatch')
            events=crossref_notices(message);store.ingest_notices(events)
            store.check('crossref-report',doi,'success',f'{len(events)} typed notice relations found in this source; inclusion remains separately assessed.')
            results.append({'doi':doi,'status':'checked','notice_relations':len(events)})
        except (FetchError,ValueError,TypeError) as exc:
            store.check('crossref-report',doi,'failed',str(exc));results.append({'doi':doi,'status':'incomplete','detail':str(exc)})
            if getattr(exc,'status','')=='deferred':break
    unattempted=len(queue)-len(results)
    return {'reports_attempted':len(results),'reports_deferred':unattempted,
            'incomplete':sum(r['status']=='incomplete' for r in results)+unattempted,**store.reconcile()}
