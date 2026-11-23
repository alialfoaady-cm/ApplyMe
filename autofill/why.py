"""'Why do you want to work here?' answers.

1. Company research: reuses the cover-letter research (company_research table, 90 days) or runs it once.
2. One small Haiku call writes <= ~80 words: their project -> Ali's similar real project -> same/similar tools.
3. Free local checks: the company fact has a source from the research; Ali's project and every tool appear in
   resume.pdf or profile.yaml; no long runs copied from the resume; short; no GPA. One retry, else left for Ali.
4. Saved per role (why_answers) + a notes file with sources in cover_letters/.
"""
import json
import re
from pathlib import Path

import budget
import db
import letters
import rank
from autofill import engine

MAX_TOKENS = 1200
MAX_WORDS = 90

SYSTEM = """You write a short answer to a job-application question like "Why do you want to work here?" for a
student. The student reviews it before submitting.

Shape (natural wording, under 80 words, first person, plain, no cliches like "passionate" or "dynamic"):
"I really like [one of their real projects/products]. I built something similar ([the student's real project])
using [the same or similar tools]. I enjoy this kind of work and would like to continue it at [company]."

Hard rules:
- Company facts: ONLY from COMPANY RESEARCH. Pick 1 (at most 2) projects/products and give the source_url of the
  fact you used, copied exactly from the research.
- Student facts: ONLY projects and tools that appear in RESUME TEXT, HIGHLIGHTS or PROFILE. Never claim a tool,
  skill or experience that is not there. List every tool you mention in my_tools, spelled as in the resume.
- If no real project of the student honestly matches anything the company does, set honest_match=false and write
  a short general but honest answer (what draws the student to the company's work, using only real facts), with
  no invented similarity.
- Do not paste or list the resume. Paraphrase; never copy sentences from the research. Never mention GPA."""

SCHEMA = {
    "type": "object",
    "properties": {
        "honest_match": {"type": "boolean"},
        "company_project": {"type": "string"},
        "source_url": {"type": "string"},
        "my_project": {"type": "string"},
        "my_tools": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
    },
    "required": ["honest_match", "company_project", "source_url", "my_project", "my_tools", "answer"],
    "additionalProperties": False,
}

STOP = {"with", "from", "using", "that", "this", "project", "projects", "built", "work", "team", "about", "based",
        "system", "your", "their"}


def _words(s):
    return f" {engine.norm(s)} "


def _research_urls(info, sources):
    urls = {s["url"] for s in sources}
    urls |= set(re.findall(r"https?://[^\s\"']+", json.dumps(info)))
    return {u.rstrip("/.,") for u in urls}


def problems(r, allowed, resume, urls):
    """Free local checks. Returns a list of problems (empty = OK)."""
    out = []
    text = r["answer"].strip()
    if not text:
        return ["empty answer"]
    if len(text.split()) > MAX_WORDS:
        out.append(f"too long ({len(text.split())} words, limit about 80)")
    if re.search(r"\bG\.?P\.?A\b|grade point", text, re.I):
        out.append("mentions GPA")
    for t in r["my_tools"]:
        if engine.norm(t) and _words(t) not in allowed:
            out.append(f"tool not on the resume/profile: {t}")
    if r["honest_match"]:
        if r["source_url"].rstrip("/.,") not in urls:
            out.append("company fact has no source from the research")
        key = [w for w in engine.norm(r["my_project"]).split() if len(w) > 3 and w not in STOP]
        if not key or sum(f" {w} " in allowed for w in key) * 2 < len(key):
            out.append(f"project not found on the resume/profile: {r['my_project']}")
    copied = letters.copied_phrases(text, resume, n=8)
    if copied:
        out.append(f"copies the resume word for word: '{copied[0]}'")
    return out


