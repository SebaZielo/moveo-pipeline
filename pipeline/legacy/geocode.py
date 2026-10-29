"""On The Move: put every current listing on the map, and check the pins we already have.

How a listing gets its pin (first rule that works wins; every result must sit inside the
listing's stated neighborhood or town before it is accepted):
  1. its street address matches a venue in the organizer directory (city open data, Mapa Cultural, OSM)
  2. its venue name matches a directory venue
  3. another listing at the same venue already has a checked pin
  4. (online) its street address, via the city's official geocoder (USIG) for CABA, else the national Georef API
  5. (online) its venue name, via OpenStreetMap Nominatim, then Photon; the place found must carry the same name

Output is the app's "locations" collection: docs part0..N, each {"map": {eventId: [lat, lng, conf]}}.
conf is "h" (address or name+neighborhood agree) or "m" (name match only). [null, null, "x"] marks a
coordinate stored on the event itself that is wrong (far from its neighborhood): the app ignores it.
Lookups are cached in out/venuecache.json (saved to locations/venues) so a venue is only looked up once.

Usage:
  python3 geocode.py run --events db/events --directory out/directory.json --locations db/locations \
      [--online] [--max-lookups 400] [--today YYYY-MM-DD]
"""
import argparse, datetime, glob, json, math, os, re, sys, time
from collections import defaultdict

try:
    from unidecode import unidecode
except ImportError:  # pip install --break-system-packages unidecode rapidfuzz
    import unicodedata
    def unidecode(s):
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
from rapidfuzz import fuzz

UA = {"User-Agent": "OnTheMove-personal-app/1.0 (personal events map for Buenos Aires)"}
BOX = (-35.0, -58.95, -34.25, -58.05)  # S, W, N, E
CABA_C = (-34.615, -58.445)
PART_SIZE = 900

