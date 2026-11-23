"""The ApplyMe Chrome window: start it, connect to it, spot sign-in pages and captchas, show the double-check panel.

Chrome (since version 136) refuses automation on your everyday profile, so ApplyMe uses its own profile folder.
You sign in to Google there once, by hand. Passwords are never typed or stored by this code (hard rule 5).
"""
import html
import json
import os
import re
import subprocess
import time
from pathlib import Path

import requests

from autofill.guard import GuardError, check, safe_click


def settings(cfg):
    a = cfg.get("autofill", {})
    return (a.get("chrome_path", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(os.path.expandvars(a.get("profile_dir", r"%LOCALAPPDATA%\ApplyMe\ChromeProfile"))),
            int(a.get("debug_port", 9222)))


def running(port):
    try:
        return requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2).ok
    except requests.RequestException:
        return False


def start(cfg):
    """Start Chrome with the ApplyMe profile (if it isn't running yet). Returns True if it was just started."""
    chrome, profile, port = settings(cfg)
    if running(port):
        return False
    if not Path(chrome).exists():
        raise SystemExit(f"Chrome not found at {chrome}. Fix autofill.chrome_path in config.yaml.")
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([chrome, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                      "--no-first-run", "--no-default-browser-check", "about:blank"])
    for _ in range(30):
        if running(port):
            return True
        time.sleep(0.5)
    raise SystemExit("Chrome started but ApplyMe could not connect to it. Close all ApplyMe Chrome windows and retry.")


def connect(cfg):
    """Connect Playwright to the running ApplyMe Chrome. Returns (playwright, context)."""
    from playwright.sync_api import sync_playwright
    _, _, port = settings(cfg)
    pw = sync_playwright().start()
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = b.contexts[0] if b.contexts else b.new_context()
    return pw, ctx


# ------------------------------------------------------------------ sign-in pages and captchas
BLOCKER_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 30 && r.height > 30 && getComputedStyle(e).visibility !== 'hidden'; };
  if ([...document.querySelectorAll('input[type=password]')].some(vis)) return 'sign-in';
  const big = e => { const r = e.getBoundingClientRect(); return r.width > 200 && r.height > 200 && getComputedStyle(e).visibility !== 'hidden'; };
  for (const f of document.querySelectorAll('iframe')) {
    const s = (f.src || '') + ' ' + (f.title || '');
    if (/recaptcha\/(api2|enterprise)\/bframe/i.test(s) && big(f)) return 'captcha';          // picture challenge open
    if (/recaptcha\/(api2|enterprise)\/anchor/i.test(s) && !/size=invisible/i.test(s) && vis(f)) return 'captcha';  // "I'm not a robot" box
    if (/hcaptcha\.com/i.test(s) && /challenge|checkbox/i.test(s) && vis(f)) return 'captcha';
  }
  if (/just a moment|attention required|verify you are human/i.test(document.title)) return 'captcha';
  return null;
}
"""


def blocker(page):
    try:
        return page.evaluate(BLOCKER_JS)
    except Exception:
        return None


def try_google_sign_in(page, email):
    """Click 'Continue/Sign in with Google' if the page offers it, then pick Ali's account in the chooser.
    Never types anything. Returns a message for the terminal, or None if there was no Google button."""
    btn = page.get_by_role("button", name=re.compile(r"(continue|sign in|log ?in) with google", re.I))
    if not btn.count():
        return None
    try:
        with page.context.expect_page(timeout=8000) as popup_info:
            safe_click(btn.first, "nav")
        g = popup_info.value
    except GuardError as e:
        return str(e)
    except Exception:
        g = page  # same-tab redirect
    g.wait_for_load_state("domcontentloaded")
    acct = g.locator(f'[data-identifier="{email}"], [data-email="{email}"]')
    if acct.count():
        check(email, "option", email)
        acct.first.click()
        g.wait_for_timeout(2500)
    if blocker(g) or ("accounts.google.com" in g.url and g.locator("input[type=password], input[type=tel]").count()):
        return "Google wants a password or a verification step: please finish signing in yourself"
    return "clicked Continue with Google"


# ------------------------------------------------------------------ the double-check panel
PANEL_JS = r"""
(h) => {
  const old = document.getElementById('applyme-panel'); if (old) old.remove();
  const d = document.createElement('div'); d.id = 'applyme-panel';
  d.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;width:360px;max-height:70vh;overflow:auto;' +
    'background:#fffbe6;border:2px solid #d4a400;border-radius:8px;padding:10px 12px;font:13px/1.4 Segoe UI,Arial;' +
    'color:#222;box-shadow:0 4px 16px rgba(0,0,0,.25)';
  d.innerHTML = h;
  const b = document.createElement('button'); b.type = 'button'; b.textContent = 'Hide';
  b.style.cssText = 'float:right;margin-left:8px'; b.onclick = () => d.remove();
  d.prepend(b); document.body.appendChild(d);
}
"""


def panel_html(posting, answers, notes):
    esc = html.escape
    guesses = [a for a in answers if a["guess"] and a["value"] not in (None, "", [])]
    left = [a for a in answers if a["value"] in (None, "", []) and (a["ask"] or a.get("required"))]
    parts = [f"<b>ApplyMe</b> #{posting['id']}: filled for you. <b>Review everything, then click Submit "
             f"yourself.</b><br><small>Afterwards run: python applyme.py mark {posting['id']} applied</small>"]
    if guesses:
        parts.append("<h4 style='margin:8px 0 4px'>Double-check these guesses</h4><ul style='margin:0;padding-left:18px'>")
        parts += [f"<li><b>{esc(a['label'][:90])}</b><br>&rarr; {esc(_show(a['value']))}"
                  f"<br><small>{esc(a['note'] or a['source'])}</small></li>" for a in guesses]
        parts.append("</ul>")
    if left:
        parts.append("<h4 style='margin:8px 0 4px'>Left for you</h4><ul style='margin:0;padding-left:18px'>")
        parts += [f"<li>{esc(a['label'][:90])}<br><small>{esc(a['note'])}</small></li>" for a in left]
        parts.append("</ul>")
    if notes:
        parts.append("<h4 style='margin:8px 0 4px'>Notes</h4><ul style='margin:0;padding-left:18px'>")
        parts += [f"<li>{esc(n)}</li>" for n in notes]
        parts.append("</ul>")
    return "".join(parts)


def _show(v):
    return ", ".join(v) if isinstance(v, list) else str(v)[:160]


def show_panel(page, posting, answers, notes):
    try:
        page.evaluate(PANEL_JS, panel_html(posting, answers, notes))
    except Exception:
        pass


def summary_text(posting, answers, notes):
    lines = [f"#{posting['id']} {posting['company_name']} - {posting['title']}", f"URL: {posting['url']}"]
    lines.append("Double-check these guesses:")
    lines += [f"  - {a['label'][:100]} -> {_show(a['value'])}  ({a['note'] or a['source']})"
              for a in answers if a["guess"] and a["value"] not in (None, "", [])] or ["  (none)"]
    lines.append("Left for you:")
    lines += [f"  - {a['label'][:100]}  ({a['note']})" for a in answers
              if a["value"] in (None, "", []) and (a["ask"] or a.get("required"))] or ["  (none)"]
    lines.append("Notes:")
    lines += [f"  - {n}" for n in notes] or ["  (none)"]
    return "\n".join(lines)


def dumps(x):
    return json.dumps(x, ensure_ascii=False)
