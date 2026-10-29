"""MOVEO daily pipeline: everything the Claude scheduled tasks did, on GitHub Actions + Supabase.

  python3 -m pipeline.run all            cleanup, harvest, blog, datecheck, map, artwork, sync (in that order)
  python3 -m pipeline.run harvest        (or any one step: cleanup harvest blog datecheck map artwork sync)
  python3 -m pipeline.run status         document counts per collection
  python3 -m pipeline.run import --dir DUMP [--only-missing]
                                         one-time copy of the Claude app database (folders of <id>.json per collection)
Options: --dry-run (read everything, run everything, write nothing; shows what would change), --today YYYY-MM-DD.

Steps (each is independent; one failing never stops the others):
  cleanup   delete listings that are over (ended/cancelled, run ended 3+ days ago, every date 3+ days ago),
            never anything with a reaction, never an open-ended place                     [date check step 2a]
  harvest   organizer directory (rebuilt monthly, agenda pages discovered daily), ticket platforms and
            agendas -> new listings, extra dates, the far-ahead "Don't Forget" list      [harvester]
  blog      blog/guide listings -> concrete postings linking to the original page        [date check part 2]
  datecheck listings' dates checked against the page they link to                        [date check part 1]
  map       pins for every current listing                                               [map]
  artwork   a real cover picture for listings without one, stored in Supabase Storage    [artwork]
  sync      current listings copied to the website's tables (only when WEBSITE_SYNC=on)  [website sync]
"""
import argparse, datetime as dt, glob, json, os, shutil, subprocess, sys, time, traceback
from collections import Counter

from . import llm
from .store import Collection, Store, merge

HERE = os.path.dirname(os.path.abspath(__file__))
LEGACY = os.path.join(HERE, "legacy")
BA = dt.timezone(dt.timedelta(hours=-3))
WORK = os.path.abspath(os.environ.get("MOVEO_WORK", "work"))
CODE_DOCS = {"code", "dircode", "artwork", "geocode", "sync", "blogreader"}   # old Claude-era code copies: not needed


def sh(args, timeout, cwd=WORK):
    """Run a legacy script; its output goes to the log. Returns True if it finished."""
    print("\n$ " + " ".join(args), flush=True)
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable] + args, cwd=cwd, timeout=timeout)
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"(stopped after {timeout}s; keeping what it produced)")
        ok = False
    print(f"({round(time.time() - t0)}s)", flush=True)
    return ok


