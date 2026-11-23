"""Database helpers. Everything is stored in applyme.db (a single SQLite file)."""
import json
import re
import sqlite3
from datetime import datetime

STATUSES = ("new", "saved", "ready_to_review", "applied", "interview", "rejected", "offer", "skipped")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm TEXT NOT NULL UNIQUE,          -- lowercase name used for matching
    ats TEXT,                           -- greenhouse / lever / ashby / none
    slug TEXT,
    in_companies_txt INTEGER DEFAULT 0,
    limits_simultaneous_apps INTEGER DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,               -- simplify, greenhouse:stripe, lever:palantir, ...
    source_ref TEXT,                    -- the source's own job id
    url TEXT NOT NULL UNIQUE,           -- one row per role
    dedupe_key TEXT,                    -- company + title + locations, to catch the same role from two sources
    company_id INTEGER REFERENCES companies(id),
    company_name TEXT,
    title TEXT,
    locations_json TEXT,                -- raw location strings from the source
    terms_json TEXT,                    -- raw term strings (Simplify)
    description TEXT,                   -- plain text (company boards only)
    sponsorship TEXT,
    degrees_json TEXT,
    category TEXT,
    date_posted TEXT,
    first_seen TEXT,
    last_seen TEXT,
    active INTEGER DEFAULT 1,
    -- Everything below is recomputed from the fields above on every run (see filters.classify),
    -- so changing config.yaml never needs a re-download.
    location_raw TEXT, city TEXT, state TEXT,
    is_remote INTEGER, is_michigan INTEGER, is_detroit_metro INTEGER, location_unclear INTEGER,
    term TEXT, start_date TEXT, end_date TEXT, length_weeks INTEGER, job_type TEXT,
    role_priority INTEGER,
    role_reason TEXT,                   -- why it was excluded for role/term (NULL = fine)
    location_reason TEXT,               -- e.g. "outside Michigan (CA)" (stored even when kept)
    in_scope INTEGER,
    exclude_reason TEXT,
    flags TEXT                          -- JSON list of flag strings
);
CREATE INDEX IF NOT EXISTS idx_postings_key ON postings(dedupe_key);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL UNIQUE REFERENCES postings(id),
    status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({",".join(repr(s) for s in STATUSES)})),
    applied_date TEXT,
    cover_letter_path TEXT,
    notes TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started TEXT,
    finished TEXT,
    summary TEXT
);

