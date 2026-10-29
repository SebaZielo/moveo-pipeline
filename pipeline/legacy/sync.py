#!/usr/bin/env python3
"""Copy On The Move listings from the Claude app's database into the website's Supabase database.

Input: a folder of event JSON files exactly as ArtifactData saves them (db/events/<id>.json), and optionally the
"locations" collection folder (db/locations/part*.json: {"map": {eventId: [lat, lng, conf]}}).

  python3 sync_from_artifact.py --events db/events [--locations db/locations] --city ar-buenos-aires
      writes to Supabase using SUPABASE_URL and SUPABASE_SERVICE_KEY from the environment
  python3 sync_from_artifact.py --events db/events --sql out.sql
      writes SQL instead (for local testing)

Only CURRENT listings are sent (ongoing places, or a date today or later). Listings that were sent before and are no
longer in the Claude app or have expired are marked hidden ("expired") on the website, never deleted, so saved
reactions keep their titles. Images: the original artwork URL the collector found (imgHint) when it is a public
http(s) address; Claude-hosted pictures are not public and are left out.
"""
import argparse, datetime as dt, glob, json, os, re, sys, unicodedata, urllib.request

def slugify(s, n=70):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return (s[:n].rstrip("-")) or "x"

DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}

def num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None

def is_current(d, today):
    if d.get("verify") in ("cancelled", "ended"):
        return False
    if d.get("end") and d["end"] < today:
        return False
    if d.get("kind") == "ongoing":
        return True
    return any(x >= today for x in d.get("dates") or [])

