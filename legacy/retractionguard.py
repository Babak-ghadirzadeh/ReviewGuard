#!/usr/bin/env python3
"""
RetractionGuard - scan a journal's systematic reviews / meta-analyses and flag
every review that cites a retracted study.

Pipeline
  1. Pull all articles of one journal (by ISSN) from OpenAlex.
  2. Detect systematic reviews / meta-analyses (title, then abstract heuristics).
  3. Collect every reference of every review and check them against OpenAlex's
     retraction flag (Retraction Watch derived) and, optionally, a local
     Retraction Watch CSV.
  4. Enrich each hit with the retraction date/type via Crossref.
  5. Compare retraction date with review publication date, so each flag is
     classed as "post-publication retraction" (reassessment pathway) or
     "retracted before review publication" (screening check).
  6. Store results in SQLite so repeat runs report only NEW flags.
  7. Write CSV + JSON + a self-contained HTML report.

Standard library only (Python 3.9+).

Usage
  python retractionguard.py --issn 1234-5678 --email you@org.com --since 2015-01-01
  python retractionguard.py --demo          # offline demo with synthetic data

Important limitation: a *reference list* is not the same as the set of
*included studies*. A flag means "this review cites a retracted paper";
a human must confirm whether it was actually included in the synthesis.
"""
import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"

TITLE_RE = re.compile(
    r"systematic(ally)? review|systematic literature review|meta-?analy|"
    r"pooled analysis|umbrella review|network meta|living review", re.I)
METHOD_RE = re.compile(
    r"\b(we|this study|this review|the present study|our)\b[^.]{0,80}"
    r"\b(systematic review|meta-?analy\w+)", re.I)
ISSN_RE = re.compile(r"^\d{4}-\d{3}[\dXx]$")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Http:
    def __init__(self, email, delay=0.12):
        self.email = email
        self.delay = delay

    def get_json(self, url, params=None):
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
                params, safe=",:|*")
        for attempt in range(5):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": f"RetractionGuard/1.0 (mailto:{self.email})",
                    "Accept": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.load(r)
                time.sleep(self.delay)
                return data
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Request failed after retries: {url}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def short_id(oa_id):
    return (oa_id or "").rsplit("/", 1)[-1]


def clean_doi(doi):
    if not doi:
        return None
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", doi.strip(), flags=re.I).lower()


def parse_date(s):
    """'2024-05-01' / '2024-05' / '2024' -> datetime.date (or None)."""
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", s or "")
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return dt.date(y, mo, d)
    except ValueError:
        return None


def abstract_text(work):
    inv = work.get("abstract_inverted_index")
    if not inv:
        return ""
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def detect_review(work, title_rx=TITLE_RE):
    if title_rx.search(work.get("title") or ""):
        return "title"
    if METHOD_RE.search(abstract_text(work)):
        return "abstract"
    return None


# --------------------------------------------------------------------------- #
# Data collection
# --------------------------------------------------------------------------- #
def fetch_journal_works(http, issn, since, max_works=None):
    flt = (f"primary_location.source.issn:{issn},type:article|review,"
           f"from_publication_date:{since}")
    cursor, works = "*", []
    while cursor:
        data = http.get_json(f"{OPENALEX}/works", {
            "filter": flt, "per-page": 200, "cursor": cursor,
            "select": "id,doi,title,publication_date,type,referenced_works,"
                      "abstract_inverted_index,is_retracted"})
        if not data:
            break
        works.extend(data.get("results", []))
        log(f"  fetched {len(works)} journal articles...")
        cursor = (data.get("meta") or {}).get("next_cursor")
        if max_works and len(works) >= max_works:
            works = works[:max_works]
            break
    return works


def load_rw_csv(path):
    """Retraction Watch database CSV -> {doi: {date, nature, reason}}."""
    out = {}
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            doi = clean_doi(row.get("OriginalPaperDOI"))
            nature = (row.get("RetractionNature") or "").strip()
            if not doi or doi in ("unavailable", "") or nature.lower() == "reinstatement":
                continue
            out[doi] = {"date": parse_date((row.get("RetractionDate") or "").split(" ")[0]),
                        "nature": nature or "Retraction",
                        "reason": (row.get("Reason") or "").strip(),
                        "notice_doi": clean_doi(row.get("RetractionDOI"))}
    return out


