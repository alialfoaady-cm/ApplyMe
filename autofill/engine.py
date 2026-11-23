"""Decide the answer to every form question, cheapest source first (hard rule 3):

  0. ask-Ali list (salary, criminal history, non-competes, legal attestations, GPA)  -> Ali, never guessed
  1. profile.yaml facts                                                             (free)
  2. form_answers in profile.yaml                                                   (free)
  3. answer cache (earlier answers; never salary or GPA)                            (free)
  4. Claude, one batched call for the rest (facts from profile/resume only)         (~$0.001)

A question is a dict: key, label, kind, options, required, description.
  kind: text | textarea | select | combo | radio | checkbox | multi | yesno | file | location | school
An answer is a dict: key, label, kind, value (str, or list for multi), source, guess, ask, note.
"""
import difflib
import hashlib
import json
import re
from datetime import date

import budget
import db
import rank

MAX_TOKENS = 2500

# ---------------------------------------------------------------- 0. things only Ali may answer
ASK_ALI = [
    ("salary", re.compile(r"salary|compensation|pay (rate|range|expectation)|expected (pay|wage|rate)|desired (pay|wage|rate)"
                          r"|hourly (rate|wage)|wage expectation|how much .* paid", re.I)),
    ("criminal history", re.compile(r"criminal|convicted|conviction|felony|misdemeanou?r|arrested|pleaded guilty", re.I)),
    ("non-compete", re.compile(r"non-?compete|non-?solicit|restrictive covenant|non-?disclosure agreement", re.I)),
    ("legal attestation", re.compile(r"\battest|\bcertify\b|certification that|\bi confirm\b|confirm that|signature|e-?sign|"
                                     r"\bi agree\b|\bi acknowledge|"
                                     r"\bi understand\b|\bi consent\b|consent to|terms (and|&) conditions|"
                                     r"privacy (policy|notice|statement)|by (checking|submitting|clicking)", re.I)),
    ("GPA", re.compile(r"\bG\.?P\.?A\b|grade point", re.I)),
]
NEVER_CACHE = {"salary", "GPA"}  # hard rule 4

SKILLS_RE = re.compile(r"experience|proficien|familiar|skill|programming|languages? do you|framework|tools?\b|"
                       r"technolog|certif|licen[cs]e|years of", re.I)
DECLINE = re.compile(r"decline|don'?t wish|do not wish|prefer not|choose not|not to (say|disclose|answer)|i don'?t want", re.I)
WHY_RE = re.compile(r"why (do you want|are you interested|this (company|role|position|team)|us\b|join|would you like)|"
                    r"what (interests|excites|draws|attracts) you|interest(ed)? in (working|this|joining)|"
                    r"why .* (company|role|position|team|us)\??$|^why [\w .,&'-]{1,40}\??$", re.I)


def norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split())


def qkey(q):
    opts = sorted(norm(o) for o in q.get("options") or [])
    return norm(q["label"])[:200] + "|" + hashlib.sha256(json.dumps(opts).encode()).hexdigest()[:12]


def ask_ali_topic(q):
    """The help text is only read for single checkboxes ("I agree to ..."), where the label can be short."""
    if q["kind"] == "file":
        return None
    text = q["label"] + (f" {q.get('description') or ''}" if q["kind"] == "checkbox" else "")
    for topic, rx in ASK_ALI:
        if rx.search(text):
            return topic
    return None


def is_yes_no(options):
    """True if the options are answers like Yes / No / Unsure (not a list of things to pick from)."""
    return bool(options) and any(norm(o).split(" ")[0] in ("yes", "no") for o in options) and len(options) <= 4


# ---------------------------------------------------------------- matching an answer to the offered options
def pick_option(value, options):
    """Return (option, exact). exact=False means it's the closest option, i.e. a guess to double-check."""
    if not options:
        return value, True
    v = norm(value)
    for o in options:
        if norm(o) == v:
            return o, True
    year = re.search(r"\b(19|20)\d\d\b", str(value))
    if year and any(re.fullmatch(r"(19|20)\d\d", norm(o)) for o in options):  # options are plain years
        for o in options:
            if norm(o) == year.group(0):
                return o, True
    for o in options:  # "Yes" vs "Yes, I am authorized..."
        no = norm(o)
        if no.startswith(v + " ") or (len(v) > 3 and (v in no.split(" ") or no.startswith(v))):
            return o, True
    scored = sorted(options, key=lambda o: difflib.SequenceMatcher(None, v, norm(o)).ratio(), reverse=True)
    return scored[0], False


