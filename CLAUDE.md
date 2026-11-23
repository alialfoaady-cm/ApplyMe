# ApplyMe: Internship Finder & Application Prep Tool

Owner: Ali Alfoaady. Runs locally on his Windows gaming laptop. Ali is a beginner programmer: explain in plain language, give exact commands, keep setup simple, prefer few dependencies. All personal data lives in `profile.yaml`; all settings in `config.yaml`. Read both before doing anything.

## HARD RULES (never break)
1. NEVER submit an application. Stop at the final review screen. Ali clicks submit. Autofill only clicks allowed buttons (Apply, Apply Manually, Next, Continue, Save and Continue, Add, Upload, Continue/Sign in with Google, and the answer option it chose), never Submit/Send/Finish/Confirm, never presses Enter in a field, and stops before the final submit. Status may become ready_to_review, never applied.
2. NEVER reword, rewrite or reformat text Ali wrote on his resume. Resume must stay ONE page with identical formatting. The resume uploaded is always the unchanged resume.pdf.
3. Never invent facts about Ali. Form answers come from profile.yaml -> form_answers -> answer cache -> Claude (facts from profile/resume only). For required fields with no exact match choose the closest option and list it in the "Double-check these guesses" box. Never guess salary, criminal history, non-competes, legal attestations/signatures/certifications/"I agree" boxes, or skills/experience not shown in profile/resume: pause and ask Ali. For obscure demographic questions choose the closest answer or "decline to answer". Consent rules (profile.yaml form_answers): data/privacy/processing consent = Yes, AI-notetaker/recording consent = Yes, texts/SMS about the application = Yes, marketing consent/messages = No; any box that makes Ali confirm a fact about his eligibility or legal status ("I certify", "true and accurate", authorization, citizenship) still pauses. "Why this company" answers: research the company (cached, shared with cover letters), match 1-2 real, sourced company projects/products to a real project and tools from resume/profile (under ~80 words: "I really like X; I built something similar (Y) using Z; I would like to continue this at C"), never claim tools or experience he lacks, never paste the resume, sources in a notes file, always shown in the double-check box; if no honest match, say so and write a general honest answer. Start dates: May 2027 or the earliest 2027 option; never a date before 2027 or before today.
4. GPA is never entered anywhere. If a field is required, pause and ask him. Ali's GPA and salary answers are never cached or stored. Do NOT skip or hide postings because of a GPA minimum: include and rank them normally, just show a "GPA minimum" flag so Ali knows.
5. Never type, ask for or store passwords. Sign-in happens only by clicking "Continue/Sign in with Google" in the ApplyMe Chrome profile, or by Ali himself. Never bypass or solve captchas or 2-step verification: pause and tell Ali.
6. Use public APIs and public job feeds. Do not scrape Jobright or anything behind a login. Autofill may fill Ali's own application forms in his ApplyMe browser, but never collects data from logged-in pages. Never download whole job lists from Workday or Oracle (single public job pages only).
7. Keep secrets (API keys) in `.env` only, never in code, never committed.
8. Cover letters are uploaded only after Ali approves the draft (`python applyme.py approve <#>`).

## Resume tailoring policy (cost-gated)
Default: every application uses `resume.pdf` unchanged. Tailoring is OPTIONAL and only happens when ALL are true:
- fit score >= `tailoring.min_fit_score` in config.yaml
- `resume_source.docx` exists (without it, tailoring is skipped entirely)
- weekly tailoring budget in config.yaml is not used up
- the change is selection/reordering of Ali's EXISTING text only (e.g., order of skills or sections). No new wording, no deleted facts, still one page, same look.
- Ali approves a plain-language description of the change BEFORE a file is generated.
Cheaper default for every posting: write a 2-3 sentence "why I fit" draft note (using only real facts from profile/resume) that Ali can paste into "why this company" fields. Drafts are for his review.
If tailoring would cost more than it is worth for a posting, say so and use the base resume.

