#!/usr/bin/env python3
"""On The Move harvester.

Pulls DATED events happening today..today+WINDOW in Buenos Aires from ticketing platforms,
agendas and venues, and turns them into app listings.

  python3 harvester.py harvest [--only a,b] [--window 7]   -> out/candidates.json, out/report.json
  python3 harvester.py build --existing db/events            -> out/new/*.json, out/extend/*.json, out/review.json
  python3 harvester.py apply-review out/fixes.json          -> rewrites out/new/*.json after a human/Claude review

Every source is a plain dict in SOURCES. Extraction is generic on purpose:
  * JSON-LD Event objects (any page),
  * event-like objects inside embedded JSON (__NEXT_DATA__, window.__X__ = {...}, application/json),
  * for kind "browser": every JSON response the page loads (XHR/fetch) while it renders,
  * plus hand-written adapters for RA (GraphQL) and Luma (public API).
If a source returns nothing, fix its entry (urls / follow regex / kind) rather than the engine.
"""
import argparse, concurrent.futures as cf, datetime as dt, glob, hashlib, html as htmlmod, json, os, re, sys, threading, time, unicodedata
from urllib.parse import urljoin, urlparse, quote

from collections import defaultdict

import requests
from bs4 import BeautifulSoup

BA = dt.timezone(dt.timedelta(hours=-3))
NOW = dt.datetime.now(BA)
TODAY = NOW.date()
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/127.0 Safari/537.36")
OUT = "out"
# "Don't Forget": sources are read this far ahead; nightlife and events beyond the daily window are kept as normal listings
FUTURE_DAYS = 200
FUTURE_GENRES = {"nightlife", "events"}

# ------------------------------------------------------------------ sources
EB_CATS = ["music", "nightlife", "arts", "food-and-drink", "business", "hobbies", "community", "film-and-media", "sports-and-fitness", "science-and-tech"]
SOURCES = [
    # --- organizers' own agenda pages (from the AMBA organizer directory)
    {"name": "organizers", "kind": "organizers", "genre": None, "limit": 450},
    # --- venues' public Instagram accounts through Meta's official API (needs the owner's token, see run_instagram)
    {"name": "instagram", "kind": "instagram", "genre": None, "limit": 180},
    # --- platforms with APIs
    {"name": "ra", "kind": "ra", "genre": "nightlife", "note": "RA GraphQL eventListings for area buenosaires/ar"},
    {"name": "luma", "kind": "luma", "genre": "events", "slug": "buenos-aires"},
    # --- server-rendered listings (JSON-LD or embedded JSON)
    {"name": "eventbrite", "kind": "pages", "genre": None, "browser": False, "stop_empty": True, "pages": [1, 30],
     "urls": ["https://www.eventbrite.com.ar/d/argentina--buenos-aires/all-events/?page={p}"]},
    # --- the platforms Qué Hacemos itself lists from (2026-10-19): their own public data feeds, city set by CITY
    {"name": "passline", "kind": "passline_api", "genre": None},
    {"name": "alternativa", "kind": "alternativa_json", "genre": "theater"},
    {"name": "alpogo", "kind": "alpogo_api", "genre": None},
    {"name": "plateanet", "kind": "plateanet_api", "genre": "theater"},
    {"name": "venti", "kind": "venti_api", "genre": None},
    # --- Qué Hacemos' full catalog, each event listed under and linked to its original platform
    {"name": "quehacemos", "kind": "quehacemos_api", "genre": None},
    {"name": "allevents", "kind": "allevents", "genre": None,
     "urls": [f"https://allevents.in/buenos-aires/{c}" for c in
              ["all", "today", "tomorrow", "this-weekend", "music", "parties", "art", "food-drinks", "business", "workshops", "sports", "theatre", "comedy"]]},
    {"name": "songkick", "kind": "pages", "genre": "music", "stop_empty": True, "pages": [1, 8],
     "urls": ["https://www.songkick.com/metro-areas/32911-argentina-buenos-aires?page={p}"]},
    {"name": "indiehoy", "kind": "pages", "genre": "music", "urls": ["https://indiehoy.com/eventos/"],
     "follow": r"https://indiehoy\.com/eventos/[a-z0-9\-]+/?$", "max_follow": 80},
    {"name": "cartelera_teatro", "kind": "pages", "genre": "theater", "urls": ["https://cartelera.ar/teatro"],
     "follow": r"https://cartelera\.ar/teatro/[a-z0-9\-/]+", "max_follow": 120},
    {"name": "complejoteatral", "kind": "pages", "genre": "theater", "urls": ["https://complejoteatral.gob.ar/"],
     "follow": r"https://complejoteatral\.gob\.ar/ver/[a-z0-9\-]+", "max_follow": 60},
    # --- JavaScript apps: load in a headless browser and read the JSON they fetch
    {"name": "bombo", "kind": "pages", "genre": "nightlife", "urls": ["https://wearebombo.com/eventos/ar"],
     "follow": r"https://wearebombo\.com/evento/[a-z0-9\-]+$", "max_follow": 140},
    {"name": "livepass", "kind": "pages", "genre": None, "urls": ["https://livepass.com.ar/"],
     "follow": r"https://(www\.)?livepass\.com\.ar/events/[a-z0-9\-]+$", "max_follow": 200},
    {"name": "allaccess", "kind": "pages", "genre": None, "urls": ["https://www.allaccess.com.ar/"],
     "follow": r"https://www\.allaccess\.com\.ar/event/[a-z0-9\-]+$", "max_follow": 60},
    {"name": "fever", "kind": "pages", "genre": None, "urls": ["https://feverup.com/es/buenos-aires"],
     "follow": r"https://feverup\.com/m/\d+", "strip_query": True, "max_follow": 140},
    {"name": "palaciolibertad", "kind": "pages", "genre": None, "urls": ["https://palaciolibertad.gob.ar/agenda/"],
     "follow": r"https://palaciolibertad\.gob\.ar/events/[a-z0-9\-]+/?$", "max_follow": 90},
    {"name": "ticketek", "kind": "ticketek_api", "genre": None},
    {"name": "entradauno", "kind": "entradauno_json", "genre": None},
    {"name": "linda", "kind": "linda_api", "genre": None, "note": "Buenos Aires city government agenda (replaces gcba_agenda)"},
    {"name": "teatrocolon", "kind": "teatrocolon", "genre": "music"},
    {"name": "hoymilonga", "kind": "browser", "genre": "tango",
     "urls": ["https://www.hoy-milonga.com/buenos-aires/es/milongas"]},
    {"name": "meetup", "kind": "browser", "genre": "events",
     "urls": ["https://www.meetup.com/find/?location=ar--Buenos%20Aires&source=EVENTS&eventType=inPerson"]},
    {"name": "bandsintown", "kind": "pages", "genre": "music", "stop_empty": True, "pages": [1, 6],
     "urls": ["https://www.bandsintown.com/c/buenos-aires-argentina?page={p}"]},
]

# ------------------------------------------------------------------ platform registry (sources/registry in the app database)
# Added 2026-10-06 from the ranked top-30-per-category list. All of them run every day.
# A registry source that yields events can be promoted: drop its "tier" so it runs every day.
REGISTRY = json.loads(r'''[
 {
  "name": "r_linda_buenosaires",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://linda.buenosaires.gob.ar/agenda"
  ],
  "tier": "registry",
  "note": "registry: BA Ciudad agenda (LINDA) (government, js-only) for museums, outdoor, events, galleries, theater, film",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_cinemark",
  "kind": "browser",
  "genre": "film",
  "urls": [
   "https://www.cinemark.com.ar/cartelera"
  ],
  "tier": "registry",
  "note": "registry: Cinemark Argentina (ticketing, js-only) for film",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_centroculturalrecoleta",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.centroculturalrecoleta.org/"
  ],
  "tier": "registry",
  "note": "registry: Centro Cultural Recoleta (venue-calendar, listing-readable) for galleries, museums, classes, film"
 },
 {
  "name": "r_gcba_ferias_y_mercados",
  "kind": "pages",
  "genre": "markets",
  "urls": [
   "https://www.buenosaires.gob.ar/ferias-y-mercados"
  ],
  "tier": "registry",
  "note": "registry: Buenos Aires Ciudad Ferias y Mercados (government, listing-readable) for markets"
 },
 {
  "name": "r_feverup",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://feverup.com/es/buenos-aires"
  ],
  "tier": "registry",
  "note": "registry: Fever (event-platform, listing-readable) for food, classes, nightlife, tango, newplaces, markets, music, outdoor, events, theater, museums",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_xceed_me",
  "kind": "browser",
  "genre": "nightlife",
  "urls": [
   "https://xceed.me/en/buenos-aires/events"
  ],
  "tier": "registry",
  "note": "registry: Xceed (ticketing, js-only) for nightlife",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_hoyts",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.hoyts.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Cinemas Hoyts (ticketing, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_malba",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.malba.org.ar/"
  ],
  "tier": "registry",
  "note": "registry: MALBA (venue-calendar, listing-readable) for museums, galleries, film"
 },
 {
  "name": "r_forbesargentina",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.forbesargentina.com/lifestyle/restaurantes-aperturas-primer-semestre-empiezan-marcar-rumbo-gastronomico-ano-n94566",
   "https://www.forbesargentina.com/negocios/agenda-startup-2026-cuales-seran-eventos-convocatorias-imperdibles-emprendedores-n84149"
  ],
  "tier": "registry",
  "note": "registry: Forbes Argentina Lifestyle (media-guide, listing-readable) for newplaces, food, events, markets",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_cinepolis",
  "kind": "browser",
  "genre": "film",
  "urls": [
   "https://www.cinepolis.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Cinepolis Argentina (ticketing, blocked) for film",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_mnba",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.mnba.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: Museo Nacional de Bellas Artes (venue-calendar, listing-readable) for museums, galleries",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_infogastronomica",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.infogastronomica.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Info Gastronomica (media-guide, listing-readable) for newplaces, food, markets",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_timeout",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.timeout.com/buenos-aires",
   "https://www.timeout.com/buenos-aires/events-calendar"
  ],
  "tier": "registry",
  "note": "registry: Time Out Buenos Aires (media-guide, listing-readable) for food, newplaces, nightlife, outdoor, markets, music, events, tango, museums, theater, galleries, classes, film"
 },
 {
  "name": "r_todotango",
  "kind": "pages",
  "genre": "tango",
  "urls": [
   "https://www.todotango.com/"
  ],
  "tier": "registry",
  "note": "registry: TodoTango (media-guide, listing-readable) for tango"
 },
 {
  "name": "r_cartelera",
  "kind": "browser",
  "genre": "theater",
  "urls": [
   "https://www.cartelera.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Cartelera Teatro (media-guide, js-only) for theater"
 },
 {
  "name": "r_ecartelera",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.ecartelera.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: eCartelera Argentina (media-guide, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_museos",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.museos.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: Museos Argentina (Secretaria de Cultura) (government, listing-readable) for museums, galleries",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_mamba",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.mamba.org.ar/"
  ],
  "tier": "registry",
  "note": "registry: MAMBA (venue-calendar, listing-readable) for galleries, museums",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_bairessecreta",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://bairessecreta.com/",
   "https://bairessecreta.com"
  ],
  "tier": "registry",
  "note": "registry: Buenos Aires Secreta (media-guide, listing-readable) for newplaces, outdoor, markets, food, events, classes"
 },
 {
  "name": "r_tangoportodos",
  "kind": "browser",
  "genre": "tango",
  "urls": [
   "https://www.tangoportodos.com/"
  ],
  "tier": "registry",
  "note": "registry: Tango Por Todos (aggregator, blocked) for tango"
 },
 {
  "name": "r_cinemacenter",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.cinemacenter.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Cinemacenter (ticketing, listing-readable) for film",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_proa",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.proa.org/"
  ],
  "tier": "registry",
  "note": "registry: Fundacion PROA (venue-calendar, listing-readable) for galleries, museums"
 },
 {
  "name": "r_guiaoleo",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://guiaoleo.com/"
  ],
  "tier": "registry",
  "note": "registry: Guia Oleo (aggregator, listing-readable) for newplaces, food",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_neventum",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.neventum.com/tradeshows/argentina"
  ],
  "tier": "registry",
  "note": "registry: Neventum (aggregator, listing-readable) for events"
 },
 {
  "name": "r_gcba_agendacultural",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.buenosaires.gob.ar/agendacultural"
  ],
  "tier": "registry",
  "note": "registry: Agenda Cultural GCBA (government, blocked) for tango, music, nightlife",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_incaa",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.incaa.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: INCAA espacios (Gaumont) (government, listing-readable) for film"
 },
 {
  "name": "r_coleccionfortabat",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.coleccionfortabat.org.ar/"
  ],
  "tier": "registry",
  "note": "registry: Coleccion Fortabat (venue-calendar, listing-readable) for galleries, museums"
 },
 {
  "name": "r_tripadvisor",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.tripadvisor.com.ar/Restaurants-g312741-Buenos_Aires_Capital_Federal_District.html",
   "https://www.tripadvisor.com.ar/Attractions-g312741-Activities-Buenos_Aires_Capital_Federal_District.html"
  ],
  "tier": "registry",
  "note": "registry: Tripadvisor BA attractions (aggregator, blocked) for food, newplaces, museums",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_superprof",
  "kind": "pages",
  "genre": "classes",
  "urls": [
   "https://www.superprof.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Superprof (aggregator, listing-readable) for classes",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_ntradeshows",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.ntradeshows.com/argentina/"
  ],
  "tier": "registry",
  "note": "registry: NTradeshows (aggregator, listing-readable) for events"
 },
 {
  "name": "r_turismo_buenosaires",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://turismo.buenosaires.gob.ar/es/node/73344",
   "https://turismo.buenosaires.gob.ar/es/agenda"
  ],
  "tier": "registry",
  "note": "registry: Turismo Buenos Aires - Semana de las Milongas (government, blocked) for tango, museums, galleries",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_cinematecanacional",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.cinematecanacional.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: Cinemateca Nacional (INCAA) (government, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_fundacionosde",
  "kind": "pages",
  "genre": "galleries",
  "urls": [
   "https://www.fundacionosde.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Fundacion OSDE Espacio de Arte (venue-calendar, listing-readable) for galleries"
 },
 {
  "name": "r_meteored",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.meteored.com.ar/noticias/actualidad/mejores-ferias-y-mercados-en-buenos-aires-turismo.html"
  ],
  "tier": "registry",
  "note": "registry: Meteored Ferias (media-guide, listing-readable) for markets, newplaces",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_eventseye",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.eventseye.com/fairs/c1_trade-shows_argentina.html"
  ],
  "tier": "registry",
  "note": "registry: EventsEye (aggregator, listing-readable) for events"
 },
 {
  "name": "r_tuentrada",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.tuentrada.com/",
   "https://www.tuentrada.com"
  ],
  "tier": "registry",
  "note": "registry: Tuentrada (ticketing, listing-readable) for theater, music, nightlife, outdoor, tango, events",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_bafici",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.bafici.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: BAFICI (government, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_arteba",
  "kind": "pages",
  "genre": "galleries",
  "urls": [
   "https://www.arteba.org/"
  ],
  "tier": "registry",
  "note": "registry: arteba (organizer, listing-readable) for galleries"
 },
 {
  "name": "r_canal26",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.canal26.com/turismo/",
   "https://www.canal26.com/general/2025/10/28/calendario-runner-2025-todas-las-carreras-que-quedan-antes-de-que-termine-el-ano/"
  ],
  "tier": "registry",
  "note": "registry: Canal 26 Turismo (media-guide, listing-readable) for newplaces, outdoor, markets, food",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_c5n",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.c5n.com/lifestyle/cuales-son-las-mejores-ferias-y-mercados-comprar-artesanias-buenos-aires-n231106",
   "https://www.c5n.com/deportes/running-calendario-carreras-2025-argentina-n191978"
  ],
  "tier": "registry",
  "note": "registry: C5N Lifestyle (media-guide, listing-readable) for markets, outdoor",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_startupgrind",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.startupgrind.com/buenos-aires/"
  ],
  "tier": "registry",
  "note": "registry: Startup Grind Buenos Aires (community, listing-readable) for events"
 },
 {
  "name": "r_dice_fm",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://dice.fm/browse/Buenos_Aires"
  ],
  "tier": "registry",
  "note": "registry: Dice (ticketing, blocked) for nightlife, music, tango",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_ramona",
  "kind": "pages",
  "genre": "galleries",
  "urls": [
   "https://www.ramona.org.ar/"
  ],
  "tier": "registry",
  "note": "registry: Ramona (media-guide, listing-readable) for galleries",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_infonegocios_info",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://infonegocios.info/"
  ],
  "tier": "registry",
  "note": "registry: Infonegocios (media-guide, listing-readable) for newplaces, food, markets",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_gcba_deportes",
  "kind": "pages",
  "genre": "outdoor",
  "urls": [
   "https://www.buenosaires.gob.ar/deportes"
  ],
  "tier": "registry",
  "note": "registry: Buenos Aires Ciudad - Deportes (government, listing-readable) for outdoor"
 },
 {
  "name": "r_getonbrd",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.getonbrd.com/events/2026"
  ],
  "tier": "registry",
  "note": "registry: GetOnBoard Events (aggregator, listing-readable) for events"
 },
 {
  "name": "r_allaccess",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.allaccess.com.ar/",
   "https://www.allaccess.com.ar"
  ],
  "tier": "registry",
  "note": "registry: Allaccess (ticketing, blocked) for music, nightlife, outdoor",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_teatrocervantes",
  "kind": "pages",
  "genre": "theater",
  "urls": [
   "https://www.teatrocervantes.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: Teatro Nacional Cervantes (venue-calendar, listing-readable) for theater"
 },
 {
  "name": "r_atmosfera_cine",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.atmosfera.cine.ar/"
  ],
  "tier": "registry",
  "note": "registry: Atmosfera Cine (venue-calendar, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_artealdia",
  "kind": "pages",
  "genre": "galleries",
  "urls": [
   "https://www.artealdia.com/"
  ],
  "tier": "registry",
  "note": "registry: Arte al Dia (media-guide, listing-readable) for galleries"
 },
 {
  "name": "r_lausina",
  "kind": "pages",
  "genre": "classes",
  "urls": [
   "https://www.lausina.org/"
  ],
  "tier": "registry",
  "note": "registry: Usina del Arte (venue-calendar, listing-readable) for classes"
 },
 {
  "name": "r_releasebeforeready",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.releasebeforeready.com"
  ],
  "tier": "registry",
  "note": "registry: Release Before Ready (organizer, listing-readable) for events",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_gcba_usinadelarte",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.buenosaires.gob.ar/usinadelarte"
  ],
  "tier": "registry",
  "note": "registry: Usina del Arte (venue-calendar, blocked) for tango, music",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_teatrocoliseo",
  "kind": "pages",
  "genre": "theater",
  "urls": [
   "https://www.teatrocoliseo.org.ar/"
  ],
  "tier": "registry",
  "note": "registry: Teatro Coliseo (venue-calendar, listing-readable) for theater"
 },
 {
  "name": "r_filmaffinity",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://www.filmaffinity.com/ar/"
  ],
  "tier": "registry",
  "note": "registry: FilmAffinity Argentina (media-guide, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_museoevita",
  "kind": "pages",
  "genre": "museums",
  "urls": [
   "https://www.museoevita.org/"
  ],
  "tier": "registry",
  "note": "registry: Museo Evita (venue-calendar, listing-readable) for museums",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_artsy",
  "kind": "browser",
  "genre": "galleries",
  "urls": [
   "https://www.artsy.net/"
  ],
  "tier": "registry",
  "note": "registry: Artsy (aggregator, js-only) for galleries"
 },
 {
  "name": "r_entradauno",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.entradauno.com/"
  ],
  "tier": "registry",
  "note": "registry: Entradauno (ticketing, js-only) for music, theater, nightlife, tango",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_timbre4",
  "kind": "pages",
  "genre": "theater",
  "urls": [
   "https://www.timbre4.com/"
  ],
  "tier": "registry",
  "note": "registry: Teatro Timbre 4 (venue-calendar, listing-readable) for theater"
 },
 {
  "name": "r_lanacion",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.lanacion.com.ar/espectaculos/",
   "https://www.lanacion.com.ar/sociedad/running-este-es-el-calendario-de-carreras-de-la-argentina-en-mayo-2026-nid29042026/",
   "https://www.lanacion.com.ar/gastronomia/"
  ],
  "tier": "registry",
  "note": "registry: La Nacion espectaculos/cultura (media-guide, listing-readable) for film, outdoor, newplaces, theater, museums, galleries, markets, food",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_mali",
  "kind": "pages",
  "genre": "museums",
  "urls": [
   "https://www.mali.org.ar/"
  ],
  "tier": "registry",
  "note": "registry: MALI (venue-calendar, listing-readable) for museums",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_elcultural",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.elcultural.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: El Cultural San Martin (venue-calendar, listing-readable) for galleries, museums",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_clarin",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.clarin.com/espectaculos/",
   "https://www.clarin.com/ollas-y-sartenes/"
  ],
  "tier": "registry",
  "note": "registry: Clarin espectaculos/cultura (media-guide, listing-readable) for film, newplaces, theater, museums, galleries, markets, food",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_ticketmaster",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.ticketmaster.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Ticketmaster Argentina (ticketing, blocked) for music, nightlife",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_usinadelarte",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.usinadelarte.org/"
  ],
  "tier": "registry",
  "note": "registry: La Usina del Arte (venue-calendar, js-only) for galleries, theater, film, museums",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_argentina",
  "kind": "pages",
  "genre": "museums",
  "urls": [
   "https://www.argentina.gob.ar/cultura"
  ],
  "tier": "registry",
  "note": "registry: Cultura Argentina agenda (government, listing-readable) for museums"
 },
 {
  "name": "r_civitatis",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.civitatis.com/es/buenos-aires/"
  ],
  "tier": "registry",
  "note": "registry: Civitatis (aggregator, listing-readable) for food, classes, markets, museums, newplaces"
 },
 {
  "name": "r_infobae",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.infobae.com/"
  ],
  "tier": "registry",
  "note": "registry: Infobae (media-guide, listing-readable) for newplaces, markets",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_uba",
  "kind": "pages",
  "genre": "classes",
  "urls": [
   "https://www.uba.ar/"
  ],
  "tier": "registry",
  "note": "registry: UBA (organizer, listing-readable) for classes",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_marathons",
  "kind": "pages",
  "genre": "outdoor",
  "urls": [
   "https://www.marathons.com/es/events/maraton-de-buenos-aires_NCHg8"
  ],
  "tier": "registry",
  "note": "registry: Maratón de Buenos Aires (aggregator, listing-readable) for outdoor"
 },
 {
  "name": "r_cultura",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.cultura.gob.ar/"
  ],
  "tier": "registry",
  "note": "registry: Palais de Glace / Museos Nacionales (government, listing-readable) for museums, galleries, classes",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_viator",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.viator.com/es-AR/Buenos-Aires/d901",
   "https://www.viator.com/Buenos-Aires/d901",
   "https://www.viator.com"
  ],
  "tier": "registry",
  "note": "registry: Viator (aggregator, js-only) for food, classes, museums, outdoor",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_perfil",
  "kind": "pages",
  "genre": "newplaces",
  "urls": [
   "https://www.perfil.com/"
  ],
  "tier": "registry",
  "note": "registry: Perfil (media-guide, listing-readable) for newplaces",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_domestika",
  "kind": "pages",
  "genre": "classes",
  "urls": [
   "https://www.domestika.org/es"
  ],
  "tier": "registry",
  "note": "registry: Domestika (aggregator, listing-readable) for classes",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_culturalkirchner",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://culturalkirchner.gob.ar"
  ],
  "tier": "registry",
  "note": "registry: CCK (venue-calendar, listing-readable) for events",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_getyourguide",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.getyourguide.com/es-es/ciudad-de-buenos-aires-l1/",
   "https://www.getyourguide.com/buenos-aires-l484/"
  ],
  "tier": "registry",
  "note": "registry: GetYourGuide (aggregator, js-only) for food, classes, museums",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_airbnb",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.airbnb.com.ar/s/Buenos-Aires/experiences"
  ],
  "tier": "registry",
  "note": "registry: Airbnb Experiences (aggregator, blocked) for classes, food",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_cronista",
  "kind": "pages",
  "genre": "newplaces",
  "urls": [
   "https://www.cronista.com/"
  ],
  "tier": "registry",
  "note": "registry: El Cronista (media-guide, listing-readable) for newplaces",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_teatroelpicadero",
  "kind": "browser",
  "genre": "theater",
  "urls": [
   "https://www.teatroelpicadero.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Teatro Picadero (venue-calendar, js-only) for theater"
 },
 {
  "name": "r_thefork_es",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.thefork.es/"
  ],
  "tier": "registry",
  "note": "registry: TheFork (aggregator, js-only) for newplaces, food",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_buenosairesherald",
  "kind": "pages",
  "genre": "tango",
  "urls": [
   "https://buenosairesherald.com/what-to-do-in-argentina/what-to-do-in-buenos-aires/herald-favorites-these-tango-clubs-offer-the-best-milongas-in-buenos-aires"
  ],
  "tier": "registry",
  "note": "registry: Buenos Aires Herald - best milongas (media-guide, listing-readable) for tango",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_deportick",
  "kind": "browser",
  "genre": "outdoor",
  "urls": [
   "https://www.deportick.com"
  ],
  "tier": "registry",
  "note": "registry: Deportick (ticketing, blocked) for outdoor"
 },
 {
  "name": "r_mercado",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://mercado.com.ar/agenda/la-rural-sera-sede-de-argentina-alimenta-2026-y-preve-mas-de-300-expositores"
  ],
  "tier": "registry",
  "note": "registry: Mercado agenda (media-guide, listing-readable) for events",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_instagram",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.instagram.com/"
  ],
  "tier": "registry",
  "note": "registry: Instagram (community, blocked) for newplaces, markets, food, classes",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_expoagro",
  "kind": "pages",
  "genre": "events",
  "urls": [
   "https://www.expoagro.com.ar"
  ],
  "tier": "registry",
  "note": "registry: Expoagro (organizer, listing-readable) for events"
 },
 {
  "name": "r_movistararena",
  "kind": "browser",
  "genre": "music",
  "urls": [
   "https://www.movistararena.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Movistar Arena (venue-calendar, blocked) for music",
  "retired": "replaced by a direct feed"
 },
 {
  "name": "r_pagina12",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.pagina12.com.ar/secciones/cultura-y-espectaculos"
  ],
  "tier": "registry",
  "note": "registry: Pagina/12 cultura y espectaculos (media-guide, listing-readable) for theater, galleries, film",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_entradaweb",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.entradaweb.com.ar",
   "https://www.entradaweb.com/"
  ],
  "tier": "registry",
  "note": "registry: Entradaweb (ticketing, js-only) for outdoor, theater"
 },
 {
  "name": "r_winedering",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://www.winedering.com/es/wine-tasting-in-buenos-aires-expert-led-experience-with-regional-pairings_d5632_travel"
  ],
  "tier": "registry",
  "note": "registry: Winedering (aggregator, listing-readable) for newplaces, food"
 },
 {
  "name": "r_larural",
  "kind": "browser",
  "genre": "events",
  "urls": [
   "https://www.larural.org.ar"
  ],
  "tier": "registry",
  "note": "registry: La Rural Predio Ferial (venue-calendar, blocked) for events"
 },
 {
  "name": "r_lunapark",
  "kind": "browser",
  "genre": "music",
  "urls": [
   "https://www.lunapark.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Luna Park (venue-calendar, blocked) for music"
 },
 {
  "name": "r_autoentrada",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.autoentrada.com",
   "https://www.autoentrada.com/"
  ],
  "tier": "registry",
  "note": "registry: Autoentrada (ticketing, js-only) for outdoor, theater"
 },
 {
  "name": "r_therealargentina",
  "kind": "pages",
  "genre": null,
  "urls": [
   "https://therealargentina.com/en/"
  ],
  "tier": "registry",
  "note": "registry: The Real Argentina (media-guide, listing-readable) for newplaces, food",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_baferial",
  "kind": "browser",
  "genre": "events",
  "urls": [
   "https://www.baferial.com.ar"
  ],
  "tier": "registry",
  "note": "registry: BA Ferial (venue-calendar, blocked) for events"
 },
 {
  "name": "r_teatrovorterix",
  "kind": "browser",
  "genre": null,
  "urls": [
   "https://www.teatrovorterix.com/"
  ],
  "tier": "registry",
  "note": "registry: Teatro Vorterix (venue-calendar, blocked) for music, nightlife"
 },
 {
  "name": "r_costasalguero",
  "kind": "browser",
  "genre": "events",
  "urls": [
   "https://www.costasalguero.com"
  ],
  "tier": "registry",
  "note": "registry: Costa Salguero / Centro Costa Salguero (venue-calendar, blocked) for events"
 },
 {
  "name": "r_niceto",
  "kind": "browser",
  "genre": "nightlife",
  "urls": [
   "https://www.niceto.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Niceto Club (venue-calendar, blocked) for nightlife"
 },
 {
  "name": "r_wikiloc",
  "kind": "browser",
  "genre": "outdoor",
  "urls": [
   "https://www.wikiloc.com/hiking-trails/buenos-aires"
  ],
  "tier": "registry",
  "note": "registry: Wikiloc (community, blocked) for outdoor",
  "retired": "no dated events (tours, reviews, showtimes)"
 },
 {
  "name": "r_rollingstone",
  "kind": "browser",
  "genre": "music",
  "urls": [
   "https://www.rollingstone.com.ar/"
  ],
  "tier": "registry",
  "note": "registry: Rolling Stone Argentina (media-guide, blocked) for music",
  "retired": "news or guide article, not an event listing"
 },
 {
  "name": "r_letterboxd",
  "kind": "pages",
  "genre": "film",
  "urls": [
   "https://letterboxd.com/"
  ],
  "tier": "registry",
  "note": "registry: Letterboxd (community, listing-readable) for film",
  "retired": "site down or refusing every request"
 },
 {
  "name": "r_gba",
  "kind": "pages",
  "genre": "classes",
  "urls": [
   "https://www.gba.gob.ar/cultura"
  ],
  "tier": "registry",
  "note": "registry: Provincia de Buenos Aires Cultura (government, listing-readable) for classes"
 },
 {
  "name": "r_tigre",
  "kind": "browser",
  "genre": "outdoor",
  "urls": [
   "https://www.tigre.gob.ar"
  ],
  "tier": "registry",
  "note": "registry: Municipio de Tigre agenda (government, blocked) for outdoor"
 }
]''')
SOURCES += REGISTRY

