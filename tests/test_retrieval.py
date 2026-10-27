import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile
from reviewguard.http import Http,Response,FetchError,validate_url,PublicRedirect
from reviewguard.extract import parse_html,extract_jats,extract_docx
from reviewguard.retrieve import retrieve_article,discover_journal,retrieve_all
from reviewguard.store import Store

DOI='10.1234/example'

class Reply(io.BytesIO):
    def __init__(self,payload=b'{}',url='https://example.org/source',content_type='application/json',length=None):
        super().__init__(payload);self.url=url
        self.headers={'Content-Type':content_type,'Content-Length':str(len(payload) if length is None else length)}
    def geturl(self):return self.url

class HttpChecks(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.cache=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def test_local_destinations_and_embedded_credentials_refused(self):
        private=[(2,1,6,'',('127.0.0.1',443))]
        with patch('reviewguard.http.socket.getaddrinfo',return_value=private):
            for url in ['https://example.org/file','file:///etc/passwd','https://user:pass@example.org/file']:
                with self.assertRaises(FetchError):validate_url(url)

    def test_redirect_to_local_destination_refused(self):
        private=[(2,1,6,'',('192.168.1.1',443))]
        with patch('reviewguard.http.socket.getaddrinfo',return_value=private):
            with self.assertRaises(FetchError):PublicRedirect().redirect_request(urllib.request.Request('https://example.org'),None,302,'',{},'https://192.168.1.1/file')

    def test_404_not_silent_empty_result(self):
        class Opener:
            def open(self,*a,**k):raise urllib.error.HTTPError('https://example.org',404,'not found',{},None)
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,opener=Opener(),sleeper=lambda n:None)
            with self.assertRaises(FetchError) as caught:h.get('https://example.org/x')
            self.assertEqual(caught.exception.status,'not_found')

    def test_retry_after_honored(self):
        sleeps=[]
        class Opener:
            calls=0
            def open(self,*a,**k):
                self.calls+=1
                if self.calls==1:raise urllib.error.HTTPError('https://example.org',429,'rate limit',{'Retry-After':'3'},None)
                return Reply()
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,opener=Opener(),sleeper=sleeps.append);h.get('https://example.org/x')
            self.assertIn(3,sleeps);self.assertEqual(h.requests,2)

    def test_long_retry_after_is_deferred_without_long_sleep(self):
        class Opener:
            def open(self,*a,**k):raise urllib.error.HTTPError('https://example.org',429,'rate limit',{'Retry-After':'120'},None)
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,opener=Opener(),sleeper=lambda n:self.fail('Should defer rather than sleep.'))
            with self.assertRaises(FetchError) as caught:h.get('https://example.org/x')
            self.assertEqual(caught.exception.status,'deferred')

    def test_truncated_download_is_not_cached(self):
        class Opener:
            def open(self,*a,**k):return Reply(b'partial',length=100)
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,opener=Opener(),sleeper=lambda n:None)
            with self.assertRaises(FetchError):h.get('https://example.org/x')
            self.assertFalse(list(self.cache.glob('*.json')))

    def test_cache_reuses_response_without_network(self):
        class Opener:
            calls=0
            def open(self,*a,**k):self.calls+=1;return Reply(b'{"ok":true}')
        opener=Opener()
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,opener=opener,sleeper=lambda n:None)
            first=h.get('https://example.org/x');second=h.get('https://example.org/x')
            self.assertFalse(first.cached);self.assertTrue(second.cached);self.assertEqual(opener.calls,1)

    def test_cached_response_available_without_dns(self):
        class Opener:
            def open(self,*a,**k):return Reply()
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,opener=Opener(),sleeper=lambda n:None);h.get('https://example.org/x')
        with patch('reviewguard.http.validate_url',side_effect=FetchError('offline')):
            self.assertTrue(h.get('https://example.org/x').cached)

    def test_authorization_not_forwarded_to_new_redirect_host(self):
        req=urllib.request.Request('https://api.openalex.org/x',headers={'Authorization':'Bearer private-key'})
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            redirected=PublicRedirect().redirect_request(req,None,302,'',{},'https://example.org/file')
            self.assertIsNone(redirected.get_header('Authorization'))

    def test_per_run_request_cap_defers(self):
        class Opener:
            def open(self,*a,**k):return Reply()
        with patch('reviewguard.http.validate_url',side_effect=lambda url:url):
            h=Http(self.cache,max_requests=1,opener=Opener(),sleeper=lambda n:None)
            h.get('https://example.org/x')
            with self.assertRaises(FetchError) as caught:h.get('https://example.org/y')
            self.assertEqual(caught.exception.status,'deferred')

