"""MOVEO date check, without Claude: confirm each listing's dates against the page it links to.

Same rules as the Claude date check (PART 1):
  * queue: listings available in the next 7 days not checked for 3+ days, then never-checked ones (newest first),
    then the rest by oldest check; up to --max listings or --minutes, one page fetch per url, polite per site;
  * writes only kind, dates, days, time, start, end, verify, verifiedAt, verifyNote;
  * verify = ok (page confirms) / fixed (corrected) / unconfirmed (page unreadable or silent) / cancelled / ended;
  * NEVER guesses: dates are only changed when the page states them in machine-readable form (JSON-LD Event,
    the Luma API). Dates read from page text can only confirm a listing, add a missing run end ("hasta el ...")
    or missing weekdays; a mention of "cancelado/suspendido" in text is noted, never used to hide a listing.
Blog/guide links are skipped here: the blog reader handles those.

  python3 -m pipeline.datecheck --events DIR --out FILE [--today YYYY-MM-DD] [--max 400] [--minutes 25]
  -> FILE: {"updates": {id: {fields}}, "report": {...}}
"""
import argparse, concurrent.futures as cf, datetime as dt, glob, json, os, re, sys, time
from collections import Counter, defaultdict
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy"))
import blogreader as br  # noqa: E402

BA = dt.timezone(dt.timedelta(hours=-3))
CANCEL_TXT = re.compile(r"\b(cancelad[oa]s?|suspendid[oa]s?|reprogramad[oa]s?|cancelled|canceled|postponed)\b", re.I)
SOCIAL = re.compile(r"instagram\.com|facebook\.com|fb\.me|tiktok\.com|x\.com/|twitter\.com|linktr\.ee|wa\.me", re.I)


def local_dt(v):
    """ISO date/datetime string -> (date, 'HH:MM' or None) in Buenos Aires time."""
    if not isinstance(v, str):
        return None, None
    v = v.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?", v)
    if not m:
        return None, None
    try:
        d = dt.date.fromisoformat(m.group(1))
    except ValueError:
        return None, None
    if not m.group(2):
        return d, None
    tz = m.group(3)
    t = dt.datetime.fromisoformat(m.group(1) + "T" + m.group(2))
    if tz:
        off = dt.timezone.utc if tz == "Z" else dt.timezone(dt.timedelta(hours=int(tz[:3]), minutes=int(tz[0] + tz[-2:])))
        t = t.replace(tzinfo=off).astimezone(BA)
    if t.hour == 0 and t.minute == 0 and not tz:
        return t.date(), None
    return t.date(), t.strftime("%H:%M")


def luma_events(url):
    slug = urlparse(url).path.strip("/").split("/")[0]
    if not slug:
        return 0, []
    st, _, body = br.fetch(f"https://api.lu.ma/url?url={slug}")
    try:
        data = json.loads(body)
    except Exception:
        return st, []
    out, stack = [], [data]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            if x.get("start_at") and (x.get("name") or x.get("api_id")):
                d, t = local_dt(x["start_at"])
                e, _ = local_dt(x.get("end_at"))
                if d:
                    out.append({"name": x.get("name") or "", "start": d, "time": t, "end": e, "status": ""})
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    # a single event page: the top-level event has no name in the walk sometimes; keep what we found
    return st or 200, out


def read_page(url):
    """-> {status, ok, events: [{name, start, time, end, status}], text, social}"""
    if SOCIAL.search(url):
        return {"status": 0, "ok": False, "events": [], "text": "", "social": True}
    if re.search(r"(^|\.)lu\.ma/|luma\.com/", url):
        st, evs = luma_events(url)
        if evs:
            return {"status": st, "ok": True, "events": evs, "text": " ".join(e["name"] for e in evs)}
    st, final, html = br.fetch(url)
    if not html:
        return {"status": st, "ok": False, "events": [], "text": ""}
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    evs = []
    for o in br.jsonld_objects([s.get_text() for s in soup.find_all("script", type="application/ld+json")]):
        t = br.typ(o)
        if not re.search(r"Event|Screening|Performance|Festival|Exhibition|Concert|Theater", t):
            continue
        d, tm = local_dt(o.get("startDate"))
        e, _ = local_dt(o.get("endDate"))
        status = str(o.get("eventStatus") or "")
        if d:
            evs.append({"name": str(o.get("name") or ""), "start": d, "time": tm, "end": e, "status": status})
        for sch in (o.get("eventSchedule") if isinstance(o.get("eventSchedule"), list) else [o.get("eventSchedule")] if o.get("eventSchedule") else []):
            if isinstance(sch, dict):
                sd, stm = local_dt(sch.get("startDate"))
                se, _ = local_dt(sch.get("endDate"))
                if sd:
                    evs.append({"name": str(o.get("name") or ""), "start": sd, "time": stm or local_dt("2000-01-01T" + str(sch.get("startTime") or "")[:5])[1],
                                "end": se, "status": status, "byDay": sch.get("byDay")})
    for s in soup(["script", "style", "noscript"]):
        s.decompose()
    return {"status": st, "ok": True, "events": evs, "text": soup.get_text("\n", strip=True)[:200000]}