# ------------------------------------------------------------------ http
_last = {}
_lock = threading.Lock()
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9,en;q=0.8"})


def polite(url, per_sec=2.0):
    host = urlparse(url).netloc
    while True:
        with _lock:
            t = time.time()
            if t - _last.get(host, 0) >= 1 / per_sec:
                _last[host] = t
                return
        time.sleep(0.1)


def get(url, **kw):
    polite(url)
    kw.setdefault("timeout", 20)
    return SESSION.get(url, **kw)


# ------------------------------------------------------------------ dates
MONTHS = {m: i + 1 for i, m in enumerate(["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"])}
MONTHS["setiembre"] = 9


def parse_dt(v):
    """Return an aware datetime in Buenos Aires time (or a date at 00:00 with has_time False)."""
    if v is None or v == "":
        return None, False
    if isinstance(v, (int, float)):
        x = v / 1000 if v > 1e11 else v
        if not 1.5e9 < x < 2.2e9:
            return None, False
        return dt.datetime.fromtimestamp(x, BA), True
    if not isinstance(v, str):
        return None, False
    s = v.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})?)?", s)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        if not m[4]:
            try:
                return dt.datetime(y, mo, d, tzinfo=BA), False
            except ValueError:
                return None, False
        hh, mi, ss = int(m[4]), int(m[5]), int(m[6] or 0)
        tz = BA
        if m[7]:
            if m[7] == "Z":
                tz = dt.timezone.utc
            else:
                z = m[7].replace(":", "")
                tz = dt.timezone((1 if z[0] == "+" else -1) * dt.timedelta(hours=int(z[1:3]), minutes=int(z[3:5])))
        try:
            return dt.datetime(y, mo, d, hh, mi, ss, tzinfo=tz).astimezone(BA), True
        except ValueError:
            return None, False
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s)
    if m:
        try:
            if m[4]:
                return dt.datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]), tzinfo=BA), True
            return dt.datetime(int(m[3]), int(m[2]), int(m[1]), tzinfo=BA), False
        except ValueError:
            return None, False
    return None, False


def in_window(d, window):
    return TODAY <= d <= TODAY + dt.timedelta(days=window)


# ------------------------------------------------------------------ generic extraction
TITLE_KEYS = ["name", "title", "nombre", "titulo", "eventName", "event_name", "eventTitle"]
START_KEYS = ["startDate", "start_date", "start_at", "startAt", "startsAt", "starts_at", "start_time", "startTime", "dateTime", "datetime",
              "date", "fecha", "fecha_inicio", "fechaInicio", "eventDate", "event_date", "begin", "start", "local_date", "fechaHora", "firstDate"]
MULTI_KEYS = ["dates", "sessions", "functions", "funciones", "performances", "showtimes", "shows", "schedule", "fechas", "eventSchedule", "occurrences"]
VENUE_KEYS = ["venue", "location", "place", "lugar", "sala", "venueName", "venue_name", "espacio", "recinto", "teatro", "club"]
URL_KEYS = ["url", "eventUrl", "event_url", "link", "permalink", "contentUrl", "href", "shareUrl", "slug", "path"]
PRICE_KEYS = ["price", "precio", "minPrice", "min_price", "priceFrom", "price_from", "lowPrice", "fromPrice", "valor", "offers", "ticket_info"]
IMG_KEYS = ["image", "imageUrl", "image_url", "cover", "cover_url", "coverUrl", "flyer", "flyerFront", "poster", "thumbnail", "banner", "img"]
DESC_KEYS = ["description", "descripcion", "summary", "subtitle", "about", "blurb", "short_description"]
EVENT_TYPES = {"Event", "MusicEvent", "TheaterEvent", "DanceEvent", "ComedyEvent", "ExhibitionEvent", "SocialEvent", "Festival", "ScreeningEvent",
               "EducationEvent", "BusinessEvent", "SportsEvent", "FoodEvent", "LiteraryEvent", "VisualArtsEvent", "ChildrensEvent", "SaleEvent",
               "CourseInstance", "PublicationEvent", "DeliveryEvent", "Hackathon"}


