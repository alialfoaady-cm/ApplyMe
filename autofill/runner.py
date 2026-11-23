"""python applyme.py open N: fill the top N unapplied roles in the ApplyMe Chrome window, stopping BEFORE submit."""
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import budget
import db
import fetch
from autofill import browser, engine, forms, greenhouse
from autofill.guard import GuardError, safe_click

ACTIVE = ("new", "saved", "ready_to_review")


# ------------------------------------------------------------------ which site, and the form's address
def site_of(url):
    host = urlsplit(url).netloc.lower()
    if "greenhouse.io" in host or "gh_jid=" in url:
        return "greenhouse"
    if host == "jobs.lever.co":
        return "lever"
    if host == "jobs.ashbyhq.com":
        return "ashby"
    if "myworkdayjobs.com" in host or "myworkdaysite.com" in host:
        return "workday"
    return None


def form_url(site, url):
    if site == "greenhouse":
        return greenhouse.form_url(url)
    if site == "lever":
        m = re.match(r"(https://jobs\.lever\.co/[^/]+/[0-9a-f-]{36})", url)
        return m.group(1) + "/apply" if m else None
    if site == "ashby":
        m = re.match(r"(https://jobs\.ashbyhq\.com/[^/]+/[0-9a-f-]{36})", url)
        return m.group(1) + "/application" if m else None
    return None


def pick_roles(con, cfg, n, refill=False, include_unsupported=False):
    sites = set(cfg.get("autofill", {}).get("sites", ["greenhouse", "lever", "ashby"]))
    hours = cfg.get("autofill", {}).get("refill_after_hours", 24)
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    rows = con.execute(f"""
        SELECT p.*, a.status, a.letter_approved, a.cover_letter_path, s.fit_note,
               COALESCE(s.score, p.rule_score, 0) AS best
        FROM postings p JOIN applications a ON a.posting_id=p.id LEFT JOIN scores s ON s.posting_id=p.id
        WHERE p.in_scope=1 AND p.active=1 AND a.status IN ({",".join("?" * len(ACTIVE))})
        ORDER BY best DESC, p.date_posted DESC""", ACTIVE).fetchall()
    chosen, skipped = [], []
    for r in rows:
        if len(chosen) >= n:
            break
        site = site_of(r["url"])
        if fetch.never_fetch(r["url"]):
            site = None
        if not refill and con.execute("SELECT 1 FROM fill_runs WHERE posting_id=? AND ts>=?", (r["id"], since)).fetchone():
            continue
        if site in sites:
            chosen.append((r, site))
        else:
            skipped.append((r, site or urlsplit(r["url"]).netloc))
            if include_unsupported:
                chosen.append((r, None))
    return chosen, skipped


# ------------------------------------------------------------------ asking Ali in the terminal
def make_ask(tab_label):
    def ask(q, topic):
        print(f"\n[{tab_label}] needs you ({topic}): {q['label'][:200]}")
        opts = q.get("options") or []
        for i, o in enumerate(opts, 1):
            print(f"   {i}. {o[:100]}")
        if topic in ("GPA", "salary"):
            print("   (this answer is NOT saved anywhere)")
        hint = "number(s) like 1 or 1,3" if opts else "your answer"
        try:
            v = input(f"   Type {hint}, or just press Enter to leave it for yourself in the browser: ").strip()
        except EOFError:
            v = ""
        if not v:
            return None
        if opts and re.fullmatch(r"[\d, ]+", v):
            picked = [opts[int(x) - 1] for x in re.findall(r"\d+", v) if 0 < int(x) <= len(opts)]
            return picked if q["kind"] == "multi" else (picked[0] if picked else None)
        return v
    return ask


def wait_for_ali(msg):
    print(f"\n>>> {msg}")
    try:
        v = input(">>> Press Enter when done, or type s to skip this role: ").strip().lower()
    except EOFError:
        v = "s"
    return v != "s"


# ------------------------------------------------------------------ one role
def fill_role(con, cfg, here, page, posting, site, dry_run=False):
    """Returns (answers, notes, cost). Never clicks submit (guard.py)."""
    notes, cost = [], 0.0
    url = form_url(site, posting["url"])
    if not url:
        return [], ["could not find the application form address; open it from the posting link"], 0.0
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    if site == "greenhouse":
        page.wait_for_selector("#first_name, #email", timeout=20000)

    # Sign-in pages and captchas: never typed through or solved (hard rule 5)
    for _ in range(3):
        b = browser.blocker(page)
        if not b:
            break
        if dry_run:
            return [], [f"page shows a {b}; skipped in dry run"], 0.0
        if b == "sign-in":
            msg = browser.try_google_sign_in(page, (rank_profile(here).get("email") or ""))
            if msg and "clicked" in msg and not browser.blocker(page):
                notes.append(msg)
                break
        if not wait_for_ali(f"#{posting['id']} {posting['company_name']}: the page shows a {b}. "
                            f"Please handle it yourself in that tab (ApplyMe never types passwords or solves captchas)."):
            return [], [f"skipped: {b}"], 0.0

    qs = forms.extract(page, site)
    if site == "greenhouse":  # options and required flags from the public API
        api = {q["key"]: q for q in greenhouse.questions(posting["url"])}
        for q in qs:
            a = api.get(q["key"])
            if a:
                q.update(label=a["label"] or q["label"], options=a["options"] or q["options"],
                         required=a["required"], description=a["description"])
            if q["kind"] == "combo" and not q["options"]:
                q["options"] = forms.read_combo_options(page, q["key"])

    # Files first (Lever reads the resume and fills fields; our answers then overwrite them)
    for q in [q for q in qs if q["kind"] == "file"]:
        L = q["label"].lower() + " " + q["key"].lower()
        if "cover" in L:
            if posting["letter_approved"] and posting["cover_letter_path"] and Path(posting["cover_letter_path"]).exists():
                if not dry_run:
                    forms.upload(page, q["key"], posting["cover_letter_path"])
                notes.append("attached your approved cover letter")
            elif q["required"]:
                notes.append(f"cover letter REQUIRED: run 'python applyme.py letter {posting['id']}', read it, then "
                             f"'python applyme.py approve {posting['id']}' and open this role again")
            else:
                notes.append(f"cover letter optional: none attached (letter {posting['id']} + approve {posting['id']} "
                             f"if you want one)")
        elif "resume" in L or "cv" in L or q["required"]:
            rp = resume_path(here)
            if not dry_run:
                forms.upload(page, q["key"], rp)
                page.wait_for_timeout(2500)
            print(f"   {'would upload' if dry_run else 'uploaded'} resume: {rp}")
            notes.append(f"attached resume (unchanged): {rp}")

    tab = f"#{posting['id']} {posting['company_name'][:20]}"
    ask = (lambda q, t: None) if dry_run else make_ask(tab)
    answers, cost = engine.plan(con, cfg, here, qs, posting, posting["fit_note"], ask)
    byq = {q["key"]: q for q in qs}
    for a in answers:
        if a["value"] in (None, "", []) or dry_run:
            continue
        problem = forms.fill(page, byq[a["key"]], a)
        if problem:
            a["note"] = f"{problem} (wanted: {browser._show(a['value'])})"
            a["value"], a["ask"] = None, True
    return answers, notes, cost


