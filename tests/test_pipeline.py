"""Synthetic evidence only: no publisher text or proprietary article fixtures."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from reviewguard.store import Store
from reviewguard.domain import Notice,parse_date
from reviewguard.extract import extract_jats,extract_store,structured_html
from reviewguard.contributions import analyze_review
from reviewguard.pipeline import crossref_window,discover,load_config,run_pipeline
from reviewguard.http import Response,FetchError
from reviewguard.retrieve import check_tracked

REVIEW='10.1000/review';REPORT='10.1000/report1';ISSN='2045-2322'

def article(sentence=None,section='Results',labels=None,dois=True):
    sentence=sentence or 'Finally, 3 studies were included in the systematic review (Fig. 1) [<xref ref-type="bibr" rid="r1">1</xref>–<xref ref-type="bibr" rid="r3">3</xref>].'
    refs=''.join(f'<ref id="r{i}"><label>{(labels or {}).get(i,i)}.</label><mixed-citation>Trial {i}</mixed-citation>'+
                 (f'<pub-id pub-id-type="doi">10.1000/report{i}</pub-id>' if dois else '')+'</ref>' for i in range(1,4))
    return f'<article><front><article-meta><article-id pub-id-type="doi">{REVIEW}</article-id></article-meta></front><body><sec><title>{section}</title><p id="selection">{sentence}</p></sec></body><back><ref-list>{refs}</ref-list></back></article>'.encode()

class AutomaticEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
        self.s.seed([{'doi':REVIEW,'title':'A systematic review'}])
        self.s.metadata(REVIEW,{'DOI':REVIEW,'title':['A systematic review']},'candidate')
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def source(self,data=None):
        path=self.s.root/'source.xml';path.write_bytes(data or article())
        self.sha=self.s.add_document(REVIEW,'fulltext_xml',path,'https://example.org/article.xml')
        extract_store(self.s)
    def test_range_uses_labels_and_retains_figure_abbreviation(self):
        d=extract_jats(article());c=d['citation_contexts'][0]
        self.assertEqual(c['ref_keys'],['jats:r1','jats:r3','jats:r2'])
        self.assertIn('Fig. 1',c['text']);self.assertFalse(c['diagnostics'])
    def test_linked_reports_automatically_create_editorial_case(self):
        self.source();a=analyze_review(self.s,REVIEW)
        self.assertEqual(a['linked_reports'],3)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM decisions').fetchone()[0],0)
        self.s.ingest_notices([Notice('synthetic','1',REPORT,'retraction',parse_date('2026-10-01'),'10.1000/notice','',{})])
        self.s.reconcile();case=self.s.rows('SELECT * FROM cases')[0]
        self.assertEqual(case['category'],'editorial_assessment_required')
        self.assertEqual(self.s.snapshot()['coverage']['verified_included_reports'],0)
        self.assertEqual(self.s.snapshot()['coverage']['automatically_linked_reports'],3)
        self.assertEqual(self.s.snapshot()['coverage']['contributing_report_dois'],3)
        self.assertEqual(self.s.snapshot()['coverage']['contributing_reports_without_fresh_crossref_check'],3)
    def test_rerun_is_idempotent_and_cached(self):
        self.source();analyze_review(self.s,REVIEW);again=analyze_review(self.s,REVIEW)
        self.assertTrue(again['cached']);self.assertEqual(len(self.s.active_links()),3)
    def test_label_mismatch_abstains(self):
        self.source(article(labels={1:18}));a=analyze_review(self.s,REVIEW)
        self.assertEqual(a['linked_reports'],0);self.assertTrue(a['issues'])
    def test_missing_range_label_abstains(self):
        self.source(article(labels={2:9}));self.assertEqual(analyze_review(self.s,REVIEW)['linked_reports'],0)
    def test_general_citation_does_not_create_contribution(self):
        self.source(article('Background is reported by <xref ref-type="bibr" rid="r1">1</xref>.'))
        self.assertEqual(analyze_review(self.s,REVIEW)['linked_reports'],0)
    def test_previous_review_and_methods_are_not_contributions(self):
        cases=[('A previous review included 3 studies [<xref ref-type="bibr" rid="r1">1</xref>].','Results'),
               ('We included 3 studies [<xref ref-type="bibr" rid="r1">1</xref>].','Discussion'),
               ('Studies were included if they used the methods in <xref ref-type="bibr" rid="r1">1</xref>.','Methods'),
               ('3 studies were not included [<xref ref-type="bibr" rid="r1">1</xref>].','Results')]
        for text,section in cases:
            with self.subTest(text=text):
                self.source(article(text,section));self.assertEqual(analyze_review(self.s,REVIEW,True)['linked_reports'],0)
    def test_no_doi_is_explicit_unresolved(self):
        self.source(article(dois=False));a=analyze_review(self.s,REVIEW)
        self.assertEqual(a['linked_reports'],0);self.assertTrue(any('DOI' in i['issue'] for i in a['issues']))
    def test_count_mismatch_is_not_complete_coverage(self):
        self.source(article(article().decode().split('<p id="selection">')[1].split('</p>')[0].replace('3 studies','4 studies')))
        a=analyze_review(self.s,REVIEW);self.assertEqual(a['linked_reports'],3)
        self.assertFalse(a['coverage_complete']);self.assertTrue(any('Stated count' in i['issue'] for i in a['issues']))
    def test_changed_source_retires_automatic_links_and_reopens_case(self):
        self.source();analyze_review(self.s,REVIEW)
        self.s.ingest_notices([Notice('synthetic','1',REPORT,'retraction',parse_date('2026-10-01'),None,'',{})]);self.s.reconcile()
        case=self.s.rows('SELECT * FROM cases')[0];self.s.case_decision(case['id'],'closed','Reviewed','editor')
        self.source(article('No contributing list is provided.'));analyze_review(self.s,REVIEW);self.s.reconcile()
        self.assertFalse(self.s.active_links());self.assertEqual(self.s.rows('SELECT * FROM cases')[0]['category'],'inclusion_verification_required')
        self.assertEqual(self.s.rows('SELECT * FROM cases')[0]['state'],'open')
    def test_human_exclusion_overrides_automatic_link(self):
        self.source();analyze_review(self.s,REVIEW)
        self.s.decide_reference(REVIEW,'jats:r1','excluded','Corrected classification','Table 1','editor',self.sha)
        self.assertEqual(len(self.s.active_links()),2)
    def test_out_of_scope_never_automatically_linked(self):
        self.source();self.s.metadata(REVIEW,{'DOI':REVIEW,'title':['Protocol']},'out_of_scope')
        self.assertEqual(analyze_review(self.s,REVIEW)['status'],'out_of_scope');self.assertFalse(self.s.active_links())
    def test_html_matching_identity_and_linked_references(self):
        text=f'''<html><head><meta name="citation_doi" content="{REVIEW}"></head><body>
        <section data-title="Results"><p>Finally, 1 study was included [<a href="#ref-r1">1</a>].</p>
        <p>We included 3 studies [<a href="#ref-r1">1</a>].</p></section>
        <section data-title="References"><ol><li data-counter="1."><p id="ref-r1">A trial</p><a href="https://doi.org/{REPORT}">DOI</a></li></ol></section></body></html>'''
        d=structured_html(text,REVIEW)
        self.assertEqual(d['references'][0]['doi'],REPORT)
        self.assertEqual(d['citation_contexts'][1]['ref_keys'],['jats:r1'])
        self.assertFalse(structured_html(text,'10.1000/other')['references'])
    def test_written_count_and_same_article_relative_anchor(self):
        text=f'''<html><meta name="citation_doi" content="{REVIEW}"><section data-title="Results">
        <p>The final analysis included data from three studies <a href="#ref-r1">1</a>,<a href="/articles/review#ref-r2">2</a>,<a href="/articles/review#ref-r3">3</a>.</p></section>
        <section data-title="References"><ol>'''+''.join(f'<li data-counter="{i}."><p id="ref-r{i}">Trial {i}</p><a href="https://doi.org/10.1000/report{i}">DOI</a></li>' for i in range(1,4))+'</ol></section></html>'
        path=self.s.root/'source.html';path.write_text(text)
        self.s.add_document(REVIEW,'publisher_html',path,'https://example.org/articles/review');extract_store(self.s)
        self.assertEqual(analyze_review(self.s,REVIEW)['linked_reports'],3)
        d=structured_html(text.replace('/articles/review#ref-r2','/articles/other#ref-r2'),REVIEW)
        self.assertNotIn('jats:r2',d['citation_contexts'][0]['ref_keys'])

class JournalPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.config=load_config()
        self.config['focused_bootstrap']=False
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def work(self,title='A systematic review',doi=REVIEW):
        return {'DOI':doi,'title':[title],'ISSN':[ISSN],'reference':[{'key':'one','DOI':REPORT}]}
    def test_discovery_retains_candidate_without_references_and_logs_unknown(self):
        works=[self.work(),self.work('An unclassified study','10.1000/unknown')];works[0].pop('reference')
        class Fake:
            def get_json(_,url,params,**kw):return {'message':{'items':works}},None
        r=crossref_window(self.s,Fake(),ISSN,'2026-10-01','2026-10-01')
        self.assertEqual(r['status'],'complete');self.assertEqual(r['candidate_reviews'],1)
        self.assertEqual(len(self.s.rows('SELECT * FROM journal_works')),2)
        self.assertEqual(len(self.s.rows('SELECT * FROM reviews')),1)
    def test_wrong_journal_never_imported(self):
        work=self.work();work['ISSN']=['0000-0000']
        class Fake:
            def get_json(_,url,params,**kw):return {'message':{'items':[work]}},None
        self.assertEqual(crossref_window(self.s,Fake(),ISSN,'2026-10-01','2026-10-01')['status'],'failed')
        self.assertFalse(self.s.rows('SELECT * FROM reviews'))
    def test_bounded_window_never_marked_complete(self):
        works=[self.work(doi=f'10.1000/test{i}') for i in range(200)]
        class Fake:
            def get_json(_,url,params,**kw):return {'message':{'items':works,'next-cursor':'next'}},None
        r=crossref_window(self.s,Fake(),ISSN,'2026-10-01','2026-10-01',1)
        self.assertEqual(r['status'],'partial');self.assertEqual(self.s.rows('SELECT status FROM discovery_windows')[0]['status'],'partial')
    def test_index_watermark_not_advanced_on_failed_query(self):
        class Fake:
            def get_json(*a,**kw):raise FetchError('offline')
        self.config['backfill_since']='2026-10-01'
        discover(self.s,Fake(),self.config,today=dt.date(2026,10,2))
        self.assertIsNone(self.s.setting('index-watermark:'+ISSN))
    def test_completed_backfill_day_skipped_on_next_run(self):
        calls=[]
        class Fake:
            def get_json(_,url,params,**kw):calls.append(params['filter']);return {'message':{'items':[]}},None
        self.config['backfill_since']='2026-10-01'
        discover(self.s,Fake(),self.config,today=dt.date(2026,10,2));calls.clear()
        discover(self.s,Fake(),self.config,today=dt.date(2026,10,2))
        self.assertEqual(len(calls),1);self.assertIn('from-index-date',calls[0])
    def test_pipeline_works_without_network_or_model_and_preserves_failures(self):
        class Fake:
            def get_json(*a,**kw):raise FetchError('offline')
            def get(*a,**kw):raise FetchError('offline')
        r=run_pipeline(self.s,Fake(),self.config)
        self.assertEqual(r['status'],'incomplete');self.assertTrue(r['errors'])
        self.assertTrue((self.s.root/'reports/dashboard.html').exists())
        self.assertEqual(self.s.rows('SELECT status FROM runs')[-1]['status'],'incomplete')
    def test_full_automated_discovery_retrieval_analysis_and_retraction(self):
        payload=article();work=self.work();work['resource']={'primary':{'URL':'https://example.org/paper'}}
        work['license']=[{'URL':'https://creativecommons.org/licenses/by/4.0/'}]
        root=self.s.root
        def response(data,mime,url):
            raw=data if isinstance(data,bytes) else json.dumps(data).encode()
            sha=hashlib.sha256(raw).hexdigest();path=root/(sha+'.bin');path.write_bytes(raw)
            return Response(path,url,mime,sha)
        class Fake:
            def get_json(_,url,params=None,**kw):
                if '/journals/' in url:return {'message':{'items':[work]}},None
                if '/search' in url:return {'resultList':{'result':[{'doi':REVIEW,'pmcid':'PMC100','isOpenAccess':'Y'}]}},None
                if REVIEW in url:
                    data={'message':work};return data,response(data,'application/json',url)
                data={'message':{'DOI':url.split('/works/')[1]}}
                return data,response(data,'application/json',url)
            def get(_,url,**kw):
                if url.endswith('fullTextXML'):return response(payload,'application/xml',url)
                raise FetchError('No publisher route')
        def notices(store,http):
            store.ingest_notices([Notice('synthetic','1',REPORT,'retraction',parse_date('2026-10-01'),'10.1000/notice','',{})])
            return {'synthetic_only':True}
        self.config['ai']['enabled']=False;self.config['backfill_since']='2026-10-01'
        with patch('reviewguard.pipeline.refresh_rw',side_effect=notices):r=run_pipeline(self.s,Fake(),self.config)
        self.assertEqual(r['analysis'][0]['linked_reports'],3)
        self.assertEqual(self.s.rows('SELECT category FROM cases')[0]['category'],'editorial_assessment_required')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM decisions').fetchone()[0],0)
        self.assertTrue((root/'reports/editorial_cases.csv').exists())
    def test_included_check_skips_background_bibliography(self):
        self.s.seed([{'doi':REVIEW}]);self.s.metadata(REVIEW,{'DOI':REVIEW,'reference':[{'key':'x','DOI':REPORT}]},'candidate')
        class Fake:
            def get_json(*a,**kw):raise AssertionError('Background citation should not be checked')
        self.assertEqual(check_tracked(self.s,Fake(),only_included=True)['reports_attempted'],0)

if __name__=='__main__':unittest.main()
