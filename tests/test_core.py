import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from reviewguard.domain import clean_doi,parse_date,timing,eligibility,Notice,crossref_notices,current_notice_state
from reviewguard.store import Store
from reviewguard.report import write_reports,csv_safe,html_report
from reviewguard.ai import validate_proposal,guard_exclusion

REVIEW='10.1000/review';REPORT='10.1000/report'

class IdentifiersAndDates(unittest.TestCase):
    def test_doi_resolver_query_and_prefix(self):
        self.assertEqual(clean_doi(' DOI:10.1234/ABC '),'10.1234/abc')
        self.assertEqual(clean_doi('https://doi.org/10.1234/ABC?utm_source=test#section'),'10.1234/abc')

    def test_raw_doi_suffix_not_mangled(self):
        self.assertEqual(clean_doi('10.1234/a?b;c'),'10.1234/a?b;c')
        self.assertEqual(clean_doi('10.1234/a(b)'),'10.1234/a(b)')

    def test_invalid_or_missing_identifiers(self):
        for value in ['unavailable','N/A','http://evil.example/10.1234/a','10.1234/white space',None]:
            self.assertIsNone(clean_doi(value))

    def test_rw_dates_and_iso(self):
        self.assertEqual(parse_date('7/25/2026 0:00').start,'2026-07-25')
        self.assertEqual(parse_date('2024-02-29T00:00:00Z').start,'2024-02-29')

    def test_partial_date_preserves_uncertainty(self):
        year=parse_date('2024');month=parse_date('2024-02')
        self.assertEqual((year.start,year.end,year.precision),('2024-01-01','2024-12-31','year'))
        self.assertEqual(month.end,'2024-02-29')
        self.assertEqual(timing(year,parse_date('2024-07-01')),'ordering_indeterminate')

    def test_invalid_date_does_not_abort(self):
        self.assertIsNone(parse_date([2024,2,31]).start)
        self.assertEqual(parse_date('2024garbage').precision,'invalid')

    def test_date_ordering(self):
        review=parse_date('2024-05-01')
        self.assertEqual(timing(review,parse_date('2024-05-01')),'ordering_indeterminate')
        self.assertEqual(timing(review,parse_date('2024-05-02')),'after_review_publication')
        self.assertEqual(timing(review,parse_date('2024-04-30')),'before_review_publication')
        self.assertEqual(timing(review,parse_date('')),'date_unknown')

    def test_eligibility_is_candidate_not_verified(self):
        self.assertEqual(eligibility('A meta‑analysis of trials')['status'],'candidate')
        for title in ['A systematic review protocol','An umbrella review','A narrative review']:
            self.assertEqual(eligibility(title)['status'],'out_of_scope')
        self.assertEqual(eligibility('A randomized clinical trial')['status'],'unclassified')

    def test_crossref_directions_and_event_identity(self):
        original={'DOI':REPORT,'updated-by':[
            {'DOI':'10.1000/concern','type':'expression_of_concern','updated':{'date-parts':[[2020]]}},
            {'DOI':'10.1000/retraction','type':'retraction','updated':{'date-parts':[[2025,1,2]]}}]}
        events=crossref_notices(original)
        self.assertEqual(len(events),2)
        self.assertEqual({e.nature for e in events},{'retraction','expression_of_concern'})
        notice={'DOI':'10.1000/retraction','update-to':[{'DOI':REPORT,'type':'retraction'}]}
        self.assertEqual(crossref_notices(notice)[0].original_doi,REPORT)
        self.assertEqual(crossref_notices(notice)[0].notice_doi,'10.1000/retraction')