def _yes_no(value, options):
    """Pick the Yes-ish or No-ish option."""
    want = "yes" if str(value).lower().startswith("y") else "no"
    for o in options or []:
        if norm(o).split(" ")[:1] == [want]:
            return o, True
    return pick_option(want, options)


# ---------------------------------------------------------------- 1. profile facts
def _eeo(field, value, options):
    """Demographic answers: profile value, else 'decline to answer' (hard rule 3)."""
    if not options:
        return value, True
    if value and not DECLINE.search(str(value)) and str(value).lower() != "decline_to_answer":
        v = str(value).lower()
        if field == "gender":
            for o in options:
                n = norm(o)
                if ("female" in v or "woman" in v) and ("female" in n or "woman" in n):
                    return o, True
                if v in ("male", "man") and re.search(r"\b(male|man)\b", n) and "female" not in n:
                    return o, True
        if field == "veteran" and "not" in v:
            for o in options:
                if re.search(r"\bnot\b|\bno\b", norm(o)) and not DECLINE.search(o):
                    return o, True
        if field == "disability" and v.startswith("no"):
            for o in options:
                if re.search(r"^no\b|do not have|don'?t have", norm(o)):
                    return o, True
        if field == "hispanic" and v.startswith("no"):
            for o in options:
                if norm(o).startswith("no"):
                    return o, True
        opt, exact = pick_option(value, options)
        if exact:
            return opt, True
    for o in options:
        if DECLINE.search(o):
            return o, True
    return pick_option(str(value or "decline"), options)


def profile_answer(q, p):
    """Return (value, exact) from profile.yaml, or None if the profile doesn't cover this question."""
    r = _profile_answer(q, p)
    # A yes/no question must get a yes/no answer; otherwise the profile didn't really cover it.
    if r and is_yes_no(q.get("options")) and (not r[1] or norm(str(r[0])).split(" ")[0] not in ("yes", "no")):
        return None  # a "closest option" pick on a Yes/No question means the profile didn't answer it
    return r


