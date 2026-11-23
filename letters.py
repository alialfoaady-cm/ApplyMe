"""Phase 2 (on request): one cover-letter DRAFT for one posting. Run: python applyme.py letter <#>

1. Company research with web search: once per company, cached in company_research.
2. One call writes the letter from real facts only (profile.yaml, resume.pdf, research, the posting).
3. Free local checks: word count, and phrases copied from the research or the posting.
4. Saves cover_letters/<company>_<role>.txt and .pdf, plus <company>_<role>_research_notes.txt with links.
Your contact details are added to the PDF here on your computer; they are never sent to the API.
"""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import budget
import db
import fetch
import rank

RESEARCH_MAX_TOKENS = 3000
WRITE_MAX_TOKENS = 4000
SEARCH_TOKENS_EST = 15000  # worst-case input tokens added per web search (for the budget check)

RESEARCH_PROMPT = """Research the company "{company}" for a student's internship cover letter.
Role: {title}. Location: {location}.
Use web search (at most {n} searches) to find, from reliable sources (the company's own site, reputable news):
- what the company builds or does
- notable products or projects
- recent news (last 12 months)
- the mission
- anything about the specific team or role, if available
Rules: only report facts you actually found in a source, each with its source URL. Write every fact in your own
words (no quotes, no copied sentences). If you found nothing for a field, leave it empty.
Reply with ONLY a JSON object (no other text) in this shape:
{{"what_they_build": {{"fact": "", "source_url": ""}},
 "products_or_projects": [{{"fact": "", "source_url": ""}}],
 "recent_news": [{{"fact": "", "date": "", "source_url": ""}}],
 "mission": {{"fact": "", "source_url": ""}},
 "team_or_role": [{{"fact": "", "source_url": ""}}]}}"""

WRITE_SYSTEM = """You write cover-letter DRAFTS for a student. The student reviews every draft before using it.

Hard rules:
- Use ONLY facts from STUDENT PROFILE, HIGHLIGHTS and RESUME TEXT about the student. Never invent experience,
  skills, numbers, awards, people or connections. Never claim experience the student does not have.
- Use ONLY company facts from COMPANY RESEARCH. Never invent projects, quotes or connections.
- Paraphrase. Never copy sentences or phrases from the posting or the research.
- Never mention GPA.
- Body of 200-300 words, plain and human, no cliches ("I am writing to express", "passionate", "dynamic",
  "fast-paced", "leverage", "synergy", "perfect fit"), under one page.
- Pick the single best match between the company (what they build, a product, recent news, mission or the team)
  and ONE real thing from the student's life or projects, and tie it back clearly. One strong link beats a list.
- Start with "Dear {greeting}," and end with "Sincerely,\\nAli Alfoaady". No address block or date (added later).
"""

LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "letter": {"type": "string", "description": "the full letter from 'Dear' to the signature"},
        "best_match": {"type": "string", "description": "one sentence: which company fact was tied to which "
                                                        "student fact"},
        "company_facts_used": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["letter", "best_match", "company_facts_used"],
    "additionalProperties": False,
}


def safe_name(s, n=40):
    return re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")[:n] or "x"


def ask(prompt, default=""):
    try:
        v = input(prompt).strip()
    except EOFError:
        v = ""
    return v or default


# ------------------------------------------------------------------ research (once per company)
def _search_tool(model, n):
    # Newer search tool (filters results before they reach the model) on Sonnet; basic tool elsewhere.
    t = "web_search_20260209" if model.startswith("claude-sonnet") else "web_search_20250305"
    return {"type": t, "name": "web_search", "max_uses": n}


def _call_research(con, cfg, client, model, prompt, n, company_id):
    budget.check(con, cfg, model, len(prompt), RESEARCH_MAX_TOKENS, max_searches=n,
                 search_tokens=n * SEARCH_TOKENS_EST)
    messages = [{"role": "user", "content": prompt}]
    resp, usd = None, 0.0
    for _ in range(3):  # the server may pause a long search turn; continue it (max 2 times)
        try:
            resp = client.messages.create(model=model, max_tokens=RESEARCH_MAX_TOKENS, messages=messages,
                                          tools=[_search_tool(model, n)])
        except Exception as e:
            budget.log(con, "research", model, None, company_id=company_id, ok=False, note=budget.safe_error(e))
            raise
        usd += budget.log(con, "research", model, resp, company_id=company_id,
                          ok=resp.stop_reason in ("end_turn", "pause_turn"), note=resp.stop_reason)
        if resp.stop_reason != "pause_turn":
            break
        messages = messages[:1] + [{"role": "assistant", "content": resp.content}]
        budget.check(con, cfg, model, len(prompt), RESEARCH_MAX_TOKENS, search_tokens=n * SEARCH_TOKENS_EST)
    return resp, usd


def _parse_research(resp):
    text = "".join(b.text for b in resp.content if b.type == "text")
    sources = []
    for b in resp.content:
        if b.type == "web_search_tool_result" and isinstance(b.content, list):
            for item in b.content:
                url = getattr(item, "url", None)
                if url and url not in [s["url"] for s in sources]:
                    sources.append({"url": url, "title": getattr(item, "title", "")})
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not data:
        data = {"raw_notes": text.strip()[:4000]}
    return data, sources


