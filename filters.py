"""Free rules: role matching, location, term dates and flags. No AI, no internet."""
import json
import re
from datetime import date

# ---------------------------------------------------------------- locations
STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
ABBRS = set(STATES.values())

# Detroit metro (commutable from Garden City): Wayne, Oakland, Macomb, Washtenaw, Livingston counties.
DETROIT_METRO = {
    "detroit", "dearborn", "dearborn heights", "garden city", "westland", "livonia", "canton", "plymouth",
    "northville", "novi", "farmington", "farmington hills", "southfield", "troy", "auburn hills", "pontiac",
    "rochester", "rochester hills", "bloomfield", "bloomfield hills", "west bloomfield", "birmingham",
    "royal oak", "madison heights", "warren", "sterling heights", "shelby township", "macomb",
    "clinton township", "ann arbor", "ypsilanti", "romulus", "taylor", "southgate", "wyandotte", "allen park",
    "inkster", "redford", "wixom", "milford", "lake orion", "orion township", "commerce township",
    "highland park", "hamtramck", "ferndale", "brighton", "belleville", "van buren", "chelsea", "dexter",
    "saline", "utica", "south lyon", "new hudson", "walled lake", "oak park", "roseville", "st. clair shores",
    "grosse pointe", "trenton", "flat rock", "woodhaven", "brownstown", "mount clemens", "chesterfield",
    "lincoln park", "riverview", "ecorse", "melvindale", "white lake", "waterford", "clarkston", "holly",
    "fenton", "howell", "hartland", "plymouth township", "canton township", "superior township",
}
OTHER_MICHIGAN = {
    "lansing", "east lansing", "grand rapids", "kalamazoo", "flint", "holland", "midland", "saginaw",
    "battle creek", "traverse city", "muskegon", "jackson", "benton harbor", "st. joseph", "zeeland",
    "marshall", "houghton", "bay city", "mount pleasant", "grand haven", "wyoming, mi", "kentwood",
    "portage", "okemos", "adrian", "monroe", "port huron", "marquette", "big rapids", "allendale",
}
# Places that clearly aren't Michigan even though they have no state (so not "unclear").
KNOWN_ELSEWHERE = {"sf", "nyc", "la", "south sf", "bay area", "silicon valley", "dc", "washington dc",
                   "san francisco", "seattle", "new york", "new york city", "boston", "chicago", "austin",
                   "los angeles", "denver", "atlanta", "dallas", "houston", "san jose", "mountain view",
                   "palo alto", "sunnyvale", "miami", "philadelphia", "pittsburgh", "portland", "san diego",
                   "phoenix", "salt lake city", "minneapolis", "raleigh", "redmond", "bellevue", "menlo park",
                   "santa clara", "irvine", "nashville", "columbus", "cleveland", "indianapolis", "st. louis"}
FOREIGN = ("canada", "uk", "united kingdom", "india", "germany", "mexico", "china", "japan", "israel",
           "ireland", "france", "netherlands", "australia", "singapore", "poland", "brazil", "korea",
           "spain", "italy", "sweden", "switzerland", "romania", "taiwan", "ontario", "toronto", "london",
           "vancouver", "montreal", "bangalore", "bengaluru", "hyderabad", "berlin", "munich", "tel aviv",
           "europe", "emea", "apac", "latam", "united arab emirates", "uae", "dubai", "hong kong", "philippines",
           "vietnam", "argentina", "colombia", "chile", "portugal", "denmark", "finland", "norway", "belgium",
           "austria", "czech", "hungary", "egypt", "nigeria", "kenya", "south africa", "new zealand", "pakistan",
           "dublin", "bucharest", "paris", "amsterdam", "sydney", "tokyo", "seoul", "sao paulo", "são paulo",
           "zurich", "stockholm", "madrid", "barcelona", "warsaw", "krakow", "kraków", "wrocław", "prague",
           "lisbon", "mexico city", "bogota", "bogotá", "shanghai", "beijing", "shenzhen", "taipei", "manila",
           "budapest", "brussels", "copenhagen", "oslo", "helsinki", "vienna", "milan", "athens", "istanbul")


