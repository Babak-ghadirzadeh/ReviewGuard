# RetractionGuard

Scans one journal's systematic reviews and meta-analyses and flags every review that cites a retracted study. Each flag is classed as retracted **after** review publication (reassessment pathway: notify authors, linked notice, re-run the analysis) or **before** it (screening check).

No dependencies beyond Python 3.9+.

## Run
```bash
python retractionguard.py --issn 1234-5678 --email you@org.com --since 2015-01-01
python retractionguard.py --demo        # offline demo, synthetic data
```
Optional: `--rw-csv retraction_watch.csv` adds the Retraction Watch database as a second source. `--review-regex` overrides review detection.

Outputs (folder `rg_output/`): `report.html`, `flagged_reviews.csv`, `summary.json`. State lives in `retractionguard.db`, so repeat runs mark only **NEW** flags. Schedule it weekly with cron or Task Scheduler.

## How it works
OpenAlex (journal articles, references, retraction flag) -> Crossref (retraction date and notice) -> optional Retraction Watch CSV -> SQLite -> reports.

## Limitations (read before selling)
- A reference list is not the list of included studies. A flag means "cites a retracted paper"; a human must confirm inclusion.
- Review detection is heuristic (title, then abstract wording). Some reviews will be missed or misdetected.
- Coverage depends on OpenAlex/Crossref reference data, which is incomplete for some publishers.
- Not tested against the live APIs in the build environment (no internet there); only the offline `--demo` was run. Do a live trial on a journal you know first.
- Check the current terms of OpenAlex, Crossref and Retraction Watch for commercial use, and add your own licence and support terms.

## Product ideas
Email/Slack alerts on NEW flags, a web dashboard, full-text matching against included-studies tables, auto-drafted author notices, multi-journal portfolios.
