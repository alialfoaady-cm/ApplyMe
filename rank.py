"""Phase 2 ranking: free rules -> public job page text -> one small Claude call per top role.

Everything is cached: pages in page_cache, scores in scores (a role is re-scored only if its text changes),
and every paid call is logged in api_calls with its cost.
"""
import hashlib
import json
import re

import yaml

import budget
import db
import fetch
import filters

SCORE_MAX_TOKENS = 900
ACTIVE_STATUSES = ("new", "saved", "ready_to_review")

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100 fit for this student"},
        "reason": {"type": "string", "description": "exactly 2 sentences"},
        "fit_note": {"type": "string", "description": "2-3 sentences, first person, only real facts"},
        "gpa_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "citizenship_required": {"type": "boolean"},
        "clearance_required": {"type": "boolean"},
        "housing_or_relocation": {"type": "boolean"},
        "early_career_program": {"type": "boolean"},
        "term_text": {"type": "string", "description": "term/dates as stated, or empty"},
    },
    "required": ["score", "reason", "fit_note", "gpa_min", "citizenship_required", "clearance_required",
                 "housing_or_relocation", "early_career_program", "term_text"],
    "additionalProperties": False,
}

SYSTEM = """You rank internship and co-op postings for one student and write short, honest notes.

Scoring (0-100):
- Best: software engineering internships; then other tech internships (IT, data, QA, embedded, firmware,
  cybersecurity, DevOps, robotics); then automotive software/embedded/vehicle roles.
- Programs open to freshmen/sophomores are a plus. Detroit-metro is a plus; the student prefers in-person work.
- Summer 2027 is preferred, but other terms and co-ops are fine (do not lower the score much for them).
- Lower the score a lot if the posting requires a degree level, major or class year the student does not have.
- A GPA minimum must NOT affect the score and must NOT be mentioned in reason or fit_note. Only report it in
  gpa_min (the student shows it separately).

reason: exactly 2 plain sentences on why the score is what it is.
fit_note: 2-3 sentences the student could adapt for a "why this company / why you" field. Use ONLY facts from
the student profile and resume below. Never invent experience, skills, numbers or connections. Do not quote the
posting.
Flags: read them from the posting text only. gpa_min = the minimum GPA number if one is stated, else null.
term_text = the term or dates as the posting states them (empty if not stated)."""


def sha(text):
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


# ------------------------------------------------------------------ student facts (no contact details, no GPA)
def profile_facts(profile):
    e = profile.get("education", {})
    a = profile.get("availability", {})
    w = profile.get("work_authorization", {})
    return {
        "school": e.get("school"), "degree": e.get("degree"), "major": e.get("major"),
        "graduation": e.get("graduation"), "current_year": e.get("current_year"),
        "home_area": f"{profile.get('location', {}).get('city', '')}, {profile.get('location', {}).get('state', '')}",
        "preferred_window": a.get("preferred_window"), "co_op_ok": a.get("coop_ok"),
        "us_citizen": w.get("us_citizen"), "needs_sponsorship": w.get("requires_sponsorship_now_or_future"),
        "languages": profile.get("languages"),
    }


def strip_contact(text):
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[email removed]", text)
    text = re.sub(r"\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}", "[phone removed]", text)
    return re.sub(r"\b(gpa|grade point average)\b[^\n]{0,20}\d\.\d+", "[GPA removed]", text, flags=re.I)


def resume_text(con, path):
    """Plain text of resume.pdf (contact details and GPA removed), cached by file hash."""
    data = path.read_bytes()
    h = hashlib.sha256(data).hexdigest()[:16]
    row = con.execute("SELECT text FROM resume_cache WHERE sha=?", (h,)).fetchone()
    if row:
        return row["text"]
    import pypdf
    raw = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(str(path)).pages)
    text = strip_contact(re.sub(r"[ \t]+", " ", raw)).strip()
    con.execute("INSERT OR REPLACE INTO resume_cache(sha, text) VALUES (?, ?)", (h, text))
    con.commit()
    return text


def load_profile(here):
    path = here / "profile.yaml"
    if not path.exists():
        raise SystemExit("profile.yaml not found. Copy profile.example.yaml to profile.yaml and fill in your details.")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ step A + B: pick and fetch