def jload(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def fresh(*sub):
    p = os.path.join(WORK, *sub)
    shutil.rmtree(p, ignore_errors=True)
    os.makedirs(p, exist_ok=True)
    return p


class Ctx:
    def __init__(self, store, today):
        self.st, self.today = store, today
        self.T = today.isoformat()
        self._events = None

    @property
    def events(self):
        if self._events is None:
            self._events = Collection(self.st, "events")
        return self._events

    def export_events(self):
        return self.events.export(os.path.join(WORK, "db", "events"))

    def fx(self):
        m = self.st.get("settings", "meta") or {}
        try:
            return float(m.get("fx")) if m.get("fx") else None
        except (TypeError, ValueError):
            return None

    def directory_file(self):
        """Join the stored directory chunks into out/directory.json (what every script reads)."""
        os.makedirs(os.path.join(WORK, "out"), exist_ok=True)
        path = os.path.join(WORK, "out", "directory.json")
        if os.path.exists(path):
            return path
        docs = self.st.list("directory")
        meta = docs.get("meta") or {}
        s = "".join((docs.get(f"part{k}") or {}).get("s", "") for k in range(int(meta.get("chunks") or 0)))
        with open(path, "w") as f:
            f.write(s or "[]")
        return path


# ---------------------------------------------------------------------- steps
def step_cleanup(c):
    ev = c.events
    reacted = set(c.st.list("reactions", ids_only=True))
    cut = (c.today - dt.timedelta(days=3)).isoformat()
    gone = []
    for i, d in ev.docs.items():
        if i in reacted:
            continue
        ds = d.get("dates") or []
        if (d.get("verify") in ("ended", "cancelled") or (d.get("end") and d["end"] < cut)
                or (d.get("kind") == "event" and ds and all(x < cut for x in ds))):
            gone.append(i)
    for i in gone:
        del ev.docs[i]
    res = ev.save()
    return {"deleted": res["deleted"], "kept_for_reactions": len(reacted)}


def write_chunks(c, collection, folder):
    """Store out/<folder>/part*.json + meta.json as a chunked collection; meta written last."""
    files = sorted(glob.glob(os.path.join(folder, "part*.json")))
    if not files:
        return 0
    col = Collection(c.st, collection)
    new = {os.path.basename(f)[:-5]: jload(f) for f in files}
    for k in [k for k in col.docs if k.startswith("part") and k not in new]:
        del col.docs[k]
    col.docs.update(new)
    col.save()
    c.st.put(collection, {"meta": jload(os.path.join(folder, "meta.json"))})
    return len(files)


def step_harvest(c):
    out = {}
    fresh("out", "new"); fresh("out", "extend")
    c.export_events()
    fut = Collection(c.st, "future")
    fut.export(os.path.join(WORK, "db", "future"))
    py = lambda *a: [os.path.join(LEGACY, a[0])] + list(a[1:])

    # --- organizer directory: rebuild monthly, discover agenda pages daily
    dirs = c.st.list("directory")
    meta = dirs.get("meta") or {}
    prev = "".join((dirs.get(f"part{k}") or {}).get("s", "") for k in range(int(meta.get("chunks") or 0)))
    with open(os.path.join(WORK, "out", "directory_prev.json"), "w") as f:
        f.write(prev or "[]")
    age = 999
    try:
        age = (c.today - dt.date.fromisoformat(meta["updatedAt"][:10])).days
    except Exception:
        pass
    rebuilt = False
    if age >= 30 or not prev:
        rebuilt = sh(py("directory.py", "build", "--prev", "out/directory_prev.json", "--events", "db/events"), 2700)
    if not rebuilt:
        shutil.copy(os.path.join(WORK, "out", "directory_prev.json"), os.path.join(WORK, "out", "directory.json"))
    sh(py("directory.py", "discover", "--limit", "300"), 1200)
    sh(py("directory.py", "chunks"), 300)
    out["directory_rebuilt"] = rebuilt
    out["directory_chunks"] = write_chunks(c, "directory", os.path.join(WORK, "out", "dirchunks"))
    if rebuilt:
        write_chunks(c, "dirgeo", os.path.join(WORK, "out", "geochunks"))
    out["directory_report"] = {k: v for k, v in (jload(os.path.join(WORK, "out", "dir_report.json"), {}) or {}).items() if k != "log"} if rebuilt else None

    # --- harvest + far-ahead list + build
    sh(py("harvester.py", "harvest", "--window", "7"), int(os.environ.get("HARVEST_SECONDS", "2400")))
    report = jload(os.path.join(WORK, "out", "report.json"), {}) or {}
    if os.path.exists(os.path.join(WORK, "out", "future_candidates.json")):
        if sh(py("harvester.py", "future", "--existing", "db/future"), 600):
            new_f = {os.path.basename(f)[:-5]: jload(f) for f in glob.glob(os.path.join(WORK, "out", "future", "*.json")) if not f.endswith("_delete.json")}
            for k in jload(os.path.join(WORK, "out", "future", "_delete.json"), []) or []:
                fut.docs.pop(k, None)
            fut.docs.update(new_f)
            out["future_docs"] = fut.save()
    fx = c.fx()
    sh(py("harvester.py", "build", "--existing", "db/events") + (["--fx", str(fx)] if fx else []), 900)

    # --- review (Claude API if a key is set, else rules) and write
    review = jload(os.path.join(WORK, "out", "review.json"), []) or []
    fixes, how = llm.review(review, "harvest")
    with open(os.path.join(WORK, "out", "fixes.json"), "w") as f:
        json.dump(fixes, f, ensure_ascii=False)
    sh(py("harvester.py", "apply-review", "out/fixes.json"), 300)
    added, by_genre, by_source = 0, Counter(), Counter()
    for f in glob.glob(os.path.join(WORK, "out", "new", "*.json")):
        d = jload(f)
        if d and d.get("id") and d["id"] not in c.events.docs:
            c.events.docs[d["id"]] = d
            added += 1; by_genre[d.get("genre")] += 1; by_source[(d.get("source") or "").split("+")[0]] += 1
    extended = 0
    for f in glob.glob(os.path.join(WORK, "out", "extend", "*.json")):
        x = jload(f)
        d = c.events.docs.get((x or {}).get("id"))
        if d and x.get("dates"):
            d["dates"] = x["dates"]; extended += 1
    c.events.save()

    # --- status doc (same shape as before)
    state = c.st.get("harvest", "state") or {}
    srcs = state.get("sources") or {}
    for name, rep in report.items():
        old = srcs.get(name) or {}
        log = rep.get("log") or []
        srcs[name] = {"count": rep.get("count", 0), "lastOk": c.T if rep.get("count") else old.get("lastOk"),
                      "notes": (log[-1] if isinstance(log, list) and log else str(log))[:160]}
    zero = sorted(n for n, r in report.items() if not r.get("count"))
    run = {"date": c.T, "candidates": sum(r.get("count", 0) for r in report.values()), "added": added,
           "extended": extended, "deleted": 0, "repaired": [], "by": "pipeline", "review": how}
    state.update(sources=srcs, updatedAt=dt.datetime.now(BA).isoformat(timespec="seconds"),
                 runs=((state.get("runs") or []) + [run])[-30:],
                 notes=f"{c.T}: pipeline run; {added} added ({how} review), {extended} extended; {len(zero)} sources at 0.")
    c.st.put("harvest", {"state": state})
    out.update(added=added, extended=extended, review=how, reviewed=len(review), dropped=sum(1 for v in fixes.values() if v.get("drop")),
               by_genre=dict(by_genre.most_common()), top_sources=dict(by_source.most_common(8)), sources_at_zero=zero)
    return out


def step_blog(c):
    c.export_events()
    dfile = c.directory_file()
    ob = fresh("out_blog")
    fx = c.fx()
    sh([os.path.join(LEGACY, "blogreader.py"), "run", "--events", "db/events", "--directory", dfile, "--out", ob,
        "--cache", os.path.join(WORK, "cache_blog"), "--max-listings", "400", "--minutes", os.environ.get("BLOG_MINUTES", "22"),
        "--browser", "--new"] + (["--fx", str(fx)] if fx else []), 1800)
    rep = jload(os.path.join(ob, "blog_report.json"), {}) or {}
    updated = 0
    for f in glob.glob(os.path.join(ob, "updates", "*.json")):
        i = os.path.basename(f)[:-5]
        if i in c.events.docs:
            merge(c.events.docs[i], jload(f, {}))
            updated += 1
    # new postings from the same posts: Claude (if a key is set) writes the line and checks they're real
    new_rev = jload(os.path.join(ob, "blog_new_review.json"), []) or []
    fixes, how = llm.review(new_rev, "blog")
    added = 0
    for f in glob.glob(os.path.join(ob, "new", "*.json")):
        d = jload(f)
        if not d or d["id"] in c.events.docs:
            continue
        fx_ = fixes.get(d["id"], {})
        if fx_.get("drop"):
            continue
        for k in ("desc", "genre", "title", "venue"):
            if fx_.get(k):
                d[k] = fx_[k]
        if how != "claude":
            # without a reviewer: only postings whose original page was found, with a plain description
            if d.get("linkType") not in ("event", "venue", "instagram"):
                continue
            g = {"music": "Live music", "nightlife": "Night out", "theater": "Show", "tango": "Tango", "film": "Screening/talk",
                 "classes": "Workshop", "events": "Event", "food": "Food & drink", "markets": "Market", "galleries": "Art",
                 "museums": "Museum", "outdoor": "Outdoor"}.get(d.get("genre"), "Event")
            d["desc"] = d.get("desc") or f"{g} at {d.get('venue') or 'see page'}"[:110]
        if not d.get("desc"):
            continue
        c.events.docs[d["id"]] = d
        added += 1
    c.events.save()
    links = {k[5:]: v for k, v in rep.items() if k.startswith("link_")}
    return {"posts_read": rep.get("articles", 0), "listings_checked": rep.get("listings_done", 0), "updated": updated,
            "links": links, "duplicates_hidden": rep.get("duplicates_of_platform_listing", 0), "new_postings": added,
            "new_review": how, "left_for_next_run": rep.get("listings_left", 0)}


def step_datecheck(c):
    c.export_events()
    outf = os.path.join(WORK, "out", "datecheck.json")
    os.makedirs(os.path.dirname(outf), exist_ok=True)
    sh(["-m", "pipeline.datecheck", "--events", os.path.join(WORK, "db", "events"), "--out", outf, "--today", c.T,
        "--max", os.environ.get("DATECHECK_MAX", "400"), "--minutes", os.environ.get("DATECHECK_MINUTES", "25")],
       2400, cwd=os.path.dirname(HERE))
    res = jload(outf, {}) or {}
    n = 0
    examples = []
    for i, p in (res.get("updates") or {}).items():
        d = c.events.docs.get(i)
        if not d:
            continue
        if p.get("verify") == "fixed" and len(examples) < 5:
            examples.append(f"{d.get('title', '')[:50]}: {p.get('verifyNote', '')}")
        merge(d, p)
        n += 1
    c.events.save()
    return dict(res.get("report") or {}, written=n, top_fixes=examples)


def step_map(c):
    c.export_events()
    dfile = c.directory_file()
    loc = Collection(c.st, "locations")
    loc.export(os.path.join(WORK, "db", "locations"))
    fresh("out", "locations")
    sh([os.path.join(LEGACY, "geocode.py"), "run", "--events", "db/events", "--directory", dfile, "--locations", "db/locations",
        "--online", "--max-lookups", "400", "--today", c.T], 1800)
    files = {os.path.basename(f)[:-5]: jload(f) for f in glob.glob(os.path.join(WORK, "out", "locations", "*.json"))}
    if not files:
        return {"error": "map produced nothing; pins left as they were"}
    for k in [k for k in loc.docs if k.startswith("part") and k not in files]:
        del loc.docs[k]
    loc.docs.update(files)
    saved = loc.save()
    rep = jload(os.path.join(WORK, "out", "geo_report.json"), {}) or {}
    return dict({k: v for k, v in rep.items() if not isinstance(v, (list, dict))}, docs=saved)


def step_artwork(c):
    c.export_events()
    dfile = c.directory_file()
    for f in glob.glob(os.path.join(WORK, "out", "art", "*")):
        os.remove(f)
    sh([os.path.join(LEGACY, "artwork.py"), "pick", "--events", "db/events", "--directory", dfile, "--max", "400",
        "--upgrade", "150", "--minutes", os.environ.get("ARTWORK_MINUTES", "35"), "--today", c.T], 3000)
    man = jload(os.path.join(WORK, "out", "art_manifest.json"), []) or []
    stored, old, by = 0, [], Counter()
    for m in man:
        d = c.events.docs.get(m["id"])
        if not d or not os.path.exists(os.path.join(WORK, m["file"])):
            continue
        path = f"events/{m['id']}-{(m.get('imgHash') or 'x')[:10]}.jpg"
        try:
            with open(os.path.join(WORK, m["file"]), "rb") as f:
                url = c.st.upload("covers", path, f.read())
        except Exception as ex:
            print("upload failed:", m["id"], str(ex)[:120])
            continue
        if (d.get("imgId") or "").startswith("events/") and d["imgId"] != path:
            old.append(d["imgId"])
        d.update(img=url, imgId=path, imgSource=m["imgSource"], imgHash=m.get("imgHash"), imgAddedAt=c.T)
        stored += 1; by[m["imgSource"]] += 1
    c.events.save()
    try:
        c.st.remove("covers", old)
    except Exception as ex:
        print("could not remove replaced pictures:", str(ex)[:120])
    return {"pictures_stored": stored, "by_source": dict(by), "replaced": len(old)}


def step_sync(c):
    if os.environ.get("WEBSITE_SYNC", "off").lower() not in ("on", "true", "1", "yes"):
        return {"skipped": "WEBSITE_SYNC is off (the Claude website sync task still feeds the website)"}
    c.export_events()
    loc = Collection(c.st, "locations")
    loc.export(os.path.join(WORK, "db", "locations"))
    args = [os.path.join(LEGACY, "sync.py"), "--events", "db/events", "--locations", "db/locations", "--today", c.T]
    if c.st.dry:
        args += ["--sql", "out/website.sql"]
    ok = sh(args, 1200)
    return {"ok": ok}


STEPS = {"cleanup": step_cleanup, "harvest": step_harvest, "blog": step_blog, "datecheck": step_datecheck,
         "map": step_map, "artwork": step_artwork, "sync": step_sync}
ORDER = ["cleanup", "harvest", "blog", "datecheck", "map", "artwork", "sync"]


# ---------------------------------------------------------------------- one-time copy + status
def cmd_import(st, folder, only_missing):
    tot = {}
    for sub in sorted(os.listdir(folder)):
        p = os.path.join(folder, sub)
        if not os.path.isdir(p):
            continue
        docs = {}
        for f in glob.glob(os.path.join(p, "*.json")):
            i = os.path.basename(f)[:-5]
            if sub == "harvest" and i in CODE_DOCS:
                continue
            d = jload(f)
            if isinstance(d, dict) and set(d) >= {"data"} and isinstance(d["data"], dict) and len(d) <= 4:
                d = d["data"]                      # a wrapped list result
            if d is not None:
                docs[i] = d
        if only_missing:
            have = st.list(sub, ids_only=True)
            docs = {i: d for i, d in docs.items() if i not in have}
        st.put(sub, docs)
        tot[sub] = len(docs)
        print(f"{sub:12s} {len(docs):6d} documents copied", flush=True)
    return tot


def cmd_status(st):
    out = {}
    for col in ("events", "reactions", "settings", "harvest", "directory", "dirgeo", "locations", "future", "runs"):
        out[col] = len(st.list(col, ids_only=True))
    print(json.dumps(out, indent=1))
    return out


def summary_md(today, results, dry):
    lines = [f"## MOVEO pipeline {today}" + (" (dry run: nothing written)" if dry else ""), ""]
    for name, r in results.items():
        lines.append(f"**{name}**: " + ("FAILED: " + r["error"] if "error" in r and len(r) == 1 else
                                        "; ".join(f"{k} {json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v}" for k, v in r.items())))
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=ORDER + ["all", "status", "import"])
    ap.add_argument("--steps", help="comma list, e.g. harvest,blog (with 'all')")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--today")
    ap.add_argument("--dir")
    ap.add_argument("--only-missing", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run or os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes", "on")
    st = Store(dry_run=dry)
    if a.cmd == "status":
        return cmd_status(st)
    if a.cmd == "import":
        return cmd_import(st, a.dir, a.only_missing)
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(BA).date()
    os.makedirs(WORK, exist_ok=True)
    c = Ctx(st, today)
    steps = ORDER if a.cmd == "all" else [a.cmd]
    if a.steps:
        steps = [s.strip() for s in a.steps.split(",") if s.strip() in STEPS]
    results, failed = {}, []
    for s in steps:
        print(f"\n===== {s} =====", flush=True)
        t0 = time.time()
        try:
            results[s] = STEPS[s](c)
        except Exception as ex:
            traceback.print_exc()
            results[s] = {"error": f"{type(ex).__name__}: {str(ex)[:300]}"}
            failed.append(s)
            c._events = None                       # reload fresh for the next step
        results[s]["minutes"] = round((time.time() - t0) / 60, 1)
        print(json.dumps(results[s], ensure_ascii=False, default=str), flush=True)
    md = summary_md(c.T, results, dry)
    print("\n" + md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md + "\n")
    if not dry:
        runs = st.get("runs", c.T) or {}
        runs.update({s: dict(r, at=dt.datetime.now(BA).isoformat(timespec="minutes")) for s, r in results.items()})
        st.put("runs", {c.T: json.loads(json.dumps(runs, default=str))})
    print(f"\nwrites {st.writes}, deletes {st.deletes}" + (" (dry run: none sent)" if dry else ""))
    if failed:
        sys.exit(f"failed steps: {', '.join(failed)}")


if __name__ == "__main__":
    main()
