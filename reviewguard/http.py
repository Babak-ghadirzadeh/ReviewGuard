"""Bounded public HTTP, safe redirects, disk cache and provider failures.

The request/retry adapter extends the original urllib-based Http implementation.
404/403 are recorded failures, never empty successful research results.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import datetime as dt
import email.utils
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

class FetchError(RuntimeError):
    def __init__(self, message, status='failed'):
        self.status=status
        super().__init__(message)

def validate_url(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme not in {'https','http'} or not u.hostname or u.username or u.password:
        raise FetchError('Only public HTTP(S) URLs without embedded credentials are accepted.','unsafe_url')
    if u.port not in {None,80,443}:
        raise FetchError('Nonstandard public download port refused.','unsafe_url')
    try:
        addresses=socket.getaddrinfo(u.hostname,u.port or (443 if u.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FetchError('Host lookup failed: '+str(exc)) from exc
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise FetchError('Local/private download destinations are refused.','unsafe_url')
    return url

class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        redirected=super().redirect_request(req,fp,code,msg,headers,newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).hostname!=urllib.parse.urlsplit(newurl).hostname:
            redirected.remove_header('Authorization')
        return redirected

@dataclass
class Response:
    path: Path
    url: str
    content_type: str
    sha256: str
    cached: bool=False

class Http:
    def __init__(self, cache, email=None, max_requests=250, refresh=False, opener=None, sleeper=time.sleep):
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.email=email or os.getenv('RG_EMAIL','')
        self.max_requests=max_requests;self.requests=0;self.refresh=refresh
        self.opener=opener or urllib.request.build_opener(PublicRedirect())
        self.sleeper=sleeper
        self.openalex_key=os.getenv('OPENALEX_API_KEY','')

    def get(self,url,max_bytes=12_000_000,cache_seconds=86400,headers=None):
        key=hashlib.sha256(url.encode()).hexdigest()
        target=self.cache/(key+'.bin');meta=self.cache/(key+'.json')
        if not self.refresh and target.exists() and meta.exists() and time.time()-meta.stat().st_mtime<cache_seconds:
            info=json.loads(meta.read_text())
            if target.stat().st_size<=max_bytes and hashlib.sha256(target.read_bytes()).hexdigest()==info['sha256']:
                return Response(target,info['url'],info['content_type'],info['sha256'],True)
        validate_url(url)
        request_headers={'User-Agent':'ReviewGuard/0.3'+(f' (mailto:{self.email})' if self.email else ''),'Accept':'application/json,application/xml,text/html,application/pdf,*/*;q=0.5'}
        request_headers.update(headers or {})
        host=urllib.parse.urlsplit(url).hostname
        if host=='api.openalex.org' and self.openalex_key:
            request_headers['Authorization']='Bearer '+self.openalex_key
        for attempt in range(3):
            if self.requests>=self.max_requests:
                raise FetchError('Per-run free-service request cap reached; remaining work deferred.','deferred')
            self.requests+=1
            tmp=None
            try:
                req=urllib.request.Request(url,headers=request_headers)
                with self.opener.open(req,timeout=25) as r:
                    final=r.geturl();validate_url(final)
                    declared=int(r.headers.get('Content-Length') or 0)
                    if declared>max_bytes:
                        raise FetchError('Download exceeds configured size limit.','too_large')
                    sha=hashlib.sha256();size=0
                    fd,tmp=tempfile.mkstemp(prefix='download-',dir=self.cache)
                    with os.fdopen(fd,'wb') as f:
                        while chunk:=r.read(65536):
                            size+=len(chunk)
                            if size>max_bytes:raise FetchError('Download exceeds configured size limit.','too_large')
                            f.write(chunk);sha.update(chunk)
                    if not size or (declared and size!=declared):
                        raise FetchError('Empty/truncated download.','incomplete')
                    content_type=r.headers.get('Content-Type','').split(';')[0].strip().lower()
                    if target.exists():
                        old_sha=hashlib.sha256(target.read_bytes()).hexdigest()
                        archive=self.cache/(old_sha+'.bin')
                        if not archive.exists():target.replace(archive)
                    os.replace(tmp,target);tmp=None
                    info={'url':final,'content_type':content_type,'sha256':sha.hexdigest(),'bytes':size}
                    meta_tmp=meta.with_suffix('.tmp');meta_tmp.write_text(json.dumps(info));os.replace(meta_tmp,meta)
                    self.sleeper(.15)
                    return Response(target,final,content_type,info['sha256'])
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in {429,500,502,503,504} and attempt<2:
                    retry=exc.headers.get('Retry-After','')
                    try: delay=float(retry)
                    except ValueError:
                        try:delay=(email.utils.parsedate_to_datetime(retry)-dt.datetime.now(dt.timezone.utc)).total_seconds()
                        except (TypeError,ValueError):delay=2**attempt
                    if delay>30:raise FetchError('Provider requested a longer pause; retry next run.','deferred') from exc
                    self.sleeper(max(0,delay));continue
                status='access_blocked' if exc.code in {401,403,451} else ('not_found' if exc.code==404 else 'failed')
                raise FetchError(f'HTTP {exc.code}; no access restrictions were bypassed.',status) from exc
            except (urllib.error.URLError,TimeoutError,ConnectionError) as exc:
                if attempt<2:self.sleeper(2**attempt);continue
                raise FetchError('Network request failed after retries: '+str(exc)) from exc
            finally:
                if tmp:
                    Path(tmp).unlink(missing_ok=True)
        raise FetchError('Request failed.')

    def get_json(self,url,params=None,cache_seconds=86400):
        if params:url+=('&' if '?' in url else '?')+urllib.parse.urlencode(params)
        response=self.get(url,cache_seconds=cache_seconds)
        try:return json.loads(response.path.read_bytes()),response
        except (ValueError,UnicodeError) as exc:
            raise FetchError('Provider returned invalid JSON; assessment incomplete.','invalid_response') from exc
