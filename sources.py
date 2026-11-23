"""Download postings from free public sources. Nothing here needs a login or an API key."""
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from db import norm_company

HEADERS = {"User-Agent": "ApplyMe/1.0 (personal internship search script)"}
TIMEOUT = 30

# The Pitt CSC list moved into SimplifyJobs, so this one feed covers both. It includes off-season terms and co-ops.
SIMPLIFY_URLS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
]


def get_json(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def clean_url(url):
    """Remove tracking bits (utm_*, ref=Simplify) but keep real ids like gh_jid."""
    parts = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in ("ref", "source", "src", "trk", "trackingid", "refid")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), urlencode(q), ""))


def html_to_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n", s)).strip()


# Different names the sources use for the same employer (used only to spot the same role twice).
KEY_ALIASES = {"robert bosch venture capital": "bosch", "robert bosch": "bosch", "bosch group": "bosch"}


def make_key(company, title, locations):
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    locs = ",".join(sorted(re.sub(r"[^a-z]+", "", l.lower())[:20] for l in locations or []))
    c = norm_company(company)
    return f"{KEY_ALIASES.get(c, c)}|{t}|{locs}"


def ts_to_date(ts):
    try:
        ts = float(ts)
        if ts > 1e11:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        return ""


# ------------------------------------------------------------------ SimplifyJobs
def fetch_simplify():
    last_err = None
    for url in SIMPLIFY_URLS:
        try:
            data = get_json(url)
            break
        except Exception as e:  # try the older repo
            last_err = e
    else:
        raise RuntimeError(f"SimplifyJobs list unavailable: {last_err}")
    out = []
    for x in data:
        if not x.get("active") or not x.get("is_visible", True) or not x.get("url"):
            continue
        out.append({
            "source": "simplify", "source_ref": x.get("id"), "url": clean_url(x["url"]),
            "company_name": x.get("company_name", "").strip(), "title": x.get("title", "").strip(),
            "locations": x.get("locations") or [], "terms": x.get("terms") or [],
            "sponsorship": x.get("sponsorship", ""), "degrees": x.get("degrees") or [],
            "category": x.get("category", ""), "date_posted": ts_to_date(x.get("date_posted")),
            "description": "",
        })
    return out


# ------------------------------------------------------------------ company job boards
def greenhouse_jobs(company, slug):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        main = j.get("location", {}).get("name", "")
        locs = [main]
        if "," not in main and "remote" not in main.lower():  # vague, e.g. "In-Office": use the office list
            locs = [o.get("name", "") for o in j.get("offices", [])] + [main]
        locs = [l for l in dict.fromkeys(locs) if l]
        out.append({"source": f"greenhouse:{slug}", "source_ref": str(j.get("id")), "url": clean_url(j["absolute_url"]),
                    "company_name": company, "title": j.get("title", ""), "locations": locs,
                    "description": html_to_text(j.get("content", "")),
                    "date_posted": (j.get("first_published") or j.get("updated_at") or "")[:10]})
    return out