def parse_location(s):
    """One location string -> dict(city, state, remote, foreign, known)."""
    raw = (s or "").strip()
    low = raw.lower()
    out = {"raw": raw, "city": "", "state": "", "remote": False, "foreign": False, "known": False}
    if not low:
        return out
    if any(re.search(rf"\b{re.escape(f)}\b", low) for f in FOREIGN):
        out["foreign"] = True
    if "remote" in low or "anywhere" in low or "work from home" in low:
        out["remote"] = not out["foreign"]
    parts = [p.strip() for p in re.split(r"[,|/\-–(]", raw) if p.strip()]
    # State: a two-letter code or full state name in any part after the first.
    for i, p in enumerate(parts[1:] + parts[:1]):
        pl = p.lower().strip(" .)")
        first = i == len(parts) - 1  # the first part is usually the city ("LA" = Los Angeles, not Louisiana)
        if not first and p.strip(" .)") in ABBRS and p.strip() != p.strip().lower():
            out["state"] = p.strip(" .)")
        elif pl in STATES:
            out["state"] = STATES[pl]
        if out["state"]:
            break
    city = parts[0].lower().strip() if parts else ""
    if city in ("remote", "hybrid", "onsite", "on-site", "in office") and len(parts) > 1:
        city = parts[1].lower().strip()
    out["city"] = city.title() if city else ""
    if not out["state"] and (city in DETROIT_METRO or city in OTHER_MICHIGAN) and not out["foreign"]:
        out["state"] = "MI"
    if "michigan" in low and not out["state"]:
        out["state"] = "MI"
    elsewhere = any(p.lower().strip(" .") in KNOWN_ELSEWHERE for p in parts)
    out["known"] = bool(out["state"] or out["remote"] or out["foreign"] or low in KNOWN_ELSEWHERE or elsewhere
                        or "united states" in low and "remote" in low)
    return out


def classify_location(locations):
    locs = [parse_location(s) for s in locations if s and s.strip()]
    mi = [l for l in locs if l["state"] == "MI"]
    det = [l for l in mi if l["city"].lower() in DETROIT_METRO]
    remote = [l for l in locs if l["remote"]]
    best = (det or mi or remote or locs or [{"city": "", "state": "", "raw": ""}])[0]
    other_states = sorted({l["state"] for l in locs if l["state"] and l["state"] != "MI"})
    unclear = not mi and not remote and not any(l["known"] for l in locs)
    if mi:
        reason = "Detroit metro" if det else "Michigan"
    elif remote:
        reason = "remote (US)"
    elif unclear:
        reason = "location unclear"
    elif other_states:
        reason = f"outside Michigan ({', '.join(other_states[:4])}{'…' if len(other_states) > 4 else ''})"
    else:
        reason = "outside the US" if any(l["foreign"] for l in locs) else "outside Michigan"
    return {
        "location_raw": "; ".join(l["raw"] for l in locs)[:500],
        "city": best["city"], "state": best["state"] or ("" if not remote else "Remote"),
        "is_remote": int(bool(remote)), "is_michigan": int(bool(mi)), "is_detroit_metro": int(bool(det)),
        "location_unclear": int(unclear), "location_reason": reason,
    }


def location_in_scope(c, scope, has_housing_flag, remote_ok=True):
    if scope == "all":
        return True
    if c["is_michigan"] or c["location_unclear"] or (remote_ok and c["is_remote"]):
        return True
    return scope == "michigan_plus_relocation" and has_housing_flag


# ---------------------------------------------------------------- terms
TERM_MONTHS = {"spring": (1, 5), "winter": (1, 4), "summer": (5, 8), "fall": (9, 12), "autumn": (9, 12)}
TERM_RE = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*'?(20\d\d|\d\d)\b", re.I)
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
DATE_RANGE_RE = re.compile(
    rf"\b({MONTHS})[a-z]*\.?\s+(\d{{1,2}}),?\s*(20\d\d)?\s*(?:-|–|—|to|through|until)\s*"
    rf"({MONTHS})[a-z]*\.?\s+(\d{{1,2}}),?\s*(20\d\d)", re.I)
