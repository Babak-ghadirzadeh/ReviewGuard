"""SQLite persistence: document versions, typed notices and durable cases."""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import csv
import json
import sqlite3
from .domain import now, fingerprint, clean_doi, rw_notices, current_notice_state, ROLES, INCLUDED

SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS reviews(
 doi TEXT PRIMARY KEY, sample_id TEXT, title TEXT NOT NULL DEFAULT '', scholar_url TEXT,
 journal TEXT, eligibility TEXT NOT NULL DEFAULT 'unclassified',
 metadata_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
 id INTEGER PRIMARY KEY, review_doi TEXT NOT NULL REFERENCES reviews(doi), kind TEXT NOT NULL,
 sha256 TEXT NOT NULL, path TEXT NOT NULL, source_url TEXT, content_type TEXT,
 licence TEXT, fetched_at TEXT NOT NULL, UNIQUE(review_doi,kind,sha256));
CREATE TABLE IF NOT EXISTS attempts(
 id INTEGER PRIMARY KEY, review_doi TEXT, provider TEXT NOT NULL, kind TEXT NOT NULL,
 url TEXT, status TEXT NOT NULL, detail TEXT, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reference_candidates(
 review_doi TEXT NOT NULL REFERENCES reviews(doi), ref_key TEXT NOT NULL,
 report_doi TEXT, title TEXT, raw_json TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'uncertain',
 verified INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL DEFAULT '', locator TEXT NOT NULL DEFAULT '',
 document_sha TEXT, decision_by TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(review_doi,ref_key));
CREATE INDEX IF NOT EXISTS reference_doi_idx ON reference_candidates(report_doi);
CREATE TABLE IF NOT EXISTS notices(
 content_hash TEXT PRIMARY KEY, source TEXT NOT NULL, source_key TEXT NOT NULL,
 original_doi TEXT NOT NULL, nature TEXT NOT NULL,
 date_raw TEXT, date_start TEXT, date_end TEXT, date_precision TEXT,
 notice_doi TEXT, reason TEXT, raw_json TEXT NOT NULL,
 current INTEGER NOT NULL DEFAULT 1, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS notices_doi_idx ON notices(original_doi,current);
CREATE TABLE IF NOT EXISTS source_checks(
 provider TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, detail TEXT,
 attempted_at TEXT NOT NULL, succeeded_at TEXT, PRIMARY KEY(provider,target));
CREATE TABLE IF NOT EXISTS runs(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 status TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS cases(
 id INTEGER PRIMARY KEY, review_doi TEXT NOT NULL, report_doi TEXT NOT NULL,
 category TEXT NOT NULL, fingerprint TEXT NOT NULL, unread INTEGER NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'open', first_seen TEXT NOT NULL, last_changed TEXT NOT NULL,
 detail_json TEXT NOT NULL, UNIQUE(review_doi,report_doi));
CREATE TABLE IF NOT EXISTS decisions(
 id INTEGER PRIMARY KEY, case_id INTEGER REFERENCES cases(id), review_doi TEXT, ref_key TEXT,
 action TEXT NOT NULL, note TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_candidates(
 issn TEXT, review_id TEXT, ref_id TEXT, first_seen TEXT, imported_at TEXT,
 PRIMARY KEY(issn,review_id,ref_id));
CREATE TABLE IF NOT EXISTS automatic_links(
 review_doi TEXT NOT NULL REFERENCES reviews(doi), ref_key TEXT NOT NULL, report_doi TEXT NOT NULL,
 role TEXT NOT NULL, document_sha TEXT NOT NULL, evidence TEXT NOT NULL, locator TEXT NOT NULL,
 method TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 PRIMARY KEY(review_doi,ref_key,document_sha,locator));
CREATE INDEX IF NOT EXISTS auto_doi_idx ON automatic_links(report_doi,active);
CREATE TABLE IF NOT EXISTS assessments(
 review_doi TEXT PRIMARY KEY REFERENCES reviews(doi), input_hash TEXT NOT NULL,
 status TEXT NOT NULL, detail_json TEXT NOT NULL, assessed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS discovery_windows(
 issn TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
 status TEXT NOT NULL, records INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
 PRIMARY KEY(issn,start_date,end_date));
CREATE TABLE IF NOT EXISTS journal_works(
 issn TEXT NOT NULL, doi TEXT NOT NULL, eligibility TEXT NOT NULL, title TEXT NOT NULL,
 last_seen TEXT NOT NULL, PRIMARY KEY(issn,doi));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''

class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.root/'reviewguard.sqlite3', timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute('PRAGMA foreign_keys=ON')
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.executescript(SCHEMA)
        if self.con.execute('SELECT MAX(version) FROM schema_version').fetchone()[0] not in {1,2}:
            raise RuntimeError('Unsupported database schema; do not modify this database with this version.')
        with self.con:self.con.execute('INSERT OR IGNORE INTO schema_version VALUES(2)')

    def close(self):
        self.con.close()

    def rows(self, sql, args=()):
        return [dict(r) for r in self.con.execute(sql, args)]

    def seed(self, entries):
        with self.con:
            for e in entries:
                doi = clean_doi(e.get('doi'))
                if not doi:
                    raise ValueError('Every seed article needs a valid DOI. Scholar URLs are provenance, not identifiers.')
                self.con.execute('''INSERT INTO reviews(doi,sample_id,title,scholar_url,updated_at)
                 VALUES(?,?,?,?,?) ON CONFLICT(doi) DO UPDATE SET
                 sample_id=coalesce(excluded.sample_id,reviews.sample_id),
                 scholar_url=coalesce(excluded.scholar_url,reviews.scholar_url), updated_at=excluded.updated_at''',
                 (doi,e.get('id'),e.get('title',''),e.get('scholar_url'),now()))

    def review(self, doi):
        rows=self.rows('SELECT * FROM reviews WHERE doi=?',(clean_doi(doi),))
        if not rows:
            raise ValueError('Unknown review DOI; import it before retrieval/assessment.')
        return rows[0]

    def metadata(self, doi, message, eligibility):
        doi=clean_doi(doi)
        if clean_doi(message.get('DOI')) != doi:
            raise ValueError('Metadata DOI does not match requested article.')
        with self.con:
            self.con.execute('UPDATE reviews SET title=?,journal=?,eligibility=?,metadata_json=?,updated_at=? WHERE doi=?',
             ((message.get('title') or [''])[0],(message.get('container-title') or [''])[0],eligibility,json.dumps(message),now(),doi))
            for i,ref in enumerate(message.get('reference') or []):
                key=str(ref.get('key') or f'crossref-{i+1}')
                self.con.execute('''INSERT INTO reference_candidates
                 (review_doi,ref_key,report_doi,title,raw_json,updated_at) VALUES(?,?,?,?,?,?)
                 ON CONFLICT(review_doi,ref_key) DO UPDATE SET
                 raw_json=excluded.raw_json,title=excluded.title,
                 role=CASE WHEN report_doi IS excluded.report_doi THEN role ELSE 'uncertain' END,
                 verified=CASE WHEN report_doi IS excluded.report_doi THEN verified ELSE 0 END,
                 report_doi=excluded.report_doi,updated_at=excluded.updated_at''',
                 (doi,key,clean_doi(ref.get('DOI')),ref.get('article-title',''),json.dumps(ref),now()))

    def add_document(self, doi, kind, path, source_url='', content_type='', licence=''):
        import hashlib
        path=Path(path).resolve()
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.con:
            self.con.execute('''INSERT OR IGNORE INTO documents
             (review_doi,kind,sha256,path,source_url,content_type,licence,fetched_at) VALUES(?,?,?,?,?,?,?,?)''',
             (clean_doi(doi),kind,sha,str(path),source_url,content_type,licence,now()))
        return sha

    def attempt(self, doi, provider, kind, url, status, detail=''):
        with self.con:
            self.con.execute('INSERT INTO attempts(review_doi,provider,kind,url,status,detail,checked_at) VALUES(?,?,?,?,?,?,?)',
                             (doi,provider,kind,url,status,str(detail)[:4000],now()))

    def check(self, provider, target, status, detail=''):
        ts=now()
        with self.con:
            self.con.execute('''INSERT INTO source_checks VALUES(?,?,?,?,?,?)
            ON CONFLICT(provider,target) DO UPDATE SET status=excluded.status,detail=excluded.detail,
            attempted_at=excluded.attempted_at,
            succeeded_at=CASE WHEN excluded.status='success' THEN excluded.succeeded_at ELSE source_checks.succeeded_at END''',
            (provider,target,status,str(detail)[:4000],ts,ts if status=='success' else None))

    def start_run(self, kind):
        with self.con:
            return self.con.execute('INSERT INTO runs(kind,started_at,status) VALUES(?,?,?)',(kind,now(),'running')).lastrowid

    def finish_run(self, rid, status, detail):
        with self.con:
            self.con.execute('UPDATE runs SET finished_at=?,status=?,detail=? WHERE id=?',(now(),status,json.dumps(detail),rid))

    def _notice(self, e):
        ts=now()
        self.con.execute('UPDATE notices SET current=0 WHERE source=? AND source_key=? AND original_doi=? AND content_hash<>?',
                         (e.source,e.source_key,e.original_doi,e.content_hash))
        self.con.execute('''INSERT INTO notices VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(content_hash) DO UPDATE SET current=1,last_seen=excluded.last_seen''',
         (e.content_hash,e.source,e.source_key,e.original_doi,e.nature,e.date.raw,e.date.start,e.date.end,e.date.precision,
          e.notice_doi,e.reason,json.dumps(e.raw),1,ts,ts))

    def ingest_notices(self, events):
        events=list(events)
        with self.con:
            for e in events:
                self._notice(e)
        return len(events)

    def import_rw(self, path, complete_snapshot=False):
        """Streaming transaction; an invalid/truncated CSV leaves last good state.
        complete_snapshot is explicit: fixture/partial imports never remove rows.
        """
        count=bad=unsupported=0
        required={'Record ID','OriginalPaperDOI','RetractionNature','RetractionDate'}
        previous=self.con.execute("SELECT count(*) FROM notices WHERE source='retraction-watch' AND current=1").fetchone()[0]
        try:
            with self.con, open(path,encoding='utf-8-sig',newline='') as f:
                reader=csv.DictReader(f)
                if not required.issubset(reader.fieldnames or []):
                    raise ValueError('Retraction Watch CSV has missing/changed required columns.')
                if complete_snapshot:
                    self.con.execute("UPDATE notices SET current=0 WHERE source='retraction-watch'")
                for row in reader:
                    if None in row or any(row.get(k) is None for k in required):
                        raise ValueError('Malformed/truncated CSV row; snapshot not committed.')
                    events=rw_notices(row)
                    if not events:
                        bad+=1
                    for event in events:
                        self._notice(event)
                        count+=1
                        if event.date.precision=='invalid':
                            unsupported+=1
                    if count+bad > 1_000_000:
                        raise ValueError('CSV row safety limit exceeded.')
                if count==0:
                    raise ValueError('No DOI-addressable notices; snapshot not committed.')
                if complete_snapshot and (bad+count < 1000 or (previous and count < previous * .8)):
                    raise ValueError('Suspiciously small complete snapshot; previous notices retained for review.')
            result={'events':count,'rows_without_usable_doi':bad,'invalid_dates':unsupported,'complete_snapshot':complete_snapshot}
            self.check('retraction-watch','snapshot' if complete_snapshot else 'partial-import','success',json.dumps(result))
            return result
        except Exception as exc:
            self.check('retraction-watch','snapshot' if complete_snapshot else 'partial-import','failed',str(exc))
            raise

    def decide_reference(self, doi, ref_key, role, evidence, locator, actor, document_sha, report_doi=None):
        if role not in ROLES:
            raise ValueError('Unknown inclusion role.')
        if not all(str(x or '').strip() for x in [evidence, locator, actor, document_sha]):
            raise ValueError('A human decision needs evidence, locator, actor and a document hash.')
        doi=clean_doi(doi)
        doc=self.rows('SELECT * FROM documents WHERE review_doi=? AND sha256=?',(doi,document_sha))
        if not doc:
            raise ValueError('Evidence document is not registered for this review.')
        row=self.rows('SELECT * FROM reference_candidates WHERE review_doi=? AND ref_key=?',(doi,ref_key))
        if not row:
            raise ValueError('Unknown bibliography reference key.')
        target=clean_doi(report_doi) if report_doi else row[0]['report_doi']
        if role in INCLUDED and not target:
            raise ValueError('Confirm a report DOI before creating an included-report link.')
        with self.con:
            self.con.execute('''UPDATE reference_candidates SET role=?,verified=1,evidence=?,locator=?,decision_by=?,
             document_sha=?,report_doi=?,updated_at=? WHERE review_doi=? AND ref_key=?''',
             (role,evidence,locator,actor,document_sha,target,now(),doi,ref_key))
            self.con.execute('INSERT INTO decisions(review_doi,ref_key,action,note,actor,created_at) VALUES(?,?,?,?,?,?)',
                             (doi,ref_key,role,json.dumps({'evidence':evidence,'locator':locator,'document_sha':document_sha}),actor,now()))

    def reconcile(self):
        """A report link and event history update a case; rendering never acknowledges it."""
        new=changed=0
        groups={}
        for ref in self.rows('SELECT * FROM reference_candidates WHERE report_doi IS NOT NULL ORDER BY review_doi,report_doi,ref_key'):
            groups.setdefault((ref['review_doi'],ref['report_doi']),[]).append(ref)
        for link in self.active_links():
            ref=dict(link,verified=1,decision_by='automatic:'+link['method'])
            groups.setdefault((ref['review_doi'],ref['report_doi']),[]).append(ref)
        for (review,report),refs in groups.items():
            events=self.rows('SELECT * FROM notices WHERE original_doi=? AND current=1 ORDER BY content_hash',(report,))
            old=self.rows('SELECT * FROM cases WHERE review_doi=? AND report_doi=?',(review,report))
            if not events and not old:
                continue
            state=current_notice_state(events)
            verified_included=any(r['verified'] and r['role'] in INCLUDED for r in refs)
            unresolved=any(not r['verified'] or r['role']=='uncertain' for r in refs)
            if state['status']=='conflicting_notice_history': category='notice_history_verification_required'
            elif state['status']=='retraction': category='editorial_assessment_required' if verified_included else ('inclusion_verification_required' if unresolved else 'noncontributing_citation')
            elif state['status']=='reinstated': category='reinstatement_assessment'
            elif state['status']=='expression_of_concern': category='concern_assessment'
            elif not events: category='notice_record_unavailable'
            else: category='other_notice_recorded'
            signature=fingerprint([category,[(r['ref_key'],r['role'],r['verified'],r['report_doi'],r['document_sha'],r['evidence'],r['locator']) for r in refs], [e['content_hash'] for e in events]])
            detail={'action':'Editorial assessment only; no author notification is sent.',
                    'status':state['status'],'references':refs,'notices':events}
            if not old and category in {'noncontributing_citation','other_notice_recorded'}:
                continue
            ts=now()
            with self.con:
                if not old:
                    self.con.execute('INSERT INTO cases(review_doi,report_doi,category,fingerprint,first_seen,last_changed,detail_json) VALUES(?,?,?,?,?,?,?)',
                                     (review,report,category,signature,ts,ts,json.dumps(detail)))
                    new+=1
                elif old[0]['fingerprint'] != signature:
                    self.con.execute("UPDATE cases SET category=?,fingerprint=?,unread=1,state='open',last_changed=?,detail_json=? WHERE id=?",
                                     (category,signature,ts,json.dumps(detail),old[0]['id']))
                    self.con.execute('INSERT INTO decisions(case_id,action,note,actor,created_at) VALUES(?,?,?,?,?)',
                                     (old[0]['id'],'reopened','Notice or inclusion evidence changed.','system',ts))
                    changed+=1
        return {'new_cases':new,'updated_cases':changed}

    def active_links(self,doi=None):
        """Human decisions override automatic claims for the same report.

        Machine links remain separately labelled and never become human verified.
        """
        sql='''SELECT a.* FROM automatic_links a WHERE a.active=1 AND NOT EXISTS
        (SELECT 1 FROM reference_candidates r WHERE r.review_doi=a.review_doi
         AND r.report_doi=a.report_doi AND r.verified=1 AND r.role NOT IN
         ('included_quantitative','included_qualitative'))'''
        if doi:sql+=' AND a.review_doi=?'
        return self.rows(sql+' ORDER BY a.review_doi,a.report_doi,a.ref_key,a.locator',(doi,) if doi else ())

    def replace_automatic_links(self,doi,links,input_hash,status,detail):
        with self.con:
            self.con.execute('UPDATE automatic_links SET active=0 WHERE review_doi=?',(doi,))
            for link in links:
                if link['role'] not in INCLUDED or not clean_doi(link['report_doi']):raise ValueError('Invalid automatic contribution link.')
                if not self.rows('SELECT id FROM documents WHERE review_doi=? AND sha256=?',(doi,link['document_sha'])):
                    raise ValueError('Automatic evidence document is not registered.')
                self.con.execute('''INSERT INTO automatic_links VALUES(?,?,?,?,?,?,?,?,1,?)
                ON CONFLICT(review_doi,ref_key,document_sha,locator) DO UPDATE SET
                 report_doi=excluded.report_doi,role=excluded.role,evidence=excluded.evidence,
                 method=excluded.method,active=1,updated_at=excluded.updated_at''',
                 (doi,link['ref_key'],link['report_doi'],link['role'],link['document_sha'],link['evidence'],link['locator'],link['method'],now()))
            self.con.execute('''INSERT INTO assessments VALUES(?,?,?,?,?)
            ON CONFLICT(review_doi) DO UPDATE SET input_hash=excluded.input_hash,status=excluded.status,
            detail_json=excluded.detail_json,assessed_at=excluded.assessed_at''',
            (doi,input_hash,status,json.dumps(detail),now()))

    def setting(self,key,default=None):
        row=self.rows('SELECT value FROM settings WHERE key=?',(key,))
        return json.loads(row[0]['value']) if row else default

    def set_setting(self,key,value):
        with self.con:self.con.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,json.dumps(value)))

    def case_decision(self, cid, state, note, actor):
        if state not in {'open','under_review','closed'} or not note.strip() or not actor.strip():
            raise ValueError('A case decision needs an allowed state, note and actor.')
        with self.con:
            cur=self.con.execute('UPDATE cases SET unread=0,state=? WHERE id=?',(state,cid))
            if cur.rowcount != 1: raise ValueError('Unknown case.')
            self.con.execute('INSERT INTO decisions(case_id,action,note,actor,created_at) VALUES(?,?,?,?,?)',(cid,state,note,actor,now()))

    def migrate_legacy(self, path):
        uri=Path(path).resolve().as_uri()+'?mode=ro'
        old=sqlite3.connect(uri,uri=True)
        try:
            count=0
            with self.con:
                for row in old.execute('SELECT issn,review_id,ref_id,first_seen FROM flags'):
                    count+=self.con.execute('INSERT OR IGNORE INTO legacy_candidates VALUES(?,?,?,?,?)',(*row,now())).rowcount
            return {'legacy_unverified_candidates_imported':count}
        finally:
            old.close()

    def snapshot(self):
        reviews=self.rows('SELECT * FROM reviews ORDER BY CASE WHEN sample_id IS NULL THEN 1 ELSE 0 END,sample_id,doi')
        for r in reviews:
            r.pop('metadata_json',None)
            r['documents']=self.rows('SELECT kind,sha256,path,source_url,licence,fetched_at FROM documents WHERE review_doi=? ORDER BY id',(r['doi'],))
            r['attempts']=self.rows('SELECT provider,kind,status,detail,url,checked_at FROM attempts WHERE review_doi=? ORDER BY id DESC LIMIT 30',(r['doi'],))
            r['reference_count']=self.con.execute('SELECT count(*) FROM reference_candidates WHERE review_doi=?',(r['doi'],)).fetchone()[0]
            r['verified_included']=self.con.execute("SELECT count(DISTINCT report_doi) FROM reference_candidates WHERE review_doi=? AND verified=1 AND role IN ('included_quantitative','included_qualitative')",(r['doi'],)).fetchone()[0]
            r['assessment']='incomplete: inclusion extraction and adjudication are not finished'
            r['automatic_links']=self.active_links(r['doi'])
            r['automatically_included']=len({a['report_doi'] for a in r['automatic_links']})
            assessment=self.rows('SELECT * FROM assessments WHERE review_doi=?',(r['doi'],))
            r['automation']=dict(assessment[0],detail=json.loads(assessment[0]['detail_json'])) if assessment else None
            if assessment:r['assessment']=assessment[0]['status']
        cases=self.rows('SELECT * FROM cases ORDER BY unread DESC,last_changed DESC')
        for c in cases: c['detail']=json.loads(c.pop('detail_json'))
        proposals=[]
        proposal_dir=self.root/'ai_proposals'
        if proposal_dir.exists():
            for path in sorted(proposal_dir.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]:
                try:proposals.append(json.loads(path.read_text()))
                except (OSError,ValueError):pass
        distinct_reports=self.con.execute('SELECT count(*) FROM (SELECT report_doi FROM reference_candidates WHERE report_doi IS NOT NULL UNION SELECT report_doi FROM automatic_links WHERE active=1)').fetchone()[0]
        checked_reports=self.con.execute("SELECT count(DISTINCT target) FROM source_checks WHERE provider='crossref-report' AND succeeded_at IS NOT NULL AND target IN (SELECT report_doi FROM reference_candidates)").fetchone()[0]
        return {'generated_at':now(),'reviews':reviews,'cases':cases,
                'ai_proposals':proposals,
                'provider_health':self.rows('SELECT * FROM source_checks ORDER BY provider,target'),
                'runs':self.rows('SELECT * FROM runs ORDER BY id DESC LIMIT 20'),
                'decisions':self.rows('SELECT * FROM decisions ORDER BY id DESC LIMIT 50'),
                'coverage':{'reviews':len(reviews),'reference_candidates':self.con.execute('SELECT count(*) FROM reference_candidates').fetchone()[0],
                            'verified_included_reports':self.con.execute("SELECT count(*) FROM (SELECT DISTINCT review_doi,report_doi FROM reference_candidates WHERE verified=1 AND role IN ('included_quantitative','included_qualitative'))").fetchone()[0],
                            'automatically_linked_reports':len({(a['review_doi'],a['report_doi']) for a in self.active_links()}),
                            'unresolved_references':self.con.execute("SELECT count(*) FROM reference_candidates WHERE verified=0 OR role='uncertain' OR report_doi IS NULL").fetchone()[0],
                            'distinct_report_dois':distinct_reports,'report_dois_without_crossref_success':distinct_reports-checked_reports,
                            'current_notice_records':self.con.execute('SELECT count(*) FROM notices WHERE current=1').fetchone()[0],
                            'legacy_unverified_candidates':self.con.execute('SELECT count(*) FROM legacy_candidates').fetchone()[0]},
                'journal_discovery':self.rows('SELECT * FROM discovery_windows ORDER BY issn,start_date DESC LIMIT 50'),
                'limitation':'Research alpha. Automatic evidence links are not human verified. No matching notice is not proof of no retraction. Unsupported layouts and missing identities remain incomplete.'}
