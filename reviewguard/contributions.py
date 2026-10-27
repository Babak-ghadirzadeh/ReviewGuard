"""Automatic, source-backed contribution detection with explicit abstention.

The initial rule accepts linked bibliography targets in a sentence explicitly
describing the current review's final included synthesis. General citations,
eligibility criteria, discussion and other reviews' results are not enough.
No all-layout scientific accuracy claim is made by this adapter.
"""
import json
import re
from pathlib import Path
from .domain import fingerprint, INCLUDED
from .extract import EXTRACTION_VERSION

ENGINE_VERSION='linked-inclusion-sentence-v2'
NUMBER=r'(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)'
POSITIVE=re.compile(
    r'\b(?:we\s+included\s+(?:a\s+total\s+of\s+)?\d+\s+(?:\w+\s+){0,3}(?:studies|trials|reports|articles)|'
    r'\d+\s+(?:\w+\s+){0,3}(?:studies|trials|reports|articles)\b.{0,160}?'
    r'(?:were\s+(?:therefore\s+)?(?:included|incorporated)|met\s+(?:the\s+)?inclusion\s+criteria)|'
    r'(?:included|incorporated)\s+\d+\s+(?:\w+\s+){0,3}(?:studies|trials|reports|articles)|'
    r'(?:final|present|current|our)\s+(?:analysis|synthesis|review)\s+(?:included|incorporated|comprised)\s+'
    r'(?:data\s+from\s+)?'+NUMBER+r'\s+(?:studies|trials|reports|articles))',re.I)
NEGATIVE=re.compile(r'\b(?:excluded|not included|would be|will be|planned|eligible if|previous(?:ly)?|prior|another|other review|other meta|their review|their meta)\b',re.I)

def supported_sentence(context):
    text=context['text'];section=context.get('section','')
    # Restrict to Results/study selection/characteristics. This deliberately
    # abstains on publishers that do not preserve section structure.
    return (bool(re.search(r'results?|study selection|study characteristics|included studies|characteristics of',section,re.I))
            and not re.search(r'discussion|conclusion|introduction|background',section,re.I)
            and bool(POSITIVE.search(text)) and not NEGATIVE.search(text)
            and not context.get('diagnostics'))

def active_documents(store,doi):
    return store.rows('''SELECT d.* FROM documents d WHERE d.review_doi=? AND d.kind NOT LIKE 'metadata%'
      AND NOT EXISTS (SELECT 1 FROM documents n WHERE n.review_doi=d.review_doi AND n.kind=d.kind
       AND coalesce(n.source_url,'')=coalesce(d.source_url,'') AND n.id>d.id) ORDER BY d.id''',(doi,))