def resume_path(here):
    """The one file ever uploaded as the resume: the unchanged resume.pdf (hard rule 2)."""
    return (Path(here) / "resume.pdf").resolve()


def rank_profile(here):
    import rank
    return rank.load_profile(here)


# ------------------------------------------------------------------ the command
def open_roles(con, cfg, here, n, dry_run=False, refill=False, include_unsupported=False):
    chosen, skipped = pick_roles(con, cfg, n, refill or dry_run, include_unsupported)
    if skipped:
        print(f"Skipped {len(skipped)} higher-ranked role(s) on sites autofill doesn't support yet:")
        for r, s in skipped[:10]:
            print(f"  #{r['id']:<5} {r['company_name'][:24]:24} {s}")
    if not chosen:
        print("No roles to open.")
        return
    rp = resume_path(here)
    if not rp.exists():
        print(f"Resume not found: {rp}\nPut your resume there (named resume.pdf) and run this again.")
        return
    print(f"Resume that will be uploaded (unchanged): {rp}")
    from playwright.sync_api import sync_playwright
    pw = None
    if dry_run:
        pw = sync_playwright().start()
        ctx = pw.chromium.launch(channel="chrome", headless=True).new_context()
        print("Dry run: reading the forms in a hidden browser. Nothing is filled.\n")
    else:
        if browser.start(cfg):
            print("Started the ApplyMe Chrome window. (First time? Sign in to Google in it once; it remembers you.)")
        pw, ctx = browser.connect(cfg)
    total = 0.0
    reports = here / "reports"
    reports.mkdir(exist_ok=True)
    try:
        for posting, site in chosen:
            print(f"\n=== #{posting['id']} {posting['company_name']} - {posting['title']} ({site or 'unsupported'})")
            page = ctx.new_page()
            if not site:
                page.goto(posting["url"])
                print("   opened unfilled (site not supported yet)")
                continue
            try:
                answers, notes, cost = fill_role(con, cfg, here, page, posting, site, dry_run)
            except budget.BudgetExceeded as e:
                print(e)
                break
            except (GuardError, Exception) as e:
                print(f"   stopped: {budget.safe_error(e)}")
                continue
            total += cost
            if dry_run:
                for a in answers:
                    print(f"   {a['source']:16} {'GUESS ' if a['guess'] else ''}{'YOU ' if a['ask'] else ''}"
                          f"{a['label'][:60]!r} -> {browser._show(a['value'])[:60] if a['value'] else '-'}")
                for nt in notes:
                    print(f"   note: {nt}")
                continue
            browser.show_panel(page, posting, answers, notes)
            text = browser.summary_text(posting, answers, notes)
            path = reports / f"fill_{posting['id']}_{date.today()}.txt"
            path.write_text(text + "\n", encoding="utf-8")
            filled = sum(1 for a in answers if a["value"] not in (None, "", []))
            guesses = [a for a in answers if a["guess"] and a["value"] not in (None, "", [])]
            left = [a for a in answers if a["value"] in (None, "", []) and (a["ask"] or a.get("required"))]
            con.execute("INSERT INTO fill_runs(posting_id, ts, site, filled, guesses_json, left_json, cost_usd) "
                        "VALUES (?,?,?,?,?,?,?)", (posting["id"], db.now(), site, filled,
                                                   browser.dumps([a["label"] for a in guesses]),
                                                   browser.dumps([a["label"] for a in left]), cost))
            con.execute("UPDATE applications SET status='ready_to_review', updated_at=? "
                        "WHERE posting_id=? AND status IN ('new','saved')", (db.now(), posting["id"]))
            con.commit()
            print(f"   filled {filled}, {len(guesses)} guess(es) to double-check, {len(left)} left for you, "
                  f"${cost:.4f}. Saved: {path}")
    finally:
        if pw:
            pw.stop()  # disconnects only; the Chrome window and its tabs stay open for you
    print(f"\nAutofill cost this run: ${total:.4f}")
    print(budget.summary_text(con, cfg))
    if not dry_run:
        print("\nNothing was submitted. Review each tab, click Submit yourself, then run:"
              "\n  python applyme.py mark <#> applied")
