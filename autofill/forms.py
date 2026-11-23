"""Read questions from an application page and fill in answers. Every click goes through guard.safe_click.
Only fill / select / tick / upload / click-an-option are used. Enter is never pressed (it can submit a form).
"""
import re

from autofill import engine
from autofill.guard import GuardError, safe_click

# Where each site keeps one question (container) and its title.
SITES = {
    "greenhouse": {"container": ".field-wrapper, fieldset, .select", "title": "", "extra": ""},
    "lever": {"container": ".application-question, .application-additional", "title": ".application-label, .text",
              "extra": ""},
    "ashby": {"container": ".ashby-application-form-field-entry", "title": ".ashby-application-form-question-title",
              "extra": ".ashby-application-form-input-yesno"},
}

EXTRACT_JS = r"""
(cfg) => {
  const out = []; let n = 0; const done = new Set();
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const txt = e => e ? clean(e.innerText || e.textContent) : '';
  const vis = e => { if (e.type === 'file') return true; const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
                     return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const labelFor = e => {
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) return txt(l); }
    const l = e.closest('label'); if (l) return txt(l);
    if (e.getAttribute('aria-label')) return clean(e.getAttribute('aria-label'));
    const lb = e.getAttribute('aria-labelledby');
    if (lb) { const x = document.getElementById(lb.split(' ')[0]); if (x) return txt(x); }
    const nx = e.nextElementSibling; if (nx && nx.tagName === 'SPAN') return txt(nx);
    return '';
  };
  const box = e => (cfg.container && e.closest(cfg.container)) || e.closest('fieldset') || e.parentElement;
  const titleOf = (c, e) => {
    if (cfg.title) { const t = c.querySelector(cfg.title); if (t && txt(t)) return txt(t); }
    const lg = c.querySelector('legend'); if (lg && txt(lg)) return txt(lg);
    return labelFor(e);
  };
  const isReq = (c, e, title) => !!(e.required || e.getAttribute('aria-required') === 'true' || /[*✱]\s*$/.test(title)
      || (c && c.querySelector('.required, [class*="_required_"], [aria-required="true"]')));
  const strip = s => clean(s.replace(/[*✱]+\s*$/, ''));
  const tag = (els, key) => els.forEach(x => x.setAttribute('data-applyme-key', key));
  const desc = c => { const d = c && c.querySelector('.description, .help-text, [class*="description"]'); return d ? txt(d).slice(0, 400) : ''; };

  // Ashby yes/no button pairs
  if (cfg.extra) for (const yn of document.querySelectorAll(cfg.extra)) {
    const c = box(yn); const title = titleOf(c, yn); const key = 'yn' + (n++);
    tag([yn], key); yn.querySelectorAll('input').forEach(i => done.add(i));
    out.push({key, label: strip(title), kind: 'yesno', options: [...yn.querySelectorAll('button')].map(txt),
              required: isReq(c, yn, title), description: desc(c)});
  }
  for (const e of document.querySelectorAll('input, select, textarea')) {
    if (done.has(e)) continue;
    const t = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'search', 'image', 'reset'].includes(t)) continue;
    if (/captcha/i.test(e.name || '') || /captcha/i.test(e.id || '')) continue;
    if (e.closest('.ashby-application-form-autofill-pane, .iti__dropdown-content')) continue;  // skip "autofill from resume"
    if (e.getAttribute('aria-hidden') === 'true' || /requiredInput/.test(e.className)) continue;  // hidden helper inputs
    if (!vis(e)) continue;
    const c = box(e);
    let title = titleOf(c, e);
    const key = e.id || e.name || ('k' + (n++));
    if (t === 'radio' || t === 'checkbox') {
      const site = e.closest('fieldset') || (cfg.container && e.closest(cfg.container));
      const group = (t === 'checkbox' && site) ? [...site.querySelectorAll('input[type=checkbox]')]
                  : e.name ? [...document.querySelectorAll(`input[name="${CSS.escape(e.name)}"]`)]
                  : [...c.querySelectorAll(`input[type=${t}]`)];
      group.forEach(x => done.add(x));
      const gkey = 'g' + (n++); tag(group, gkey);
      const opts = group.map(labelFor);
      if (t === 'checkbox' && group.length === 1) {
        if (!title || title === opts[0]) title = opts[0];
        out.push({key: gkey, label: strip(title), kind: 'checkbox', options: [], required: isReq(c, e, title),
                  description: clean((opts[0] || '') + ' ' + desc(c)).slice(0, 400)});
      } else {
        out.push({key: gkey, label: strip(title), kind: t === 'radio' ? 'radio' : 'multi', options: opts,
                  required: isReq(c, e, title), description: desc(c)});
      }
      continue;
    }
    const own = labelFor(e) || clean(e.getAttribute('placeholder') || '');
    if (own && title && own !== title && !title.includes(own)) title = `${title} - ${own}`;  // e.g. "Education History - School"
    else if (!title) title = own;
    done.add(e); tag([e], key);
    let kind = 'text', opts = [];
    if (t === 'file') kind = 'file';
    else if (e.tagName === 'SELECT') { kind = 'select'; opts = [...e.options].filter(o => o.value !== '' && !/^select|^choose|^--/i.test(o.text.trim())).map(o => clean(o.text)); }
    else if (e.tagName === 'TEXTAREA') kind = 'textarea';
    else if (e.getAttribute('role') === 'combobox') kind = /^school--/.test(e.id) ? 'school' : /location/i.test(e.id) ? 'location' : 'combo';
    else if (e.id === 'location-input' || /location/i.test(e.className)) kind = 'location';
    out.push({key, label: strip(title), kind, options: opts, required: isReq(c, e, title), description: desc(c)});
  }
  return out;
}
"""


