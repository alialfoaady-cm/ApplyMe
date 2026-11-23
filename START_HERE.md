# ApplyMe: start here

Files in this folder:
- CLAUDE.md : the rules and plan Claude Code reads every session
- profile.yaml : your info and form answers (edit anything wrong)
- config.yaml : what roles you want and cost limits
- companies.txt : starter company list
- cover_letters/ : will be created by the tool; drafts for you to review
- resume.pdf : your final resume (do not edit)
- .env.example : copy to .env later when you have an API key

Kickoff prompt to paste into Claude Code (in plan mode):

"Read CLAUDE.md, profile.yaml and config.yaml. Plan Phase 1 only (ingest internship and co-op postings, any term into a local SQLite database). Use as little usage as possible, keep setup simple for a beginner on Windows, and show me the plan before writing code."

## Phase 2: scoring, cover letters, adding your own finds
One-time setup: run `pip install -r requirements.txt`, then put your key in `.env` as `ANTHROPIC_API_KEY=sk-ant-...`. Never share that file.

- `python applyme.py ingest` now also scores the top 40 in-scope roles with Claude (cheapest model, about $0.0006 each). It only scores new or changed roles, so a normal day costs about 1 cent. Add `--no-ai` to skip scoring.
- `python applyme.py rank` re-runs scoring by itself. `--dry-run` is free: it only does the rules and downloads job pages.
- `python applyme.py letter 123` writes a cover-letter DRAFT for posting #123, after asking whether the form has a cover letter field.
  - It costs about 5-7 cents for the first letter at a company and about 2 cents for each later role there, because the research is reused.
  - The files go to `cover_letters/`: a .txt, a .pdf and a research-notes file with the source links. Read and edit every draft yourself.
- `python applyme.py add <url>` adds a posting you found yourself.
  - LinkedIn, Indeed and Handshake pages are never downloaded; it asks you for the details instead.
  - When it asks for the description, paste it and finish with a line containing only END.
- `python applyme.py spend` shows how much of the budget is used.
  - Every call is logged, and calls are refused before sending if they could take you past $4.00. That limit is set by `budget.hard_stop_usd` in config.yaml.
  - The top of the report shows the same total.

## Phase 4: autofill (Greenhouse, Lever, Ashby for now; Workday comes next)
**First time only:**
1. `pip install -r requirements.txt`
2. `python applyme.py browser` opens a separate "ApplyMe" Chrome window. Chrome no longer lets programs control your normal profile.
3. Sign in to Google in that window yourself. ApplyMe never types or stores passwords.

**Every time:**
- `python applyme.py open 5 --dry-run` is optional. It shows what it would answer and fills nothing.
- `python applyme.py open 5` fills your top 5 unapplied roles, each in its own tab, and **stops before Submit**.
  - The terminal asks you about anything it must not guess: salary, criminal history, non-competes, "I agree / I certify" boxes, GPA, and skills not on your resume. Press Enter to leave a question for yourself in the browser.
  - Each tab shows a yellow **"Double-check these guesses"** box. The same list is saved in `reports\fill_<#>_<date>.txt`.
  - The terminal prints the exact resume file it uploads (always your unchanged `resume.pdf`).
  - "Why do you want to work here?" gets a short researched answer: one of their real projects, plus your similar project and the tools you used.
    - It's always in the double-check box, with sources in `cover_letters\why_<company>_<role>_notes.txt`.
    - It costs about 4 cents the first time at a company and about 0.1 cent after that (the research is shared with cover letters).
  - Consent boxes follow the rules in profile.yaml:
    - privacy/data, AI notetaker and texts about your application = Yes
    - marketing = No
    - anything asking you to certify a fact about yourself still waits for you.
- Read each tab, fix anything, click **Submit yourself**, then run `python applyme.py mark <#> applied`.

**Other commands:**
- `python applyme.py approve <#>` lets autofill attach that role's cover letter, after you've read it.
- `python applyme.py answers` lists saved answers. `python applyme.py answers fix <id> "new answer"` corrects one.

Close the ApplyMe Chrome window when you're done.

## Daily use (Phase 1)
Open PowerShell and run:

    cd C:\Users\saamt\Documents\ApplyMe
    python applyme.py ingest

It downloads new postings (about 30 seconds), opens today's list in your browser and updates tracker.csv.
Other commands:
- `python applyme.py report` : rebuild the list without downloading (use it after editing config.yaml)
- `python applyme.py mark 123 applied` : set the status of posting #123 (saved, skipped, applied, interview, rejected, offer)
- `python applyme.py verify` : re-check which companies in companies.txt have a public job feed
