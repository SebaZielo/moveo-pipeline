#!/usr/bin/env python3
"""On The Move organizer directory for the City of Buenos Aires + Greater Buenos Aires (AMBA, 40 partidos).

  python3 directory.py build [--prev out/directory_prev.json] [--events db/events]
      -> out/directory.json   one row per organizer (venue, club, theater, promoter...), with
                              zone (CABA/GBA) > partido or comuna > town or barrio > 1 km cell
      -> out/geo.json         simplified boundaries (barrios, partidos) + town points, for the app
      -> out/dir_report.json  counts per source / area
  python3 directory.py discover [--limit 300]
      -> finds each organizer's agenda page (website -> events page) and whether it is machine-readable;
         updates out/directory.json in place (results survive rebuilds via --prev)
  python3 directory.py chunks
      -> out/dirchunks/part*.json + meta.json, and out/geochunks/..., ready for the app database

Sources: City of Buenos Aires open data (espacios culturales), national Ministry of Culture "Mapa Cultural"
(CKAN API), OpenStreetMap (Overpass), Alternativa Teatral venue list, and the venues already in the app.
Every source is optional: if one fails the rest still build. Fix a failing source's function, not the pipeline.
"""
import argparse, csv, glob, hashlib, io, json, math, os, re, sys, time, unicodedata
from collections import Counter, defaultdict
from urllib.parse import urljoin, urlparse

import requests

OUT = "out"
UA = "OnTheMove-personal-app/1.0 (directory builder)"
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"})

# AMBA: CABA + 40 partidos (official AMBA definition)
BBOX = (-35.30, -59.40, -33.90, -57.75)  # south, west, north, east
AMBA_PARTIDOS = ["Almirante Brown", "Avellaneda", "Berazategui", "Esteban Echeverría", "Ezeiza", "Florencio Varela", "General San Martín",
                 "Hurlingham", "Ituzaingó", "José C. Paz", "La Matanza", "Lanús", "Lomas de Zamora", "Malvinas Argentinas", "Merlo", "Moreno",
                 "Morón", "Quilmes", "San Fernando", "San Isidro", "San Miguel", "Tigre", "Tres de Febrero", "Vicente López",
                 "Berisso", "Brandsen", "Campana", "Cañuelas", "Ensenada", "Escobar", "Exaltación de la Cruz", "General Las Heras",
                 "General Rodríguez", "La Plata", "Luján", "Marcos Paz", "Pilar", "Presidente Perón", "San Vicente", "Zárate"]
COMUNA = {}
for c, names in {1: "Retiro, San Nicolás, Puerto Madero, San Telmo, Monserrat, Constitución", 2: "Recoleta", 3: "Balvanera, San Cristóbal",
                 4: "La Boca, Barracas, Parque Patricios, Nueva Pompeya", 5: "Almagro, Boedo", 6: "Caballito", 7: "Flores, Parque Chacabuco",
                 8: "Villa Soldati, Villa Riachuelo, Villa Lugano", 9: "Liniers, Mataderos, Parque Avellaneda",
                 10: "Villa Real, Monte Castro, Versalles, Floresta, Vélez Sarsfield, Villa Luro",
                 11: "Villa General Mitre, Villa Devoto, Villa del Parque, Villa Santa Rita",
                 12: "Coghlan, Saavedra, Villa Urquiza, Villa Pueyrredón", 13: "Núñez, Belgrano, Colegiales", 14: "Palermo",
                 15: "Chacarita, Villa Crespo, La Paternal, Villa Ortúzar, Agronomía, Parque Chas"}.items():
    for n in names.split(", "):
        COMUNA[n] = c

# 1 km grid (south-west origin of the AMBA box)
LAT0, LNG0 = BBOX[0], BBOX[1]
DLAT = 1 / 111.32
DLNG = 1 / (111.32 * math.cos(math.radians(34.6)))


