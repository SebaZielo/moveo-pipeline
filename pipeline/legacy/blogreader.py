#!/usr/bin/env python3
"""
MOVEO blog reader  (harvest/blogreader)

Turns blog / newspaper / guide posts (Infobae "Guía de Arte y Cultura", Time Out lists,
La Nación, Cronista, Michelin...) into CONCRETE postings:

  one item per event or place  ->  title, when (dates / days / hours / run window),
  where (venue, address, neighborhood), price (free / ARS / USD), the ORIGINAL page
  (event or ticket page, else venue site / Instagram, Google Maps last), and pictures
  (original page's picture as imgHint; the blog's picture only as an address, never downloaded).

Owner's rules (2026-10-27/28):
  - details may be read from blogs, but every posting links to the original page;
  - when the original page states dates, times, venue or price, it wins;
  - blog/roundup photos are never downloaded or stored, only their web address (imgBackup);
  - nothing is dropped just because its original page could not be found.

Commands
  read URL [URL...]            parse posts and print the items (testing)
  run   --events DIR ...        work through blog-linked listings, write out/ files
  versions                      build versions.json from this session's ArtifactData list results
  batches --versions FILE       turn out/updates + out/new into ready-to-send batch files
  apply-review FILE             merge the agent's review answers (desc/genre/url/drop) into out/

Only needs: requests, beautifulsoup4, lxml   (playwright optional, for JS-only pages)
"""
import argparse, datetime as dt, glob, hashlib, json, os, re, sys, time, unicodedata, urllib.parse
from collections import Counter, defaultdict

VERSION = "1.1 (2026-10-29)"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# --------------------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------------------
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if not unicodedata.combining(c))

def norm(s):
    return re.sub(r"\s+", " ", strip_accents(s or "").lower()).strip()

STOP = set("""el la los las un una unos unas de del al a en y o e u con por para sin sobre entre
the a an of and or in on at to for with by from this that de la le du des et
muestra exposicion show obra festival ciclo taller clase noche night bar restaurante restaurant cafe
buenos aires caba ciudad nuevo nueva new edition edicion vol parte part""".split())

def tokens(s, keep_stop=False):
    t = re.findall(r"[a-z0-9]+", norm(s))
    return [w for w in t if keep_stop or (w not in STOP and len(w) > 1)]

def sim(a, b):
    """similarity of two titles: distinctive-token overlap (0..1)."""
    A, B = set(tokens(a)), set(tokens(b))
    if not A or not B:
        return 0.0
    inter = len(A & B)
    return inter / min(len(A), len(B)) * (0.6 + 0.4 * inter / max(len(A), len(B)))

VSTOP = set("el la los las de del y e en the of and at a".split())

def vtokens(s):
    s = re.sub(r"\(.*?\)", " ", s or "")
    s = s.split(",")[0]
    return [w for w in re.findall(r"[a-z0-9]+", norm(s)) if w not in VSTOP]

TYPEWORDS = set("""restaurant restaurante resto bar cafe cafeteria museo museum teatro theater galeria gallery club
feria market mercado tienda shop store libreria bookstore hotel hostel parrilla pizzeria bodegon cerveceria""".split())

def ttoks(s):
    s = re.sub(r"\(.*?\)", " ", s or "")
    return [w for w in re.findall(r"[a-z0-9]+", norm(s)) if w not in VSTOP and len(w) > 1]

def vsim(a, b):
    """venue-name similarity: Jaccard on all words (bar/cafe/teatro count, so 'Ruby Bar' != 'Galeria Ruby')."""
    A, B = set(vtokens(a)), set(vtokens(b))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)

def domain(u):
    try:
        h = urllib.parse.urlparse(u or "").netloc.lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h

def clean_title(t):
    t = re.sub(r"^\s*\d{1,2}\s*[.)\-–]\s*", "", t or "")            # "3. Title"
    t = t.strip(" *_·•-–—: ")
    return re.sub(r"\s+", " ", t)

# outlets whose pages are discovery pages, never the original
BLOG_DOMAINS = ("timeout.com", "infobae.com", "lanacion.com.ar", "clarin.com", "cronista.com",
                "perfil.com", "pagina12.com.ar", "indiehoy.com", "rollingstone", "guide.michelin.com",
                "tripadvisor", "wikipedia.org", "quehacemos.com.ar", "allevents.in", "buenosairessecreta",
                "infonegocios", "lavoz.com.ar", "tn.com.ar", "elle.com", "vogue", "forbesargentina",
                "marieclaire", "billboard", "agendacultural", "planetajoy", "losandes.com.ar")

def is_blog(u):
    d = domain(u)
    return any(b in d for b in BLOG_DOMAINS)

# original-page platforms: domain fragment -> label (ranked first)
PLATFORMS = [("alternativateatral", "Alternativa Teatral"), ("passline", "Passline"), ("venti", "Venti"),
             ("eventbrite", "Eventbrite"), ("ticketek", "Ticketek"), ("alpogo", "Alpogo"),
             ("plateanet", "Plateanet"), ("entradauno", "EntradaUno"), ("allaccess", "All Access"),
             ("livepass", "LivePass"), ("tuentrada", "TuEntrada"), ("lu.ma", "Luma"), ("luma.com", "Luma"),
             ("meetup.com", "Meetup"), ("ra.co", "Resident Advisor"), ("ticketmaster", "Ticketmaster"),
             ("tickethoy", "TicketHoy"), ("entradaweb", "EntradaWeb"), ("qrticket", "QR Ticket"),
             ("articket", "Articket"), ("autoentrada", "AutoEntrada"), ("bombo", "Bombo"),
             ("fever", "Fever"), ("teatrocolon.org.ar", "Teatro Colón"), ("complejoteatral", "Complejo Teatral"),
             ("eventim", "Eventim"), ("linktr.ee", "Linktree")]
PLATFORM_WORDS = {  # names written in prose ("entradas por Alternativa Teatral")
    "alternativa teatral": "alternativateatral.com", "passline": "passline.com", "venti": "venti.com.ar",
    "eventbrite": "eventbrite.com.ar", "ticketek": "ticketek.com.ar", "alpogo": "alpogo.com",
    "plateanet": "plateanet.com", "entradauno": "entradauno.com", "all access": "allaccess.com.ar",
    "allaccess": "allaccess.com.ar", "livepass": "livepass.com.ar", "tuentrada": "tuentrada.com",
    "luma": "lu.ma", "meetup": "meetup.com", "resident advisor": "ra.co", "ticketmaster": "ticketmaster.com.ar",
    "bombo": "bombo.com.ar", "fever": "feverup.com", "entradaweb": "entradaweb.com.ar",
}
NOT_ORIGINAL = ("facebook.com/sharer", "twitter.com/intent", "x.com/intent", "wa.me", "whatsapp", "mailto:",
                "t.me/share", "linkedin.com/share", "pinterest.com/pin", "profile.google.com", "news.google.com",
                "google.com/maps", "maps.app.goo.gl", "goo.gl/maps", "maps.google", "javascript:", "/newsletter",
                "apple.com/app", "play.google.com")

def link_rank(u):
    """lower is better: 0 ticket/event platform, 1 venue/organizer site, 2 instagram, 9 not usable."""
    lu = (u or "").lower()
    if not lu.startswith("http") or any(x in lu for x in NOT_ORIGINAL) or is_blog(lu):
        return 9
    d = domain(lu)
    if any(p in d or p in lu for p, _ in PLATFORMS):
        return 0
    if "instagram.com" in d:
        return 2
    if d in ("facebook.com", "m.facebook.com", "tiktok.com", "youtube.com", "youtu.be", "spotify.com",
             "open.spotify.com", "twitter.com", "x.com"):
        return 3
    return 1

def is_maps(u):
    lu = (u or "").lower()
    return "google.com/maps" in lu or "maps.app.goo.gl" in lu or "goo.gl/maps" in lu or "maps.google" in lu

def maps_url(q):
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(q)

# --------------------------------------------------------------------------------------
# dates, days, hours, prices
# --------------------------------------------------------------------------------------
MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
          "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
          "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
          "september": 9, "october": 10, "november": 11, "december": 12,
          "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
          "oct": 10, "nov": 11, "dec": 12, "ene": 1, "abr": 4, "ago": 8, "dic": 12}
MON_RX = "|".join(sorted(MONTHS, key=len, reverse=True))
WD = {"lunes": "mon", "martes": "tue", "miercoles": "wed", "jueves": "thu", "viernes": "fri", "sabado": "sat",
      "sabados": "sat", "domingo": "sun", "domingos": "sun",
      "monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat",
      "sunday": "sun", "mondays": "mon", "tuesdays": "tue", "wednesdays": "wed", "thursdays": "thu",
      "fridays": "fri", "saturdays": "sat", "sundays": "sun",
      "mon": "mon", "tue": "tue", "tues": "tue", "wed": "wed", "thu": "thu", "thur": "thu", "thurs": "thu",
      "fri": "fri", "sat": "sat", "sun": "sun", "lun": "mon", "mar": "tue", "mie": "wed", "jue": "thu",
      "vie": "fri", "sab": "sat", "dom": "sun"}
WD_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WD_LONG = r"(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)"
WD_ANY = r"(?:lunes|martes|miercoles|jueves|viernes|sabados?|domingos?|mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?|tues|thurs|thur|mon|tue|wed|thu|fri|sat|sun)"

def wd_range(a, b):
    i, j = WD_ORDER.index(WD[a]), WD_ORDER.index(WD[b])
    out = []
    while True:
        out.append(WD_ORDER[i])
        if i == j or len(out) > 7:
            return out
        i = (i + 1) % 7

def mk_date(day, month, year_hint, ref):
    try:
        day = int(day)
        month = MONTHS[month] if isinstance(month, str) else int(month)
        if year_hint:
            y = int(year_hint)
            y = y + 2000 if y < 100 else y
            return dt.date(y, month, day)
        d = dt.date(ref.year, month, day)
        if d < ref - dt.timedelta(days=75):          # "8 de enero" read in November -> next year
            d = dt.date(ref.year + 1, month, day)
        elif d > ref + dt.timedelta(days=300):       # "28 de diciembre" read in January -> last year
            d = dt.date(ref.year - 1, month, day)
        return d
    except Exception:
        return None

def hhmm(h, m=None, ampm=None):
    try:
        h = int(h); m = int(m or 0)
    except Exception:
        return None
    if ampm:
        a = ampm.lower().replace(".", "")
        if a.startswith("p") and h < 12:
            h += 12
        if a.startswith("a") and h == 12:
            h = 0
    if h == 24:
        h = 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"