## Cover letters (always write one when the application makes it optional)
- If a cover letter is required or optional, write one. If the form has no cover letter field at all, do not.
- For each letter, FIRST research the company with web search: what they build, notable projects/products, recent news, mission, and the specific team/role if known. Pick the single best match between that and Ali's real life or projects (SixSense, Hack Dearborn 3rd place, his 5 years restoring cars incl. the Grand Highlander and M340i, his embedded/Python/C++ work, Arabic/Farsi, store operations lead experience) and tie it back clearly.
- Only use verifiable company facts from sources found in research. Never invent projects, quotes or connections. Never claim experience Ali doesn't have. Put the source links in a separate "research notes" file next to each letter, not in the letter.
- Length: under one page (about 200-300 words), plain and human, no clichés, addressed to the named recruiter if known, otherwise "Hiring Team".
- Save as plain text + PDF in `cover_letters/<company>_<role>.pdf`. These are DRAFTS: Ali reads and approves before anything is uploaded.
- Never reproduce text from the company's website or other sources; paraphrase.
- Cost control: do research once per company and cache it in SQLite so multiple roles at the same company reuse it.

## Location rules
- Michigan only, in-person (for now). Prioritize the Detroit metro area (commutable from Garden City), then the rest of Michigan. Ali prefers in-person work.
- Every role located outside Michigan (hybrid or on-site) AND every fully remote role is EXCLUDED from reports, ranking, cover letters and any LLM calls, so it costs no usage. Remote roles are still stored (reason "remote (you prefer in-person)"); setting `location.treat_remote_as_local: true` in config.yaml brings them back instantly. A Michigan role that also allows remote stays in scope.
- Forms asking for a work arrangement preference get "In-person" (profile.yaml form_answers).
- Still store city/state for EVERY posting in the database, including excluded ones, with a reason. Do not delete them at ingest.
- Location scope is controlled by `location.scope` in config.yaml: `michigan` (current), `michigan_plus_relocation` (also other states that mention housing/relocation/stipend), `all`. Changing it must work instantly with no re-scraping.
- If a posting's location is unclear, keep it and flag "location unclear".

## Application tracking (source of truth = local SQLite; Discord = notifications + buttons)
- One row per ROLE (posting), never per company. Companies can have many roles. Tables: companies, postings (unique by posting URL/ID), applications.
- Statuses: new, saved, ready_to_review, applied, interview, rejected, offer, skipped. Store applied date, cover letter path, notes.
- Show "applied to N roles at this company" next to each posting and warn when a company limits simultaneous applications or when Ali already applied to a similar role there.
- NEVER mark a role "applied" automatically. Only Ali does, via Discord button, a command, or the report page.
- Also export tracker.csv each day so Ali can open it in Excel/Google Sheets.
- Discord (Ali already has a server and a clipping bot; keep ApplyMe separate): Step 1 use a webhook posting to a private #applyme channel (new matches, daily summary, links to cover letter drafts). Step 2 (optional) a SEPARATE small bot with buttons Applied / Skip / Interview / Rejected that update the database. Ask Ali for the channel/webhook; keys go in .env only.
- Keep the Discord server private. Post job info and links only. Never post his phone, address or the resume contents.

## Pipeline (one phase per session; stop so Ali can test)
Phase 1 - Ingest: public sources into SQLite: SimplifyJobs and Pitt CSC GitHub internship lists, Greenhouse/Lever/Ashby public job endpoints for companies in companies.txt (verify each works, drop ones that don't), optionally JobSpy. Dedupe. Keep internships AND co-ops for ANY term (summer, fall, spring, year-round), not just summer. Record the term, start/end dates and length, and flag co-ops or terms longer than a normal summer since they may conflict with classes (Ali decides).
Phase 2 - Rank: free rules/filters first; Claude only for top candidates (score + 2-sentence reason + fit note). Flags: GPA minimum, citizenship/clearance, housing/relocation mention, early-career (freshman/sophomore) program.
Phase 3 - Daily report: local HTML page of ranked jobs, apply links, status (new / reviewing / applied / skipped).
Phase 3b - Tracker + Discord: build the tracker tables early (Phase 1) and the Discord webhook feed here; buttons/bot later.
Phase 4 - Autofill: our own Playwright autofill in a separate "ApplyMe" Chrome profile (Chrome blocks automating the default profile). 4a Greenhouse/Lever/Ashby, 4b Workday. `python applyme.py open N` fills the top N unapplied roles and stops before submit.
Phase 5 - Cover letters per policy above (build this before tailoring; it is the higher-value feature).
Phase 6 (optional) - Resume tailoring per policy above.

## Cost discipline
Rules before any LLM call; batch calls; cache everything in SQLite; small model for scoring; plan mode before each phase; one phase per session.