class ExtractionChecks(unittest.TestCase):
    def test_boolean_html_attribute_does_not_crash_supplement_discovery(self):
        parsed=parse_html('<html><meta name="citation_doi" content="10.1234/example"><a download data-test="supp-info-link" href="/files/supplement.docx">Supplementary Table 1</a></html>','https://example.org/article')
        self.assertEqual(parsed.supplements[0]['url'],'https://example.org/files/supplement.docx')

    def test_html_does_not_execute_or_include_script_in_evidence(self):
        parsed=parse_html('<html><script>malicious()</script><p>Source evidence.</p><table><tr><td>Trial A</td><td>2024</td></tr></table></html>')
        self.assertNotIn('malicious',parsed.text);self.assertEqual(parsed.rows[0]['cells'],['Trial A','2024'])

    def test_jats_verifies_article_and_retains_reference_roles_as_unknown(self):
        xml=b'<article><front><article-id pub-id-type="doi">10.1234/example</article-id></front><body><p id="p1">Studies were included.</p><table-wrap id="t1"><table><tr><td>Trial A</td></tr></table></table-wrap></body><back><ref-list><ref id="R1"><mixed-citation>Trial A<pub-id pub-id-type="doi">10.1234/trial</pub-id></mixed-citation></ref></ref-list></back></article>'
        result=extract_jats(xml,DOI)
        self.assertEqual(result['references'][0]['doi'],'10.1234/trial');self.assertFalse(result['inclusion_complete'])
        with self.assertRaises(ValueError):extract_jats(xml,'10.1234/other')

    def test_xml_entity_or_malformed_response_rejected(self):
        for xml in [b'<article><bad>',b'<!DOCTYPE article [<!ENTITY x SYSTEM "file:///etc/passwd">]><article>&x;</article>']:
            with self.assertRaises(ValueError):extract_jats(xml,DOI)

    def test_docx_tables_are_extracted_with_locations(self):
        with tempfile.TemporaryDirectory() as td:
            file=Path(td)/'supp.docx'
            xml=b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Table S1</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Trial A</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>'
            with zipfile.ZipFile(file,'w') as z:z.writestr('word/document.xml',xml)
            result=extract_docx(file);self.assertEqual(result['tables'][0]['rows'][0]['cells'],['Trial A'])
            self.assertEqual(result['tables'][0]['locator'],'DOCX table 1')

class RetrievalChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root)
        self.store.seed([{'doi':DOI,'id':'test','scholar_url':'https://scholar.google.com/example'}])
    def tearDown(self):self.store.close();self.tmp.cleanup()

    def test_blocked_article_retains_metadata_and_incomplete_state(self):
        root=self.root
        class FakeHttp:
            def get_json(self,url,params=None,**kw):
                if 'crossref' in url:
                    data={'message':{'DOI':DOI,'title':['A systematic review'],'resource':{'primary':{'URL':'https://example.org/article'}},'reference':[{'key':'r1','DOI':'10.1234/trial'}]}}
                else:data={'resultList':{'result':[]}}
                path=root/'response.json';path.write_text(json.dumps(data))
                return data,Response(path,url,'application/json',__import__('hashlib').sha256(path.read_bytes()).hexdigest())
            def get(self,*a,**k):raise FetchError('HTTP 403','access_blocked')
        result=retrieve_article(self.store,FakeHttp(),DOI)
        self.assertTrue(result['metadata']);self.assertEqual(result['fulltext_files'],0)
        self.assertIn('incomplete',result['assessment'])
        self.assertEqual(self.store.snapshot()['coverage']['reference_candidates'],1)
        self.assertEqual(self.store.snapshot()['coverage']['verified_included_reports'],0)

    def test_pagination_without_free_key_explicitly_incomplete(self):
        class FakeHttp:
            openalex_key=''
            def get_json(self,*a,**kw):return {'meta':{'next_cursor':'next'},'results':[{'doi':DOI,'title':'A systematic review','referenced_works':[]}]},None
        result=discover_journal(self.store,FakeHttp(),'2045-2322',max_works=10,max_pages=2)
        self.assertEqual(result['status'],'incomplete_key_required');self.assertEqual(result['candidate_reviews'],1)
        self.assertTrue(self.store.rows("SELECT * FROM attempts WHERE kind='reference_coverage'"))

    def test_wrong_issn_checksum_rejected_before_network(self):
        with self.assertRaises(ValueError):discover_journal(self.store,None,'2045-2323')

    def test_per_article_error_does_not_discard_other_articles(self):
        self.store.seed([{'doi':'10.1234/second','id':'second'}])
        with patch('reviewguard.retrieve.retrieve_article',side_effect=[TypeError('bad external schema'),{'doi':'10.1234/second','fulltext_files':1,'supplement_files':1}]):
            results=retrieve_all(self.store,None)
        self.assertEqual(len(results),2);self.assertEqual(results[0]['assessment'],'incomplete')
        self.assertEqual(self.store.rows('SELECT * FROM runs')[0]['status'],'complete_with_gaps')

if __name__=='__main__':unittest.main()
