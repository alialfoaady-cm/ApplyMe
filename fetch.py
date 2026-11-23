"""Read the public text of one job posting. No logins, no AI, no cost.

Order of preference:
  1. the job site's own public JSON (Greenhouse, Lever, Ashby, SmartRecruiters, Workday)
  2. the schema.org "JobPosting" data most job pages include
  3. the visible text of the page
robots.txt is respected, at most 1 request per second per site, and some sites are never fetched.
Results are cached in the page_cache table.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timedelta
from urllib import robotparser
from urllib.parse import urlsplit

import requests

import db
from sources import HEADERS, html_to_text

# Login walls or terms that forbid automated reading: never fetched, Ali types the details instead.
NEVER_FETCH = ("linkedin.com", "indeed.com", "joinhandshake.com", "handshake.com", "glassdoor.com",
               "ziprecruiter.com", "jobright.ai")
REFETCH_DAYS = 7
MIN_USEFUL_CHARS = 500
TIMEOUT = 20

_last_hit = {}
_robots = {}
_ashby_boards = {}


def never_fetch(url):
    host = urlsplit(url).netloc.lower()
    return any(host == d or host.endswith("." + d) for d in NEVER_FETCH)


def _polite(host):
    wait = 1.0 - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.time()


def _get(url, accept=None):
    _polite(urlsplit(url).netloc)
    headers = dict(HEADERS, Accept=accept) if accept else HEADERS
    return requests.get(url, headers=headers, timeout=TIMEOUT)


def robots_ok(url):
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = _get(base + "/robots.txt")
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(HEADERS["User-Agent"], url)


def _clean(s):
    return html_to_text(s or "")


# ------------------------------------------------------------------ site-specific public JSON
def greenhouse(url):
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url) or \
        re.search(r"[?&]for=([\w-]+).*?[?&]token=(\d+)", url)
    if not m:
        return None
    r = _get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}")
    if r.status_code != 200:
        return {"status": r.status_code, "method": "greenhouse-api"}
    j = r.json()
    return {"status": 200, "method": "greenhouse-api", "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""), "text": _clean(j.get("content"))}


def lever(url):
    m = re.search(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    r = _get(f"https://api.lever.co/v0/postings/{m.group(1)}/{m.group(2)}")
    if r.status_code != 200:
        return {"status": r.status_code, "method": "lever-api"}
    j = r.json()
    text = "\n".join([j.get("descriptionPlain", "")] +
                     [f"{l.get('text', '')}\n{_clean(l.get('content'))}" for l in j.get("lists", [])] +
                     [j.get("additionalPlain", "")])
    return {"status": 200, "method": "lever-api", "title": j.get("text", ""),
            "location": (j.get("categories") or {}).get("location", ""), "text": text}


def ashby(url):
    m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{36})", url)
    if not m:
        return None
    slug, jid = m.group(1), m.group(2)
    if slug not in _ashby_boards:
        r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        _ashby_boards[slug] = r.json().get("jobs", []) if r.status_code == 200 else []
    for j in _ashby_boards[slug]:
        if j.get("id") == jid:
            return {"status": 200, "method": "ashby-api", "title": j.get("title", ""),
                    "location": j.get("location", ""),
                    "text": j.get("descriptionPlain") or _clean(j.get("descriptionHtml"))}
    return {"status": 404, "method": "ashby-api"}


def smartrecruiters(url):
    m = re.search(r"smartrecruiters\.com/([\w-]+)/(\d+)", url)
    if not m:
        return None
    r = _get(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
    if r.status_code != 200:
        return {"status": r.status_code, "method": "smartrecruiters-api"}
    j = r.json()
    sec = (j.get("jobAd") or {}).get("sections") or {}
    text = "\n".join(f"{(sec.get(k) or {}).get('title', '')}\n{_clean((sec.get(k) or {}).get('text'))}"
                     for k in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"))
    loc = j.get("location") or {}
    return {"status": 200, "method": "smartrecruiters-api", "title": j.get("name", ""),
            "location": ", ".join(x for x in (loc.get("city"), loc.get("region")) if x), "text": text}


def workday(url):
    """The JSON a Workday job page loads in your browser (no login)."""
    parts = urlsplit(url)
    host = parts.netloc
    if "myworkdayjobs.com" in host:
        tenant = host.split(".")[0]
        m = re.match(r"/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)/job/(.+)", parts.path)
        if not m:
            return None
        site, rest = m.group(1), m.group(2)
    elif "myworkdaysite.com" in host:
        m = re.match(r"/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([^/]+)/([^/]+)/job/(.+)", parts.path)
        if not m:
            return None
        tenant, site, rest = m.groups()
    else:
        return None
    api = f"https://{host}/wday/cxs/{tenant}/{site}/job/{rest}"
    if not robots_ok(api):
        return {"status": 0, "method": "skipped (robots.txt)"}
    r = _get(api, accept="application/json")
    if r.status_code != 200:
        return {"status": r.status_code, "method": "workday-json"}
    info = r.json().get("jobPostingInfo", {})
    return {"status": 200, "method": "workday-json", "title": info.get("title", ""),
            "location": info.get("location", ""), "text": _clean(info.get("jobDescription"))}


def oracle(url):
    """Oracle Recruiting Cloud: the public JSON its job page loads (no login)."""
    parts = urlsplit(url)
    m = re.search(r"/sites/([^/]+)/(?:job|requisitions/preview)/(\d+)", parts.path)
    if not m:
        return None
    api = (f"https://{parts.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f'?expand=all&onlyData=true&finder=ById;siteNumber={m.group(1)},Id="{m.group(2)}"')
    if not robots_ok(api):
        return {"status": 0, "method": "skipped (robots.txt)"}
    r = _get(api, accept="application/json")
    items = r.json().get("items", []) if r.status_code == 200 else []
    if not items:
        return {"status": r.status_code if r.status_code != 200 else 404, "method": "oracle-json"}
    j = items[0]
    text = "\n".join(_clean(j.get(k)) for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr",
                                                "ExternalQualificationsStr", "CorporateDescriptionStr") if j.get(k))
    return {"status": 200, "method": "oracle-json", "title": j.get("Title", ""),
            "location": j.get("PrimaryLocation", ""), "text": text}


def workable(url):
    m = re.search(r"apply\.workable\.com/([^/]+)/j/([0-9A-Za-z]+)", url)
    if not m:
        return None
    r = _get(f"https://apply.workable.com/api/v2/accounts/{m.group(1)}/jobs/{m.group(2)}", accept="application/json")
    if r.status_code != 200:
        return {"status": r.status_code, "method": "workable-json"}
    j = r.json()
    loc = j.get("location") or {}
    where = ", ".join(x for x in (loc.get("city"), loc.get("region")) if x) + ("; Remote" if j.get("remote") else "")
    text = "\n".join(_clean(j.get(k)) for k in ("description", "requirements", "benefits") if j.get(k))
    return {"status": 200, "method": "workable-json", "title": j.get("title", ""), "location": where, "text": text}


SITE_HANDLERS = [("greenhouse.io", greenhouse), ("lever.co", lever), ("ashbyhq.com", ashby),
                 ("smartrecruiters.com", smartrecruiters), ("myworkdayjobs.com", workday),
                 ("myworkdaysite.com", workday), ("oraclecloud.com", oracle), ("apply.workable.com", workable)]


# ------------------------------------------------------------------ any other public page
def _jsonld_jobposting(html):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            if isinstance(it, dict) and "JobPosting" in str(it.get("@type", "")):
                return it
    return None


def _jsonld_location(jp):
    locs = jp.get("jobLocation") or []
    locs = locs if isinstance(locs, list) else [locs]
    out = []
    for l in locs:
        a = (l or {}).get("address") or {}
        if isinstance(a, dict):
            out.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressRegion")) if x))
    if "TELECOMMUTE" in str(jp.get("jobLocationType", "")).upper():
        out.append("Remote")
    return "; ".join(x for x in out if x)


def generic(url):
    if not robots_ok(url):
        return {"status": 0, "method": "skipped (robots.txt)"}
    r = _get(url)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
        return {"status": r.status_code, "method": "html"}
    html = r.text
    jp = _jsonld_jobposting(html)
    if jp:
        org = jp.get("hiringOrganization") or {}
        return {"status": 200, "method": "jsonld", "title": jp.get("title", ""),
                "company": org.get("name", "") if isinstance(org, dict) else "",
                "location": _jsonld_location(jp), "text": _clean(jp.get("description"))}
    body = re.sub(r"<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return {"status": 200, "method": "html", "title": _clean(title.group(1)) if title else "",
            "text": _clean(body)}


# ------------------------------------------------------------------ main entry
def fetch(con, url, force=False):
    """Return a dict(status, method, title, company, location, text, useful) for a posting URL, cached."""
    row = con.execute("SELECT * FROM page_cache WHERE url=?", (url,)).fetchone()
    fresh = (datetime.now() - timedelta(days=REFETCH_DAYS)).isoformat()
    if row and not force and row["fetched_at"] >= fresh:
        out = dict(row)
    else:
        if never_fetch(url):
            out = {"status": 0, "method": "skipped (site not allowed)"}
        else:
            out = None
            try:
                for domain, handler in SITE_HANDLERS:
                    if domain in urlsplit(url).netloc:
                        out = handler(url)
                        break
                if out is None:
                    out = generic(url)
            except (requests.RequestException, ValueError) as e:
                out = {"status": 0, "method": f"error: {type(e).__name__}"}
        text = (out.get("text") or "").strip()[:30000]
        out = {"url": url, "fetched_at": db.now(), "status": out.get("status", 0), "method": out.get("method", ""),
               "title": out.get("title", ""), "company": out.get("company", ""), "location": out.get("location", ""),
               "text": text, "sha": hashlib.sha256(text.encode()).hexdigest()[:16] if text else ""}
        con.execute("""INSERT OR REPLACE INTO page_cache(url, fetched_at, status, method, title, company, location,
                       text, sha) VALUES (:url,:fetched_at,:status,:method,:title,:company,:location,:text,:sha)""", out)
        con.commit()
    out["useful"] = len(out.get("text") or "") >= MIN_USEFUL_CHARS
    return out