def _profile_answer(q, p):
    L = norm(q["label"])
    opts = q.get("options") or []
    loc, edu, w, eeo = p.get("location", {}), p.get("education", {}), p.get("work_authorization", {}), p.get("eeo", {})
    od = p.get("other_defaults", {})
    links = p.get("links", {})

    def has(*words):
        return any(re.search(rf"\b{w}\b", L) for w in words)

    if q["kind"] == "file":
        return None
    if has("first name", "given name", "preferred first name", "preferred name", "legal first name"):
        return p["name"]["first"], True
    if has("last name", "family name", "surname", "legal last name"):
        return p["name"]["last"], True
    if L in ("name", "full name", "legal name", "your name", "full legal name") or has("full name"):
        return f"{p['name']['first']} {p['name']['last']}", True
    if has("email", "e mail"):
        return p.get("email"), True
    if has("phone", "mobile", "telephone", "cell"):
        return p.get("phone"), True
    if has("linkedin"):
        return links.get("linkedin"), True
    if has("github"):
        return links.get("github"), True
    if has("website", "portfolio", "personal site", "personal url") and links.get("github"):
        return links.get("github"), False  # GitHub is the closest thing to a portfolio: double-check
    if q["kind"] == "location" or has("current location", "where are you located", "location city", "city and state"):
        return f"{loc.get('city')}, {loc.get('state')}", True
    if has("street", "address line 1", "address 1") or L == "address":
        return loc.get("street_address"), True
    short = len(L) <= 30
    if has("city") and short:
        return loc.get("city"), True
    if (L in ("state", "province", "state province", "state or province", "state of residence")
            or (L.startswith("state ") and short)):
        return pick_option(loc.get("state"), opts) if opts else (loc.get("state"), True)
    if has("zip", "postal"):
        return str(loc.get("zip")), True
    if has("country") and (short or has("country of residence")) and not has("countries"):
        return pick_option("United States", opts) if opts else ("United States", True)
    if q["kind"] == "school" or has("school", "university", "college", "institution"):
        if has("high school"):
            return None
        return pick_option(edu.get("school"), opts) if opts else (edu.get("school"), True)
    if has("graduation", "grad date", "expected to graduate", "graduation date", "anticipated graduation"):
        if opts:
            return pick_option(edu.get("graduation", ""), opts)
        return edu.get("graduation"), True
    if has("degree", "level of education", "education level", "highest level"):
        return pick_option("Bachelor's", opts) if opts else (edu.get("degree"), True)
    if has("year in school", "class standing", "current year", "academic standing", "class year"):
        return pick_option(edu.get("current_year"), opts) if opts else (edu.get("current_year"), True)
    if has("sponsor", "sponsorship", "require a visa", "need a visa", "visa sponsorship"):
        return _yes_no("yes" if w.get("requires_sponsorship_now_or_future") else "no", opts) if opts else \
            ("Yes" if w.get("requires_sponsorship_now_or_future") else "No", True)
    if has("authorized", "authorised", "eligible to work", "legally", "right to work", "work authorization"):
        return _yes_no("yes" if w.get("authorized_to_work_in_us") else "no", opts) if opts else ("Yes", True)
    if has("citizen", "citizenship") and not has("dual"):
        if opts and any(norm(o).startswith(("yes", "no")) for o in opts):
            return _yes_no("yes" if w.get("us_citizen") else "no", opts)
        return pick_option("U.S. Citizen", opts) if opts else ("U.S. Citizen", True)
    if has("hispanic", "latino", "latinx"):
        return _eeo("hispanic", eeo.get("hispanic_or_latino"), opts)
    if has("gender", "sex") and not has("sexual orientation", "transgender"):
        return _eeo("gender", eeo.get("gender"), opts)
    if has("race", "ethnicity", "racial"):
        return _eeo("race", eeo.get("race"), opts)
    if has("veteran", "military", "armed forces"):
        return _eeo("veteran", eeo.get("veteran"), opts)
    if has("disability", "disabled"):
        return _eeo("disability", eeo.get("disability"), opts)
    if has("sexual orientation", "transgender", "lgbt"):
        return _eeo("other", None, opts)
    if has("pronoun"):
        return pick_option(p.get("pronouns"), opts) if opts else (p.get("pronouns"), True)
    if has("how did you hear", "how did you learn", "where did you hear", "how did you find", "referral source",
           "how you heard", "hear about", "learn about this") or L == "source":
        return pick_option(od.get("how_did_you_hear", "Online job board"), opts) if opts else \
            (od.get("how_did_you_hear"), True)
    if has("language", "languages") and q["kind"] == "multi":
        want = [l.split(" ")[0] for l in p.get("languages", [])] + ["Persian"]
        chosen = [o for o in opts if any(norm(w) in norm(o).split(" ") for w in want)]
        return (chosen, True) if chosen else None
    return None


# ---------------------------------------------------------------- start dates: never before 2027 or today
MONTH_NUM = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
SEASON_NUM = {"spring": 1, "winter": 1, "summer": 5, "fall": 9, "autumn": 9}
DATE_RE = re.compile(r"\b(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?|(spring|winter|summer|fall|autumn))?"
                     r"[\s,'-]*((?:19|20)\d\d)\b", re.I)
NUM_DATE_RE = re.compile(r"\b(?:(20\d\d)-(\d{1,2})(?:-\d{1,2})?|(\d{1,2})/(?:\d{1,2}/)?(20\d\d))\b")
NOW_RE = re.compile(r"\b(immediately|asap|as soon as possible|right away|right now)\b", re.I)
START_RE = re.compile(r"\b(start|starting|begin|availability date|available date)\b", re.I)
DATE_Q_RE = re.compile(r"\b(start|starting|begin|availab\w*|which terms?|semester|season)\b", re.I)
NOT_START_RE = re.compile(r"\b(education|school|degree|graduat\w*|employment|employer|job history|work history|"
                          r"end date|attended)\b", re.I)


def start_floor(p):
    """Earliest allowed start (year, month): the later of today and form_answers 'earliest' (2027-01-01)."""
    t = date.today()
    floor = (t.year, t.month)
    for fa in p.get("form_answers", []):
        e = fa.get("earliest")
        if e:
            y, m = (e.year, e.month) if hasattr(e, "year") else tuple(int(x) for x in str(e).split("-")[:2])
            floor = max(floor, (y, m))
    return floor


