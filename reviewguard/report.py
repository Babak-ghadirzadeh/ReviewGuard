"""Read-only local editorial dashboard, atomic reports and safe CSV export."""
from pathlib import Path
import csv
import html
import io
import json
import os
import tempfile
import datetime as dt
from zoneinfo import ZoneInfo

def escape(x):return html.escape(str(x if x is not None else ''))

def localtime(value):
    try:return dt.datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Tehran')).strftime('%d %b %Y %H:%M')
    except (ValueError,TypeError):return str(value or 'No successful check')

def csv_safe(x):
    value=str(x if x is not None else '')
    return "'"+value if value.startswith(('\t','\r')) or value.lstrip().startswith(('=','+','-','@')) else value

def atomic_write(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='report-',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as f:f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:Path(tmp).unlink(missing_ok=True)

def html_report(snapshot,api_url='status.json',csrf=None):
    labels={'reviews':'Reviews discovered','automatically_linked_reports':'Automatic review–report links',
            'verified_included_reports':'Human-confirmed links','contributing_report_dois':'Distinct contributing reports',
            'contributing_reports_without_fresh_crossref_check':'Contributing reports awaiting fresh check',
            'current_notice_records':'Notice records available'}
    cards=''.join(f'<div class="card"><b>{escape(snapshot["coverage"].get(k,0))}</b><span>{escape(label)}</span></div>' for k,label in labels.items())
    reviews=''
    for r in snapshot['reviews']:
        kinds={d['kind'] for d in r['documents']}
        successes=[a for a in r['attempts'] if a['status']=='success']
        blocked=[a for a in r['attempts'] if a['status'] in {'access_blocked','failed','unresolved','extraction_failed'}]
        labels={'metadata_crossref':'Article metadata','publisher_html':'Publisher page','fulltext_xml':'Repository full text',
                'fulltext_pdf':'Publisher PDF','local_fulltext':'User-provided full text','supplement':'Supplement'}
        sources='; '.join(labels.get(k,k.replace('_',' ')) for k in sorted(kinds)) or 'No source files retrieved'
        details=''.join(f'<li>{escape(a["provider"])} / {escape(a["kind"])}: {escape(a["status"])} — {escape(a["detail"])}</li>' for a in blocked[:8])
        reviews+=f'''<tr><td><strong>{escape(r['sample_id'] or 'Discovered review')}</strong><br>{escape(r['title'])}<br><a href="https://doi.org/{escape(r['doi'])}" target="_blank" rel="noopener noreferrer">{escape(r['doi'])}</a></td>
        <td>{escape(sources)}<br><small>{len(r['documents'])} versioned files; {r['reference_count']} bibliography candidates</small></td>
        <td><span class="badge">{escape(r['assessment'].replace('_',' '))}</span><br>{r.get('automatically_included',0)} automatically linked reports<br>{r['verified_included']} human-confirmed reports<br><details><summary>Analysis and access details</summary><pre>{escape(json.dumps(r.get('automation',{}),indent=2,ensure_ascii=False))}</pre><ul>{details or '<li>Extraction completeness remains unverified.</li>'}</ul></details>
        <details><summary>Contributing report evidence</summary><pre>{escape(json.dumps(r.get('automatic_links',[]),indent=2,ensure_ascii=False))}</pre></details></td></tr>'''
    cases=''
    for c in snapshot['cases']:
        form=''
        if csrf:
            form=f'''<form action="/cases/{c['id']}" method="post"><input type="hidden" name="csrf" value="{escape(csrf)}">
            <label>Decision <select name="state"><option value="under_review">Under review</option><option value="closed">Close case</option><option value="open">Reopen case</option></select></label>
            <label>Your name <input name="actor" required maxlength="200"></label><label>Editorial note <textarea name="note" required maxlength="3000"></textarea></label><button type="submit">Save decision</button></form>'''
        cases+=f'''<tr><td>#{c['id']} {'<b class="badge">Unread</b>' if c['unread'] else ''}</td><td>{escape(c['review_doi'])}<br>Report: {escape(c['report_doi'])}</td>
        <td>{escape(c['category'].replace('_',' '))}<br>{escape(c['state'])}{form}</td><td><details><summary>Evidence and notice history</summary><pre>{escape(json.dumps(c['detail'],indent=2,ensure_ascii=False))}</pre></details></td></tr>'''
    health=''.join(f'<tr><td>{escape(p["provider"])}<br>{escape(p["target"])}</td><td>{escape(p["status"])}</td><td>{escape(localtime(p["attempted_at"]))}</td><td>{escape(localtime(p["succeeded_at"]))}</td></tr>' for p in snapshot['provider_health'])
    proposals=''.join(f'<tr><td>{escape(a["review_doi"])}<br>{escape(a["ref_key"])} · {escape(a["locator"])}</td><td>{escape(a["proposal"]["role"].replace("_"," "))}<br><small>Unverified proposal</small></td><td>{escape(a["proposal"]["evidence_quote"])}<br><small>{escape(a["proposal"]["reason"])}</small></td></tr>' for a in snapshot.get('ai_proposals',[]))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>ReviewGuard · Local editorial dashboard</title>
    <style>:root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f6fa;color:#17334a;font:15px/1.5 system-ui,sans-serif}}main{{max-width:1280px;margin:auto;padding:32px 24px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:center}}h1{{font-size:34px;margin:0}}h2{{margin-top:32px}}a{{color:#116a82}}.sub{{color:#536879}}.notice{{background:#fff4d8;border-left:4px solid #b77b16;padding:16px 20px;border-radius:8px;margin:24px 0}}.cards{{display:flex;flex-wrap:wrap;gap:12px}}.card{{background:white;padding:18px;border-radius:10px;min-width:150px;flex:1;border:1px solid #e0e6ee}}.card b{{display:block;font-size:28px}}.card span{{display:block;color:#536879;font-size:13px}}.panel{{background:white;border-radius:12px;border:1px solid #e0e6ee;overflow:auto}}table{{border-collapse:collapse;width:100%}}th,td{{padding:14px;text-align:left;vertical-align:top;border-bottom:1px solid #e7edf2}}th{{font-size:12px;text-transform:uppercase;background:#eaf0f5;color:#536879}}.badge{{display:inline-block;background:#fff0cb;color:#815500;padding:3px 8px;border-radius:20px;font-size:12px}}small{{color:#536879}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-width:650px;font-size:12px}}details{{margin-top:8px}}label{{display:block;margin-top:8px}}input,textarea,select,button{{font:inherit;max-width:100%;padding:6px;margin:3px 0}}textarea{{display:block}}button{{background:#116a82;color:white;border:0;border-radius:5px;cursor:pointer}}footer{{margin:30px 0;color:#536879;font-size:13px}}@media(max-width:650px){{main{{padding:18px 12px}}header{{display:block}}th,td{{padding:10px}}}}</style></head><body><main>
    <header><div><h1>ReviewGuard</h1><div class="sub">Journal retraction surveillance · research alpha 0.3</div></div><small>Updated {escape(localtime(snapshot['generated_at']))} Tehran<br><a href="{escape(api_url)}">Export current status</a> · <a href="/">Refresh dashboard</a></small></header>
    <div class="notice"><strong>Automatic monitoring with incomplete coverage.</strong> Source-backed contribution links are registered automatically. Unsupported layouts and ambiguous identities remain visible. Machine links have not been independently validated. Zero cases does not mean zero retractions. Actions remain editorial.</div>
    <div class="cards">{cards}</div><details><summary>Detailed coverage counts</summary><pre>{escape(json.dumps(snapshot['coverage'],indent=2))}</pre></details><h2>Editorial cases</h2><div class="panel"><table><thead><tr><th>Case</th><th>Review and report</th><th>Assessment</th><th>Evidence</th></tr></thead><tbody>{cases or '<tr><td colspan="4">No cases recorded. Inclusion coverage and source checks must be reviewed before interpreting this count.</td></tr>'}</tbody></table></div>
    <h2>Article retrieval</h2><div class="panel"><table><thead><tr><th>Review</th><th>Sources retrieved</th><th>Assessment coverage</th></tr></thead><tbody>{reviews or '<tr><td colspan="3">Import article seeds to begin.</td></tr>'}</tbody></table></div>
    <h2>Local AI analysis of ambiguous evidence</h2><div class="panel"><table><thead><tr><th>Source</th><th>Proposed role</th><th>Supporting evidence</th></tr></thead><tbody>{proposals or '<tr><td colspan="3">No local model proposals recorded. Automatic evidence links run independently; AI proposals remain experimental.</td></tr>'}</tbody></table></div>
    <h2>Journal discovery coverage</h2><div class="panel"><pre>{escape(json.dumps(snapshot.get('journal_discovery',[]),indent=2))}</pre></div>
    <h2>Source health</h2><div class="panel"><table><thead><tr><th>Provider / target</th><th>Status</th><th>Latest attempt (Tehran)</th><th>Latest success (Tehran)</th></tr></thead><tbody>{health or '<tr><td colspan="4">No external checks recorded.</td></tr>'}</tbody></table></div>
    <footer>{escape(snapshot['limitation'])}<br>Scientific Reports: effectiveness pilot. Scientific Reviews: compatibility pilot with a documented scope conflict. {'Use case forms to record editorial decisions.' if csrf else 'This exported snapshot is read-only. Open the local server for case decisions.'}</footer></main></body></html>'''

def write_reports(store,out):
    snap=store.snapshot();out=Path(out)
    atomic_write(out/'status.json',json.dumps(snap,ensure_ascii=False,indent=2))
    atomic_write(out/'dashboard.html',html_report(snap))
    f=io.StringIO(newline='');w=csv.writer(f)
    w.writerow(['case_id','review_doi','report_doi','category','state','unread','last_changed'])
    for c in snap['cases']:w.writerow([csv_safe(c[k]) for k in ['id','review_doi','report_doi','category','state','unread','last_changed']])
    atomic_write(out/'editorial_cases.csv',f.getvalue())
    return {'reports':str(out.resolve()),'cases':len(snap['cases']),'unread':sum(c['unread'] for c in snap['cases'])}

def serve(root,port=8765):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import hmac,re,secrets
    from urllib.parse import parse_qs
    from .store import Store
    csrf=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def valid_host(self):return self.headers.get('Host') in {f'127.0.0.1:{port}',f'localhost:{port}'}
        def do_GET(self):
            if not self.valid_host():
                self.send_error(403);return
            if self.path not in {'/','/api/status'}:
                self.send_error(404);return
            s=Store(root)
            try:snap=s.snapshot()
            finally:s.close()
            body=(json.dumps(snap,ensure_ascii=False) if self.path=='/api/status' else html_report(snap,'/api/status',csrf)).encode()
            self.send_response(200);self.send_header('Content-Type','application/json; charset=utf-8' if self.path=='/api/status' else 'text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers();self.wfile.write(body)
        def do_POST(self):
            origin=self.headers.get('Origin')
            if not self.valid_host() or origin not in {f'http://127.0.0.1:{port}',f'http://localhost:{port}'}:
                self.send_error(403);return
            match=re.fullmatch(r'/cases/(\d+)',self.path)
            if not match:self.send_error(404);return
            try:length=int(self.headers.get('Content-Length','0'))
            except ValueError:self.send_error(400);return
            if not 0<length<=20000 or self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':
                self.send_error(400);return
            try:form=parse_qs(self.rfile.read(length).decode('utf-8',errors='replace'),max_num_fields=10)
            except ValueError:self.send_error(400);return
            field=lambda k:form.get(k,[''])[0]
            if not hmac.compare_digest(field('csrf'),csrf):self.send_error(403);return
            if len(field('actor'))>200 or len(field('note'))>3000:self.send_error(400);return
            from .__main__ import writer_lock
            try:
                with writer_lock(root):
                    store=Store(root)
                    try:
                        store.case_decision(int(match[1]),field('state'),field('note'),field('actor'))
                        write_reports(store,store.root/'reports')
                    finally:store.close()
            except (ValueError,OSError,RuntimeError) as exc:
                self.send_error(409,str(exc));return
            self.send_response(303);self.send_header('Location','/');self.end_headers()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'ReviewGuard is available at http://127.0.0.1:{port} (this Mac only).',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
