"""On The Move: find a REAL picture for every current listing.

Order of preference (first one that works wins):
  1. hint     the image the source itself published with the listing (imgHint, written by the harvester)
  2. page     the listing's own page: JSON-LD image, else the first photo of the post body, else og:image.
              On roundup articles (Time Out, Infobae, ...) only the photo of THIS item's section, never the lead photo.
  3. venue    the venue's own website (from the organizer directory, or the listing's url host if it is the venue's site)
  4. (blog pictures are never downloaded: the listing only keeps their web address in imgBackup, owner 2026-10-27)
  5. place    a Wikimedia Commons photo whose file name actually names this venue (a photo of the real place)
  6. stock    a Wikimedia Commons photo of the topic (last resort; these get retried every run)
imgSource is "original" for 1-3, "blog" for 4, "venue" for 5, "stock" for 6.
Steps 1-3 always look at the ORIGINAL page (the listing's url) before any blog picture is used.

Every run also retries up to --upgrade listings that still have a stock or blog picture, newest first, so a blog picture
is replaced as soon as the original page publishes one.

  python3 artwork.py pick --events db/events --directory db/directory [--max 400] [--upgrade 150] [--today YYYY-MM-DD]
  -> out/art/<eventId>.jpg and out/art_manifest.json: [{id, file, imgSource, imgHash, via, replaces}]
     "replaces" is the old imgId when an existing stock picture is being upgraded (delete that asset after writing).
"""
import argparse, concurrent.futures as cf, datetime as dt, glob, hashlib, io, json, os, re, sys, threading, time, unicodedata
from urllib.parse import urljoin, urlparse, quote

import requests
from bs4 import BeautifulSoup
from PIL import Image

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/127.0 Safari/537.36")
H = {"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9,en;q=0.8"}
MIN_W, MIN_H = 480, 300
BAD_URL = re.compile(r"logo|icon|avatar|sprite|placeholder|default|blank|spacer|pixel|badge|favicon|banner-app|share-|whatsapp|facebook|twitter|instagram|gravatar|emoji|/flags?/|qr", re.I)
ROUNDUP = re.compile(r"timeout\.com|infobae\.com|quehacemos\.com\.ar/(?!eventos/)|quepasaen\.com|malevamag|lanacion\.com\.ar|clarin\.com|pagina12\.com\.ar|perfil\.com|cronista\.com|buenosairessecreta|forbesargentina|bacanal|inforgastronomica|infogastronomica|vanidades|rollingstone", re.I)
STOP = set("""de del la el los las y en a al con para por un una the of and at in on for with to from bar club
 teatro centro cultural espacio sala casa buenos aires caba ciudad nuevo nueva new show live en vivo""".split())
STOCK_TOPIC = {
    "music": "concierto banda en vivo Buenos Aires", "nightlife": "Buenos Aires bar night", "tango": "tango milonga dancers Buenos Aires",
    "theater": "teatro Buenos Aires escenario", "museums": "museo Buenos Aires sala", "galleries": "art gallery exhibition Buenos Aires",
    "newplaces": "restaurant interior Buenos Aires", "food": "restaurant Buenos Aires food", "markets": "feria Buenos Aires puestos",
    "film": "cine sala Buenos Aires", "classes": "workshop class Buenos Aires", "outdoor": "Buenos Aires park people", "events": "conference hall Buenos Aires",
}
lock = threading.Lock()
S = requests.Session(); S.headers.update(H)


