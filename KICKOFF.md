# ApplyMe: kickoff instructions for Claude Code

You are building ApplyMe for Ali, a beginner programmer on a Windows gaming laptop. This file is your task for the FIRST session only.

## Before anything else
1. Read CLAUDE.md, profile.yaml, config.yaml and companies.txt in this folder. CLAUDE.md contains hard rules (never submit applications, never reword his resume, never enter GPA, secrets only in .env). They override anything else.
2. Work in plan mode. Show Ali a short plain-language plan and wait for approval before writing code.
3. Keep usage low. Ali is on a Claude Pro plan with a weekly limit. Prefer simple code, standard library and a few well-known packages. No big frameworks.

## Session 1 scope: Phase 1 only (Ingest + tracker tables)
- Check what is installed (Python, pip, Git) and tell Ali exactly what to install if anything is missing, step by step.
- Create the SQLite database (applyme.db) with tables: companies, postings, applications. One row per role; postings unique by URL/ID. See "Application tracking" in CLAUDE.md for statuses.
- Pull internship and co-op postings for any term (summer 2027 is the favorite) from public sources: SimplifyJobs and Pitt CSC GitHub lists first; then the public job endpoints (Greenhouse, Lever, Ashby) for companies in companies.txt. Verify each company works and report which ones failed.
- Dedupe, filter out postings that clearly do not match config.yaml, and keep Michigan (and remote) roles only per location.scope in config.yaml, but still store every posting with its location; flag (do not hide) GPA minimums, citizenship/clearance requirements and housing/relocation mentions.
- Write a simple daily command, for example `python applyme.py ingest`, and a plain-text or HTML list of the new postings (with term dates and any GPA flag, never hidden) so Ali can look at real results.
- No LLM calls and no API key in this phase.

## Stop and report
When Phase 1 works, stop. Tell Ali in plain language: how many postings were found, how many sources worked, what broke, how to run it again, and how much of his usage you think the next phase (ranking) will take. Do not start Phase 2 until he says so.

## Discord (do not build yet)
Only note where it will plug in. Ali will later create a private #applyme channel and webhook (DISCORD_WEBHOOK_URL in .env). Do not ask him for a bot token yet. Never ask him to paste secrets into the chat; secrets go only in the .env file.

## Things to ask Ali (only when you need them)
- Missing installs or permission prompts on his laptop.
- Any posting source that needs a login (skip it; do not scrape behind logins).