def analyze_review(store,doi,force=False):
    review=store.review(doi);docs=active_documents(store,doi)
    signature=fingerprint([ENGINE_VERSION,EXTRACTION_VERSION,review['eligibility'],review['metadata_json'],
                           [(d['id'],d['sha256']) for d in docs]])
    previous=store.rows('SELECT * FROM assessments WHERE review_doi=?',(doi,))
    if previous and previous[0]['input_hash']==signature and not force:
        return dict(json.loads(previous[0]['detail_json']),cached=True)
    links=[];issues=[];contexts_seen=0;structured=0;matches=[]
    if review['eligibility']!='candidate':
        status='out_of_scope' if review['eligibility']=='out_of_scope' else 'eligibility_unresolved'
        detail={'doi':doi,'status':status,'linked_reports':0,'issues':['No sufficient current-review eligibility evidence.']}
        store.replace_automatic_links(doi,[],signature,status,detail);return detail
    role='included_quantitative' if re.search(r'meta[\s‐‑–-]?analys',review['title'],re.I) else 'included_qualitative'
    for doc in docs:
        path=store.root/'extracted'/(str(doc['id'])+'-'+doc['sha256']+'.json')
        try:data=json.loads(path.read_text())
        except (OSError,ValueError):issues.append({'document':doc['id'],'issue':'Extraction unavailable.'});continue
        if data.get('extraction_version')!=EXTRACTION_VERSION:
            issues.append({'document':doc['id'],'issue':'Extraction version stale.'});continue
        refs={r['key']:r for r in data.get('references',[])}
        contexts=data.get('citation_contexts',[]);contexts_seen+=len(contexts)
        if contexts:structured+=1
        for c in contexts:
            if not supported_sentence(c):
                if POSITIVE.search(c['text']) and c.get('diagnostics'):
                    issues.append({'document':doc['id'],'locator':c['locator'],'issue':'; '.join(c['diagnostics'])})
                continue
            targets=c['ref_keys']
            if not targets:continue
            claimed=re.search(r'(\d+)\s+(?:\w+\s+){0,3}(?:studies|trials|reports|articles)\b',c['text'],re.I)
            if claimed and int(claimed[1])!=len(set(targets)):
                issues.append({'document':doc['id'],'locator':c['locator'],
                               'issue':f'Stated count {claimed[1]} differs from {len(set(targets))} linked bibliography reports; study/report reconciliation unresolved.'})
            matches.append({'document':doc['id'],'locator':c['locator'],'evidence':c['text'],'target_count':len(targets)})
            for key in targets:
                ref=refs.get(key,{})
                if not ref.get('doi'):
                    issues.append({'document':doc['id'],'locator':c['locator'],'ref_key':key,'issue':'Contributing report has no resolved DOI.'});continue
                links.append({'ref_key':key,'report_doi':ref['doi'],'role':role,'document_sha':doc['sha256'],
                              'evidence':c['text'],'locator':c['locator'],'method':ENGINE_VERSION})
    # Disagreeing full-text versions cannot be silently merged into a certainty.
    sets={}
    for link in links:sets.setdefault(link['document_sha'],set()).add(link['report_doi'])
    if len({tuple(sorted(s)) for s in sets.values()})>1:
        issues.append({'issue':'Structured sources disagree about contributing reports. Links retained as partial evidence; editorial verification needed.'})
    count=len({l['report_doi'] for l in links})
    status='monitoring_partial' if count else ('content_unavailable' if not docs else 'contribution_unresolved')
    detail={'doi':doi,'status':status,'linked_reports':count,'structured_documents':structured,
            'citation_contexts':contexts_seen,'evidence_matches':matches,'issues':issues,
            'engine':ENGINE_VERSION,'coverage_complete':False}
    if not structured:detail['issues'].append({'issue':'No supported structured citation layout; PDF/OCR or another adapter is needed.'})
    store.replace_automatic_links(doi,links,signature,status,detail)
    store.attempt(doi,'automatic-analysis','contribution_links','',status,json.dumps({'linked_reports':count,'issues':len(issues)}))
    return detail

def analyze_all(store,force=False):
    return [analyze_review(store,r['doi'],force) for r in store.rows('SELECT doi FROM reviews ORDER BY doi')]

def propose_ambiguous(store,model='qwen3:1.7b',limit=3):
    """Bounded local AI assistance; scientific qualification is still pending.

    AI cannot override the evidence rules. Normal automatic links do not need
    humans or an installed model. Proposals help investigate unsupported text.
    """
    from .ai import assess
    results=[];seen=set()
    prior=store.root/'ai_proposals'
    for path in prior.glob('*.json') if prior.exists() else []:
        try:
            p=json.loads(path.read_text());seen.add((p['review_doi'],p['ref_key'],p['document_sha'],p['locator'],p['model']))
        except (OSError,ValueError,KeyError):pass
    for review in store.rows("SELECT doi FROM reviews WHERE eligibility='candidate' ORDER BY doi"):
        for doc in active_documents(store,review['doi']):
            path=store.root/'extracted'/(str(doc['id'])+'-'+doc['sha256']+'.json')
            try:data=json.loads(path.read_text())
            except (OSError,ValueError):continue
            for c in data.get('citation_contexts',[]):
                if supported_sentence(c) or 'includ' not in c['text'].lower():continue
                for key in c['ref_keys']:
                    identity=(review['doi'],key,doc['sha256'],c['locator'],model)
                    if identity in seen:continue
                    if len(results)>=limit:return results
                    try:
                        result=assess(store,review['doi'],key,c['text'],c['locator'],doc['sha256'],model)
                        results.append(result)
                    except (ValueError,RuntimeError) as exc:
                        results.append({'status':'unavailable_or_rejected','detail':str(exc)})
                        if 'unavailable' in str(exc).lower() or 'not installed' in str(exc).lower():return results
    return results