# ---------------------------------------------------------------- neighborhoods (centroid, accept radius km)
BARRIOS = {"palermo": [-34.5795, -58.425], "palermo soho": [-34.5885, -58.43], "palermo hollywood": [-34.5835, -58.437], "palermo chico": [-34.578, -58.403], "las canitas": [-34.5685, -58.435], "recoleta": [-34.588, -58.397], "recoleta / retiro": [-34.589, -58.386], "retiro": [-34.592, -58.375], "puerto retiro": [-34.585, -58.37], "san nicolas": [-34.603, -58.379], "centro": [-34.604, -58.381], "microcentro": [-34.602, -58.374], "city center": [-34.604, -58.381], "buenos aires": [-34.604, -58.383], "congreso": [-34.609, -58.392], "monserrat": [-34.612, -58.38], "montserrat": [-34.612, -58.38], "san telmo": [-34.621, -58.372], "la boca": [-34.634, -58.363], "puerto madero": [-34.611, -58.363], "costanera": [-34.615, -58.35], "balvanera": [-34.609, -58.405], "abasto": [-34.6035, -58.411], "almagro": [-34.607, -58.421], "villa crespo": [-34.599, -58.438], "chacarita": [-34.588, -58.455], "colegiales": [-34.575, -58.45], "belgrano": [-34.562, -58.458], "belgrano (barrio chino)": [-34.56, -58.448], "barrio chino / belgrano": [-34.56, -58.448], "nunez": [-34.545, -58.463], "costanera norte": [-34.56, -58.408], "ciudad universitaria": [-34.542, -58.443], "saavedra": [-34.553, -58.487], "coghlan": [-34.56, -58.476], "villa urquiza": [-34.573, -58.487], "villa ortuzar": [-34.581, -58.466], "agronomia": [-34.593, -58.49], "villa devoto": [-34.601, -58.513], "villa devoto / itinerant": [-34.601, -58.513], "villa del parque": [-34.605, -58.49], "villa general mitre": [-34.61, -58.47], "caballito": [-34.618, -58.443], "flores": [-34.628, -58.463], "floresta": [-34.628, -58.483], "villa real": [-34.62, -58.525], "versalles": [-34.631, -58.52], "liniers": [-34.642, -58.521], "mataderos": [-34.657, -58.502], "san cristobal": [-34.624, -58.401], "constitucion": [-34.627, -58.383], "boedo": [-34.63, -58.418], "parque patricios": [-34.637, -58.401], "barracas": [-34.645, -58.383], "nueva pompeya": [-34.65, -58.418], "parque chacabuco": [-34.636, -58.44], "villa soldati": [-34.662, -58.445], "villa lugano": [-34.675, -58.472]}
SUBURBS = {"vicente lopez": [-34.526, -58.475], "florida": [-34.531, -58.493], "florida (vicente lopez)": [-34.531, -58.493], "olivos": [-34.51, -58.488], "martinez": [-34.492, -58.506], "san isidro": [-34.471, -58.528], "acassuso": [-34.48, -58.507], "beccar": [-34.46, -58.537], "san fernando": [-34.442, -58.561], "san fernando (gba)": [-34.442, -58.561], "tigre": [-34.426, -58.58], "tigre (gba)": [-34.426, -58.58], "escobar": [-34.35, -58.796], "avellaneda": [-34.662, -58.365], "lanus": [-34.7, -58.392], "lomas de zamora": [-34.761, -58.406], "banfield": [-34.744, -58.396], "adrogue": [-34.798, -58.39], "adrogue (gba)": [-34.798, -58.39], "quilmes": [-34.72, -58.254], "bernal": [-34.71, -58.28], "bernal oeste": [-34.715, -58.295], "caseros": [-34.605, -58.562], "ramos mejia": [-34.641, -58.565], "haedo": [-34.644, -58.594], "moron": [-34.653, -58.62], "ituzaingo": [-34.658, -58.667], "ituzaingo (gba)": [-34.658, -58.667], "san justo": [-34.683, -58.56], "san justo (la matanza)": [-34.683, -58.56], "tapiales": [-34.702, -58.508], "tapiales (la matanza)": [-34.702, -58.508], "villa udaondo": [-34.618, -58.683], "san martin": [-34.575, -58.537], "pilar": [-34.459, -58.914], "la plata": [-34.921, -57.954]}
ALIAS = {"palermo soho": "palermo", "palermo hollywood": "palermo", "palermo chico": "palermo", "las canitas": "palermo", "centro": "san nicolas", "microcentro": "san nicolas", "city center": "san nicolas", "buenos aires": "san nicolas", "congreso": "balvanera", "abasto": "balvanera", "puerto retiro": "retiro", "recoleta / retiro": "recoleta", "montserrat": "monserrat", "belgrano (barrio chino)": "belgrano", "barrio chino / belgrano": "belgrano", "villa devoto / itinerant": "villa devoto", "costanera norte": "palermo", "once": "balvanera", "barrio norte": "recoleta", "parque centenario": "caballito", "bajo belgrano": "belgrano", "tribunales": "san nicolas", "palermo viejo": "palermo soho"}
CITYWIDE = {"buenos aires", "caba", "ciudad de buenos aires", "capital federal", "capital", "bs as", "city", "ciudad autonoma de buenos aires", "amba", "gba", "argentina"}


def norm(s):
    s = unidecode(str(s or "")).lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


STOP = set("""teatro centro cultural club bar espacio sala la el los las de del y the en at casa complejo
 auditorio galeria museo cc ccc arte bs as buenos aires caba ciudad av avenida""".split())
GENERIC = re.compile(r"\b(see event|venue not confirmed|venue not stated|various|varios|tbd|tba|a confirmar|online|virtual|fixture|secret location|ubicacion secreta|several venues|multiple venues|distintas sedes|consultar|meeting point|citywide)\b")


def core(s):
    return " ".join(t for t in norm(s).split() if t not in STOP)


def km(a, b):
    return math.hypot((a[0] - b[0]) * 111.32, (a[1] - b[1]) * 111.32 * math.cos(math.radians(34.6)))


def inbox(p):
    return BOX[0] <= p[0] <= BOX[2] and BOX[1] <= p[1] <= BOX[3]


def area_of(nb, dir_a2=None):
    """(centroid, radius_km, is_caba) for the listing's stated neighborhood, or None."""
    for raw in (nb, dir_a2):
        n = norm(raw)
        if not n:
            continue
        if n in CITYWIDE:
            return CABA_C, 14.0, True
        for part in [n, re.sub(r"\s*\(.*\)\s*$", "", n)] + [p.strip() for p in re.split(r"[/,]| y ", n)]:
            part = ALIAS.get(part, part)
            if part in BARRIOS:
                return tuple(BARRIOS[part]), 2.6, True
            if part in SUBURBS:
                return tuple(SUBURBS[part]), 6.0, False
    return None