def candidates(con, cfg, limit=None):
    n = limit or cfg.get("scoring", {}).get("only_score_top_n_after_rules", 40)
    qs = ",".join("?" * len(ACTIVE_STATUSES))
    return con.execute(f"""SELECT p.* FROM postings p JOIN applications a ON a.posting_id=p.id
                           WHERE p.in_scope=1 AND p.active=1 AND a.status IN ({qs})
                           ORDER BY p.rule_score DESC, p.date_posted DESC LIMIT ?""",
                       (*ACTIVE_STATUSES, n)).fetchall()


def fill_descriptions(con, rows):
    """Download the public page for roles with no description. Returns (fetched_ok, unavailable)."""
    ok = bad = 0
    for r in rows:
        if r["description"] and r["desc_source"] != "unavailable":
            continue
        page = fetch.fetch(con, r["url"])
        if page["useful"]:
            con.execute("UPDATE postings SET description=?, desc_source=? WHERE id=?",
                        (page["text"], page["method"], r["id"]))
            ok += 1
        else:
            con.execute("UPDATE postings SET desc_source='unavailable' WHERE id=?", (r["id"],))
            bad += 1
        print(f"  #{r['id']:<5} {page['method'][:24]:24} {len(page.get('text') or ''):6} chars  "
              f"{r['company_name'][:22]} - {r['title'][:50]}")
    con.commit()
    return ok, bad


# ------------------------------------------------------------------ step C: Claude score
def llm_flag_strings(f, above=None):
    out = []
    if f.get("gpa_min"):
        g = float(f["gpa_min"])
        out.append(f"GPA min {g:.1f}" + (f" (above {above})" if above is not None and g > float(above) else ""))
    if f.get("citizenship_required"):
        out.append("US citizenship required")
    if f.get("clearance_required"):
        out.append("clearance/export control")
    if f.get("housing_or_relocation"):
        out.append("housing/relocation mentioned")
    if f.get("early_career_program"):
        out.append("early-career program")
    return out


def merge_flags(rule_flags, llm_flags):
    """A flag shows if either the free rules or Claude found it. GPA: keep one badge (the higher minimum)."""
    out = list(rule_flags)
    for f in llm_flags:
        if f.startswith("GPA min"):
            have = [x for x in out if x.startswith("GPA min")]
            if have:
                if float(f.split()[2]) > float(have[0].split()[2]):
                    out[out.index(have[0])] = f
                continue
        if f not in out:
            out.append(f)
    return out


def posting_prompt(r, max_chars):
    desc = (r["description"] or "")[:max_chars]
    parts = [f"Company: {r['company_name']}", f"Title: {r['title']}", f"Locations: {r['location_raw']}",
             f"Term (from listing): {r['term'] or 'not stated'}", f"Type: {r['job_type']}"]
    if r["sponsorship"]:
        parts.append(f"Listing note: {r['sponsorship']}")
    parts.append("Posting text:\n" + (desc if desc.strip() else "(not available - judge from the title only, and "
                                                               "say so in the reason)"))
    return "\n".join(parts)


GPA_RE = re.compile(r"\bG\.?P\.?A\b|grade point", re.I)
GPA_REMINDER = ("\n\nReminder: your reason and fit_note must not mention GPA or grade point average in any way. "
                "Report a GPA minimum only in gpa_min.")