def fold(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def words(s):
    return [w for w in re.findall(r"[a-z0-9]{3,}", fold(s)) if w not in STOP]


def fetch(url, timeout=12):
    try:
        r = S.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return r
    except Exception:
        pass
    return None


class Picker:
    def __init__(self, used_hashes, directory):
        self.used = set(used_hashes)
        self.by_name = {}
        for o in directory or []:
            k = " ".join(words(o.get("n")))
            if k and o.get("w"):
                self.by_name.setdefault(k, o["w"])
        self.stats = {}

    # ---------------------------------------------------------- image download + checks
    def take(self, url, referer=None):
        if not url or url.startswith("data:") or url.lower().split("?")[0].endswith(".svg") or BAD_URL.search(url):
            return None
        try:
            r = S.get(url, timeout=15, headers={"Referer": referer} if referer else None)
            if r.status_code != 200 or len(r.content) < 12000:
                return None
            raw = r.content
            h = hashlib.sha256(raw).hexdigest()
            with lock:
                if h in self.used:
                    return None
            im = Image.open(io.BytesIO(raw))
            w, hh = im.size
            if w < MIN_W or hh < MIN_H or w / hh > 3.2 or w / hh < 0.45:
                return None
            im = im.convert("RGB")
            if w > 1080:
                im = im.resize((1080, round(hh * 1080 / w)))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=80, optimize=True)
            with lock:
                if h in self.used:
                    return None
                self.used.add(h)
            return buf.getvalue(), h
        except Exception:
            return None

    # ---------------------------------------------------------- page images
    def page_images(self, url, e):
        r = fetch(url)
        if not r or "html" not in r.headers.get("content-type", ""):
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        # JSON-LD image of an Event / Place / Restaurant
        for sc in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(sc.string or "")
            except Exception:
                continue
            for d in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
                if isinstance(d, dict) and d.get("image"):
                    im = d["image"]
                    im = im[0] if isinstance(im, list) else im
                    im = im.get("url") if isinstance(im, dict) else im
                    if isinstance(im, str):
                        out.append(urljoin(r.url, im))
        roundup = bool(ROUNDUP.search(r.url))
        body = soup.find("article") or soup.find("main") or soup.body or soup
        for tag in body.find_all(["header", "nav", "footer", "aside", "form"]):
            tag.decompose()

        def src_of(img):
            for a in ("data-src", "data-lazy-src", "data-original", "src"):
                v = img.get(a)
                if v and not v.startswith("data:"):
                    return urljoin(r.url, v)
            ss = img.get("srcset") or img.get("data-srcset")
            if ss:
                best = max((c.strip().split(" ") for c in ss.split(",") if c.strip()), key=lambda c: int(re.sub(r"\D", "", c[1]) or 0) if len(c) > 1 else 0)
                return urljoin(r.url, best[0])
            return None

        heads = body.find_all(["h2", "h3"])
        if roundup or len(heads) >= 3:
            # a page that lists several items: find THIS item's heading and take the photo of its section
            want = set(words(e.get("title"))) | set(words((e.get("venue") or "").split(",")[0]))
            for hd in body.find_all(["h2", "h3", "h4", "strong"]):
                if len(want & set(words(hd.get_text(" ")))) >= min(2, len(want)):
                    near = []
                    for sib in list(hd.find_all_next(limit=25)):
                        if sib.name in ("h2", "h3") and sib is not hd:
                            break
                        if sib.name == "img":
                            near.append(src_of(sib))
                    prev = hd.find_previous("img")
                    if prev:
                        near.append(src_of(prev))
                    return [u for u in near if u] + out
            if roundup:
                return out  # never the article's lead photo
            h1 = soup.find("h1") or soup.find("title")
            if not h1 or len(set(words(e.get("title"))) & set(words(h1.get_text(" ")))) < min(2, len(set(words(e.get("title"))))):
                return out  # a list page about something else: its photos are not this listing's
        for img in body.find_all("img", limit=12):
            u = src_of(img)
            if u:
                out.append(u)
        for prop in ("og:image", "twitter:image", "og:image:url"):
            m = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
            if m and m.get("content"):
                out.append(urljoin(r.url, m["content"]))
        return out

    # ---------------------------------------------------------- commons
    def commons(self, query, must=None, limit=20):
        r = fetch("https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrnamespace=6&gsrsearch="
                  + quote(query) + f"&gsrlimit={limit}&prop=imageinfo&iiprop=url|size&iiurlwidth=1080&format=json", timeout=15)
        if not r:
            return []
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        out = []
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("width", 0) < 800 or not re.search(r"\.(jpe?g|png|webp)$", p.get("title", ""), re.I):
                continue
            if must and not must.issubset(set(words(p.get("title")))):
                continue
            out.append(ii.get("thumburl") or ii.get("url"))
        return out

    # ---------------------------------------------------------- the chain
    def pick(self, e, allow_stock=True, allow_blog=True):
        venue = (e.get("venue") or "").split(",")[0].strip()
        tried = []
        steps = [("hint", lambda: [e["imgHint"]] if e.get("imgHint") else [])]
        if e.get("url") and not re.search(r"instagram\.com|facebook\.com", e["url"]):
            steps.append(("page", lambda: self.page_images(e["url"], e)))
        site = self.by_name.get(" ".join(words(venue)))
        if site and (not e.get("url") or urlparse(site).netloc not in e.get("url", "")):
            steps.append(("venue", lambda: self.page_images(site if site.startswith("http") else "https://" + site, {"title": venue, "venue": venue})))
        # Blog/roundup pictures are NEVER downloaded or stored (owner, 2026-10-27): the listing keeps only the
        # picture's web address in imgBackup, and the website shows it from there when nothing else exists.
        vw = set(words(venue))
        if len(vw) >= 1 and len(" ".join(vw)) >= 5:
            steps.append(("place", lambda: self.commons(f'"{venue}" Buenos Aires', must=vw)))
        if allow_stock:
            topic = " ".join(words(e.get("title"))[:3])
            steps.append(("stock", lambda: self.commons(f"{topic} Buenos Aires") + self.commons(STOCK_TOPIC.get(e.get("genre"), "Buenos Aires"))))
        for via, fn in steps:
            try:
                urls = fn()
            except Exception:
                urls = []
            for u in urls[:8]:
                if u in tried:
                    continue
                tried.append(u)
                got = self.take(u, referer=e.get("url"))
                if got:
                    src = {"hint": "original", "page": "original", "venue": "original", "blog": "blog", "place": "venue", "stock": "stock"}[via]
                    with lock:
                        self.stats[via] = self.stats.get(via, 0) + 1
                    return got, src, via
        return None