ADDR = re.compile(r"((?:av\.?|avenida|calle|pje\.?|pasaje|bv\.?|boulevard)?\s*[a-záéíóúñü][a-záéíóúñü\.\s]{2,40}?)\s(\d{2,5})\b", re.I)


def clean_street(st):
    st = norm(st)
    st = re.sub(r"^(av|avenida|calle|pje|pasaje|bv|boulevard|gral|general)\s+", "", st)
    return re.sub(r"\s+(av|avenida)$", "", st)


def parse_venue(v, extra_addr=""):
    """-> (name, (street, number) or None, several_venues)"""
    v = str(v or "").strip()
    if " / " in v or v.count("/") == 1 and all(len(x.strip()) > 3 for x in v.split("/")):
        return "", None, True
    parts = [p.strip() for p in re.split(r",|\(|\)| - | – ", v) if p.strip()]
    name, addr = (parts[0] if parts else ""), None
    for p in parts + [extra_addr or ""]:
        m = ADDR.search(p)
        if m and not re.search(r"\b(19|20)\d\d\b", p):
            addr = (clean_street(m.group(1)), int(m.group(2)))
            if p == name:
                name = ""
            break
    return name, addr, False


# ---------------------------------------------------------------- local gazetteer
class Gazetteer:
    def __init__(self, directory):
        self.by_core, self.by_street = defaultdict(list), defaultdict(list)
        for o in directory:
            if not isinstance(o.get("la"), (int, float)) or not inbox((o["la"], o["ln"])):
                continue
            c = core(o.get("n"))
            if len(c) >= 3:
                self.by_core[c].append(o)
            m = ADDR.search(o.get("ad") or "")
            if m:
                self.by_street[clean_street(m.group(1))].append((int(m.group(2)), o))
        self.cores = list(self.by_core)
        self.venue_pts = defaultdict(list)

    def name_cands(self, name):
        c = core(name)
        if len(c) < 4:
            return []
        out = list(self.by_core.get(c, []))
        if not out and len(c) >= 6:
            for k in self.cores:
                if abs(len(k) - len(c)) <= 12 and fuzz.token_set_ratio(c, k) >= 93 and fuzz.ratio(c, k) >= 80:
                    out += self.by_core[k]
        return [((o["la"], o["ln"]), "dir:" + o["n"]) for o in out]

    def addr_cands(self, addr):
        if not addr:
            return []
        st, num = addr
        return [((o["la"], o["ln"]), "dir-addr:" + o["n"]) for n, o in self.by_street.get(st, []) if abs(n - num) <= 40]


def choose(cands, ar):
    """Keep candidates inside the stated area; accept when they agree within 300 m (or the area picks one)."""
    if ar:
        cands = [c for c in cands if km(c[0], ar[0]) <= ar[1]]
    cands = [c for c in cands if inbox(c[0])]
    if not cands:
        return None
    strong = bool(ar) and ar[1] < 10
    if strong:
        cands.sort(key=lambda c: km(c[0], ar[0]))
    first = cands[0][0]
    agree = [c for c in cands if km(c[0], first) <= 0.3]
    if len(agree) < len(cands) and not strong:
        return None
    lat = sum(c[0][0] for c in agree) / len(agree)
    lng = sum(c[0][1] for c in agree) / len(agree)
    return (round(lat, 6), round(lng, 6)), agree[0][1], len(agree), strong