def parse_when(text, ref):
    """Read dates, run windows, weekdays and hours from Spanish or English prose.
    Returns dict(dates, start, end, days, time, hours, opening, notes)."""
    raw = text or ""
    t = norm(raw)
    t = re.sub(r"(\d)\s*(?:º|°|ro\b)", r"\1", t)
    t = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", t)
    out = {"dates": set(), "start": None, "end": None, "days": [], "time": None, "hours": None,
           "opening": None, "notes": []}
    spans = []

    def take(m):
        spans.append(m.span())

    def free(m):
        a, b = m.span()
        return not any(a < y and b > x for x, y in spans)

    Y = r"(?:\s+(?:de|del)?\s*(\d{4}))?"
    # --- run windows -------------------------------------------------------------------
    for m in re.finditer(r"(?:del?|desde el)\s+(?:" + WD_LONG + r"\s+)?(\d{1,2})(?:\s+de\s+(" + MON_RX + r"))?" + Y +
                         r"\s+(?:al|hasta el|y el|a)\s+(?:" + WD_LONG + r"\s+)?(\d{1,2})\s+de\s+(" + MON_RX + r")" + Y, t):
        d2 = mk_date(m.group(4), m.group(5), m.group(6), ref)
        d1 = mk_date(m.group(1), m.group(2) or m.group(5), m.group(3) or m.group(6), ref)
        if d1 and d2:
            if d1 > d2:
                d1 = d1.replace(year=d1.year - 1)
            out["start"], out["end"] = d1, d2; take(m)
    for m in re.finditer(r"(?:from\s+)?(?:" + WD_ANY + r",?\s+)?(" + MON_RX + r")\.?\s+(\d{1,2})\s*(?:-|–|—|to|through|until|till)\s*(?:" + WD_ANY + r",?\s+)?(?:(" + MON_RX + r")\.?\s+)?(\d{1,2})\b", t):
        if not free(m):
            continue
        d1 = mk_date(m.group(2), m.group(1), None, ref)
        d2 = mk_date(m.group(4), m.group(3) or m.group(1), None, ref)
        if d1 and d2 and d2 >= d1 and (d2 - d1).days < 400:
            out["start"], out["end"] = d1, d2; take(m)
    for m in re.finditer(r"(?:hasta (?:el|el dia)?|until|through|till|runs until|closes on)\s+(?:(?:el\s+)?" + WD_ANY + r",?\s+)?(?:(\d{1,2})\s+(?:de\s+|of\s+)?(" + MON_RX + r")|(" + MON_RX + r")\.?\s+(\d{1,2}))" + Y, t):
        if not free(m):
            continue
        d = mk_date(m.group(1) or m.group(4), m.group(2) or m.group(3), m.group(5), ref)
        if d:
            out["end"] = d; take(m)
    for m in re.finditer(r"(?:a partir del?|desde el|from|starting|opens on|abre el)\s+(?:(?:el\s+)?" + WD_ANY + r",?\s+)?(?:(\d{1,2})\s+(?:de\s+)?(" + MON_RX + r")|(" + MON_RX + r")\.?\s+(\d{1,2}))" + Y, t):
        if not free(m):
            continue
        d = mk_date(m.group(1) or m.group(4), m.group(2) or m.group(3), m.group(5), ref)
        if d:
            out["start"] = d; take(m)
    # --- opening ("inauguracion ... el lunes 26 de octubre, a las 18") --------------------
    m = re.search(r"(?:inaugura\w*|apertura|opening|vernissage)[^.]{0,60}?(\d{1,2})\s+de\s+(" + MON_RX + r")(?:[^.]{0,30}?a las\s+(\d{1,2})(?:[:.](\d{2}))?)?", t)
    if m:
        d = mk_date(m.group(1), m.group(2), None, ref)
        if d:
            out["opening"] = (d, hhmm(m.group(3), m.group(4)) if m.group(3) else None)
            if not out["start"]:
                out["start"] = d
            take(m)
    # --- single dates ----------------------------------------------------------------------
    for m in re.finditer(r"(?:(?:el|los|the)\s+)?(?:" + WD_ANY + r",?\s+)?(\d{1,2})(?:\s*(?:y|,|and)\s*(\d{1,2}))?(?:\s*(?:y|,|and)\s*(\d{1,2}))?\s+(?:de\s+|of\s+)?(" + MON_RX + r")\b" + Y, t):
        if not free(m):
            continue
        for g in (1, 2, 3):
            if m.group(g):
                d = mk_date(m.group(g), m.group(4), m.group(5), ref)
                if d:
                    out["dates"].add(d)
        take(m)
    for m in re.finditer(r"(?:" + WD_ANY + r",?\s+)?\b(" + MON_RX + r")\.?\s+(\d{1,2})\b(?!\s*(?:-|–|to|through)\s*\d)(?:,?\s+(\d{4}))?", t):
        if not free(m) or m.group(1) in ("mar", "sun", "sat", "dom"):
            continue
        d = mk_date(m.group(2), m.group(1), m.group(3), ref)
        if d:
            out["dates"].add(d); take(m)
    for m in re.finditer(r"(?<![\d/.$])\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b(?![\d/])", t):
        if not free(m):
            continue
        d = mk_date(m.group(1), int(m.group(2)), m.group(3), ref)
        if d:
            out["dates"].add(d); take(m)
    # --- weekdays --------------------------------------------------------------------------
    days = set()
    tt = t
    for a, b in sorted(spans, reverse=True):          # don't read "lunes 26 de octubre" as every Monday
        tt = tt[:a] + " " * (b - a) + tt[b:]
    if re.search(r"todos los dias|todos los dias de la semana|diariamente|de lunes a domingo|lunes a domingo|every day|daily|7 days a week|open daily|abierto todos los dias", tt):
        days.update(WD_ORDER)
    for m in re.finditer(r"(?:de\s+|from\s+)?(" + WD_ANY + r")\.?\s*(?:a|al|hasta|to|through|-|–|—)\s*(" + WD_ANY + r")\b", tt):
        try:
            days.update(wd_range(m.group(1), m.group(2)))
        except Exception:
            pass
    for m in re.finditer(r"(?:todos los|todas las|los|cada|every|on)\s+(" + WD_LONG + r")(?:\s*(?:,|y|e|and|&)\s*(" + WD_LONG + r"))?(?:\s*(?:,|y|e|and|&)\s*(" + WD_LONG + r"))?", tt):
        for g in (1, 2, 3):
            if m.group(g):
                days.add(WD[m.group(g)])
    for m in re.finditer(r"\b(" + WD_LONG + r")((?:\s*,\s*" + WD_LONG + r")*)\s+(?:y|e|and|&)\s+(" + WD_LONG + r")\b(?!\s*\d)", tt):
        for w_ in [m.group(1), m.group(3)] + re.findall(WD_LONG, m.group(2) or ""):
            days.add(WD[w_])
    for m in re.finditer(r"\b(lunes|martes|miercoles|jueves|viernes|sabados|domingos|mondays|tuesdays|wednesdays|thursdays|fridays|saturdays|sundays)\b", tt):
        w = m.group(1)
        if w.endswith("s") and w not in ("lunes", "martes", "miercoles", "jueves", "viernes"):
            days.add(WD[w])
    if days:
        out["days"] = [d for d in WD_ORDER if d in days]
    # --- hours -----------------------------------------------------------------------------
    for m in re.finditer(r"(?:\bde|desde las|from|open|,)\s*(\d{1,2})(?:[:.](\d{2}))?\s*(h|hs|horas|am|pm)?\s*(?:a|hasta(?: las)?|to|-|–|—)\s*(?:las\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(h|hs|horas|am|pm)?\b", tt):
        if re.match(r"\s*(?:de\s+)?(" + MON_RX + r")\b", t[m.end():m.end() + 14]):
            continue                                   # "del 30 de octubre al 1 de noviembre"
        h1, h2 = int(m.group(1)), int(m.group(4))
        if h1 > 24 or h2 > 24 or (m.group(2) is None and m.group(3) is None and m.group(6) is None and h1 < 7 and h2 < 7):
            continue
        a2 = m.group(6) if m.group(6) in ("am", "pm") else None
        a1 = m.group(3) if m.group(3) in ("am", "pm") else (a2 if a2 and h1 <= 12 and h1 <= h2 else None)
        o = hhmm(m.group(1), m.group(2), a1)
        c = hhmm(m.group(4), m.group(5), a2)
        if o and c:
            out["hours"] = (o, c); out["time"] = o
            break
    if not out["time"]:
        for rx in (r"(?:a las|a la|desde las|at|from)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|h|hs|horas)?\b",
                   r"\b(\d{1,2})[:.](\d{2})\s*(am|pm|h|hs|horas)?\b(?!\d)",
                   r"\b(\d{1,2})\s*(am|pm|hs|h)\b"):
            m = re.search(rx, tt)
            if m:
                g = m.groups()
                if len(g) == 2:
                    val = hhmm(g[0], None, g[1] if g[1] in ("am", "pm") else None)
                else:
                    if g[1] and len(g[1]) == 2 and rx.startswith(r"\b(\d{1,2})[:.]") and re.match(r"\d{3}", t[m.start(2):m.start(2) + 3] or ""):
                        continue
                    val = hhmm(g[0], g[1], g[2] if g[2] in ("am", "pm") else None)
                if val:
                    out["time"] = val
                    break
    return out

PRICE_FREE = re.compile(r"entrada (?:libre|gratuita)|libre y gratuit|gratuit[oa]s?\b|\bgratis\b|sin cargo|no arancelad|"
                        r"\bfree (?:entry|admission|entrance|of charge|event)\b|admission is free|entry is free|\bit'?s free\b|^free\b|\bfree\.", re.I)

def parse_price(text):
    t = text or ""
    nt = norm(t)
    res = {"free": False, "ars": None, "usd": None, "note": ""}
    if PRICE_FREE.search(nt) and not re.search(r"\$\s?\d", t):
        res["free"] = True
        res["note"] = "Free"
        return res
    ars = []
    for m in re.finditer(r"(?:ARS|\$)\s?(\d{1,3}(?:[.,\s]\d{3})+|\d{3,7})(?:,\d{2})?\b", t):
        v = int(re.sub(r"[.,\s]", "", m.group(1)))
        if 200 <= v <= 2_000_000:
            ars.append(v)
    for m in re.finditer(r"(?:USD|US\$|U\$S|u\$s|dólares)\s?(\d{1,4})(?:[.,]\d{2})?\b", t, re.I):
        res["usd"] = int(m.group(1))
    if ars:
        res["ars"] = min(ars)
        res["note"] = ("From " if len(set(ars)) > 1 or re.search(r"desde|from", nt) else "") + f"ARS {res['ars']:,}".replace(",", ".")
    if PRICE_FREE.search(nt) and ars:
        res["note"] += " (some free options)"
    return res