def label(url):
    d = br.domain(url)
    for frag, name in br.PLATFORMS:
        if frag in d or frag in url:
            return name
    return d.replace("www.", "")


def fmt_dates(ds, t=None):
    ds = sorted(ds)
    s = ", ".join(dt.date.fromisoformat(x).strftime("%a %d %b") for x in ds[:4]) + (f" +{len(ds) - 4}" if len(ds) > 4 else "")
    return s + (f" {t}" if t else "")


def decide(e, P, today):
    """-> (patch dict or None, outcome)"""
    T = today.isoformat()
    horizon = max(today + dt.timedelta(days=90), dt.date(today.year, 12, 31)).isoformat()
    base = {"verifiedAt": T}
    src = label(e["url"])
    if P.get("social"):
        if e.get("verify"):
            return dict(base), "skipped_social"
        return dict(base, verify="unconfirmed", verifyNote=f"{src}: not machine-readable"), "unconfirmed"
    if not P["ok"] and (P["status"] in (0, 401, 403, 408, 429) or P["status"] >= 500):
        return None, "unreachable"                                  # network trouble or blocked: try again next run
    if not P["ok"]:
        note = f"Page gone ({P['status']})" if P["status"] in (404, 410) else "Page unreadable"
        return dict(base, verify="unconfirmed", verifyNote=f"{src}: {note}"[:90]), "unconfirmed"

    title = e.get("title") or ""
    evs = P["events"]
    names = {x["name"] for x in evs if x["name"]}
    matched = [x for x in evs if x["name"] and br.sim(title, x["name"]) >= 0.5]
    if not matched and evs and len(names) <= 1 and e.get("kind") == "event":
        # a page about one event (its name may be shortened): accept it if the title shares words with it
        nm = next(iter(names), "")
        if not nm or br.sim(title, nm) >= 0.25 or len(set(br.tokens(title)) & set(br.tokens(nm))) >= 1:
            matched = evs
    fut_now = sorted(d for d in e.get("dates") or [] if d >= T)

    if matched:
        if all(re.search(r"Cancel", x["status"]) for x in matched):
            return dict(base, verify="cancelled", verifyNote=f"{src}: event cancelled"), "cancelled"
        live = [x for x in matched if not re.search(r"Cancel", x["status"])]
        runs = [x for x in live if x["end"] and (x["end"] - x["start"]).days > 4]
        if runs:                                                    # an exhibition / season window
            r = max(runs, key=lambda x: x["end"])
            if r["end"] < today:
                return dict(base, verify="ended", verifyNote=f"{src}: ran until {r['end'].strftime('%d %b')}"), "ended"
            p = dict(base, kind="ongoing", start=r["start"].isoformat(), end=r["end"].isoformat())
            if e.get("kind") == "event":
                p["dates"] = []
            changed = any(e.get(k) != p[k] for k in ("kind", "start", "end"))
            p["verify"] = "fixed" if changed else "ok"
            p["verifyNote"] = f"{src}: {r['start'].strftime('%d %b')} to {r['end'].strftime('%d %b')}"[:90]
            return p, p["verify"]
        fut = sorted({x["start"].isoformat() for x in live if x["start"] >= today and x["start"].isoformat() <= horizon})
        if not fut:
            last = max(x["end"] or x["start"] for x in live) if live else None
            # The page only shows past dates. Ticket pages of recurring events (Meetup, Eventbrite series) often show
            # just the occurrence they were opened on, so this is NOT proof the listing is over: flag it, never hide it.
            # (Listings whose own dates are all past are removed by the cleanup step.)
            if last and last < today:
                return dict(base, verify="unconfirmed", verifyNote=f"{src}: page shows only past dates (last {last.strftime('%d %b')})"), "unconfirmed_past"
            return dict(base, verify="unconfirmed", verifyNote=f"{src}: no upcoming date listed"), "unconfirmed"
        times = Counter(x["time"] for x in live if x["time"] and x["start"].isoformat() in fut)
        t = times.most_common(1)[0][0] if times else None
        p = dict(base)
        if e.get("kind") != "event":
            # a weekly listing whose page lists dated functions (a play, a cycle): switch to the stated dates.
            # Places (restaurants, museums, markets...) stay places unless the event name is clearly the listing.
            def jac(x):
                a, b = set(br.tokens(title)), set(br.tokens(x["name"]))
                return len(a & b) / max(1, len(a | b))
            best = max(jac(x) for x in live) if live else 0
            if e.get("genre") in ("food", "museums", "galleries", "markets", "outdoor") and best < 0.8:
                return dict(base, verify="ok" if e.get("verify") == "ok" else "unconfirmed",
                            verifyNote=f"{src}: page lists dated events, kept as a place"[:90]), "unconfirmed"
            p.update(kind="event", dates=fut, days=[])
            for k in ("start", "end"):
                if e.get(k):
                    p[k] = {"__delete__": True}
        elif fut != fut_now:
            p["dates"] = fut
        if t and t != (e.get("time") or ""):
            p["time"] = t
        changed = any(k in p for k in ("kind", "dates", "time"))
        p["verify"] = "fixed" if changed else "ok"
        p["verifyNote"] = f"{src}: {fmt_dates(fut, t)}"[:90]
        return p, p["verify"]

    # no machine-readable event: read the text around the title (confirm only)
    text = P["text"]
    nt = br.norm(text)
    toks = [w for w in br.tokens(title) if len(w) > 3][:4]
    pos = sorted(nt.find(w) for w in toks if nt.find(w) >= 0)
    if not pos or (len(toks) >= 2 and len(pos) < 2):
        return dict(base, verify="unconfirmed", verifyNote=f"{src}: page doesn't mention it"[:90]), "unconfirmed"
    win = text[max(0, pos[0] - 200): pos[0] + 900]
    if CANCEL_TXT.search(win):
        return dict(base, verify="unconfirmed", verifyNote=f"{src}: page mentions cancelled/suspended – check"[:90]), "flag_cancel"
    w = br.parse_when(win, today)
    f = br.when_fields(w, today)
    note = br.when_note(w)
    if e.get("kind") == "event":
        if fut_now and set(fut_now) & set(f.get("dates") or []):
            return dict(base, verify="ok", verifyNote=f"{src}: {note}"[:90]), "ok"
        return dict(base, verify="unconfirmed", verifyNote=f"{src}: no clear date on page"[:90]), "unconfirmed"
    p = dict(base)
    if f.get("end") and not e.get("end") and f["end"] >= T and re.search(r"\bhasta\b|\buntil\b|\bthrough\b", br.norm(win)):
        p["end"] = f["end"]
    if f.get("days") and not e.get("days") and len(f["days"]) < 7:
        p["days"] = f["days"]
    if len(p) > 1:
        p.update(verify="fixed", verifyNote=f"{src}: {note}"[:90])
        return p, "fixed"
    if (f.get("days") and f["days"] == e.get("days")) or (f.get("end") and f["end"] == e.get("end")):
        return dict(base, verify="ok", verifyNote=f"{src}: {note}"[:90]), "ok"
    return dict(base, verify="unconfirmed", verifyNote=f"{src}: no clear schedule on page"[:90]), "unconfirmed"