def cell(lat, lng):
    return f"{int((lat - LAT0) / DLAT)}_{int((lng - LNG0) / DLNG)}"


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(partido de|municipio de|the|el|la|los|las|de|del)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def km(a, b, c, d):
    return math.hypot((a - c) * 111.32, (b - d) * 111.32 * math.cos(math.radians(34.6)))


def fetch(url, **kw):
    kw.setdefault("timeout", 60)
    for i in range(3):
        try:
            r = S.get(url, **kw)
            if r.status_code == 200:
                return r
            print("  ", r.status_code, url[:100])
        except Exception as ex:
            print("  !!", url[:100], str(ex)[:120])
        time.sleep(3 + i * 5)
    return None


def overpass(q):
    for u in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"):
        for _ in range(2):
            try:
                r = S.post(u, data={"data": q}, timeout=400)
                if r.status_code == 200:
                    return r.json()
                print("  overpass", u, r.status_code)
            except Exception as ex:
                print("  overpass", u, str(ex)[:100])
            time.sleep(20)
    raise RuntimeError("Overpass unavailable")


# ------------------------------------------------------------------ geometry
def rings_from_members(members):
    """Stitch a relation's outer ways into closed rings of (lng, lat)."""
    segs = [[(p["lon"], p["lat"]) for p in m.get("geometry") or [] if p] for m in members
            if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry")]
    segs = [s for s in segs if len(s) >= 2]
    rings = []
    while segs:
        ring = segs.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, s in enumerate(segs):
                if s[0] == ring[-1]:
                    ring += s[1:]
                elif s[-1] == ring[-1]:
                    ring += s[::-1][1:]
                elif s[-1] == ring[0]:
                    ring = s[:-1] + ring
                elif s[0] == ring[0]:
                    ring = s[::-1][:-1] + ring
                else:
                    continue
                segs.pop(i); changed = True
                break
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def simplify(pts, tol=0.0004):
    if len(pts) < 5:
        return pts
    if pts[0] == pts[-1]:  # closed ring: split at the farthest vertex so both halves have distinct ends
        k = max(range(len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
        ring = simplify(pts[: k + 1], tol) + simplify(pts[k:], tol)[1:]
        return ring if len(ring) >= 4 else pts

    def rdp(a, b):
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1e-12
        best, bi = 0, None
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L
            if d > best:
                best, bi = d, i
        if best > tol and bi:
            return rdp(a, bi)[:-1] + rdp(bi, b)
        return [pts[a], pts[b]]
    return rdp(0, len(pts) - 1)


def inside(lng, lat, ring):
    c = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]; xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            c = not c
        j = i
    return c


class Units:
    def __init__(self, units):
        self.u = units
        for x in units:
            xs = [p[0] for r in x["rings"] for p in r]; ys = [p[1] for r in x["rings"] for p in r]
            x["bb"] = (min(xs), min(ys), max(xs), max(ys))

    def find(self, lat, lng, level=None):
        for x in self.u:
            if level and x["level"] != level:
                continue
            b = x["bb"]
            if b[0] <= lng <= b[2] and b[1] <= lat <= b[3] and any(inside(lng, lat, r) for r in x["rings"]):
                return x
        return None


def build_geo():
    """Barrios of CABA (OSM admin_level 10) and AMBA partidos (OSM admin_level 5), plus town points."""
    s, w, n, e = BBOX
    data = overpass(f"""[out:json][timeout:300];
(
  rel["boundary"="administrative"]["admin_level"="9"](-34.72,-58.54,-34.52,-58.33);
  rel["boundary"="administrative"]["admin_level"="5"]({s},{w},{n},{e});
  node["place"~"^(city|town|suburb|village)$"]({s},{w},{n},{e});
);
out geom;""")
    units, places = [], []
    wanted = {norm(p): p for p in AMBA_PARTIDOS}
    barrio_names = {norm(b): b for b in COMUNA}
    for el in data.get("elements", []):
        t = el.get("tags", {})
        if el["type"] == "node":
            if t.get("name"):
                places.append({"name": t["name"], "lat": el["lat"], "lng": el["lon"], "kind": t.get("place")})
            continue
        name = t.get("name", "")
        lvl = t.get("admin_level")
        rings = [simplify(r) for r in rings_from_members(el.get("members", []))]
        if not rings:
            continue
        if lvl == "9" and norm(name) in barrio_names:
            b = barrio_names[norm(name)]
            units.append({"id": "b-" + norm(b).replace(" ", "-"), "name": b, "level": "barrio", "zone": "CABA",
                          "parent": f"Comuna {COMUNA[b]}", "rings": rings})
        elif lvl == "5" and norm(name) in wanted:
            p = wanted[norm(name)]
            units.append({"id": "p-" + norm(p).replace(" ", "-"), "name": p, "level": "partido", "zone": "GBA", "parent": "Greater Buenos Aires", "rings": rings})
    got_b = {u["name"] for u in units if u["level"] == "barrio"}
    got_p = {u["name"] for u in units if u["level"] == "partido"}
    print(f"geo: {len(got_b)}/48 barrios, {len(got_p)}/40 partidos, {len(places)} places")
    missing = sorted(set(AMBA_PARTIDOS) - got_p)
    if missing:
        print("  partidos missing:", ", ".join(missing))
    # places belong to the partido/barrio they sit in; CABA suburbs are barrios already
    U = Units(units)
    for p in places:
        x = U.find(p["lat"], p["lng"])
        p["unit"] = x["id"] if x else None
    places = [p for p in places if p["unit"] and p["unit"].startswith("p-")]
    return units, places


# ------------------------------------------------------------------ sources
TYPES = {  # type -> app genres it feeds
    "theater": ["theater"], "cultural-center": ["music", "theater", "classes", "galleries"], "cinema": ["film"], "museum": ["museums"],
    "gallery": ["galleries"], "club": ["nightlife", "music"], "music-venue": ["music"], "bar": ["music", "nightlife", "food"],
    "milonga": ["tango"], "library": ["film", "classes"], "bookstore": ["film"], "record-store": ["music"], "market": ["markets"],
    "school": ["classes"], "convention": ["events"], "stadium": ["outdoor", "music"], "community": ["classes", "events"],
    "promoter": ["music", "nightlife"], "venue": ["music", "theater"], "other": [],
}


def classify(text):
    t = norm(text)
    for typ, rx in [("milonga", r"milonga|tango"), ("cinema", r"\bcine|sala de cine|incaa"), ("theater", r"teatr|escenic|sala\b|auditorio"),
                    ("museum", r"museo"), ("gallery", r"galer|exhibicion|exposicion|arte contemporaneo"),
                    ("club", r"nightclub|discoteca|boliche|club nocturno|\bdisco\b"), ("music-venue", r"music venue|jazz|musica en vivo|live music|pena|club de musica|club de jazz|musica"),
                    ("bar", r"\bbar\b|bares|pub|cerveceria|bodegon"), ("library", r"bibliotec"), ("bookstore", r"librer"), ("record-store", r"disquer"),
                    ("market", r"feri|mercado|marketplace"), ("school", r"formacion|escuela|taller|instituto"), ("convention", r"convenc|exhibition centre|predio ferial|expo"),
                    ("stadium", r"estadio|stadium"), ("community", r"community|sociedad de fomento|club social|centro vecinal|social centre"),
                    ("cultural-center", r"centro cultural|arts centre|espacio cultural|casa de la cultura|casa del bicentenario|centro de arte")]:
        if re.search(rx, t):
            return typ
    return "other"


def pick(row, *pats):
    for p in pats:
        for k, v in row.items():
            if k and re.search(p, k, re.I) and str(v or "").strip():
                return str(v).strip()
    return ""


def to_float(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def rows_from_csv(text):
    sample = text[:4000]
    delim = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


def src_gcba(log):
    urls = ["https://cdn.buenosaires.gob.ar/datosabiertos/datasets/ministerio-de-cultura/espacios-culturales/espacios-culturales.csv",
            "https://data.buenosaires.gob.ar/dataset/espacios-culturales/resource/juqdkmgo-711-resource/download"]
    for u in urls:
        r = fetch(u)
        if not r:
            continue
        r.encoding = r.apparent_encoding or "utf-8"
        rows = rows_from_csv(r.text)
        log.append(f"gcba {u[-60:]}: {len(rows)} rows, columns {list(rows[0].keys())[:25] if rows else []}")
        out = []
        for row in rows:
            cat = pick(row, r"^funcion", r"categor", r"^tipo")
            sub = pick(row, r"subcat", r"subtipo")
            if re.search(r"monument|lugar hist|calesit", norm(cat + " " + sub)):
                continue
            name = pick(row, r"^establecim", r"^nombre", r"^name")
            lat = to_float(pick(row, r"^lat", r"latitud")); lng = to_float(pick(row, r"^lon", r"^lng", r"longitud"))
            if not name or not lat or not lng:
                continue
            typ = classify(sub) if sub and classify(sub) != "other" else classify(f"{cat} {name}")
            out.append({"name": name, "type": typ, "lat": lat, "lng": lng,
                        "address": pick(row, r"direcc", r"domicilio", r"calle"), "barrio": pick(row, r"^barrio"),
                        "web": pick(row, r"^web", r"sitio", r"url", r"pagina"), "ig": pick(row, r"instagram"),
                        "src": "gcba", "cat": f"{cat} / {sub}".strip(" /")})
        return out
    return []


def src_mapa_cultural(log):
    r = fetch("https://datos.gob.ar/api/3/action/package_show?id=cultura-mapa-cultural-espacios-culturales")
    if not r:
        r = fetch("https://datos.cultura.gob.ar/api/3/action/package_show?id=mapa-cultural-espacios-culturales")
    if not r:
        return []
    res = r.json().get("result", {}).get("resources", [])
    keep = re.compile(r"teatr|centro|museo|cine|biblioteca popular|espacio|sala|milonga|galer|librer|disquer|casa|complejo|incaa|bicentenario|pena|club|auditor", re.I)
    out = []
    for x in res:
        name, url = x.get("name", ""), x.get("url", "")
        if not url.lower().endswith(".csv") and "csv" not in (x.get("format") or "").lower():
            continue
        if not keep.search(name):
            log.append(f"mapa cultural skip {name}")
            continue
        rr = fetch(url)
        if not rr:
            continue
        rr.encoding = rr.apparent_encoding or "utf-8"
        rows = rows_from_csv(rr.text)
        n0 = len(out)
        for row in rows:
            prov = norm(pick(row, r"^provincia"))
            if prov and not re.search(r"buenos aires|capital|ciudad autonoma|caba", prov):
                continue
            lat = to_float(pick(row, r"^lat", r"latitud")); lng = to_float(pick(row, r"^lon", r"^lng", r"longitud"))
            nm = pick(row, r"^nombre", r"^establecim", r"^denomin")
            if not nm or not lat or not lng or not (BBOX[0] < lat < BBOX[2] and BBOX[1] < lng < BBOX[3]):
                continue
            out.append({"name": nm, "type": classify(f"{name} {pick(row, r'tipo', r'categ')} {nm}"), "lat": lat, "lng": lng,
                        "address": pick(row, r"direcc", r"domicilio"), "web": pick(row, r"^web", r"sitio", r"url"),
                        "ig": pick(row, r"instagram"), "src": "mapa-cultural", "cat": name})
        log.append(f"mapa cultural '{name}': {len(out) - n0} in AMBA")
    return out


OSM_Q = """[out:json][timeout:400];
(
  nwr["amenity"~"^(theatre|arts_centre|cinema|nightclub|events_venue|music_venue|community_centre|social_centre|conference_centre|exhibition_centre|planetarium|marketplace)$"]["name"]({bb});
  nwr["tourism"~"^(museum|gallery)$"]["name"]({bb});
  nwr["leisure"~"^(dance|stadium)$"]["name"]({bb});
  nwr["amenity"~"^(bar|pub|biergarten)$"]["live_music"="yes"]["name"]({bb});
  nwr["amenity"~"^(bar|pub)$"]["name"~"jazz|blues|club|cultural|pe[ñn]a|milonga|tango|rock|live|m[uú]sica",i]({bb});
  nwr["shop"~"^(music|books)$"]["name"]({bb});
);
out center tags;"""


def src_osm(log):
    s, w, n, e = BBOX
    data = overpass(OSM_Q.replace("{bb}", f"{s},{w},{n},{e}"))
    out = []
    for el in data.get("elements", []):
        t = el.get("tags", {})
        lat = el.get("lat") or (el.get("center") or {}).get("lat"); lng = el.get("lon") or (el.get("center") or {}).get("lon")
        if not lat or not t.get("name"):
            continue
        kind = t.get("amenity") or t.get("tourism") or t.get("leisure") or ("record-store" if t.get("shop") == "music" else "bookstore" if t.get("shop") == "books" else "")
        typ = {"theatre": "theater", "arts_centre": "cultural-center", "cinema": "cinema", "nightclub": "club", "events_venue": "venue",
               "music_venue": "music-venue", "community_centre": "community", "social_centre": "community", "conference_centre": "convention",
               "exhibition_centre": "convention", "planetarium": "museum", "marketplace": "market", "museum": "museum", "gallery": "gallery",
               "dance": "milonga" if re.search(r"tango|milonga", t["name"], re.I) else "club", "stadium": "stadium",
               "bar": "bar", "pub": "bar", "biergarten": "bar"}.get(kind, classify(kind + " " + t["name"]))
        addr = " ".join(x for x in [t.get("addr:street", ""), t.get("addr:housenumber", "")] if x)
        out.append({"name": t["name"], "type": typ, "lat": lat, "lng": lng, "address": addr,
                    "web": t.get("website") or t.get("contact:website") or t.get("url") or "",
                    "ig": t.get("contact:instagram") or t.get("instagram") or "", "src": "osm", "cat": kind})
    log.append(f"osm: {len(out)}")
    return out


def src_alternativa(log):
    r = fetch("https://www.alternativateatral.com/teatros.asp")
    if not r:
        return []
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        if re.search(r"/teatro\d+", a["href"]):
            nm = a.get_text(" ", strip=True)
            if nm and len(nm) > 2:
                out.append({"name": nm.title() if nm.isupper() else nm, "type": "theater", "lat": None, "lng": None, "address": "",
                            "web": "", "ig": "", "src": "alternativa", "agenda": urljoin(r.url, a["href"]), "cat": "sala"})
    log.append(f"alternativa: {len(out)} venues (matched by name; unmatched need geocoding)")
    return out


def src_listings(path, log):
    out = []
    for f in glob.glob(os.path.join(path, "*.json")):
        d = json.load(open(f)); d = d.get("data", d)
        v = (d.get("venue") or "").split("(")[0].split(",")[0].strip()
        if not v or norm(v) in ("", "various", "varios", "see event page", "buenos aires", "online") or not d.get("lat"):
            continue
        out.append({"name": v, "type": {"tango": "milonga", "film": "cinema", "museums": "museum", "galleries": "gallery", "theater": "theater",
                                        "nightlife": "club", "music": "music-venue", "markets": "market", "classes": "school", "events": "convention"}.get(d.get("genre"), "venue"),
                    "lat": d["lat"], "lng": d["lng"], "address": "", "web": "", "ig": "", "src": "listings", "cat": d.get("genre", "")})
    log.append(f"listings with coordinates: {len(out)}")
    return out


# ------------------------------------------------------------------ merge + areas
def oid(name, lat, lng):
    return "o" + hashlib.md5(f"{norm(name)}|{round(lat or 0, 3)}|{round(lng or 0, 3)}".encode()).hexdigest()[:9]


def merge(items):
    """Same organizer from several sources: same normalized name within 250 m (or fuzzy name within 80 m)."""
    out, grid = [], defaultdict(list)
    prio = {"gcba": 0, "mapa-cultural": 1, "osm": 2, "listings": 3, "alternativa": 4}
    for it in sorted(items, key=lambda x: prio.get(x["src"], 9)):
        if it.get("lat") is None:
            continue
        k = (round(it["lat"] / 0.005), round(it["lng"] / 0.005))
        cand = [o for dx in (-1, 0, 1) for dy in (-1, 0, 1) for o in grid[(k[0] + dx, k[1] + dy)]]
        nn = norm(it["name"]); toks = set(nn.split())
        hit = None
        for o in cand:
            d = km(it["lat"], it["lng"], o["lat"], o["lng"])
            on = norm(o["name"]); ot = set(on.split())
            if (on == nn and d < 0.25) or (toks and ot and len(toks & ot) / max(len(toks), len(ot)) >= 0.75 and d < 0.08) or \
               ((nn in on or on in nn) and min(len(nn), len(on)) >= 6 and d < 0.12):
                hit = o; break
        if hit:
            hit["srcs"] = sorted(set(hit["srcs"]) | {it["src"]})
            for f in ("web", "ig", "address", "agenda"):
                if not hit.get(f) and it.get(f):
                    hit[f] = it[f]
            if hit["type"] in ("other", "venue", "community") and it["type"] not in ("other", "venue"):
                hit["type"] = it["type"]
            continue
        o = dict(it); o["srcs"] = [it["src"]]
        out.append(o); grid[k].append(o)
    # name-only sources (Alternativa): attach to a located organizer with the same name
    byname = defaultdict(list)
    for o in out:
        byname[norm(o["name"])].append(o)
    unmatched = []
    for it in items:
        if it.get("lat") is not None:
            continue
        m = byname.get(norm(it["name"]))
        if m:
            m[0]["srcs"] = sorted(set(m[0]["srcs"]) | {it["src"]})
            m[0].setdefault("agenda", it.get("agenda"))
        else:
            unmatched.append(it)
    return out, unmatched


def assign_areas(orgs, units, places):
    U = Units(units)
    pl = defaultdict(list)
    for p in places:
        pl[p["unit"]].append(p)
    kept = []
    for o in orgs:
        x = U.find(o["lat"], o["lng"])
        if not x:
            continue  # outside CABA + the 40 partidos
        if x["level"] == "barrio":
            o["zone"], o["area1"], o["area2"] = "CABA", x["parent"], x["name"]
        else:
            o["zone"], o["area1"] = "GBA", x["name"]
            near = min(pl.get(x["id"], []), key=lambda p: km(o["lat"], o["lng"], p["lat"], p["lng"]), default=None)
            o["area2"] = near["name"] if near and km(o["lat"], o["lng"], near["lat"], near["lng"]) < 6 else x["name"]
        o["cell"] = cell(o["lat"], o["lng"])
        kept.append(o)
    return kept


def compact(o):
    d = {"i": oid(o["name"], o["lat"], o["lng"]), "n": o["name"][:90], "t": o["type"], "la": round(o["lat"], 6), "ln": round(o["lng"], 6),
         "z": o["zone"], "a1": o["area1"], "a2": o["area2"], "c": o["cell"], "s": o["srcs"]}
    for k, f in (("ad", "address"), ("w", "web"), ("ig", "ig"), ("ag", "agenda")):
        v = (o.get(f) or "").strip()
        if v:
            d[k] = v[:160]
    return d


def build(args):
    os.makedirs(OUT, exist_ok=True)
    log, items, report = [], [], {}
    fns = [("gcba", src_gcba), ("mapa-cultural", src_mapa_cultural), ("osm", src_osm), ("alternativa", src_alternativa)]
    for name, fn in fns:
        try:
            got = fn(log)
        except Exception as ex:
            got = []; log.append(f"{name} FAILED {type(ex).__name__}: {str(ex)[:200]}")
        report[name] = len(got); items += got
        print(f"{name:14s} {len(got)}")
    if args.events and os.path.isdir(args.events):
        got = src_listings(args.events, log); report["listings"] = len(got); items += got
    units, places = build_geo()
    orgs, unmatched = merge(items)
    orgs = assign_areas(orgs, units, places)
    rows = [compact(o) for o in orgs]
    # keep what agenda discovery learned before (ids are stable: name + location)
    if args.prev and os.path.exists(args.prev):
        prev = {r["i"]: r for r in json.load(open(args.prev))}
        for r in rows:
            p = prev.get(r["i"])
            if p:
                for k in ("ag", "rd", "chk", "ev", "last", "note"):
                    if k in p and k not in r:
                        r[k] = p[k]
        # promoters/collectives added from event platforms have no fixed place: keep them
        ids = {r["i"] for r in rows}
        rows += [p for p in prev.values() if p.get("t") == "promoter" and p["i"] not in ids]
    json.dump(rows, open(f"{OUT}/directory.json", "w"), ensure_ascii=False, separators=(",", ":"))
    geo = {"v": 1, "units": [{k: u[k] for k in ("id", "name", "level", "zone", "parent")} | {"rings": [[[round(x, 5), round(y, 5)] for x, y in r] for r in u["rings"]]} for u in units],
           "places": [{"name": p["name"], "lat": round(p["lat"], 5), "lng": round(p["lng"], 5), "unit": p["unit"]} for p in places],
           "grid": {"lat0": LAT0, "lng0": LNG0, "dlat": DLAT, "dlng": DLNG}}
    json.dump(geo, open(f"{OUT}/geo.json", "w"), ensure_ascii=False, separators=(",", ":"))
    by_zone = Counter(r["z"] for r in rows); by_a1 = Counter((r["z"], r["a1"]) for r in rows); by_t = Counter(r["t"] for r in rows)
    report.update({"organizers": len(rows), "zones": dict(by_zone), "types": dict(by_t.most_common()), "unmatched_name_only": len(unmatched),
                   "top_areas": [f"{a} ({z}): {n}" for (z, a), n in by_a1.most_common(20)], "log": log})
    json.dump(report, open(f"{OUT}/dir_report.json", "w"), ensure_ascii=False, indent=1)
    json.dump(unmatched, open(f"{OUT}/unmatched.json", "w"), ensure_ascii=False, indent=1)
    print("organizers", len(rows), dict(by_zone)); print("types", dict(by_t.most_common(12)))


# ------------------------------------------------------------------ agenda discovery
AGENDA_WORDS = re.compile(r"agenda|eventos?|cartelera|programaci|calendario|shows?|fechas|actividades|pr[oó]xim|funciones|entradas|tickets|events|line-?up", re.I)
DATE_RX = re.compile(r"\b([0-3]?\d)[/.-](1[0-2]|0?[1-9])\b|\b([0-3]?\d)\s+de\s+(enero|febrero|marzo|abril|mayo|junio|julio|agosto|sept?iembre|octubre|noviembre|diciembre)", re.I)


def discover(args):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import harvester as H  # reuse the event extractors
    rows = json.load(open(f"{OUT}/directory.json"))
    today = time.strftime("%Y-%m-%d")
    todo = [r for r in rows if (r.get("w") or r.get("ag")) and r.get("chk", "") < time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
            and not re.search(r"instagram\.com|facebook\.com|linktr\.ee", r.get("w", ""))]
    order = {"club": 0, "music-venue": 0, "theater": 1, "cultural-center": 1, "milonga": 1, "bar": 2, "cinema": 2, "gallery": 2, "museum": 3}
    todo.sort(key=lambda r: (order.get(r["t"], 5), r.get("chk", "")))
    todo = todo[: args.limit]
    print("checking", len(todo), "organizer sites")
    from bs4 import BeautifulSoup
    import concurrent.futures as cf

    def one(r):
        url = r.get("ag") or r["w"]
        if not url.startswith("http"):
            url = "https://" + url.lstrip("/")
        try:
            resp = H.get(url, timeout=15)
            if resp.status_code != 200:
                return r["i"], {"rd": "dead", "note": f"HTTP {resp.status_code}"}
            evs, soup = H.from_html(resp.text, resp.url, "org", 30)
            if evs:
                return r["i"], {"ag": resp.url, "rd": "structured", "ev": len(evs)}
            cands = []
            for a in soup.find_all("a", href=True):
                label = (a.get_text(" ", strip=True) + " " + a["href"])[:200]
                u = urljoin(resp.url, a["href"])
                if AGENDA_WORDS.search(label) and urlparse(u).netloc.endswith(urlparse(resp.url).netloc.replace("www.", "")) and u not in cands:
                    cands.append(u)
            for u in cands[:3]:
                r2 = H.get(u, timeout=15)
                if r2.status_code != 200:
                    continue
                evs, s2 = H.from_html(r2.text, r2.url, "org", 30)
                if evs:
                    return r["i"], {"ag": r2.url, "rd": "structured", "ev": len(evs)}
                if len(DATE_RX.findall(s2.get_text(" "))) >= 4:
                    return r["i"], {"ag": r2.url, "rd": "text"}
            if len(DATE_RX.findall(soup.get_text(" "))) >= 4:
                return r["i"], {"ag": resp.url, "rd": "text"}
            ig = next((a["href"] for a in soup.find_all("a", href=True) if "instagram.com/" in a["href"]), "")
            return r["i"], {"rd": "none", **({"ig": ig[:160]} if ig and not r.get("ig") else {})}
        except Exception as ex:
            return r["i"], {"rd": "dead", "note": str(ex)[:80]}

    res = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for i, upd in ex.map(one, todo):
            res[i] = upd
    for r in rows:
        if r["i"] in res:
            r.update(res[r["i"]]); r["chk"] = today
    for r in rows:
        if r.get("ig") and not r.get("w") and not r.get("ag"):
            r["rd"] = "instagram-only"
    json.dump(rows, open(f"{OUT}/directory.json", "w"), ensure_ascii=False, separators=(",", ":"))
    c = Counter(v.get("rd") for v in res.values())
    print("discovery:", dict(c)); print("all organizers by readability:", dict(Counter(r.get("rd", "unchecked") for r in rows)))


def chunks(args):
    for name, src, folder in (("directory", f"{OUT}/directory.json", f"{OUT}/dirchunks"), ("geo", f"{OUT}/geo.json", f"{OUT}/geochunks")):
        if not os.path.exists(src):
            continue
        os.makedirs(folder, exist_ok=True)
        for f in glob.glob(f"{folder}/*.json"):
            os.remove(f)
        text = open(src).read()
        parts = [text[i:i + 120000] for i in range(0, len(text), 120000)]
        for k, p in enumerate(parts):
            json.dump({"s": p}, open(f"{folder}/part{k}.json", "w"), ensure_ascii=False)
        meta = {"chunks": len(parts), "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S-03:00")}
        if name == "directory":
            rows = json.loads(text)
            meta["count"] = len(rows)
            meta["readable"] = sum(1 for r in rows if r.get("rd") in ("structured", "text"))
        json.dump(meta, open(f"{folder}/meta.json", "w"))
        print(name, "->", len(parts), "chunks")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build"); b.add_argument("--prev"); b.add_argument("--events", default="db/events")
    d = sp.add_parser("discover"); d.add_argument("--limit", type=int, default=300)
    sp.add_parser("chunks")
    a = ap.parse_args()
    {"build": build, "discover": discover, "chunks": chunks}[a.cmd](a)