def txt(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return re.sub(r"\s+", " ", htmlmod.unescape(BeautifulSoup(v, "lxml").get_text(" ") if "<" in v else v)).strip()
    if isinstance(v, dict):
        for k in ("name", "title", "nombre", "text", "es", "value"):
            if isinstance(v.get(k), str):
                return txt(v[k])
    if isinstance(v, list) and v:
        return txt(v[0])
    return ""


def first(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def resolve(v, refs):
    if isinstance(v, dict) and "__ref" in v and refs:
        return refs.get(v["__ref"], v)
    return v


def venue_of(d, refs=None):
    v = resolve(first(d, VENUE_KEYS), refs)
    name, addr, lat, lng = "", "", None, None
    if isinstance(v, list) and v:
        v = resolve(v[0], refs)
    if isinstance(v, dict):
        name = txt(first(v, ["name", "nombre", "title", "venueName"]))
        a = resolve(first(v, ["address", "direccion", "full_address", "streetAddress", "address1", "formatted_address"]), refs)
        if isinstance(a, dict):
            addr = ", ".join(x for x in [txt(a.get("streetAddress")), txt(a.get("addressLocality"))] if x)
        else:
            addr = txt(a)
        geo = v.get("geo") if isinstance(v.get("geo"), dict) else v
        try:
            lat = float(first(geo, ["latitude", "lat"])); lng = float(first(geo, ["longitude", "lng", "lon"]))
        except (TypeError, ValueError):
            lat = lng = None
    elif isinstance(v, str):
        name = txt(v)
    if not addr:
        gai = d.get("geo_address_info")
        if isinstance(gai, dict):
            addr = txt(gai.get("full_address") or gai.get("address"))
            name = name or txt(gai.get("address"))
    return name, addr, lat, lng


def price_of(d):
    p = first(d, PRICE_KEYS)
    free = bool(d.get("isAccessibleForFree") is True or d.get("is_free") is True or d.get("free") is True)
    cur = txt(d.get("priceCurrency") or d.get("currency"))
    val = None
    if isinstance(p, list):
        vals = [price_of({"offers": x} if isinstance(x, dict) else {"price": x})[0] for x in p]
        vals = [x for x in vals if x is not None]
        val = min(vals) if vals else None
    elif isinstance(p, dict):
        if p.get("is_free") is True:
            free = True
        inner = first(p, ["lowPrice", "price", "min", "amount", "cents", "value"])
        if isinstance(inner, dict):
            inner = first(inner, ["cents", "amount", "value"])
            if inner is not None and "cents" in json.dumps(p.get("price", {})):
                inner = float(inner) / 100
        cur = txt(p.get("priceCurrency") or p.get("currency")) or cur
        try:
            val = float(str(inner).replace(",", ".")) if inner is not None else None
        except ValueError:
            val = None
    elif p is not None:
        m = re.search(r"\d[\d.,]*", str(p))
        if m:
            s = m[0]
            s = s.replace(".", "").replace(",", ".") if re.search(r"\d\.\d{3}", s) else s.replace(",", "")
            try:
                val = float(s)
            except ValueError:
                val = None
        if re.search(r"gratis|free|libre", str(p), re.I):
            free = True
    if free and not val:
        return 0.0, "ARS"
    return val, (cur or "ARS").upper()


def url_of(d, base):
    u = first(d, URL_KEYS)
    if isinstance(u, dict):
        u = first(u, ["url", "href"])
    if not isinstance(u, str) or not u.strip():
        return ""
    u = u.strip()
    if u.startswith("http"):
        return u
    if first(d, ["slug"]) == u and "/" not in u:
        return ""  # a bare slug; the caller keeps the page url instead
    return urljoin(base, u)


def img_of(d):
    v = first(d, IMG_KEYS)
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, dict):
        v = first(v, ["url", "src", "contentUrl", "filename", "original"])
    return v if isinstance(v, str) and v.startswith("http") else ""


def dates_of(d, window, refs=None):
    """All (date, 'HH:MM' or '') occurrences of an event-like dict inside the window."""
    occ = []
    s = first(d, START_KEYS)
    if isinstance(s, dict):  # {"date": ..., "time": ...} or {"local": ...}
        s = first(s, ["dateTime", "local", "datetime", "date", "utc", "iso"])
    x, has_t = parse_dt(s)
    if x:
        t = txt(first(d, ["time", "hora", "startTime", "start_time"])) if not has_t else ""
        hm = x.strftime("%H:%M") if has_t else (t[:5] if re.match(r"^\d{1,2}:\d{2}", t) else "")
        occ.append((x.date(), "" if hm == "00:00" else hm))
        # a run (exhibition / season) with an end date
        e, _ = parse_dt(first(d, ["endDate", "end_date", "end_at", "endAt", "fecha_fin", "fechaFin"]))
        if e and (e.date() - x.date()).days > 1:
            occ.append(("RUN", x.date(), e.date()))
    for k in MULTI_KEYS:
        v = d.get(k)
        if isinstance(v, list):
            for it in v[:200]:
                it = resolve(it, refs)
                if isinstance(it, dict):
                    y, ht = parse_dt(first(it, START_KEYS))
                    if y:
                        occ.append((y.date(), y.strftime("%H:%M") if ht else ""))
                else:
                    y, ht = parse_dt(it)
                    if y:
                        occ.append((y.date(), y.strftime("%H:%M") if ht else ""))
    out, runs = [], []
    for o in occ:
        if o[0] == "RUN":
            runs.append(o[1:])
        elif in_window(o[0], window):
            out.append(o)
    return out, runs


def to_candidate(d, base, source, window, refs=None, kind_hint=None):
    title = txt(resolve(first(d, TITLE_KEYS), refs))
    if not title or len(title) < 3 or len(title) > 160:
        return None
    occ, runs = dates_of(d, window, refs)
    run = None
    if not occ and runs:
        a, b = runs[0]
        if a <= TODAY + dt.timedelta(days=window) and b >= TODAY:
            run = (max(a, TODAY).isoformat(), b.isoformat())
    if not occ and not run:
        return None
    venue, addr, lat, lng = venue_of(d, refs)
    price, cur = price_of(d)
    desc = txt(first(d, DESC_KEYS))[:600]
    tags = []
    for k in ("genre", "genres", "category", "categories", "tags", "type", "@type", "eventType", "keywords", "subcategory"):
        v = d.get(k)
        if isinstance(v, list):
            tags += [txt(resolve(x, refs)) for x in v[:8]]
        elif v:
            tags.append(txt(v))
    artists = d.get("artists") or d.get("performer") or d.get("performers")
    if isinstance(artists, list):
        tags += ["artist:" + txt(resolve(a, refs)) for a in artists[:6]]
    return {
        "source": source, "title": title, "url": url_of(d, base) or base, "page": base,
        "dates": sorted({o[0].isoformat() for o in occ}),
        "time": next((o[1] for o in sorted(occ) if o[1]), ""),
        "run": run, "venue": venue, "address": addr, "lat": lat, "lng": lng,
        "price": price, "currency": cur, "desc_raw": desc, "img_hint": img_of(d), "tags": [t for t in tags if t][:14],
    }


def walk(obj, fn, depth=0):
    if depth > 14:
        return
    if isinstance(obj, dict):
        fn(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk(v, fn, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:2000]:
            if isinstance(v, (dict, list)):
                walk(v, fn, depth + 1)


def looks_like_event(d):
    t = d.get("@type")
    if isinstance(t, list):
        t = next((x for x in t if x in EVENT_TYPES), None)
    if t in EVENT_TYPES:
        return True
    if isinstance(t, str) and t.endswith("Event"):
        return True
    return first(d, TITLE_KEYS) is not None and (first(d, START_KEYS) is not None or any(isinstance(d.get(k), list) and d.get(k) for k in MULTI_KEYS))


def from_json(obj, base, source, window, refs=None):
    found = []
    if refs is None and isinstance(obj, dict):
        # Apollo / normalized caches: {"Event:123": {...}, "Venue:9": {...}}
        cache = obj.get("__APOLLO_STATE__") or (obj.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__") if isinstance(obj.get("props"), dict) else None)
        if isinstance(cache, dict):
            refs = cache

    def fn(d):
        if looks_like_event(d):
            c = to_candidate(d, base, source, window, refs)
            if c:
                found.append(c)
    walk(obj, fn)
    return found


SCRIPT_JSON = re.compile(r"(?:window\.)?__[A-Z0-9_]+__\s*=\s*({.*?})\s*;?\s*(?:</script>|$)", re.S)


def from_html(html, base, source, window):
    soup = BeautifulSoup(html, "lxml")
    found = []
    for s in soup.find_all("script"):
        typ = (s.get("type") or "").lower()
        raw = s.string or s.get_text() or ""
        if not raw.strip():
            continue
        data = None
        if "ld+json" in typ or typ == "application/json" or s.get("id") == "__NEXT_DATA__":
            try:
                data = json.loads(raw)
            except Exception:
                try:
                    data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
                except Exception:
                    data = None
        elif "__" in raw[:4000]:
            m = SCRIPT_JSON.search(raw)
            if m:
                try:
                    data = json.loads(m[1])
                except Exception:
                    data = None
        if data is not None:
            found += from_json(data, base, source, window)
    return found, soup


def links(soup, base, pattern):
    rx = re.compile(pattern)
    out = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0]
        if rx.match(u) and u not in out:
            out.append(u)
    return out


# ------------------------------------------------------------------ source kinds
def run_pages(src, window, log):
    found, follow = [], []
    for u in src["urls"]:
        try:
            r = get(u)
            log.append(f"{u} -> {r.status_code} {len(r.text)}b")
            if r.status_code != 200:
                continue
            f, soup = from_html(r.text, r.url, src["name"], window)
            found += f
            if src.get("follow"):
                follow += [x for x in links(soup, r.url, src["follow"]) if x not in follow]
        except Exception as ex:
            log.append(f"{u} !! {ex}")
    follow = follow[: src.get("max_follow", 80)]
    if follow:
        log.append(f"following {len(follow)} detail pages")

        def one(u):
            try:
                r = get(u)
                return from_html(r.text, r.url, src["name"], window)[0] if r.status_code == 200 else []
            except Exception:
                return []
        with cf.ThreadPoolExecutor(6) as ex:
            for f in ex.map(one, follow):
                found += f
    return found


def run_allevents(src, window, log):
    found = []
    dec = json.JSONDecoder()
    for u in src["urls"]:
        try:
            r = get(u)
            log.append(f"{u} -> {r.status_code} {len(r.text)}b")
            if r.status_code != 200:
                continue
            for m in re.finditer(r"events_data\s*=\s*", r.text):
                try:
                    arr, _ = dec.raw_decode(r.text[m.end():])
                except Exception:
                    continue
                for e in arr if isinstance(arr, list) else []:
                    if not isinstance(e, dict) or not e.get("start_time"):
                        continue
                    v = e.get("venue") or {}
                    if (v.get("city") or "") not in ("Buenos Aires", ""):
                        continue
                    try:
                        # AllEvents' start_time is the LOCAL wall-clock time written as if it were UTC
                        # (checked 2026-10-27: 21:00 shows as 18:00 if converted); read it as UTC, then label it -03:00
                        d0 = dt.datetime.fromtimestamp(int(e["start_time"]), dt.timezone.utc).replace(tzinfo=dt.timezone(dt.timedelta(hours=-3)))
                    except Exception:
                        continue
                    e2 = dict(e); e2["name"] = e.get("eventname_raw") or e.get("eventname")
                    e2["startDate"] = d0.isoformat()
                    e2["url"] = e.get("event_url") or e.get("share_url")
                    e2["location"] = {"name": e.get("location_raw") or e.get("location"), "address": v.get("full_address"),
                                      "latitude": v.get("latitude"), "longitude": v.get("longitude")}
                    e2["image"] = e.get("thumb_url_large") or e.get("banner_url")
                    e2["tags"] = (e.get("categories") or []) + (e.get("tags") or [])
                    c = to_candidate(e2, u, src["name"], window)
                    if c:
                        found.append(c)
        except Exception as ex:
            log.append(f"{u} !! {ex}")
    return found


_CA_DONE = []


def ensure_proxy_ca(log):
    """Cloud tasks route HTTPS through an inspecting proxy; Chromium needs its CA (or ignore_https_errors)."""
    if _CA_DONE:
        return
    _CA_DONE.append(1)
    import glob as _g, shutil, subprocess
    cas = [c for c in _g.glob("/root/.ccr/*.crt") if "ca" in os.path.basename(c).lower()]
    if not cas:
        return
    try:
        if not shutil.which("certutil"):
            subprocess.run(["apt-get", "install", "-y", "-q", "libnss3-tools"], capture_output=True, timeout=180)
        os.makedirs(os.path.expanduser("~/.pki/nssdb"), exist_ok=True)
        db = "sql:" + os.path.expanduser("~/.pki/nssdb")
        if not os.path.exists(os.path.expanduser("~/.pki/nssdb/cert9.db")):
            subprocess.run(["certutil", "-N", "-d", db, "--empty-password"], capture_output=True, timeout=30)
        for i, c in enumerate(cas):
            subprocess.run(["certutil", "-A", "-n", f"ccr-proxy-{i}", "-t", "C,,", "-i", c, "-d", db], capture_output=True, timeout=30)
        log.append(f"proxy CA registered for Chromium ({len(cas)} cert)")
    except Exception as ex:
        log.append(f"proxy CA setup skipped: {str(ex)[:80]}")


def run_browser(src, window, log):
    ensure_proxy_ca(log)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.append("playwright missing: pip install --break-system-packages playwright")
        return run_pages(src, window, log)
    found, follow, dumps = [], [], []
    exe = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
    with sync_playwright() as p:
        kw = {"headless": True, "args": ["--no-sandbox"]}
        try:
            b = p.chromium.launch(executable_path=exe, **kw) if exe else p.chromium.launch(**kw)
        except Exception:
            b = p.chromium.launch(**kw)
        ctx = b.new_context(user_agent=UA, locale="es-AR", timezone_id="America/Argentina/Buenos_Aires", ignore_https_errors=True)
        page = ctx.new_page()
        captured = []

        def on_resp(resp):
            try:
                ct = resp.headers.get("content-type", "")
                if "json" in ct and resp.request.resource_type in ("xhr", "fetch", "document", "other"):
                    captured.append((resp.url, resp.json()))
            except Exception:
                pass
        page.on("response", on_resp)
        for u in src["urls"]:
            try:
                page.goto(u, wait_until="networkidle", timeout=45000)
            except Exception as ex:
                log.append(f"{u} goto: {str(ex)[:80]}")
            try:
                for _ in range(src.get("scrolls", 4)):
                    page.mouse.wheel(0, 4000); page.wait_for_timeout(1200)
                html = page.content()
                f, soup = from_html(html, page.url, src["name"], window)
                found += f
                dumps.append((u, soup.get_text("\n")[:40000]))
                if src.get("follow"):
                    follow += [x for x in links(soup, page.url, src["follow"]) if x not in follow]
                log.append(f"{u} -> dom {len(f)} ev, {len(captured)} json responses so far")
            except Exception as ex:
                log.append(f"{u} !! {str(ex)[:120]}")
        for cu, data in captured:
            f = from_json(data, cu, src["name"], window)
            if f:
                log.append(f"  json {cu[:110]} -> {len(f)}")
            found += f
        follow = follow[: src.get("max_follow", 80)]
        if follow:
            log.append(f"following {len(follow)} detail pages (http first)")
        for u in follow:
            got = []
            try:
                r = get(u)
                if r.status_code == 200:
                    got = from_html(r.text, r.url, src["name"], window)[0]
            except Exception:
                pass
            if not got and src.get("follow_browser", True) and len(follow) <= 150:
                n0 = len(captured)
                try:
                    page.goto(u, wait_until="networkidle", timeout=30000)
                    got = from_html(page.content(), page.url, src["name"], window)[0]
                    for cu, data in captured[n0:]:
                        for c in from_json(data, u, src["name"], window):
                            if c["url"] == cu:
                                c["url"] = u
                            got.append(c)
                except Exception:
                    pass
            found += got
        b.close()
    os.makedirs(f"{OUT}/text", exist_ok=True)
    with open(f"{OUT}/text/{src['name']}.txt", "w") as fh:
        for u, t in dumps:
            fh.write(f"==== {u}\n{t}\n")
    with open(f"{OUT}/text/{src['name']}.json_urls.txt", "w") as fh:
        fh.write("\n".join(cu for cu, _ in captured))
    return found


RA_Q = """query GET_EVENT_LISTINGS($filters: FilterInputDtoInput, $pageSize: Int, $page: Int) {
  eventListings(filters: $filters, pageSize: $pageSize, page: $page) {
    data { id listingDate event { id title date startTime endTime contentUrl content cost
      images { filename } venue { name address } artists { name } genres { name } } }
    totalResults } }"""


def ra_area(log):
    hdr = {"Content-Type": "application/json", "Referer": "https://ra.co/events/ar/buenosaires", "Origin": "https://ra.co"}
    for q in ('{ area(areaUrlName: "buenosaires", countryUrlCode: "ar") { id name } }',
              '{ areas(searchTerm: "Buenos Aires", limit: 5) { id name urlName country { urlCode } } }',
              '{ areas(searchTerm: "Buenos Aires") { id name urlName } }'):
        try:
            polite("https://ra.co")
            r = SESSION.post("https://ra.co/graphql", json={"query": q}, headers=hdr, timeout=20)
            data = (r.json() or {}).get("data") or {}
            a = data.get("area")
            if a and a.get("id"):
                return int(a["id"])
            for a in data.get("areas") or []:
                if "buenos" in (a.get("urlName") or a.get("name") or "").lower():
                    return int(a["id"])
        except Exception as ex:
            log.append(f"ra area query: {str(ex)[:80]}")
    try:
        r = get("https://ra.co/events/ar/buenosaires")
        for pat in (r'"area":\{"__typename":"Area","id":"(\d+)"', r'"areaId":"?(\d+)', r'Area:(\d+)"', r'"areas":\{"eq":(\d+)'):
            m = re.search(pat, r.text)
            if m:
                return int(m[1])
    except Exception as ex:
        log.append(f"ra area page: {ex}")
    try:  # the listing page asks GraphQL for its own area id; read it off that request
        ensure_proxy_ca(log)
        from playwright.sync_api import sync_playwright
        found = []
        with sync_playwright() as p:
            exe = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
            b = p.chromium.launch(executable_path=exe, headless=True, args=["--no-sandbox"]) if exe else p.chromium.launch(headless=True, args=["--no-sandbox"])
            pg = b.new_context(user_agent=UA, ignore_https_errors=True).new_page()
            pg.on("request", lambda rq: found.extend(re.findall(r'"areas"\s*:\s*\{\s*"eq"\s*:\s*"?(\d+)', rq.post_data or "")) if "graphql" in rq.url else None)
            pg.goto("https://ra.co/events/ar/buenosaires", wait_until="networkidle", timeout=45000)
            b.close()
        if found:
            return int(found[0])
    except Exception as ex:
        log.append(f"ra area browser: {str(ex)[:80]}")
    return None


def run_ra(src, window, log):
    area = src.get("area") or ra_area(log)
    log.append(f"RA area id {area}")
    if not area:
        return []
    found, page = [], 1
    hdr = {"Content-Type": "application/json", "Referer": "https://ra.co/events/ar/buenosaires", "Origin": "https://ra.co"}
    while page <= 10:
        body = {"query": RA_Q, "variables": {"filters": {"areas": {"eq": area}, "listingDate": {
            "gte": TODAY.isoformat(), "lte": (TODAY + dt.timedelta(days=window)).isoformat()}}, "pageSize": 100, "page": page}}
        polite("https://ra.co")
        r = SESSION.post("https://ra.co/graphql", json=body, headers=hdr, timeout=25)
        if r.status_code != 200:
            log.append(f"RA graphql {r.status_code} {r.text[:200]}")
            break
        js = r.json()
        if js.get("errors"):
            log.append(f"RA errors {str(js['errors'])[:300]}")
            break
        data = (js.get("data") or {}).get("eventListings") or {}
        rows = data.get("data") or []
        for row in rows:
            e = row.get("event") or {}
            st, _ = parse_dt(e.get("startTime") or e.get("date"))
            if not st or not in_window(st.date(), window):
                continue
            imgs = e.get("images") or []
            found.append({"source": "ra", "title": e.get("title", ""), "url": urljoin("https://ra.co", e.get("contentUrl") or ""), "page": "https://ra.co/events/ar/buenosaires",
                          "dates": [st.date().isoformat()], "time": st.strftime("%H:%M"), "run": None,
                          "venue": (e.get("venue") or {}).get("name", ""), "address": (e.get("venue") or {}).get("address", ""), "lat": None, "lng": None,
                          "price": price_of({"price": e.get("cost")})[0] if e.get("cost") else None, "currency": "ARS",
                          "desc_raw": txt(e.get("content"))[:600], "img_hint": imgs[0].get("filename", "") if imgs else "",
                          "tags": ["dj", "club"] + [g.get("name", "") for g in (e.get("genres") or [])] + ["artist:" + a.get("name", "") for a in (e.get("artists") or [])[:6]]})
        if len(rows) < 100:
            break
        page += 1
    return found


LUMA_API = "https://api.lu.ma"
LUMA_CENTER = (-34.6037, -58.3816)
LUMA_RADIUS_KM = 40          # CABA plus the near suburbs
LUMA_SEED_CALS = []          # extra community calendars to always read (cal-...); found ones are added each run


def _km(a, b):
    import math
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def luma_candidate(ent, window):
    """One Luma entry (discover feed or calendar item) -> candidate, or None when online, outside the city or outside the window."""
    ev = ent.get("event") or {}
    name = (ev.get("name") or "").strip()
    if not name or ev.get("location_type") in ("online", "zoom") or ev.get("visibility") not in (None, "public"):
        return None
    co = ev.get("coordinate") or {}
    gi = ev.get("geo_address_info") or {}
    pc = gi.get("place_coordinate") or {}
    lat, lng = co.get("latitude") or pc.get("latitude"), co.get("longitude") or pc.get("longitude")
    if lat is None or lng is None or _km((float(lat), float(lng)), LUMA_CENTER) > LUMA_RADIUS_KM:
        return None
    try:
        tz = BA
        st = dt.datetime.fromisoformat(ev["start_at"].replace("Z", "+00:00")).astimezone(tz)
        en = dt.datetime.fromisoformat((ev.get("end_at") or ev["start_at"]).replace("Z", "+00:00")).astimezone(tz)
    except Exception:
        return None
    last = TODAY + dt.timedelta(days=window)
    if (en - dt.timedelta(hours=8)).date() < TODAY and st.date() < TODAY or st.date() > last:
        return None
    dates, run = [], None
    end_d = max(st.date(), (en - dt.timedelta(hours=8)).date())   # a party ending at 4am belongs to the night it started
    span = (end_d - st.date()).days
    if span <= 6:   # one-off or a few days (a conference): list each day
        d = st.date()
        while d <= end_d:
            if TODAY <= d <= last:
                dates.append(d.isoformat())
            d += dt.timedelta(days=1)
    else:           # long run (an exhibition, a course)
        run = (max(st.date(), TODAY).isoformat(), end_d.isoformat())
    if not dates and not run:
        return None
    slug = ev.get("url") or ""
    url = slug if slug.startswith("http") else "https://luma.com/" + slug
    venue = (gi.get("address") or "").strip()
    addr = gi.get("full_address") or gi.get("short_address") or ""
    if not venue or venue == addr:
        venue = (gi.get("short_address") or gi.get("description") or "").split(",")[0]
    ti = ent.get("ticket_info") or {}
    price, cur = None, ""
    if ti.get("is_free"):
        price, cur = 0.0, "USD"
    elif isinstance(ti.get("price"), dict) and ti["price"].get("cents") is not None:
        price, cur = ti["price"]["cents"] / 100, (ti["price"].get("currency") or "usd").upper()
    hosts = [h.get("name") for h in (ent.get("hosts") or []) if isinstance(h, dict) and h.get("name")]
    cal = ent.get("calendar") or {}
    tags = ["luma"] + (["calendar:" + cal["name"]] if cal.get("name") and cal.get("name") not in ("Personal", "Events") else []) + ["host:" + h for h in hosts[:4]]
    if ti.get("is_sold_out"):
        tags.append("sold out")
    if ti.get("require_approval"):
        tags.append("approval required")
    return {
        "source": "luma", "title": name[:160], "url": url, "page": url, "lumaId": ev.get("api_id"),
        "dates": dates, "time": st.strftime("%H:%M") if dates else "", "run": run,
        "venue": venue[:120], "address": addr[:200], "lat": float(lat), "lng": float(lng),
        "price": price, "currency": cur, "desc_raw": " ".join(filter(None, [cal.get("name") if cal.get("name") not in ("Personal", "Events") else "", ("Hosted by " + ", ".join(hosts[:3])) if hosts else ""]))[:600],
        "img_hint": ev.get("cover_url") or "", "tags": tags[:14], "_cal": cal.get("api_id") or ev.get("calendar_api_id"),
    }


def run_luma(src, window, log):
    """Every public in-person Luma event within ~40 km of the Obelisco: the location feed (what Luma's own 'near you'
    shows, paged to the end), the city page, and every community calendar those events came from (future items)."""
    seen, found, cals = set(), [], dict.fromkeys(src.get("calendars", []) + LUMA_SEED_CALS)
    stats = defaultdict(int)

    def take(ent, how):
        c = luma_candidate(ent, window)
        stats[how + "_seen"] += 1
        if not c:
            return
        cal = c.pop("_cal", None)
        if cal:
            cals.setdefault(cal, None)
        if c["lumaId"] and c["lumaId"] in seen:
            return
        seen.add(c["lumaId"])
        found.append(c)
        stats[how] += 1

    def pages(url, how, maxp):
        cursor = None
        for _ in range(maxp):
            u = url + (f"&pagination_cursor={quote(cursor)}" if cursor else "")
            try:
                r = get(u)
                if r.status_code != 200:
                    stats[how + "_http" + str(r.status_code)] += 1
                    return
                js = r.json()
            except Exception as ex:
                stats[how + "_" + type(ex).__name__] += 1
                return
            ents = js.get("entries") or []
            for ent in ents:
                take(ent, how)
            # the location feed is in date order: stop once it is past the window
            try:
                lastd = dt.datetime.fromisoformat(ents[-1]["event"]["start_at"].replace("Z", "+00:00")).astimezone(BA).date()
                if lastd > TODAY + dt.timedelta(days=window):
                    return
            except Exception:
                pass
            if not js.get("has_more") or not js.get("next_cursor"):
                return
            cursor = js["next_cursor"]

    lat, lng = LUMA_CENTER
    # 1) location feed around the centre, then two more points so the north and south suburbs are covered too
    for (la, lo) in [(lat, lng), (-34.545, -58.47), (-34.66, -58.43)]:
        pages(f"{LUMA_API}/discover/get-paginated-events?latitude={la}&longitude={lo}&pagination_limit=50", "near", 80)
    # 2) the city page
    try:
        js = get(f"{LUMA_API}/url?url={src.get('slug', 'buenos-aires')}").json()
        pid = ((js.get("data") or {}).get("place") or {}).get("api_id")
        if pid:
            pages(f"{LUMA_API}/discover/get-paginated-events?discover_place_api_id={pid}&pagination_limit=50", "city", 10)
    except Exception as ex:
        stats["city_" + type(ex).__name__] += 1
    # 3) every calendar seen: its other upcoming events (many community events never reach the feeds)
    cal_ids = [c for c in cals if c][: src.get("max_calendars", 250)]
    for cid in cal_ids:
        pages(f"{LUMA_API}/calendar/get-items?calendar_api_id={cid}&period=future&pagination_limit=50", "calendar", 6)
    try:
        json.dump(cal_ids, open(f"{OUT}/luma_calendars.json", "w"))
    except Exception:
        pass
    log.append(f"luma: {len(found)} in-person events in the window (location feed {stats['near']}, city page {stats['city']}, "
               f"calendars {stats['calendar']} from {len(cal_ids)} calendars; looked at {stats['near_seen'] + stats['city_seen'] + stats['calendar_seen']})"
               + (f"; problems {dict((k, v) for k, v in stats.items() if '_http' in k or k[0].isupper() or 'Error' in k)}" if any('_http' in k or 'Error' in k for k in stats) else ""))
    return found

def run_organizers(src, window, log):
    """Each organizer in the directory whose agenda page is machine-readable (found by directory.py discover)."""
    path = src.get("path", f"{OUT}/directory.json")
    if not os.path.exists(path):
        log.append("no directory yet (run directory.py build + discover)")
        return []
    rows = [r for r in json.load(open(path)) if r.get("rd") == "structured" and r.get("ag")]
    order = {"club": 0, "music-venue": 0, "milonga": 1, "theater": 1, "cultural-center": 1, "bar": 2, "cinema": 2, "gallery": 2}
    rows.sort(key=lambda r: (order.get(r.get("t"), 5), -(r.get("ev") or 0)))
    rows = rows[: src.get("limit", 450)]
    log.append(f"{len(rows)} organizer agendas")
    found = []

    def one(r):
        try:
            resp = get(r["ag"], timeout=15)
            if resp.status_code != 200:
                return []
            evs = from_html(resp.text, resp.url, "organizer", window)[0]
        except Exception:
            return []
        for c in evs:
            # the organizer's own page: exact place and area are known
            if not c.get("venue"):
                c["venue"] = r["n"]
            if not c.get("lat") and norm(c["venue"]) == norm(r["n"]):
                c["lat"], c["lng"] = r["la"], r["ln"]
            c["org"] = {"i": r["i"], "n": r["n"], "t": r.get("t"), "z": r.get("z"), "a1": r.get("a1"), "a2": r.get("a2"), "c": r.get("c")}
            c["genre_hint"] = {"milonga": "tango", "cinema": "film", "museum": "museums", "gallery": "galleries", "theater": "theater",
                               "club": "nightlife", "music-venue": "music", "market": "markets", "school": "classes", "convention": "events"}.get(r.get("t"))
        return evs
    with cf.ThreadPoolExecutor(8) as ex:
        for evs in ex.map(one, rows):
            found += evs
    return found


# ------------------------------------------------------------------ Instagram (official Graph API, Business Discovery)
# Reads the recent posts of venues' PUBLIC professional Instagram accounts through Meta's own API, using the
# owner's token. No scraping, no browser, no login as anyone. Only posts whose caption names a date inside the
# window become candidates; undated posts (details only on the flyer) are saved to out/ig_undated.json.
# Credentials: IG_CREDS below (filled in by the app session once the owner sends them), or ./ig.json.
IG_CREDS = {"token": "", "ig_user_id": "", "graph": "https://graph.facebook.com/v25.0"}
IG_PER_RUN = 180          # Meta allows about 200 calls an hour for one account; one call per venue
IG_MAX_AGE_DAYS = 21      # ignore posts older than this
IG_WEEKDAYS = {"lunes": 0, "lun": 0, "martes": 1, "mar": 1, "miercoles": 2, "mie": 2, "mier": 2, "jueves": 3, "jue": 3,
               "viernes": 4, "vie": 4, "sabado": 5, "sab": 5, "domingo": 6, "dom": 6}
IG_MON = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12}


def _ig_fold(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def ig_handle(v):
    v = (v or "").strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", v)
    h = m.group(1) if m else v.lstrip("@").split("/")[0].split("?")[0]
    return h if re.fullmatch(r"[A-Za-z0-9_.]{2,30}", h or "") and h.lower() not in ("p", "reel", "explore", "stories") else None


def ig_dates(caption, posted, window):
    """Dates (and a time) that a caption announces, inside today..today+window. posted = date the post went up."""
    t = _ig_fold(caption)
    end = TODAY + dt.timedelta(days=window)
    found, mentioned = set(), []

    def add(d):
        mentioned.append(d)
        if TODAY <= d <= end:
            found.add(d)

    def year_for(mo, day):
        y = posted.year
        try:
            d = dt.date(y, mo, day)
        except ValueError:
            return None
        if d < posted - dt.timedelta(days=60):   # "05/01" posted in December means next year
            try:
                d = dt.date(y + 1, mo, day)
            except ValueError:
                return None
        return d

    # 18/10, 18-10, 18.10, 18/10/26  (not times like 21.30, not prices)
    for m in re.finditer(r"(?<![\d$.,:])(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?:\s*[/.\-]\s*(\d{2,4}))?(?![\d:])(?!\s*(?:hs|h\b|horas|am|pm))", t):
        day, mo = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= mo <= 12:
            if m.group(3):
                y = int(m.group(3)); y = y + 2000 if y < 100 else y
                try:
                    add(dt.date(y, mo, day))
                except ValueError:
                    pass
            else:
                d = year_for(mo, day)
                if d:
                    add(d)
    # 18 de octubre / 18 oct / sabado 18 de oct
    for m in re.finditer(r"\b(\d{1,2})\s*(?:de\s+)?(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[a-z]*\b", t):
        d = year_for(IG_MON[m.group(2)], int(m.group(1)))
        if d:
            add(d)
    # octubre 18
    for m in re.finditer(r"\b(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)[a-z]*\.?\s+(\d{1,2})\b(?![.:]\d|\s*(?:hs|h\b|horas))", t):
        d = year_for(IG_MON[m.group(1)], int(m.group(2)))
        if d:
            add(d)
    # hoy / manana / esta noche / este sabado / todos los jueves (only for fresh posts)
    if not mentioned and (TODAY - posted).days <= 6:  # no explicit date anywhere (not even a past one)
        if re.search(r"\b(hoy|esta noche|tonight)\b", t):
            add(posted)
        if re.search(r"\b(manana|tomorrow)\b", t):
            add(posted + dt.timedelta(days=1))
        every = re.findall(r"\btodos los (lunes|martes|miercoles|jueves|viernes|sabados?|domingos?)\b", t)
        for w in re.findall(r"\b(?:este|esta|el|proximo|this)\s+(lunes|martes|miercoles|jueves|viernes|sabado|domingo)\b", t):
            wd = IG_WEEKDAYS[w]
            add(posted + dt.timedelta(days=(wd - posted.weekday()) % 7))
        for w in every:
            wd = IG_WEEKDAYS[w.rstrip("s") if w.rstrip("s") in IG_WEEKDAYS else w]
            d = TODAY + dt.timedelta(days=(wd - TODAY.weekday()) % 7)
            while d <= end:
                add(d)
                d += dt.timedelta(days=7)
    tm = ""
    m = (re.search(r"\b([01]?\d|2[0-3])\s*:\s*([0-5]\d)\b", t)
         or re.search(r"\b([01]?\d|2[0-3])\s*[.h]\s*([0-5]\d)\s*(?:hs|h|horas)\b", t)
         or re.search(r"\b([01]?\d|2[0-3])\s*(?:hs|h|horas)\b", t))
    if m:
        tm = f"{int(m.group(1)):02d}:{(m.group(2) if m.lastindex and m.lastindex >= 2 else '00')}"
    return sorted(found), tm


def ig_title(caption, org_name):
    for line in (caption or "").splitlines():
        line = re.sub(r"[#@][\w.]+", "", line)
        line = re.sub(r"[^\w\s$%&'’!¡?¿:.,/()\-–+|·]", "", line).strip(" -–|·:.")
        line = re.sub(r"\s{2,}", " ", line)
        if len(line) >= 6:
            if len(line) < 18 or re.fullmatch(r"[\W\d\s]*(?:hoy|manana|este|esta|sabado|viernes|jueves|domingo|\d+[/.]\d+)[\w\s/.]*", _ig_fold(line)):
                line = f"{org_name}: {line}"
            return line[:110]
    return org_name


def ig_price(caption):
    t = _ig_fold(caption)
    if re.search(r"\b(gratis|gratuit[oa]|entrada libre|free entry|sin cargo|a la gorra)\b", t):
        return 0, "ARS"
    m = re.search(r"\$\s?(\d{1,3}(?:[.,]\d{3})+|\d{3,6})", caption or "")
    if m:
        return int(re.sub(r"[.,]", "", m.group(1))), "ARS"
    return None, ""


def run_instagram(src, window, log):
    creds = dict(IG_CREDS)
    if os.path.exists("ig.json"):
        creds.update(json.load(open("ig.json")))
    if not creds.get("token") or not creds.get("ig_user_id"):
        log.append("waiting for the owner's Instagram API token (not a broken source: do not repair)")
        return []
    path = src.get("path", f"{OUT}/directory.json")
    if not os.path.exists(path):
        log.append("no directory yet")
        return []
    rows = []
    for r in json.load(open(path)):
        h = ig_handle(r.get("ig"))
        if h and r.get("rd") != "dead":
            rows.append((h, r))
    seen, uniq = set(), []
    for h, r in rows:
        if h.lower() not in seen:
            seen.add(h.lower()); uniq.append((h, r))
    # Instagram-only venues every day; the rest rotate so each is read about once a week
    first = [x for x in uniq if x[1].get("rd") == "instagram-only"]
    rest = [x for x in uniq if x[1].get("rd") != "instagram-only"]
    slot = TODAY.toordinal()
    rest = [x for i, x in enumerate(sorted(rest, key=lambda x: x[0].lower())) if (i + slot) % 7 == 0]
    todo = (first + rest)[: src.get("limit", IG_PER_RUN)]
    log.append(f"{len(uniq)} venues with an Instagram handle; reading {len(todo)} today ({len(first)} Instagram-only)")
    fields = ("business_discovery.username({h}){{username,name,media.limit(12)"
              "{{caption,timestamp,permalink,media_type,media_url,thumbnail_url}}}}")
    found, undated, errors, calls = [], [], defaultdict(int), 0
    gmap = {"milonga": "tango", "cinema": "film", "museum": "museums", "gallery": "galleries", "theater": "theater",
            "club": "nightlife", "music-venue": "music", "market": "markets", "school": "classes", "convention": "events", "bar": "nightlife"}
    for h, r in todo:
        try:
            resp = requests.get(f"{creds.get('graph', IG_CREDS['graph'])}/{creds['ig_user_id']}",
                                params={"fields": fields.format(h=h), "access_token": creds["token"]}, timeout=25)
            calls += 1
            j = resp.json()
        except Exception as ex:
            errors[type(ex).__name__] += 1
            continue
        if "error" in j:
            e = j["error"]
            code = e.get("code")
            errors[f"{code}:{e.get('error_subcode', '')}"] += 1
            if code == 190:
                log.append("TOKEN EXPIRED OR REVOKED: the owner must make a new one (guide parts D to F)")
                break
            if code in (4, 17, 32, 613):
                log.append(f"rate limit reached after {calls} calls; the rest wait for tomorrow")
                break
            continue   # e.g. 110/2207013: not a professional account, or renamed handle
        media = ((j.get("business_discovery") or {}).get("media") or {}).get("data") or []
        for p in media:
            try:
                posted = dt.datetime.fromisoformat(p["timestamp"].replace("+0000", "+00:00")).astimezone(BA).date()
            except Exception:
                continue
            if (TODAY - posted).days > IG_MAX_AGE_DAYS:
                continue
            cap = p.get("caption") or ""
            dates, tm = ig_dates(cap, posted, window)
            if not dates:
                if (TODAY - posted).days <= 7:
                    undated.append({"org": r["i"], "venue": r["n"], "handle": h, "permalink": p.get("permalink"),
                                    "image": p.get("media_url") if p.get("media_type") != "VIDEO" else p.get("thumbnail_url"),
                                    "caption": cap[:500], "posted": posted.isoformat()})
                continue
            price, cur = ig_price(cap)
            found.append({
                "source": "instagram", "title": ig_title(cap, r["n"]), "url": p.get("permalink"), "page": p.get("permalink"),
                "dates": [d.isoformat() for d in dates], "time": tm, "run": None,
                "venue": r["n"], "address": r.get("ad", ""), "lat": r.get("la"), "lng": r.get("ln"),
                "price": price, "currency": cur, "desc_raw": cap[:600], "img_hint": "",
                "tags": ["instagram", p.get("media_type", "").lower()],
                "org": {"i": r["i"], "n": r["n"], "t": r.get("t"), "z": r.get("z"), "a1": r.get("a1"), "a2": r.get("a2"), "c": r.get("c")},
                "genre_hint": gmap.get(r.get("t")),
            })
    json.dump(undated, open(f"{OUT}/ig_undated.json", "w"), ensure_ascii=False, indent=1)
    log.append(f"{calls} API calls, {len(found)} dated posts, {len(undated)} recent undated posts saved for flyer reading"
               + (f", errors {dict(errors)}" if errors else ""))
    return found


# ------------------------------------------------------------------ city profile + platform collectors (added 2026-10-19)
# One CITY dict says where to look on every platform. To harvest another city, copy this dict, change the
# values (Passline region names, Alternativa provinces, Alpogo province ids, Plateanet zones, Venti city,
# bounding box, towns to skip) and run the same collectors. Nothing else in the collectors is city-specific.
CITY = {
    "name": "Buenos Aires metro (AMBA)",
    "country": "AR",
    "bbox": (-35.30, -59.40, -33.90, -57.75),          # south, west, north, east
    "passline": {"country": "argentina", "regions": ["Capital Federal", "Buenos Aires"]},
    "alternativa": {"provinces": ["CABA", "Buenos Aires"]},
    "alpogo": {"provincia_ids": ["1", "25"]},           # 1 Buenos Aires (includes the city), 25 Capital Federal
    "plateanet": {"zones": ["CABA", "GBA Oeste", "GBA Sur", "GBA Norte", "La Plata", "Prov. Buenos Aires"]},
    "venti": {"cities": ["Buenos Aires"]},
    "quehacemos": {"provinces": ["Buenos Aires"]},
    "entradauno": {"zones": ["CABA", "Buenos Aires", "Canning", "La Plata", "Lanus", "San Isidro", "Tigre"]},     # Argentina-wide catalog; province "Buenos Aires" includes the city
    # province-wide feeds also carry the coast and the interior: drop these towns (matched in town/venue text)
    "skip_towns": ["mar del plata", "pergamino", "junin", "salto", "bahia blanca", "tandil", "necochea", "pinamar", "villa gesell",
                   "olavarria", "azul", "chascomus", "san nicolas de los arroyos", "san pedro", "mercedes", "chivilcoy", "nueve de julio",
                   "9 de julio", "bragado", "tres arroyos", "miramar", "balcarce", "trenque lauquen", "lobos", "navarro", "baradero",
                   "ramallo", "arrecifes", "pehuajo", "bolivar", "dolores", "san clemente", "mar de ajo", "santa teresita", "coronel suarez",
                   "carmen de patagones", "carhue", "rojas", "colon", "lincoln", "chacabuco", "arrecifes", "capitan sarmiento",
                   "san antonio de areco", "mar del tuyu", "costa del este", "cariló", "carilo", "general madariaga", "monte hermoso", "la pampa",
                   # other provinces and big cities (province-wide and national feeds)
                   "rosario", "santa fe", "cordoba", "mendoza", "tucuman", "san miguel de tucuman", "salta", "jujuy", "neuquen", "bariloche",
                   "san carlos de bariloche", "ushuaia", "rio grande", "tierra del fuego", "san juan", "san luis", "la rioja", "catamarca",
                   "corrientes", "misiones", "posadas", "resistencia", "chaco", "formosa", "santiago del estero", "entre rios", "parana",
                   "rafaela", "villa carlos paz", "carlos paz", "rio cuarto", "villa maria", "chubut", "trelew", "puerto madryn", "pto madryn",
                   "comodoro rivadavia", "rio negro", "cipolletti", "general roca", "santa cruz", "rio gallegos", "el calafate", "funes",
                   "montevideo", "punta del este", "uruguay", "santiago de chile", "chile", "lima", "peru", "asuncion", "paraguay"],
}


def in_city(lat=None, lng=None, text=""):
    """True when a point is inside the city box, or (no point) when the town text is not a skipped town."""
    if lat is not None and lng is not None:
        s, w, n, e = CITY["bbox"]
        try:
            return s <= float(lat) <= n and w <= float(lng) <= e
        except (TypeError, ValueError):
            pass
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    # a town counts only as its own address part ("Mitre 754, Rosario, Santa Fe"), so a street named after a province
    # ("Av. Cordoba 946") never drops a city venue
    parts = [re.sub(r"[^a-z ]+", " ", p).strip() for p in re.split(r"[,\-\u2013|/()]", t) if not re.search(r"\d", p)]  # "Corrientes 1659" is a street
    skip = CITY["skip_towns"]
    return not any(p == k or p.startswith(k + " ") and len(p) <= len(k) + 12 for p in parts if p for k in skip)


def _json_or_jsonp(t):
    """JSON, or JSONP like jsoncallback({...}) / ({...}); None when it's neither (an HTML challenge page, for example)."""
    t = (t or "").strip().lstrip("\ufeff").strip()
    m = re.match(r"^[A-Za-z_$][\w$.]*\s*\(", t)
    if m or t.startswith("("):
        t = t[t.index("(") + 1:t.rindex(")")].strip().lstrip("\ufeff")
    if t[:1] not in "[{":
        return None
    try:
        return json.loads(t)
    except ValueError:
        return None


def http_json(url, method="GET", body=None, headers=None, origin=None, log=None):
    """JSON from a public endpoint. Falls back to calling it from inside a real browser page on the platform's
    own site (same request the site itself makes) when the plain request is refused."""
    hdr = {"Accept": "application/json, text/plain, */*"}
    hdr.update(headers or {})
    try:
        polite(url)
        if method == "POST":
            r = SESSION.post(url, json=body, headers=hdr, timeout=30)
        else:
            r = SESSION.get(url, headers=hdr, timeout=30)
        if r.status_code == 200:
            j = _json_or_jsonp(r.text)
            if j is not None:
                return j
        if log is not None:
            log.append(f"plain {method} {url[:70]} -> {r.status_code} {r.text[:60]!r}")
    except Exception as ex:
        if log is not None:
            log.append(f"plain {method} {url[:70]} !! {str(ex)[:80]}")
    if not origin:
        return None
    def in_page():  # same request from the platform's own page, in the shared Chromium
        _browser_html_now(origin, log, 500)
        pg = _PW["ctx"].new_page()
        try:
            pg.goto(origin, wait_until="domcontentloaded", timeout=45000)
            js = """async ([u, m, b, h]) => { const r = await fetch(u, {method: m, headers: h, body: b ? JSON.stringify(b) : undefined});
                     return await r.text(); }"""
            return pg.evaluate(js, [url, method, body, dict(hdr, **({"Content-Type": "application/json"} if body else {}))])
        finally:
            pg.close()
    try:
        j = _json_or_jsonp(_PW_EXEC.submit(in_page).result(timeout=120))
        if j is None:
            raise ValueError("not JSON")
        return j
    except Exception as ex:
        if log is not None:
            log.append(f"browser {method} {url[:70]} !! {str(ex)[:100]}")
        return None


def _cand(source, title, url, dates, time_="", venue="", address="", lat=None, lng=None, price=None, currency="ARS",
          desc="", img="", tags=None, run=None, genre_hint=None, neighborhood_=""):
    c = {"source": source, "title": htmlmod.unescape(str(title or "")).strip(), "url": url, "page": url, "dates": sorted(set(dates)),
         "time": ("" if time_ in ("00:00", None) else (time_ or "")[:5]), "run": run, "venue": htmlmod.unescape(str(venue or "")).strip(),
         "address": htmlmod.unescape(str(address or "")).strip(), "lat": lat, "lng": lng, "price": price, "currency": currency,
         "desc_raw": txt(desc)[:600], "img_hint": img or "", "tags": [t for t in (tags or []) if t][:14]}
    if genre_hint:
        c["genre_hint"] = genre_hint
    if neighborhood_:
        c["hood"] = neighborhood_
    return c


def _window_days(window):
    return [TODAY + dt.timedelta(days=i) for i in range(window + 1)]


# --- Passline: api.passline.com/v1/event/GetBillboardByFilters (the search box's own request)
def run_passline_api(src, window, log):
    cfg = CITY["passline"]
    regions = http_json(f"https://api.passline.com/v1/general/GetRegionsByCountry/{cfg['country']}", origin="https://home.passline.com/home", log=log) or []
    want = {norm(r) for r in cfg["regions"]}
    ids = [r["id"] for r in regions if norm(htmlmod.unescape(r.get("nombre", ""))) in want]
    log.append(f"passline regions {ids}")
    found, seen = [], set()
    end = TODAY + dt.timedelta(days=window)
    for rid in ids:
        off = 0
        while off < 6000:
            body = {"country": cfg["country"], "commune": "", "type": 0, "start_date": f"{TODAY} 00:00:00", "end_date": f"{end} 23:59:59",
                    "text": "", "tag_id": "", "tag": None, "offset": "1", "limit": f"{off},300", "region": rid}
            rows = http_json("https://api.passline.com/v1/event/GetBillboardByFilters", "POST", body,
                             {"Content-Type": "application/json", "Origin": "https://home.passline.com", "Referer": "https://home.passline.com/"},
                             origin="https://home.passline.com/home", log=log)
            if not isinstance(rows, list):
                log.append(f"passline region {rid} offset {off}: no list")
                break
            for e in rows:
                if e.get("id") in seen:
                    continue
                seen.add(e.get("id"))
                town = htmlmod.unescape(e.get("nombre_communa") or "")
                if not in_city(text=town + " " + (e.get("lugar") or "")):
                    continue
                d0, d1 = e.get("fecha_inicio"), e.get("fecha_termino") or e.get("fecha_inicio")
                try:
                    a, b = dt.date.fromisoformat(d0), dt.date.fromisoformat(d1)
                except (TypeError, ValueError):
                    continue
                dates, run = [], None
                if a == b or (b - a).days <= 1:
                    dates = [a.isoformat()] if in_window(a, window) else []
                else:  # a run: use the open days when given, else keep it as a date range
                    wd = [w for w in re.findall(r"lun|mar|mi[eé]|jue|vie|s[aá]b|dom", norm(e.get("dias_atencion") or ""))]
                    if wd:
                        idx = {"lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6}
                        ok = {idx.get(w, -1) for w in wd}
                        dates = [d.isoformat() for d in _window_days(window) if a <= d <= b and d.weekday() in ok]
                    if not dates and b >= TODAY:
                        run = (max(a, TODAY).isoformat(), b.isoformat())
                if not dates and not run:
                    continue
                img = e.get("miniatura") or ""
                found.append(_cand("passline", e.get("nombre"), f"https://www.passline.com/eventos/{e.get('slug')}", dates,
                                   (e.get("hora_inicio") or "")[:5], e.get("lugar"), town, None, None,
                                   float(e["precio_min"]) if str(e.get("precio_min") or "").replace(".", "", 1).isdigit() else None,
                                   "ARS", "", img.replace("-min.", "-rec.") if img else "",
                                   [e.get("category_name"), e.get("upper_category_name"), e.get("artistas") and "artist:" + str(e.get("artistas"))[:60],
                                    f"org:passline:{e.get('productor')}"], run, neighborhood_=town.title() if town.upper() not in ("C.A.B.A.", "CA", "CABA") else ""))
            if len(rows) < 300:
                break
            off += 300
    return found


# --- Alternativa Teatral: get-json.php?t=novedades&r=cartelera (the whole cartelera in one file)
def run_alternativa_json(src, window, log):
    j = http_json("https://www.alternativateatral.com/get-json.php?t=novedades&r=cartelera", origin="https://www.alternativateatral.com/cartelera.asp", log=log)
    if not isinstance(j, dict):
        return []
    provs = {norm(p) for p in CITY["alternativa"]["provinces"]}
    days = _window_days(window)
    found = []
    for sid, e in (j.get("espectaculos") or {}).items():
        tags = [c.get("descripcion") for c in (e.get("clasificaciones") or {}).values()]
        for lid, l in (e.get("lugares") or {}).items():
            if norm(l.get("provincia")) not in provs or not in_city(l.get("lat"), l.get("lng"), l.get("zona")):
                continue
            dates, times = set(), []
            for f in (l.get("funciones") or {}).values():
                try:
                    a, b = dt.date.fromisoformat(f["fecha_desde"]), dt.date.fromisoformat(f["fecha_hasta"])
                    wd = (int(f["nro_dia"]) + 5) % 7      # their 1 = Sunday ... 7 = Saturday; Python 0 = Monday
                except (KeyError, TypeError, ValueError):
                    continue
                for d in days:
                    if a <= d <= b and d.weekday() == wd:
                        dates.add(d.isoformat()); times.append((d, f.get("hora") or ""))
            if not dates:
                continue
            first_time = sorted(times)[0][1] if times else ""
            img = e.get("imagen_ficha") or ""
            if img and not img.startswith("http"):
                img = urljoin("https://img.alternativateatral.com/", img.lstrip("/"))
            name = l.get("nombre") or ""
            found.append(_cand("alternativa", e.get("titulo"), urljoin("https://www.alternativateatral.com/", e.get("url") or ""), dates, first_time,
                               name.title() if name.isupper() else name, l.get("direccion"), l.get("lat"), l.get("lng"), None, "ARS", "", img,
                               tags + ([f"tickets:{e.get('url_entradas')}"] if e.get("url_entradas") else []),
                               genre_hint="theater", neighborhood_=l.get("zona") or ""))
    return found


# --- Alpogo: alpogo.com/api/events/getEvents2 (one row per performance)
def run_alpogo_api(src, window, log):
    rows, seen = [], set()
    for pid in CITY["alpogo"]["provincia_ids"]:
        off = 0
        while off < 4000:
            body = {"limit": 200, "offset": off, "eventos_estado": "activo", "funciones_estado": "activo", "provincia_id": pid,
                    "tipo_actividad": None, "fecha_inicio": None, "fecha_fin": None}
            a = http_json("https://alpogo.com/api/events/getEvents2", "POST", body,
                          {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "Origin": "https://alpogo.com", "Referer": "https://alpogo.com/"},
                          origin="https://alpogo.com/", log=log)
            if not isinstance(a, list):
                break
            for x in a:
                if x.get("id_funcion") not in seen:
                    seen.add(x.get("id_funcion")); rows.append(x)
            if len(a) < 200:
                break
            off += 200
    by_ev = {}
    for x in rows:
        st, _ = parse_dt((x.get("fecha_funcion") or "").replace(" ", "T"))
        if not st or not in_window(st.date(), window) or not in_city(text=x.get("lugar")):
            continue
        k = x.get("id_evento") or x.get("id")
        c = by_ev.get(k)
        if not c:
            url = x.get("url") or f"https://alpogo.com/evento/{x.get('slug')}"
            price = None
            try:
                price = 0.0 if str(x.get("gratuito")) == "1" else float(str(x.get("menor_precio") or "").replace(",", "")) or None
            except ValueError:
                pass
            c = by_ev[k] = _cand("alpogo", x.get("nombre"), url if url.startswith("http") else urljoin("https://alpogo.com/", url), [],
                                 st.strftime("%H:%M"), x.get("lugar"), "", None, None, price, "ARS", x.get("descripcion_corta") or "",
                                 x.get("imagen_grilla") or x.get("imagen_cabecera") or "", [x.get("actividad"), x.get("tipo_espacio")])
        c["dates"] = sorted(set(c["dates"]) | {st.date().isoformat()})
    return list(by_ev.values())


# --- Plateanet: mobile.plateanet.com/laravel/public/api/obtenerobrasencartelNew (every show on sale)
SP_MONTH = r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"


def run_plateanet_api(src, window, log):
    a = http_json("https://mobile.plateanet.com/laravel/public/api/obtenerobrasencartelNew", origin="https://www.plateanet.com/home", log=log)
    if not isinstance(a, list):
        return []
    zones = {norm(z) for z in CITY["plateanet"]["zones"]}
    found = []
    for o in a:
        if norm(o.get("cZona")) not in zones:
            continue
        txt_dates = " , ".join(o.get("cFechas") or [])
        dates, times = set(), []
        for d, mon, y, hh in re.findall(r"(\d{1,2})\s+" + SP_MONTH + r"\s+(\d{4})\s+(\d{1,2}:\d{2})", txt_dates.lower()):
            try:
                day = dt.date(int(y), MONTHS[mon], int(d))
            except (ValueError, KeyError):
                continue
            if in_window(day, window):
                dates.add(day.isoformat()); times.append((day, hh))
        if not dates or not in_city(text=o.get("cZona")):
            continue
        imgs = o.get("imagenesObra") or []
        img = (imgs[0].get("rutaCompleta") if imgs else "") or o.get("cImagenCarrusel") or o.get("cImagenBusqueda") or ""
        url = o.get("cUrl") or ""
        if url and not url.startswith("http"):
            url = urljoin("https://www.plateanet.com/", url)
        zone = o.get("cZona") or ""
        found.append(_cand("plateanet", o.get("cNombre"), url, dates, sorted(times)[0][1] if times else "", o.get("cTeatroNombre"), "", None, None,
                           None, "ARS", o.get("cDescripcion") or "", img, [o.get("cCategoria"), o.get("cActores") and "artist:" + str(o.get("cActores"))[:60]],
                           genre_hint="theater", neighborhood_="" if norm(zone) == "caba" else zone))
    return found


# --- Venti: api.venti.live/api/home/events (paged)
def run_venti_api(src, window, log):
    cities = {norm(c) for c in CITY["venti"]["cities"]}
    found, page = [], 1
    while page <= 40:
        j = http_json(f"https://api.venti.live/api/home/events?limit=100&page={page}", origin="https://venti.live/eventos", log=log)
        if not isinstance(j, dict):
            break
        for e in j.get("events") or []:
            v = e.get("venue") or {}
            city = norm(((v.get("city") or {}).get("name")) if isinstance(v.get("city"), dict) else v.get("city"))
            lat, lng = v.get("latitude"), v.get("longitude")
            if lat is not None and not in_city(lat, lng):
                continue
            if lat is None and city and city not in cities:
                continue
            st, _ = parse_dt(e.get("startDate"))
            if not st or not in_window(st.date(), window):
                continue
            found.append(_cand("venti", e.get("name"), f"https://venti.live/evento/{e.get('urlName')}", [st.date().isoformat()], st.strftime("%H:%M"),
                               v.get("placeName") or e.get("placeName"), v.get("readableAddress") or "", lat, lng, None, "ARS", "",
                               e.get("bannerImg") or e.get("promoImg") or "", []))
        if page >= (j.get("totalPages") or 1):
            break
        page += 1
    return found


# --- Qué Hacemos catalog: api.quehacemos.com.ar/api/v1/events/ (their whole cartelera, every event carries the
# original ticket/venue page in "link"). We list the event under the ORIGINAL platform and link straight to it;
# Qué Hacemos is only where we found it (c["via"]).
QH_TYPES = {"recital": "music", "festival": "music", "cuarteto": "music", "teatro": "theater", "stand up": "theater",
            "fiesta": "nightlife", "electronica": "nightlife", "deporte": "outdoor", "arte": "galleries", "cine": "film",
            "charla": "film", "expo": "events", "gastronomia": "food"}
SOURCE_NAMES = {"passline.com": "passline", "alternativateatral.com": "alternativa", "plateanet.com": "plateanet", "venti.com.ar": "venti",
                "venti.live": "venti", "alpogo.com": "alpogo", "livepass.com.ar": "livepass", "tuentrada.com": "tuentrada",
                "ticketek.com.ar": "ticketek", "etickets.com.ar": "etickets", "movistararena.com.ar": "movistararena",
                "allaccess.com.ar": "allaccess", "eventbrite.com.ar": "eventbrite", "eventbrite.com": "eventbrite", "bandsintown.com": "bandsintown",
                "qrticket.app": "qrticket", "articket.com.ar": "articket", "enigmatickets.com": "enigmatickets", "planout.ar": "planout",
                "getdogma.app": "dogma", "atrapalo.com.ar": "atrapalo", "agendade.com.ar": "agendade", "instagram.com": "instagram"}


def platform_of(url):
    h = urlparse(url or "").netloc.lower()
    for p in ("www.", "publico.", "ventas.", "entradas.", "tickets.", "m."):
        if h.startswith(p):
            h = h[len(p):]
    return SOURCE_NAMES.get(h) or (h.split(".")[0] if h else "")


def clean_link(u):
    u = (u or "").strip()
    if not u:
        return ""
    if not u.startswith("http"):
        u = "https://" + u.lstrip("/")
    p = urlparse(u)
    if "eventbrite" in p.netloc or "_gl=" in u or "fbclid=" in u or "utm_" in u:  # drop tracking, keep the page
        keep = [q for q in (p.query or "").split("&") if q and not re.match(r"(_gl|aff|utm_\w+|fbclid|gclid)=", q)]
        u = p._replace(query="&".join(keep), fragment="").geturl()
    return u


def _qh_price(e):
    p = e.get("min_price") if e.get("min_price") is not None else e.get("price")
    try:
        return float(p) if p is not None else (0.0 if e.get("price_type") in ("gratis", "free") else None)
    except (TypeError, ValueError):
        return None


def run_quehacemos_api(src, window, log):
    cfg = CITY.get("quehacemos") or {}
    if not cfg:
        return []
    base = "https://api.quehacemos.com.ar/api/v1/events/"
    rows, cur, n = [], None, 0
    for prov in cfg.get("provinces", []):
        cur, n = None, 0
        while n < 40:
            u = f"{base}?province={quote(prov)}&limit=500&paginated=1" + (f"&cursor={cur}" if cur else "")
            j = http_json(u, origin="https://www.quehacemos.com.ar/buenos-aires", log=log)
            if not isinstance(j, dict) or not isinstance(j.get("events"), list):
                log.append(f"quehacemos page {n}: no events")
                break
            rows += j["events"]
            cur, n = j.get("next_cursor"), n + 1
            if not cur:
                break
    log.append(f"quehacemos catalog rows {len(rows)}")
    by_link = {}
    for e in rows:
        link = clean_link(e.get("link") or e.get("ticket_url") or e.get("source_url"))
        if not link or "quehacemos" in link:
            continue
        if not in_city(e.get("latitude"), e.get("longitude"), " ".join(filter(None, [e.get("city"), e.get("venue"), e.get("address")]))):
            continue
        st, _ = parse_dt(e.get("date"))
        if not st or not in_window(st.date(), window):
            continue
        c = by_link.get(link)
        if not c:   # a lead: where to look, never what to show (details are read from the source page later)
            c = by_link[link] = {"source": "quehacemos", "title": htmlmod.unescape(str(e.get("title") or "")).strip(), "url": link, "src_link": link,
                                 "dates": [], "genre_hint": QH_TYPES.get(e.get("event_type")), "tags": [e.get("event_type")],
                                 "venue_hint": e.get("venue_name") or e.get("venue") or "", "address_hint": e.get("address") or "",
                                 "lat": None, "lng": None, "lead": True, "time": st.strftime("%H:%M"),
                                 "fb": {"venue": e.get("venue_name") or e.get("venue") or "", "address": e.get("address") or "",
                                        "lat": e.get("latitude"), "lng": e.get("longitude"), "price": _qh_price(e), "currency": "ARS",
                                        "desc_raw": txt(e.get("description") or "")[:600], "img": e.get("image_url") or "",
                                        "hood": "" if (e.get("city") or "") in ("Buenos Aires", "CABA", "Ca") else (e.get("city") or "")}}
        c["dates"] = sorted(set(c["dates"]) | {st.date().isoformat()})
    from collections import Counter
    log.append("leads by platform " + json.dumps(Counter(platform_of(c["url"]) for c in by_link.values()).most_common(15)))
    return list(by_link.values())


# ------------------------------------------------------------------ more platform feeds (added 2026-10-19, v15)
# Every one of these reads the platform's own structured data (JSON API or schema.org JSON-LD on its pages), so each
# listing carries the platform's own date and links to the platform's own event page: anyone can click through and check.

_PW = {"p": None, "b": None, "ctx": None}
_PW_EXEC = cf.ThreadPoolExecutor(1)   # Playwright's sync API belongs to one thread: every browser fetch runs on this one


def _browser_html(url, log=None, wait_ms=1500):
    """Page HTML as a real browser sees it (for sites that refuse plain requests). One shared Chromium per run."""
    try:
        return _PW_EXEC.submit(_browser_html_now, url, log, wait_ms).result(timeout=120)
    except Exception as ex:
        if log is not None:
            log.append(f"browser html {url[:70]} !! {str(ex)[:100]}")
        return None


def _pw_close():
    def close():
        try:
            if _PW["b"]:
                _PW["b"].close()
            if _PW["p"]:
                _PW["p"].stop()
        except Exception:
            pass
        _PW.update(p=None, b=None, ctx=None)
    try:
        _PW_EXEC.submit(close).result(timeout=30)
    except Exception:
        pass


def _browser_html_now(url, log=None, wait_ms=1500):
    if True:
        try:
            if _PW["ctx"] is None:
                ensure_proxy_ca(log if log is not None else [])
                from playwright.sync_api import sync_playwright
                _PW["p"] = sync_playwright().start()
                exe = "/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None
                kw = {"headless": True, "args": ["--no-sandbox"]}
                _PW["b"] = _PW["p"].chromium.launch(executable_path=exe, **kw) if exe else _PW["p"].chromium.launch(**kw)
                _PW["ctx"] = _PW["b"].new_context(user_agent=UA, locale="es-AR", ignore_https_errors=True)
            pg = _PW["ctx"].new_page()
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(wait_ms)
                return pg.content()
            finally:
                pg.close()
        except Exception as ex:
            if log is not None:
                log.append(f"browser html {url[:70]} !! {str(ex)[:100]}")
            return None


def fetch_html(url, log=None, browser_ok=True):
    """(html, final_url). Plain request first with browser-like headers; real browser when refused or empty."""
    try:
        r = get(url, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        if r.status_code == 200 and len(r.text) > 2000:
            return r.text, r.url
        if log is not None:
            log.append(f"{url[:80]} -> {r.status_code} {len(r.text)}b")
    except Exception as ex:
        if log is not None:
            log.append(f"{url[:80]} !! {str(ex)[:80]}")
    if browser_ok:
        h = _browser_html(url, log)
        if h:
            return h, url
    return None, url


def run_pages(src, window, log):
    """Listing pages -> JSON-LD/embedded JSON events, plus detail pages matched by `follow` (read the same way).
    urls may contain {p} with "pages": [first, last] to walk numbered pages."""
    urls = []
    for u in src["urls"]:
        if "{p}" in u:
            a, b = src.get("pages", [1, 3])
            urls += [u.replace("{p}", str(p)) for p in range(a, b + 1)]
        else:
            urls.append(u)
    found, follow = [], []
    for u in urls:
        html, final = fetch_html(u, log, src.get("browser", True))
        if not html:
            continue
        f, soup = from_html(html, final, src["name"], window)
        found += f
        if src.get("follow"):
            for x in links(soup, final, src["follow"]):
                if src.get("strip_query"):
                    x = x.split("?")[0]
                if x not in follow:
                    follow.append(x)
        if src.get("stop_empty") and not f and not (src.get("follow") and links(soup, final, src["follow"])):
            break  # numbered pages ran out
    log.append(f"listing pages {len(urls)} -> {len(found)} events, {len(follow)} detail links")
    follow = follow[: src.get("max_follow", 80)]

    def one(u):
        html, final = fetch_html(u, None, src.get("browser", True))
        return from_html(html, final, src["name"], window)[0] if html else []
    if follow:
        with cf.ThreadPoolExecutor(6) as ex:
            for f in ex.map(one, follow):
                found += f
    return found


# --- Ticketek: its CMS API (category pages list every show; each show's node carries schema.org events per performance)
def run_ticketek_api(src, window, log):
    B = "https://prod-cms-api.ticketek.com.ar/api/1.0/node%3Fpath%3D"
    shows = []
    for c in ("home", "musica", "teatro", "familia", "deportes", "especiales"):
        j = http_json(B + c, origin="https://www.ticketek.com.ar/", log=log)
        for m in re.finditer(r'"url":\s*"([a-z0-9\-]+(?:/[a-z0-9\-]+)?)"', json.dumps(j or {})):
            if m[1] not in shows:
                shows.append(m[1])
    log.append(f"ticketek shows {len(shows)}")
    found = []

    def one(path):
        j = http_json(B + path.replace("/", "--"), origin="https://www.ticketek.com.ar/", log=None)
        if not isinstance(j, dict):
            return []
        out = from_json(j.get("google_structured_data") or [], "https://www.ticketek.com.ar/" + path, "ticketek", window)
        for c in out:
            c["url"] = "https://www.ticketek.com.ar/" + path
            if j.get("category"):
                c["tags"] = (c.get("tags") or []) + [str(j.get("category"))[:40]]
        return out
    with cf.ThreadPoolExecutor(6) as ex:
        for f in ex.map(one, shows[:400]):
            found += f
    return found


# --- EntradaUno: the whole cartelera in one JSON file (shows, performances, venues with their zone)
def run_entradauno_json(src, window, log):
    j = http_json("https://s3.sa-east-1.amazonaws.com/contenido.general.entradauno/cache/12/cartelera.json",
                  origin="https://www.entradauno.com/", log=log)
    try:
        c = j["oData"]["oCartelera"]
    except (TypeError, KeyError):
        return []
    zones = {norm(z) for z in CITY.get("entradauno", {}).get("zones", [])}
    est = {e["idEstablecimiento"]: e for e in c.get("listaEstablecimiento") or []}
    cats = {x.get("idEspectaculoCartelCategoria"): x.get("cNombre") for x in c.get("listaEspectaculoCartelCategoria") or []}
    found = []
    for s in c.get("listaEspectaculoCartel") or []:
        by_place = defaultdict(list)
        for f in s.get("oFuncionLista") or []:
            e = est.get(f.get("idEstablecimiento")) or {}
            if norm(e.get("cZona")) not in zones:
                continue
            st, has_t = parse_dt(f.get("dFuncion"))
            if st and in_window(st.date(), window):
                by_place[f.get("idEstablecimiento")].append(st)
        for pid, sts in by_place.items():
            e = est.get(pid) or {}
            url = s.get("cWebUri") or (f"https://www.entradauno.com/landing/{s.get('cSeo')}?idEspectaculoCartel={s.get('idEspectaculoCartel')}"
                                       f"&cHashValidacion={s.get('cHashValidacion')}")
            img = next((i.get("cUri") for i in s.get("listaImagen") or [] if i.get("cUri")), "")
            found.append(_cand("entradauno", s.get("cNombre"), url, [x.date().isoformat() for x in sts], min(sts).strftime("%H:%M"),
                               e.get("cNombre"), e.get("cDomicilio"), None, None, s.get("fPrecioDesde"), "ARS", s.get("cDescripcion") or "",
                               img, [cats.get(k) for k in s.get("listaIdEspectaculoCartelCategoria") or []],
                               neighborhood_="" if norm(e.get("cZona")) in ("caba", "buenos aires") else e.get("cZona") or ""))
    return found


# --- Buenos Aires city agenda (Linda): the city government's own event API, with dates, hours, coordinates
def run_linda_api(src, window, log):
    found, seen = [], set()
    days = {d.isoformat() for d in _window_days(window)}
    for page in range(1, 8):
        j = http_json(f"https://linda.buenosaires.gob.ar/api/frontend/events/filter?limit=200&page={page}",
                      origin="https://linda.buenosaires.gob.ar/agenda", log=log)
        if not isinstance(j, dict):
            break
        for e in j.get("events") or []:
            if e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            ds = sorted({str(x)[:10] for x in (e.get("fechasDelEvento") or [])} & days)
            run = None
            if not ds:
                a, b = str(e.get("fechaInicio") or "")[:10], str(e.get("fechaFin") or "")[:10]
                if a and b and a <= (TODAY + dt.timedelta(days=window)).isoformat() and b >= TODAY.isoformat() and a != b:
                    run = (max(a, TODAY.isoformat()), b)
                elif a in days:
                    ds = [a]
            if not ds and not run:
                continue
            u = e.get("ubicacion") or {}
            hrs = [h.get("hora") for h in e.get("horarios") or [] if h.get("hora")]
            free = e.get("acceso") == "sin_costo"
            img = e.get("imageUrl") or ""
            found.append(_cand("linda", e.get("title"), f"https://linda.buenosaires.gob.ar/eventos/{e.get('slug')}", ds,
                               (hrs[0] if hrs else (e.get("horario") or ""))[:5], u.get("titulo") or "", e.get("direccion") or u.get("direccion") or "",
                               u.get("latitud"), u.get("longitud"), 0.0 if free else None, "ARS", e.get("description") or "",
                               img if not img or img.startswith("http") else "https://" + img.lstrip("/"),
                               list(e.get("etiquetas") or []) + list(e.get("agrupadores") or [])[:4], run,
                               neighborhood_=(e.get("barrio") or "").replace("_", " ").title()))
        pg = j.get("pagination") or {}
        if page >= (pg.get("totalPages") or 1):
            break
    return found


# --- Teatro Colón: its monthly calendar page (one block per performance day)
def run_teatrocolon(src, window, log):
    found = []
    m0 = dt.date(TODAY.year, TODAY.month, 1)
    for k in range(3):
        y, mo = (m0.year + (m0.month - 1 + k) // 12), (m0.month - 1 + k) % 12 + 1
        html, final = fetch_html(f"https://teatrocolon.org.ar/calendario/?a={y}&mes={mo}", log)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for it in soup.select("div.calendar-item[id^=day-]"):
            m = re.match(r"day-(\d+)-(\d+)", it.get("id", ""))
            if not m:
                continue
            try:
                d = dt.date(y, int(m[2]), int(m[1]))
            except ValueError:
                continue
            if not in_window(d, window):
                continue
            hh = re.search(r"(\d{1,2})[.:](\d{2})", it.select_one(".day-hour").get_text(" ") if it.select_one(".day-hour") else "")
            body = it.select_one(".event-content") or it
            cat = body.select_one(".category")
            ttl = body.select_one(".colon-serif-regular") or body.find(["h2", "h3"])
            info = next((a["href"] for a in it.find_all("a", href=True) if "teatrocolon.org.ar" in a["href"] and "/produccion/" in a["href"]), None)
            img = it.find("img")
            title = (ttl.get_text(" ", strip=True) if ttl else "").strip()
            if not title:
                continue
            found.append(_cand("teatrocolon", title, info or final, [d.isoformat()], f"{int(hh[1]):02d}:{hh[2]}" if hh else "",
                               "Teatro Colón", "Cerrito 628, CABA", -34.6011, -58.3830, None, "ARS",
                               body.get_text(" ", strip=True)[:400], img.get("src", "") if img else "",
                               [cat.get_text(" ", strip=True) if cat else "", "música clásica"]))
    by = {}
    for c in found:  # one listing per production with all its days
        k = (c["title"], c["url"])
        if k in by:
            by[k]["dates"] = sorted(set(by[k]["dates"]) | set(c["dates"]))
        else:
            by[k] = c
    return list(by.values())


# ------------------------------------------------------------------ source resolver (added 2026-10-27, v16)
# Owner's rule: information can be gathered anywhere, but EVENT DETAILS COME FROM THE SOURCE.
# Qué Hacemos, AllEvents, blogs and roundup articles are only used to FIND events ("leads"). For every lead the
# harvester opens the original publisher's page (ticket page, venue page, organizer page) and reads title, dates,
# time, venue, price, picture and description from THAT page: its schema.org data first, then its own text.
# A lead whose source page can't be read, or doesn't state a date in the window, is dropped (and counted).
RESOLVE_SOURCES = {"quehacemos", "allevents", "indiehoy", "cartelera_teatro"}   # discovery only, never details
HOST_ALIAS = {"venti.com.ar": "venti.live", "publico.alternativateatral.com": "alternativateatral.com", "m.cinesargentinos.com.ar": "cinesargentinos.com.ar",
              "home.passline.com": "passline.com", "luma.com": "lu.ma"}
KEEP_QUERY_HOSTS = ("entradauno.com", "comprar.tuentrada.com", "plateanet.com")
JS_ONLY_HOSTS = ("movistararena.com.ar", "plateanet.com")        # their event data only appears after the page runs
SKIP_LINK_HOSTS = ("facebook.com", "twitter.com", "x.com", "whatsapp.com", "wa.me", "t.me", "telegram.me", "pinterest.", "linkedin.com",
                   "google.", "goo.gl", "maps.app", "youtube.com", "youtu.be", "spotify.com", "tiktok.com", "mailto:", "allevents.in",
                   "quehacemos.com.ar", "apple.com", "play.google", "addtoany", "sharethis", "doubleclick")
TICKET_HOST_HINT = re.compile(r"passline|alternativa|plateanet|venti|alpogo|livepass|tuentrada|ticketek|articket|qrticket|etickets|movistararena|allaccess|"
                              r"allmusicparties|enigmatickets|planout|getdogma|atrapalo|flashpass|alltickets|ticketsati|entradauno|eventbrite|lu\.ma|meetup|"
                              r"ra\.co|bombo|fever|catpass|wonder-ticket|latangente|autoentrada|deportick|calendariodecarreras|sympla|ticketmaster|ticket", re.I)
LINK_TEXT_HINT = re.compile(r"entradas|tickets?|comprar|reserv|inscrip|m[aá]s info|sitio|web oficial|ver evento|link|registr", re.I)


def norm_url(u):
    """Comparable form of an event link: lower host without www/aliases, path without trailing slash, query only where it identifies the event."""
    try:
        p = urlparse(u if str(u).startswith("http") else "https://" + str(u).lstrip("/"))
    except Exception:
        return ""
    h = p.netloc.lower()
    h = h[4:] if h.startswith("www.") else h
    h = HOST_ALIAS.get(h, h)
    q = ("?" + p.query) if p.query and any(k in h for k in KEEP_QUERY_HOSTS) else ""
    return h + p.path.rstrip("/").lower() + q


# --- dates written in Spanish/English page text ("Miércoles 28 de Octubre", "VIE 30 OCT", "29/10/2026", "2026-10-29")
_MON = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "aug": 8, "sep": 9, "set": 9,
        "oct": 10, "nov": 11, "dic": 12, "dec": 12}


def _mk_date(y, m, d):
    try:
        if y is None:  # no year written: this year, or next year when that date has already passed by more than 2 months
            x = dt.date(TODAY.year, m, d)
            return dt.date(TODAY.year + 1, m, d) if (TODAY - x).days > 60 else x
        y = int(y)
        y = y + 2000 if y < 100 else y
        return dt.date(y, m, d)
    except ValueError:
        return None


def text_dates(t, window):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    out = set()
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        out.add(_mk_date(m[1], int(m[2]), int(m[3])))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t):
        if 1 <= int(m[2]) <= 12:
            out.add(_mk_date(m[3], int(m[2]), int(m[1])))
    for m in re.finditer(r"\b(\d{1,2})(?:\s+de)?\s+(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic|jan|apr|aug|dec)[a-z]*\.?(?:\s+(?:de\s+)?(\d{4}))?", t):
        out.add(_mk_date(m[3], _MON[m[2]], int(m[1])))
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?(?:\s+(\d{4}))?", t):
        out.add(_mk_date(m[3], _MON[m[1]], int(m[2])))
    return sorted(d.isoformat() for d in out if d and in_window(d, window))


def text_time(t):
    t = (t or "").lower()
    m = re.search(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*(?:hs?\b|h\b|horas|am|pm)?", t)
    if m and not re.match(r"\d{1,2}[:.]\d{2}[:.]\d{2}", t[m.start():m.start() + 9]):
        return f"{int(m[1]):02d}:{m[2]}"
    m = re.search(r"\b([01]?\d|2[0-3])\s*(?:hs|h)\b", t)
    return f"{int(m[1]):02d}:00" if m else ""


def text_price(t):
    tl = (t or "").lower()
    if re.search(r"\b(gratis|entrada libre|sin cargo|free entry|gratuit[oa])\b", tl):
        return 0.0
    m = re.search(r"\$\s?(\d{1,3}(?:[.,]\d{3})+|\d{3,7})(?![\d.,])", tl)
    if m:
        try:
            return float(m[1].replace(".", "").replace(",", ""))
        except ValueError:
            return None
    return None


def _toks(s):
    return {w for w in norm(s).split() if len(w) > 2}


def _title_match(a, b):
    ta, tb = _toks(a), _toks(b)
    return len(ta & tb) / max(1, min(len(ta), len(tb))) if ta and tb else 0.0


def _clean_title(t, hint=""):
    t = htmlmod.unescape(t or "").strip()
    t = re.sub(r"^(compr[aá]\s+tus\s+)?entradas?\s+(para|de)\s+", "", t, flags=re.I)
    t = re.sub(r"^(consegu[ií]\s+tus\s+entradas\s+para\s+)", "", t, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[|»·–—]\s+|\s+-\s+", t) if p.strip()]
    if len(parts) > 1:
        brand = re.compile(r"passline|ticketek|tuentrada|qrticket|etickets|plateanet|movistar arena|articket|venti|alpogo|livepass|allaccess|dogma|eventbrite|"
                           r"fever|bombo|alternativa|^inicio$|^home$|entradas online", re.I)
        parts = [p for p in parts if not brand.search(p)] or parts
        t = max(parts, key=lambda p: (_title_match(p, hint), len(p))) if hint else parts[0]
    return t[:120]


def _meta(soup, *names):
    for n in names:
        m = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
        if m and m.get("content"):
            return m["content"].strip()
    return ""


def _main_text(soup):
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg"]):
        tag.decompose()
    box = soup.find("main") or soup.find("article") or soup.body or soup
    return box.get_text("\n", strip=True)[:20000]


def parse_source_page(html, final, platform, window, hint):
    """Candidates for ONE event read from its own page. hint = what the lead said (title/venue/address), used only to pick the
    right event on multi-event pages and to confirm a venue the page itself names."""
    cands, soup = from_html(html, final, platform, window)
    h_title = hint.get("title") or ""
    if cands:
        best = sorted(cands, key=lambda c: (-_title_match(c["title"], h_title), c["dates"][:1]))
        pick = [c for c in best if _title_match(c["title"], h_title) >= 0.34] or ([best[0]] if len(cands) == 1 or not h_title else [])
        out = []
        for c in pick[:3]:
            same_host = norm_url(c.get("url", "")).split("/")[0] == norm_url(final).split("/")[0]
            c = dict(c, source=platform, url=c["url"] if same_host else final)
            c["img_hint"] = c.get("img_hint") or _meta(soup, "og:image")
            c["detail_source"] = "schema.org"
            out.append(c)
        if out:
            return out
    # no usable structured data: the page's own words
    soup = BeautifulSoup(html, "lxml")
    title = _clean_title(_meta(soup, "og:title") or (soup.title.string if soup.title else ""), h_title)
    if not title or (h_title and _title_match(title, h_title) < 0.25 and _title_match(_meta(soup, "og:description"), h_title) < 0.34):
        return []                                   # page isn't about this event (moved, expired, home page)
    ogd = _meta(soup, "og:description", "description")
    body = _main_text(BeautifulSoup(html, "lxml"))
    dates = text_dates(ogd + "\n" + body, window)
    if not dates:
        return []
    if len(dates) > 40:                             # a calendar of many things: too vague to trust
        return []
    m = re.search(r"\s+en\s+(.{3,60})$", _meta(soup, "og:title") or "")
    venue = ""
    if m and _title_match(m[1], hint.get("venue") or m[1]) > 0:
        venue = m[1].strip()
    blob = norm(body)
    if not venue and hint.get("venue") and norm(hint["venue"]) and norm(hint["venue"]) in blob:
        venue = hint["venue"]                       # named on the source page itself
    if not venue:                                   # "Lugar: Club Tri" / "Dirección: ..." written on the page
        m = re.search(r"(?im)^\s*(?:lugar|sede|venue|d[oó]nde|espacio|sala)\s*[:\-]\s*(.{3,70})$", body)
        if m:
            venue = m[1].strip()
    addr = hint["address"] if hint.get("address") and norm(hint["address"]).split(" ")[0] in blob and norm(hint["address"]) in blob else ""
    c = _cand(platform, title, final, dates, text_time(ogd + "\n" + body), venue, addr, None, None, text_price(body), "ARS", ogd,
              _meta(soup, "og:image"), [])
    c["detail_source"] = "page text"
    return [c]


def outbound_source_links(html, base):
    """Links on an article/aggregator page that lead to the original publisher, best first."""
    soup = BeautifulSoup(html, "lxml")
    bh = urlparse(base).netloc.lower().replace("www.", "")
    scored = []
    for a in soup.find_all("a", href=True):
        u = urljoin(base, a["href"]).split("#")[0]
        if not u.startswith("http"):
            continue
        h = urlparse(u).netloc.lower().replace("www.", "")
        if not h or h == bh or any(s in u.lower() for s in SKIP_LINK_HOSTS) or len(urlparse(u).path) < 3:
            continue
        txt_ = a.get_text(" ", strip=True)
        s = (3 if TICKET_HOST_HINT.search(h) else 0) + (2 if LINK_TEXT_HINT.search(txt_) else 0) + (1 if re.search(r"\d", urlparse(u).path) else 0)
        scored.append((s, u))
    seen, out = set(), []
    for s, u in sorted(scored, key=lambda x: -x[0]):
        k = norm_url(u)
        if k not in seen and s > 0:
            seen.add(k)
            out.append(u)
    return out[:4]


def _existing_index(path="db/events"):
    idx = {}
    for f in glob.glob(os.path.join(path, "*.json")):
        try:
            d = json.load(open(f))
            d = d.get("data", d) if isinstance(d.get("data"), dict) else d
            idx[norm_url(d.get("url") or "")] = set(d.get("dates") or [])
        except Exception:
            pass
    idx.pop("", None)
    return idx


def _disc(c):
    """What the discovery site (Qué Hacemos, AllEvents, a blog) said about the event."""
    if c.get("fb"):
        return c["fb"]
    return {"venue": c.get("venue") or "", "address": c.get("address") or "", "lat": c.get("lat"), "lng": c.get("lng"), "price": c.get("price"),
            "currency": c.get("currency") or "ARS", "desc_raw": c.get("desc_raw") or "", "img": c.get("img_hint") or "", "hood": c.get("hood") or ""}


def _fill_gaps(o, c):
    """The original page wins; the discovery site only fills fields the page left empty. Its picture is only a backup."""
    d = _disc(c)
    for k in ("venue", "address", "lat", "lng", "desc_raw", "hood"):
        if not o.get(k) and d.get(k):
            o[k] = d[k]
    if o.get("price") is None and d.get("price") is not None:
        o["price"], o["currency"] = d["price"], d.get("currency") or "ARS"
    if not o.get("time") and c.get("time"):
        o["time"] = c["time"]
    if not o.get("img_hint") and d.get("img"):
        o["img_backup"] = d["img"]


def _from_discovery(c, link, name):
    d = _disc(c)
    if not c.get("title") or not (c.get("dates") or c.get("run")):
        return None
    o = {"source": platform_of(link) if link else c.get("source"), "title": c["title"], "url": link or c.get("url"), "page": link or c.get("url"),
         "dates": c.get("dates") or [], "run": c.get("run"), "time": c.get("time") or "", "venue": d["venue"], "address": d["address"],
         "lat": d["lat"], "lng": d["lng"], "price": d["price"], "currency": d.get("currency") or "ARS", "desc_raw": d["desc_raw"],
         "img_hint": "", "img_backup": d["img"], "tags": [t for t in c.get("tags") or [] if t][:14], "genre_hint": c.get("genre_hint"),
         "via": name, "found_on": c.get("url") if link and c.get("url") != link else "", "detail_source": "discovery site"}
    if d.get("hood"):
        o["hood"] = d["hood"]
    return o


def resolve_leads(name, leads, covered, existing, window, log, cap=1300, browser_cap=120):
    """Turn discovery leads into candidates whose details come from the source page."""
    from collections import Counter
    stats = Counter()
    todo = []
    for c in leads:
        link = c.get("src_link") or c.get("url")
        k = norm_url(link)
        if k in covered:
            stats["covered by a direct feed"] += 1
            continue
        if k in existing and set(c.get("dates") or []) <= existing[k]:
            stats["already in the app"] += 1
            continue
        todo.append(c)
    todo.sort(key=lambda c: (c.get("dates") or ["9"])[0])
    if len(todo) > cap:
        stats["over today's cap (next run)"] += len(todo) - cap
        todo = todo[:cap]
    browser_left = [browser_cap]
    lock = threading.Lock()

    def one(c):
        hint = {"title": c.get("title"), "venue": c.get("venue_hint") or "", "address": c.get("address_hint") or ""}
        links = [c.get("src_link")] if c.get("src_link") else []
        if not links:                               # an aggregator/blog page: find the publisher's link on it
            html, final = fetch_html(c["url"], None, browser_ok=False)
            if not html:
                return "kept as found (page unreadable)", [x for x in [_from_discovery(c, None, name)] if x]
            links = outbound_source_links(html, final)
            if not links:
                return "kept as found (no original link)", [x for x in [_from_discovery(c, None, name)] if x]
        for link in links:
            plat = platform_of(link) or "web"
            host = norm_url(link).split("/")[0]
            html, final = (None, link) if any(h in host for h in JS_ONLY_HOSTS) else fetch_html(link, None, browser_ok=False)
            out = parse_source_page(html, final, plat, window, hint) if html else []
            if not out:
                with lock:
                    use_browser = browser_left[0] > 0
                    browser_left[0] -= 1
                if use_browser:
                    h2 = _browser_html(link, None)
                    out = parse_source_page(h2, link, plat, window, hint) if h2 else []
            if out:
                for o in out:
                    o["via"] = name
                    o["found_on"] = c.get("found_on") or (c.get("url") if c.get("url") != link else "")
                    o["genre_hint"] = o.get("genre_hint") or c.get("genre_hint")
                    o["tags"] = list(dict.fromkeys((o.get("tags") or []) + [t for t in c.get("tags") or [] if t and ":" not in t]))[:14]
                    _fill_gaps(o, c)
                    if not in_city(o.get("lat"), o.get("lng"), " ".join(str(o.get(k) or "") for k in ("address", "venue"))):
                        return "source page is outside the city", []
                return "details from the source page", out
        # the original page didn't give us the details: keep what the discovery site said, but link to the original
        fb = _from_discovery(c, links[0] if links else None, name)
        if fb is None:
            return "dropped: nothing usable", []
        return ("kept with the discovery site's details, linked to the original" if links else "kept as found (no original link)"), [fb]
    found = []
    with cf.ThreadPoolExecutor(8) as ex:
        for why, out in ex.map(one, todo):
            stats[why] += 1
            found += out
    log.append(f"{name} leads {len(leads)}: " + json.dumps(dict(stats), ensure_ascii=False))
    return found


# Platform categories mapped straight to app genres (checked before the keyword rules)
PLATFORM_CATS = [
    ("tango", r"^tango$|milonga|danza|ballet|baile|flamenco|contempor[aá]ne"), ("theater", r"teatro|comedia|stand ?up|humor|musical|drama|unipersonal|improvisaci|clown|t[ií]teres|circo|performance|vari[eé]t|infantil|esc[eé]nicas"),
    ("nightlife", r"^fiesta|electr[oó]nica|^party|boliche|club nocturno"), ("film", r"^cine|charla|literatura|^talk"),
    ("galleries", r"muestra|exposici|arte & cultura|artes visuales"), ("food", r"gastron|vino|enoturismo|cerveza|food"),
    ("classes", r"taller|curso|clase|workshop|seminario"), ("events", r"conferencia|congreso|networking|exposiciones y conferencias|negocios"),
    ("outdoor", r"deporte|running|aventura|aire libre"), ("markets", r"^feria|mercado"),
    ("music", r"recital|concierto|m[uú]sica|jazz|rock|pop|folklore|cl[aá]sica|show en vivo|live music|^show$|conciertos|blues|reggae|cumbia|trap|rap"),
]


KINDS = {"allevents": run_allevents, "pages": run_pages, "browser": run_browser, "ra": run_ra, "luma": run_luma, "organizers": run_organizers, "instagram": run_instagram,
         "passline_api": run_passline_api, "alternativa_json": run_alternativa_json, "alpogo_api": run_alpogo_api,
         "plateanet_api": run_plateanet_api, "venti_api": run_venti_api, "quehacemos_api": run_quehacemos_api,
         "ticketek_api": run_ticketek_api, "entradauno_json": run_entradauno_json, "linda_api": run_linda_api, "teatrocolon": run_teatrocolon}


def harvest(args):
    os.makedirs(OUT, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    srcs = [s for s in SOURCES if (not only or s["name"] in only) and not s.get("retired")]
    if not only:  # every registry platform is checked every day (owner asked, 2026-10-06)
        print(f"registry platforms today: {sum(1 for s in srcs if s.get('tier') == 'registry')}")
    report, cands = {}, []

    def one(src):
        log, t0 = [], time.time()
        try:
            f = KINDS[src["kind"]](src, max(args.window, FUTURE_DAYS), log)
        except Exception as ex:
            f = []
            log.append(f"FAILED {type(ex).__name__}: {str(ex)[:300]}")
        kept = [c for c in f if in_city(c.get("lat"), c.get("lng"), " ".join(str(c.get(k) or "") for k in ("address", "venue", "hood")))]
        if len(kept) < len(f):
            log.append(f"outside {CITY['name']}: {len(f) - len(kept)} dropped")
        f = kept
        for c in f:
            if not c.get("genre_hint"):
                c["genre_hint"] = src.get("genre")
        return src["name"], f, {"count": len(f), "secs": round(time.time() - t0), "log": log[-40:]}

    browser = [s for s in srcs if s["kind"] == "browser"]
    other = [s for s in srcs if s["kind"] != "browser"]
    with cf.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(one, other))
    for s in browser:  # one browser at a time keeps memory in check
        results.append(one(s))
    if only and os.path.exists(f"{OUT}/candidates.json"):  # re-running some sources keeps the others' results
        cands = [c for c in json.load(open(f"{OUT}/candidates.json")) if c.get("via", c["source"]) not in only]
        if os.path.exists(f"{OUT}/report.json"):
            report = {k: v for k, v in json.load(open(f"{OUT}/report.json")).items() if k not in only}
    # discovery sources (Qué Hacemos, AllEvents, blogs) -> details from each event's own source page
    covered = set()
    for name, f, rep in results:
        if name in RESOLVE_SOURCES:
            continue
        for c in f:
            covered.add(norm_url(c.get("url") or ""))
            covered.update(norm_url(t[8:]) for t in c.get("tags") or [] if isinstance(t, str) and t.startswith("tickets:"))
    if only and os.path.exists(f"{OUT}/candidates.json"):
        covered.update(norm_url(c.get("url") or "") for c in cands)
    covered.discard("")
    existing = _existing_index()
    for i, (name, f, rep) in enumerate(results):
        if name in RESOLVE_SOURCES and f:
            t0 = time.time()
            f2 = resolve_leads(name, f, covered, existing, max(args.window, FUTURE_DAYS), rep["log"])
            f2 = [c for c in f2 if norm_url(c["url"]) not in covered]
            covered.update(norm_url(c["url"]) for c in f2)
            for c in f2:
                c.setdefault("genre_hint", None)
            rep["secs"] += round(time.time() - t0)
            rep["leads"] = len(f)
            results[i] = (name, f2, rep)
    edge = (TODAY + dt.timedelta(days=args.window)).isoformat()
    far = []
    for name, f, rep in results:
        near_n = 0
        for c in f:
            near_d = [d for d in c.get("dates") or [] if d <= edge]
            far_d = [d for d in c.get("dates") or [] if d > edge]
            run = c.get("run")
            near_run = bool(run) and run[0] <= edge
            if near_d or near_run:
                cands.append(dict(c, dates=near_d))
                near_n += 1
            if far_d and not (near_d or near_run):
                cands.append(dict(c, dates=far_d, run=None, far=True))
            elif far_d:
                far.append(dict(c, dates=far_d, run=None, far=True))
        rep["count"], rep["future"] = near_n, sum(1 for c in f if any(d > edge for d in c.get("dates") or []))
        report[name] = rep
        print(f"{name:18s} {rep['count']:5d}  +{rep['future']:4d} later  ({rep['secs']}s)")
    _pw_close()
    cands += far  # far-off dates of events that also have a date this week: separate entries, same id, merged in build
    json.dump(cands, open(f"{OUT}/candidates.json", "w"), ensure_ascii=False, indent=1)
    json.dump(report, open(f"{OUT}/report.json", "w"), ensure_ascii=False, indent=1)
    print("total candidates", len(cands))


# ------------------------------------------------------------------ build listings
def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(en|el|la|los|las|de|del|y|the|at|live|en vivo|presenta|presents)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


RULES = [  # (genre, sub, regex) checked in order against title+tags+venue (+desc)
    ("tango", None, r"\bmilonga|\btango|pr[aá]ctica de tango|orquesta t[ií]pica"),
    ("events", None, r"networking|meetup|startup|emprendedor|hackat|summit|congreso|conferencia|expo(?!sici)|feria de empleo|job fair|founders|\bia\b|inteligencia artificial|\bai\b|blockchain|crypto|product manage|developers?|devs\b|tech\b|pitch|afterwork|business|negocios|coworking"),
    ("classes", None, r"\btaller|workshop|\bclase|curso|seminario|masterclass|capacitaci|aprend[eé]"),
    ("markets", None, r"\bferia\b|mercado|market|bazar|vintage|flea|pulgas|garage sale|feria de dise"),
    ("food", None, r"degustaci|\bcata\b|vinos?\b|wine|cerveza|beer|gastron|brunch|cena\b|dinner|food|cocina|asado|parrilla|caf[eé] de especialidad|cocktail|c[oó]ctel|vermut"),
    ("film", None, r"\bcine\b|cineclub|pel[ií]cula|proyecci[oó]n|documental|screening|festival de cine|film\b|presentaci[oó]n (del |de )?libro|\blibro|lectura|poes[ií]a|charla|talk\b|literari"),
    ("museums", None, r"\bmuseo|visita guiada|recorrido guiado|noche de (los )?museos"),
    ("galleries", None, r"muestra|exposici[oó]n|exhibi|galer[ií]a|inauguraci[oó]n|\barte\b|art\b|vernissage|instalaci[oó]n|fotograf[ií]a"),
    ("outdoor", None, r"\bbici|caminata|trekking|running|marat[oó]n|\b10k|\b21k|yoga (en|al) (el )?parque|kayak|remo|paseo|excursi|partido|f[uú]tbol|\bstadium|rugby|b[aá]squet|tenis\b|carrera\b"),
    ("theater", None, r"\bteatro\b|\bobra\b|stand ?up|comedia|impro|musical\b|\bdanza|ballet|circo|clown|monólogo|mon[oó]logo|dramaturg|func[ií]on|t[ií]teres|performance"),
    ("nightlife", None, r"\bfiesta|party|\bdjs?\b|techno|\bhouse\b|\brave\b|club\b|\bafter\b|boliche|b2b|open air|electr[oó]nica|sunset|drag|karaoke|bailable"),
    ("music", None, r"recital|en vivo|concierto|concert|\bbanda|\bjazz|\brock|\bshow\b|orquesta|cantante|ac[uú]stico|gira|tour|sinf[oó]nic|\b[oó]pera\b|coro\b|piano|guitarra|folklore|folclore|pe[ñn]a|cumbia|trap|rap\b|reggae|blues|metal|punk|indie|pop\b|cuarteto|m[uú]sica"),
]
SUBS = [("jazz", r"jazz|swing|bebop"), ("blues", r"blues"), ("classical", r"orquesta sinf|sinf[oó]nic|filarm|[oó]pera|c[aá]mara|cl[aá]sic|piano solo|recital de piano|barroc|mozart|beethoven|bach|coro\b|lírica|l[ií]rica|teatro col[oó]n"),
        ("folk", r"folklore|folclore|pe[ñn]a|chacarera|zamba\b|criollo"), ("cumbia", r"cumbia|cuarteto"),
        ("hiphop", r"hip ?hop|\brap\b|\btrap\b|freestyle|batalla de gallos"), ("electronic", r"techno|house|electr[oó]nica|\bdj|synth|ambient|trance|\bedm\b"),
        ("experimental", r"experimental|noise|improvisaci[oó]n libre|avant"), ("world", r"reggae|afro|samba|bossa|brasil|flamenco|salsa|world|klezmer|balkan|candombe|m[uú]sica (de )?(india|arabe|africana)"),
        ("indie", r"\bindie|alternativ|shoegaze|dream pop|lo-?fi"), ("rock", r"\brock|metal|punk|grunge|hardcore|stoner"), ("pop", r"\bpop\b|reggaeton|urbano|balada")]
BARRIOS = ["Palermo", "Villa Crespo", "Almagro", "San Telmo", "Recoleta", "Belgrano", "Colegiales", "Chacarita", "Caballito", "Boedo", "Balvanera", "Abasto", "Monserrat",
           "San Nicolás", "Microcentro", "Retiro", "La Boca", "Barracas", "Núñez", "Saavedra", "Villa Urquiza", "Coghlan", "Villa Ortúzar", "Parque Patricios", "Puerto Madero",
           "Constitución", "San Cristóbal", "Flores", "Floresta", "Villa del Parque", "Agronomía", "Parque Chas", "Villa Devoto", "Mataderos", "Liniers", "Once", "Congreso"]
EB_GENRE = {"music": "music", "nightlife": "nightlife", "arts": "theater", "food-and-drink": "food", "business": "events", "science-and-tech": "events",
            "hobbies": "classes", "film-and-media": "film", "sports-and-fitness": "outdoor", "community": "events"}


def classify(c):
    blob = " ".join([c["title"], " ".join(c.get("tags", []))]).lower()
    blob2 = blob + " " + c.get("desc_raw", "").lower()[:300] + " " + c.get("venue", "").lower()
    genre = None
    hint = c.get("genre_hint")
    cats = " | ".join(t for t in c.get("tags", []) if t and ":" not in t).lower()
    pc = next((g for g, rx in PLATFORM_CATS if cats and any(re.search(rx, t.strip()) for t in cats.split("|"))), None) if cats else None
    if c["source"] == "eventbrite":
        m = re.search(r"/buenos-aires/([a-z\-]+)--events", c.get("page", ""))
        hint = EB_GENRE.get(m[1]) if m else hint
    for text in (blob, blob + " " + c.get("desc_raw", "").lower()[:300], blob2):
        for g, _, rx in RULES:
            if re.search(rx, text):
                genre = g; break
        if genre:
            break
    if pc and not re.search(r"\bmilonga|\btango", blob):
        genre, hint = pc, None
    if hint in ("tango", "theater") or (hint and not genre):
        genre = hint if hint != "theater" or genre in (None, "music", "nightlife") else genre
    if c["source"] == "ra":
        late = c.get("time", "") >= "22:00" or c.get("time", "") < "06:00"
        genre = "nightlife" if late or genre != "music" else "music"
    genre = genre or "events"  # unknown: the review step relabels it (was "music", which mislabeled plays and talks)
    sub = None
    if genre == "music":
        sub = next((s for s, rx in SUBS if re.search(rx, blob2)), "other")
    if genre == "events":
        sub = ("networking" if re.search(r"networking|afterwork", blob2) else "conference" if re.search(r"conferencia|summit|congreso", blob2)
               else "trade-fair" if re.search(r"expo|feria", blob2) else "job-fair" if re.search(r"empleo|job", blob2) else "meetup")
    return genre, sub


def neighborhood(c):
    a = f"{c.get('address', '')} {c.get('venue', '')}"
    for b in BARRIOS:
        if b.lower() in a.lower():
            return {"Once": "Balvanera", "Abasto": "Almagro"}.get(b, b)
    return ""


def fx_rate():
    for u in ("https://dolarapi.com/v1/dolares/blue", "https://api.bluelytics.com.ar/v2/latest"):
        try:
            js = get(u, timeout=10).json()
            v = js.get("venta") or (js.get("blue") or {}).get("value_sell")
            if v:
                return float(v)
        except Exception:
            pass
    return None


def load_existing(path):
    ex = []
    for f in glob.glob(os.path.join(path, "*.json")):
        d = json.load(open(f))
        doc = d.get("data", d)
        doc["_version"] = d.get("version") or doc.get("version")
        ex.append(doc)
    return ex


def mkid(genre, title, venue):
    return f"{genre}-{hashlib.md5((genre + title + venue).encode()).hexdigest()[:8]}"


AGGREGATORS = {"quehacemos", "allevents", "eventbrite", "indiehoy", "cartelera_teatro", "linda", "songkick", "bandsintown"}


def build(args):
    cands = json.load(open(f"{OUT}/candidates.json"))
    cands.sort(key=lambda c: (c["source"] in AGGREGATORS, bool(c.get("via"))))  # direct feeds first, then catalog finds, then aggregators
    existing = load_existing(args.existing)
    by_key, by_url = {}, {}
    for e in existing:
        by_url[(e.get("url") or "").rstrip("/")] = e
        for d in (e.get("dates") or [""]):
            by_key[(norm(e.get("title"))[:48], d)] = e
        by_key[(norm(e.get("title"))[:48], norm(e.get("venue"))[:20])] = e
    fx = args.fx or fx_rate() or 1400.0
    merged, url_key = {}, {}
    for c in cands:
        if not c.get("title") or re.search(r"^(inicio|home|eventos|agenda|cartelera|entradas)$", c["title"], re.I):
            continue
        k = (norm(c["title"])[:48], norm(c.get("venue"))[:20])
        uk = re.sub(r"^https?://(www\.)?", "", (c.get("url") or "").split("?")[0].rstrip("/").lower())
        if k not in merged and uk and uk in url_key:
            k = url_key[uk]
        if uk:
            url_key.setdefault(uk, k)
        if k in merged:
            m = merged[k]
            m["dates"] = sorted(set(m["dates"]) | set(c["dates"]))
            for f in ("time", "venue", "address", "price", "desc_raw", "img_hint", "lat", "lng", "found_on", "detail_source", "img_backup"):
                if not m.get(f) and c.get(f):
                    m[f] = c[f]
            if m["source"] in AGGREGATORS and c["source"] not in AGGREGATORS:  # the original platform becomes the listing's link
                m["also"] = sorted(set(m.get("also", [])) | {m["source"]})
                m["source"], m["url"], m["page"] = c["source"], c["url"], c.get("page", c["url"])
                for f in ("img_hint", "lat", "lng", "price", "genre_hint"):
                    if c.get(f) is not None and c.get(f) != "":
                        m[f] = c[f]
                m["tags"] = list(dict.fromkeys((c.get("tags") or []) + (m.get("tags") or [])))[:14]
            else:
                m["also"] = sorted(set(m.get("also", [])) | {c["source"]})
            # prefer an event's own page over a listing page
            if m["url"] == m.get("page") and c["url"] != c.get("page") and not (c["source"] in AGGREGATORS and m["source"] not in AGGREGATORS):
                m["url"] = c["url"]
            if c.get("org") and not m.get("org"):
                m["org"] = c["org"]
            if not c.get("far"):
                m.pop("far", None)
        else:
            merged[k] = dict(c)
    for m in merged.values():
        m["also"] = sorted(set(m.get("also", [])) - {m["source"]})
    new, extend, skipped = [], [], 0
    today = TODAY.isoformat()
    for c in merged.values():
        ex = by_url.get(c["url"].rstrip("/")) or by_key.get((norm(c["title"])[:48], norm(c.get("venue"))[:20])) \
            or next((by_key.get((norm(c["title"])[:48], d)) for d in c["dates"] if by_key.get((norm(c["title"])[:48], d))), None)
        if ex:
            add = sorted(set(c["dates"]) - set(ex.get("dates") or []))
            if ex.get("harvested") and ex.get("kind") == "event" and add:
                extend.append({"id": ex["id"], "version": ex.get("_version"), "dates": sorted(set(ex.get("dates") or []) | set(add))})
            skipped += 1
            continue
        genre, sub = classify(c)
        if c.get("far") and genre not in FUTURE_GENRES:
            skipped += 1
            continue  # beyond this week only nightlife and events are kept (the app's Don't Forget)
        ars = usd = None
        if c.get("price") is not None:
            if c.get("currency") in ("USD", "US$"):
                usd = round(c["price"]); ars = round(c["price"] * fx)
            else:
                ars = round(c["price"]); usd = round(c["price"] / fx) if c["price"] else 0
        if usd is not None and usd > 140:
            skipped += 1
            continue
        title = c["title"].strip()[:120]
        venue = (c.get("venue") or (c.get("address") or "").split(",")[0]).strip()[:80]
        doc = {
            "id": mkid(genre, title, venue), "genre": genre, "title": title,
            "kind": "event" if c["dates"] else "ongoing", "dates": c["dates"], "days": [], "time": c.get("time", ""),
            "venue": venue or "See event page", "neighborhood": neighborhood(c) or (c.get("hood") or "")[:40],
            "price_ars": ars, "price_usd": usd,
            "price_note": "Free" if usd == 0 else (f"From ARS {ars:,}".replace(",", ".") if ars else ""),
            "over_budget": bool(usd and usd > 100), "url": c["url"], "source": c["source"] + ("+" + "+".join(c["also"]) if c.get("also") else ""),
            "desc": "", "desc_raw": c.get("desc_raw", "")[:400], "addedAt": today, "harvested": True, "harvestedAt": today,
        }
        if sub:
            doc["sub"] = sub
        if c.get("via"):
            doc["via"] = c["via"]
        if c.get("found_on") and c.get("found_on") != c.get("url"):
            doc["foundOn"] = c["found_on"][:200]
        if c.get("detail_source"):
            doc["detailsFrom"] = c["detail_source"]
        if c.get("run") and not c["dates"]:
            doc["start"], doc["end"] = c["run"]
            doc["kind"] = "ongoing"
        if c.get("lat") and c.get("lng") and -34.95 < c["lat"] < -34.3 and -58.95 < c["lng"] < -58.15:
            doc["lat"], doc["lng"] = c["lat"], c["lng"]
        if c.get("address") and c["address"].strip() and c["address"].strip() != venue:
            doc["address"] = c["address"].strip()[:120]  # the map routine geocodes from this
        if c.get("img_hint"):
            doc["imgHint"] = c["img_hint"]
        elif c.get("img_backup"):
            doc["imgBackup"] = c["img_backup"]     # the discovery site's picture: used only if the original page has none
        if c.get("org"):
            o = c["org"]
            doc["orgId"] = o["i"]
            doc.update({k: v for k, v in (("zone", o.get("z")), ("area1", o.get("a1")), ("area2", o.get("a2")), ("cell", o.get("c"))) if v})
            if not doc["neighborhood"] and o.get("a2"):
                doc["neighborhood"] = o["a2"]
        new.append(doc)
    # keep ids unique
    seen, uniq = set(), []
    for d in new:
        if d["id"] not in seen:
            seen.add(d["id"]); uniq.append(d)
    uniq.sort(key=lambda d: (d["dates"] or [d.get("start") or "9"])[0])  # soonest first, so the cut drops far-off ones
    new = uniq[: args.max_new]
    os.makedirs(f"{OUT}/new", exist_ok=True)
    os.makedirs(f"{OUT}/extend", exist_ok=True)
    for f in glob.glob(f"{OUT}/new/*.json") + glob.glob(f"{OUT}/extend/*.json"):
        os.remove(f)
    review = [{"id": d["id"], "genre": d["genre"], "sub": d.get("sub"), "title": d["title"], "venue": d["venue"], "when": (d["dates"][:3], d["time"]),
               "price": d["price_note"], "source": d["source"], "raw": d["desc_raw"][:220]} for d in new]
    json.dump(review, open(f"{OUT}/review.json", "w"), ensure_ascii=False, indent=1)
    for d in new:
        json.dump(d, open(f"{OUT}/new/{d['id']}.json", "w"), ensure_ascii=False)
    for x in extend:
        json.dump(x, open(f"{OUT}/extend/{x['id']}.json", "w"), ensure_ascii=False)
    from collections import Counter
    print("fx", fx, "| new", len(new), "| date extensions", len(extend), "| already in app / over budget", skipped)
    print("new by genre", dict(Counter(d["genre"] for d in new)))
    print("new by source", dict(Counter(d["source"].split("+")[0] for d in new)))


# ------------------------------------------------------------------ "Don't Forget": the long-horizon database
def future(args):
    """Merge today's far-ahead nightlife/events candidates into the future database.

    The database is the collection "future": one doc per month of the first date ("2026-12", or "2026-12-b"
    when a month is large), each {"items": {id: rec}, "updatedAt"}. rec is compact:
    t title, d [dates], tm time, v venue, nb neighborhood, g genre, s sub, u url, src source,
    p price_usd, pn price note, img image hint, la/ln, seen (first seen), upd (last seen).
    Writes out/future/<doc>.json for every doc to set and out/future/_delete.json for docs to remove.
    """
    today = TODAY.isoformat()
    prev = {}
    for f in glob.glob(os.path.join(args.existing, "*.json")):
        d = json.load(open(f)); d = d.get("data", d) if isinstance(d.get("data"), dict) else d
        doc_id = os.path.splitext(os.path.basename(f))[0]
        if doc_id == "meta":
            continue
        for k, r in (d.get("items") or {}).items():
            prev[k] = r
    old_docs = {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(args.existing, "*.json"))} - {"meta"}
    cands = json.load(open(f"{OUT}/future_candidates.json")) if os.path.exists(f"{OUT}/future_candidates.json") else []
    fx = args.fx or fx_rate() or 1400.0
    added = updated = 0
    for c in cands:
        if not c.get("title") or re.search(r"^(inicio|home|eventos|agenda|cartelera|entradas)$", c["title"], re.I):
            continue
        genre, sub = classify(c)
        if genre not in FUTURE_GENRES:
            continue
        title = c["title"].strip()[:120]
        venue = (c.get("venue") or (c.get("address") or "").split(",")[0]).strip()[:80]
        k = mkid(genre, title, venue)
        usd = None
        if c.get("price") is not None:
            usd = round(c["price"]) if c.get("currency") in ("USD", "US$") else (round(c["price"] / fx) if c["price"] else 0)
        r = prev.get(k)
        if r:
            r["d"] = sorted(set(r.get("d", [])) | set(c["dates"]))
            r["upd"] = today
            for a, b in (("tm", c.get("time")), ("img", c.get("img_hint")), ("v", venue)):
                if b and not r.get(a):
                    r[a] = b
            updated += 1
        else:
            r = {"t": title, "d": sorted(set(c["dates"])), "tm": c.get("time", ""), "v": venue or "See event page",
                 "nb": neighborhood(c), "g": genre, "s": sub or "", "u": c["url"], "src": c["source"],
                 "p": usd, "pn": "Free" if usd == 0 else "", "img": c.get("img_hint") or "",
                 "seen": today, "upd": today}
            if c.get("lat") and c.get("lng"):
                r["la"], r["ln"] = round(c["lat"], 6), round(c["lng"], 6)
            prev[k] = r
            added += 1
    # drop what is over, and anything not seen on any source for 45 days (cancelled or unlisted)
    stale = (TODAY - dt.timedelta(days=45)).isoformat()
    keep = {}
    for k, r in prev.items():
        r["d"] = [x for x in r.get("d", []) if x >= today]
        if r["d"] and (r.get("upd") or today) >= stale:
            keep[k] = r
    by_doc = defaultdict(dict)
    for k, r in sorted(keep.items(), key=lambda kv: kv[1]["d"][0]):
        by_doc[r["d"][0][:7]][k] = r
    os.makedirs(f"{OUT}/future", exist_ok=True)
    for f in glob.glob(f"{OUT}/future/*.json"):
        os.remove(f)
    written = []
    for month, items in sorted(by_doc.items()):
        keys = list(items)
        for i in range(0, len(keys), 600):
            doc_id = month + ("" if i == 0 else "-" + "bcdefgh"[i // 600 - 1])
            json.dump({"items": {k: items[k] for k in keys[i:i + 600]}, "updatedAt": NOW.isoformat(timespec="seconds")},
                      open(f"{OUT}/future/{doc_id}.json", "w"), ensure_ascii=False, separators=(",", ":"))
            written.append(doc_id)
    json.dump(sorted(old_docs - set(written)), open(f"{OUT}/future/_delete.json", "w"))
    print(json.dumps({"future_items": len(keep), "added": added, "updated": updated, "docs": written,
                      "delete_docs": sorted(old_docs - set(written))}, indent=1))


def apply_review(args):
    """fixes.json: {"<id>": {"desc": "...", "genre": "...", "sub": "...", "title": "...", "drop": true}}"""
    fixes = json.load(open(args.fixes))
    n_drop = n_fix = 0
    for f in glob.glob(f"{OUT}/new/*.json"):
        d = json.load(open(f))
        fx = fixes.get(d["id"], {})
        os.remove(f)
        if fx.get("drop"):
            n_drop += 1
            continue
        for k in ("desc", "genre", "sub", "title", "venue", "neighborhood", "time"):
            if fx.get(k) is not None:
                d[k] = fx[k]
        if d.get("genre") not in ("music", "events"):
            d.pop("sub", None)
        d["id"] = mkid(d["genre"], d["title"], d["venue"] if d["venue"] != "See event page" else "")
        if not d.get("desc"):
            g = {"music": "Live music", "nightlife": "Night out", "theater": "Show", "tango": "Tango", "film": "Screening/talk", "classes": "Workshop",
                 "events": "Event", "food": "Food & drink", "markets": "Market", "galleries": "Art", "museums": "Museum", "outdoor": "Outdoor"}.get(d["genre"], "Event")
            d["desc"] = f"{g} at {d['venue']}"[:110]
        d.pop("desc_raw", None)
        n_fix += 1 if fx else 0
        json.dump(d, open(f"{OUT}/new/{d['id']}.json", "w"), ensure_ascii=False)
    print("dropped", n_drop, "edited", n_fix, "remaining", len(glob.glob(f"{OUT}/new/*.json")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    h = sp.add_parser("harvest"); h.add_argument("--only"); h.add_argument("--window", type=int, default=7)
    b = sp.add_parser("build"); b.add_argument("--existing", default="db/events"); b.add_argument("--max-new", type=int, default=1200); b.add_argument("--fx", type=float)
    r = sp.add_parser("apply-review"); r.add_argument("fixes")
    fu = sp.add_parser("future"); fu.add_argument("--existing", default="db/future"); fu.add_argument("--fx", type=float)
    a = ap.parse_args()
    {"harvest": harvest, "build": build, "apply-review": apply_review, "future": future}[a.cmd](a)
