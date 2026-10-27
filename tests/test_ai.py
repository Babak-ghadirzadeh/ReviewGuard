import io
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit
from reviewguard.ai import assess
from reviewguard.store import Store

DOI='10.1234/review'

class LocalAIChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.s=Store(self.root)
        self.s.seed([{'doi':DOI}]);self.s.metadata(DOI,{'DOI':DOI,'title':['A systematic review'],'reference':[{'key':'ref1','DOI':'10.1234/trial'}]},'candidate')
        self.evidence='Trial A [1] was included in the quantitative synthesis.'
        path=self.root/'source.txt';path.write_text(self.evidence);self.sha=self.s.add_document(DOI,'local_fulltext',path)
        extracted=self.root/'extracted';extracted.mkdir()
        (extracted/'source.json').write_text(json.dumps({'document_sha':self.sha,'review_doi':DOI,'text':self.evidence,'tables':[]}))

    def tearDown(self):self.s.close();self.tmp.cleanup()

    def opener(self,proposal=None,remote=False,missing_model=False):
        case=self
        def fake(request,timeout):
            case.assertTrue(request.full_url.startswith('http://127.0.0.1:11434/'))
            route=urlsplit(request.full_url).path
            if route=='/api/tags':payload={'models':[] if missing_model else [{'name':'qwen3:1.7b','digest':'test-model-digest'}]}
            elif route=='/api/show':payload={'remote_host':'remote.example'} if remote else {}
            elif route=='/api/chat':
                body=json.loads(request.data)
                case.assertIn(case.evidence,body['format']['properties']['evidence_quote']['enum'])
                case.assertFalse(body['stream']);case.assertFalse(body['think']);case.assertEqual(body['options']['num_ctx'],4096)
                payload={'done':True,'done_reason':'stop','message':{'content':json.dumps(proposal or {'role':'included_quantitative','evidence_quote':case.evidence,'reason':'Target-specific included report.'})},'prompt_eval_count':200,'eval_count':60}
            else:case.fail('Unexpected local API endpoint.')
            return io.BytesIO(json.dumps(payload).encode())
        return fake

    def call(self,**kwargs):
        return assess(self.s,DOI,'ref1',self.evidence,'Table 1',self.sha,model='qwen3:1.7b',**kwargs)

    def test_valid_local_proposal_does_not_verify_inclusion(self):
        result=self.call(opener=self.opener())
        record=json.loads(Path(result['saved_proposal']).read_text())
        self.assertFalse(record['human_verified']);self.assertEqual(record['model_digest'],'test-model-digest')
        self.assertEqual(self.s.rows('SELECT verified FROM reference_candidates')[0]['verified'],0)
        self.assertEqual(self.s.rows('SELECT * FROM cases'),[])

    def test_fabricated_quote_rejected_and_preserved(self):
        with self.assertRaises(ValueError):self.call(opener=self.opener({'role':'included_quantitative','evidence_quote':'Invented quotation','reason':'Unsupported.'}))
        self.assertEqual(len(list((self.root/'ai_rejections').glob('*.json'))),1)
        self.assertFalse((self.root/'ai_proposals').exists())

    def test_excerpt_must_come_from_registered_source(self):
        with self.assertRaises(ValueError):assess(self.s,DOI,'ref1','Unregistered invented evidence.','Table 1',self.sha,model='qwen3:1.7b',opener=lambda *a,**k:self.fail('Must reject before API call.'))

    def test_remote_model_refused(self):
        with self.assertRaises(ValueError):self.call(opener=self.opener(remote=True))

    def test_missing_model_has_no_cloud_fallback(self):
        with self.assertRaises(ValueError):self.call(opener=self.opener(missing_model=True))

    def test_unapproved_model_tag_rejected(self):
        with self.assertRaises(ValueError):assess(self.s,DOI,'ref1',self.evidence,'Table 1',self.sha,model='remote:cloud',opener=lambda *a,**k:self.fail('Must reject before API call.'))

if __name__=='__main__':unittest.main()