def lever_jobs(company, slug):
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in data:
        cat = j.get("categories", {}) or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        if j.get("workplaceType") == "remote":
            locs = locs + ["Remote"]
        desc = "\n".join([j.get("descriptionPlain", "")] +
                         [f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists", [])] +
                         [j.get("additionalPlain", "")])
        out.append({"source": f"lever:{slug}", "source_ref": j.get("id"), "url": clean_url(j["hostedUrl"]),
                    "company_name": company, "title": j.get("text", ""), "locations": [l for l in locs if l],
                    "description": desc, "date_posted": ts_to_date(j.get("createdAt")),
                    "job_type_hint": cat.get("commitment", "")})
    return out


def ashby_jobs(company, slug):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        if j.get("isRemote") or j.get("workplaceType") == "Remote":
            locs.append("Remote")
        out.append({"source": f"ashby:{slug}", "source_ref": j.get("id"), "url": clean_url(j.get("jobUrl") or j["applyUrl"]),
                    "company_name": company, "title": j.get("title", ""), "locations": [l for l in locs if l],
                    "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                    "date_posted": (j.get("publishedAt") or "")[:10]})
    return out


SR_API = "https://api.smartrecruiters.com/v1/companies"
SR_INTERN_RE = re.compile(r"\b(intern|interns|internship|co-?op|coop|apprentice(ship)?|working student)\b", re.I)


def smartrecruiters_jobs(company, slug):
    """SmartRecruiters' official public Posting API. US postings only; full text fetched for intern/co-op titles."""
    out, offset = [], 0
    while True:
        data = get_json(f"{SR_API}/{slug}/postings?country=us&limit=100&offset={offset}")
        page = data.get("content", [])
        for j in page:
            loc = j.get("location") or {}
            locs = [", ".join(x for x in (loc.get("city"), (loc.get("region") or "").upper()) if x)]
            if loc.get("remote"):
                locs.append("Remote")
            desc = ""
            if SR_INTERN_RE.search(j.get("name", "")):  # only download full text for internships/co-ops
                try:
                    d = get_json(f"{SR_API}/{slug}/postings/{j['id']}")
                    sec = (d.get("jobAd") or {}).get("sections") or {}
                    desc = "\n".join(html_to_text((sec.get(k) or {}).get("text", ""))
                                     for k in ("jobDescription", "qualifications", "additionalInformation"))
                except requests.RequestException:
                    pass
            out.append({"source": f"smartrecruiters:{slug}", "source_ref": j["id"],
                        # same URL form the SimplifyJobs list uses, so the same role isn't stored twice
                        "url": f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                        "company_name": company, "title": j.get("name", ""), "locations": [l for l in locs if l],
                        "description": desc, "date_posted": (j.get("releasedDate") or "")[:10]})
        offset += len(page)
        if not page or offset >= data.get("totalFound", 0):
            return out


FETCHERS = {"greenhouse": greenhouse_jobs, "lever": lever_jobs, "ashby": ashby_jobs,
            "smartrecruiters": smartrecruiters_jobs}


def slug_guesses(name):
    words = re.sub(r"[^a-z0-9 ]+", "", name.lower().replace("&", "and")).split()
    core = [w for w in words if w not in ("motor", "motors", "company", "companies", "inc", "corporation",
                                          "industries", "technologies", "financial", "the")] or words
    guesses = ["".join(words), "".join(core), "-".join(words), "-".join(core), core[0], words[0]]
    return list(dict.fromkeys(g for g in guesses if len(g) > 1))


def probe_board(ats, slug, company):
    """True if this ATS board exists and (for Greenhouse) really belongs to this company."""
    try:
        if ats == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}", headers=HEADERS, timeout=15)
            if r.status_code != 200:
                return False
            if company is None:  # hand-checked board, no name check needed
                return True
            board = set(norm_company(r.json().get("name", "")).split())
            target = set(norm_company(company).split())
            # e.g. "General Motors" must not match a board called "General Interest"
            return bool(board) and (board <= target or target <= board)
        if ats == "lever":
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1", headers=HEADERS, timeout=15)
            return r.status_code == 200 and isinstance(r.json(), list)
        if ats == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=HEADERS, timeout=15)
            return r.status_code == 200 and "jobs" in r.json()
        if ats == "smartrecruiters":
            r = requests.get(f"{SR_API}/{slug}/postings?limit=1", headers=HEADERS, timeout=15)
            return r.status_code == 200 and r.json().get("totalFound", 0) > 0
    except (requests.RequestException, ValueError):
        return False
    return False


# Boards whose name can't be guessed from the company name (checked by hand).
KNOWN_BOARDS = {
    "anduril": ("greenhouse", "andurilindustries"),
    "toyota research institute": ("lever", "tri"),
    "bosch": ("smartrecruiters", "BoschGroup"),
}


def find_board(company):
    """Try likely slugs on each ATS. Returns (ats, slug) or (None, None)."""
    known = KNOWN_BOARDS.get(norm_company(company))
    if known and probe_board(known[0], known[1], None):
        return known
    for ats in ("greenhouse", "lever", "ashby"):
        for slug in slug_guesses(company):
            if probe_board(ats, slug, company):
                return ats, slug
    return None, None


def find_boards(companies, workers=8):
    with ThreadPoolExecutor(workers) as ex:
        return dict(zip(companies, ex.map(find_board, companies)))


def fetch_board(company, ats, slug):
    return FETCHERS[ats](company, slug)


def fetch_boards(boards, workers=8):
    """boards: list of (company, ats, slug). Returns {(company): (postings or Exception)}."""
    def one(b):
        try:
            return b[0], fetch_board(*b)
        except Exception as e:
            return b[0], e
    with ThreadPoolExecutor(workers) as ex:
        return dict(ex.map(one, boards))
