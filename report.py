"""Write the daily HTML list and tracker.csv from the database."""
import csv
import html
import json
from datetime import date
from pathlib import Path

import budget
from rank import merge_flags

APPLIED_LIKE = ("applied", "interview", "offer", "rejected")

QUERY = """
SELECT p.*, a.status, a.applied_date, a.cover_letter_path, a.notes,
       s.score AS ai_score, s.reason AS ai_reason, s.fit_note AS ai_fit_note, s.flags_json AS ai_flags,
       (SELECT COUNT(*) FROM postings p2 JOIN applications a2 ON a2.posting_id=p2.id
         WHERE p2.company_id=p.company_id AND a2.status IN ('applied','interview','offer','rejected')) AS applied_here
FROM postings p LEFT JOIN applications a ON a.posting_id=p.id LEFT JOIN scores s ON s.posting_id=p.id
"""


def best_score(r):
    """Claude's score if the role was scored, otherwise the free rule score."""
    return r["ai_score"] if r["ai_score"] is not None else (r["rule_score"] or 0)


def sort_key(r):
    tier = 0 if r["is_detroit_metro"] else 1 if r["is_michigan"] else 2 if r["is_remote"] else 3
    return (tier, r["ai_score"] is None, -best_score(r))


def all_flags(r):
    rule = json.loads(r["flags"] or "[]")
    ai = json.loads(r["ai_flags"] or "{}").get("flags", [])
    return merge_flags(rule, ai)


def load(con, where, params=()):
    rows = con.execute(QUERY + " WHERE " + where + " ORDER BY p.date_posted DESC", params).fetchall()
    return sorted(rows, key=sort_key)  # stable sort, so newest stays first within each group


def esc(s):
    return html.escape(str(s or ""))


def badge(flag):
    cls = "red" if flag.startswith("GPA") and "above" in flag else \
          "amber" if any(w in flag for w in ("citizenship", "clearance", "co-op", "unclear", "check major",
                                              "started", "not stated")) else "blue"
    return f'<span class="b {cls}">{esc(flag)}</span>'


def term_text(r):
    if not r["term"] and not r["start_date"]:
        return "?"
    dates = f'{r["start_date"][:7]} to {r["end_date"][:7]}' if r["start_date"] else ""
    weeks = f' (~{r["length_weeks"]} wk)' if r["length_weeks"] else ""
    return f'{esc(r["term"])}<br><small>{dates}{weeks}</small>'


def score_cell(r):
    if r["ai_score"] is not None:
        return f'<b class="score">{r["ai_score"]}</b>'
    return f'<span class="rules" title="free rule score; not scored by Claude yet">{r["rule_score"] or 0}<br><small>rules</small></span>'


def notes_cell(r):
    if not r["ai_reason"]:
        return ""
    return (f'<div class="why">{esc(r["ai_reason"])}</div>'
            f'<details><summary>"Why I fit" draft (edit before using)</summary>{esc(r["ai_fit_note"])}</details>')


def spend_box(con, cfg):
    s = budget.summary(con, cfg)
    parts = ", ".join(f"{p} ${u:.4f} ({n} calls{f', {w} searches' if w else ''})" for p, n, u, w in s["by_purpose"])
    return (f'<div class="sum"><b>API spend: ${s["spent"]:.4f}</b> of the ${s["stop"]:.2f} hard stop '
            f'(${s["total"]:.2f} budget){": " + parts if parts else ""}. '
            f'<small>Worked out from list prices; the Anthropic Console shows the real bill.</small></div>')


def table(rows, warn_at):
    if not rows:
        return "<p><i>Nothing here.</i></p>"
    out = ["<table><tr><th>#</th><th>Score</th><th>Company</th><th>Role</th><th>Where</th><th>Term</th>"
           "<th>Flags</th><th>Status</th><th>Posted</th></tr>"]
    for r in rows:
        flags = all_flags(r)
        n = r["applied_here"] or 0
        applied = f'<br><small class="{"warn" if n >= warn_at else ""}">applied to {n} role(s) here</small>' if n else ""
        where = f'{esc(r["city"])}{", " if r["city"] and r["state"] else ""}{esc(r["state"])}'
        out.append(
            f'<tr><td>{r["id"]}</td><td>{score_cell(r)}</td><td>{esc(r["company_name"])}{applied}</td>'
            f'<td><a href="{esc(r["url"])}" target="_blank">{esc(r["title"])}</a><br><small>{esc(r["job_type"])}'
            f' · P{r["role_priority"] or "?"}</small>{notes_cell(r)}</td>'
            f'<td>{where}<br><small>{esc(r["location_reason"])}</small></td><td>{term_text(r)}</td>'
            f'<td>{"".join(badge(f) for f in flags)}</td><td>{esc(r["status"])}</td><td>{esc(r["date_posted"])}</td></tr>')
    out.append("</table>")
    return "\n".join(out)