def dates_in(value):
    """All (year, month) dates mentioned in an answer or option, e.g. 'May 2026', 'Summer 2027', '05/2027'."""
    text = " ".join(map(str, value)) if isinstance(value, list) else str(value)
    out = []
    for mon, season, year in DATE_RE.findall(text):
        out.append((int(year), MONTH_NUM[mon[:3].lower()] if mon else SEASON_NUM[season.lower()] if season else 1))
    for y1, m1, m2, y2 in NUM_DATE_RE.findall(text):
        out.append((int(y1), int(m1)) if y1 else (int(y2), int(m2)))
    if NOW_RE.search(text):
        t = date.today()
        out.append((t.year, t.month))
    return out


def too_early(value, floor):
    return any(d < floor for d in dates_in(value))


def is_date_q(q):
    return bool(DATE_Q_RE.search(q["label"])) and not NOT_START_RE.search(q["label"])


def _start_answer(q, fa, p):
    """May 2027 if offered, else the earliest option in 2027 or later (flagged). (None, ...) = ask Ali."""
    opts, want, floor = q.get("options") or [], str(fa.get("answer")), start_floor(p)
    if not opts:
        return want, True, ""
    dated = sorted((dates_in(o)[0], i, o) for i, o in enumerate(opts) if dates_in(o) and not too_early(o, floor))
    for d, _, o in dated:
        if dates_in(want) and d == dates_in(want)[0] and not re.search(r"spring|summer|fall|autumn|winter", o, re.I):
            return o, True, ""
    if dated:
        return dated[0][2], False, f"{want} not offered: earliest option in 2027 or later"
    return None, False, "no start date in 2027 or later is offered: you choose"


# ---------------------------------------------------------------- consent rules (form_answers with consent: true)
# Confirming a FACT about Ali (eligibility, legal status, "true and accurate", signatures) is never a consent:
# those always pause for Ali (hard rule 3).
CERT_RE = re.compile(r"\bcertif(y|ies)\b|\battest|\bi confirm\b|confirm (that|the)|true (and|,) (accurate|complete|correct)|"
                     r"accurate and complete|eligib|legally|legal status|authori[sz]ed to work|work authori[sz]ation|"
                     r"citizen|immigration|\bvisa\b|sponsor|signature|e-?sign|under penalty|background check|"
                     r"drug (test|screen)", re.I)
CONSENT_CTX = re.compile(r"consent|agree|opt[ -]?(in|out)|permission|allow|authori[sz]e|accept|acknowledge|receive|"
                         r"may (we|contact)|can we|\bjoin\b|\bokay\b|\bok\b|subscribe|sign (me )?up|would you like|wish to|"
                         r"i understand|by (checking|submitting|clicking)", re.I)
NO_WORDS = re.compile(r"\b(no|not|don'?t|do not|decline|opt out|unsubscribe|disagree|refuse)\b", re.I)


def _consent_option(want, opts):
    """The option meaning yes (I consent) or no (I don't)."""
    yes_no = [o for o in opts if norm(o).split(" ")[:1] in (["yes"], ["no"])]
    if yes_no:
        return _yes_no(want, opts)
    for o in opts:
        if (want == "No") == bool(NO_WORDS.search(o)):
            return o, True
    return pick_option(want, opts)


def consent_answer(q, p):
    """Data/privacy, AI-notetaker/recording, SMS (Yes) and marketing (No) consents. None = not a consent."""
    text = f"{q['label']} {q.get('description') or ''}"
    if CERT_RE.search(text) or not CONSENT_CTX.search(f"{text} {' '.join(q.get('options') or [])}"):
        return None
    for topic, rx in ASK_ALI:  # salary, criminal history, non-compete, GPA always go to Ali
        if topic != "legal attestation" and rx.search(text):
            return None
    t = norm(text)
    for fa in p.get("form_answers", []):
        if not fa.get("consent") or not any(re.search(rf"\b{re.escape(norm(k))}\b", t) for k in fa.get("ask", [])):
            continue
        want = "Yes" if str(fa.get("answer")).lower().startswith("y") else "No"
        why = f"consent rule: {fa.get('consent')} -> {want}"
        opts = q.get("options") or []
        if q["kind"] == "checkbox" or not opts:
            return want, True, why  # checkbox: Yes = tick it, No = leave it unticked
        opt, exact = _consent_option(want, opts)
        return opt, exact, why
    return None


