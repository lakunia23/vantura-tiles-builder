#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VANTURA - OPISY ATRAKCJI v2.2 (etap budowy kafla)

Zasada: opis ma być PRAWDZIWY, ciekawy i po polsku (nazwy własne bez zmian).
Aplikacja wyświetla średnio do 3 zdań podsumowania, a gdy opis ma więcej niż 3 zdania -
przycisk "Czytaj więcej" rozwija pełny, szczegółowy opis (do 10 zdań).

Źródła faktów, bez rang i bez ucinania punktów:
  1. Polska Wikipedia (do 10 zdań) - przez tag wikidata/wikipedia z OSM, relacje multipolygon,
     dziedziczenie z sąsiedniego budynku (<= 60 m) oraz bezpieczny GeoSearch (współrzędne + nazwa).
  2. Obcojęzyczna Wikipedia (EN + ponad 40 języków, do 10 zdań), obcojęzyczne opisy OSM oraz
     krótkie opisy Wikidata (desc_en) - tłumaczone dla KAŻDEGO punktu wyłącznie przez TranslateGemma 4B
     (Ollama) z zachowaniem nazw własnych, kontrolą liczb oraz wznawialną pamięcią tłumaczeń
     (tr_<KAFEL>_pl.json.gz współdzieloną z sąsiednimi kaflami i wznawianą między rundami).
  3. Bogate dane OpenStreetMap + Wikidata (styl architektoniczny, status UNESCO, pasmo górskie,
     park narodowy/rezerwat, typ zamku/muzeum, materiał, architekt/twórca, kierunek panoramy,
     długość jaskini, wybitność szczytu, godziny otwarcia, wstęp, inskrypcje).
Bez zdjęć (image_url = NULL) - aplikacja pobiera zdjęcie sama przy otwarciu atrakcji.
"""
import gc
import gzip
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from extract_pois import TilePoiScanner, classify_osm_tags, parse_elevation

KEEP_TAGS = (
    "historic", "tourism", "natural", "leisure", "amenity", "building", "religion", "denomination",
    "start_date", "construction_date", "year_of_construction", "heritage", "heritage:operator",
    "whc:inscription_date", "architect", "artist_name", "artist", "inscription", "description",
    "description:pl", "description:en", "description:de", "memorial", "memorial:type",
    "memorial:subject", "commemorates", "castle_type", "ruins", "artwork_type", "material",
    "height", "ele", "prominence", "depth", "operator", "fee", "opening_hours", "wheelchair",
    "addr:city", "addr:place", "is_in:city", "wikipedia", "wikidata", "name:pl", "museum",
    "museum:type", "subject", "tower:type", "tower:construction", "species", "genus",
    "protect_class", "protection_title", "denotation", "cave:ref", "length", "direction",
)


def _area_centroid(area):
    """Geometryczny centroid zewnętrznego pierścienia obiektu osmium.osm.Area (lub None)."""
    try:
        lats, lons = [], []
        for ring in area.outer_rings():
            for n in ring:
                if n.location.valid():
                    lats.append(n.lat)
                    lons.append(n.lon)
        if not lats:
            return None
        return sum(lats) / len(lats), sum(lons) / len(lons), (min(lats), min(lons), max(lats), max(lons))
    except Exception:
        return None


def _way_bbox(nodes):
    lats, lons = [], []
    for n in nodes:
        if n.location.valid():
            lats.append(n.lat)
            lons.append(n.lon)
    if not lats:
        return None
    return min(lats), min(lons), max(lats), max(lons)


class RichTilePoiScanner(TilePoiScanner):
    """Skaner POI rozszerzony o:
    - pełny zestaw tagów faktograficznych (KEEP_TAGS),
    - relacje multipolygon (area(), gdy nie pochodzą ze zwykłego way),
    - miejscowości (places), obszary chronione (protected_areas),
    - siatkę obiektów OSM z wikidata/wikipedia (geo_wiki) do dziedziczenia z budynków."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.places = []
        self.geo_wiki = []
        self.protected_areas = []

    def _attach(self, before, tags):
        if len(self.pois) > before:
            self.pois[-1]["tags"] = {k: tags.get(k) for k in KEEP_TAGS if tags.get(k)}

    def _record_geo_wiki(self, lat, lon, tags):
        wd = tags.get("wikidata")
        wp = tags.get("wikipedia")
        nm = tags.get("name:pl") or tags.get("name")
        if (wd or wp) and nm and (self.min_lat <= lat < self.max_lat and self.min_lon <= lon < self.max_lon):
            self.geo_wiki.append((lat, lon, nm, wd, wp))

    def _record_protected(self, bbox, tags):
        bnd = tags.get("boundary")
        leis = tags.get("leisure")
        nm = tags.get("name:pl") or tags.get("name")
        if not nm or not bbox:
            return
        if bnd in ("national_park", "protected_area") or leis == "nature_reserve":
            lat_span = bbox[2] - bbox[0]
            lon_span = bbox[3] - bbox[1]
            # pomijamy ogromne obszary kontynentalne/krajowe i mikroskopijne punkty
            if 0.002 <= max(lat_span, lon_span) <= 1.5:
                kind = "national_park" if bnd == "national_park" else (
                    "nature_reserve" if leis == "nature_reserve" else "protected_area"
                )
                self.protected_areas.append((bbox, nm.strip(), kind))

    def node(self, n):
        before = len(self.pois)
        super().node(n)
        self._attach(before, n.tags)
        if n.location.valid():
            lat, lon = n.location.lat, n.location.lon
            self._record_geo_wiki(lat, lon, n.tags)
            if n.tags.get("place") in ("city", "town", "village") and n.tags.get("name"):
                self.places.append((lat, lon, n.tags.get("name:pl") or n.tags.get("name"), n.tags.get("place")))

    def way(self, w):
        before = len(self.pois)
        super().way(w)
        self._attach(before, w.tags)
        if w.tags.get("wikidata") or w.tags.get("wikipedia") or w.tags.get("boundary") in ("national_park", "protected_area") or w.tags.get("leisure") == "nature_reserve":
            try:
                bb = _way_bbox(w.nodes)
                if bb:
                    lat, lon = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
                    self._record_geo_wiki(lat, lon, w.tags)
                    self._record_protected(bb, w.tags)
            except Exception:
                pass

    def area(self, a):
        # Tylko multipolygony (relacje) - zwykłe zamknięte way już obsłużyła metoda way()
        if a.from_way():
            return
        info = _area_centroid(a)
        if not info:
            return
        lat, lon, bb = info
        self._record_protected(bb, a.tags)
        self._record_geo_wiki(lat, lon, a.tags)
        if not (self.min_lat <= lat < self.max_lat and self.min_lon <= lon < self.max_lon):
            return
        cat, sub = classify_osm_tags(a.tags)
        if not cat:
            return
        self.pois.append({
            "id": f"R_{a.orig_id()}",
            "name": a.tags.get("name"),
            "lat": lat,
            "lng": lon,
            "category": cat,
            "sub_cat": sub,
            "elevation": parse_elevation(a.tags.get("ele")),
            "wiki": a.tags.get("wikipedia"),
            "wikidata": a.tags.get("wikidata"),
            "tags": {k: a.tags.get(k) for k in KEEP_TAGS if a.tags.get(k)},
        })


# ------------------------------------------------------------------ dopasowanie nazw (dziedziczenie + GeoSearch)
_GENERIC_TOKENS = {
    # PL
    "zamek", "palac", "dwor", "dworek", "ruiny", "ruina", "fort", "twierdza", "pomnik", "tablica", "muzeum",
    "galeria", "teatr", "park", "ogrod", "skwer", "szczyt", "gora", "wierch", "kopa", "jaskinia", "grota",
    "wodospad", "zrodlo", "wieza", "widokowa", "punkt", "widokowy", "kosciol", "kaplica", "kamienica", "ratusz",
    "dom", "plac", "rynek", "rezerwat", "przyrody", "narodowy", "krajobrazowy", "miejskie", "miejski", "muzealny",
    "kopiec", "krzyz", "obelisk", "rzezba", "posag", "mural", "budynek", "zespol", "dawny", "dawna", "stary", "stara",
    "wielki", "wielka", "maly", "mala", "nowy", "nowa", "dolny", "gorny", "pod", "nad", "przy", "dla", "oraz", "imienia",
    # EN / DE / FR / HU / CS / ES / IT
    "castle", "palace", "manor", "ruins", "fortress", "monument", "memorial", "museum", "gallery", "theatre",
    "theater", "park", "garden", "peak", "mount", "mountain", "hill", "cave", "grotto", "waterfall", "falls",
    "spring", "tower", "viewpoint", "lookout", "church", "chapel", "square", "reserve", "national", "nature",
    "house", "building", "hall", "statue", "sculpture", "art", "history", "historical", "city", "town", "old", "new",
    "burg", "schloss", "ruine", "denkmal", "kirche", "berg", "turm", "hohle", "aussichtsturm", "aussichtspunkt",
    "chateau", "musee", "eglise", "tour", "grotte", "mont", "jardin", "place",
    "var", "varrom", "kilato", "kilatotorony", "barlang", "hegy", "romjai", "romkert", "szobor", "emlekmu", "kastely",
    "hrad", "zamek", "rozhledna", "jeskyne", "zricenina", "kostel", "namesti", "castillo", "castello", "palazzo",
    "the", "and", "for", "with", "from", "der", "die", "das", "den", "dem", "des", "von", "und", "zum", "zur",
    "les", "une", "del", "della", "degli", "nel", "nella", "los", "las", "con", "por", "para",
}

_CONFLICT_TITLE_WORDS = {
    "synagoga": {"synagog"},
    "synagogue": {"synagog"},
    "kosciol": {"kosciol", "katedr", "bazylik", "kolegiat", "farny", "fara", "church", "kirche", "eglise"},
    "cmentarz": {"cmentarz", "nekropoli", "grob", "mogil", "cemetery", "friedhof"},
    "ulica": {"ulica", "aleja", "street", "strasse"},
    "gmina": {"gmina"},
    "powiat": {"powiat"},
    "wojewodztwo": {"wojewodztwo"},
    "gromada": {"gromada"},
    "stacja": {"stacja", "dworzec", "station", "bahnhof"},
    "szkola": {"szkola", "liceum", "gimnazjum", "school"},
    "uniwersytet": {"uniwersytet", "university"},
    "rzeka": {"rzeka", "potok", "river"},
}


def _strip_accents(s):
    s = (s or "").lower().replace("ł", "l").replace("ß", "ss").replace("ø", "o").replace("æ", "ae")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _name_tokens(s, exclude=None):
    excl = exclude or set()
    raw = re.findall(r"[a-z0-9]{3,}", _strip_accents(s))
    out = set()
    for w in raw:
        if w in _GENERIC_TOKENS or w in excl:
            continue
        # wyklucz także odmienione formy nazwy miasta (np. Bochnia -> Bochni, Kraków -> Krakowie)
        if excl and len(w) >= 4 and any(
            len(e) >= 4 and w[:max(4, min(len(w), len(e)) - 3)] == e[:max(4, min(len(w), len(e)) - 3)]
            for e in excl
        ):
            continue
        out.add(w)
    return out


def _stems_overlap(t1, t2):
    """Zwraca liczbę dopasowanych charakterystycznych rdzeni między dwoma zbiorami słów."""
    matched = 0
    for x in t1:
        for y in t2:
            if x == y:
                matched += 1
                break
            if len(x) >= 4 and len(y) >= 4:
                need = max(4, min(len(x), len(y)) - 3)
                if x[:need] == y[:need]:
                    matched += 1
                    break
    return matched


