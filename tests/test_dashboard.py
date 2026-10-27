import io
import re
import tempfile
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.parse import urlencode
from reviewguard.store import Store
from reviewguard.report import serve

class DashboardDecisions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=self.tmp.name
        store=Store(self.root)
        with store.con:
            store.con.execute("INSERT INTO cases(review_doi,report_doi,category,fingerprint,first_seen,last_changed,detail_json) VALUES('10.1000/r','10.1000/s','editorial_assessment_required','hash','2026-10-01','2026-10-01','{}')")
        store.close()
        class Server:
            def __init__(_,address,handler):self.handler=handler
            def serve_forever(_):pass
            def server_close(_):pass
        with patch('http.server.ThreadingHTTPServer',Server):serve(self.root,8765)
    def tearDown(self):self.tmp.cleanup()
    def request(self,method='GET',path='/',fields=None,origin='http://127.0.0.1:8765',host='127.0.0.1:8765'):
        h=self.handler.__new__(self.handler);h.path=path;h.headers=Message()
        h.headers['Host']=host;h.headers['Origin']=origin
        payload=urlencode(fields or {}).encode();h.headers['Content-Length']=str(len(payload))
        h.headers['Content-Type']='application/x-www-form-urlencoded'
        h.rfile=io.BytesIO(payload);h.wfile=io.BytesIO();h.codes=[];h.response_headers={}
        h.send_response=lambda code:h.codes.append(code)
        h.send_error=lambda code,*a:h.codes.append(code)
        h.send_header=lambda name,value:h.response_headers.update({name:value})
        h.end_headers=lambda:None
        getattr(h,'do_'+method)()
        return h
    def token(self):
        h=self.request();return re.search(r'name="csrf" value="([^"]+)"',h.wfile.getvalue().decode())[1]
    def test_case_form_changes_state_and_keeps_audit_history(self):
        h=self.request('POST','/cases/1',{'csrf':self.token(),'actor':'Editor','note':'Assess the review','state':'under_review'})
        self.assertEqual(h.codes,[303]);s=Store(self.root)
        self.assertEqual(s.rows('SELECT state,unread FROM cases')[0],{'state':'under_review','unread':0})
        self.assertEqual(s.rows('SELECT actor FROM decisions')[0]['actor'],'Editor');s.close()
    def test_cross_origin_and_bad_token_rejected(self):
        for origin,token in [('https://evil.example',self.token()),('http://127.0.0.1:8765','invalid')]:
            h=self.request('POST','/cases/1',{'csrf':token,'actor':'x','note':'x','state':'closed'},origin)
            self.assertEqual(h.codes,[403])
    def test_wrong_host_and_invalid_state_rejected(self):
        self.assertEqual(self.request(host='evil.example').codes,[403])
        h=self.request('POST','/cases/1',{'csrf':self.token(),'actor':'Editor','note':'x','state':'erase'})
        self.assertEqual(h.codes,[409])
    def test_read_does_not_acknowledge_and_csp_limits_forms(self):
        h=self.request();self.assertEqual(h.codes,[200]);self.assertIn("form-action 'self'",h.response_headers['Content-Security-Policy'])
        s=Store(self.root);self.assertEqual(s.rows('SELECT unread FROM cases')[0]['unread'],1);s.close()

if __name__=='__main__':unittest.main()