class Persistence(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.s=Store(self.root)
        self.s.seed([{'id':'test','doi':REVIEW,'title':'Test systematic review'}])
        self.s.metadata(REVIEW,{'DOI':REVIEW,'title':['Test systematic review'],'reference':[{'key':'ref1','DOI':REPORT}]},'candidate')
        evidence=self.root/'source.txt';evidence.write_text('Trial A contributed outcome data in Table 1.')
        self.sha=self.s.add_document(REVIEW,'local_fulltext',evidence)

    def tearDown(self):self.s.close();self.tmp.cleanup()

    def include(self,role='included_quantitative'):
        self.s.decide_reference(REVIEW,'ref1',role,'Trial A contributed outcome data','Table 1','human reviewer',self.sha)

    def event(self,nature='retraction',date='2025-01-01',key='event1',source='retraction-watch'):
        return Notice(source,key,REPORT,nature,parse_date(date),'10.1000/'+key,'',{'nature':nature,'date':date})

    def case(self):return self.s.rows('SELECT * FROM cases')[0]

    def test_unverified_reference_routes_to_verification(self):
        self.s.ingest_notices([self.event()]);self.s.reconcile()
        self.assertEqual(self.case()['category'],'inclusion_verification_required')

    def test_verified_included_routes_only_to_editorial_assessment(self):
        self.include();self.s.ingest_notices([self.event()]);self.s.reconcile()
        case=self.case();self.assertEqual(case['category'],'editorial_assessment_required')
        self.assertIn('no author notification',case['detail_json'])

    def test_background_and_excluded_do_not_create_impact_case(self):
        self.include('background');self.s.ingest_notices([self.event()]);self.s.reconcile()
        self.assertEqual(self.s.rows('SELECT * FROM cases'),[])

    def test_correction_alone_is_not_retraction(self):
        self.include();self.s.ingest_notices([self.event('correction')]);self.s.reconcile()
        self.assertEqual(self.s.rows('SELECT * FROM cases'),[])

    def test_concern_and_later_retraction_are_separate(self):
        self.include();self.s.ingest_notices([self.event('expression_of_concern','2020-01-01','concern'),self.event('retraction','2025-01-01','retraction')]);self.s.reconcile()
        self.assertEqual(self.case()['category'],'editorial_assessment_required')
        notices=json.loads(self.case()['detail_json'])['notices']
        self.assertEqual(next(e for e in notices if e['nature']=='retraction')['date_start'],'2025-01-01')

    def test_reinstatement_updates_without_deleting_history(self):
        self.include();self.s.ingest_notices([self.event()]);self.s.reconcile()
        self.s.ingest_notices([self.event('reinstatement','2026-01-01','reinstate')]);self.s.reconcile()
        self.assertEqual(self.case()['category'],'reinstatement_assessment')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM notices').fetchone()[0],2)

    def test_reinstatement_version_of_same_record_is_recognized(self):
        self.include();self.s.ingest_notices([self.event()]);self.s.reconcile()
        self.s.ingest_notices([self.event('reinstatement','2026-01-01','event1')]);self.s.reconcile()
        self.assertEqual(self.case()['category'],'reinstatement_assessment')
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM notices').fetchone()[0],2)
        self.assertEqual(self.s.con.execute('SELECT count(*) FROM notices WHERE current=1').fetchone()[0],1)

    def test_unknown_reinstatement_date_requires_history_review(self):
        self.include();self.s.ingest_notices([self.event(),self.event('reinstatement','','reinstate')]);self.s.reconcile()
        self.assertEqual(self.case()['category'],'notice_history_verification_required')

    def test_cross_provider_disagreement_not_silently_resolved(self):
        self.include();self.s.ingest_notices([self.event(source='crossref:publisher'),self.event('retraction','2025-01-01','rw'),self.event('reinstatement','2026-01-01','reinstate')]);self.s.reconcile()
        self.assertEqual(self.case()['category'],'notice_history_verification_required')

    def test_unchanged_case_is_deduplicated(self):
        self.include();self.s.ingest_notices([self.event()]);self.s.reconcile()
        self.assertEqual(self.s.reconcile(),{'new_cases':0,'updated_cases':0})
        self.assertEqual(len(self.s.rows('SELECT * FROM cases')),1)

    def test_new_notice_reopens_acknowledged_case(self):
        self.include();self.s.ingest_notices([self.event()]);self.s.reconcile()
        self.s.case_decision(self.case()['id'],'closed','Assessed by editor','editor')
        self.s.ingest_notices([self.event('retraction','2026-01-01','new-notice')]);self.s.reconcile()
        self.assertEqual((self.case()['unread'],self.case()['state']),(1,'open'))
        self.assertEqual(len(self.s.rows('SELECT * FROM decisions WHERE case_id IS NOT NULL')),2)

    def test_report_write_failure_does_not_acknowledge_cases(self):
        self.include();self.s.ingest_notices([self.event()]);self.s.reconcile()
        with patch('reviewguard.report.atomic_write',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):write_reports(self.s,self.root/'reports')
        self.assertEqual(self.case()['unread'],1)
        write_reports(self.s,self.root/'reports');self.assertEqual(self.case()['unread'],1)

    def test_missing_references_do_not_hide_review(self):
        self.s.metadata(REVIEW,{'DOI':REVIEW,'title':['Test systematic review'],'reference':[]},'candidate')
        self.assertEqual(self.s.snapshot()['coverage']['reviews'],1)
        self.assertIn('incomplete',self.s.snapshot()['reviews'][0]['assessment'])

    def test_changed_reference_identity_resets_verification(self):
        self.include()
        self.s.metadata(REVIEW,{'DOI':REVIEW,'title':['Test systematic review'],'reference':[{'key':'ref1','DOI':'10.1000/different'}]},'candidate')
        r=self.s.rows('SELECT * FROM reference_candidates')[0]
        self.assertEqual((r['verified'],r['role']),(0,'uncertain'))

    def csv(self,rows,malformed=False):
        path=self.root/'rw.csv'
        with path.open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['Record ID','OriginalPaperDOI','RetractionNature','RetractionDate','RetractionDOI'])
            w.writeheader();w.writerows(rows)
            if malformed:f.write('truncated,10.1000/report\n')
        return path

    def test_csv_dates_events_and_row_order(self):
        p=self.csv([{'Record ID':1,'OriginalPaperDOI':REPORT,'RetractionNature':'Retraction','RetractionDate':'7/25/2026 0:00'},
                    {'Record ID':2,'OriginalPaperDOI':REPORT,'RetractionNature':'Correction','RetractionDate':'8/1/2026 0:00'}])
        self.s.import_rw(p)
        rows=self.s.rows('SELECT * FROM notices');self.assertEqual(len(rows),2)
        self.assertEqual(next(r for r in rows if r['nature']=='retraction')['date_start'],'2026-07-25')

    def test_malformed_csv_rolls_back_previous_state(self):
        self.s.ingest_notices([self.event()])
        p=self.csv([{'Record ID':2,'OriginalPaperDOI':REPORT,'RetractionNature':'Correction','RetractionDate':'8/1/2026'}],True)
        with self.assertRaises(ValueError):self.s.import_rw(p,True)
        rows=self.s.rows('SELECT * FROM notices WHERE current=1');self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['nature'],'retraction')

    def test_small_snapshot_does_not_remove_old_events(self):
        self.s.ingest_notices([self.event()])
        p=self.csv([{'Record ID':2,'OriginalPaperDOI':REPORT,'RetractionNature':'Correction','RetractionDate':'8/1/2026'}])
        with self.assertRaises(ValueError):self.s.import_rw(p,True)
        self.assertEqual(self.s.rows('SELECT nature FROM notices WHERE current=1')[0]['nature'],'retraction')

    def test_provider_failure_keeps_last_success_and_marks_failure(self):
        self.s.check('test',REPORT,'success');self.s.check('test',REPORT,'failed','outage')
        result=self.s.rows('SELECT * FROM source_checks')[0]
        self.assertEqual(result['status'],'failed');self.assertIsNotNone(result['succeeded_at'])

    def test_legacy_migration_is_read_only_and_unverified(self):
        p=self.root/'legacy.db';c=sqlite3.connect(p)
        c.execute('CREATE TABLE flags(issn,review_id,ref_id,first_seen)');c.execute("INSERT INTO flags VALUES('1234-5678','W1','W2','2020-01-01')");c.commit();c.close()
        before=hashlib.sha256(p.read_bytes()).hexdigest();result=self.s.migrate_legacy(p)
        self.assertEqual(result['legacy_unverified_candidates_imported'],1)
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),before)
        self.assertEqual(self.s.rows('SELECT * FROM cases'),[])

    def test_html_escapes_external_metadata(self):
        with self.s.con:self.s.con.execute('UPDATE reviews SET title=?',('<script>alert(1)</script>',))
        html=html_report(self.s.snapshot())
        self.assertNotIn('<script>',html);self.assertIn('&lt;script&gt;',html)