def research(con, cfg, client, posting):
    lc = cfg.get("cover_letters", {})
    cid = posting["company_id"]
    days = lc.get("research_cache_days", 90)
    row = con.execute("SELECT * FROM company_research WHERE company_id=?", (cid,)).fetchone()
    if row and row["researched_at"] >= (datetime.now() - timedelta(days=days)).isoformat():
        print(f"Company research: reusing cached research from {row['researched_at'][:10]} ($0).")
        return json.loads(row["summary_json"]), json.loads(row["sources_json"]), 0.0
    n = int(lc.get("research_max_searches", 4))
    model = lc.get("research_model", "claude-haiku-5-5")
    prompt = RESEARCH_PROMPT.format(company=posting["company_name"], title=posting["title"],
                                    location=posting["location_raw"] or "", n=n)
    print(f"Company research: searching the web for {posting['company_name']} with {model} (max {n} searches)...")
    try:
        resp, usd = _call_research(con, cfg, client, model, prompt, n, cid)
    except budget.BudgetExceeded:
        raise
    except Exception as e:
        fallback = "claude-sonnet-5-5"
        if model == fallback or getattr(e, "status_code", None) != 400:
            raise
        print(f"  {model} could not run web search ({budget.safe_error(e)[:120]}); trying {fallback}...")
        model = fallback
        resp, usd = _call_research(con, cfg, client, model, prompt, n, cid)
    data, sources = _parse_research(resp)
    con.execute("""INSERT OR REPLACE INTO company_research(company_id, researched_at, model, summary_json,
                   sources_json, cost_usd) VALUES (?,?,?,?,?,?)""",
                (cid, db.now(), model, json.dumps(data), json.dumps(sources), usd))
    con.commit()
    print(f"  done: {len(sources)} sources, ${usd:.4f}")
    return data, sources, usd


# ------------------------------------------------------------------ checks (free, local)
def copied_phrases(letter, sources_text, n=8):
    """Runs of n+ words that appear word-for-word in the research or the posting."""
    def words(t):
        return re.findall(r"[a-z0-9']+", t.lower())
    src = words(sources_text)
    grams = {" ".join(src[i:i + n]) for i in range(len(src) - n + 1)}
    w = words(letter)
    hits = []
    for i in range(len(w) - n + 1):
        g = " ".join(w[i:i + n])
        if g in grams and not any(g in h for h in hits):
            hits.append(g)
    return hits


def body_words(letter):
    lines = letter.strip().splitlines()
    body = [l for l in lines if not re.match(r"^(dear |sincerely|ali alfoaady)", l.strip().lower())]
    return len(" ".join(body).split())