ADDR_RX = re.compile(
    r"((?:Av(?:enida|da)?\.?|Calle|Pje\.?|Pasaje|Bv\.?|Boulevard|Diagonal|Plaza)?\s*"
    r"(?:[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñü.'´]+|\d{1,2}\s+de\s+[A-ZÁÉÍÓÚÑ]\w+)"
    r"(?:\s+(?:de\s+|del\s+|la\s+|los\s+)?[A-ZÁÉÍÓÚÑ][\wÁÉÍÓÚÑáéíóúñü.'´]*){0,4})\s+(\d{2,5})\b(?!\s*(?:hs|h\b|horas|personas|pesos))")
BAD_ADDR_HEAD = re.compile(r"^(?:de|del|desde|hasta|a|en|y|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre|lunes|martes|miércoles|jueves|viernes|sábado|domingo|año|edición|temporada|sala|desde)\b", re.I)

def find_address(text):
    for m in ADDR_RX.finditer(text or ""):
        street, num = m.group(1).strip(), int(m.group(2))
        if BAD_ADDR_HEAD.match(street) or (1990 <= num <= 2035 and not re.match(r"(?i)(av|calle|pje|pasaje|bv)", street)):
            continue
        if len(street) < 3:
            continue
        return f"{street} {num}"
    return None

VENUE_WORDS = (r"Teatro|Museo|Centro Cultural|Centro|Espacio|Galer[ií]a|Sala|Club|Usina|Complejo|Biblioteca|Casa|"
               r"Fundaci[oó]n|Auditorio|Instituto|Palacio|Planetario|Parque|Plaza|Mercado|Feria|Bar|Caf[eé]|"
               r"Cine|Cinema|Estadio|Hip[oó]dromo|La Rural|Costa Salguero|Tecn[oó]polis|CCK|Usina del Arte|Konex|"
               r"Niceto|Bebop|Thelonious|Teatro Col[oó]n|Malba|MALBA|MAMBA|Recoleta|Borges|Kirchner|Polo|Distrito")

def find_venue(text):
    m = re.search(r"\b(?:en|at|in)\s+(?:el|la|los|las|the)?\s*((?:" + VENUE_WORDS + r")[^,.;:()\n]{0,60})", text or "")
    if m:
        v = re.split(r"\s+(?:hasta|desde|durante|a las|a partir|los|las|el|del \d|con entrada|todos|cada|este|esta|y\s+(?:el|la)|que)\b", m.group(1).strip())[0]
        v = re.sub(r"\s+(?:de|del|y|con|a|para|el|la)$", "", v.strip())
        return v[:80]
    return None

# --------------------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------------------
_last = {}
_session = None

def fetch(url, cache_dir=None, timeout=15, browser=False):
    """GET a page; returns (status, final_url, html). Polite: <= 2 requests/second per domain."""
    global _session
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cf = os.path.join(cache_dir, hashlib.md5(url.encode()).hexdigest() + ".json")
        if os.path.exists(cf):
            try:
                c = json.load(open(cf))
                return c["status"], c["url"], c["html"]
            except Exception:
                pass
    import requests
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9,en;q=0.8"})
    d = domain(url)
    wait = 0.5 - (time.time() - _last.get(d, 0))
    if wait > 0:
        time.sleep(wait)
    _last[d] = time.time()
    status, final, html = 0, url, ""
    try:
        r = _session.get(url, timeout=timeout, allow_redirects=True)
        status, final = r.status_code, r.url
        r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else r.apparent_encoding
        html = r.text if r.status_code < 400 else ""
    except Exception as ex:
        html = ""
        status = 0
    if browser and (not html or len(re.sub(r"<[^>]+>", " ", html)) < 2000):
        h2 = render(url)
        if h2:
            html, status = h2, status or 200
    if cache_dir and html:
        json.dump({"status": status, "url": final, "html": html}, open(cf, "w"))
    return status, final, html

def render(url):
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""
    exe = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if exe else p.chromium.launch(args=["--no-sandbox"])
            pg = b.new_page(user_agent=UA, ignore_https_errors=True)
            pg.goto(url, wait_until="networkidle", timeout=20000)
            h = pg.content()
            b.close()
            return h
    except Exception:
        return ""

# --------------------------------------------------------------------------------------
# article parsing -> blocks -> items
# --------------------------------------------------------------------------------------
NOISE = re.compile(r"^(publicidad|advertising|advertisement|sponsored|patrocinado|newsletter|suscrib|subscribe|sign up|"
                   r"been there, done that|le[eé] tambi[eé]n|seguir leyendo|mir[aá] tambi[eé]n|te puede interesar|"
                   r"related|recommended|recomendado|compartir|share|seguinos|follow us|foto:|photo:|cr[eé]dito|"
                   r"getty|shutterstock|\(foto|fuente:|leer m[aá]s|read more|ver m[aá]s|thank you for subscribing|"
                   r"awesome, you're subscribed|by entering your email|qu[eé] hacer en buenos aires esta semana$)", re.I)
SEP = re.compile(r"^[\s\-–—_:·•*=~.|]{3,}$")

class Block:
    __slots__ = ("kind", "level", "text", "links", "imgs", "bold", "html")
    def __init__(self, kind, text="", level=0, links=None, imgs=None, bold=False):
        self.kind, self.text, self.level = kind, text, level
        self.links, self.imgs, self.bold = links or [], imgs or [], bold
    def __repr__(self):
        return f"<{self.kind}{self.level or ''}{' B' if self.bold else ''} {self.text[:60]!r}>"

def _img_src(img, base):
    for a in ("data-src", "data-lazy-src", "data-original", "src"):
        v = img.get(a)
        if v and not v.startswith("data:"):
            return urllib.parse.urljoin(base, v)
    ss = img.get("srcset") or img.get("data-srcset")
    if ss:
        best = sorted(((int(re.sub(r"\D", "", p.split()[-1]) or 0) if len(p.split()) > 1 else 0, p.split()[0]) for p in ss.split(",") if p.strip()), reverse=True)
        if best:
            return urllib.parse.urljoin(base, best[0][1])
    return None

def _bold_share(el):
    txt = el.get_text(" ", strip=True)
    if not txt:
        return 0
    b = " ".join(x.get_text(" ", strip=True) for x in el.find_all(["strong", "b"]) if not x.find_parent(["strong", "b"]))
    return len(b) / max(1, len(txt))

BLOCK_TAGS = {"p", "li", "blockquote", "figcaption", "dd", "dt", "pre"}
HEAD_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "iframe", "svg", "button",
             "select", "input", "template"}

def walk(el, base, out):
    from bs4 import NavigableString, Tag
    for ch in el.children:
        if isinstance(ch, NavigableString):
            s = str(ch).strip()
            if len(s) > 2 and el.name in ("div", "section", "article", "main"):
                out.append(Block("p", re.sub(r"\s+", " ", s)))
            continue
        if not isinstance(ch, Tag):
            continue
        n = ch.name
        cls = " ".join(ch.get("class") or []).lower()
        if n in SKIP_TAGS or re.search(r"newsletter|related|share|social|promo|advert|\bad-|banner|subscribe|recirc|outbrain|taboola|comments", cls):
            continue
        if n in HEAD_TAGS:
            t = ch.get_text(" ", strip=True)
            if t:
                out.append(Block("h", t, HEAD_TAGS[n], links=[(a.get_text(" ", strip=True), urllib.parse.urljoin(base, a.get("href", ""))) for a in ch.find_all("a", href=True)]))
            continue
        if n == "hr":
            out.append(Block("sep")); continue
        if n in ("img", "picture", "figure"):
            imgs = [_img_src(i, base) for i in ([ch] if n == "img" else ch.find_all("img"))]
            imgs = [i for i in imgs if i]
            if imgs:
                out.append(Block("img", imgs=imgs))
            cap = ch.find("figcaption") if n == "figure" else None
            continue
        if n in BLOCK_TAGS or (n in ("div", "span") and not ch.find(list(BLOCK_TAGS | set(HEAD_TAGS) | {"div", "figure", "img", "ul", "ol"}))):
            if ch.find(list(BLOCK_TAGS - {"figcaption"}) + list(HEAD_TAGS)) and n not in ("li",):
                walk(ch, base, out); continue
            t = re.sub(r"\s+", " ", ch.get_text(" ", strip=True))
            imgs = [x for x in (_img_src(i, base) for i in ch.find_all("img")) if x]
            if imgs:
                out.append(Block("img", imgs=imgs))
            if t:
                links = [(a.get_text(" ", strip=True), urllib.parse.urljoin(base, a.get("href", ""))) for a in ch.find_all("a", href=True)]
                out.append(Block("p", t, links=links, bold=_bold_share(ch) >= 0.85))
            continue
        walk(ch, base, out)

def article_root(soup):
    for sel in ['[itemprop="articleBody"]', "article .article-body", "article", '[class*="article-body"]',
                '[class*="articleBody"]', '[class*="body-article"]', '[class*="_article"]', "main", '[role="main"]']:
        el = soup.select_one(sel)
        if el and len(el.get_text(" ", strip=True)) > 400:
            return el
    best, score = soup.body or soup, 0
    for d in soup.find_all(["div", "section"]):
        s = sum(len(p.get_text(" ", strip=True)) for p in d.find_all("p", recursive=False))
        if s > score:
            best, score = d, s
    return best

def article_meta(soup, url):
    meta = {"title": "", "published": None, "image": None}
    def m(*names):
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n}) or soup.find("meta", attrs={"itemprop": n})
            if t and t.get("content"):
                return t["content"]
        return None
    meta["title"] = (m("og:title") or (soup.title.get_text(strip=True) if soup.title else "")).strip()
    meta["image"] = m("og:image", "twitter:image")
    pub = m("article:published_time", "datePublished", "date", "pubdate", "article:modified_time")
    if not pub:
        for s in soup.find_all("script", type="application/ld+json"):
            mm = re.search(r'"datePublished"\s*:\s*"([^"]+)"', s.string or s.get_text() or "")
            if mm:
                pub = mm.group(1); break
    if not pub:
        mm = re.search(r"/(20\d\d)/(\d{1,2})/(\d{1,2})/", url)
        if mm:
            pub = f"{mm.group(1)}-{int(mm.group(2)):02d}-{int(mm.group(3)):02d}"
    if pub:
        mm = re.match(r"(\d{4})-(\d{2})-(\d{2})", pub)
        if mm:
            meta["published"] = dt.date(int(mm.group(1)), int(mm.group(2)), int(mm.group(3)))
    return meta