def find_retracted_refs(http, ref_ids, rw=None):
    """Return {openalex_short_id: {doi,title,year}} for retracted references."""
    found, ids = {}, sorted(ref_ids)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        flt = "openalex:" + "|".join(batch)
        if not rw:
            flt = "is_retracted:true," + flt
        data = http.get_json(f"{OPENALEX}/works", {
            "filter": flt, "per-page": 50,
            "select": "id,doi,title,is_retracted,publication_date"})
        for w in (data or {}).get("results", []):
            doi = clean_doi(w.get("doi"))
            if w.get("is_retracted") or (rw and doi in rw):
                found[short_id(w["id"])] = {
                    "doi": doi, "title": w.get("title"),
                    "published": w.get("publication_date")}
        if (i // 50) % 20 == 0:
            log(f"  checked {min(i + 50, len(ids))}/{len(ids)} references")
    return found


def crossref_retraction(http, doi):
    """Best-effort retraction info from Crossref 'updated-by'."""
    if not doi:
        return None
    data = http.get_json(f"{CROSSREF}/works/{urllib.parse.quote(doi, safe='/')}")
    if not data:
        return None
    best = None
    for u in (data.get("message") or {}).get("updated-by", []) or []:
        typ = (u.get("type") or u.get("label") or "").lower()
        if typ not in ("retraction", "withdrawal", "removal", "expression_of_concern",
                       "expression of concern"):
            continue
        parts = ((u.get("updated") or {}).get("date-parts") or [[None]])[0]
        d = None
        if parts and parts[0]:
            d = dt.date(parts[0], parts[1] if len(parts) > 1 else 1,
                        parts[2] if len(parts) > 2 else 1)
        cand = {"date": d, "nature": typ.replace("_", " ").title(),
                "notice_doi": clean_doi(u.get("DOI")), "source": u.get("source", "crossref")}
        if best is None or (d and (best["date"] is None or d < best["date"])):
            best = cand
    return best


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def classify(review_date, retraction_date):
    if not retraction_date or not review_date:
        return ("date unknown",
                "Confirm retraction date and whether the study was actually included.")
    if retraction_date > review_date:
        return ("retracted AFTER review publication",
                "Reassessment pathway: notify authors, publish linked notice, authors "
                "re-run the analysis without the study (suggested 4-8 weeks).")
    return ("retracted BEFORE review publication",
            "Screening check: verify whether the study was included; if so, assess "
            "consequences and whether the review needs correction.")


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
def open_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS flags(
        issn TEXT, review_id TEXT, ref_id TEXT, first_seen TEXT,
        PRIMARY KEY(issn, review_id, ref_id));
    """)
    return con


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
FIELDS = ["new", "review_doi", "review_title", "review_date", "retracted_doi",
          "retracted_title", "retraction_date", "retraction_type", "timing",
          "suggested_action", "notice_doi", "source", "review_detected_by"]


def write_reports(out_dir, issn, rows, n_articles, n_reviews):
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "flagged_reviews.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    affected = {r["review_doi"] or r["review_title"] for r in rows}
    summary = {"issn": issn, "run_date": dt.date.today().isoformat(),
               "articles_scanned": n_articles, "reviews_detected": n_reviews,
               "reviews_flagged": len(affected), "flag_rows": len(rows),
               "new_flags": sum(1 for r in rows if r["new"])}
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump({"summary": summary, "flags": rows}, f, indent=2, default=str)

    def e(x):
        return html.escape(str(x if x is not None else ""))

    def link(doi):
        return f'<a href="https://doi.org/{e(doi)}">{e(doi)}</a>' if doi else "-"

    badge = {"retracted AFTER review publication": "#B03A2E",
             "retracted BEFORE review publication": "#D98E04",
             "date unknown": "#5B6770"}
    trs = ""
    for r in sorted(rows, key=lambda x: (not x["new"], x["timing"], x["review_title"] or "")):
        trs += (f"<tr><td>{'<b class=new>NEW</b>' if r['new'] else ''}</td>"
                f"<td><b>{e(r['review_title'])}</b><br>{link(r['review_doi'])}<br>"
                f"<small>{e(r['review_date'])}</small></td>"
                f"<td>{e(r['retracted_title'])}<br>{link(r['retracted_doi'])}<br>"
                f"<small>{e(r['retraction_type'])} {e(r['retraction_date'] or '')}</small></td>"
                f"<td><span class=pill style='background:{badge.get(r['timing'], '#555')}'>"
                f"{e(r['timing'])}</span><br><small>{e(r['suggested_action'])}</small></td></tr>")
    page = f"""<!doctype html><meta charset=utf-8><title>RetractionGuard report</title>
<style>body{{font:14px/1.45 system-ui,sans-serif;margin:2rem;color:#222}}
h1{{color:#1F3A5F}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ddd;padding:8px;vertical-align:top}}th{{background:#1F3A5F;color:#fff;text-align:left}}
.pill{{color:#fff;padding:2px 8px;border-radius:10px;font-size:12px}}
.new{{background:#B03A2E;color:#fff;padding:2px 6px;border-radius:4px}}
.cards div{{display:inline-block;margin:0 1rem 1rem 0;padding:10px 16px;background:#EEF1F5;border-radius:8px}}
.cards b{{font-size:22px;color:#1F3A5F;display:block}}</style>
<h1>RetractionGuard report - ISSN {e(issn)}</h1>
<p>Run date {e(summary['run_date'])}</p>
<div class=cards><div><b>{n_articles}</b>articles scanned</div>
<div><b>{n_reviews}</b>reviews detected</div>
<div><b>{summary['reviews_flagged']}</b>reviews flagged</div>
<div><b>{summary['new_flags']}</b>new since last run</div></div>
<p><i>A flag means the review <u>cites</u> a retracted paper. Confirm whether it was
actually included in the synthesis before taking editorial action.</i></p>
<table><tr><th></th><th>Review</th><th>Retracted study cited</th><th>Timing / suggested action</th></tr>
{trs or '<tr><td colspan=4>No flagged reviews.</td></tr>'}</table>"""
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(page)
    return summary, csv_path


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def run(args, http):
    rw = load_rw_csv(args.rw_csv) if args.rw_csv else None
    title_rx = re.compile(args.review_regex, re.I) if args.review_regex else TITLE_RE

    log(f"[1/5] Fetching articles for ISSN {args.issn} since {args.since}")
    works = fetch_journal_works(http, args.issn, args.since, args.max_works)

    log("[2/5] Detecting systematic reviews / meta-analyses")
    reviews = []
    for w in works:
        how = detect_review(w, title_rx)
        if how and w.get("referenced_works"):
            w["_how"] = how
            reviews.append(w)
    log(f"  {len(reviews)} reviews with reference lists (of {len(works)} articles)")

    log("[3/5] Checking references for retractions")
    all_refs = {short_id(r) for w in reviews for r in w["referenced_works"]}
    retracted = find_retracted_refs(http, all_refs, rw)
    log(f"  {len(retracted)} retracted references found")

    log("[4/5] Enriching retraction details")
    details = {}
    for rid, info in retracted.items():
        d = crossref_retraction(http, info["doi"]) or {}
        if rw and info["doi"] in rw:
            r = rw[info["doi"]]
            d = {"date": d.get("date") or r["date"],
                 "nature": d.get("nature") or r["nature"],
                 "notice_doi": d.get("notice_doi") or r["notice_doi"],
                 "source": d.get("source") or "retraction-watch"}
        details[rid] = d

    log("[5/5] Matching reviews to retracted studies")
    con = open_db(args.db)
    today, rows = dt.date.today().isoformat(), []
    for w in reviews:
        rdate = parse_date(w.get("publication_date"))
        for ref in w["referenced_works"]:
            rid = short_id(ref)
            if rid not in retracted:
                continue
            info, d = retracted[rid], details.get(rid, {})
            timing, action = classify(rdate, d.get("date"))
            cur = con.execute(
                "INSERT OR IGNORE INTO flags VALUES(?,?,?,?)",
                (args.issn, short_id(w["id"]), rid, today))
            rows.append({
                "new": cur.rowcount == 1,
                "review_doi": clean_doi(w.get("doi")), "review_title": w.get("title"),
                "review_date": w.get("publication_date"),
                "retracted_doi": info["doi"], "retracted_title": info["title"],
                "retraction_date": d.get("date"), "retraction_type": d.get("nature", "Retraction"),
                "timing": timing, "suggested_action": action,
                "notice_doi": d.get("notice_doi"), "source": d.get("source", "openalex"),
                "review_detected_by": w["_how"]})
    con.commit()
    con.close()
    summary, path = write_reports(args.out, args.issn, rows, len(works), len(reviews))
    log(f"\nDone. {summary['reviews_flagged']} reviews flagged "
        f"({summary['new_flags']} new). Reports in: {os.path.abspath(args.out)}")
    return summary


# --------------------------------------------------------------------------- #
# Offline demo (synthetic data, no network)
# --------------------------------------------------------------------------- #
class DemoHttp:
    def get_json(self, url, params=None):
        params = params or {}
        flt = params.get("filter", "")
        if "api.crossref.org" in url:
            if "10.1000/bad1" in url:
                return {"message": {"updated-by": [{
                    "type": "retraction", "DOI": "10.1000/bad1.retraction",
                    "source": "retraction-watch", "updated": {"date-parts": [[2025, 3, 10]]}}]}}
            return {"message": {"updated-by": [{
                "type": "retraction", "DOI": "10.1000/bad2.retraction",
                "source": "publisher", "updated": {"date-parts": [[2019, 6, 1]]}}]}}
        if "primary_location.source.issn" in flt:
            return {"meta": {"next_cursor": None}, "results": [
                {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/rev1",
                 "title": "Drug X for hypertension: a systematic review and meta-analysis",
                 "publication_date": "2024-05-01", "type": "article",
                 "referenced_works": ["https://openalex.org/W100", "https://openalex.org/W101"]},
                {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1000/rev2",
                 "title": "Exercise and depression: pooled analysis of trials",
                 "publication_date": "2021-02-01", "type": "article",
                 "referenced_works": ["https://openalex.org/W102", "https://openalex.org/W101"]},
                {"id": "https://openalex.org/W3", "doi": "https://doi.org/10.1000/orig1",
                 "title": "A randomised trial of something", "publication_date": "2023-01-01",
                 "type": "article", "referenced_works": ["https://openalex.org/W100"]},
                {"id": "https://openalex.org/W4", "doi": "https://doi.org/10.1000/rev3",
                 "title": "Sleep outcomes", "publication_date": "2022-01-01", "type": "article",
                 "abstract_inverted_index": {"We": [0], "conducted": [1], "a": [2],
                                            "systematic": [3], "review": [4]},
                 "referenced_works": ["https://openalex.org/W103"]}]}
        if "openalex:" in flt:
            return {"results": [
                {"id": "https://openalex.org/W100", "doi": "https://doi.org/10.1000/bad1",
                 "title": "Trial later retracted for data fabrication", "is_retracted": True,
                 "publication_date": "2022-01-01"},
                {"id": "https://openalex.org/W102", "doi": "https://doi.org/10.1000/bad2",
                 "title": "Old trial retracted in 2019", "is_retracted": True,
                 "publication_date": "2017-01-01"}]}
        return None


def main():
    p = argparse.ArgumentParser(description="Flag systematic reviews citing retracted studies.")
    p.add_argument("--issn", help="Journal ISSN, e.g. 0000-0000")
    p.add_argument("--email", default=os.environ.get("RG_EMAIL"),
                   help="Contact email for polite API pools (or env RG_EMAIL)")
    p.add_argument("--since", default="2015-01-01", help="Earliest publication date (YYYY-MM-DD)")
    p.add_argument("--out", default="rg_output", help="Output directory")
    p.add_argument("--db", default="retractionguard.db", help="SQLite state file")
    p.add_argument("--rw-csv", help="Optional local Retraction Watch CSV for extra coverage")
    p.add_argument("--review-regex", help="Override the title regex used to detect reviews")
    p.add_argument("--max-works", type=int, help="Cap articles scanned (testing)")
    p.add_argument("--demo", action="store_true", help="Run offline on synthetic data")
    args = p.parse_args()

    if args.demo:
        args.issn = args.issn or "0000-0000"
        args.db = ":memory:" if args.db == "retractionguard.db" else args.db
        run(args, DemoHttp())
        return
    if not args.issn or not ISSN_RE.match(args.issn):
        p.error("a valid --issn (e.g. 1234-5678) is required")
    if not args.email:
        p.error("--email (or env RG_EMAIL) is required")
    run(args, Http(args.email))


if __name__ == "__main__":
    main()