MONTH_NUM = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _month_end(y, m):
    return date(y + (m == 12), m % 12 + 1, 1).toordinal() - 1


def classify_term(title, terms, description):
    """Return term label, approx start/end dates, length in weeks."""
    found = []
    for t in terms or []:
        m = TERM_RE.search(t)
        if m:
            found.append((m.group(1).lower(), int(m.group(2)) % 100 + 2000))
    if not found:
        for text in (title, (description or "")[:4000]):
            found = [(m.group(1).lower(), int(m.group(2)) % 100 + 2000) for m in TERM_RE.finditer(text or "")]
            if found:
                found = found[:3]
                break
    year_round = bool(re.search(r"year[- ]round|12[- ]month|full[- ]year", f"{title} {description or ''}", re.I))
    start = end = None
    exact = DATE_RANGE_RE.search(description or "")
    if exact:
        try:
            y2 = int(exact.group(6))
            y1 = int(exact.group(3) or y2)
            start = date(y1, MONTH_NUM[exact.group(1)[:3].lower()], int(exact.group(2)))
            end = date(y2, MONTH_NUM[exact.group(4)[:3].lower()], int(exact.group(5)))
            if not (0 < (end - start).days < 800):
                start = end = None
        except ValueError:
            start = end = None
    if not start and found:
        starts, ends = [], []
        for season, year in found:
            m1, m2 = TERM_MONTHS["fall" if season == "autumn" else season]
            starts.append(date(year, m1, 1))
            ends.append(date.fromordinal(_month_end(year, m2)))
        start, end = min(starts), max(ends)
    seen = []
    for s, y in found:
        label = f"{s.title()} {y}"
        if label not in seen:
            seen.append(label)
    label = ", ".join(seen) or ("Year-round" if year_round else "")
    if year_round and seen:
        label += " (year-round)"
    weeks = round((end - start).days / 7) if start and end else None
    return {"term": label, "start_date": start.isoformat() if start else "",
            "end_date": end.isoformat() if end else "", "length_weeks": weeks,
            "approx_dates": not exact, "year_round": year_round, "n_terms": len(seen)}


# ---------------------------------------------------------------- roles
INTERN_RE = re.compile(r"\b(intern|interns|internship|co-?op|coop|apprentice(ship)?|student researcher|"
                       r"summer (analyst|associate|engineer)|working student)\b", re.I)
COOP_RE = re.compile(r"\bco-?op\b|\bcoop\b", re.I)
P1 = re.compile(r"\b(software|swe|sde|developer|full[- ]?stack|back[- ]?end|front[- ]?end|programmer|"
                r"application development|web development|mobile|ios|android|programming|coding)\b", re.I)
P2 = re.compile(r"\b(it|information technology|data|analytics|qa|quality assurance|test|testing|validation|"
                r"embedded|firmware|cyber|security|devops|cloud|infrastructure|sre|reliability|robotics|"
                r"machine learning|ml|ai|artificial intelligence|computer|computing|network|systems|automation|"
                r"tech|technology|digital|platform|database|computer vision|perception|autonomy|"
                r"autonomous|simulation|hpc|gpu)\b", re.I)
P3 = re.compile(r"\b(automotive|vehicle|vehicles|ev|adas|powertrain|diagnostics|calibration|mobility|"
                r"battery|infotainment|telematics|manufacturing engineering)\b", re.I)
OTHER_ENG = re.compile(r"\b(mechanical|electrical|civil|chemical|industrial|aerospace|materials|"
                       r"manufacturing|hardware|electronics|structural|biomedical|nuclear)\b", re.I)
MAJOR_REQ = re.compile(r"(pursuing|majoring in|major in|degree in|studying|enrolled in)[^.]{0,80}?"
                       r"\b(mechanical|civil|chemical|aerospace|materials|industrial|biomedical|nuclear)", re.I)
CS_OK = re.compile(r"computer science|computer engineering|software engineering|\bcs\b|related (technical )?field",
                   re.I)


