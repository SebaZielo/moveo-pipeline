"""MOVEO pipeline: the few judgment steps (one-line descriptions, genre fixes, dropping non-events).

With ANTHROPIC_API_KEY set (GitHub secret), new listings are reviewed by Claude through the API in batches
of 50, capped at LLM_MAX_ITEMS a day (default 700). Without a key, simple rules do it instead: nothing is
dropped except clear kids-only, online-only and private items, and the description is "<type> at <venue>".

Model: MOVEO_MODEL (default claude-haiku-4-5-20251001). A typical day (300-500 new listings) costs a few cents.
"""
import json, os, re, time
import requests

GENRES = ["music", "theater", "tango", "museums", "galleries", "food", "markets", "film", "classes", "outdoor", "nightlife", "events"]
GUIDE = """Genres: DJ/club parties = nightlife; live bands/concerts = music; stand-up, plays, dance, circus = theater;
milongas, tango classes and tango shows = tango; talks, book launches, screenings = film; tech, business, networking,
fairs and festivals = events; workshops and courses = classes; ferias and markets = markets; tastings and food events = food;
exhibitions and openings = galleries (private/commercial) or museums (museums, cultural centers); parks, walks,
sport, rides = outdoor."""

KIDS = re.compile(r"\b(infantil(es)?|para (los )?(chicos|niñ[oa]s|chiques)|niñ[oa]s de \d|kids|children'?s|baby|bebés?|"
                  r"vacaciones de invierno|family show)\b", re.I)
ONLINE = re.compile(r"\b(online|on-line|virtual|v[ií]a zoom|zoom|webinar|streaming|transmisi[oó]n en vivo|livestream)\b", re.I)
PRIVATE = re.compile(r"\b(evento privado|private event|solo socios|members only|by invitation|con invitaci[oó]n)\b", re.I)


def key():
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def ask(prompt, max_tokens=8000):
    model = os.environ.get("MOVEO_MODEL", "claude-haiku-4-5-20251001")
    for i in range(4):
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=180, headers={
                "x-api-key": key(), "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                return "".join(b.get("text", "") for b in r.json().get("content", []))
            if r.status_code in (400, 401, 403, 404):
                print("Claude API refused:", r.status_code, r.text[:200])
                return None
        except requests.RequestException:
            pass
        time.sleep(10 * (i + 1))
    return None


def _json(text):
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def rules_review(items):
    """items: [{id, title, venue, raw/snippet, genre}] -> {id: {drop: true}} for obvious non-fits."""
    out = {}
    for it in items:
        t = " ".join(str(it.get(k) or "") for k in ("title", "raw", "snippet"))
        if KIDS.search(t) or PRIVATE.search(t) or (ONLINE.search(it.get("title") or "") and not it.get("venue")):
            out[it["id"]] = {"drop": True}
    return out


def review(items, kind="harvest"):
    """Returns ({id: fix}, how) where fix has desc/genre/sub/title/venue or drop."""
    if not items:
        return {}, "none"
    if not key():
        return rules_review(items), "rules"
    cap = int(os.environ.get("LLM_MAX_ITEMS", "700"))
    todo, fixes = items[:cap], {}
    for k in range(0, len(todo), 50):
        chunk = [{x: it.get(x) for x in ("id", "genre", "sub", "title", "venue", "when", "price", "source", "raw", "snippet", "foundOn") if it.get(x)}
                 for it in todo[k:k + 50]]
        prompt = f"""You review new listings for MOVEO, a personal app of things to do in Buenos Aires city and Greater Buenos Aires.
For EVERY item return either
  {{"desc": "<one concrete plain English line, max 110 characters: what it is and who or what is involved (artist, style, format, vibe); no hype words, no dates, no prices>", "genre": "<genre id>", "sub": "<keep or fix the sub, or null>", "title": "<only if the given title is garbled, else omit>", "venue": "<only if garbled, else omit>"}}
or {{"drop": true}} for: not a specific event or place to go (generic listing pages, ads, transfers, tickets resale), duplicates of another item in this list, outside Buenos Aires city/AMBA, online-only, private or members-only, kids-only shows.
Genre ids: {", ".join(GENRES)}.
{GUIDE}
Reply with ONLY a JSON object mapping each id to its answer.

ITEMS:
{json.dumps(chunk, ensure_ascii=False)}"""
        res = _json(ask(prompt))
        if not isinstance(res, dict):
            fixes.update(rules_review(todo[k:k + 50]))
            continue
        for i, fx in res.items():
            if not isinstance(fx, dict):
                continue
            if fx.get("genre") not in GENRES:
                fx.pop("genre", None)
            if fx.get("desc"):
                fx["desc"] = str(fx["desc"]).strip()[:110]
            fixes[i] = {a: b for a, b in fx.items() if b is not None}
    fixes.update({i: v for i, v in rules_review(items[cap:]).items()})
    return fixes, "claude"