# ------------------------------------------------------------------ PDF
def write_pdf(path, header_lines, letter):
    from fpdf import FPDF
    pdf = FPDF(format="Letter")
    pdf.set_margins(25, 22, 25)
    pdf.add_page()
    arial = Path("C:/Windows/Fonts/arial.ttf")
    if arial.exists():
        pdf.add_font("Body", "", str(arial))
        font = "Body"
    else:  # built-in font only knows basic characters
        font = "Helvetica"
        repl = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}
        for a, b in repl.items():
            letter = letter.replace(a, b)
            header_lines = [h.replace(a, b) for h in header_lines]
        letter = letter.encode("latin-1", "replace").decode("latin-1")
    pdf.set_font(font, size=11)
    for h in header_lines:
        pdf.cell(0, 5.5, h, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    for para in letter.strip().split("\n"):
        if para.strip():
            pdf.multi_cell(0, 5.5, para.strip(), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)
    pdf.output(str(path))
    return pdf.pages_count


# ------------------------------------------------------------------ main
def make_letter(con, cfg, here, posting_id):
    p = con.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()
    if not p:
        print(f"No posting #{posting_id}.")
        return
    if not p["in_scope"]:
        print(f"#{posting_id} is outside your current scope ({p['exclude_reason']}), so no letter is written.")
        return
    print(f"#{posting_id}: {p['company_name']} - {p['title']} ({p['location_raw']})")
    answer = ask("Does this application have a cover letter field (required or optional)? [y/n]: ").lower()
    if not answer.startswith("y"):
        print("No cover letter field, so no letter (nothing spent).")
        return
    greeting = ask("Recruiter / hiring manager name if the posting names one (Enter = 'Hiring Team'): ", "Hiring Team")

    if not p["description"]:
        page = fetch.fetch(con, p["url"])
        if page["useful"]:
            con.execute("UPDATE postings SET description=?, desc_source=? WHERE id=?",
                        (page["text"], page["method"], posting_id))
            con.commit()
            p = con.execute("SELECT * FROM postings WHERE id=?", (posting_id,)).fetchone()

    try:
        client = budget.client()
        info, sources, research_usd = research(con, cfg, client, p)
        profile = rank.load_profile(here)
        facts = rank.profile_facts(profile)
        resume = rank.resume_text(con, here / "resume.pdf")
        lc = cfg.get("cover_letters", {})
        model = lc.get("writer_model", "claude-sonnet-5-5")
        system = WRITE_SYSTEM.format(greeting=greeting)
        user = (f"STUDENT PROFILE (JSON):\n{json.dumps(facts)}\n\nHIGHLIGHTS:\n"
                + "\n".join(f"- {h}" for h in profile.get("highlights", []))
                + f"\n\nRESUME TEXT:\n{resume}\n\nCOMPANY RESEARCH (JSON):\n{json.dumps(info)}\n\n"
                  f"POSTING:\n{rank.posting_prompt(p, 6000)}\n\nMax {lc.get('max_words', 300)} words in the body.")
        budget.check(con, cfg, model, len(system) + len(user), WRITE_MAX_TOKENS)
        kwargs = {"output_config": {"effort": "low", "format": {"type": "json_schema", "schema": LETTER_SCHEMA}}}
        if model.startswith("claude-haiku"):
            kwargs["thinking"] = {"type": "disabled"}
        print(f"Writing the draft with {model}...")
        try:
            resp = client.messages.create(model=model, max_tokens=WRITE_MAX_TOKENS, system=system,
                                          messages=[{"role": "user", "content": user}], **kwargs)
        except Exception as e:
            budget.log(con, "letter", model, None, posting_id=posting_id, company_id=p["company_id"], ok=False,
                       note=budget.safe_error(e))
            raise
        write_usd = budget.log(con, "letter", model, resp, posting_id=posting_id, company_id=p["company_id"],
                               ok=resp.stop_reason == "end_turn", note=resp.stop_reason)
    except budget.BudgetExceeded as e:
        print(e)
        return
    except budget.NoApiKey as e:
        print(e)
        return
    except Exception as e:
        print(f"API error: {budget.safe_error(e)}")
        return

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        print(f"Could not read the reply (stop reason {resp.stop_reason}). Cost so far is logged; try again later.")
        return
    letter = out["letter"].strip()

    # Free checks
    n_words = body_words(letter)
    copied = copied_phrases(letter, json.dumps(info) + " " + (p["description"] or ""))
    warnings = []
    if n_words > cfg.get("cover_letters", {}).get("max_words", 300):
        warnings.append(f"Body is {n_words} words (over the limit): trim it before using.")
    if n_words < 150:
        warnings.append(f"Body is only {n_words} words.")
    for c in copied:
        warnings.append(f'Copied wording (8+ words match the research/posting), reword it: "...{c}..."')
    if re.search(r"\bgpa\b", letter, re.I):
        warnings.append("The letter mentions GPA: remove it.")

    # Save
    folder = here / "cover_letters"
    folder.mkdir(exist_ok=True)
    stem = f"{safe_name(p['company_name'], 30)}_{safe_name(p['title'], 50)}"
    contact = [profile["name"]["first"] + " " + profile["name"]["last"],
               " | ".join(x for x in (profile.get("email"), profile.get("phone"),
                                      f"{profile['location']['city']}, {profile['location']['state']}") if x),
               date.today().strftime("%B %d, %Y").replace(" 0", " ")]
    (folder / f"{stem}.txt").write_text("\n".join(contact) + "\n\n" + letter + "\n", encoding="utf-8")
    pages = write_pdf(folder / f"{stem}.pdf", contact, letter)
    if pages > 1:
        warnings.append("The PDF is longer than one page: trim the letter.")
    notes = [f"Research notes for: {p['company_name']} - {p['title']}", f"Posting: {p['url']}",
             f"Letter written {db.now()} with {cfg.get('cover_letters', {}).get('writer_model')}",
             f"Best match used: {out['best_match']}", "", "Company facts used in the letter:"]
    notes += [f"- {f}" for f in out["company_facts_used"]]
    notes += ["", "Research (from web search, cached per company):", json.dumps(info, indent=2), "",
              "All sources the search returned:"]
    notes += [f"- {s.get('title', '')}: {s['url']}" for s in sources]
    if warnings:
        notes += ["", "Checks:"] + [f"- {w}" for w in warnings]
    (folder / f"{stem}_research_notes.txt").write_text("\n".join(notes) + "\n", encoding="utf-8")
    con.execute("UPDATE applications SET cover_letter_path=?, updated_at=? WHERE posting_id=?",
                (str(folder / f"{stem}.pdf"), db.now(), posting_id))
    con.commit()

    print("\n" + "=" * 70 + "\nDRAFT - read and edit before uploading anything\n" + "=" * 70)
    print(letter)
    print("=" * 70)
    print(f"Body: {n_words} words. Best match: {out['best_match']}")
    for w in warnings:
        print(f"WARNING: {w}")
    print(f"Saved:\n  {folder / (stem + '.txt')}\n  {folder / (stem + '.pdf')}\n"
          f"  {folder / (stem + '_research_notes.txt')}  (source links)")
    print(f"Cost of this letter: ${research_usd + write_usd:.4f} "
          f"(research ${research_usd:.4f}, writing ${write_usd:.4f})")
    print(budget.summary_text(con, cfg))
