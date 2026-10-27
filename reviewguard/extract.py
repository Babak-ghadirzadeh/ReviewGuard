"""Local extraction with provenance; extraction is not inclusion adjudication."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin
from urllib.parse import urlsplit
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from .domain import clean_doi

EXTRACTION_VERSION = '0.3-structured-citations-3'

def citation_contexts(root, refs):
    """Retain linked citation targets and expand numeric ranges by *labels*.

    Labels are checked against link text. Bibliography ID suffixes are never
    assumed to be reference numbers (some publishers number these differently).
    """
    labels={r['key'].split(':',1)[1]:r.get('label','').rstrip('.') for r in refs}
    by_number={}
    for key,label in labels.items():
        if label.isdigit():by_number.setdefault(int(label),[]).append(key)
    parent={child:el for el in root.iter() for child in el}
    contexts=[]
    for index,p in enumerate(root.findall('.//body//p')):
        section=[];node=parent.get(p)
        while node is not None:
            if node.tag=='sec':
                title=node.find('title')
                if title is not None:section.insert(0,element_text(title))
            node=parent.get(node)
        links=[]
        def marked(el):
            if el.tag=='xref' and el.get('ref-type')=='bibr':
                n=len(links);links.append(el)
                return f' ⟦{n}⟧ '
            return (el.text or '')+''.join(marked(c)+(c.tail or '') for c in el)
        text=' '.join(marked(p).split())
        for sentence in re.split(r'(?<!Fig\.)(?<!fig\.)(?<!Tab\.)(?<!et al\.)(?<=[.!?])\s+(?=[A-Z])',text):
            ids=[int(n) for n in re.findall(r'⟦(\d+)⟧',sentence)]
            if not ids:continue
            targets=[];issues=[]
            for n in ids:
                el=links[n];rid=(el.get('rid') or '').split()
                shown=element_text(el).strip().rstrip('.')
                if len(rid)!=1 or rid[0] not in labels:
                    issues.append('Unresolved or multiple bibliography targets.');continue
                if shown.isdigit() and labels[rid[0]]!=shown:
                    issues.append('Citation text and bibliography label disagree.');continue
                targets.extend(rid)
            for a,b in re.findall(r'⟦(\d+)⟧\s*[-–—]\s*⟦(\d+)⟧',sentence):
                left=element_text(links[int(a)]).strip();right=element_text(links[int(b)]).strip()
                if not (left.isdigit() and right.isdigit() and 0<int(right)-int(left)<=500):
                    issues.append('Unsupported citation range.');continue
                for number in range(int(left),int(right)+1):
                    keys=by_number.get(number,[])
                    if len(keys)!=1:issues.append('Missing or duplicate bibliography label in range.')
                    else:targets.extend(keys)
            quote=re.sub(r'⟦(\d+)⟧',lambda m:element_text(links[int(m[1])]),sentence)
            contexts.append({'locator':p.get('id') or f'XML body paragraph {index+1}',
                             'section':' / '.join(section),'text':' '.join(quote.split()),
                             'ref_keys':['jats:'+k for k in dict.fromkeys(targets)],'diagnostics':issues})
    return contexts

class HTMLTree(HTMLParser):
    """Small inert HTML tree, used only to retain publisher citation anchors."""
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self):
        super().__init__(convert_charrefs=True);self.root=ET.Element('html');self.stack=[self.root]
    def handle_starttag(self,tag,attrs):
        el=ET.SubElement(self.stack[-1],tag,{k:v or '' for k,v in attrs})
        if tag not in self.VOID:self.stack.append(el)
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):
        el=self.stack[-1]
        if len(el):el[-1].tail=(el[-1].tail or '')+data
        else:el.text=(el.text or '')+data

def structured_html(text,expected_doi=None):
    """Convert explicit publisher bibliography anchors to the JATS adapter.

    No numeric bracket inference is made from plain prose. Unsupported layouts
    retain ordinary text extraction and are reported as incomplete.
    """
    parsed=parse_html(text)
    if expected_doi and clean_doi(parsed.meta.get('citation_doi'))!=clean_doi(expected_doi):
        return {'citation_contexts':[],'references':[],
                'structured_diagnostics':['Publisher HTML lacks matching citation_doi; automatic linking unavailable.']}
    tree=HTMLTree();tree.feed(text)
    article=ET.Element('article');body=ET.SubElement(article,'body');ref_list=ET.SubElement(article,'ref-list')
    # Springer/Nature expose one li per reference, with a stable ref-* anchor.
    for li in tree.root.iter('li'):
        anchor=next((el for el in li.iter() if el.get('id','').startswith('ref-')),None)
        if anchor is None:continue
        key=anchor.get('id')[4:];ref=ET.SubElement(ref_list,'ref',id=key)
        ET.SubElement(ref,'label').text=li.get('data-counter','').strip()
        ET.SubElement(ref,'mixed-citation').text=element_text(anchor)
        for a in li.iter('a'):
            doi=clean_doi(a.get('href')) or clean_doi(a.get('data-track-item_id'))
            if doi:ET.SubElement(ref,'pub-id',{'pub-id-type':'doi'}).text=doi;break
    def citation_id(el):
        href=el.get('href','');url=urlsplit(href)
        if not url.fragment.startswith('ref-'):return None
        if not url.path and not url.netloc:return url.fragment[4:]
        # Nature sometimes expands the final citation to a relative same-article
        # URL. Do not treat an anchor on a different article as a local citation.
        doi=clean_doi(expected_doi)
        if doi and not url.netloc and url.path.lower().rstrip('/').endswith(('/'+doi,'/'+doi.split('/',1)[1])):
            return url.fragment[4:]
        return None
    def copy(el):
        if el.tag in {'script','style','nav','header','footer'}:return None
        if el.tag=='a' and (rid:=citation_id(el)):
            out=ET.Element('xref',{'ref-type':'bibr','rid':rid});out.text=element_text(el);return out
        out=ET.Element(el.tag,dict(el.attrib));out.text=el.text
        for child in el:
            new=copy(child)
            if new is not None:new.tail=child.tail;out.append(new)
        return out
    # Publisher full-text sections are explicitly marked, excluding References,
    # Related content, editorial recommendations, and abstracts.
    for sec in tree.root.iter('section'):
        title=sec.get('data-title','')
        if not title or re.search(r'reference|abstract|author|acknowledg|funding|ethic|supplement',title,re.I):continue
        target=ET.SubElement(body,'sec');ET.SubElement(target,'title').text=title
        for p in sec.iter('p'):
            if any(citation_id(el) for el in p.iter('a')):
                target.append(copy(p))
    data=extract_jats(ET.tostring(article))
    return {'citation_contexts':data['citation_contexts'],'references':data['references'],
            'structured_diagnostics':[] if data['references'] else ['Publisher bibliography layout unsupported.']}

class ArticleHTML(HTMLParser):
    def __init__(self, base=''):
        super().__init__(convert_charrefs=True)
        self.base=base;self.parts=[];self.rows=[];self.supplements=[];self.meta={}
        self.ignore=0;self.row=None;self.cell=None;self.anchor=None;self.table=0;self.locator='body'

    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in {'script','style','nav','header','footer'}:
            self.ignore+=1
        if self.ignore:return
        if a.get('id'):self.locator=a['id']
        if tag=='meta' and a.get('name'):
            self.meta[a['name'].lower()]=a.get('content','')
        if tag=='table':self.table+=1
        if tag=='tr':self.row=[]
        if tag in {'td','th'}:self.cell=[]
        if tag=='a':self.anchor={'attrs':a,'text':[]}
        if tag in {'p','div','section','h1','h2','h3','li','br','tr'}:self.parts.append('\n')

    def handle_endtag(self,tag):
        if tag in {'script','style','nav','header','footer'} and self.ignore:
            self.ignore-=1;return
        if self.ignore:return
        if tag in {'td','th'} and self.cell is not None:
            if self.row is not None:self.row.append(' '.join(self.cell).strip())
            self.cell=None
        if tag=='tr' and self.row is not None:
            if any(self.row):self.rows.append({'locator':f'HTML table {self.table}, row {len(self.rows)+1}','cells':self.row})
            self.row=None
        if tag=='a' and self.anchor:
            attrs=self.anchor['attrs'];href=attrs.get('href','');label=' '.join(self.anchor['text'])
            combined=' '.join(str(x or '') for x in [href,label,*attrs.values()]).lower()
            if href and re.search(r'moesm|suppl_file|suppl-file|supplementary|supplemental',combined):
                link=urljoin(self.base,href)
                if not href.startswith('#') and link.startswith(('https://','http://')):
                    self.supplements.append({'url':link,'label':label[:300]})
            self.anchor=None

    def handle_data(self,data):
        if self.ignore:return
        data=re.sub(r'\s+',' ',data).strip()
        if not data:return
        self.parts.append(data+' ')
        if self.cell is not None:self.cell.append(data)
        if self.anchor:self.anchor['text'].append(data)

    @property
    def text(self):
        return re.sub(r'\n\s*\n+','\n',''.join(self.parts)).strip()

def parse_html(data,base=''):
    parser=ArticleHTML(base);parser.feed(data);return parser

def xml_root(data):
    if b'<!ENTITY' in data.upper() or b'<!DOCTYPE' in data.upper():
        # JATS often has an external DOCTYPE; do not resolve it or parse entities.
        data=re.sub(br'<!DOCTYPE[^>]*(?:\[[\s\S]*?\]\s*)?>',b'',data,flags=re.I)
        if b'<!ENTITY' in data.upper():raise ValueError('XML entity declarations refused.')
    return ET.fromstring(data)

def element_text(el):
    return ' '.join(' '.join(el.itertext()).split())

def extract_jats(data,expected_doi=None):
    try:root=xml_root(data)
    except ET.ParseError as exc:raise ValueError('Invalid full-text XML; assessment remains incomplete.') from exc
    if root.tag.split('}')[-1]!='article':raise ValueError('Response is not a JATS article.')
    dois=[clean_doi(x.text) for x in root.findall('.//article-id') if x.get('pub-id-type')=='doi']
    if expected_doi and clean_doi(expected_doi) not in dois:raise ValueError('Full-text XML article DOI does not match request.')
    blocks=[]
    for i,el in enumerate(root.findall('.//body//p')):
        blocks.append({'locator':el.get('id') or f'XML body paragraph {i+1}','text':element_text(el)})
    tables=[];diagnostics=[]
    for i,table in enumerate(root.findall('.//table-wrap')):
        rows=[{'cells':[element_text(c) for c in list(row) if c.tag in {'td','th'}]} for row in table.findall('.//tr')]
        tables.append({'locator':table.get('id') or f'XML table {i+1}','caption':element_text(table.find('caption')) if table.find('caption') is not None else '', 'rows':rows})
        if not rows:diagnostics.append(f"{table.get('id') or f'XML table {i+1}'}: no structured rows; inspect image/table source before claiming complete extraction.")
    refs=[]
    for i,el in enumerate(root.findall('.//ref-list/ref')):
        doi=None
        for d in el.findall('.//pub-id'):
            if d.get('pub-id-type')=='doi':doi=clean_doi(d.text)
        if not doi:
            for d in el.findall('.//ext-link'):
                href=d.get('{http://www.w3.org/1999/xlink}href','')
                doi=clean_doi(href) or clean_doi(element_text(d))
                if doi:break
        label=el.find('label')
        refs.append({'key':'jats:'+str(el.get('id') or i+1),'doi':doi,'text':element_text(el),
                     'label':element_text(label) if label is not None else ''})
    supplements=[]
    for el in root.findall('.//supplementary-material'):
        for child in el.iter():
            href=child.get('{http://www.w3.org/1999/xlink}href')
            if href:supplements.append({'href':href,'label':element_text(el)[:300]})
    return {'format':'jats','blocks':blocks,'tables':tables,'references':refs,'supplements':supplements,'diagnostics':diagnostics,
            'citation_contexts':citation_contexts(root,refs),
            'text':'\n'.join(b['text'] for b in blocks),'inclusion_complete':False}

def extract_docx(path):
    with zipfile.ZipFile(path) as z:
        infos=z.infolist()
        if sum(i.file_size for i in infos)>80_000_000:raise ValueError('DOCX uncompressed size limit exceeded.')
        root=xml_root(z.read('word/document.xml'))
    ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    def text(el):return ' '.join(x.text or '' for x in el.findall('.//w:t',ns))
    paragraphs=[{'locator':f'DOCX paragraph {i+1}','text':text(p)} for i,p in enumerate(root.findall('.//w:body/w:p',ns)) if text(p).strip()]
    tables=[]
    for i,t in enumerate(root.findall('.//w:tbl',ns)):
        rows=[{'cells':[text(c) for c in r.findall('w:tc',ns)]} for r in t.findall('w:tr',ns)]
        tables.append({'locator':f'DOCX table {i+1}','rows':rows})
    body='\n'.join(p['text'] for p in paragraphs)+'\n'+'\n'.join(' | '.join(r['cells']) for t in tables for r in t['rows'])
    return {'format':'docx','blocks':paragraphs,'tables':tables,'text':body,'references':[],
            'diagnostics':['Image-only figures/tables inside DOCX may need OCR; text extraction is not proof of completeness.'],'inclusion_complete':False}

def extract_pdf(path):
    try:import pdfplumber
    except ImportError as exc:raise ValueError('PDF extraction needs the optional pdfplumber package; source is retained for later processing.') from exc
    blocks=[];tables=[];diagnostics=[]
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages)>500:raise ValueError('PDF page limit exceeded.')
        for i,page in enumerate(pdf.pages,1):
            text=page.extract_text() or ''
            blocks.append({'locator':f'PDF page {i}','text':text})
            rotated=any(abs(c.get('matrix',(1,0))[1])>.1 for c in page.chars)
            if rotated:diagnostics.append(f'PDF page {i}: rotated text; orientation-aware table extraction required.')
            if len(text)<100:diagnostics.append(f'PDF page {i}: little text; OCR or visual assessment required.')
            for image in page.images:
                area=max(0,image['x1']-image['x0'])*max(0,image['bottom']-image['top'])
                if area/(page.width*page.height)>.1:
                    diagnostics.append(f'PDF page {i}: substantial image region; inspect/OCR tables even if surrounding text exists.');break
            for j,table in enumerate(page.extract_tables(),1):
                tables.append({'locator':f'PDF page {i}, table {j}','rows':[{'cells':row} for row in table]})
    return {'format':'pdf','blocks':blocks,'tables':tables,'text':'\n'.join(b['text'] for b in blocks),
            'references':[],'diagnostics':diagnostics,'inclusion_complete':False}

def extract_file(path,expected_doi=None):
    p=Path(path)
    with p.open('rb') as f:head=f.read(1000)
    if head.startswith(b'%PDF-'):return extract_pdf(p)
    if head.startswith(b'PK'):return extract_docx(p)
    text=p.read_text(encoding='utf-8',errors='replace')
    if re.search(r'<article(?:\s|>)',text[:10000],re.I) and not re.search(r'<html',text[:10000],re.I):
        return extract_jats(text.encode(),expected_doi)
    if '<html' in text[:5000].lower() or '<!doctype html' in text[:1000].lower():
        h=parse_html(text)
        structured=structured_html(text,expected_doi)
        return {'format':'html','blocks':[{'locator':'publisher HTML','text':h.text}],
                'tables':h.rows,'text':h.text,**structured,'diagnostics':['Publisher HTML completeness is unverified.'],'inclusion_complete':False}
    raise ValueError('Unsupported file format; source retained without claiming successful extraction.')

def extract_store(store):
    output=store.root/'extracted';output.mkdir(exist_ok=True)
    results=[]
    # Only the latest version of each source URL is active evidence. Historical
    # files remain retained, but cannot silently resurrect obsolete links.
    for doc in store.rows('''SELECT d.* FROM documents d WHERE kind NOT LIKE ? AND NOT EXISTS
      (SELECT 1 FROM documents n WHERE n.review_doi=d.review_doi AND n.kind=d.kind
       AND coalesce(n.source_url,'')=coalesce(d.source_url,'') AND n.id>d.id)''',('metadata%',)):
        result_path=output/(str(doc['id'])+'-'+doc['sha256']+'.json')
        if result_path.exists():
            try:
                if json.loads(result_path.read_text()).get('extraction_version')==EXTRACTION_VERSION:continue
            except (OSError,ValueError):pass
        try:
            data=extract_file(doc['path'],doc['review_doi'])
            data.update(document_sha=doc['sha256'],source_path=doc['path'],review_doi=doc['review_doi'],extraction_version=EXTRACTION_VERSION)
            result_path.write_text(json.dumps(data,ensure_ascii=False,indent=2))
            for ref in data.get('references',[]):
                with store.con:
                    store.con.execute('''INSERT INTO reference_candidates(review_doi,ref_key,report_doi,title,raw_json,updated_at)
                     VALUES(?,?,?,?,?,?) ON CONFLICT(review_doi,ref_key) DO UPDATE SET
                     report_doi=excluded.report_doi,raw_json=excluded.raw_json,updated_at=excluded.updated_at,
                     role=CASE WHEN reference_candidates.report_doi IS excluded.report_doi THEN reference_candidates.role ELSE 'uncertain' END,
                     verified=CASE WHEN reference_candidates.report_doi IS excluded.report_doi THEN reference_candidates.verified ELSE 0 END''',
                     (doc['review_doi'],ref['key'],ref['doi'],'',json.dumps(ref),__import__('reviewguard.domain',fromlist=['now']).now()))
            results.append({'document':doc['id'],'status':'extracted_unverified','tables':len(data['tables']),'diagnostics':data['diagnostics']})
        except (ValueError,OSError,zipfile.BadZipFile,ET.ParseError) as exc:
            results.append({'document':doc['id'],'status':'extraction_failed','detail':str(exc)})
        store.attempt(doc['review_doi'],'local-extraction',doc['kind'],doc['source_url'],results[-1]['status'],json.dumps(results[-1]))
    return results
