"""Typed identifiers, dates, notices and review eligibility.

Adapted from the supplied RetractionGuard helpers; dates retain precision and
notices remain separate events rather than overwriting a DOI dictionary.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import calendar
import datetime as dt
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit, unquote

ROLES = {'included_quantitative', 'included_qualitative', 'excluded', 'background', 'methods', 'uncertain'}
INCLUDED = {'included_quantitative', 'included_qualitative'}
NOTICE_TYPES = {'retraction', 'correction', 'expression_of_concern', 'reinstatement', 'withdrawal', 'removal'}

def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')

def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()

def clean_doi(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().strip('<>')
    if value.lower() in {'', 'unavailable', 'none', 'n/a', 'na', '0', '-'}:
        return None
    if re.match(r'https?://', value, re.I):
        u = urlsplit(value)
        if (u.hostname or '').lower() not in {'doi.org', 'dx.doi.org', 'www.doi.org'}:
            return None
        value = unquote(u.path.lstrip('/'))
    value = re.sub(r'^doi\s*:\s*', '', value, flags=re.I).strip()
    if not re.fullmatch(r'10\.\d{4,9}/[^\s]+', value, re.I):
        return None
    return value.lower()

def doi_list(value) -> list[str]:
    return list(dict.fromkeys(d for s in re.split(r';\s*(?=10\.)', value or '') if (d := clean_doi(s))))

@dataclass(frozen=True)
class DateSpan:
    raw: str
    start: str | None
    end: str | None
    precision: str

def parse_date(value) -> DateSpan:
    raw = str(value or '').strip()
    if isinstance(value, list):
        value = '-'.join(str(x) for x in value)
    s = str(value or '').strip()
    try:
        slash = re.fullmatch(r'(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?', s)
        if slash:
            m, d, y = map(int, slash.groups())
            date = dt.date(y, m, d).isoformat()
            return DateSpan(raw, date, date, 'day')
        match = re.fullmatch(r'(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?(?:[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?', s)
        if match:
            y, m, d = (int(x) if x else None for x in match.groups())
            if d is not None:
                date = dt.date(y, m, d).isoformat()
                return DateSpan(raw, date, date, 'day')
            if m is not None:
                return DateSpan(raw, dt.date(y,m,1).isoformat(), dt.date(y,m,calendar.monthrange(y,m)[1]).isoformat(), 'month')
            return DateSpan(raw, dt.date(y,1,1).isoformat(), dt.date(y,12,31).isoformat(), 'year')
    except (ValueError, TypeError):
        pass
    return DateSpan(raw, None, None, 'unknown' if not s else 'invalid')

def timing(review: DateSpan, notice: DateSpan) -> str:
    if not review.start or not notice.start:
        return 'date_unknown'
    if notice.start > review.end:
        return 'after_review_publication'
    if notice.end < review.start:
        return 'before_review_publication'
    return 'ordering_indeterminate'

def normalize_nature(value) -> str:
    value = re.sub(r'[\s_-]+', '_', str(value or '').strip().lower())
    return {'erratum':'correction', 'corrigendum':'correction', 'expression_of_concern':'expression_of_concern'}.get(value, value or 'unknown')

@dataclass(frozen=True)
class Notice:
    source: str
    source_key: str
    original_doi: str
    nature: str
    date: DateSpan
    notice_doi: str | None
    reason: str
    raw: dict

    @property
    def content_hash(self):
        return fingerprint(asdict(self))

def rw_notices(row) -> list[Notice]:
    nature = normalize_nature(row.get('RetractionNature'))
    record = str(row.get('Record ID') or '')
    notice_doi = clean_doi(row.get('RetractionDOI'))
    date = parse_date(row.get('RetractionDate'))
    return [Notice('retraction-watch', record or fingerprint([doi, notice_doi, nature, date.raw]), doi,
                   nature, date, notice_doi, row.get('Reason') or '', dict(row))
            for doi in doi_list(row.get('OriginalPaperDOI'))]

def crossref_notices(message) -> list[Notice]:
    this_doi = clean_doi(message.get('DOI'))
    events = []
    for direction in ('updated-by', 'update-to'):
        for u in message.get(direction) or []:
            other = clean_doi(u.get('DOI'))
            original, notice = (this_doi, other) if direction == 'updated-by' else (other, this_doi)
            if not original:
                continue
            nature = normalize_nature(u.get('type') or u.get('label'))
            date = parse_date((((u.get('updated') or {}).get('date-parts')) or [[]])[0])
            source = 'crossref:' + str(u.get('source') or 'publisher')
            key = str(u.get('record-id') or fingerprint([original, notice, nature]))
            events.append(Notice(source, key, original, nature, date, notice, '', dict(u)))
    return events

def current_notice_state(events) -> dict:
    """Conservative summary; reinstatement can only supersede earlier events
    from the same provider family. Conflicting/unknown dates require review.
    """
    events = list(events)
    groups = {}
    for e in events:
        source = e['source']
        family = 'retraction-watch' if source in {'retraction-watch', 'crossref:retraction-watch'} else source
        groups.setdefault(family, []).append(e)
    active, concerns, reinstated, uncertain = [], [], [], []
    for values in groups.values():
        retractions = [e for e in values if e['nature'] == 'retraction']
        reinstatements = [e for e in values if e['nature'] == 'reinstatement']
        for e in retractions:
            later = [r for r in reinstatements if r.get('date_start') and e.get('date_end') and r['date_start'] > e['date_end']]
            if later:
                reinstated.append(e)
            else:
                active.append(e)
                if reinstatements:
                    uncertain.append(e)
        concerns.extend(e for e in values if e['nature'] == 'expression_of_concern')
    any_reinstatement=any(e['nature']=='reinstatement' for e in events)
    status = 'retraction' if active else ('reinstated' if reinstated or any_reinstatement else ('expression_of_concern' if concerns else 'other_notice' if events else 'no_matching_notice'))
    if active and (any_reinstatement or uncertain):
        status = 'conflicting_notice_history'
    return {'status': status, 'events': events, 'active_retractions': active, 'requires_history_review': bool(uncertain) or bool(active and any_reinstatement)}

# Reuse the original abstract reconstruction and broad candidate detection idea,
# while keeping protocols, narrative and umbrella reviews outside this scope.
def abstract_text(work):
    inv = work.get('abstract_inverted_index') or {}
    positions = {i: word for word, idxs in inv.items() for i in idxs}
    return ' '.join(positions[i] for i in sorted(positions))

def eligibility(title, abstract='') -> dict:
    text = unicodedata.normalize('NFKC', title or '').lower()
    text = re.sub(r'[\u2010-\u2015\u2212]', '-', text)
    if re.search(r'\b(protocol|umbrella review|narrative review|scoping review)\b', text):
        return {'status':'out_of_scope', 'evidence':'title', 'reason':'Protocol or review type outside the confirmed initial scope.'}
    if re.search(r'systematic(?: literature)? review|meta[ -]?analys', text):
        return {'status':'candidate', 'evidence':'title', 'reason':'Requires full-text eligibility and included-study assessment.'}
    if re.search(r'\b(we|this review|this study)\b[^.]{0,100}(systematic review|meta[ -]?analys)', abstract or '', re.I):
        return {'status':'candidate', 'evidence':'abstract', 'reason':'Requires full-text confirmation.'}
    return {'status':'unclassified', 'evidence':'metadata', 'reason':'No sufficient eligibility evidence; not proof of exclusion.'}