HOODS = (r"palermo|recoleta|san telmo|belgrano|villa crespo|chacarita|almagro|colegiales|retiro|microcentro|monserrat|"
         r"puerto madero|la boca|nunez|saavedra|caballito|boedo|barracas|balvanera|once|abasto|villa urquiza|coghlan|"
         r"villa ortuzar|parque patricios|flores|floresta|mataderos|liniers|devoto|villa del parque|agronomia|"
         r"tigre|san isidro|vicente lopez|olivos|martinez|quilmes|avellaneda|lanus|la plata|banfield|adrogue|moron|ramos mejia")

def looks_like_venue_line(t):
    nt = norm(t)
    if len(t) >= 220 or re.search(r"\b(hasta|desde|a las|de \d+ a \d+|entrada|gratuit)\b", nt):
        return False
    return bool(re.search(r"\b\d{2,5}\b|c\.?\s?a\.?\s?b\.?\s?a|\bcaba\b|intersecci|esquina|\bav\.?\s|avenida|\bcalle\b", nt)
                or ("," in t and re.search(r"\b(" + HOODS + r")\b", nt)))

def is_title_like(b):
    t = b.text
    return b.kind == "p" and b.bold and 3 <= len(t) <= 160 and not NOISE.search(t) and not t.rstrip().endswith((":",))

def blocks_from_html(html, url):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    ld = [s.get_text() for s in soup.find_all("script", type="application/ld+json")]
    meta = article_meta(soup, url)
    root = article_root(soup)
    blocks = []
    walk(root, url, blocks)
    clean = []
    for b in blocks:
        if b.kind in ("p", "h"):
            if NOISE.search(b.text.strip()) or (b.kind == "p" and SEP.match(b.text)):
                if SEP.match(b.text or ""):
                    clean.append(Block("sep"))
                continue
        clean.append(b)
    return meta, clean, ld

def segment(blocks):
    """Split the block stream into items. Returns list of dicts."""
    heads = [b for b in blocks if b.kind == "h" and b.level >= 2]
    lvl_count = Counter(b.level for b in heads)
    numbered = Counter(b.level for b in heads if re.match(r"^\d{1,2}\s*[.)\-–]\s", b.text))
    item_level = None
    def is_section(h):
        t = h.text.strip()
        return (t.isupper() and len(t) < 40) or len(t) > 140
    bold_titles = sum(1 for b in blocks if is_title_like(b) and not looks_like_venue_line(b.text))
    if numbered and max(numbered.values()) >= 3:
        item_level = max(numbered, key=numbered.get)
    else:
        lvl_items = Counter(b.level for b in heads if not is_section(b))
        cands = [l for l, c in lvl_items.items() if c >= 3]
        if cands:
            L = max(cands)                   # the deepest repeated heading level holds the items
            if not (bold_titles >= 3 and bold_titles > lvl_items[L]):
                item_level = L
    items, cur, section, group = [], None, "", ""

    def close():
        nonlocal cur
        if cur and (cur["body"] or cur["venue_lines"]):
            items.append(cur)
        cur = None

    def new(title, link=None):
        nonlocal cur
        close()
        cur = {"title": clean_title(title), "section": section, "group": group, "body": [], "links": [],
               "imgs": [], "venue_lines": [], "labels": {}, "pre_imgs": list(pending_imgs)}
        if link:
            cur["links"].extend(link)

    pending_imgs = []
    use_bold = item_level is None
    for i, b in enumerate(blocks):
        nxt = blocks[i + 1] if i + 1 < len(blocks) else None
        if b.kind == "sep":
            close(); pending_imgs = []; continue
        if b.kind == "img":
            if cur:
                cur["imgs"].extend(b.imgs)
            else:
                pending_imgs = b.imgs
            continue
        if b.kind == "h":
            if item_level and b.level == item_level:
                new(b.text, b.links); pending_imgs = []
            elif item_level and b.level < item_level:
                close(); section = b.text; group = ""
            elif not item_level:
                close(); section = b.text; group = ""
            elif cur:
                cur["body"].append(b.text)
            continue
        # paragraph
        txt = b.text
        lab = re.match(r"^\s*(d[oó]nde|where|direcci[oó]n|address|cu[aá]ndo|when|horarios?|hours|d[ií]as|precio|entradas?|"
                       r"price|cost|valor|good to know|dato [uú]til|tickets?|reservas?|book(?:ing)?|qui[eé]n(?:es)?|who|web|info)\s*[:\-–]\s*(.+)$", txt, re.I)
        if use_bold and is_title_like(b):
            if cur and cur["body"] and looks_like_venue_line(txt):
                cur["venue_lines"].append(txt); cur["links"].extend(b.links); close(); continue
            if nxt is not None and nxt.kind == "p" and is_title_like(nxt) and not looks_like_venue_line(nxt.text) and not (cur and not cur["body"]):
                close(); group = txt                      # bold line followed by another title = venue/program group
                continue
            new(txt, b.links); pending_imgs = []
            continue
        if cur is None:
            continue                                      # intro text before the first item
        if lab:
            cur["labels"][norm(lab.group(1))] = lab.group(2).strip()
        cur["body"].append(txt)
        cur["links"].extend(b.links)
    close()
    return items

# --------------------------------------------------------------------------------------
# item -> concrete posting
# --------------------------------------------------------------------------------------
GENRE_HINTS = [
    ("museums", r"museo|museum|exhibici|exposici|muestra|exhibition"), ("galleries", r"galer|gallery|arte contempor|pintura|fotograf|inaugura"),
    ("theater", r"teatro|obra|theater|theatre|stand.?up|comedia|musical|circo|danza contempor"),
    ("music", r"concierto|recital|banda|jazz|orquesta|show en vivo|live music|concert|gig|cantante|rock|tango show|piano"),
    ("nightlife", r"fiesta|party|club|dj\b|techno|boliche|after"), ("tango", r"milonga|tango|salsa|bachata|baile|dance|danza|swing"),
    ("classes", r"taller|workshop|clase|class|curso|course|seminario"), ("markets", r"feria|market|mercado|vintage|antig[uü]edades"),
    ("film", r"\bcine\b|film|pel[ií]cula|screening|charla|\btalks?\b|libro|\bbooks?\b(?! via| on| a table)|lectura|poes[ií]a|presentaci[oó]n de"),
    ("food", r"restaurante|restaurant|\bbar\b|caf[eé]|parrilla|vino|wine|cerveza|beer|brunch|cocina|chef|men[uú]|pizza|food|bodeg[oó]n|cena|dinner|gnocchi|tasting|degustaci|omakase|pasta"),
    ("outdoor", r"parque|park|bici|bike|run|carrera|kayak|reserva|paseo|caminata|trekking|aire libre|outdoor"),
    ("events", r"networking|conferencia|conference|expo|summit|meetup|startup|feria de empleo"),
]

def guess_genre(text, title=""):
    nt = norm(text)
    nti = norm(title)
    best, n = None, 0
    for g, rx in GENRE_HINTS:
        c = len(re.findall(rx, nt)) + 3 * len(re.findall(rx, nti))
        if c > n:
            best, n = g, c
    return best

def read_item(item, outlet_url, ref):
    """Extract concrete fields from one item."""
    body = " ".join(item["body"])
    labels = item["labels"]
    when_txt = " ".join(v for k, v in labels.items() if re.match(r"cu[aá]ndo|when|horario|hours|dias", k)) + " " + body
    w = parse_when(when_txt, ref)
    pr_txt = " ".join(v for k, v in labels.items() if re.match(r"precio|entrada|price|cost|valor|ticket", k)) or body
    price = parse_price(pr_txt if pr_txt.strip() else body)
    if not (price["free"] or price["ars"]):
        price = parse_price(body)
    # where
    where_txt = next((v for k, v in labels.items() if re.match(r"d[oó]nde|where|direcci|address", k)), "")
    venue, address, hood, maps = None, None, None, None
    for t, u in item["links"]:
        if is_maps(u):
            maps = maps or u
            if find_address(t) or re.search(r"\d", t):
                address = address or t.strip(" .,")
    if item["venue_lines"]:
        vl = item["venue_lines"][-1].strip(" *_")
        parts = [p.strip(" .") for p in vl.split(",") if p.strip(" .")]
        parts = [p for p in parts if not re.match(r"(?i)^(c\.?\s?a\.?\s?b\.?\s?a\.?|caba|ciudad (?:autonoma )?de buenos aires|buenos aires|argentina)$", strip_accents(p))]
        if parts:
            ai = next((k for k, p in enumerate(parts) if find_address(p) or re.search(r"(?i)intersecci|esquina|\by\b.*\b(av|calle)", p)), None)
            if ai is None:
                venue = parts[0]
            else:
                if ai > 0:
                    venue = ", ".join(parts[:ai])
                address = address or parts[ai]
                if ai + 1 < len(parts) and len(parts[ai + 1]) < 40 and not find_address(parts[ai + 1]):
                    hood = parts[ai + 1]
    if where_txt:
        a = find_address(where_txt)
        address = address or a
        segs = [s.strip(" .") for s in re.split(r"[,;]", where_txt) if s.strip(" .")]
        if segs:
            if a and len(segs) > 1:
                hood = hood or (segs[-1] if not find_address(segs[-1]) and len(segs[-1]) < 40 else None)
            elif not a:
                venue = venue or segs[0]
    if not venue and item.get("group") and not re.search(r"\d{2,}", item["group"]):
        g = item["group"].strip(" *_")
        if re.search(VENUE_WORDS, g) and not re.search(r"^(muestras?|exposiciones|arte|teatro)\b", norm(g)):
            venue = re.sub(r"^(?:Muestras?|Arte|Exposiciones?) en (?:el |la )?", "", g)
    if not venue:
        # Time Out: the first short body line under the images is often the venue/organizer
        if item["body"] and len(item["body"][0]) <= 45 and not re.search(r"[.!?]$", item["body"][0]) and not find_address(item["body"][0]):
            venue = item["body"][0]
    venue = venue or find_venue(body)
    address = address or find_address(body)
    platforms = sorted({dom for word, dom in PLATFORM_WORDS.items() if re.search(r"\b" + re.escape(word) + r"\b", norm(body))})
    ig = re.findall(r"@([A-Za-z0-9_.]{3,30})", body)
    web = re.findall(r"\b((?:www\.)?[a-z0-9\-]+\.(?:com|com\.ar|org\.ar|org|ar|net|gob\.ar|gov\.ar)(?:/[\w\-./?=&%]*)?)", body, re.I)
    web = [w for w in web if not is_blog("https://" + w) and "." in w and not re.search(r"\.(jpg|png)$", w)]
    img = (item["imgs"] or item.get("pre_imgs") or [None])[0]
    return {
        "title": item["title"], "section": item["section"], "group": item["group"],
        "venue": (venue or "").strip(" .,*_")[:100] or None, "address": address, "neighborhood": hood, "mapsUrl": maps,
        "when": w, "price": price, "platformsMentioned": platforms, "instagramHandles": ig[:3], "webMentioned": web[:3],
        "links": [(t, u) for t, u in item["links"] if link_rank(u) < 9][:8],
        "blogImg": img, "foundOn": outlet_url, "snippet": body[:600],
        "genreGuess": guess_genre(item["section"] + " " + body[:400], item["title"]),
    }

