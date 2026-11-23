"""API budget: prices, the .env key loader, a hard stop before every call, and a log of every call's cost.

The API key is only ever read into the environment for the Anthropic client. It is never printed,
logged, stored in the database or put in an error message.
"""
import os
from pathlib import Path

import db

HERE = Path(__file__).resolve().parent

# US$ per million tokens (Anthropic list prices). Haiku 5.5 costs more for prompts over 100K tokens.
PRICES = {
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "long_in": 0.50, "long_out": 2.50, "long_after": 100_000},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00},
}
WEB_SEARCH_USD = 0.01  # $10 per 1,000 searches


class BudgetExceeded(Exception):
    pass


class NoApiKey(Exception):
    pass


def load_env():
    """Read KEY=value lines from .env into os.environ (without overwriting). Prints nothing."""
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


def client():
    """An Anthropic client, or NoApiKey with a safe message (the key itself is never shown)."""
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise NoApiKey("No API key found. Put ANTHROPIC_API_KEY=... in the .env file (see START_HERE.md).")
    import anthropic
    return anthropic.Anthropic(max_retries=2, timeout=120)


def price(model):
    if model not in PRICES:
        raise ValueError(f"No price known for model '{model}'. Add it to PRICES in budget.py first.")
    return PRICES[model]


def cost(model, input_tokens, output_tokens, web_searches=0):
    p = price(model)
    long = "long_after" in p and input_tokens > p["long_after"]
    rin, rout = (p["long_in"], p["long_out"]) if long else (p["in"], p["out"])
    return input_tokens * rin / 1e6 + output_tokens * rout / 1e6 + web_searches * WEB_SEARCH_USD


def limits(cfg):
    b = cfg.get("budget", {})
    return float(b.get("total_usd", 5.0)), float(b.get("hard_stop_usd", 4.0))


def spent(con):
    return con.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM api_calls").fetchone()[0]


def estimate_tokens(text):
    # Rough and on the high side: about 3 characters per token.
    return len(text) // 3 + 50


def check(con, cfg, model, prompt_chars, max_tokens, max_searches=0, search_tokens=0):
    """Refuse BEFORE sending if the worst case would pass the hard stop. Returns the worst-case $."""
    worst = cost(model, prompt_chars // 3 + 50 + search_tokens, max_tokens, max_searches)
    total, stop = limits(cfg)
    already = spent(con)
    if already + worst > stop:
        raise BudgetExceeded(
            f"Budget stop: ${already:.4f} spent so far, this call could cost up to ${worst:.4f}, "
            f"and the hard stop is ${stop:.2f} (of your ${total:.2f} budget). Nothing was sent. "
            f"Raise budget.hard_stop_usd in config.yaml only if you mean to.")
    return worst


def log(con, purpose, model, response=None, posting_id=None, company_id=None, ok=True, note=None):
    """Record one call. response = the Anthropic Message (or None if the call failed before a reply)."""
    u = getattr(response, "usage", None)
    inp = getattr(u, "input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    searches = getattr(stu, "web_search_requests", 0) or 0
    # Cache reads/writes are priced as normal input here (slightly high, never low).
    usd = cost(model, inp + cr + cw, out, searches) if u else 0.0
    con.execute(
        """INSERT INTO api_calls(ts, purpose, posting_id, company_id, model, input_tokens, output_tokens,
               cache_read_tokens, cache_write_tokens, web_searches, cost_usd, request_id, ok, note)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (db.now(), purpose, posting_id, company_id, model, inp, out, cr, cw, searches, usd,
         getattr(response, "_request_id", None), int(ok), note))
    con.commit()
    return usd


def summary(con, cfg):
    total, stop = limits(cfg)
    rows = con.execute("""SELECT purpose, COUNT(*) n, SUM(cost_usd) usd, SUM(web_searches) s
                          FROM api_calls GROUP BY purpose ORDER BY usd DESC""").fetchall()
    return {"spent": spent(con), "stop": stop, "total": total,
            "by_purpose": [(r["purpose"], r["n"], r["usd"] or 0, r["s"] or 0) for r in rows]}


def summary_text(con, cfg):
    s = summary(con, cfg)
    lines = [f"API spend: ${s['spent']:.4f} of the ${s['stop']:.2f} hard stop (${s['total']:.2f} budget)"]
    for purpose, n, usd, searches in s["by_purpose"]:
        extra = f", {searches} web searches" if searches else ""
        lines.append(f"  {purpose:9} {n:4} calls  ${usd:.4f}{extra}")
    lines.append("  (Worked out from list prices; your Anthropic Console shows the real bill.)")
    return "\n".join(lines)


def safe_error(e):
    """A short error message that can never contain the key."""
    name = type(e).__name__
    if name in ("AuthenticationError", "PermissionDeniedError"):
        return "API key missing or invalid - check .env"
    status = getattr(e, "status_code", None)
    msg = getattr(e, "message", None) or str(e)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        msg = msg.replace(key, "[hidden]")
    return f"{name}{f' ({status})' if status else ''}: {msg[:300]}"
