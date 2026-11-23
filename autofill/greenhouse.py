"""Greenhouse: questions come from the public job-board API (labels, options, required) before the page opens."""
import html
import re
from urllib.parse import parse_qs, urlsplit

import requests

from sources import HEADERS, html_to_text

API = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true"
KIND = {"input_text": "text", "textarea": "textarea", "input_file": "file", "input_hidden": None,
        "multi_value_single_select": "combo", "multi_value_multi_select": "multi"}


def resolve(url):
    """Return (slug, job_id) for a Greenhouse posting, including forms embedded on company sites (gh_jid=...)."""
    m = re.search(r"greenhouse\.io/(?!embed)([\w-]+)/jobs/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    qs = parse_qs(urlsplit(url).query)
    if "for" in qs and "token" in qs:
        return qs["for"][0], qs["token"][0]
    jid = (qs.get("gh_jid") or [None])[0]
    if not jid:
        return None
    try:
        page = requests.get(url, headers=HEADERS, timeout=20).text
    except requests.RequestException:
        return None
    m = re.search(r"greenhouse\.io/(?:embed/job_board/js\?for=|embed/job_app\?for=|)([\w-]+)", page)
    for slug in dict.fromkeys(re.findall(r"(?:for=|boards\.greenhouse\.io/|job-boards\.greenhouse\.io/)([\w-]+)", page)):
        if slug in ("embed", "v1"):
            continue
        r = requests.get(API.format(slug=slug, id=jid), headers=HEADERS, timeout=20)
        if r.status_code == 200:
            return slug, jid
    return None


def form_url(url):
    r = resolve(url)
    return f"https://job-boards.greenhouse.io/embed/job_app?for={r[0]}&token={r[1]}" if r else None


def questions(url):
    """All questions on the form, from the public API."""
    slug, jid = resolve(url)
    data = requests.get(API.format(slug=slug, id=jid), headers=HEADERS, timeout=20).json()
    out = []
    blocks = list(data.get("questions") or [])
    for c in data.get("compliance") or []:
        blocks += c.get("questions") or []
    blocks += data.get("demographic_questions", {}).get("questions", []) if isinstance(
        data.get("demographic_questions"), dict) else []
    for b in blocks:
        for f in b.get("fields") or []:
            kind = KIND.get(f.get("type"))
            if not kind or f["name"] in ("resume_text", "cover_letter_text"):
                continue
            label = html.unescape(b.get("label") or f["name"])
            label = {"VeteranStatus": "Veteran Status", "DisabilityStatus": "Disability Status"}.get(label, label)
            opts = [html.unescape(v.get("label", "")) for v in f.get("values") or []]
            if kind == "combo" and sorted(o.lower() for o in opts) in (["no", "yes"],):
                kind = "combo"
            out.append({"key": f["name"], "label": label, "kind": kind, "options": opts,
                        "required": bool(b.get("required")),
                        "description": html_to_text(html.unescape(b.get("description") or ""))[:500]})
    return out