def _title_conflicts_with_poi(poi_name, sub_cat, title):
    p_norm = _strip_accents(poi_name)
    t_words = set(re.findall(r"[a-z]{4,}", _strip_accents(title)))
    for bad_word, allowed_in_poi in _CONFLICT_TITLE_WORDS.items():
        if bad_word in t_words:
            if sub_cat == "heritage" and bad_word in ("kosciol", "synagoga"):
                continue
            if not any(ok in p_norm for ok in allowed_in_poi):
                return True
    return False


def inherit_nearby_wiki(pois, geo_wiki, log=print):
    """Dziedziczy wikidata/wikipedia z obiektu/budynku OSM w promieniu <= 60 m o zgodnej nazwie własnej."""
    if not geo_wiki:
        return 0
    grid = {}
    for item in geo_wiki:
        grid.setdefault((int(item[0] * 100), int(item[1] * 100)), []).append(item)
    inherited = 0
    for p in pois:
        if p.get("wikidata") or p.get("wiki") or (p.get("tags") or {}).get("wikipedia"):
            continue
        la, lo = p["lat"], p["lng"]
        ptoks = _name_tokens(p.get("name"))
        if not ptoks:
            continue
        best, best_d = None, 60.0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for glat, glon, gname, gwd, gwp in grid.get((int(la * 100) + dy, int(lo * 100) + dx), []):
                    x = (lo - glon) * 111320 * math.cos(math.radians(la))
                    y = (la - glat) * 110540
                    dist = math.hypot(x, y)
                    if dist > best_d:
                        continue
                    gtoks = _name_tokens(gname)
                    if not gtoks:
                        continue
                    ov = _stems_overlap(ptoks, gtoks)
                    need = 1 if min(len(ptoks), len(gtoks)) <= 2 else 2
                    if ov >= need and not _title_conflicts_with_poi(p.get("name"), p.get("sub_cat"), gname):
                        best, best_d = (gwd, gwp), dist
        if best:
            if best[0]:
                p["wikidata"] = best[0]
            if best[1]:
                p["wiki"] = best[1]
            inherited += 1
    if inherited:
        log(f"  [OSM] odziedziczono wikidata/wikipedia z sąsiednich budynków: {inherited}")
    return inherited


def attach_protected_area(pois, protected_areas):
    """Przypisuje POI nazwę parku narodowego / rezerwatu, jeśli punkt leży w jego obrysie."""
    if not protected_areas:
        return
    # najpierw parki narodowe, potem mniejsze rezerwaty
    areas = sorted(protected_areas, key=lambda a: (0 if a[2] == "national_park" else 1, (a[0][2] - a[0][0]) * (a[0][3] - a[0][1])))
    for p in pois:
        if p.get("sub_cat") == "nature_reserve":
            continue
        la, lo = p["lat"], p["lng"]
        for bb, nm, kind in areas:
            if bb[0] <= la <= bb[2] and bb[1] <= lo <= bb[3] and nm.lower() != (p.get("name") or "").lower():
                p["_protected_area"] = (nm, kind)
                break


# ------------------------------------------------------------------ poziom 3: bogaty opis z tagów OSM + Wikidata
TYPE_PL = {
    "castle": "Zamek", "palace": "Pałac", "manor": "Dwór", "ruins": "Ruiny", "fort": "Fort",
    "monument": "Pomnik", "memorial": "Miejsce pamięci", "archaeological_site": "Stanowisko archeologiczne",
    "mine": "Dawna kopalnia", "city_gate": "Brama miejska", "citywalls": "Mury miejskie",
    "battlefield": "Pole bitwy", "heritage": "Obiekt zabytkowy", "pillbox": "Schron bojowy", "bunker": "Bunkier",
    "museum": "Muzeum", "art_museum": "Muzeum sztuki", "city_square": "Plac", "gallery": "Galeria sztuki",
    "theatre": "Teatr", "arts_centre": "Centrum sztuki", "planetarium": "Planetarium", "concert_hall": "Sala koncertowa",
    "artwork": "Dzieło sztuki", "sculpture": "Rzeźba", "statue": "Posąg", "mural": "Mural", "installation": "Instalacja artystyczna",
    "mosaic": "Mozaika", "street_art": "Sztuka uliczna", "viewpoint": "Punkt widokowy", "observation_tower": "Wieża widokowa",
    "peak": "Szczyt górski", "volcano": "Wulkan", "waterfall": "Wodospad", "cave_entrance": "Jaskinia",
    "cliff": "Urwisko skalne", "rock": "Formacja skalna", "stone": "Głaz", "gorge": "Wąwóz", "spring": "Źródło", "beach": "Plaża",
    "wood": "Las", "wetland": "Mokradła", "dune": "Wydma", "glacier": "Lodowiec", "cape": "Przylądek",
    "park": "Park", "garden": "Ogród", "village_green": "Skwer", "nature_reserve": "Obszar chroniony",
}
CASTLE_TYPE_PL = {
    "defensive": "Zamek obronny", "fortress": "Twierdza", "palace": "Pałac rezydencjalny",
    "stately": "Rezydencja pałacowa", "manor": "Dwór szlachecki", "castrum": "Dawna warownia",
    "shiro": "Tradycyjny zamek japoński", "kremlin": "Warowny kompleks zamkowy", "burg": "Zamek obronny",
}
MUSEUM_TYPE_PL = {
    "history": "Muzeum historyczne", "local": "Muzeum regionalne", "art": "Muzeum sztuki",
    "nature": "Muzeum przyrodnicze", "natural_history": "Muzeum historii naturalnej",
    "technology": "Muzeum techniki", "science": "Muzeum nauki", "open_air": "Skansen",
    "military": "Muzeum wojskowe", "railway": "Muzeum kolejnictwa", "archaeological": "Muzeum archeologiczne",
    "ethnography": "Muzeum etnograficzne", "maritime": "Muzeum morskie", "aviation": "Muzeum lotnictwa",
    "transport": "Muzeum transportu", "mining": "Muzeum górnictwa", "agriculture": "Muzeum rolnictwa",
}
BUILDING_HERITAGE_PL = {
    "church": "Zabytkowy kościół", "cathedral": "Zabytkowa katedra", "chapel": "Zabytkowa kaplica",
    "synagogue": "Zabytkowa synagoga", "monastery": "Zabytkowy klasztor", "mosque": "Zabytkowy meczet",
    "temple": "Zabytkowa świątynia", "palace": "Zabytkowy pałac", "castle": "Zabytkowy zamek",
    "manor": "Zabytkowy dwór", "townhall": "Zabytkowy ratusz", "train_station": "Zabytkowy dworzec kolejowy",
    "mill": "Zabytkowy młyn", "windmill": "Zabytkowy wiatrak", "tower": "Zabytkowa wieża",
    "bridge": "Zabytkowy most", "house": "Zabytkowy dom", "villa": "Zabytkowa willa",
    "granary": "Zabytkowy spichlerz", "school": "Zabytkowy budynek szkoły",
}
MEMORIAL_PL = {
    "plaque": "Tablica pamiątkowa", "stolperstein": "Kamień pamięci (Stolperstein)", "statue": "Pomnik",
    "bust": "Popiersie", "stone": "Kamień pamiątkowy", "cross": "Krzyż pamiątkowy",
    "war_memorial": "Pomnik wojenny", "obelisk": "Obelisk", "stele": "Stela", "column": "Kolumna pamiątkowa",
}
MATERIAL_PL = {
    "bronze": "z brązu", "stone": "z kamienia", "granite": "z granitu", "marble": "z marmuru",
    "sandstone": "z piaskowca", "limestone": "z wapienia", "wood": "z drewna", "timber": "z drewna",
    "steel": "ze stali", "iron": "z żelaza", "metal": "z metalu", "brick": "z cegły",
    "concrete": "z betonu", "glass": "ze szkła", "copper": "z miedzi", "brass": "z mosiądzu",
}
TOWER_MAT_PL = {
    "wood": "Drewniana", "timber": "Drewniana", "steel": "Stalowa", "metal": "Metalowa",
    "stone": "Kamienna", "brick": "Ceglana", "concrete": "Betonowa",
}
DENOM_PL = {
    "catholic": "rzymskokatolickie", "roman_catholic": "rzymskokatolickie", "orthodox": "prawosławne",
    "lutheran": "ewangelicko-augsburskie", "evangelical": "ewangelickie", "greek_catholic": "greckokatolickie",
    "protestant": "protestanckie", "jewish": "żydowskie", "reformed": "ewangelicko-reformowane",
}
RELIGION_PL = {"christian": "chrześcijańskie", "jewish": "żydowskie", "muslim": "muzułmańskie", "buddhist": "buddyjskie"}
STYLE_QID_PL = {
    "Q176483": "gotyckim", "Q46261": "romańskim", "Q37853": "barokowym", "Q236122": "renesansowym",
    "Q186363": "neogotyckim", "Q54111": "klasycystycznym", "Q170292": "neoklasycystycznym",
    "Q34636": "secesyjnym", "Q245188": "modernistycznym", "Q122960": "rokokowym",
    "Q173782": "art déco", "Q192616": "eklektycznym", "Q502163": "neobarokowym",
    "Q502081": "neorenesansowym", "Q46825": "neoromańskim", "Q47595": "bizantyjskim",
    "Q249520": "późnogotyckim", "Q1318295": "wczesnogotyckim", "Q131271": "manierystycznym",
    "Q214265": "funkcjonalistycznym", "Q994776": "brutalistycznym", "Q191105": "empire",
    "Q744373": "historyzującym", "Q16560": "pałacowym", "Q1536021": "późnobarokowym",
}
ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI",
         "XVII", "XVIII", "XIX", "XX", "XXI"]
ROMAN_TO_INT = {r: i for i, r in enumerate(ROMAN) if i > 0}


def _date_pl(v):
    v = (v or "").strip()
    m = re.fullmatch(r"~?\s*(\d{3,4})(?:-\d{2}(?:-\d{2})?)?", v)
    if m:
        return f"{int(m.group(1))} r."
    m = re.fullmatch(r"C(\d{1,2})", v, re.I)
    if m and 0 < int(m.group(1)) < len(ROMAN):
        return f"{ROMAN[int(m.group(1))]} w."
    m = re.fullmatch(r"(\d{3,4})\.\.(\d{3,4})", v)
    if m:
        return f"lat {m.group(1)}–{m.group(2)}"
    m = re.fullmatch(r"(\d{3,4})s", v)
    if m:
        return f"lat {m.group(1)}."
    return None


DIRS_PL = ["na północ", "na północny wschód", "na wschód", "na południowy wschód",
           "na południe", "na południowy zachód", "na zachód", "na północny zachód"]
VIEW_DIRS_PL = {
    "n": "na północ", "ne": "na północny wschód", "e": "na wschód", "se": "na południowy wschód",
    "s": "na południe", "sw": "na południowy zachód", "w": "na zachód", "nw": "na północny zachód",
}


def _direction_pl(v):
    v = (v or "").strip().lower()
    if not v:
        return None
    if v in ("0-360", "360", "all", "0..360"):
        return "pełną panoramę 360°"
    if v in VIEW_DIRS_PL:
        return f"widok {VIEW_DIRS_PL[v]}"
    m = re.fullmatch(r"(\d{1,3})(?:\.\d+)?", v)
    if m:
        deg = int(m.group(1)) % 360
        return f"widok {DIRS_PL[int((deg + 22.5) // 45) % 8]}"
    m = re.fullmatch(r"(\d{1,3})-(\d{1,3})", v)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        span = (b - a) % 360
        if span >= 300 or span == 0:
            return "pełną panoramę 360°"
        mid = (a + span / 2) % 360
        return f"panoramę skierowaną {DIRS_PL[int((mid + 22.5) // 45) % 8]}"
    return None