def classify_role(title, description, category, degrees, cfg, from_intern_list):
    """Return (priority 1-4 or None, exclude_reason or None, job_type)."""
    t = title or ""
    tl = t.lower()
    job_type = "co-op" if COOP_RE.search(t) else "internship"
    if not from_intern_list and not INTERN_RE.search(t):
        return None, "not an internship/co-op", job_type
    for kw in cfg.get("exclude_keywords", []):
        if re.search(rf"\b{re.escape(kw.lower())}\b", tl):
            return None, f"excluded keyword: {kw}", job_type
    if re.search(r"\bhigh school\b", tl):
        return None, "high school program", job_type
    if re.search(r"\b(master'?s?|graduate|grad|doctoral|mba|ms|phd)\b", tl) and not re.search(
            r"undergrad", tl):
        return None, "graduate students only", job_type
    if degrees and not any("bachelor" in d.lower() or "associate" in d.lower() for d in degrees):
        return None, "graduate degree only (" + ", ".join(degrees) + ")", job_type
    if P1.search(t):
        return 1, None, job_type
    if P2.search(t):
        return 2, None, job_type
    if P3.search(t):
        return 3, None, job_type
    if OTHER_ENG.search(t):
        desc = description or ""
        if desc and MAJOR_REQ.search(desc) and not CS_OK.search(desc):
            return None, "requires a non-CS engineering major", job_type
        return 4, None, job_type
    if (category or "").lower() in ("software", "software engineering", "ai/ml/data", "hardware",
                                    "data science, ai & machine learning"):
        return 4, None, job_type  # tech category but a vague title: lowest priority
    return None, "not a target tech role", job_type


# ---------------------------------------------------------------- flags
GPA_RES = [
    re.compile(r"\b([1-4]\.\d{1,2})\s*(?:/\s*4\.0+\s*)?(?:or (?:higher|above|better)\s*)?(?:cumulative |overall |minimum |min\.? )?(?:gpa|grade point)", re.I),
    re.compile(r"\b(?:gpa|grade point average)[^.\d]{0,40}?(?:of|:|at least|minimum|min\.?|above|>=?|≥)\s*(?:a\s*)?([1-4]\.\d{1,2})", re.I),
]
CITIZEN_RE = re.compile(r"u\.?\s?s\.?\s*citizen|united states citizen|citizenship (is )?required|"
                        r"must be a (u\.?s\.? )?citizen|green card holder|permanent resident|u\.?s\.? person", re.I)
CLEARANCE_RE = re.compile(r"security clearance|clearance (is )?required|ability to obtain (a )?(secret|clearance)|"
                          r"\bitar\b|export control|ts/sci|top secret", re.I)
EARLY_RE = re.compile(r"\b(freshm[ae]n|sophomores?|first[- ]year|second[- ]year|early[- ]career program|"
                      r"explore (program|intern)|step intern|discovery program|underclass(men)?)\b", re.I)
LONG_RE = re.compile(r"\b([5-9]|1[0-2])[- ]?(months?|mo)\b|\b(8|eight|six|6) months\b", re.I)


def find_gpa_min(text):
    vals = []
    for rx in GPA_RES:
        for m in rx.finditer(text or ""):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            if 2.0 <= v < 4.0:
                vals.append(v)
    return max(vals) if vals else None


def flags_for(title, description, sponsorship, term, job_type, cfg):
    text = f"{title}\n{description or ''}"
    flags = []
    gpa = find_gpa_min(text)
    if gpa is not None:
        above = cfg.get("gpa", {}).get("flag_above")  # your own threshold lives in config.yaml (not committed)
        flags.append(f"GPA min {gpa:.1f}" + (f" (above {above})" if above is not None and gpa > float(above) else ""))
    if CITIZEN_RE.search(text) or "citizenship is required" in (sponsorship or "").lower():
        flags.append("US citizenship required")
    if CLEARANCE_RE.search(text):
        flags.append("clearance/export control")
    words = cfg.get("location", {}).get("relocate_only_if_mentions", ["housing", "relocation", "stipend"])
    if any(re.search(rf"\b{re.escape(w)}", text, re.I) for w in words):
        flags.append("housing/relocation mentioned")
    if EARLY_RE.search(text):
        flags.append("early-career program")
    longer = (job_type == "co-op" or term["year_round"] or term["n_terms"] > 1
              or (term["length_weeks"] or 0) > 18 or bool(LONG_RE.search(title or "")))
    if longer and cfg.get("flag_if_longer_than_summer", True):
        flags.append("co-op/longer than summer (check classes)")
    if not term["term"]:
        flags.append("term not stated")
    return flags