# ---------------------------------------------------------------- online lookups
class Online:
    def __init__(self, cache, max_lookups):
        import requests
        self.s = requests.Session(); self.s.headers.update(UA)
        self.cache, self.left, self.last_nom = cache, max_lookups, 0.0
        self.calls = defaultdict(int)
        self.errored = False

    def _get(self, url, params, slow=False):
        if self.left <= 0:
            return None
        if slow:
            wait = 1.1 - (time.time() - self.last_nom)
            if wait > 0:
                time.sleep(wait)
            self.last_nom = time.time()
        else:
            time.sleep(0.25)
        self.left -= 1
        try:
            r = self.s.get(url, params=params, timeout=20)
            self.calls[url.split("/")[2]] += 1
            if r.status_code != 200:
                self.errored = True
                return None
            return r.json()
        except Exception as ex:
            self.errored = True
            self.calls["failed:" + url.split("/")[2]] += 1
            print("lookup failed", url, str(ex)[:120], file=sys.stderr)
            return None

    def address(self, addr, ar):
        st, num = addr
        out = []
        if not ar or ar[2]:  # CABA (or unknown): the city's own geocoder
            j = self._get("https://servicios.usig.buenosaires.gob.ar/normalizar/", {"direccion": f"{st} {num}", "geocodificar": "TRUE", "srid": "4326"})
            for d in (j or {}).get("direccionesNormalizadas", [])[:3]:
                c = d.get("coordenadas") or {}
                try:
                    out.append(((float(c["y"]), float(c["x"])), "usig:" + d.get("direccion", "")))
                except (KeyError, TypeError, ValueError):
                    pass
        if not out:
            j = self._get("https://apis.datos.gob.ar/georef/api/direcciones", {"direccion": f"{st} {num}", "max": 10})
            for d in (j or {}).get("direcciones", []):
                u = d.get("ubicacion") or {}
                if u.get("lat") is not None:
                    out.append(((float(u["lat"]), float(u["lon"])), "georef:" + d.get("nomenclatura", "")))
        return out

    def name(self, name, ar):
        c = core(name)
        if len(c) < 4:
            return []
        q = name + (", Buenos Aires" if "buenos aires" not in norm(name) else "")
        out = []
        j = self._get("https://nominatim.openstreetmap.org/search", {"format": "jsonv2", "q": q, "countrycodes": "ar", "viewbox": f"{BOX[1]},{BOX[2]},{BOX[3]},{BOX[0]}", "bounded": 1, "limit": 6, "namedetails": 1}, slow=True)
        for r in j or []:
            nm = (r.get("namedetails") or {}).get("name") or (r.get("display_name") or "").split(",")[0]
            if nm and (fuzz.token_set_ratio(c, core(nm)) >= 86 or fuzz.partial_ratio(c, core(nm)) >= 92):
                out.append(((float(r["lat"]), float(r["lon"])), "osm:" + nm))
        if not out:
            j = self._get("https://photon.komoot.io/api/", {"q": q, "limit": 6, "bbox": f"{BOX[1]},{BOX[0]},{BOX[3]},{BOX[2]}"})
            for f in (j or {}).get("features", []):
                nm = (f.get("properties") or {}).get("name") or ""
                lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
                if lat is not None and nm and (fuzz.token_set_ratio(c, core(nm)) >= 86 or fuzz.partial_ratio(c, core(nm)) >= 92):
                    out.append(((float(lat), float(lon)), "photon:" + nm))
        return out


# ---------------------------------------------------------------- main
def load_docs(path):
    out = []
    for f in glob.glob(os.path.join(path, "*.json")):
        d = json.load(open(f))
        out.append(d.get("data", d) if isinstance(d.get("data"), dict) else d)
    return out


def load_directory(path):
    if os.path.isdir(path):  # chunked directory docs part0..N
        parts = sorted(glob.glob(os.path.join(path, "part*.json")), key=lambda f: int(re.search(r"part(\d+)", f).group(1)))
        txt = "".join((json.load(open(f)).get("data") or json.load(open(f))).get("s", "") for f in parts)
        return json.loads(txt)
    d = json.load(open(path))
    return d if isinstance(d, list) else d.get("dir") or d.get("orgs") or []