def to_row(d, city, locs, valid_sub):
    eid = d["id"]
    lat, lng = num(d.get("lat")), num(d.get("lng"))
    loc = locs.get(eid)
    if loc and loc[0] is not None:
        lat, lng = num(loc[0]), num(loc[1])
    elif loc and loc[0] is None:       # the map routine marked the event's own pin as wrong
        lat = lng = None
    cat = d.get("genre") or "events"
    sub = d.get("cat") if (cat, d.get("cat")) in valid_sub else None
    # picture: the cover the pipeline stored (public Supabase URL), else the original page's picture address,
    # else the blog picture's address (never downloaded, owner 2026-10-27)
    img, img_src = None, None
    for key, label in (("img", d.get("imgSource")), ("imgHint", "original"), ("imgBackup", "blog")):
        u = d.get(key) or ""
        if u.startswith("http") and "claude.ai" not in u:
            img, img_src = u, label
            break
    src = [s.strip().lower() for s in re.split(r"[+/]", d.get("source") or "") if s.strip()][:6]
    return {
        "id": eid, "city_id": city, "slug": slugify(d.get("title")) + "-" + eid.split("-")[-1][:8],
        "title": (d.get("title") or "")[:200], "category": cat, "subcat": sub,
        "kind": "ongoing" if d.get("kind") == "ongoing" else "event",
        "dates": sorted(set(x for x in d.get("dates") or [] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", x))),
        "days": [x for x in d.get("days") or [] if x in DAYS],
        "start_time": d.get("time") or None, "run_start": d.get("start") or None, "run_end": d.get("end") or None,
        "venue_name": (d.get("venue") or None), "neighborhood": d.get("neighborhood") or None,
        "address": d.get("address") or None, "lat": lat, "lng": lng,
        "price_min": num(d.get("price_ars")), "price_usd": num(d.get("price_usd")), "price_note": d.get("price_note") or None,
        "url": d.get("url") or None, "sources": src, "image_url": img,
        "image_source": img_src if img else None, "description": d.get("desc") or None,
        "crowd": d.get("crowd") if isinstance(d.get("crowd"), str) else None,
        "verify": d.get("verify"), "verified_at": d.get("verifiedAt") or None, "verify_note": d.get("verifyNote") or None,
        "hidden": bool(d.get("hidden")), "hide_reason": d.get("hideReason"),
        "origin": "harvested", "status": "published",
        "created_at": (d.get("addedAt") or None),
    }

def venue_rows(rows, city):
    vs = {}
    for r in rows:
        name = (r["venue_name"] or "").split(",")[0].strip()
        if not name or re.fullmatch(r"(?i)see event page|online|varios|various.*|tba.*", name):
            continue
        slug = slugify(name, 60)
        v = vs.setdefault(slug, {"city_id": city, "slug": slug, "name": name[:120], "address": None,
                                 "neighborhood": None, "lat": None, "lng": None})
        v["address"] = v["address"] or r["address"]
        v["neighborhood"] = v["neighborhood"] or r["neighborhood"]
        if v["lat"] is None and r["lat"] is not None:
            v["lat"], v["lng"] = r["lat"], r["lng"]
        r["_venue_slug"] = slug
    return list(vs.values())

# ---------------------------------------------------------------- output: SQL (local tests)
def sql_lit(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "'{" + ",".join('"' + str(x).replace("\\", "\\\\").replace('"', '\\"') + '"' for x in v).replace("'", "''") + "}'"
    return "'" + str(v).replace("'", "''") + "'"

def write_sql(path, venues, rows):
    with open(path, "w") as f:
        for v in venues:
            cols = list(v)
            f.write(f"insert into public.venues ({','.join(cols)}) values ({','.join(sql_lit(v[c]) for c in cols)}) "
                    f"on conflict (city_id, slug) do nothing;\n")
        for r in rows:
            vs = r.pop("_venue_slug", None)
            cols = [c for c in r if r[c] is not None or c in ("subcat",)]
            vals = [sql_lit(r[c]) for c in cols]
            if vs:
                cols.append("venue_id"); vals.append(f"(select id from public.venues where city_id={sql_lit(r['city_id'])} and slug={sql_lit(vs)})")
            upd = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "created_at"))
            f.write(f"insert into public.events ({','.join(cols)}) values ({','.join(vals)}) on conflict (id) do update set {upd};\n")

# ---------------------------------------------------------------- output: Supabase REST
def rest(method, base, key, path, body=None, prefer=None):
    req = urllib.request.Request(base.rstrip("/") + "/rest/v1/" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("apikey", key)
    if key.startswith("eyJ"):                      # legacy service_role JWT; new sb_secret_ keys go in apikey only
        req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    with urllib.request.urlopen(req, timeout=60) as resp:
        t = resp.read()
        return json.loads(t) if t else None

def write_supabase(venues, rows, city):
    base, key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    ids = {}
    for i in range(0, len(venues), 500):
        out = rest("POST", base, key, "venues?on_conflict=city_id,slug&select=id,slug", venues[i:i + 500],
                   "resolution=merge-duplicates,return=representation")
        ids.update({x["slug"]: x["id"] for x in out})
    for r in rows:
        vs = r.pop("_venue_slug", None)
        r["venue_id"] = ids.get(vs)
        if not r.get("created_at"):
            r.pop("created_at", None)
    for i in range(0, len(rows), 500):
        rest("POST", base, key, "events?on_conflict=id", rows[i:i + 500], "resolution=merge-duplicates,return=minimal")
    # anything we sent before that is no longer current in the Claude app: hide it as expired
    live = {r["id"] for r in rows}
    gone, start = [], 0
    while True:
        page = rest("GET", base, key, f"events?select=id&city_id=eq.{city}&origin=eq.harvested&hidden=eq.false&order=id&offset={start}&limit=1000")
        gone += [x["id"] for x in page if x["id"] not in live]
        if len(page) < 1000:
            break
        start += 1000
    for i in range(0, len(gone), 200):
        chunk = ",".join('"' + g.replace('"', '') + '"' for g in gone[i:i + 200])
        rest("PATCH", base, key, f"events?id=in.({chunk})", {"hidden": True, "hide_reason": "expired"}, "return=minimal")
    rest("PATCH", base, key, f"cities?id=eq.{city}", {"data_updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}, "return=minimal")
    return len(gone)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--locations")
    ap.add_argument("--city", default="ar-buenos-aires")
    ap.add_argument("--today", default=dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date().isoformat())
    ap.add_argument("--subcats", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed.sql"))
    ap.add_argument("--sql")
    a = ap.parse_args()
    valid_sub = set(re.findall(r"subcategories \(category,id,name_en,name_es,sort\) values \('(\w+)','(\w+)'", open(a.subcats).read()))
    locs = {}
    if a.locations:
        for f in glob.glob(os.path.join(a.locations, "*.json")):
            locs.update((json.load(open(f)).get("map") or {}))
    docs = [json.load(open(f)) for f in glob.glob(os.path.join(a.events, "*.json"))]
    cur = [d for d in docs if d.get("id") and d.get("title") and is_current(d, a.today)]
    rows = [to_row(d, a.city, locs, valid_sub) for d in cur]
    venues = venue_rows(rows, a.city)
    if a.sql:
        write_sql(a.sql, venues, rows)
        print(f"{len(rows)} current listings, {len(venues)} venues -> {a.sql}")
    else:
        hidden = write_supabase(venues, rows, a.city)
        print(f"{len(rows)} current listings and {len(venues)} venues sent; {hidden} no-longer-current listings hidden")

if __name__ == "__main__":
    main()