def extract(page, site):
    return page.evaluate(EXTRACT_JS, SITES[site])


def el(page, key):
    return page.locator(f'[data-applyme-key="{key}"]')


def _norm(s):
    return engine.norm(s)


# ------------------------------------------------------------------ dropdowns that are really search boxes
OPTION_SEL = ".select__option, [role=option]:visible"


def read_combo_options(page, key):
    """Open a Greenhouse searchable dropdown, read its options, close it with Escape (no typing, no Enter)."""
    box = el(page, key).first
    box.click()
    try:
        page.wait_for_selector(OPTION_SEL, timeout=3000)
    except Exception:
        page.keyboard.press("Escape")
        return []
    opts = [o.strip() for o in page.locator(OPTION_SEL).all_inner_texts()]
    page.keyboard.press("Escape")
    return opts


def choose_combo(page, key, value, search=None):
    """Type into a searchable dropdown to filter it, then click the matching option."""
    box = el(page, key).first
    box.click()
    box.press_sequentially((search or value)[:40], delay=15)
    try:
        page.wait_for_selector(OPTION_SEL, timeout=6000)
    except Exception:
        page.keyboard.press("Escape")
        return None
    page.wait_for_timeout(800)  # search results arrive in a moment
    texts = [t.strip() for t in page.locator(OPTION_SEL).all_inner_texts()]
    if not texts or (len(texts) == 1 and re.match(r"no (options|results)", texts[0], re.I)):
        page.keyboard.press("Escape")
        return None
    pick, exact = engine.pick_option(value, texts)
    idx = texts.index(pick)
    safe_click(page.locator(OPTION_SEL).nth(idx), "option", pick)
    return pick if exact else f"{pick} (closest)"


# ------------------------------------------------------------------ fill one answer
def fill(page, q, a):
    """Fill one answer. Returns a short problem string, or None if it worked."""
    v = a["value"]
    loc = el(page, q["key"])
    kind = q["kind"]
    try:
        if kind in ("text", "textarea"):
            loc.first.fill(str(v))
        elif kind == "select":
            loc.first.select_option(label=v)
        elif kind == "combo":
            got = choose_combo(page, q["key"], v)
            if not got:
                return "could not pick from the list"
            if "(closest)" in got:
                a["guess"], a["note"] = True, f"picked {got}"
        elif kind == "school":
            got = choose_combo(page, q["key"], v, search=v.split("-")[-1].strip() or v)
            if not got:
                return "school not found in the list"
            if "(closest)" in got:
                a["guess"], a["note"] = True, f"picked {got}"
        elif kind == "location":
            # Type "City, State" (suggestions need real typing) and accept ONLY a suggestion in the right state:
            # "Garden City" alone also matches Garden City, KS and Welwyn Garden City, England.
            city, _, state = (x.strip() for x in str(v).partition(","))
            loc.first.click()
            loc.first.fill("")
            loc.first.press_sequentially(f"{city}, {state}" if state else city, delay=40)
            sel = ".select__option, .dropdown-location, .pac-item, [role=option]:visible"
            try:
                page.wait_for_selector(sel, timeout=6000)
                page.wait_for_timeout(800)
            except Exception:
                return "typed the location; pick it from the list if one appears"
            abbr = {"michigan": "mi"}.get(_norm(state), "")
            items = page.locator(sel)
            for i in range(min(items.count(), 15)):
                t = items.nth(i).inner_text().strip()
                words = f" {_norm(t)} "
                if f" {_norm(city)} " in words and not re.search(rf"\w {_norm(city)}", _norm(t).split(",")[0]) \
                        and (not state or f" {_norm(state)} " in words or (abbr and f" {abbr} " in words)):
                    safe_click(items.nth(i), "option", t)
                    break
            else:
                page.keyboard.press("Escape")
                return f"no '{v}' suggestion found; pick your location from the list"
        elif kind == "radio":
            for i in range(loc.count()):
                r = loc.nth(i)
                if _norm(_label_of(r)) == _norm(v):
                    r.check()
                    break
            else:
                return "option not found"
        elif kind == "checkbox":
            if str(v).lower().startswith(("y", "true", "i ")) or v is True:
                loc.first.check()
        elif kind == "multi":
            wanted = {_norm(x) for x in (v if isinstance(v, list) else [v])}
            for i in range(loc.count()):
                c = loc.nth(i)
                if _norm(_label_of(c)) in wanted:
                    c.check()
        elif kind == "yesno":
            btn = loc.first.locator("button", has_text=re.compile(rf"^\s*{re.escape(v)}\s*$", re.I)).first
            safe_click(btn, "option", v)
    except GuardError as e:
        return f"guard: {e}"
    except Exception as e:
        return f"could not fill ({type(e).__name__})"
    return None


def _label_of(input_loc):
    return input_loc.evaluate("""e => { if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
        if (l) return l.innerText; } const l = e.closest('label'); if (l) return l.innerText;
        const s = e.nextElementSibling; return s ? s.innerText : ''; }""")


def upload(page, key, path):
    el(page, key).first.set_input_files(str(path))