def run(a):
    today = a.today or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-3))).date().isoformat()
    E = load_docs(a.events)
    G = Gazetteer(load_directory(a.directory))
    prev, cache = {}, {}
    if a.locations and os.path.isdir(a.locations):
        for f in glob.glob(os.path.join(a.locations, "*.json")):
            d = json.load(open(f)); d = d.get("data", d) if isinstance(d.get("data"), dict) else d
            if os.path.basename(f).startswith("venues"):
                cache = d.get("v", {})
            else:
                prev.update(d.get("map", {}))

    def current(e):
        if e.get("verify") in ("ended", "cancelled"):
            return False
        if e.get("kind") == "event":
            return any(x >= today for x in e.get("dates") or [])
        return not (e.get("end") and e["end"] < today)

    def has_ll(e):
        return isinstance(e.get("lat"), (int, float)) and isinstance(e.get("lng"), (int, float))

    # pins already on events: keep the good ones, flag the ones far from their neighborhood
    bad = set()
    for e in E:
        if not has_ll(e):
            continue
        p, ar = (e["lat"], e["lng"]), area_of(e.get("neighborhood"), e.get("area2"))
        if not inbox(p) or (ar and km(p, ar[0]) > ar[1] + 1.5):
            bad.add(e["id"])
        else:
            c = core(parse_venue(e.get("venue"))[0])
            if len(c) >= 4:
                G.venue_pts[c].append(p)
    online = Online(cache, a.max_lookups) if a.online else None
    out, st = {}, defaultdict(int)
    todo = [e for e in E if current(e)]
    todo.sort(key=lambda e: e.get("addedAt") or "", reverse=True)
    for e in todo:
        eid = e["id"]
        if has_ll(e) and eid not in bad:
            continue  # the event's own pin is fine
        old = prev.get(eid)
        if old and old[0] is not None:
            out[eid] = old; st["kept"] += 1; continue
        v = e.get("venue") or ""
        name, addr, multi = parse_venue(v, e.get("address", ""))
        ar = area_of(e.get("neighborhood"), e.get("area2"))
        res = None
        if not (multi or GENERIC.search(norm(v)) or not v.strip()):
            res = choose(G.addr_cands(addr), ar)
            c = core(name)
            weak = (not ar or ar[1] >= 10) and " " not in c and len(c) < 6
            if not res and not weak:
                res = choose(G.name_cands(name) + [(p, "other listings") for p in G.venue_pts.get(c, [])], ar)
            if not res and online:
                key = norm(v) + "|" + (norm(e.get("neighborhood")) or "")
                hit = cache.get(key)
                if hit and hit[0] is not None:
                    res = ((hit[0], hit[1]), hit[2], 1, hit[3] == "h")
                elif not hit or (hit[0] is None and hit[-1] < (datetime.date.fromisoformat(today) - datetime.timedelta(days=14)).isoformat()):
                    found = None
                    online.errored = False
                    if addr:
                        found = choose(online.address(addr, ar), ar)
                    if not found and not weak:
                        found = choose(online.name(name, ar), ar)
                    if found:
                        res = found
                        cache[key] = [found[0][0], found[0][1], found[1][:60], "h" if (found[3] or found[1].startswith(("usig", "georef"))) else "m", today]
                    elif online.left > 0 and not online.errored:  # only remember a real "not found", never a network failure
                        cache[key] = [None, None, "miss", "", today]
        if res:
            (lat, lng), why, n, strong = res
            conf = "h" if (strong or n > 1 or why.startswith(("dir-addr", "usig", "georef"))) else "m"
            out[eid] = [lat, lng, conf]
            st["placed (replaced a wrong pin)" if eid in bad else "placed"] += 1
        elif eid in bad:
            out[eid] = [None, None, "x"]; st["wrong pin removed, no replacement yet"] += 1
        else:
            st["still neighborhood-only"] += 1

    os.makedirs("out/locations", exist_ok=True)
    for f in glob.glob("out/locations/*.json"):
        os.remove(f)
    items = sorted(out.items())
    n = max(1, math.ceil(len(items) / PART_SIZE))
    for k in range(n):
        json.dump({"map": dict(items[k * PART_SIZE:(k + 1) * PART_SIZE]), "updatedAt": today}, open(f"out/locations/part{k}.json", "w"), ensure_ascii=False, separators=(",", ":"))
    # keep the cache small: drop misses older than 60 days
    cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=60)).isoformat()
    cache = {k: v for k, v in cache.items() if v[0] is not None or v[-1] >= cutoff}
    json.dump({"v": cache, "updatedAt": today}, open("out/locations/venues.json", "w"), ensure_ascii=False, separators=(",", ":"))
    rep = {"today": today, "current": len(todo), "parts": n, "stats": st, "lookups": dict(online.calls) if online else {}, "cache": len(cache)}
    json.dump(rep, open("out/geo_report.json", "w"), indent=1)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("run")
    r.add_argument("--events", required=True); r.add_argument("--directory", required=True)
    r.add_argument("--locations"); r.add_argument("--online", action="store_true")
    r.add_argument("--max-lookups", type=int, default=400); r.add_argument("--today")
    a = ap.parse_args()
    run(a)