def when_fields(w, today):
    """Map parse_when output onto the app's fields."""
    f = {}
    dates = sorted(d for d in w["dates"] if d >= today)
    if w["start"] or w["end"] or w["days"] or w["hours"]:
        f["kind"] = "ongoing"
        f["days"] = w["days"] or ([] if not w["hours"] else WD_ORDER)
        if w["start"]:
            f["start"] = w["start"].isoformat()
        if w["end"]:
            f["end"] = w["end"].isoformat()
        if (w["start"] and w["end"] and not w["days"] and (w["end"] - w["start"]).days <= 4):
            f = {"kind": "event", "dates": [(w["start"] + dt.timedelta(days=i)).isoformat() for i in range((w["end"] - w["start"]).days + 1)
                                              if w["start"] + dt.timedelta(days=i) >= today]}
            if w["hours"]:
                f["time"] = w["hours"][0]
        elif dates and not w["days"] and not w["start"] and not w["end"]:
            f = {"kind": "event", "dates": [d.isoformat() for d in dates]}
    elif dates:
        f["kind"] = "event"
        f["dates"] = [d.isoformat() for d in dates]
    if w["time"]:
        f["time"] = w["time"]
    return f

def when_note(w):
    bits = []
    if w.get("opening"):
        d, t = w["opening"]
        bits.append("Opening " + d.strftime("%a %d %b") + (f" {t}" if t else ""))
    if w.get("days"):
        ds = w["days"]
        run = ds == WD_ORDER[WD_ORDER.index(ds[0]):WD_ORDER.index(ds[0]) + len(ds)]
        lab = "daily" if len(ds) == 7 else ds[0].title() if len(ds) == 1 else ("–".join([ds[0].title(), ds[-1].title()]) if run and len(ds) > 2 else ", ".join(d.title() for d in ds))
        bits.append(lab + (f" {w['hours'][0]}–{w['hours'][1]}" if w.get("hours") else (f" {w['time']}" if w.get("time") else "")))
    elif w.get("hours"):
        bits.append(f"{w['hours'][0]}–{w['hours'][1]}")
    if w.get("end"):
        bits.append("until " + w["end"].strftime("%d %b"))
    if w.get("dates") and not w.get("days"):
        ds = sorted(w["dates"])[:4]
        bits.append(", ".join(d.strftime("%a %d %b") for d in ds) + (f" {w['time']}" if w.get("time") else ""))
    return " · ".join(bits)[:90]

def fields_note(f, w):
    """Short human note of the final timing, e.g. 'Sat 31 Oct, Sat 07 Nov 20:30'."""
    if f.get("kind") == "event" and f.get("dates"):
        ds = [dt.date.fromisoformat(d) for d in f["dates"][:4]]
        more = f" +{len(f['dates']) - 4}" if len(f["dates"]) > 4 else ""
        return (", ".join(d.strftime("%a %d %b") for d in ds) + more + (f" {f['time']}" if f.get("time") else ""))[:90]
    w2 = dict(w)
    w2["days"] = f.get("days") or w.get("days")
    if f.get("end"):
        w2["end"] = dt.date.fromisoformat(f["end"])
    w2["dates"] = set()
    return when_note(w2)

# --------------------------------------------------------------------------------------
# original page
# --------------------------------------------------------------------------------------
def jsonld_objects(texts):
    out = []
    for t in texts:
        try:
            data = json.loads(t)
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", t))
            except Exception:
                continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                out.append(x)
                for k in ("@graph", "subEvent", "event", "events", "itemListElement", "item", "mainEntity"):
                    if k in x:
                        stack.append(x[k])
    return out

def typ(o):
    t = o.get("@type")
    return " ".join(t) if isinstance(t, list) else (t or "")

DAYMAP = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
          "mo": "mon", "tu": "tue", "we": "wed", "th": "thu", "fr": "fri", "sa": "sat", "su": "sun"}

def read_original(url, today, cache_dir=None, browser=False, title=None):
    """Read the original page: JSON-LD Event / Place, og tags. Returns dict of facts it states."""
    st, final, html = fetch(url, cache_dir, browser=browser)
    res = {"status": st, "url": final or url, "ok": bool(html)}
    if not html:
        return res
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    lds = jsonld_objects([s.get_text() for s in soup.find_all("script", type="application/ld+json")])
    og = lambda p: (soup.find("meta", attrs={"property": p}) or soup.find("meta", attrs={"name": p}) or {}).get("content") if (soup.find("meta", attrs={"property": p}) or soup.find("meta", attrs={"name": p})) else None
    res["image"] = og("og:image") or og("twitter:image")
    res["title"] = og("og:title") or (soup.title.get_text(strip=True) if soup.title else "")
    dates, times, price, place = set(), [], None, {}
    hours, days = None, set()
    for o in lds:
        t = typ(o)
        if "Event" in t or "Screening" in t or "Performance" in t:
            if title and o.get("name") and sim(title, o.get("name")) < 0.34 and len([x for x in lds if "Event" in typ(x)]) > 1:
                continue
            for k in ("startDate",):
                v = o.get(k)
                if isinstance(v, str):
                    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?", v)
                    if m:
                        d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                        if d >= today:
                            dates.add(d)
                        if m.group(4):
                            hh = int(m.group(4))
                            if re.search(r"Z|\+00:?00$", v):      # UTC -> Buenos Aires
                                hh = (hh - 3) % 24
                            times.append(f"{hh:02d}:{m.group(5)}")
            e = o.get("endDate")
            if isinstance(e, str) and re.match(r"\d{4}-\d{2}-\d{2}", e):
                res["end"] = e[:10]
            loc = o.get("location")
            if isinstance(loc, list):
                loc = loc[0] if loc else None
            if isinstance(loc, dict):
                place["venue"] = loc.get("name")
                a = loc.get("address")
                if isinstance(a, dict):
                    place["address"] = ", ".join(x for x in [a.get("streetAddress"), a.get("addressLocality")] if x)
                elif isinstance(a, str):
                    place["address"] = a
            offers = o.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if isinstance(offers, list):
                ps = []
                for of in offers:
                    if isinstance(of, dict):
                        for k in ("price", "lowPrice"):
                            try:
                                ps.append(float(str(of.get(k)).replace(",", ".")))
                            except Exception:
                                pass
                        cur = of.get("priceCurrency")
                if ps:
                    price = {"value": min(ps), "currency": (cur or "ARS")}
            if not res.get("image"):
                im = o.get("image")
                res["image"] = im[0] if isinstance(im, list) and im else (im.get("url") if isinstance(im, dict) else im)
        if any(k in t for k in ("Place", "LocalBusiness", "Restaurant", "BarOrPub", "Museum", "Store", "CafeOrCoffeeShop",
                                "FoodEstablishment", "NightClub", "TouristAttraction", "PerformingArtsTheater", "ArtGallery")):
            a = o.get("address")
            if isinstance(a, dict) and not place.get("address"):
                place["address"] = ", ".join(x for x in [a.get("streetAddress"), a.get("addressLocality")] if x)
            place.setdefault("venue", o.get("name"))
            for spec in (o.get("openingHoursSpecification") or []) if isinstance(o.get("openingHoursSpecification"), list) else [o.get("openingHoursSpecification")] if o.get("openingHoursSpecification") else []:
                if not isinstance(spec, dict):
                    continue
                dw = spec.get("dayOfWeek")
                dw = dw if isinstance(dw, list) else [dw]
                for d in dw:
                    k = str(d or "").split("/")[-1].lower()
                    if k in DAYMAP:
                        days.add(DAYMAP[k])
                if spec.get("opens") and spec.get("closes"):
                    hours = (spec["opens"][:5], spec["closes"][:5])
            oh = o.get("openingHours")
            if oh and not days:
                for s in (oh if isinstance(oh, list) else [oh]):
                    m = re.match(r"([A-Za-z]{2})(?:-([A-Za-z]{2}))?\s+(\d{2}:\d{2})-(\d{2}:\d{2})", str(s))
                    if m:
                        a, b = DAYMAP.get(m.group(1).lower()), DAYMAP.get((m.group(2) or m.group(1)).lower())
                        if a and b:
                            i, j = WD_ORDER.index(a), WD_ORDER.index(b)
                            while True:
                                days.add(WD_ORDER[i])
                                if i == j:
                                    break
                                i = (i + 1) % 7
                            hours = (m.group(3), m.group(4))
    if dates:
        res["dates"] = sorted(d.isoformat() for d in dates)
    if times:
        res["time"] = Counter(times).most_common(1)[0][0]
    if price:
        res["price"] = price
    if place:
        res["place"] = place
    if days:
        res["days"] = [d for d in WD_ORDER if d in days]
    if hours:
        res["hours"] = hours
    # no JSON-LD dates: look for the title in the page text and read the dates written near it
    if not dates and title:
        text = soup.get_text("\n", strip=True)
        nt = norm(text)
        toks = [w for w in tokens(title) if len(w) > 3][:4]
        pos = [nt.find(w) for w in toks if nt.find(w) >= 0]
        if pos:
            win = text[max(0, min(pos) - 200): min(pos) + 900]
            w = parse_when(win, today)
            res["textWhen"] = w
    return res

# --------------------------------------------------------------------------------------
# databases: events in the app, venue directory
# --------------------------------------------------------------------------------------
def load_events(d):
    ev = {}
    for f in glob.glob(os.path.join(d, "*.json")):
        try:
            e = json.load(open(f))
        except Exception:
            continue
        e = e.get("data", e)
        if e.get("id"):
            ev[e["id"]] = e
    return ev

