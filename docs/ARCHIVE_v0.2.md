# ReviewGuard 0.2

A local, free/open-source development prototype for monitoring retractions affecting systematic reviews and meta-analyses. **This milestone repairs the deterministic core and retrieves source evidence; it does not yet provide validated automatic included-study identification.**

Scientific Reports (ISSN 2045-2322) is the additional effectiveness pilot confirmed by the user. Scientific Reviews (ISSN 3120-4953) remains a discovery/eligibility compatibility pilot: its scope excludes systematic reviews and meta-analyses.

## What works

- DOI-based metadata retrieval for the six supplied papers, preserving the user's Scholar links as provenance.
- Observed public publisher supplement downloads and Europe PMC open-access full-text XML retrieval, with exact DOI checks.
- Versioned local sources, recorded licences/access attempts, disk caching and explicit missing/blocked states.
- Local XML/HTML/DOCX extraction; optional PDF extraction with image/rotation warnings.
- Streaming Retraction Watch import, real slash-form dates, notice types, event versions, reinstatements and source disagreements.
- Durable unread editorial cases, source health, decision history and atomic HTML/JSON/CSV reports.
- Bounded journal discovery, a free OpenAlex key option, request limits and oldest/never-checked report prioritization.
- A read-only dashboard served only on this Mac.
- Experimental local Ollama inclusion proposals with schema validation, exact source passages, model digest and an abstention guard. Proposals do not verify inclusion or send author notifications.

The original supplied script is preserved unchanged in `legacy/`; the new core extends its urllib, SQLite, identifier/date, batching and local-report approach. Existing legacy database flags can be imported as **unverified citation candidates**, never as confirmed inclusion evidence.

## Start from the source directory

Python 3.10+ is required. The core, dashboard and DOCX/XML extraction use the standard library. No paid API, hosted model, Docker or external database is required.

```bash
python3 -m reviewguard init
python3 -m reviewguard retrieve
python3 -m reviewguard extract
python3 -m reviewguard serve
```