STYLE = """body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#222;background:#fafafa}
table{border-collapse:collapse;width:100%;background:#fff;font-size:14px}
th,td{border:1px solid #ddd;padding:6px 8px;vertical-align:top;text-align:left}th{background:#f0f0f0}
small{color:#666}.b{display:inline-block;padding:1px 6px;margin:1px;border-radius:8px;font-size:12px}
.red{background:#fdd;color:#900;font-weight:bold}.amber{background:#fff1cc;color:#7a5200}.blue{background:#e3efff;color:#134}
.warn{color:#b00;font-weight:bold}.score{font-size:18px}.rules{color:#888}
.why{font-size:13px;color:#333;margin-top:4px}details{font-size:13px;color:#225}.sum{background:#fff;border:1px solid #ddd;padding:10px 14px;margin-bottom:16px}"""


def write_html(con, cfg, run, summary, folder="reports"):
    Path(folder).mkdir(exist_ok=True)
    limit = cfg.get("limits", {}).get("max_new_postings_in_report", 30)
    warn_at = cfg.get("tracking", {}).get("warn_if_roles_applied_at_company_at_least", 2)
    new = load(con, "p.in_scope=1 AND p.first_seen>=?", (run["started"],)) if run else []
    all_rows = load(con, "p.in_scope=1")
    scope = cfg.get("location", {}).get("scope", "michigan")
    lines = "".join(f"<li>{esc(k)}: {esc(v)}</li>" for k, v in summary.items())
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>ApplyMe {date.today()}</title>
<style>{STYLE}</style></head><body>
<h1>ApplyMe: internships and co-ops ({date.today():%b %d, %Y})</h1>
{spend_box(con, cfg)}
<div class="sum"><b>Location scope:</b> {esc(scope)} · sorted Detroit metro → rest of Michigan{" → remote" if cfg.get("location", {}).get("treat_remote_as_local") else ""}, then
then by score (Claude's 0-100 score in bold; grey "rules" = free rule score, not yet scored by Claude).
P1 = software, P2 = other tech, P3 = automotive, P4 = other engineering or vague title.
<ul>{lines}</ul>To mark a role: <code>python applyme.py mark &lt;#&gt; applied</code> (or saved / skipped / interview /
rejected / offer). Cover letter draft: <code>python applyme.py letter &lt;#&gt;</code>.
GPA flags are never hidden, just shown in red when the minimum is above gpa.flag_above in config.yaml.</div>
<h2>New since last run ({len(new)}{f", showing first {limit}" if len(new) > limit else ""})</h2>
{table(new[:limit], warn_at)}
<h2>All open roles in scope ({len(all_rows)})</h2>
{table(all_rows, warn_at)}
</body></html>"""
    path = Path(folder) / f"new_postings_{date.today():%Y-%m-%d}.html"
    path.write_text(body, encoding="utf-8")
    return path, len(new), len(all_rows)


def write_csv(con, path="tracker.csv"):
    rows = load(con, "(p.in_scope=1) OR (a.status IS NOT NULL AND a.status<>'new')")
    cols = ["id", "status", "applied_date", "score", "company_name", "title", "city", "state", "term", "start_date",
            "end_date", "job_type", "role_priority", "flags", "reason", "fit_note", "url", "cover_letter_path",
            "notes", "date_posted", "first_seen", "active"]
    special = {"flags": lambda r: "; ".join(all_flags(r)), "score": best_score,
               "reason": lambda r: r["ai_reason"] or "", "fit_note": lambda r: r["ai_fit_note"] or ""}
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig so Excel shows accents properly
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([special[c](r) if c in special else r[c] for c in cols])
    return path, len(rows)