def queue(events, today, limit):
    T = today.isoformat()
    soon = (today + dt.timedelta(days=7)).isoformat()
    stale = (today - dt.timedelta(days=3)).isoformat()
    week = (today - dt.timedelta(days=7)).isoformat()
    a, b, c = [], [], []
    for e in events.values():
        u = e.get("url") or ""
        if e.get("verify") in ("cancelled", "ended") or e.get("hidden") or not u.startswith("http") or br.is_blog(u):
            continue
        if e.get("end") and e["end"] < T:
            continue
        if e.get("kind") == "event" and not any(d >= T for d in e.get("dates") or []):
            continue
        va = e.get("verifiedAt") or ""
        avail = e.get("kind") != "event" or any(T <= d <= soon for d in e.get("dates") or [])
        if avail and va < stale:
            a.append(e)
        elif not va:
            b.append(e)
        elif va < week:
            c.append(e)
    b.sort(key=lambda e: e.get("addedAt") or "", reverse=True)
    c.sort(key=lambda e: e.get("verifiedAt") or "")
    return (a + b + c)[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--today")
    ap.add_argument("--max", type=int, default=400)
    ap.add_argument("--minutes", type=float, default=25)
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(BA).date()
    events = {}
    for f in glob.glob(os.path.join(a.events, "*.json")):
        d = json.load(open(f))
        if d.get("id"):
            events[d["id"]] = d
    q = queue(events, today, a.max)
    by_url = defaultdict(list)
    for e in q:
        by_url[e["url"]].append(e)
    by_dom = defaultdict(list)
    for u in by_url:
        by_dom[br.domain(u)].append(u)
    deadline = time.time() + a.minutes * 60
    updates, rep = {}, Counter()

    def work(dom):
        res = []
        for u in by_dom[dom]:
            if time.time() > deadline:
                break
            try:
                P = read_page(u)
            except Exception as ex:          # one bad page never stops the run
                P = {"status": 0, "ok": False, "events": [], "text": "", "err": str(ex)[:100]}
            for e in by_url[u]:
                try:
                    res.append((e["id"],) + decide(e, P, today))
                except Exception as ex:
                    res.append((e["id"], None, "error"))
        return res

    with cf.ThreadPoolExecutor(8) as ex:
        for res in ex.map(work, sorted(by_dom, key=lambda d: -len(by_dom[d]))):
            for i, p, outcome in res:
                rep[outcome] += 1
                if p:
                    updates[i] = p
    rep = dict(rep, queued=len(q), checked=len(updates),
               ranges=sum(1 for p in updates.values() if "end" in p and not isinstance(p["end"], dict)))
    json.dump({"updates": updates, "report": rep}, open(a.out, "w"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(rep))


if __name__ == "__main__":
    main()