def drop_gpa_sentences(text):
    """Last-resort guard: remove any sentence that mentions GPA."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return " ".join(s for s in sentences if not GPA_RE.search(s)).strip()


def score_one(con, cfg, client, r, facts, resume, model):
    """Score one role. If the reply mentions GPA, ask once more; then strip any GPA sentence that is left."""
    data, usd = _score_call(con, cfg, client, r, facts, resume, model, "")
    if data and (GPA_RE.search(data["reason"]) or GPA_RE.search(data["fit_note"])):
        retry, usd2 = _score_call(con, cfg, client, r, facts, resume, model, GPA_REMINDER)
        usd += usd2
        data = retry or data
    if not data:
        return None, usd
    data["reason"] = drop_gpa_sentences(data["reason"])
    data["fit_note"] = drop_gpa_sentences(data["fit_note"])
    con.execute("""INSERT OR REPLACE INTO scores(posting_id, desc_sha, model, score, reason, fit_note, flags_json,
                   created) VALUES (?,?,?,?,?,?,?,?)""",
                (r["id"], sha(r["title"] + (r["description"] or "")), model, data["score"], data["reason"],
                 data["fit_note"], json.dumps({"flags": llm_flag_strings(data, cfg.get("gpa", {}).get("flag_above")), "term_text": data["term_text"],
                                               "gpa_min": data["gpa_min"]}), db.now()))
    con.commit()
    return data, usd


def _score_call(con, cfg, client, r, facts, resume, model, extra):
    max_chars = cfg.get("scoring", {}).get("max_description_chars", 8000)
    student = f"STUDENT PROFILE (JSON):\n{json.dumps(facts)}\n\nRESUME TEXT:\n{resume}"
    user = f"{student}\n\nPOSTING:\n{posting_prompt(r, max_chars)}{extra}"
    budget.check(con, cfg, model, len(SYSTEM) + len(user), SCORE_MAX_TOKENS)
    try:
        resp = client.messages.create(
            model=model, max_tokens=SCORE_MAX_TOKENS, system=SYSTEM,
            thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCORE_SCHEMA}},
            messages=[{"role": "user", "content": user}])
    except Exception as e:
        budget.log(con, "score", model, None, posting_id=r["id"], company_id=r["company_id"], ok=False,
                   note=budget.safe_error(e))
        raise
    usd = budget.log(con, "score", model, resp, posting_id=r["id"], company_id=r["company_id"],
                     ok=resp.stop_reason == "end_turn", note=resp.stop_reason)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        print(f"  #{r['id']}: could not read the reply (stop reason {resp.stop_reason}); skipped")
        return None, usd
    data["score"] = max(0, min(100, int(data["score"])))
    return data, usd


def run(con, cfg, here, limit=None, dry_run=False):
    print("Ranking: free rule scores...")
    filters.reclassify_all(con, cfg)
    rows = candidates(con, cfg, limit)
    print(f"Top {len(rows)} in-scope roles by rule score. Reading their public job pages (cached for a week)...")
    ok, bad = fill_descriptions(con, rows)
    print(f"  {ok} descriptions downloaded, {bad} unavailable (flagged 'check posting').")
    filters.reclassify_all(con, cfg)  # regex flags now see the real descriptions
    rows = candidates(con, cfg, limit)
    todo = []
    for r in rows:
        cached = con.execute("SELECT desc_sha FROM scores WHERE posting_id=?", (r["id"],)).fetchone()
        if not cached or cached["desc_sha"] != sha(r["title"] + (r["description"] or "")):
            todo.append(r)
    print(f"{len(rows) - len(todo)} already scored (cached, $0). {len(todo)} need a Claude score.")
    if dry_run or not todo:
        if dry_run:
            print("Dry run: no Claude calls made.")
        return {"scored": 0, "cost": 0.0}
    model = cfg.get("scoring", {}).get("model", "claude-haiku-5-5")
    budget.price(model)  # fail early if the model has no known price
    try:
        client = budget.client()
    except budget.NoApiKey as e:
        print(f"{e}\nRoles keep their free rule scores for now.")
        return {"scored": 0, "cost": 0.0}
    facts = profile_facts(load_profile(here))
    resume = resume_text(con, here / "resume.pdf")
    scored, total = 0, 0.0
    for r in todo:
        try:
            data, usd = score_one(con, cfg, client, r, facts, resume, model)
        except budget.BudgetExceeded as e:
            print(e)
            break
        except Exception as e:
            msg = budget.safe_error(e)
            print(f"  #{r['id']}: API error: {msg}")
            if "key" in msg.lower():
                break
            continue
        total += usd
        if data:
            scored += 1
            print(f"  #{r['id']:<5} score {data['score']:3}  ${usd:.4f}  {r['company_name'][:22]} - {r['title'][:45]}")
    print(f"Scored {scored} roles for ${total:.4f}.")
    print(budget.summary_text(con, cfg))
    return {"scored": scored, "cost": total}