# ---------------------------------------------------------------- 2. form_answers
def form_answer(q, p):
    L = norm(q["label"])
    opts = q.get("options") or []
    floor = start_floor(p)
    for fa in p.get("form_answers", []):
        if fa.get("consent") or not any(re.search(rf"\b{re.escape(norm(k))}\b", L) for k in fa.get("ask", [])):
            continue  # consent entries are handled by consent_answer only
        ans = fa.get("answer")
        if fa.get("earliest"):  # start date
            if NOT_START_RE.search(q["label"]) or is_yes_no(opts):
                continue  # e.g. "Education - start date" or "Can you start in Jan-Sep 2027? Yes/No"
            return _start_answer(q, fa, p)
        if ans == "SELECT_ALL":
            ok = [o for o in opts if not too_early(o, floor)]  # never a 2026 (or past) term
            if q["kind"] == "multi":
                return (ok, True, "ticked every option offered" + (" from 2027 on" if len(ok) < len(opts) else "")) \
                    if ok else (None, False, "every option offered is before 2027: you choose")
            if opts:  # single choice: closest to the preferred window, flagged
                if not ok:
                    return None, False, "every option offered is before 2027: you choose"
                window = p.get("availability", {}).get("preferred_window", "")
                opt, _ = pick_option(window, ok) if window else (ok[0], False)
                yes = [o for o in ok if norm(o).startswith("yes")]
                return (yes[0] if yes else opt), False, "single-choice availability: closest to your preferred window"
            return None
        if is_yes_no(opts) and norm(str(ans)).split(" ")[0] not in ("yes", "no"):
            continue  # e.g. "In-person" doesn't answer "Are you OK working remotely? Yes/No"
        if not opts:
            return ans, True, ""
        for cand in [ans] + list(fa.get("fallback", [])):
            opt, exact = pick_option(cand, opts)
            if exact:
                return opt, True, ""
        opt, _ = pick_option(ans, opts)
        return opt, False, f'"{ans}" not offered; closest option'
    return None


# ---------------------------------------------------------------- 3. cache
def cache_get(con, q):
    if len(norm(q["label"])) < 4:  # an unlabelled field must never reuse another field's answer
        return None
    row = con.execute("SELECT * FROM answer_cache WHERE qkey=?", (qkey(q),)).fetchone()
    if not row:
        return None
    con.execute("UPDATE answer_cache SET used=used+1 WHERE id=?", (row["id"],))
    return json.loads(row["answer_json"]), row["source"], bool(row["is_guess"])


def cache_put(con, q, value, source, guess):
    if len(norm(q["label"])) < 4:
        return
    con.execute("""INSERT OR REPLACE INTO answer_cache(qkey, label, options_json, answer_json, source, is_guess, created)
                   VALUES (?,?,?,?,?,?,?)""",
                (qkey(q), q["label"][:500], json.dumps(q.get("options") or []), json.dumps(value), source,
                 int(guess), db.now()))
    con.commit()


# ---------------------------------------------------------------- 4. Claude (one call per form)
SYSTEM = """You answer job-application form questions for one student, using ONLY the facts given
(STUDENT PROFILE, FORM ANSWERS, RESUME TEXT). Never invent experience, skills, employers, dates, numbers or
awards.

For each question return:
- answer: for questions with options, copy one option text EXACTLY; for multi-select put the chosen option
  texts in answers; for text questions write a short factual answer (one line, no essays).
- based_on: the fact you used (quote a few words of the profile/resume), or "" if none.
- is_guess: true if no fact answers it exactly and you picked the closest option.
- must_ask_ali: true when the facts do not answer it and guessing would be wrong. ALWAYS true for: salary or pay,
  criminal history, non-compete or similar agreements, legal statements to agree to or certify, and any
  skill, tool, certification, license or experience question that the resume/profile does not clearly show
  (never answer "Yes" to a skill or experience the resume does not show).
Demographic questions not covered: choose the decline-to-answer option if there is one.
Start dates and terms: never any date in 2026 or earlier. Prefer May 2027, else the earliest 2027 option."""

