# Validation status — 0.3.0a1

This is a research alpha. Working software and valid structured outputs do not establish correct scientific interpretation, complete extraction or complete journal coverage.

## Software checks

The current suite has 87 offline tests, passing locally on Python 3.12 and 3.14. It covers identifier/date handling, both Crossref notice directions, typed events, reinstatement conflicts, transactional notice imports, request limits, source identities, dangerous redirects, atomic reports and CSV escaping, AI output rejection, explicit automatic contribution evidence, citation ranges and label conflicts, background/methods/previous-review negatives, missing DOIs, source version replacement, case reopening and human overrides.

The full synthetic journal test begins with discovery data, retrieves a structured article, automatically registers three contributing report DOIs and creates an editorial case from a synthetic retraction, with zero human inclusion decisions. It also covers failed upstream retrieval, unclassified discovery records, bounded traversal and restartable backfill.

Local dashboard tests cover saved editorial decisions, immutable decision history, read-without-acknowledgement, Host/Origin/token rejection and invalid states. GitHub CI is configured for Linux Python 3.10/3.12/3.14 and macOS 3.12; remote results are pending publication.

## Live public-source pilot, 27 October 2026

A fresh isolated deployment began with only the Scientific Reports ISSN configuration, with no sample seeds and no hand-entered inclusion links. A ranked starting search found 20 candidates; a bounded monitor pass retrieved one review, DOI **10.1038/s41598-017-04393-2**, its public publisher page, one supplement and publisher PDF. The structured Results sentence yielded **17 distinct contributing report DOIs automatically**, from 23 linked bibliography targets. Six targets lacked a resolved DOI. The sentence states 24 studies, so the count discrepancy remains visible. This is an operational demonstration, not an independently adjudicated accuracy estimate.

A separate content-retrieval pilot for **10.1038/s41598-026-36763-0** retrieved publisher HTML, two supplements and PDF. The final-analysis inclusion sentence mapped three report DOIs automatically. Supporting written counts and relative same-article citation anchors required adapter fixes now covered by regression tests.

The full Retraction Watch snapshot was downloaded through its public source. The attempted discovery interval was bounded and incomplete; the fresh deployment retained 19 review candidates for later retrieval. Initial Ollama assistance was unavailable and remained an explicit exception. No model fallback or cloud service was used. A subsequent pass checked all 17 automatically mapped reports in Crossref, retrieved a second review with repository XML and a supplement, and generated three local Ollama proposals after its local-only server was started. Those proposals remain unverified and did not create automatic links. The live snapshot also found a retracted bibliography report in another discovered review, which correctly entered inclusion verification rather than being labelled a contributing-study editorial case. Current real cases must not be fabricated to demonstrate the positive route; positive case behavior is tested with clearly synthetic notices.

On the supplied source articles, automated analysis also found partial DOI maps in the ovarian and ML/suicide reviews. Zinc/Helicobacter retrieval barriers, image/rotated tables, missing identities and inconsistent study/report counts remain unresolved. These examples guided development and are not a held-out benchmark.

## Local model qualification

The previously installed `qwen3:1.7b` model failed two early role-classification smoke tests: PRISMA methods evidence was classified as an exclusion, and a generic screening exclusion sentence was incorrectly applied to an individual report. Earlier unconstrained quotations were also rejected for not matching source text. Restricting quote selection repaired output structure, not interpretation.

An exclusion guard now abstains on unsupported target-specific claims. Models only produce experimental proposals; they cannot create accepted machine links or replace evidence rules. No independent held-out local model evaluation has passed. The optional 4B comparison was not performed; a prior execution approval review could not complete at an account usage limit, not a safety determination. It was not bypassed.

## Before editorial reliance

1. Expand and evaluate adapters for included-study tables, PDFs/OCR, supplements and DOI-less identities.
2. Independently annotate contributing report identities using full text and supplements; distinguish studies, reports, algorithms/outcomes and effect estimates.
3. Hold out whole reviews across eligible designs, publishers and layouts. Predeclare thresholds; measure precision, recall, abstention, unresolved identity rate, discovery recall and end-to-end coverage with denominators and uncertainty.
4. Validate real positive review–contributing-report–notice cases, reinstatement conflicts, source edits, disk failures and backup restoration.
5. Complete at least seven unattended daily runs on the target 8GB M1, measuring peak memory, duration, provider failures and deferred work. Scheduler installation alone is not reliability validation.

Human editorial judgment remains necessary for scientific consequences and exceptions. Routine supported contribution links are created automatically; development annotation is not a normal author submission requirement.