def answer(con, cfg, here, posting, q, use_claude=True):
    """Returns (text or None, note, cost, honest_match)."""
    key = engine.qkey(q)
    row = con.execute("SELECT * FROM why_answers WHERE posting_id=? AND qkey=?", (posting["id"], key)).fetchone()
    if row:
        return row["answer"], f"researched answer (saved earlier; sources: {row['notes_path']})", 0.0, \
            bool(row["honest_match"])
    if not use_claude:
        return None, "would research the company and write a short answer (~4 cents new company, ~0.1 cent after)", \
            0.0, False

    client = budget.client()
    info, sources, cost = letters.research(con, cfg, client, posting)
    p = rank.load_profile(here)
    resume = rank.resume_text(con, here / "resume.pdf")
    highlights = p.get("highlights", [])
    allowed = _words(" ".join([resume, " ".join(highlights), json.dumps(rank.profile_facts(p))]))
    urls = _research_urls(info, sources)
    model = cfg.get("autofill", {}).get("why_model", cfg.get("autofill", {}).get("model", "claude-haiku-5-5"))
    user = (f"COMPANY: {posting['company_name']}\nROLE: {posting['title']}\n"
            f"ROLE DESCRIPTION (start):\n{(posting['description'] or '')[:2000]}\n\n"
            f"COMPANY RESEARCH (JSON, with source_url per fact):\n{json.dumps(info)[:6000]}\n\n"
            f"HIGHLIGHTS:\n{json.dumps(highlights)}\n\nPROFILE:\n{json.dumps(rank.profile_facts(p))}\n\n"
            f"RESUME TEXT:\n{resume}\n\nQUESTION: {q['label'][:400]}")
    messages = [{"role": "user", "content": user}]
    r, probs = None, ["no answer"]
    for attempt in range(2):
        budget.check(con, cfg, model, len(SYSTEM) + sum(len(str(m["content"])) for m in messages), MAX_TOKENS)
        try:
            resp = client.messages.create(
                model=model, max_tokens=MAX_TOKENS, system=SYSTEM, thinking={"type": "disabled"},
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
                messages=messages)
        except Exception as e:
            budget.log(con, "why", model, None, posting_id=posting["id"], company_id=posting["company_id"], ok=False,
                       note=budget.safe_error(e))
            raise
        cost += budget.log(con, "why", model, resp, posting_id=posting["id"], company_id=posting["company_id"],
                           ok=resp.stop_reason == "end_turn", note=resp.stop_reason)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            r = json.loads(text)
        except ValueError:
            r = None
            continue
        probs = problems(r, allowed, resume, urls)
        if not probs:
            break
        messages = [{"role": "user", "content": user}, {"role": "assistant", "content": text},
                    {"role": "user", "content": "Fix these problems and answer again in the same JSON shape: "
                                                + "; ".join(probs)}]

    notes_path = _write_notes(here, posting, q, r, probs, sources, info, cost)
    if not r or probs:
        return None, f"could not write an honest answer ({'; '.join(probs)[:150]}): please write it yourself. " \
                     f"Research: {notes_path}", cost, False
    con.execute("INSERT OR REPLACE INTO why_answers VALUES (?,?,?,?,?,?,?)",
                (posting["id"], key, r["answer"].strip(), int(r["honest_match"]), str(notes_path), cost, db.now()))
    con.commit()
    if r["honest_match"]:
        note = f"researched answer: their {r['company_project'][:60]} <-> your {r['my_project'][:40]}. " \
               f"Check it; sources: {notes_path}"
    else:
        note = f"NO honest project match found, so this is a general answer. Check it; research: {notes_path}"
    return r["answer"].strip(), note, cost, r["honest_match"]


def _write_notes(here, posting, q, r, probs, sources, info, cost):
    folder = here / "cover_letters"
    folder.mkdir(exist_ok=True)
    path = folder / f"why_{letters.safe_name(posting['company_name'], 30)}_{letters.safe_name(posting['title'], 40)}_notes.txt"
    lines = [f"Why-this-company answer for #{posting['id']} {posting['company_name']} - {posting['title']}",
             f"Question: {q['label']}", ""]
    if r:
        lines += ["Answer:", r.get("answer", "").strip(), "",
                  f"Honest project match: {'yes' if r.get('honest_match') else 'NO (general answer)'}",
                  f"Their project/product: {r.get('company_project', '')}",
                  f"Source for it: {r.get('source_url', '')}",
                  f"Your project used: {r.get('my_project', '')}",
                  f"Your tools mentioned: {', '.join(r.get('my_tools', [])) or '-'}", ""]
    if probs and probs != []:
        lines += ["NOT USED, problems found by the local checks:"] + [f"- {x}" for x in probs] + [""]
    lines += ["Company research facts (from the cached research):"]
    for k, v in (info or {}).items():
        for item in (v if isinstance(v, list) else [v]):
            if isinstance(item, dict) and item.get("fact"):
                lines.append(f"- [{k}] {item['fact']} ({item.get('source_url', '')})")
            elif isinstance(item, str) and item:
                lines.append(f"- [{k}] {item[:4000]}")
    lines += ["", "All sources:"] + [f"- {s.get('title', '')} {s['url']}" for s in sources]
    lines += ["", f"Cost of this answer (including any new research): ${cost:.4f}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