SCHEMA = {
    "type": "object",
    "properties": {"answers": {"type": "array", "items": {
        "type": "object",
        "properties": {"key": {"type": "string"}, "answer": {"type": "string"},
                       "answers": {"type": "array", "items": {"type": "string"}},
                       "based_on": {"type": "string"}, "is_guess": {"type": "boolean"},
                       "must_ask_ali": {"type": "boolean"}},
        "required": ["key", "answer", "answers", "based_on", "is_guess", "must_ask_ali"],
        "additionalProperties": False}}},
    "required": ["answers"], "additionalProperties": False,
}


def claude_answers(con, cfg, here, questions, posting_id, company_id):
    """One call for all of a form's remaining questions. Returns ({key: result}, cost)."""
    if not questions:
        return {}, 0.0
    model = cfg.get("autofill", {}).get("model", "claude-haiku-5-5")
    p = rank.load_profile(here)
    facts = rank.profile_facts(p)
    resume = rank.resume_text(con, here / "resume.pdf")
    qs = [{"key": q["key"], "question": q["label"][:400], "help_text": (q.get("description") or "")[:300],
           "type": q["kind"], "options": (q.get("options") or [])[:60], "required": bool(q.get("required"))}
          for q in questions]
    user = (f"STUDENT PROFILE (JSON):\n{json.dumps(facts)}\n\nFORM ANSWERS:\n"
            f"{json.dumps([{'ask': f.get('ask'), 'answer': f.get('answer')} for f in p.get('form_answers', [])])}"
            f"\n\nRESUME TEXT:\n{resume}\n\nQUESTIONS (JSON):\n{json.dumps(qs)}")
    budget.check(con, cfg, model, len(SYSTEM) + len(user), MAX_TOKENS)
    client = budget.client()
    try:
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM, thinking={"type": "disabled"},
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": user}])
    except Exception as e:
        budget.log(con, "autofill", model, None, posting_id=posting_id, company_id=company_id, ok=False,
                   note=budget.safe_error(e))
        raise
    usd = budget.log(con, "autofill", model, resp, posting_id=posting_id, company_id=company_id,
                     ok=resp.stop_reason == "end_turn", note=resp.stop_reason)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        data = json.loads(text)["answers"]
    except (ValueError, KeyError):
        return {}, usd
    return {a["key"]: a for a in data}, usd