Open [the local dashboard](http://127.0.0.1:8765). On this Mac, `Launch ReviewGuard.command` starts the viewer. This is a read-only viewer; case and inclusion decisions use the commands below. A local-only configuration created during development can point to the already populated workspace data directory. It is excluded from the portable release archive.

All commands accept `--data-dir` **before** the command:

```bash
python3 -m reviewguard --data-dir /absolute/path/to/local-data status
python3 -m reviewguard --data-dir /absolute/path/to/local-data report
```

Without a local override, the source checkout uses `data/`. An installed package uses `reviewguard-data/` in the current directory. `RG_DATA_DIR` overrides both. Data, caches, article files and editorial decisions are excluded from Git and the source archive.

To install into a project virtual environment instead of running directly from source:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m reviewguard init
```

Optional PDF extraction:

```bash
.venv/bin/python -m pip install '.[pdf]'
```

PDF source files are retained if that optional library is unavailable. Missing extraction capability is reported rather than interpreted as an empty review.

## Actual first-run retrieval

The first six-article run retrieved all six Crossref metadata records, four public supplementary DOCX files (ovarian, endometrial, machine-learning/suicide, and tocilizumab), and two Europe PMC full-text XML articles (ovarian and machine-learning/suicide). Zinc and Helicobacter publisher requests were blocked; their separate supplements remain unresolved. Existing user-uploaded PDFs were imported locally for all six reviews.

The full Retraction Watch snapshot processed 67,404 DOI-addressable notice records and 5,952 rows without usable DOIs. No date parse was invalid in that snapshot. These are importer observations, not a count of affected sample studies or independent retracted articles.

Structured XML exposed the ovarian review's image-based tables as missing structured rows, matching the earlier PDF audit. The PDF extractor flags the rotated endometrial table. The recovered supplements contain usable text/table rows, but rows can repeat reports, outcomes or algorithms. They are not automatically independent studies.

A focused, bounded OpenAlex query found five Scientific Reports review candidates. They are discovery candidates with sources not yet retrieved, not a validated journal backfile. The first broader 50-record query found no candidates; neither bounded query estimates complete recall.

## Retrieve missing evidence and check notices

The six Scholar URLs are preserved in the article seed configuration. Scholar pages were inaccessible through the retrieval tool. ReviewGuard therefore uses DOIs confirmed from the supplied PDFs and metadata services. It does not scrape Scholar, guess supplement filenames, solve access challenges or use paid locator services.

```bash
python3 -m reviewguard import-file --doi 10.1080/14767058.2025.2528696 --path /absolute/path/to/supplement.docx --kind supplement
python3 -m reviewguard extract
python3 -m reviewguard refresh-notices
python3 -m reviewguard check
```

`refresh-notices` imports a complete snapshot transactionally and matches all known report DOIs. `check` additionally queries Crossref notice relations for DOI-addressable bibliography reports; their inclusion roles remain separate. A source failure or missing identifier does not prove no retraction.

```bash
python3 -m reviewguard --max-requests 100 discover-journal --issn 2045-2322 --since 2026-01-01 --query 'systematic review' --max-works 20 --max-pages 1
```

For full cursor traversal, obtain a **free, unfunded OpenAlex account** and supply `OPENALEX_API_KEY` locally. `RG_EMAIL` optionally supplies a contact address in the request header. Do not put either in source control. Do not add prepaid credits: this project has no paid fallback. A per-run HTTP request cap, cache and explicit pagination status limit work; API account allowances are shared with other uses and must still be monitored. When the cap is reached, later report checks retain priority for the next run rather than being starved indefinitely.

## Local AI assessment

Run Ollama locally with cloud features disabled and one request at a time:

```bash
OLLAMA_NO_CLOUD=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_CONTEXT_LENGTH=4096 ollama serve
```

The free `qwen3:1.7b` model was installed and tested on this Mac. In the initial two-chunk role smoke check, it produced unsupported exclusion labels despite selecting real source passages. Those role assignments did **not** meet the quality bar. The source evidence, original model proposals, model digest and failed-quality annotation are retained locally. The code now abstains on unsupported target-specific exclusions. The revised prompt/guard is covered by offline tests; it has not been qualified for accuracy on a held-out dataset.

An optional comparison using `qwen3:4b` was not downloaded: the execution approval system could not complete review because of an account usage limit. No paid alternative was used. The original 4B candidate remains untested here.

Example after selecting a real bibliography key, source hash and evidence section from the extraction:

```bash
python3 -m reviewguard assess --review REVIEW_DOI --ref-key REFERENCE_KEY --document-sha SOURCE_SHA256 --locator 'Table S1, row 2' --evidence-file /absolute/path/to/source-excerpt.txt --model qwen3:1.7b
```

The excerpt must occur in the extraction of the registered source version. The allowed quote values are exact source passages; quotes, fields and roles are validated. Only explicitly allowed local model tags can run. The call is limited to a 4,096-token context and bounded output. Valid JSON and a real quotation do not establish a correct role. Model output remains a proposal requiring human review; the code does not automatically infer scientific impact or trigger author contact.

## Record human decisions

Inspect `status.json`, extraction files and source evidence before recording a decision. Roles are `included_quantitative`, `included_qualitative`, `excluded`, `background`, `methods`, or `uncertain`.

```bash
python3 -m reviewguard decide-reference --review REVIEW_DOI --ref-key REFERENCE_KEY --role included_quantitative --document-sha SOURCE_SHA256 --evidence 'Exact supporting source text' --locator 'Table 1, row 3' --actor 'reviewer name'
python3 -m reviewguard decide-case 1 --state under_review --note 'Reviewing inclusion evidence and publisher notice' --actor 'editor name'
```

Included roles require a confirmed report DOI and a registered evidence document. A human attests to the source/identity relationship; the command does not replace that judgement. A new notice, changed evidence or reinstatement can reopen a case. Rendering or exporting does not acknowledge unread cases. The only automatic editorial recommendation is assessment.

## Daily operation

```bash
python3 -m reviewguard daily
```

This refreshes notice data, checks tracked reports, reconciles cases and writes reports. Successful runs less than 20 hours old are skipped unless `--force` is supplied. An incomplete run retains failure details and returns a nonzero status. Work is serialized by a local writer lock. It does not repeatedly re-extract unchanged PDFs or run the model over every article every day.

`docs/launchd.example.plist` is a scheduling template, **not an activated job**. Configure the actual Python path, working/data directories and logging before installation. The 09:00 example follows the Mac's local timezone; use Asia/Tehran for this user. `RunAtLoad` allows overdue work at login. A powered-off Mac cannot monitor; the application records last attempt and last success separately. Validate the complete workflow before enabling a permanent schedule.

## Validation and remaining work

```bash
python3 -m unittest discover -s tests -v
python3 -m reviewguard demo
```

The demo uses an isolated `synthetic-demo/` directory. It never inserts demonstration notices into the actual review database. See `docs/VALIDATION.md` for verified behavior and unresolved release gates.

Remaining work includes reliable included-study extraction, local OCR/orientation recovery, identity reconciliation for conflicting citations, no-DOI/PMID support, robust report-to-study grouping and held-out model validation. Full-journal discovery requires more than the small initial queries. The dashboard is currently read-only and does not provide an ingestion or decision form. Whole-system peak memory and daily reliability have not yet been validated. There is no production-ready accuracy claim.

## Source and licence

New project code is MIT-licensed; optional `pdfplumber` is MIT and Qwen3 weights use Apache-2.0. Article licences remain separate and are recorded with each source. The portable archive contains code, seed metadata, synthetic tests and instructions; it excludes the runtime database, fetched articles/supplements, credentials and model files. Confirm ownership/provenance of the supplied legacy code before public GitHub release. No repository has been created or pushed.

Primary implementation references: [Crossref notices](https://www.crossref.org/documentation/retrieve-metadata/retraction-watch/), [Crossref licence information](https://www.crossref.org/documentation/retrieve-metadata/), [Europe PMC API](https://europepmc.org/RestfulWebService), [OpenAlex authentication](https://help.openalex.org/api/authentication/), [Ollama structured output](https://docs.ollama.com/capabilities/structured-outputs), [Ollama local-only mode](https://docs.ollama.com/faq), [Apple scheduling](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html).