def load(path):
    out = []
    for f in glob.glob(os.path.join(path, "*.json")):
        d = json.load(open(f))
        out.append(d.get("data", d) if isinstance(d.get("data"), dict) else d)
    return out


def load_dir(path):
    if not path or not os.path.exists(path):
        return []
    if os.path.isdir(path):
        parts = sorted(glob.glob(os.path.join(path, "part*.json")), key=lambda f: int(re.search(r"part(\d+)", f).group(1)))
        return json.loads("".join((json.load(open(f)).get("data") or json.load(open(f))).get("s", "") for f in parts))
    return json.load(open(path))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["pick"])
    ap.add_argument("--events", required=True); ap.add_argument("--directory")
    ap.add_argument("--max", type=int, default=400); ap.add_argument("--upgrade", type=int, default=150)
    ap.add_argument("--today"); ap.add_argument("--minutes", type=float, default=35)
    a = ap.parse_args()
    today = a.today or dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).date().isoformat()
    E = load(a.events)

    def current(e):
        if e.get("verify") in ("ended", "cancelled"):
            return False
        if e.get("kind") == "event":
            return any(x >= today for x in e.get("dates") or [])
        return not (e.get("end") and e["end"] < today)

    cur = [e for e in E if current(e)]
    order = {g: i for i, g in enumerate(["music", "nightlife", "newplaces", "food", "events", "tango", "theater", "outdoor", "markets", "galleries", "museums", "film", "classes"])}
    missing = sorted([e for e in cur if not e.get("img")], key=lambda e: (-(int((e.get("addedAt") or "0").replace("-", "") or 0)), order.get(e["genre"], 9)))[: a.max]
    stock = sorted([e for e in cur if e.get("img") and e.get("imgSource") in ("stock", "blog")], key=lambda e: e.get("addedAt") or "", reverse=True)[: a.upgrade]
    P = Picker([e.get("imgHash") for e in E if e.get("imgHash")], load_dir(a.directory))
    os.makedirs("out/art", exist_ok=True)
    manifest, deadline = [], time.time() + a.minutes * 60

    def work(job):
        e, upgrade = job
        if time.time() > deadline:
            return None
        # an upgrade only counts if it's better: a blog picture is replaced only by an original one
        res = P.pick(e, allow_stock=not upgrade, allow_blog=not (upgrade and e.get("imgSource") == "blog"))
        if res and upgrade and res[1] in ("stock", "venue") and e.get("imgSource") == "blog":
            return None
        if not res:
            return None
        (jpg, h), src, via = res
        f = f"out/art/{e['id']}.jpg"
        open(f, "wb").write(jpg)
        return {"id": e["id"], "file": f, "imgSource": src, "imgHash": h, "via": via, "replaces": e.get("imgId") if upgrade else None}

    jobs = [(e, False) for e in missing] + [(e, True) for e in stock]
    with cf.ThreadPoolExecutor(8) as ex:
        for m in ex.map(work, jobs):
            if m:
                manifest.append(m)
    json.dump(manifest, open("out/art_manifest.json", "w"), indent=1)
    rep = {"current": len(cur), "had_no_picture": len(missing), "stock_retried": len(stock),
           "found": len(manifest), "by_step": P.stats,
           "upgraded_from_stock": sum(1 for m in manifest if m["replaces"])}
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