# ---------------------------------------------------------------- putting it together
def plan(con, cfg, here, questions, posting, fit_note, ask_fn, use_claude=True):
    """Decide every answer. ask_fn(question, topic) -> str/list/None is called for ask-Ali questions
    (None = leave it for Ali in the browser). Returns (answers, cost)."""
    p = rank.load_profile(here)
    out, todo, cost = [], [], 0.0
    floor = start_floor(p)

    def add(q, value, source, guess=False, ask=False, note=""):
        # Start dates / terms: never before 2027 or today, whatever the source (cache, Claude, closest option).
        if value not in (None, "", []) and source not in ("you (just now)", "your earlier answer") and is_date_q(q):
            if isinstance(value, list):
                kept = [v for v in value if not too_early(v, floor)]
                value, note = (kept, note) if kept else (None, "every choice was before 2027: you choose")
            elif too_early(value, floor):
                value, note = None, f"refused '{value}': start dates must be 2027 or later"
            if value is None:
                source, guess, ask = "left for you", False, True
        out.append({"key": q["key"], "label": q["label"], "kind": q["kind"], "required": q.get("required"),
                    "value": value, "source": source, "guess": guess, "ask": ask, "note": note})

    for q in questions:
        q["label"] = " ".join((q.get("label") or "").split())
    label_count = {}
    for q in questions:
        label_count[norm(q["label"])] = label_count.get(norm(q["label"]), 0) + 1
    for q in questions:
        if q["kind"] == "file":
            continue  # resume / cover letter handled by the site adapter
        if len(norm(q["label"])) < 2 or (label_count[norm(q["label"])] > 1 and q["kind"] in ("select", "combo", "text")
                                         and not profile_answer(q, p)):
            # unlabelled, or several fields share one label (e.g. start/end month and year): never guess
            if q.get("required"):
                add(q, None, "left for you", ask=True, note="unclear field (no label of its own): fill it yourself")
            else:
                add(q, None, "left blank", note="unclear optional field")
            continue
        r = consent_answer(q, p)
        if r:
            add(q, r[0], "consent rule", guess=not r[1], note=r[2])
            continue
        topic = ask_ali_topic(q)
        if topic:
            if topic == "GPA" and not q.get("required"):
                add(q, None, "left blank", note="GPA is never entered (optional field)")
                continue
            if topic not in NEVER_CACHE:
                hit = cache_get(con, q)
                if hit and hit[1] == "ali":
                    add(q, hit[0], "your earlier answer")
                    continue
            v = ask_fn(q, topic)
            if v in (None, "", []):
                add(q, None, "left for you", ask=True, note=f"{topic}: you answer this one")
            else:
                if topic not in NEVER_CACHE:
                    cache_put(con, q, v, "ali", False)
                add(q, v, "you (just now)")
            continue
        r = profile_answer(q, p)
        if r and r[0] not in (None, "", []):
            add(q, r[0], "profile", guess=not r[1], note="" if r[1] else "closest option to your profile")
            continue
        r = form_answer(q, p)
        if r and r[0] in (None, "", []):  # e.g. only 2026 start dates offered
            v = ask_fn(q, r[2])
            add(q, v or None, "you (just now)" if v else "left for you", ask=not v, note="" if v else r[2])
            continue
        if r:
            add(q, r[0], "form_answers", guess=not r[1], note=r[2])
            continue
        if q["kind"] == "textarea" or (q["kind"] == "text" and WHY_RE.search(q["label"])):
            if WHY_RE.search(q["label"]):
                from autofill import why  # researched answer, always shown in the double-check box
                try:
                    v, note, c, _ = why.answer(con, cfg, here, posting, q, use_claude)
                except budget.BudgetExceeded:
                    raise
                except Exception as e:
                    v, note, c = None, f"research failed ({budget.safe_error(e)[:80]}): please write it yourself", 0.0
                cost += c
                if v:
                    add(q, v, "researched answer", guess=True, note=note)
                else:
                    add(q, None, "left for you", ask=bool(q.get("required")), note=note)
            elif q.get("required"):
                add(q, None, "left for you", ask=True, note="written answer: please write this yourself")
            else:
                add(q, None, "left blank", note="optional written answer")
            continue
        hit = cache_get(con, q)
        if hit:
            add(q, hit[0], "earlier answer" if hit[1] == "claude" else "your earlier answer", guess=hit[2],
                note="from an earlier form" if hit[2] else "")
            continue
        if not q.get("required") and q["kind"] in ("text", "location"):
            add(q, None, "left blank", note="optional, not in your profile")
            continue
        todo.append(q)

    if todo and use_claude:
        results, cost = claude_answers(con, cfg, here, todo, posting["id"], posting["company_id"])
    else:
        results = {}
    for q in todo:
        a = results.get(q["key"])
        if not a:
            add(q, None, "left for you", ask=bool(q.get("required")),
                note="not answered (dry run)" if not use_claude else "Claude could not answer it")
            continue
        if not a["must_ask_ali"] and a["is_guess"] and norm(a["answer"]) in ("other", "unsure", "n a", "none"):
            a["must_ask_ali"] = True  # a guessed "Other" says nothing: ask instead
        if a["must_ask_ali"]:
            v = ask_fn(q, "not covered by your profile/resume")
            if v in (None, "", []):
                add(q, None, "left for you", ask=True, note="not covered by your profile or resume")
            else:
                cache_put(con, q, v, "ali", False)
                add(q, v, "you (just now)")
            continue
        opts = q.get("options") or []
        if q["kind"] == "multi":
            value = [o for o in opts if norm(o) in {norm(x) for x in a["answers"]}]
            guess = a["is_guess"] or not value
        elif opts:
            value, exact = pick_option(a["answer"], opts)
            guess = a["is_guess"] or not exact
        else:
            value, guess = a["answer"], a["is_guess"]
        if value in (None, "", []):
            add(q, None, "left for you", ask=bool(q.get("required")), note="no usable answer")
            continue
        skills = SKILLS_RE.search(q["label"])
        cache_put(con, q, value, "claude", guess or bool(skills))
        note = f"based on: {a['based_on'][:80]}" if a["based_on"] else "closest option"
        if skills:
            note = "skills/experience: check every answer matches your resume. " + note
        add(q, value, "Claude", guess=guess or bool(skills), note=note)
    return out, cost