def attach_locality(pois, places):
    """Dla każdego POI: najbliższa miejscowość (do 8 km), odległość i kierunek - wyliczone z mapy."""
    if not places:
        return
    grid = {}
    for pl in places:
        grid.setdefault((int(pl[0] * 10), int(pl[1] * 10)), []).append(pl)
    for p in pois:
        la, lo = p["lat"], p["lng"]
        best, bd = None, 1e18
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for pl in grid.get((int(la * 10) + dy, int(lo * 10) + dx), []):
                    x = (lo - pl[1]) * 111320 * math.cos(math.radians(la))
                    y = (la - pl[0]) * 110540
                    d = x * x + y * y
                    if d < bd:
                        best, bd = (pl, x, y), d
        if best:
            pl, x, y = best
            dist = math.sqrt(bd)
            if dist <= 8000 and pl[2] != p.get("name"):
                ang = (math.degrees(math.atan2(x, y)) + 360) % 360
                p["_locality"] = (pl[2], dist, DIRS_PL[int((ang + 22.5) // 45) % 8])


def _num(v):
    m = re.match(r"\s*(\d+(?:[.,]\d+)?)", str(v))
    return m.group(1).replace(",", ".") if m else None


def _fmt_num(v):
    n = _num(v)
    if not n:
        return None
    if "." in n:
        n = n.rstrip("0").rstrip(".")
    return n.replace(".", ",")


def _clean_inscription(raw):
    ins = re.sub(r"\s*\|\s*", " – ", raw)
    ins = re.sub(r"\s+", " ", ins).strip(" –-;")
    return ins


def describe_from_osm(p):
    """Tworzy naturalny, ciekawy i w 100% oparty na faktach opis po polsku z danych OSM i Wikidata."""
    t = p.get("tags") or {}
    wd = p.get("_wd") or {}
    sub = p.get("sub_cat") or ""
    sent = []

    # 1. Doprecyzowany rodzaj obiektu
    kind = TYPE_PL.get(sub, "Atrakcja")
    if sub == "castle" and (t.get("castle_type") or "").lower() in CASTLE_TYPE_PL:
        kind = CASTLE_TYPE_PL[t["castle_type"].lower()]
    elif sub == "museum":
        mt = (t.get("museum") or t.get("museum:type") or "").lower()
        if mt in MUSEUM_TYPE_PL:
            kind = MUSEUM_TYPE_PL[mt]
    elif sub == "memorial" and (t.get("memorial") or t.get("memorial:type") or "").lower() in MEMORIAL_PL:
        kind = MEMORIAL_PL[(t.get("memorial") or t.get("memorial:type")).lower()]
    elif sub == "heritage" and (t.get("building") or "").lower() in BUILDING_HERITAGE_PL:
        kind = BUILDING_HERITAGE_PL[t["building"].lower()]
    elif sub == "observation_tower":
        mat = (t.get("material") or t.get("tower:construction") or "").lower()
        if mat in TOWER_MAT_PL:
            kind = f"{TOWER_MAT_PL[mat]} wieża widokowa"
    elif sub == "nature_reserve" and t.get("protection_title"):
        pt = t["protection_title"].strip()
        if len(pt) <= 45 and looks_polish(pt):
            kind = pt[0].upper() + pt[1:]

    # Materiał w pierwszym zdaniu dla rzeźb / pomników / dzieł sztuki
    mat_txt = MATERIAL_PL.get((t.get("material") or "").lower())
    if mat_txt and sub in ("sculpture", "statue", "monument", "memorial", "artwork"):
        kind = f"{kind} {mat_txt}"

    # 2. Pierwsze zdanie: co to jest + wysokość (dla szczytów) + pasmo/park/położenie
    ele = p.get("elevation") or (_num(t["ele"]) if t.get("ele") else None) or wd.get("elevation")
    city = t.get("addr:city") or t.get("addr:place") or t.get("is_in:city")
    loc = p.get("_locality")
    mrange = wd.get("mountain_range")
    prot = p.get("_protected_area")

    parts_first = [kind]
    if ele and sub in ("peak", "volcano"):
        parts_first.append(f"o wysokości {int(float(ele))} m n.p.m.")
    if mrange and sub in ("peak", "volcano", "viewpoint", "cave_entrance", "rock", "cliff", "gorge", "waterfall", "ruins", "castle"):
        parts_first.append(f"w paśmie {mrange}")
    if prot and prot[0] not in (mrange or ""):
        parts_first.append(f"na terenie obszaru {prot[0]}")
    if city and city != p.get("name"):
        parts_first.append(f"w miejscowości {city}")
    elif loc and loc[1] < 700:
        parts_first.append(f"w miejscowości {loc[0]}")
    elif loc:
        km = loc[1] / 1000.0
        kmtxt = f"{km:.0f}" if km >= 1.5 else f"{km:.1f}".replace(".", ",")
        parts_first.append(f"ok. {kmtxt} km {loc[2]} od miejscowości {loc[0]}")

    first = " ".join(parts_first)
    # jeśli mamy krótki kierunek panoramy na punkcie widokowym, ładnie wplatamy w pierwsze zdanie
    vdir = _direction_pl(t.get("direction")) if sub in ("viewpoint", "observation_tower") else None
    if vdir:
        first += f", oferujący {vdir}"
    sent.append(first + ".")

    # 3. Krótki opis z Wikidata (po polsku lub przetłumaczony z angielskiego; pomijamy meta-opisy i duplikaty)
    wd_desc = wd.get("desc_pl") or wd.get("desc_en_pl")
    if wd_desc and len(wd_desc.strip()) > 8:
        dp = wd_desc.strip()
        dp_low = dp.lower()
        is_meta = any(bad in dp_low for bad in (
            "wikimedia", "ujednoznaczniaj", "disambiguation", "lista w projekcie", "kategoria w projekcie",
        ))
        is_dup = (dp_low in first.lower()) or (
            len(dp) < 55 and dp_low.startswith(kind.lower()) and _name_tokens(dp) <= _name_tokens(first)
        )
        if not is_meta and not is_dup:
            sent.append(dp[0].upper() + dp[1:])

    # 4. Data powstania + styl architektoniczny + data zniszczenia (połączone w naturalne zdanie!)
    d = None
    for k in ("start_date", "construction_date", "year_of_construction"):
        if t.get(k):
            d = _date_pl(t[k])
            if d:
                break
    if not d and wd.get("inception"):
        d = wd["inception"]
    style = wd.get("style_pl")
    dissolved = wd.get("dissolved")
    if d and style:
        s_hist = f"Pochodzi z {d} i został wzniesiony w stylu {style}"
        if dissolved:
            s_hist += f" (zniszczony w {dissolved})"
        sent.append(s_hist)
    elif d:
        s_hist = f"Pochodzi z {d}"
        if dissolved:
            s_hist += f", zniszczony w {dissolved}"
        sent.append(s_hist)
    elif style:
        sent.append(f"Wzniesiony w stylu {style}")

    # 5. Status UNESCO / Pomnik historii / rejestr zabytków
    is_unesco = wd.get("unesco") or t.get("heritage") == "1" or bool(t.get("whc:inscription_date"))
    if is_unesco:
        yr = _date_pl(t.get("whc:inscription_date"))
        sent.append(f"Obiekt wpisany na Listę Światowego Dziedzictwa UNESCO{f' w {yr}' if yr else ''}")
    elif wd.get("pomnik_historii"):
        sent.append("Wyróżniony statusem Pomnika historii")
    elif t.get("heritage"):
        sent.append("Figuruje w rejestrze zabytków")

    # 6. Temat / upamiętnienie / wyznanie / twórca / architekt
    subj = t.get("memorial:subject") or t.get("commemorates") or t.get("subject")
    if subj and len(subj) <= 120 and (looks_polish(subj) or len(subj.split()) <= 4):
        sent.append(f"Poświęcony: {subj.strip()}")
    if t.get("denomination") in DENOM_PL:
        sent.append(f"Wyznanie: {DENOM_PL[t['denomination']]}")
    elif t.get("religion") in RELIGION_PL:
        sent.append(f"Wyznanie: {RELIGION_PL[t['religion']]}")

    arch = t.get("architect") or wd.get("architect")
    if arch:
        sent.append(f"Projekt: {arch}")
    artist = t.get("artist_name") or t.get("artist") or wd.get("creator")
    if artist and artist != arch:
        sent.append(f"Autor: {artist}")

    # 7. Wymiary / parametry przyrodnicze (bez mylenia głębokości jaskini z wysokością budynku!)
    if ele and sub in ("viewpoint", "observation_tower", "cave_entrance", "spring", "rock", "cliff", "glacier"):
        sent.append(f"Położenie na wysokości {int(float(ele))} m n.p.m.")
    prom = _fmt_num(t.get("prominence")) if t.get("prominence") and sub in ("peak", "volcano") else None
    if prom:
        sent.append(f"Wybitność szczytu wynosi {prom} m")
    if sub == "cave_entrance":
        clen = _fmt_num(t.get("length")) if t.get("length") else None
        cdep = _fmt_num(t.get("depth")) if t.get("depth") else None
        if clen and cdep:
            sent.append(f"Korytarze jaskini mają {clen} m długości i {cdep} m deniwelacji")
        elif clen:
            sent.append(f"Długość korytarzy jaskini wynosi {clen} m")
        elif cdep:
            sent.append(f"Deniwelacja jaskini wynosi {cdep} m")
    else:
        h = _fmt_num(t.get("height")) if t.get("height") else None
        if h and float(h.replace(",", ".")) >= 3:
            sent.append(f"Wysokość obiektu: {h} m")
        if sub in ("citywalls", "gorge", "waterfall", "beach") and t.get("length"):
            ln = _fmt_num(t["length"])
            if ln:
                sent.append(f"Długość: {ln} m")

    if t.get("species"):
        sent.append(f"Gatunek: {t['species']}")

    # 8. Inskrypcja (oczyszczona ze znaków "|") i opis z OSM
    if t.get("inscription"):
        ins = _clean_inscription(t["inscription"])
        if 3 <= len(ins) <= 200:
            sent.append(f"Napis na obiekcie: „{ins}”")
    desc = t.get("description:pl") or p.get("_desc_translated") or t.get("description")
    if desc and not t.get("description:pl") and not p.get("_desc_translated") and not looks_polish(desc):
        desc = None
    if desc and len(desc) > 15:
        desc = full_sentences(desc, 600, 4) or (desc.strip() if len(desc) <= 300 else "")
        if desc:
            sent.append(desc.rstrip("."))

    # 9. Informacje praktyczne (wstęp, godziny, dostępność)
    prac = []
    if t.get("fee") == "no":
        prac.append("wstęp bezpłatny")
    elif t.get("fee") == "yes":
        prac.append("wstęp płatny")
    if t.get("wheelchair") == "yes":
        prac.append("obiekt dostępny dla osób na wózkach")
    if prac:
        s_pr = ", ".join(prac)
        sent.append(s_pr[0].upper() + s_pr[1:])
    if t.get("opening_hours") and len(t["opening_hours"]) <= 60:
        oh = opening_hours_pl(t["opening_hours"])
        if oh:
            sent.append(f"Godziny otwarcia: {oh}")

    parts = [x if x.endswith((".", "!", "?", ".”", "!”", "?”")) else x + "." for x in sent if x]
    out = []
    for x in parts:
        if out and len(" ".join(out)) + 1 + len(x) > DESC_MAX_CHARS:
            break
        out.append(x)
    return " ".join(out)


# ------------------------------------------------------------------ poziomy 1-2: Wikipedia + Wikidata
# Języki Wikipedii (kolejność = większe i rzetelniejsze wersje najpierw)
OTHER_WIKI_LANGS = [
    "de", "fr", "es", "it", "cs", "sk", "uk", "nl", "sv", "pt", "hu", "ro", "lt", "lv", "et", "fi",
    "no", "da", "hr", "sl", "sr", "bg", "el", "tr", "ru", "be", "ca", "eu", "gl", "ja", "zh", "ko",
    "ar", "he", "vi", "th", "id", "is", "ka", "hy", "sq", "mk", "bs",
]
LANG_NAMES_PL = {
    "en": "angielskiej", "de": "niemieckiej", "fr": "francuskiej", "es": "hiszpańskiej", "it": "włoskiej",
    "cs": "czeskiej", "sk": "słowackiej", "uk": "ukraińskiej", "nl": "niderlandzkiej", "sv": "szwedzkiej",
    "pt": "portugalskiej", "hu": "węgierskiej", "ro": "rumuńskiej", "lt": "litewskiej", "lv": "łotewskiej",
    "et": "estońskiej", "fi": "fińskiej", "no": "norweskiej", "da": "duńskiej", "hr": "chorwackiej",
    "sl": "słoweńskiej", "sr": "serbskiej", "bg": "bułgarskiej", "el": "greckiej", "tr": "tureckiej",
    "ru": "rosyjskiej", "be": "białoruskiej", "ca": "katalońskiej", "eu": "baskijskiej", "gl": "galicyjskiej",
    "ja": "japońskiej", "zh": "chińskiej", "ko": "koreańskiej", "ar": "arabskiej", "he": "hebrajskiej",
    "vi": "wietnamskiej", "th": "tajskiej", "id": "indonezyjskiej", "is": "islandzkiej", "ka": "gruzińskiej",
    "hy": "ormiańskiej", "sq": "albańskiej", "mk": "macedońskiej", "bs": "bośniackiej",
}
LANG_NAMES_EN = {
    "en": "English", "de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "cs": "Czech",
    "sk": "Slovak", "uk": "Ukrainian", "nl": "Dutch", "sv": "Swedish", "pt": "Portuguese", "hu": "Hungarian",
    "ro": "Romanian", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian", "fi": "Finnish", "no": "Norwegian",
    "da": "Danish", "hr": "Croatian", "sl": "Slovenian", "sr": "Serbian", "bg": "Bulgarian", "el": "Greek",
    "tr": "Turkish", "ru": "Russian", "be": "Belarusian", "ca": "Catalan", "eu": "Basque", "gl": "Galician",
    "ja": "Japanese", "zh": "Chinese", "ko": "Korean", "ar": "Arabic", "he": "Hebrew", "vi": "Vietnamese",
    "th": "Thai", "id": "Indonesian", "is": "Icelandic", "ka": "Georgian", "hy": "Armenian", "sq": "Albanian",
    "mk": "Macedonian", "bs": "Bosnian",
}
NLLB_LANG = {
    "pl": "pol_Latn", "en": "eng_Latn", "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn", "it": "ita_Latn",
    "cs": "ces_Latn", "sk": "slk_Latn", "uk": "ukr_Cyrl", "nl": "nld_Latn", "sv": "swe_Latn", "pt": "por_Latn",
    "hu": "hun_Latn", "ro": "ron_Latn", "lt": "lit_Latn", "lv": "lvs_Latn", "et": "est_Latn", "fi": "fin_Latn",
    "no": "nob_Latn", "da": "dan_Latn", "hr": "hrv_Latn", "sl": "slv_Latn", "sr": "srp_Cyrl", "bg": "bul_Cyrl",
    "el": "ell_Grek", "tr": "tur_Latn", "ru": "rus_Cyrl", "be": "bel_Cyrl", "ca": "cat_Latn", "eu": "eus_Latn",
    "gl": "glg_Latn", "ja": "jpn_Jpan", "zh": "zho_Hans", "ko": "kor_Hang", "ar": "arb_Arab", "he": "heb_Hebr",
    "vi": "vie_Latn", "th": "tha_Thai", "id": "ind_Latn", "is": "isl_Latn", "ka": "kat_Geor", "hy": "hye_Armn",
    "sq": "als_Latn", "mk": "mkd_Cyrl", "bs": "bos_Latn",
}

_STOP_SECTIONS_RE = re.compile(
    r"\n=+\s*(Przypisy|Bibliografia|Linki zewnętrzne|Zobacz też|Uwagi|Galeria|"
    r"References|See also|External links|Further reading|Notes|Gallery|Sources|"
    r"Literatur|Weblinks|Einzelnachweise|Siehe auch|Anmerkungen|"
    r"Notes et références|Voir aussi|Bibliographie|Liens externes|Annexes|"
    r"Note|Bibliografia|Voci correlate|Altri progetti|Collegamenti esterni|"
    r"Referencias|Véase también|Enlaces externos|Bibliografía|"
    r"Jegyzetek|Források|További információk|Kapcsolódó szócikkek|Irodalom|"
    r"Odkazy|Reference|Externí odkazy|Literatura|Související články|"
    r"Poznámky|Referencie|Externé odkazy|Pozri aj|"
    r"Примітки|Джерела|Посилання|Див\. також|Примечания|Ссылки|Литература|См\. также)\s*=+",
    re.I,
)


def clean_wiki_text(txt):
    """Usuwa artefakty Wikipedii: znaki zastępcze/kontrolne, zapisy IPA, [potrzebny przypis], puste nawiasy."""
    if not txt:
        return ""
    # usuń znaki kontrolne i U+FFFD (gdyby kiedyś pojawiły się w API)
    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]+(?:\d+[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]+)?", " ", txt)
    # usuń zapisy wymowy IPA i przyciski "posłuchaj"
    txt = re.sub(r"\s*\([^()]{0,120}\bIPA\b[^()]{0,120}\)", "", txt)
    txt = re.sub(r"\s*\(\s*(?:wym\.|posłuchaj|listen|audio)[^()]{0,100}\)", "", txt, flags=re.I)
    txt = re.sub(r"\b(?:posłuchaj|listen)\s*[ⓘℹ]?", "", txt, flags=re.I)
    txt = re.sub(r"\[\s*(?:\d+|potrzebny przypis|citation needed|weryfikacja)\s*\]", "", txt, flags=re.I)
    txt = re.sub(r"\(\s*[;,–—-]?\s*\)", "", txt)
    txt = re.sub(r"\s+([,.;:])", r"\1", txt)
    return re.sub(r"\s+", " ", txt).strip()


def _claim_qid(claims, prop):
    try:
        return claims[prop][0]["mainsnak"]["datavalue"]["value"]["id"]
    except Exception:
        return None


def _claim_all_qids(claims, prop):
    out = []
    for c in claims.get(prop) or []:
        try:
            q = c["mainsnak"]["datavalue"]["value"]["id"]
            if q:
                out.append(q)
        except Exception:
            pass
    return out


def _claim_amount(claims, prop):
    try:
        v = claims[prop][0]["mainsnak"]["datavalue"]["value"]["amount"]
        return float(str(v).lstrip("+"))
    except Exception:
        return None


class Wiki:
    def __init__(self, contact, log=print):
        self.ua = f"VanturaTileBuilder/2.2 (offline route planner tiles; contact: {contact})"
        self.log = log

    def _get(self, url, params):
        params = dict(params, format="json", maxlag="5")
        full = url + "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            try:
                req = urllib.request.Request(full, headers={"User-Agent": self.ua, "Accept-Encoding": "gzip"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read()
                    if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    data = json.loads(raw.decode("utf-8"))
                if data.get("error", {}).get("code") == "maxlag":
                    time.sleep(5 * (attempt + 1))
                    continue
                return data
            except urllib.error.HTTPError as e:
                wait = int(e.headers.get("Retry-After", "0") or 0) or 5 * (attempt + 1)
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(min(wait, 120))
                    continue
                return {}
            except Exception:
                time.sleep(5 * (attempt + 1))
        return {}

    def _wbgetentities_safe(self, chunk, extra_params):
        """Bezpieczne wbgetentities: gdy 1 błędne/usunięte QID z OSM wywoła 'no-such-entity',
        usuwa samo wadliwe ID i pobiera pozostałe 49 encji (nic nie ginie!)."""
        pending = [list(chunk)]
        entities = {}
        while pending:
            cur = pending.pop()
            if not cur:
                continue
            params = dict(extra_params, action="wbgetentities", ids="|".join(cur))
            data = self._get("https://www.wikidata.org/w/api.php", params)
            err = data.get("error") or {}
            if err.get("code") == "no-such-entity":
                bad_id = err.get("id")
                if bad_id and bad_id in cur:
                    rem = [q for q in cur if q != bad_id]
                    if rem:
                        pending.append(rem)
                elif len(cur) > 1:
                    mid = len(cur) // 2
                    pending.append(cur[mid:])
                    pending.append(cur[:mid])
                continue
            for qid, ent in (data.get("entities") or {}).items():
                if not ent.get("missing"):
                    entities[qid] = ent
        return entities

    def labels_pl(self, qids):
        """Pobiera polskie (albo angielskie jako fallback) etykiety dla zbioru QID (paczki po 50)."""
        out = {}
        qids = [q for q in dict.fromkeys(qids) if re.fullmatch(r"Q\d+", q or "")]
        for i in range(0, len(qids), 50):
            ents = self._wbgetentities_safe(qids[i:i + 50], {"props": "labels", "languages": "pl|en"})
            for qid, ent in ents.items():
                labs = ent.get("labels") or {}
                val = (labs.get("pl") or labs.get("en") or {}).get("value")
                if val:
                    out[qid] = val
            time.sleep(0.15)
        return out

    def sitelinks(self, qids):
        """QID -> sitelinki (pl, en, other), opisy (desc_pl, desc_en), etykieta PL, daty, styl, UNESCO, pasmo, twórca."""
        out = {}
        ref_qids = set()
        qids = [q for q in dict.fromkeys(qids) if re.fullmatch(r"Q\d+", q or "")]
        sitefilter = "|".join(f"{l}wiki" for l in ["pl", "en"] + OTHER_WIKI_LANGS)
        for i in range(0, len(qids), 50):
            ents = self._wbgetentities_safe(qids[i:i + 50], {
                "props": "sitelinks|descriptions|claims|labels",
                "sitefilter": sitefilter, "languages": "pl|en",
            })
            for qid, ent in ents.items():
                sl = ent.get("sitelinks") or {}
                rec = {k[:2]: v.get("title") for k, v in sl.items() if k in ("plwiki", "enwiki")}
                for lang in OTHER_WIKI_LANGS:
                    if f"{lang}wiki" in sl:
                        rec["other"] = (lang, sl[f"{lang}wiki"].get("title"))
                        break
                descs = ent.get("descriptions") or {}
                if (descs.get("pl") or {}).get("value"):
                    rec["desc_pl"] = descs["pl"]["value"]
                if (descs.get("en") or {}).get("value"):
                    rec["desc_en"] = descs["en"]["value"]
                lab = ((ent.get("labels") or {}).get("pl") or {}).get("value")
                if lab:
                    rec["label_pl"] = lab
                cl = ent.get("claims") or {}
                rec["inception"] = _wikidata_date(cl.get("P571"))
                rec["dissolved"] = _wikidata_date(cl.get("P576"))
                style_q = _claim_qid(cl, "P149")
                if style_q in STYLE_QID_PL:
                    rec["style_pl"] = STYLE_QID_PL[style_q]
                her_qs = _claim_all_qids(cl, "P1435")
                if "Q9259" in her_qs:
                    rec["unesco"] = True
                if "Q9329713" in her_qs:
                    rec["pomnik_historii"] = True
                ele = _claim_amount(cl, "P2044")
                if ele and 1 <= ele <= 9000:
                    rec["elevation"] = ele
                for key, prop in (("_mrange_q", "P4552"), ("_arch_q", "P84"), ("_creator_q", "P170")):
                    rq = _claim_qid(cl, prop)
                    if rq:
                        rec[key] = rq
                        ref_qids.add(rq)
                out[qid] = rec
            time.sleep(0.15)
        if ref_qids:
            ref_labels = self.labels_pl(list(ref_qids))
            for rec in out.values():
                if rec.get("_mrange_q") in ref_labels:
                    rec["mountain_range"] = ref_labels[rec["_mrange_q"]]
                if rec.get("_arch_q") in ref_labels:
                    rec["architect"] = ref_labels[rec["_arch_q"]]
                if rec.get("_creator_q") in ref_labels:
                    rec["creator"] = ref_labels[rec["_creator_q"]]
        return out

    def extracts(self, lang, titles):
        """tytuł -> wstęp artykułu jako zwykły tekst (paczki po 20)."""
        out = {}
        titles = list(dict.fromkeys(t for t in titles if t))
        for i in range(0, len(titles), 20):
            chunk = titles[i:i + 20]
            data = self._get(f"https://{lang}.wikipedia.org/w/api.php", {
                "action": "query", "prop": "extracts", "exintro": "1", "explaintext": "1", "redirects": "1",
                "exlimit": "20", "titles": "|".join(chunk),
            })
            q = data.get("query") or {}
            back = {}
            for n in q.get("normalized", []) + q.get("redirects", []):
                back[n["to"]] = back.get(n["from"], n["from"])
            for page in (q.get("pages") or {}).values():
                txt = clean_wiki_text(page.get("extract") or "")
                if txt:
                    title = page.get("title")
                    out[title] = txt
                    if title in back:
                        out[back[title]] = txt
            time.sleep(0.15)
        return out

    def article_start(self, lang, title):
        """Początek całego artykułu (do 10 zdań, także spoza wstępu) - na 'Czytaj więcej'.
        WAŻNE: exsectionformat='wiki' zwraca nagłówki '== Tytuł ==' (a NIE śmieci U+FFFD z 'raw')."""
        data = self._get(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "prop": "extracts", "exsentences": "10", "explaintext": "1",
            "exsectionformat": "wiki", "redirects": "1", "titles": title,
        })
        for page in ((data.get("query") or {}).get("pages") or {}).values():
            txt = page.get("extract") or ""
            txt = _STOP_SECTIONS_RE.split(txt)[0]
            txt = re.sub(r"\n=+[^=\n]+=+\s*", "\n", txt)
            txt = re.sub(r"^=+[^=\n]+=+\s*", "", txt)
            return clean_wiki_text(txt)
        return ""

    def geosearch(self, lang, lat, lon, radius=350, limit=50):
        data = self._get(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "list": "geosearch",
            "gscoord": f"{lat:.5f}|{lon:.5f}", "gsradius": str(min(10000, radius)), "gslimit": str(min(50, limit)),
        })
        return (data.get("query") or {}).get("geosearch") or []


def _wikidata_date(claims):
    """P571 / P576 -> '1889 r.' / 'XVIII w.'; None gdy brak lub niepewna precyzja."""
    try:
        v = claims[0]["mainsnak"]["datavalue"]["value"]
        m = re.match(r"([+-])(\d+)-", v["time"])
        year = int(m.group(2)) * (1 if m.group(1) == "+" else -1)
        prec = v.get("precision", 9)
        if year <= 0:
            return None
        if prec >= 9:
            return f"{year} r."
        if prec == 7:
            c = (year - 1) // 100 + 1
            return f"{ROMAN[c]} w." if c < len(ROMAN) else None
    except Exception:
        return None
    return None


ENGLISH_HINT = re.compile(r"\b(the|and|was|were|is|of|with|which|who|built|located|contains|visitors)\b", re.I)
FOREIGN_CHARS = re.compile(r"[őűäüößřěčďťňľĺŕůáéíúýâêîôûàèìòùãõñçøåæ]", re.I)
POLISH_WORDS = re.compile(r"\b(i|w|na|jest|się|oraz|przez|został|została|zostało|który|która|które|od|do|z|ze|nad|pod)\b", re.I)


def looks_polish(text):
    """Czy tekst jest po polsku (opis z mapy w innym języku nie trafia do opisu bez tłumaczenia)."""
    if FOREIGN_CHARS.search(text):
        return False
    if re.search(r"[ąćęłńśźż]", text, re.I):
        return True
    return len(POLISH_WORDS.findall(text)) >= 2 and not looks_english(text)


_OH_DAYS = {"Mo": "pon.", "Tu": "wt.", "We": "śr.", "Th": "czw.", "Fr": "pt.", "Sa": "sob.", "Su": "niedz.", "PH": "święta"}
_OH_MONTHS = {"Jan": "sty", "Feb": "lut", "Mar": "mar", "Apr": "kwi", "May": "maj", "Jun": "cze", "Jul": "lip",
              "Aug": "sie", "Sep": "wrz", "Oct": "paź", "Nov": "lis", "Dec": "gru"}


def opening_hours_pl(v):
    """Godziny otwarcia z zapisu OSM ("Nov-Mar Mo-Su 06:00-20:00") na polski; None gdy zapis nietypowy."""
    v = v.strip()
    if v == "24/7":
        return "całą dobę"
    if re.search(r"[a-z]{4,}|sunrise|sunset|\(|\|\|", v, re.I):
        return None
    out = re.sub(r"\b(Mo|Tu|We|Th|Fr|Sa|Su|PH)\b", lambda m: _OH_DAYS[m.group(1)], v)
    out = re.sub(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", lambda m: _OH_MONTHS[m.group(1)], out)
    out = re.sub(r"\boff\b|\bclosed\b", "nieczynne", out)
    out = out.replace(";", ",").replace("-", "–")
    if re.search(r"[A-Za-z]{2,}", re.sub(r"pon|wt|śr|czw|pt|sob|niedz|święta|nieczynne|sty|lut|mar|kwi|maj|cze|lip|sie|wrz|paź|lis|gru", "", out)):
        return None
    return out


# słowo pospolite w nazwie (w różnych językach) -> (polskie słowo, rodzaje obiektu, przy których wolno tłumaczyć)
_CAVE = {"cave_entrance"}
_CASTLE = {"castle", "ruins", "fort"}
_PALACE = {"palace", "manor", "castle"}
_FALL = {"waterfall"}
_SPRING = {"spring"}
_TOWER = {"observation_tower", "viewpoint"}
_VIEW = {"viewpoint"}
_MUSEUM = {"museum", "art_museum"}
_RUINS = {"ruins", "castle"}
NAME_WORDS = {
    "cave": ("Jaskinia", _CAVE), "cavern": ("Jaskinia", _CAVE), "höhle": ("Jaskinia", _CAVE),
    "barlang": ("Jaskinia", _CAVE), "zsomboly": ("Jaskinia", _CAVE), "jeskyně": ("Jaskinia", _CAVE),
    "jaskyňa": ("Jaskinia", _CAVE), "grotte": ("Grota", _CAVE), "grotta": ("Grota", _CAVE), "cueva": ("Jaskinia", _CAVE),
    "peștera": ("Jaskinia", _CAVE), "jama": ("Jaskinia", _CAVE), "špilja": ("Jaskinia", _CAVE), "pećina": ("Jaskinia", _CAVE),
    "castle": ("Zamek", _CASTLE), "burg": ("Zamek", _CASTLE), "burgruine": ("Ruiny zamku", _RUINS),
    "vár": ("Zamek", _CASTLE), "várrom": ("Ruiny zamku", _RUINS), "hrad": ("Zamek", _CASTLE),
    "zřícenina": ("Ruiny", _RUINS), "zrúcanina": ("Ruiny", _RUINS), "château": ("Zamek", _PALACE),
    "castillo": ("Zamek", _CASTLE), "castello": ("Zamek", _CASTLE), "schloss": ("Pałac", _PALACE),
    "palace": ("Pałac", _PALACE), "palast": ("Pałac", _PALACE), "kastély": ("Pałac", _PALACE),
    "zámek": ("Pałac", _PALACE), "zámok": ("Pałac", _PALACE), "kaštieľ": ("Dwór", _PALACE), "kúria": ("Dwór", _PALACE),
    "manor": ("Dwór", _PALACE), "ruine": ("Ruiny", _RUINS), "ruins": ("Ruiny", _RUINS), "rom": ("Ruiny", _RUINS),
    "waterfall": ("Wodospad", _FALL), "falls": ("Wodospad", _FALL), "wasserfall": ("Wodospad", _FALL),
    "vízesés": ("Wodospad", _FALL), "vodopád": ("Wodospad", _FALL), "cascada": ("Wodospad", _FALL),
    "cascata": ("Wodospad", _FALL), "cascade": ("Wodospad", _FALL),
    "spring": ("Źródło", _SPRING), "quelle": ("Źródło", _SPRING), "forrás": ("Źródło", _SPRING),
    "pramen": ("Źródło", _SPRING), "prameň": ("Źródło", _SPRING), "kút": ("Źródło", _SPRING),
    "kilátó": ("Wieża widokowa", _TOWER), "kilátótorony": ("Wieża widokowa", _TOWER),
    "aussichtsturm": ("Wieża widokowa", _TOWER), "rozhledna": ("Wieża widokowa", _TOWER),
    "rozhľadňa": ("Wieża widokowa", _TOWER), "lookout": ("Punkt widokowy", _VIEW),
    "aussichtspunkt": ("Punkt widokowy", _VIEW), "viewpoint": ("Punkt widokowy", _VIEW),
    "museum": ("Muzeum", _MUSEUM), "múzeum": ("Muzeum", _MUSEUM), "muzeum": ("Muzeum", _MUSEUM),
    "musée": ("Muzeum", _MUSEUM), "museo": ("Muzeum", _MUSEUM),
}
_NAME_GLUE = {"of", "the", "de", "la", "le", "les", "del", "della", "di", "da", "der", "die", "das", "des", "im", "am",
              "an", "auf", "zu", "von", "und", "and", "és", "a", "az", "na", "pod", "nad", "u", "v", "ve", "y", "e"}
MUSEUM_ADJ = {"mestské": "Miejskie", "městské": "Miejskie", "városi": "Miejskie", "city": "Miejskie", "town": "Miejskie",
              "municipal": "Miejskie", "stadt": "Miejskie", "helytörténeti": "Regionalne", "regionálne": "Regionalne",
              "regionální": "Regionalne", "vlastivedné": "Regionalne", "vlastivědné": "Regionalne", "regional": "Regionalne",
              "local": "Regionalne", "megyei": "Okręgowe", "národné": "Narodowe", "národní": "Narodowe", "nemzeti": "Narodowe",
              "national": "Narodowe", "zemské": "Krajowe", "landes": "Krajowe", "diecézne": "Diecezjalne", "egyházmegyei": "Diecezjalne"}


def polish_name(name, sub, wd_label=None):
    """Zachowuje nazwy własne bez zmian; tłumaczy jedynie słowo pospolite (np. Burg Forchtenstein -> Zamek Forchtenstein)
    albo używa oficjalnego polskiego egzonimu z Wikidata (np. Zamek Królewski na Wawelu)."""
    name = (name or "").strip()
    if not name:
        return None
    if wd_label and wd_label.strip() and wd_label.strip().lower() != name.lower() and not wd_label.strip().startswith("Q"):
        return wd_label.strip()
    tokens = name.split()
    if len(tokens) == 1 and "-" in name:
        head, _, tail = name.rpartition("-")
        w = NAME_WORDS.get(tail.lower())
        if w and sub in w[1] and head:
            return f"{w[0]} {head}"
        return None
    for pos in (0, len(tokens) - 1):
        w = NAME_WORDS.get(tokens[pos].lower().strip(",."))
        if not w or sub not in w[1] or len(tokens) < 2:
            continue
        rest = tokens[1:] if pos == 0 else tokens[:-1]
        if any(t.lower() in _NAME_GLUE for t in rest):
            return None
        if w[0] == "Muzeum" and len(rest) == 1 and rest[0].lower() in MUSEUM_ADJ:
            return f"Muzeum {MUSEUM_ADJ[rest[0].lower()]}"
        if any(t[:1].isalpha() and t[:1].islower() for t in rest):
            return None
        if not any(ch.isupper() for ch in "".join(rest)):
            return None
        return f"{w[0]} {' '.join(rest)}"
    return None


def looks_english(text):
    return len(ENGLISH_HINT.findall(text)) >= 2 and not re.search(r"[ąćęłńóśźż]", text, re.I)


PRIORITY_SUB = [
    "castle", "palace", "museum", "art_museum", "viewpoint", "waterfall", "ruins", "fort", "manor",
    "city_gate", "citywalls", "nature_reserve", "peak", "volcano", "monument", "observation_tower",
    "cave_entrance", "archaeological_site", "mine", "theatre", "gallery", "garden", "park", "statue", "sculpture",
]
GEOSEARCH_SUB = {
    "castle", "palace", "manor", "ruins", "fort", "museum", "art_museum", "gallery", "theatre",
    "monument", "peak", "volcano", "waterfall", "cave_entrance", "observation_tower", "archaeological_site",
    "mine", "city_gate", "citywalls", "battlefield", "planetarium", "concert_hall",
}

_ABBR = {
    "św", "pw", "ul", "im", "ok", "tzw", "r", "w", "wsp", "gen", "ks", "bł", "płk", "dr", "prof", "nr", "pl",
    "al", "zm", "ur", "in", "m", "n", "p", "pn", "pd", "wsch", "zach", "woj", "pow", "gm", "par", "kpt", "por",
    "mjr", "abp", "bp", "o", "s", "hr", "ppłk", "inż", "mgr", "tj", "np", "jw", "wg", "ds", "st", "mł", "kl", "ss",
    "e", "ew", "jun", "sen", "ang", "łac", "niem", "fr", "lit", "ukr", "czes", "ros", "dz", "op", "cyt", "red", "itd", "itp", "dot",
    "st", "mr", "mrs", "ms", "vs", "etc", "ca", "approx", "no", "fig", "vol", "ft", "sq", "mt", "jr", "sr",
}


def _split_sentences(txt):
    """Dzieli na zdania, ale nie po skrótach (św., pw., im., ok., m.in., r.) ani inicjałach (J. Piłsudski)."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻÄÖÜÉÈÊÁÍÓÚČŘŠŽ0-9„\"])", txt)
    out = []
    for p in parts:
        if out:
            last = re.search(r"(\S+)\.$", out[-1])
            word = last.group(1).lower().split(".")[-1] if last else ""
            year_end = re.search(r"(\d{3,4}|\b[IVXL]+)\s+(r|w)\.$", out[-1])
            if last and not year_end and (word in _ABBR or len(word) == 1 or out[-1].endswith("m.in.") or (word.isdigit() and len(word) <= 2)):
                out[-1] = out[-1] + " " + p
                continue
        out.append(p)
    return out


DESC_MAX_CHARS = int(os.environ.get("DESC_MAX_CHARS", "2000"))
DESC_MAX_SENTENCES = int(os.environ.get("DESC_MAX_SENTENCES", "10"))
SENTENCE_HARD_CAP = 1500


def full_sentences(txt, max_chars=None, max_sentences=None):
    """Tylko PEŁNE zdania (nigdy cięcia w środku zdania ani na skrócie), do max_chars i max_sentences."""
    max_chars = max_chars or DESC_MAX_CHARS
    max_sentences = max_sentences or DESC_MAX_SENTENCES
    txt = clean_wiki_text(txt or "")
    out = []
    for sent in _split_sentences(txt):
        sent = sent.strip()
        if not sent:
            continue
        if not out:
            if len(sent) > SENTENCE_HARD_CAP:
                return ""
            out.append(sent)
            continue
        if len(out) >= max_sentences or len(" ".join(out)) + 1 + len(sent) > max_chars:
            break
        out.append(sent)
    while len(out) > 1:
        m = re.search(r"(\S+)\.$", out[-1])
        w = m.group(1).lower().split(".")[-1] if m else ""
        if m and not re.search(r"(\d{3,4}|\b[IVXL]+)\s+(r|w)\.$", out[-1]) and (w in _ABBR or len(w) == 1 or out[-1].endswith("m.in.")):
            out.pop()
        else:
            break
    text = " ".join(out).strip()
    if text and not text.endswith((".", "!", "?", "”", "\"", ")")):
        closed = [x for x in out if x.endswith((".", "!", "?", "”", "\""))]
        text = " ".join(closed).strip()
    return text


def short_text(txt, max_chars=None):
    return full_sentences(txt, max_chars)


# ------------------------------------------------------------------ tłumaczenie (TranslateGemma / Ollama + NLLB-200)
def _extract_allowed_numbers(text):
    """Zbiór dozwolonych liczb z oryginału: uwzględnia separatory tysięcy (1.350 / 1,350 / 1 350),
    cyfry rzymskie I–XXI (wieki) oraz dekady (1920s -> 1920, 20)."""
    allowed = set()
    # surowe liczby
    for n in re.findall(r"\d+", text):
        allowed.add(n)
    # liczby z separatorami tysięcy: 1.350, 1,350, 1 350
    for m in re.findall(r"\d{1,3}(?:[.,\s\u00a0]\d{3})+", text):
        allowed.add(re.sub(r"[.,\s\u00a0]", "", m))
    # dekady typu 1920s / 1890-er -> dopuszcza dwucyfrową dekadę ("lata 20.") oraz wiek (XX / 20)
    for m in re.findall(r"\b(1\d)(\d0)s?\b", text):
        allowed.add(m[0] + m[1])
        allowed.add(m[1])
        allowed.add(str(int(m[0]) + 1))
    # lata 4-cyfrowe -> dopuszcza numer wieku (np. 1350 -> 14 / XIV)
    for y in re.findall(r"\b([12]\d{3})\b", text):
        allowed.add(str((int(y) - 1) // 100 + 1))
    # cyfry rzymskie (wieki) -> ich odpowiedniki arabskie
    for rm in re.findall(r"\b([IVX]{1,4})\b", text):
        if rm in ROMAN_TO_INT:
            allowed.add(str(ROMAN_TO_INT[rm]))
    return allowed


def numbers_ok(source, translated):
    """Każda liczba w tłumaczeniu musi wynikać z oryginału (ochrona przed zmyślonymi datami/wymiarami,
    ale bez fałszywego odrzucania formatowania tysięcy 1.350 -> 1350 czy wieków XIV -> 14)."""
    src = _extract_allowed_numbers(source)
    # normalizuj separatory tysięcy w tłumaczeniu
    tr_norm = re.sub(r"(?<=\d)[\s\u00a0](?=\d{3}\b)", "", translated)
    for n in re.findall(r"\d+", tr_norm):
        if n not in src:
            return False
    return True


TR_STATS = {}


def _tr_stat(key):
    TR_STATS[key] = TR_STATS.get(key, 0) + 1


def ollama_available(url):
    try:
        base = url.split("/api/")[0]
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


def ollama_unload(url, model):
    """Zwalnia model z pamięci RAM po zakończeniu etapu POI (przed uruchomieniem GraphHoppera)."""
    try:
        base = url.split("/api/")[0]
        body = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(base + "/api/generate", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception:
        pass


def translate_ollama(text, name, url, model, lang="en", source="wiki", min_len=20):
    """Tłumaczenie przez Ollamę: obsługuje oficjalny format TranslateGemma (wysoka jakość polskiego)
    oraz ogólne modele (np. qwen2.5:3b) jako fallback."""
    text = full_sentences(text, 2000, 10) or text[:2000]
    base = url.split("/api/")[0]
    is_tg = "translategemma" in (model or "").lower()
    src_code = lang if lang in LANG_NAMES_EN else "en"
    src_en = LANG_NAMES_EN.get(src_code, "English")

    try:
        if is_tg:
            prompt = (
                f"You are a professional {src_en} ({src_code}) to Polish (pl) translator. "
                f"Your goal is to accurately convey the meaning and nuances of the original {src_en} text "
                "while adhering to Polish grammar, vocabulary, and cultural sensitivities. "
                "Keep proper nouns (names of places, people, and monuments without standard Polish exonyms) unchanged. "
                "Produce only the Polish translation, without any additional explanations or commentary. "
                f"Please translate the following {src_en} text into Polish:\n\n\n{text}"
            )
            body = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "keep_alive": "5m",
                "options": {"temperature": 0.1, "num_predict": 900, "num_ctx": 4096},
            }).encode()
            req = urllib.request.Request(base + "/api/chat", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as r:
                out = (json.loads(r.read().decode("utf-8")).get("message") or {}).get("content", "").strip()
        else:
            where = f"z {LANG_NAMES_PL.get(lang, 'obcojęzycznej')} Wikipedii" if source == "wiki" else "(opis obiektu z mapy, w języku obcym)"
            prompt = (
                f"Przetłumacz poniższy tekst {where} na naturalny, ciekawy i poprawny język polski "
                "(zachowaj wszystkie ważne informacje, do 10 pełnych zdań). "
                "Nazwy własne (nazwiska, nazwy obiektów i miejscowości bez polskiej nazwy) zostaw bez zmian. "
                "Używaj WYŁĄCZNIE informacji z tekstu. Nie dodawaj żadnych faktów, dat, liczb ani ocen. "
                f"Obiekt: {name}.\nTEKST:\n{text}\nTŁUMACZENIE:"
            )
            body = json.dumps({
                "model": model, "prompt": prompt, "stream": False, "keep_alive": "5m",
                "options": {"temperature": 0.1, "num_predict": 900, "num_ctx": 4096},
            }).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=240) as r:
                out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
    except Exception as e:
        _tr_stat(f"llm:błąd ({type(e).__name__})")
        return None

    out = clean_wiki_text(out).strip('"')
    if len(out) < min_len:
        _tr_stat("llm:pusta")
        return None
    if not numbers_ok(text, out):
        _tr_stat("llm:odrzucone-liczby")
        return None
    res = full_sentences(out) if (source == "wiki" or len(out) >= 40) else out
    res = res or (out if source != "wiki" else None)
    _tr_stat("llm:udane" if res else "llm:bez-zdań")
    return res


def translate_en_pl(text, name, url, model, lang="en", source="wiki"):
    """Zgodność wsteczna."""
    return translate_ollama(text, name, url, model, lang, source)


# ------------------------------------------------------------------ bezpieczny Wikipedia GeoSearch
def geosearch_missing(pois, wiki, pl_title, en_title, other_title, log=print):
    """Dla niepowiązanych POI (bez wikidata/wikipedia) szuka artykułu w Wikipedii
    po współrzędnych (<= 180 m, szczyty <= 350 m) i ścisłej zgodności nazwy własnej (bez nazwy miasta)."""
    max_q = int(os.environ.get("WIKI_GEOSEARCH_MAX", "200"))
    if max_q <= 0:
        return 0

    cands = [
        i for i, p in enumerate(pois)
        if i not in pl_title and i not in en_title and i not in other_title
        and not p.get("wikidata")
        and p.get("sub_cat") in GEOSEARCH_SUB
        and p.get("name") and len(p["name"].strip()) >= 4
    ]
    if not cands:
        return 0

    # Wybierz języki do GeoSearch: zawsze PL (jeśli są polskie artykuły w kaflu) + dominujący lokalny + EN
    langs = []
    if len(pl_title) >= 5:
        langs.append("pl")
    lang_counts = {}
    for lang, _ in other_title.values():
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    if lang_counts:
        top_local = max(lang_counts, key=lang_counts.get)
        if top_local not in langs:
            langs.append(top_local)
    if "en" not in langs:
        langs.append("en")
    langs = langs[:2]

    # Klasteryzacja po komórkach ~600 m, żeby 1 zapytanie obsłużyło wszystkie sąsiednie POI
    clusters = {}
    for i in cands:
        key = (int(pois[i]["lat"] * 160), int(pois[i]["lng"] * 100))
        clusters.setdefault(key, []).append(i)
    ordered_clusters = sorted(clusters.values(), key=lambda idxs: -len(idxs))

    queries = 0
    found = 0
    for idxs in ordered_clusters:
        if queries >= max_q:
            break
        clat = sum(pois[i]["lat"] for i in idxs) / len(idxs)
        clon = sum(pois[i]["lng"] for i in idxs) / len(idxs)
        for lang in langs:
            if queries >= max_q:
                break
            unmatched = [i for i in idxs if i not in pl_title and i not in en_title and i not in other_title]
            if not unmatched:
                break
            items = wiki.geosearch(lang, clat, clon, radius=550, limit=50)
            queries += 1
            time.sleep(0.12)
            if not items:
                continue
            for i in unmatched:
                p = pois[i]
                t = p.get("tags") or {}
                city_excl = set()
                for cval in (t.get("addr:city"), t.get("addr:place"), t.get("is_in:city"), (p.get("_locality") or [None])[0]):
                    if cval:
                        city_excl.update(re.findall(r"[a-z0-9]{3,}", _strip_accents(cval)))
                ptoks = _name_tokens(p["name"], exclude=city_excl)
                if not ptoks:
                    continue
                max_d = 350.0 if p.get("sub_cat") in ("peak", "volcano") else 180.0
                best_title, best_dist = None, max_d
                for it in items:
                    ilat, ilon = it.get("lat"), it.get("lon")
                    if ilat is None or ilon is None:
                        continue
                    dist = math.hypot((p["lng"] - ilon) * 111320 * math.cos(math.radians(p["lat"])),
                                      (p["lat"] - ilat) * 110540)
                    if dist > best_dist:
                        continue
                    title = it.get("title") or ""
                    if _title_conflicts_with_poi(p["name"], p.get("sub_cat"), title):
                        continue
                    ttoks = _name_tokens(title, exclude=city_excl)
                    if not ttoks:
                        continue
                    ov = _stems_overlap(ptoks, ttoks)
                    need = 1 if (len(ptoks) == 1 and max(len(w) for w in ptoks) >= 5) else min(2, len(ptoks))
                    if ov >= need:
                        best_title, best_dist = title, dist
                if best_title:
                    if lang == "pl":
                        pl_title[i] = best_title
                    elif lang == "en":
                        en_title[i] = best_title
                    else:
                        other_title[i] = (lang, best_title)
                    found += 1
    if queries:
        log(f"  [GeoSearch] znaleziono brakujących artykułów Wikipedii: {found} (zapytań: {queries}, języki: {langs})")
    return found


def _txt_cache_key(lang, src_txt):
    return f"txt:{lang}:{hashlib.md5(src_txt.encode('utf-8')).hexdigest()[:12]}"


# ------------------------------------------------------------------ główna funkcja etapu
def enrich_descriptions(pois, log=print, places=None, geo_wiki=None, protected_areas=None,
                        tr_cache=None, save_cache=None, deadline_ts=None):
    """Ustawia p['description'] i p['desc_source'] ('wiki_pl' | 'wiki_<lang>_pl' | 'osm') dla KAŻDEGO POI.
    Bez rang i bez ucinania punktów: wszystkie obcojęzyczne artykuły Wikipedii, opisy OSM i opisy Wikidata
    są tłumaczone wyłącznie przez TranslateGemma 4B (Ollama). Jeśli bardzo gęsty kafel osiągnie limit czasu
    zadania (deadline_ts), zapisuje postęp w pamięci tłumaczeń (save_cache) i zwraca stats['incomplete']=True,
    aby kolejna runda dokończyła resztę bez powtarzania już przetłumaczonych punktów."""
    contact = os.environ.get("WIKI_CONTACT", "").strip()
    llm_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
    llm_model = os.environ.get("OLLAMA_MODEL", "translategemma:4b")
    llm_max = int(os.environ.get("LLM_MAX_PER_TILE", "999999"))
    stats = {"wiki_pl": 0, "wiki_en_pl": 0, "osm": 0, "incomplete": False}
    cache = dict(tr_cache) if tr_cache else {}
    local_tr = {}

    attach_locality(pois, places or [])
    attach_protected_area(pois, protected_areas or [])
    if geo_wiki:
        inherit_nearby_wiki(pois, geo_wiki, log)

    pl_title, en_title, osm_other_title = {}, {}, {}
    wiki = Wiki(contact, log) if contact else None
    if wiki:
        for i, p in enumerate(pois):
            wp = (p.get("tags") or {}).get("wikipedia") or p.get("wiki") or ""
            if ":" in wp:
                lang, title = wp.split(":", 1)
                lang, title = lang.strip().lower(), title.strip()
                if lang == "pl":
                    pl_title[i] = title
                elif lang == "en":
                    en_title[i] = title
                elif lang in OTHER_WIKI_LANGS:
                    osm_other_title[i] = (lang, title)
        qids = [p.get("wikidata") for p in pois if p.get("wikidata")]
        links = wiki.sitelinks(qids) if qids else {}
        if qids:
            log(f"  [Wikidata] pobrano encji: {len(links)}/{len(set(qids))}")
        for i, p in enumerate(pois):
            sl = links.get(p.get("wikidata") or "", {})
            if sl.get("pl"):
                pl_title.setdefault(i, sl["pl"])
            if sl.get("en"):
                en_title.setdefault(i, sl["en"])
            if p.get("wikidata") in links:
                p["_wd"] = links[p["wikidata"]]

        other_title = dict(osm_other_title)
        for i, p in enumerate(pois):
            o = (p.get("_wd") or {}).get("other")
            if o and o[1] and i not in pl_title and i not in en_title:
                other_title.setdefault(i, o)

        geosearch_missing(pois, wiki, pl_title, en_title, other_title, log)

        pl_txt = wiki.extracts("pl", list(pl_title.values())) if pl_title else {}
        need_en = [i for i in en_title if i not in pl_title or pl_title[i] not in pl_txt]
        en_txt = wiki.extracts("en", [en_title[i] for i in need_en]) if need_en else {}
        for i, p in enumerate(pois):
            o = (p.get("_wd") or {}).get("other")
            if o and o[1] and not (i in pl_title and pl_title[i] in pl_txt) and not (i in en_title and en_txt.get(en_title[i])):
                other_title.setdefault(i, o)
        other_active = {
            i: o for i, o in other_title.items()
            if not (i in pl_title and pl_title[i] in pl_txt) and not (i in en_title and en_txt.get(en_title[i]))
        }
        other_txt = {}
        for lang in sorted({o[0] for o in other_active.values()}):
            got = wiki.extracts(lang, [o[1] for o in other_active.values() if o[0] == lang])
            other_txt.update({(lang, t): v for t, v in got.items()})
        other_title = other_active
    else:
        log("  ⚠️ Brak WIKI_CONTACT - opisy tylko z danych OSM (poziom 3)")
        pl_txt, en_txt, other_title, other_txt = {}, {}, {}, {}

    has_pl = {i for i, t in pl_title.items() if pl_txt.get(t)}

    # "Czytaj więcej" (do 10 zdań) dla KAŻDEGO artykułu polskiej Wikipedii o krótkim wstępie (< 5 zdań)
    long_max = int(os.environ.get("WIKI_LONG_MAX_PER_TILE", "10000"))
    if wiki and long_max > 0:
        short_ones = [i for i in has_pl if len(_split_sentences(full_sentences(pl_txt[pl_title[i]]))) < 5]
        got = 0
        for i in short_ones[:long_max]:
            if deadline_ts and time.time() >= deadline_ts:
                break
            try:
                longer = wiki.article_start("pl", pl_title[i])
            except Exception:
                continue
            if len(_split_sentences(full_sentences(longer))) > len(_split_sentences(full_sentences(pl_txt[pl_title[i]]))):
                pl_txt[pl_title[i]] = longer
                got += 1
            time.sleep(0.06)
        if short_ones:
            log(f"  [Opisy] dłuższe teksty z polskiej Wikipedii (do 10 zdań): {got}/{min(len(short_ones), long_max)}")

    # Kolejki wszystkich punktów wymagających tłumaczenia (BEZ rang i BEZ ucinania kategorii!)
    en_queue = [i for i in en_title if i not in has_pl and en_txt.get(en_title[i])]
    other_queue = [i for i, o in other_title.items() if other_txt.get(o)]
    osm_tr_queue = [
        i for i, p in enumerate(pois)
        if i not in has_pl and i not in en_title and i not in other_title
        and ((p.get("tags") or {}).get("description:en") or (p.get("tags") or {}).get("description"))
        and not (p.get("tags") or {}).get("description:pl")
        and len(((p.get("tags") or {}).get("description:en") or (p.get("tags") or {}).get("description") or "")) > 20
        and not looks_polish((p.get("tags") or {}).get("description:en") or (p.get("tags") or {}).get("description") or "")
    ]
    wd_en_queue = [
        i for i, p in enumerate(pois)
        if i not in has_pl and i not in en_title and i not in other_title
        and (p.get("_wd") or {}).get("desc_en") and not (p.get("_wd") or {}).get("desc_pl")
    ]

    # Zastosuj istniejące tłumaczenia z pamięci (własnej z poprzedniej rundy + sąsiednich kafli)
    translated = {}
    cache_hits = 0
    todo_wiki = []
    seen_wiki_keys = set()
    for kind, i in ([("en", i) for i in en_queue] + [("other", i) for i in other_queue]):
        lang = "en" if kind == "en" else other_title[i][0]
        title = en_title[i] if kind == "en" else other_title[i][1]
        wkey = f"wiki:{lang}:{title}"
        wd_id = pois[i].get("wikidata")
        wdkey = f"wd:{wd_id}" if wd_id else None
        hit = cache.get(wkey) or (cache.get(wdkey) if wdkey else None)
        if hit and isinstance(hit, list) and len(hit) == 2 and hit[1]:
            translated[i] = hit[1]
            if hit[0] and hit[0] != "en":
                pois[i]["_tr_lang"] = hit[0]
            local_tr[wkey] = hit
            if wdkey:
                local_tr[wdkey] = hit
            cache_hits += 1
        else:
            todo_wiki.append((kind, i, lang, title, wkey, wdkey))

    todo_osm = []
    for i in osm_tr_queue:
        t = pois[i].get("tags") or {}
        src_d = t.get("description:en") or t.get("description")
        dlang = "en" if (t.get("description:en") or looks_english(src_d)) else "de"
        ckey = _txt_cache_key(dlang, src_d)
        hit = cache.get(ckey)
        if hit and isinstance(hit, list) and len(hit) == 2 and hit[1]:
            pois[i]["_desc_translated"] = hit[1]
            local_tr[ckey] = hit
            cache_hits += 1
        else:
            todo_osm.append((i, dlang, src_d, ckey))

    todo_wd = []
    for i in wd_en_queue:
        src_d = pois[i]["_wd"]["desc_en"]
        ckey = _txt_cache_key("en", src_d)
        hit = cache.get(ckey)
        if hit and isinstance(hit, list) and len(hit) == 2 and hit[1]:
            pois[i]["_wd"]["desc_en_pl"] = hit[1]
            local_tr[ckey] = hit
            cache_hits += 1
        else:
            todo_wd.append((i, src_d, ckey))

    if cache_hits:
        log(f"  [Cache tłumaczeń] odzyskano gotowych tłumaczeń (sąsiednie kafle / poprzednia runda): {cache_hits}")

    # Tłumaczenia wyłącznie przez TranslateGemma 4B (Ollama) — bez rang, KAŻDY punkt, z zapisem postępu
    TR_STATS.clear()
    t_tr_start = time.time()
    total_todo = len(todo_wiki) + len(todo_osm) + len(todo_wd)
    ollama_ok = llm_max > 0 and total_todo > 0 and ollama_available(llm_url)

    if ollama_ok:
        foreign_long_max = int(os.environ.get("WIKI_FOREIGN_LONG_MAX", "10000"))
        llm_done = 0
        since_save = 0
        consec_err = 0
        last_save_t = time.time()
        incomplete = False
        fgot = 0

        def checkpoint(force_save=False):
            nonlocal since_save, last_save_t
            if save_cache and local_tr and (force_save or since_save >= 20 or (time.time() - last_save_t) >= 600):
                save_cache(local_tr)
                since_save = 0
                last_save_t = time.time()

        # 1. Wszystkie obcojęzyczne artykuły Wikipedii (każdy do 10 zdań pod "Czytaj więcej")
        for idx_w, (kind, i, lang, title, wkey, wdkey) in enumerate(todo_wiki):
            if llm_done >= llm_max:
                break
            # Jeśli inny POI w tym samym kaflu już przetłumaczył ten sam artykuł chwilę temu — użyj od razu!
            hit = cache.get(wkey) or (cache.get(wdkey) if wdkey else None)
            if hit and isinstance(hit, list) and len(hit) == 2 and hit[1]:
                translated[i] = hit[1]
                if hit[0] and hit[0] != "en":
                    pois[i]["_tr_lang"] = hit[0]
                continue
            if deadline_ts and time.time() >= deadline_ts:
                incomplete = True
                log(f"  ⏸️ Limit czasu etapu tłumaczeń — zapisano postęp ({idx_w}/{len(todo_wiki)} artykułów Wikipedii); dokończy kolejna runda")
                break
            src_txt = en_txt[title] if kind == "en" else other_txt[(lang, title)]
            # Dobierz do 10 zdań tuż przed tłumaczeniem (tylko dla jeszcze nieprzetłumaczonego artykułu)
            if wiki and fgot < foreign_long_max and len(_split_sentences(full_sentences(src_txt))) < 4:
                try:
                    longer = wiki.article_start(lang, title)
                    if len(_split_sentences(full_sentences(longer))) > len(_split_sentences(full_sentences(src_txt))):
                        src_txt = longer
                        fgot += 1
                except Exception:
                    pass
                time.sleep(0.06)
            before_err = sum(v for k, v in TR_STATS.items() if k.startswith("llm:błąd"))
            tr = translate_ollama(src_txt, pois[i].get("name"), llm_url, llm_model, lang, "wiki")
            after_err = sum(v for k, v in TR_STATS.items() if k.startswith("llm:błąd"))
            if after_err > before_err:
                consec_err += 1
                if consec_err >= 5:
                    incomplete = True
                    log("  ⚠️ Ollama przestała odpowiadać (5 błędów pod rząd) — zapisuję postęp, dokończy kolejna runda")
                    break
            else:
                consec_err = 0
            llm_done += 1
            if tr:
                translated[i] = tr
                if kind == "other":
                    pois[i]["_tr_lang"] = lang
                entry = [lang, tr]
                cache[wkey] = entry
                local_tr[wkey] = entry
                if wdkey:
                    cache[wdkey] = entry
                    local_tr[wdkey] = entry
                since_save += 1
                checkpoint()

        # 2. Obcojęzyczne opisy z OSM (krótkie)
        if not incomplete:
            for idx_o, (i, dlang, src_d, ckey) in enumerate(todo_osm):
                if llm_done >= llm_max:
                    break
                if ckey in cache:
                    pois[i]["_desc_translated"] = cache[ckey][1]
                    continue
                if deadline_ts and time.time() >= deadline_ts:
                    incomplete = True
                    log(f"  ⏸️ Limit czasu etapu tłumaczeń — zapisano postęp ({idx_o}/{len(todo_osm)} opisów OSM); dokończy kolejna runda")
                    break
                tr = translate_ollama(src_d, pois[i].get("name"), llm_url, llm_model, dlang, "osm", min_len=12)
                llm_done += 1
                if tr:
                    pois[i]["_desc_translated"] = tr
                    entry = [dlang, tr]
                    cache[ckey] = entry
                    local_tr[ckey] = entry
                    since_save += 1
                    checkpoint()

        # 3. Krótkie angielskie opisy z Wikidata (desc_en) — dają konkretny kontekst punktom bez Wikipedii
        if not incomplete:
            for idx_d, (i, src_d, ckey) in enumerate(todo_wd):
                if llm_done >= llm_max:
                    break
                if ckey in cache:
                    pois[i]["_wd"]["desc_en_pl"] = cache[ckey][1]
                    continue
                if deadline_ts and time.time() >= deadline_ts:
                    incomplete = True
                    log(f"  ⏸️ Limit czasu etapu tłumaczeń — zapisano postęp ({idx_d}/{len(todo_wd)} opisów Wikidata); dokończy kolejna runda")
                    break
                tr = translate_ollama(src_d, pois[i].get("name"), llm_url, llm_model, "en", "osm", min_len=8)
                llm_done += 1
                if tr:
                    pois[i]["_wd"]["desc_en_pl"] = tr
                    entry = ["en", tr]
                    cache[ckey] = entry
                    local_tr[ckey] = entry
                    since_save += 1
                    checkpoint()

        ollama_unload(llm_url, llm_model)
        checkpoint(force_save=True)
        log(f"  [Tłumaczenia TranslateGemma] do przetłumaczenia: {len(todo_wiki)} Wiki + {len(todo_osm)} OSM + {len(todo_wd)} Wikidata "
            f"| z cache: {cache_hits} | rozszerzonych do 10 zdań: {fgot} | wynik: {TR_STATS or 'brak prób'} | {time.time() - t_tr_start:.0f} s")
        if incomplete:
            stats["incomplete"] = True
            return stats
    else:
        if save_cache and local_tr:
            save_cache(local_tr)
        if llm_max > 0 and total_todo > 0:
            log(f"  ⚠️ Model TranslateGemma niedostępny ({llm_url}) - {total_todo} opisów bez tłumaczenia")

    for i, p in enumerate(pois):
        desc, src = None, None
        t = pl_title.get(i)
        if i in has_pl:
            desc, src = full_sentences(pl_txt[t]), "wiki_pl"
        elif i in translated:
            desc, src = translated[i], f"wiki_{p.get('_tr_lang', 'en')}_pl"
        if not desc or len(desc) < 25:
            desc, src = describe_from_osm(p), "osm"
        p["description"] = desc
        p["desc_source"] = src

    # Polskie nazwy ogólne (nazwy własne zostają bez zmian, oryginał zachowany w name_local)
    renamed = 0
    for p in pois:
        pl = polish_name(p.get("name"), p.get("sub_cat"), (p.get("_wd") or {}).get("label_pl"))
        if pl and pl != p.get("name"):
            p["name_local"] = p["name"]
            p["name"] = pl
            renamed += 1
    if renamed:
        log(f"  [Nazwy] po polsku: {renamed} (nazwy własne bez zmian, oryginały zachowane)")

    # Nazwy z samych cyfr ("1956") -> "Miejsce pamięci 1956"
    for p in pois:
        if re.fullmatch(r"[\d\s.,/–-]+", p.get("name") or ""):
            kind = TYPE_PL.get(p.get("sub_cat") or "", "Atrakcja")
            if p.get("sub_cat") == "memorial" and (p.get("tags") or {}).get("memorial") in MEMORIAL_PL:
                kind = MEMORIAL_PL[p["tags"]["memorial"]]
            p["name"] = f"{kind} {p['name'].strip()}"

    # Deduplikacja: (1) ta sama nazwa i rodzaj w promieniu ~60 m, (2) ten sam wikidata_id w promieniu ~150 m
    best = {}
    for idx, p in enumerate(pois):
        key = ((p.get("name") or "").strip().lower(), p.get("sub_cat"), round(p["lat"] / 0.0006), round(p["lng"] / 0.0009))
        score = (p["desc_source"].startswith("wiki"), bool(p.get("wikidata")), len(p.get("description") or ""))
        if key not in best or score > best[key][0]:
            best[key] = (score, idx)
    keep1 = [pois[idx] for _, idx in sorted(best.values(), key=lambda x: x[1])]

    best_wd = {}
    final_pois = []
    for p in keep1:
        wd_id = p.get("wikidata")
        if wd_id:
            wkey = (wd_id, round(p["lat"] / 0.0015), round(p["lng"] / 0.0022))
            sub_rank = -(PRIORITY_SUB.index(p["sub_cat"]) if p.get("sub_cat") in PRIORITY_SUB else len(PRIORITY_SUB))
            score = (p["desc_source"].startswith("wiki"), sub_rank, len(p.get("description") or ""))
            if wkey not in best_wd:
                best_wd[wkey] = (score, len(final_pois))
                final_pois.append(p)
            elif score > best_wd[wkey][0]:
                final_pois[best_wd[wkey][1]] = p
                best_wd[wkey] = (score, best_wd[wkey][1])
        else:
            final_pois.append(p)

    removed = len(pois) - len(final_pois)
    pois[:] = final_pois
    if removed:
        log(f"  [Opisy] usunięto duplikaty: {removed}")

    for p in pois:
        key = p["desc_source"] if p["desc_source"] in stats else "wiki_en_pl"
        stats[key] += 1
    log(f"  [Opisy] Wikipedia PL: {stats['wiki_pl']} | tłumaczenia EN/inne->PL: {stats['wiki_en_pl']} "
        f"(w tym z innych języków niż EN: {sum(1 for p in pois if p['desc_source'].startswith('wiki_') and p['desc_source'] not in ('wiki_pl', 'wiki_en_pl'))}) | "
        f"z danych OSM/Wikidata: {stats['osm']}")
    return stats