-- Phase 2 ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_calls (          -- every paid Claude call, for the budget
    id INTEGER PRIMARY KEY,
    ts TEXT,
    purpose TEXT,                               -- score / research / letter
    posting_id INTEGER,
    company_id INTEGER,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    web_searches INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    request_id TEXT,
    ok INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS page_cache (         -- public job pages we downloaded
    url TEXT PRIMARY KEY,
    fetched_at TEXT,
    status INTEGER,
    method TEXT,                                -- greenhouse-api, workday-json, jsonld, html, skipped, error
    title TEXT,
    company TEXT,
    location TEXT,
    text TEXT,
    sha TEXT
);

CREATE TABLE IF NOT EXISTS scores (             -- Claude's score per role (re-scored only if the text changes)
    posting_id INTEGER PRIMARY KEY REFERENCES postings(id),
    desc_sha TEXT,
    model TEXT,
    score INTEGER,
    reason TEXT,
    fit_note TEXT,
    flags_json TEXT,
    created TEXT
);

CREATE TABLE IF NOT EXISTS company_research (   -- cover-letter research, once per company
    company_id INTEGER PRIMARY KEY REFERENCES companies(id),
    researched_at TEXT,
    model TEXT,
    summary_json TEXT,
    sources_json TEXT,
    cost_usd REAL
);

CREATE TABLE IF NOT EXISTS resume_cache (
    sha TEXT PRIMARY KEY,
    text TEXT
);

-- Phase 4 ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS answer_cache (       -- earlier form answers (never salary or GPA)
    id INTEGER PRIMARY KEY,
    qkey TEXT UNIQUE,                           -- normalized question + its options
    label TEXT,
    options_json TEXT,
    answer_json TEXT,                           -- a string, or a list for multi-select
    source TEXT,                                -- claude / ali
    is_guess INTEGER DEFAULT 0,
    created TEXT,
    used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fill_runs (          -- every autofill of a role
    id INTEGER PRIMARY KEY,
    posting_id INTEGER,
    ts TEXT,
    site TEXT,
    filled INTEGER,
    guesses_json TEXT,
    left_json TEXT,
    cost_usd REAL
);

CREATE TABLE IF NOT EXISTS why_answers (        -- researched "why this company" answers, one per role + question
    posting_id INTEGER,
    qkey TEXT,
    answer TEXT,
    honest_match INTEGER,
    notes_path TEXT,
    cost_usd REAL,
    created TEXT,
    PRIMARY KEY (posting_id, qkey)
);
"""

# Columns added after Phase 1 (added to existing databases automatically).
NEW_COLUMNS = {"postings": {"rule_score": "INTEGER", "desc_source": "TEXT"},
               "applications": {"letter_approved": "INTEGER DEFAULT 0"}}

COMPANY_SUFFIXES = r"\b(inc|incorporated|llc|ltd|corp|corporation|co|company|group|plc|gmbh|ag|the)\b"


def now():
    return datetime.now().isoformat(timespec="seconds")


def norm_company(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(COMPANY_SUFFIXES, " ", s)
    return " ".join(s.split())


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, cols in NEW_COLUMNS.items():
        have = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.commit()
    return con


def company_id(con, name, **fields):
    """Find or create a company row; returns its id."""
    norm = norm_company(name) or name.lower()
    row = con.execute("SELECT id FROM companies WHERE norm=?", (norm,)).fetchone()
    if row:
        cid = row["id"]
    else:
        cid = con.execute("INSERT INTO companies(name, norm) VALUES (?, ?)", (name, norm)).lastrowid
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE companies SET {sets} WHERE id=?", (*fields.values(), cid))
    return cid


def upsert_posting(con, p, run_time):
    """Add a posting or refresh an existing one. Returns True if it is brand new."""
    url = p["url"]
    row = con.execute("SELECT id, url, source, description FROM postings WHERE url=?", (url,)).fetchone()
    if not row and p.get("dedupe_key"):
        row = con.execute("SELECT id, url, source, description FROM postings WHERE dedupe_key=?",
                          (p["dedupe_key"],)).fetchone()
    if row:
        # Same role seen again. Keep the richer description and remember every source that has it.
        sources = set(row["source"].split(","))
        sources.add(p["source"])
        desc = p.get("description") or row["description"]
        con.execute("UPDATE postings SET last_seen=?, active=1, description=?, source=? WHERE id=?",
                    (run_time, desc, ",".join(sorted(sources)), row["id"]))
        if row["url"] == url:  # same listing: pick up edits the company made (title, places, terms)
            con.execute("UPDATE postings SET title=?, locations_json=?, terms_json=? WHERE id=?",
                        (p["title"], json.dumps(p.get("locations") or []), json.dumps(p.get("terms") or []),
                         row["id"]))
        return False
    cid = company_id(con, p["company_name"])
    cur = con.execute(
        """INSERT INTO postings(source, source_ref, url, dedupe_key, company_id, company_name, title,
               locations_json, terms_json, description, sponsorship, degrees_json, category,
               date_posted, first_seen, last_seen, active)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (p["source"], p.get("source_ref"), url, p.get("dedupe_key"), cid, p["company_name"], p["title"],
         json.dumps(p.get("locations") or []), json.dumps(p.get("terms") or []), p.get("description") or "",
         p.get("sponsorship") or "", json.dumps(p.get("degrees") or []), p.get("category") or "",
         p.get("date_posted") or "", run_time, run_time))
    con.execute("INSERT INTO applications(posting_id, status, updated_at) VALUES (?, 'new', ?)",
                (cur.lastrowid, run_time))
    return True


def mark_missing_inactive(con, ok_sources, run_time):
    """Postings from a source that worked this run but weren't seen are no longer listed."""
    n = 0
    for src in ok_sources:
        n += con.execute(
            "UPDATE postings SET active=0 WHERE active=1 AND last_seen<? AND (',' || source || ',') LIKE ?",
            (run_time, f"%,{src},%")).rowcount
    return n


def set_status(con, posting_id, status, note=None):
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    if not con.execute("SELECT 1 FROM postings WHERE id=?", (posting_id,)).fetchone():
        raise ValueError(f"no posting with id {posting_id}")
    con.execute("INSERT OR IGNORE INTO applications(posting_id) VALUES (?)", (posting_id,))
    applied = now()[:10] if status == "applied" else None
    con.execute("""UPDATE applications SET status=?, updated_at=?,
                   applied_date=COALESCE(?, applied_date), notes=COALESCE(?, notes) WHERE posting_id=?""",
                (status, now(), applied, note, posting_id))
    con.commit()