def load_directory(path):
    if not path or not os.path.exists(path):
        return []
    if os.path.isdir(path):                         # db/directory with meta + part<k>.json
        try:
            meta = json.load(open(os.path.join(path, "meta.json")))
            meta = meta.get("data", meta)
            s = "".join(json.load(open(os.path.join(path, f"part{k}.json"))).get("s", "") for k in range(int(meta.get("chunks", 0))))
            return json.loads(s)
        except Exception:
            return []
    try:
        return json.load(open(path))
    except Exception:
        return []

class Index:
    def __init__(self, events, directory):
        self.events = events
        self.orig = [e for e in events.values() if e.get("url") and not is_blog(e["url"]) and not e.get("hidden")]
        self.by_tok = defaultdict(list)
        for e in self.orig:
            for w in set(tokens(e.get("title", ""))):
                self.by_tok[w].append(e)
        self.dir = [d for d in directory if d.get("n")]
        self.dir_tok = defaultdict(list)
        for d in self.dir:
            for w in set(vtokens(d["n"])):
                self.dir_tok[w].append(d)

    def match_event(self, title, venue=None, dates=None, kind=None, address=None):
        """The same event already in the app from a ticket platform / agenda. Strict: a wrong match
        would hide a listing, so short titles need the venue to agree too."""
        cands = Counter()
        for w in set(tokens(title)):
            if len(self.by_tok.get(w, [])) < 400:
                for e in self.by_tok.get(w, []):
                    cands[e["id"]] += 1
        best, bs = None, 0
        A = set(ttoks(title))
        for eid, _ in cands.most_common(60):
            e = self.events[eid]
            B = set(ttoks(e.get("title", "")))
            if not A or not B:
                continue
            jac = len(A & B) / len(A | B)
            cov = len(A & B) / min(len(A), len(B))
            vs = vsim(venue, e.get("venue")) if venue and e.get("venue") else 0
            dok = bool(dates and e.get("dates") and set(dates) & set(e["dates"]))
            if kind == "ongoing" and e.get("kind") == "event" and not (jac >= 0.9 and vs >= 0.6):
                continue                                   # a place is not the same thing as a one-off event
            na = re.findall(r"\b\d{2,5}\b", (address or "") + " " + (venue or "")); nb = re.findall(r"\b\d{2,5}\b", e.get("venue") or "")
            if na and nb and na[0] != nb[0]:
                continue                                   # another branch / another address
            same_place = bool(na and nb and na[0] == nb[0] and vs >= 0.5 and kind == "ongoing" and e.get("kind") == "ongoing" and jac >= 0.3)
            ok = same_place or (jac >= 0.8 and min(len(A), len(B)) >= 2 and (vs >= 0.5 or dok or not venue)) or \
                 (cov >= 0.99 and min(len(A), len(B)) >= 2 and abs(len(A) - len(B)) <= 1 and vs >= 0.6 and not ((A ^ B) & TYPEWORDS)) or \
                 (jac >= 0.99 and vs >= 0.6)
            sc = jac + 0.3 * vs + 0.2 * dok
            if ok and sc > bs:
                best, bs = e, sc
        return (best, bs) if best else (None, 0)

    def match_venue(self, name, address=None, genre=None):
        if not name:
            return None
        pool = {}
        for w in set(vtokens(name)):
            for d in self.dir_tok.get(w, [])[:300]:
                pool[id(d)] = d
        best, bs = None, 0
        num = re.findall(r"\b\d{2,5}\b", address or "")
        okt = GENRE_TYPES.get(genre or "")
        for d in pool.values():
            v = vsim(name, d["n"])
            nd = re.findall(r"\b\d{2,5}\b", d.get("ad") or "")
            if num and nd:
                if num[0] != nd[0]:
                    continue                               # different street number: different place
                ok = v >= 0.5
            else:
                ok = v >= 0.85 and (not okt or d.get("t") in okt)
            if ok and v > bs:
                best, bs = d, v
        return best

GENRE_TYPES = {
    "food": {"bar", "venue", "club", "other", "market", "music-venue", "milonga"},
    "newplaces": {"bar", "venue", "club", "other", "market", "music-venue", "bookstore", "record-store"},
    "music": {"music-venue", "bar", "club", "venue", "theater", "cultural-center", "milonga", "stadium", "other"},
    "nightlife": {"music-venue", "bar", "club", "venue", "other", "milonga"},
    "theater": {"theater", "cultural-center", "venue", "club", "other", "bar", "music-venue"},
    "museums": {"museum", "gallery", "cultural-center", "other", "library", "venue"},
    "galleries": {"gallery", "museum", "cultural-center", "other", "venue", "bookstore"},
    "tango": {"milonga", "club", "cultural-center", "venue", "theater", "bar", "school", "community", "music-venue"},
    "film": {"cinema", "cultural-center", "library", "bookstore", "museum", "theater", "venue"},
    "classes": {"school", "cultural-center", "library", "community", "venue", "bookstore", "other", "club"},
    "markets": {"market", "other", "community", "venue"},
}

def clean_site(u):
    u = (u or "").strip()
    if not u or re.search(r"\.(jpe?g|png|gif|webp|pdf)(\?|#|$)", u, re.I):
        return None
    u = re.sub(r"(?i)^(https?://)+", "", u)
    u = re.sub(r"(?i)^(https?:)?/+", "", u)
    host, _, path = u.partition("/")
    host = host.lower()
    if path and path == path.upper():
        path = path.lower()                              # directory entries are often ALL CAPS
    if "." not in host:
        return None
    return "https://" + host + ("/" + path if path else "/")

# --------------------------------------------------------------------------------------
# one listing / one item -> fields to write
# --------------------------------------------------------------------------------------
def resolve(info, idx, today, cache_dir=None, browser=False, read_pages=True):
    """Find the original page for an item, read it, and merge (original wins)."""
    w = info["when"]
    wf = when_fields(w, today)
    out = {"linkType": None, "url": None, "dupOf": None, "fields": {}, "detailsFrom": "blog", "missing": [], "searchHint": None}
    # 1. a link inside the item
    links = sorted(info["links"], key=lambda tu: link_rank(tu[1]))
    if links and link_rank(links[0][1]) <= 2:
        out["url"] = links[0][1]
        out["linkType"] = {0: "event", 1: "venue", 2: "instagram"}[link_rank(links[0][1])]
    # 2. the same event already in the app from a ticket platform / agenda
    m, s = idx.match_event(info["title"], info.get("venue"), wf.get("dates"), info.get("kindHint") or wf.get("kind"), info.get("address"))
    if m and (not out["url"] or out["linkType"] != "event"):
        out["url"], out["linkType"], out["dupOf"] = m["url"], "event", m["id"]
    # 3. venue directory (agenda page > website > instagram)
    v = idx.match_venue(info.get("venue"), info.get("address"), info.get("genreGuess")) if info.get("venue") else None
    if v:
        if not info.get("address") and v.get("ad"):
            info["address"] = v["ad"].replace(", CABA", "").title()
        info["neighborhood"] = info.get("neighborhood") or v.get("a2")
        info["lat"], info["lng"] = v.get("la"), v.get("ln")
        if not out["url"]:
            site = clean_site(v.get("ag")) or clean_site(v.get("w"))
            if site and not is_blog(site) and "thefork" not in site:
                out["url"], out["linkType"] = site, "venue"
            elif v.get("ig"):
                ig = v["ig"]
                out["url"], out["linkType"] = ig if ig.startswith("http") else "https://www.instagram.com/" + ig.strip("@/") + "/", "instagram"
    # 4. websites / Instagram named in the text
    if not out["url"] and info.get("webMentioned"):
        out["url"], out["linkType"] = "https://" + info["webMentioned"][0].lstrip("/"), "venue"
    if not out["url"] and info.get("instagramHandles"):
        out["url"], out["linkType"] = "https://www.instagram.com/" + info["instagramHandles"][0] + "/", "instagram"
    # 5. a ticket platform was named: the agent can search it
    if out["linkType"] != "event" and info.get("platformsMentioned"):
        out["searchHint"] = f'site:{info["platformsMentioned"][0]} "{info["title"]}"'
    # 6. read the original page; it wins
    orig = None
    if read_pages and out["url"] and out["linkType"] in ("event", "venue") and not out["dupOf"]:
        try:
            orig = read_original(out["url"], today, cache_dir, browser, info["title"])
        except Exception:
            orig = None
    f = dict(wf)
    changed = []
    if orig and orig.get("ok"):
        if orig.get("dates"):
            first = orig["dates"][0]
            long_run = orig.get("end") and (dt.date.fromisoformat(orig["end"]) - dt.date.fromisoformat(first)).days > 4
            if long_run:                                  # exhibition / season: a window, not single dates
                if f.get("end") and f["end"] != orig["end"]:
                    changed.append("end")
                f["kind"], f["start"], f["end"] = "ongoing", min(first, f.get("start") or first), orig["end"]
            else:
                if (f.get("dates") and set(f["dates"]) != set(orig["dates"])) or f.get("days"):
                    changed.append("dates")
                f = {k: v for k, v in f.items() if k not in ("days", "start", "end")}
                f["kind"], f["dates"] = "event", orig["dates"]
            out["detailsFrom"] = "source"
        elif orig.get("textWhen"):
            tw = when_fields(orig["textWhen"], today)
            if tw.get("dates") or tw.get("days"):
                if f.get("dates") and tw.get("dates") and set(tw["dates"]) != set(f["dates"]):
                    changed.append("dates")
                f.update(tw)
                out["detailsFrom"] = "source"
        if orig.get("time") and f.get("time") and orig["time"] != f["time"]:
            changed.append("time")
        if orig.get("time"):
            f["time"] = orig["time"]
        if orig.get("days") and (f.get("kind") == "ongoing" or not f.get("kind")):
            f["kind"], f["days"] = "ongoing", orig["days"]
            if orig.get("hours"):
                f["time"] = orig["hours"][0]
                w = dict(w); w["hours"] = orig["hours"]; w["days"] = orig["days"]
            out["detailsFrom"] = "source"
        pl = orig.get("place") or {}
        if pl.get("address") and len(pl["address"]) > 5:
            if info.get("address") and norm(pl["address"])[:12] != norm(info["address"])[:12]:
                changed.append("address")
            info["address"] = pl["address"]
        if pl.get("venue") and not info.get("venue"):
            info["venue"] = pl["venue"]
        if orig.get("price") and orig["price"]["currency"] in ("ARS", "$"):
            v_ = int(orig["price"]["value"])
            if v_ == 0:
                info["price"] = {"free": True, "ars": 0, "usd": None, "note": "Free"}
            elif v_ >= 200:
                if info["price"].get("ars") and abs(info["price"]["ars"] - v_) > 1:
                    changed.append("price")
                info["price"] = {"free": False, "ars": v_, "usd": None, "note": f"From ARS {v_:,}".replace(",", ".")}
        if orig.get("end") and f.get("kind") == "ongoing":
            f["end"] = orig["end"]
        if orig.get("image"):
            out["imgHint"] = orig["image"]
    # Maps last
    if not out["url"]:
        q = ", ".join(x for x in [info.get("venue"), info.get("address"), "Buenos Aires"] if x)
        out["url"] = info.get("mapsUrl") or (maps_url(q) if (info.get("venue") or info.get("address")) else None)
        out["linkType"] = "maps" if out["url"] else None
    out["fields"] = f
    out["changed"] = changed
    out["note"] = fields_note(f, w)
    # completeness
    if not (f.get("dates") or f.get("days") or f.get("start") or f.get("end")):
        out["missing"].append("when")
    if not info.get("address"):
        out["missing"].append("address")
    if out["linkType"] in (None, "maps"):
        out["missing"].append("original page")
    if not (info["price"]["free"] or info["price"]["ars"] or info["price"]["usd"]):
        out["missing"].append("price")
    return out

