"""The submit guard. Every click the autofill makes goes through safe_click(), which refuses anything that could
submit an application. Hard rule 1: Ali clicks Submit himself.

Two kinds of clicks are allowed:
  nav    - moving through a form: only button texts on NAV_ALLOWED
  option - picking an answer (a Yes/No button, a dropdown item): only the exact option text we chose
Anything whose text matches DENIED is refused in both cases, whatever the button type.
The autofill never presses Enter in a field (Enter can submit a form); it only fills, selects, ticks and uploads.
"""
import re

DENIED = re.compile(r"\b(submit|submitting|send|finish|finalize|complete|confirm|done|sign and)\b", re.I)
NAV_ALLOWED = {"apply", "apply now", "apply for this job", "apply manually", "next", "continue", "save and continue",
               "add", "add another", "upload", "continue with google", "sign in with google", "log in with google",
               "login with google"}


class GuardError(Exception):
    pass


def _norm(text):
    return " ".join((text or "").replace("→", " ").split()).strip(" .:*>").lower()


def check(text, purpose, chosen=None):
    """Raise GuardError if this click is not allowed. Pure function (tested in tests/test_guard.py)."""
    t = _norm(text)
    if DENIED.search(t):
        raise GuardError(f'refused to click "{text.strip()}" (could submit the application)')
    if purpose == "nav":
        if t not in NAV_ALLOWED:
            raise GuardError(f'refused to click "{text.strip()}" (not on the allowed list)')
    elif purpose == "option":
        if chosen is None or t != _norm(chosen):
            raise GuardError(f'refused to click "{text.strip()}" (not the chosen answer "{chosen}")')
    else:
        raise GuardError(f"unknown click purpose {purpose!r}")


def element_text(locator):
    for get in (lambda: locator.inner_text(timeout=2000), lambda: locator.get_attribute("aria-label"),
                lambda: locator.get_attribute("value")):
        try:
            t = get()
        except Exception:
            t = None
        if t and t.strip():
            return t
    return ""


def safe_click(locator, purpose, chosen=None):
    check(element_text(locator), purpose, chosen)
    locator.click()