class EvidenceChecks(unittest.TestCase):
    def test_generic_exclusion_does_not_exclude_target_reference(self):
        proposal={'role':'excluded','evidence_quote':'125 studies were excluded.','reason':'They were ineligible.'}
        guarded,status=guard_exclusion(proposal,'jats:CR69','{"doi":"10.1000/report"}')
        self.assertEqual(guarded['role'],'uncertain');self.assertEqual(status,'unsupported_target_exclusion')

    def test_guideline_citation_not_mislabelled_as_explicit_exclusion(self):
        proposal={'role':'excluded','evidence_quote':'We followed PRISMA guidelines [26].','reason':'No contribution.'}
        guarded,status=guard_exclusion(proposal,'jats:CR26','{"doi":"10.1136/bmj.n71"}')
        self.assertEqual(guarded['role'],'uncertain')

    def test_target_specific_exclusion_retained_as_unverified_proposal(self):
        proposal={'role':'excluded','evidence_quote':'Reports [69] were excluded due to missing data.','reason':'Explicit exclusion.'}
        guarded,status=guard_exclusion(proposal,'jats:CR69','{}')
        self.assertEqual(guarded['role'],'excluded');self.assertIsNone(status)

    def test_rejects_fabricated_ai_quote(self):
        with self.assertRaises(ValueError):validate_proposal({'role':'included_quantitative','evidence_quote':'invented','reason':'yes'},'actual source')

    def test_rejects_ai_role_without_evidence(self):
        with self.assertRaises(ValueError):validate_proposal({'role':'included_quantitative','evidence_quote':'','reason':'yes'},'actual source')

    def test_uncertain_proposal_can_abstain(self):
        self.assertEqual(validate_proposal({'role':'uncertain','evidence_quote':'','reason':'Missing table'},'source')['role'],'uncertain')

    def test_csv_formula_prefixes(self):
        for value in ['=1+1',' +SUM(1,2)','@formula','-1+1']:
            self.assertTrue(csv_safe(value).startswith("'"))

if __name__=='__main__':unittest.main()