def posting_fields(info, r, today, fx=None):
    """The ArtifactData fields for a listing built from a blog item."""
    f = {}
    f.update(r["fields"])
    if r["url"]:
        f["url"] = r["url"]
    f["foundOn"] = info["foundOn"]
    f["linkType"] = r["linkType"]
    f["detailsFrom"] = r["detailsFrom"]
    f["sourceCheckedAt"] = today.isoformat()
    # the app shows "venue" as "Name, Street 123": keep that format
    if info.get("venue") or info.get("address"):
        name, addr = (info.get("venue") or "").strip(" ,"), (info.get("address") or "").strip(" ,")
        if addr and norm(addr)[:10] in norm(name):
            addr = ""
        f["venue"] = ", ".join(x for x in (name, addr) if x)
    if info.get("address"):
        f["address"] = info["address"]
    if info.get("neighborhood"):
        f["neighborhood"] = info["neighborhood"]
    if info.get("lat") and info.get("lng"):
        f["lat"], f["lng"] = info["lat"], info["lng"]
    p = info["price"]
    if p["free"]:
        f.update({"price_ars": 0, "price_usd": 0, "price_note": "Free", "over_budget": False})
    elif p["ars"]:
        f["price_ars"] = p["ars"]
        if fx:
            f["price_usd"] = round(p["ars"] / fx)
            f["over_budget"] = f["price_usd"] > 110
        f["price_note"] = p["note"]
    elif p["usd"]:
        f["price_usd"] = p["usd"]; f["price_note"] = f"USD {p['usd']}"
    if info.get("blogImg"):
        f["imgBackup"] = info["blogImg"]                  # web address only; never downloaded
    if r.get("imgHint"):
        f["imgHint"] = r["imgHint"]
    if f.get("kind") == "event" or f.get("kind") == "ongoing":
        f["verify"] = "fixed" if r.get("changed") else "ok"
        f["verifiedAt"] = today.isoformat()
        outlet = domain(info["foundOn"]).split(".")[0].title()
        f["verifyNote"] = (("From original page: " if r["detailsFrom"] == "source" else f"From {outlet}: ") + (r.get("note") or ""))[:90]
    if r.get("dupOf"):
        f["hidden"] = True
        f["hideReason"] = f"dup of {r['dupOf']}"
    return f

def read_post(url, today, cache_dir=None, browser=False):
    st, final, html = fetch(url, cache_dir, browser=browser)
    if not html:
        return None, [], st
    meta, blocks, _ = blocks_from_html(html, final or url)
    ref = meta["published"] or today
    items = [read_item(it, url, ref) for it in segment(blocks)]
    return meta, items, st

# --------------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------------
def j(o):
    def d(x):
        if isinstance(x, (dt.date,)):
            return x.isoformat()
        if isinstance(x, set):
            return sorted(x)
        if isinstance(x, tuple):
            return list(x)
        return str(x)
    return json.dumps(o, ensure_ascii=False, indent=1, default=d)

def cmd_read(a):
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    for u in a.urls:
        if os.path.exists(u):
            html = open(u, encoding="utf-8").read()
            meta, blocks, _ = blocks_from_html(html, a.base or "https://example.com/")
            ref = meta["published"] or today
            items = [read_item(it, a.base or u, ref) for it in segment(blocks)]
        else:
            meta, items, st = read_post(u, today, a.cache, a.browser)
        print(f"# {u}  ->  {len(items)} items")
        for it in items:
            wf = when_fields(it["when"], today)
            print(j({"title": it["title"], "section": it["section"], "venue": it["venue"], "address": it["address"],
                     "hood": it["neighborhood"], "when": wf, "note": when_note(it["when"]),
                     "price": it["price"], "platforms": it["platformsMentioned"], "links": it["links"][:3],
                     "img": it["blogImg"], "genre": it["genreGuess"]}))

def cmd_run(a):
    today = dt.date.fromisoformat(a.today) if a.today else (dt.datetime.utcnow() - dt.timedelta(hours=3)).date()
    events = load_events(a.events)
    idx = Index(events, load_directory(a.directory))
    os.makedirs(os.path.join(a.out, "updates"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "new"), exist_ok=True)
    fx = a.fx
    def current(e):
        if e.get("verify") in ("ended", "cancelled") or e.get("hidden"):
            return False
        if e.get("end") and e["end"] < today.isoformat():
            return False
        if e.get("kind") == "event":
            return any(d >= today.isoformat() for d in e.get("dates") or [])
        return True
    stale = (today - dt.timedelta(days=a.recheck_days)).isoformat()
    queue = [e for e in events.values() if current(e) and is_blog(e.get("url") or "") and (e.get("sourceCheckedAt") or "") < stale]
    soon = (today + dt.timedelta(days=7)).isoformat()
    def prio(e):
        ds = [d for d in e.get("dates") or [] if d >= today.isoformat()]
        return (0 if ds and ds[0] <= soon else 1 if e.get("kind") == "event" else 2, ds[0] if ds else "9")
    queue.sort(key=prio)
    by_article = defaultdict(list)
    for e in queue:
        by_article[e["url"].split("#")[0]].append(e)
    articles = sorted(by_article, key=lambda u: (min(prio(e) for e in by_article[u]), -len(by_article[u])))
    rep = Counter(); updates = []; review = []; new = []; seen_new = set()
    existing_titles = defaultdict(list)
    for e in events.values():
        for w in set(tokens(e.get("title", ""))):
            existing_titles[w].append(e)
    done_listings = 0
    t0 = time.time()
    for u in articles:
        if rep["articles"] >= a.max_articles or done_listings >= a.max_listings or time.time() - t0 > a.minutes * 60:
            break
        meta, items, st = read_post(u, today, a.cache, a.browser)
        rep["articles"] += 1
        if not items:
            rep["articles_unreadable"] += 1
        for e in by_article[u]:
            done_listings += 1
            best, bs = None, 0
            for it in items:
                s = sim(e.get("title", ""), it["title"]) + (0.2 if e.get("venue") and it.get("venue") and sim(e["venue"], it["venue"]) > 0.5 else 0)
                if s > bs:
                    best, bs = it, s
            if not best or bs < 0.5:
                # article no longer lists it, or could not be read: try the venue/the app's other listings anyway
                info = {"title": e.get("title", ""), "venue": (e.get("venue") or "").split(",")[0] or None,
                        "address": e.get("address") or find_address(e.get("venue") or ""), "neighborhood": e.get("neighborhood"),
                        "mapsUrl": None, "when": parse_when("", today), "price": {"free": False, "ars": None, "usd": None, "note": ""},
                        "platformsMentioned": [], "instagramHandles": [], "webMentioned": [], "links": [], "blogImg": None,
                        "foundOn": u, "snippet": "", "genreGuess": e.get("genre"), "kindHint": e.get("kind")}
                rep["not_in_article" if items else "article_unreadable"] += 1
            else:
                info = best
                info["kindHint"] = e.get("kind")
                info["genreGuess"] = e.get("genre") or info.get("genreGuess")
                if not info.get("venue") and e.get("venue"):
                    info["venue"] = e["venue"].split(",")[0]
                rep["matched"] += 1
            r = resolve(info, idx, today, a.cache, a.browser, read_pages=not a.no_pages)
            f = posting_fields(info, r, today, fx)
            if not r["fields"]:
                for k in ("verify", "verifiedAt", "verifyNote"):
                    f.pop(k, None)
            f = {k: v for k, v in f.items() if v not in (None, "", [])}
            if f.get("kind") == "event":                        # switching to specific dates: drop old weekly/window fields
                for k in ("start", "end"):
                    if e.get(k):
                        f[k] = {"__delete__": True}
                if e.get("days"):
                    f["days"] = []
            elif f.get("kind") == "ongoing" and e.get("dates"):
                f["dates"] = []
            if not best or bs < 0.5:
                if not items and r["linkType"] in (None, "maps"):
                    rep["skipped_article_unreadable"] += 1          # try again next run; write nothing
                    continue
                f["verifyNote"] = f"Not found in {domain(u)} article anymore"[:90] if items else f.get("verifyNote", f"{domain(u)} article unreadable")[:90]
                if not items:
                    f.pop("sourceCheckedAt", None)                   # article unreadable: re-try it later
            json.dump(f, open(os.path.join(a.out, "updates", e["id"] + ".json"), "w"), ensure_ascii=False)
            updates.append(e["id"])
            rep["link_" + str(r["linkType"])] += 1
            rep["detailsFrom_" + r["detailsFrom"]] += 1
            if r.get("dupOf"):
                rep["duplicates_of_platform_listing"] += 1
            if r["missing"]:
                review.append({"id": e["id"], "title": e.get("title"), "venue": info.get("venue"), "address": info.get("address"),
                               "foundOn": u, "missing": r["missing"], "searchHint": r.get("searchHint") or f'"{e.get("title")}" {info.get("venue") or ""} Buenos Aires',
                               "snippet": info.get("snippet", "")[:400], "proposed": f})
        # every other concrete item in the article that the app doesn't have yet -> new posting
        if a.new and items:
            for it in items:
                wf = when_fields(it["when"], today)
                if not (wf.get("dates") or wf.get("days") or wf.get("start")):
                    continue
                if wf.get("end") and wf["end"] < today.isoformat():
                    continue
                if wf.get("kind") == "event" and not wf.get("dates"):
                    continue
                key = norm(it["title"])[:40]
                if key in seen_new:
                    continue
                cand = {x["id"]: x for w in set(tokens(it["title"])) for x in existing_titles.get(w, [])[:300]}
                if any(sim(it["title"], x.get("title", "")) >= 0.72 for x in cand.values()):
                    continue
                seen_new.add(key)
                r = resolve(it, idx, today, a.cache, a.browser, read_pages=not a.no_pages)
                if r.get("dupOf"):
                    continue
                if not (it.get("venue") or it.get("address")):
                    continue
                f = posting_fields(it, r, today, fx)
                f = {k: v for k, v in f.items() if v not in (None, "", [])}
                genre = it["genreGuess"] or "events"
                nid = f"{genre}-" + hashlib.md5((genre + it["title"] + (it.get("venue") or "")).encode()).hexdigest()[:8]
                doc = {"id": nid, "genre": genre, "title": it["title"][:120], "kind": f.get("kind", "event"),
                       "dates": f.get("dates", []), "days": f.get("days", []), "time": f.get("time", ""),
                       "venue": it.get("venue") or "", "neighborhood": it.get("neighborhood") or "",
                       "price_ars": f.get("price_ars"), "price_usd": f.get("price_usd"), "price_note": f.get("price_note", ""),
                       "over_budget": f.get("over_budget", False), "url": f.get("url") or u,
                       "source": domain(u).split(".")[0], "desc": "", "addedAt": today.isoformat(), "fromBlog": True}
                doc.update({k: v for k, v in f.items() if k not in doc or doc[k] in (None, "", [])})
                json.dump(doc, open(os.path.join(a.out, "new", nid + ".json"), "w"), ensure_ascii=False)
                new.append({"id": nid, "title": doc["title"], "genre": genre, "venue": doc["venue"], "when": r.get("note") or "",
                            "url": doc["url"], "linkType": doc.get("linkType"), "missing": r["missing"],
                            "snippet": it["snippet"][:400], "foundOn": u})
                rep["new_items"] += 1
    rep["listings_done"] = done_listings
    rep["listings_left"] = max(0, len(queue) - done_listings)
    json.dump(updates, open(os.path.join(a.out, "blog_updates.json"), "w"))
    json.dump(review, open(os.path.join(a.out, "blog_review.json"), "w"), ensure_ascii=False, indent=1)
    json.dump(new, open(os.path.join(a.out, "blog_new_review.json"), "w"), ensure_ascii=False, indent=1)
    json.dump(dict(rep), open(os.path.join(a.out, "blog_report.json"), "w"), indent=1)
    print(j(dict(rep)))