# ---------------------------------------------------------------- everything together
def classify(row, cfg, today=None):
    """Compute all derived columns for one posting row (dict-like). Pure function."""
    today = today or date.today()
    locations = json.loads(row["locations_json"] or "[]")
    # Vague board locations like "In-Office": use a place named at the end of the title ("... - Austin, TX").
    title_loc = re.search(r"\s[-–(]\s*([A-Z][\w .']+,\s*[A-Z][\w .]+)\)?\s*$", row["title"] or "")
    if title_loc and not any("," in l or "remote" in l.lower() for l in locations):
        locations = [title_loc.group(1)] + locations
    terms = json.loads(row["terms_json"] or "[]")
    degrees = json.loads(row["degrees_json"] or "[]")
    srcs = row["source"].split(",")
    from_list = "simplify" in srcs or "manual" in srcs  # curated lists: the title needn't say "intern"
    loc = classify_location(locations)
    term = classify_term(row["title"], terms, row["description"])
    priority, role_reason, job_type = classify_role(row["title"], row["description"], row["category"],
                                                    degrees, cfg, from_list)
    if not role_reason and term["end_date"] and term["end_date"] < today.isoformat():
        role_reason = f"term already over ({term['term']})"
    flags = flags_for(row["title"], row["description"], row["sponsorship"], term, job_type, cfg)
    if term["start_date"] and term["start_date"] < today.isoformat() <= (term["end_date"] or "9999"):
        flags.append("term already started")
    if loc["location_unclear"]:
        flags.append("location unclear")
    if priority == 4 and OTHER_ENG.search(row["title"] or ""):
        flags.append("other engineering: check major")
    if row["desc_source"] == "unavailable":
        flags.append("description unavailable: check posting")
    lcfg = cfg.get("location", {})
    in_loc = location_in_scope(loc, lcfg.get("scope", "michigan"),
                               "housing/relocation mentioned" in flags, lcfg.get("treat_remote_as_local", True))
    exclude = None
    if not row["active"]:
        exclude = "no longer listed"
    elif role_reason:
        exclude = role_reason
    elif not in_loc:
        exclude = "remote (you prefer in-person)" if loc["is_remote"] and not loc["is_michigan"] \
            else loc["location_reason"]
    out = dict(loc)
    out.update(term=term["term"], start_date=term["start_date"], end_date=term["end_date"],
               length_weeks=term["length_weeks"], job_type=job_type, role_priority=priority,
               role_reason=role_reason, in_scope=int(exclude is None), exclude_reason=exclude,
               flags=json.dumps(flags),
               rule_score=rule_score(priority, loc, term["term"], flags, row["date_posted"], today))
    return out


# ---------------------------------------------------------------- free rule score (0-100)
def rule_score(priority, loc, term_label, flags, date_posted, today):
    """Cheap first-pass ranking. A GPA minimum or citizenship rule never lowers the score."""
    if not priority:
        return 0
    s = {1: 40, 2: 30, 3: 25, 4: 10}[priority]
    s += 30 if loc["is_detroit_metro"] else 20 if loc["is_michigan"] else 15 if loc["is_remote"] else         5 if loc["location_unclear"] else 0
    s += 15 if "Summer 2027" in (term_label or "") else 5 if term_label else 0
    s += 10 if "early-career program" in flags else 0
    try:
        if (today - date.fromisoformat(date_posted[:10])).days <= 14:
            s += 5
    except (TypeError, ValueError):
        pass
    s -= 5 if "clearance/export control" in flags else 0
    return max(0, min(100, s))


def reclassify_all(con, cfg):
    cols = None
    rows = con.execute("SELECT * FROM postings").fetchall()
    for r in rows:
        c = classify(r, cfg)
        cols = cols or list(c)
        con.execute(f"UPDATE postings SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?",
                    (*[c[k] for k in cols], r["id"]))
    con.commit()
    return len(rows)