def cmd_versions(a):
    """Build versions.json (doc id -> version) from this session's ArtifactData list results."""
    files = sorted(glob.glob(os.path.expanduser(a.transcripts) + "/**/*.jsonl", recursive=True), key=os.path.getmtime)
    files = files[-a.last:] if a.last else files
    ver = {}
    pat1 = re.compile(r'- "([A-Za-z0-9_\-.~:@+]+)"\s+\d+ bytes\s+version (\d+)')
    pat2 = re.compile(r'"([a-z]+-[0-9a-f]{8})" (\d+)')
    for fpath in files:
        for line in open(fpath, errors="ignore"):
            if 'from collection' not in line or '"events"' not in line and '\\"events\\"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            stack = [o]
            while stack:
                x = stack.pop()
                if isinstance(x, str):
                    if "documents from collection" in x and "events" in x:
                        for m in pat1.finditer(x):
                            ver[m.group(1)] = int(m.group(2))
                        for m in pat2.finditer(x):
                            ver[m.group(1)] = int(m.group(2))
                elif isinstance(x, dict):
                    stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)
    json.dump(ver, open(a.output, "w"))
    print(f"{len(ver)} versions -> {a.output}")

def cmd_batches(a):
    ver = json.load(open(a.versions))
    os.makedirs(os.path.join(a.out, "batches"), exist_ok=True)
    writes, missing = [], []
    for f in sorted(glob.glob(os.path.join(a.out, "updates", "*.json"))):
        i = os.path.basename(f)[:-5]
        if i not in ver:
            missing.append(i); continue
        writes.append({"op": "update", "collection": "events", "doc_id": i, "file_path": os.path.abspath(f), "if_version": ver[i]})
    for f in sorted(glob.glob(os.path.join(a.out, "new", "*.json"))):
        d = json.load(open(f))
        if d.get("drop") or not d.get("desc"):
            continue                                 # new postings need the agent's one-line description first
        i = d["id"]
        if i in ver:
            continue
        writes.append({"op": "set", "collection": "events", "doc_id": i, "file_path": os.path.abspath(f)})
    for k in range(0, len(writes), 50):
        json.dump(writes[k:k + 50], open(os.path.join(a.out, "batches", f"b{k // 50:03d}.json"), "w"))
    print(f"{len(writes)} writes in {(len(writes) + 49) // 50} batches; {len(missing)} updates skipped (no version known)")

def cmd_apply_review(a):
    """fixes.json: {id: {desc, genre, url, linkType, dates, days, time, venue, address, drop, hidden, hideReason}}"""
    fixes = json.load(open(a.fixes))
    n = 0
    for i, fx_ in fixes.items():
        for sub in ("updates", "new"):
            p = os.path.join(a.out, sub, i + ".json")
            if os.path.exists(p):
                d = json.load(open(p))
                d.update({k: v for k, v in fx_.items() if v is not None})
                if fx_.get("url") and not is_maps(fx_["url"]):
                    d["linkType"] = fx_.get("linkType") or ("instagram" if "instagram.com" in fx_["url"] else "event" if link_rank(fx_["url"]) == 0 else "venue")
                json.dump(d, open(p, "w"), ensure_ascii=False)
                n += 1
    print(f"applied {n} fixes")

def _now_when(e):
    if e.get("kind") == "event" and e.get("dates"):
        return ", ".join(e["dates"][:3]) + (f" {e['time']}" if e.get("time") else "")
    bits = []
    if e.get("days"):
        bits.append(",".join(e["days"]))
    if e.get("time"):
        bits.append(e["time"])
    if e.get("end"):
        bits.append("until " + e["end"])
    return " ".join(bits) or "—"

def _price(d):
    if d.get("price_note"):
        return d["price_note"]
    if d.get("price_ars") == 0:
        return "Free"
    if d.get("price_ars"):
        return f"ARS {d['price_ars']}"
    return "—"

def cmd_preview(a):
    """Side-by-side table: each listing now vs. what the reader would write. Writes nothing to the app."""
    events = load_events(a.events)
    rows = ["| Listing | NOW: link · when · where · price | AFTER: link · when · where · price | Details from |",
            "|---|---|---|---|"]
    n = 0
    for f in sorted(glob.glob(os.path.join(a.out, "updates", "*.json"))):
        i = os.path.basename(f)[:-5]
        e, u = events.get(i, {}), json.load(open(f))
        if n >= a.limit:
            break
        n += 1
        now = f"{domain(e.get('url'))} · {_now_when(e)} · {e.get('venue') or '—'} · {_price(e)}"
        after_link = f"{u.get('linkType') or '—'}: {domain(u.get('url') or e.get('url'))}"
        hide = " · HIDDEN (dup of ticket-site listing)" if u.get("hidden") else ""
        after = f"{after_link} · {u.get('verifyNote', '').split(': ', 1)[-1] or _now_when({**e, **u})} · {u.get('venue') or e.get('venue') or '—'} · {_price({**e, **u})}{hide}"
        rows.append(f"| {(e.get('title') or i)[:60]} | {now} | {after} | {u.get('detailsFrom', '—')} |".replace("\n", " "))
    newrows = []
    if os.path.exists(os.path.join(a.out, "blog_new_review.json")):
        for x in json.load(open(os.path.join(a.out, "blog_new_review.json")))[:a.limit]:
            newrows.append(f"| {x['title'][:60]} | {x['genre']} | {x['when']} | {x['venue']} | {x.get('linkType')}: {domain(x['url'])} |")
    out = ["## Listings: now vs. after the blog reader", "", *rows, ""]
    if newrows:
        out += ["## New postings it would add", "", "| Title | Genre | When | Where | Link |", "|---|---|---|---|---|", *newrows, ""]
    rep = os.path.join(a.out, "blog_report.json")
    if os.path.exists(rep):
        out += ["## Report", "", "```", open(rep).read(), "```"]
    txt = "\n".join(out)
    open(os.path.join(a.out, "preview.md"), "w").write(txt)
    print(txt)

def main():
    p = argparse.ArgumentParser(description="MOVEO blog reader " + VERSION)
    sp = p.add_subparsers(dest="cmd", required=True)
    r = sp.add_parser("read"); r.add_argument("urls", nargs="+"); r.add_argument("--today"); r.add_argument("--cache")
    r.add_argument("--browser", action="store_true"); r.add_argument("--base")
    r.set_defaults(fn=cmd_read)
    r = sp.add_parser("run")
    r.add_argument("--events", required=True); r.add_argument("--directory"); r.add_argument("--out", default="out")
    r.add_argument("--today"); r.add_argument("--cache", default="cache"); r.add_argument("--fx", type=float)
    r.add_argument("--max-articles", type=int, default=80); r.add_argument("--max-listings", type=int, default=400)
    r.add_argument("--minutes", type=float, default=25); r.add_argument("--recheck-days", type=int, default=14)
    r.add_argument("--browser", action="store_true"); r.add_argument("--no-pages", action="store_true")
    r.add_argument("--new", action="store_true", help="also propose new postings for other items in the same posts")
    r.set_defaults(fn=cmd_run)
    r = sp.add_parser("versions"); r.add_argument("--transcripts", default="~/.claude/projects")
    r.add_argument("--last", type=int, default=3); r.add_argument("--output", default="versions.json")
    r.set_defaults(fn=cmd_versions)
    r = sp.add_parser("batches"); r.add_argument("--versions", required=True); r.add_argument("--out", default="out")
    r.set_defaults(fn=cmd_batches)
    r = sp.add_parser("preview"); r.add_argument("--events", required=True); r.add_argument("--out", default="out")
    r.add_argument("--limit", type=int, default=60)
    r.set_defaults(fn=cmd_preview)
    r = sp.add_parser("apply-review"); r.add_argument("fixes"); r.add_argument("--out", default="out")
    r.set_defaults(fn=cmd_apply_review)
    a = p.parse_args()
    a.fn(a)

if __name__ == "__main__":
    main()
