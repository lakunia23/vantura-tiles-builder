#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VANTURA - OPISY ATRAKCJI (etap budowy kafla, zastępuje dawną Maszynę 2)

Zasada: opis ma być PRAWDZIWY i ciekawy. Trzy poziomy, od najlepszego:
  1. Polska Wikipedia  - wstęp artykułu (pełne zdania, do 10 zdań - w aplikacji 2-3 + "Czytaj więcej"). Artykuł wskazany przez tag wikidata/wikipedia
                         z OpenStreetMap (bez zgadywania po nazwie - zero pomyłek "nie ten obiekt").
  2. Angielska Wikipedia - lokalny model (Ollama) TŁUMACZY i streszcza wyłącznie ten tekst.
                         Kontrola: jeśli w tłumaczeniu pojawi się liczba, której nie ma w oryginale,
                         tłumaczenie jest odrzucane (model nie może dopisać dat ani wymiarów).
  3. Dane OpenStreetMap - opis złożony z prawdziwych tagów: rodzaj obiektu, czas powstania, rejestr
                         zabytków, architekt/autor, wysokość, napis, opis z OSM, wstęp, godziny otwarcia.
                         Każde zdanie wynika z konkretnego tagu - bez przymiotników "na zachętę".
Bez zdjęć (image_url = NULL) - aplikacja pobiera zdjęcie sama, gdy użytkownik otworzy atrakcję.

Zmienne (.env):
  WIKI_CONTACT=twoj@email   (WYMAGANE dla poziomów 1-2: zasady Wikimedia wymagają kontaktu w nagłówku
                             User-Agent; bez tego poziomy 1-2 są pomijane, zostaje poziom 3)
  OLLAMA_URL=http://localhost:11434/api/generate, OLLAMA_MODEL=qwen2.5:3b, LLM_MAX_PER_TILE=20
  (LLM_MAX_PER_TILE=0 wyłącza tłumaczenie; wtedy obiekty tylko z angielską Wikipedią dostają poziom 3)
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

from extract_pois import TilePoiScanner

KEEP_TAGS = (
    "historic", "tourism", "natural", "leisure", "amenity", "building", "religion", "denomination",
    "start_date", "construction_date", "year_of_construction", "heritage", "architect", "artist_name", "artist",
    "inscription", "description", "description:pl", "memorial", "memorial:type", "castle_type", "ruins",
    "artwork_type", "material", "height", "ele", "operator", "fee", "opening_hours", "addr:city", "addr:place",
    "is_in:city", "wikipedia", "wikidata", "name:pl", "museum", "subject", "tower:type", "species", "genus",
    "protect_class", "denotation", "cave:ref", "length", "direction",
)


class RichTilePoiScanner(TilePoiScanner):
    """Jak TilePoiScanner, ale zachowuje tagi z faktami potrzebnymi do opisu oraz zbiera miejscowości
    (do prawdziwego opisu położenia: "ok. 3 km na północ od miejscowości X")."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.places = []

    def _attach(self, before, tags):
        if len(self.pois) > before:
            self.pois[-1]["tags"] = {k: tags.get(k) for k in KEEP_TAGS if tags.get(k)}

    def node(self, n):
        before = len(self.pois)
        super().node(n)
        self._attach(before, n.tags)
        if n.tags.get("place") in ("city", "town", "village") and n.tags.get("name"):
            self.places.append((n.location.lat, n.location.lon, n.tags.get("name:pl") or n.tags.get("name"),
                                n.tags.get("place")))

    def way(self, w):
        before = len(self.pois)
        super().way(w)
        self._attach(before, w.tags)


# ------------------------------------------------------------------ poziom 3: opis z tagów OSM (prawdziwy)
TYPE_PL = {
    "castle": "Zamek", "palace": "Pałac", "manor": "Dwór", "ruins": "Ruiny", "fort": "Fort",
    "monument": "Pomnik", "memorial": "Miejsce pamięci", "archaeological_site": "Stanowisko archeologiczne",
    "mine": "Dawna kopalnia", "city_gate": "Brama miejska", "citywalls": "Mury miejskie",
    "battlefield": "Pole bitwy", "heritage": "Obiekt zabytkowy", "pillbox": "Schron bojowy", "bunker": "Bunkier",
    "museum": "Muzeum", "art_museum": "Muzeum sztuki", "city_square": "Plac", "gallery": "Galeria sztuki",
    "theatre": "Teatr", "arts_centre": "Centrum sztuki", "planetarium": "Planetarium", "concert_hall": "Sala koncertowa",
    "artwork": "Dzieło sztuki", "sculpture": "Rzeźba", "statue": "Posąg", "mural": "Mural", "installation": "Instalacja artystyczna",
    "mosaic": "Mozaika", "street_art": "Sztuka uliczna", "viewpoint": "Punkt widokowy", "observation_tower": "Wieża widokowa",
    "peak": "Szczyt", "volcano": "Wulkan", "waterfall": "Wodospad", "cave_entrance": "Wejście do jaskini",
    "cliff": "Urwisko", "rock": "Skała", "stone": "Głaz", "gorge": "Wąwóz", "spring": "Źródło", "beach": "Plaża",
    "wood": "Las", "wetland": "Mokradła", "dune": "Wydma", "glacier": "Lodowiec", "cape": "Przylądek",
    "park": "Park", "garden": "Ogród", "village_green": "Skwer", "nature_reserve": "Obszar chroniony",
}
MEMORIAL_PL = {"plaque": "Tablica pamiątkowa", "stolperstein": "Kamień pamięci (Stolperstein)", "statue": "Pomnik",
               "bust": "Popiersie", "stone": "Kamień pamiątkowy", "cross": "Krzyż pamiątkowy", "war_memorial": "Pomnik wojenny",
               "obelisk": "Obelisk", "stele": "Stela"}
DENOM_PL = {"catholic": "rzymskokatolickie", "roman_catholic": "rzymskokatolickie", "orthodox": "prawosławne",
            "lutheran": "ewangelicko-augsburskie", "evangelical": "ewangelickie", "greek_catholic": "greckokatolickie",
            "protestant": "protestanckie", "jewish": "żydowskie"}
RELIGION_PL = {"christian": "chrześcijańskie", "jewish": "żydowskie", "muslim": "muzułmańskie", "buddhist": "buddyjskie"}
ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII", "XIII", "XIV", "XV", "XVI",
         "XVII", "XVIII", "XIX", "XX", "XXI"]


def _date_pl(v):
    v = v.strip()
    m = re.fullmatch(r"~?\s*(\d{3,4})(?:-\d{2}(?:-\d{2})?)?", v)
    if m:
        return f"{int(m.group(1))} r."
    m = re.fullmatch(r"C(\d{1,2})", v, re.I)
    if m and 0 < int(m.group(1)) < len(ROMAN):
        return f"{ROMAN[int(m.group(1))]} w."
    m = re.fullmatch(r"(\d{3,4})\.\.(\d{3,4})", v)
    if m:
        return f"lat {m.group(1)}-{m.group(2)}"
    m = re.fullmatch(r"(\d{3,4})s", v)
    if m:
        return f"lat {m.group(1)}."
    return None


DIRS_PL = ["na północ", "na północny wschód", "na wschód", "na południowy wschód",
           "na południe", "na południowy zachód", "na zachód", "na północny zachód"]


def attach_locality(pois, places):
    """Dla każdego POI: najbliższa miejscowość (do 8 km), odległość i kierunek - wyliczone z mapy."""
    if not places:
        return
    import math
    grid = {}
    for pl in places:
        grid.setdefault((int(pl[0] * 10), int(pl[1] * 10)), []).append(pl)
    for p in pois:
        la, lo = p["lat"], p["lng"]
        best, bd = None, 1e18
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for pl in grid.get((int(la * 10) + dy, int(lo * 10) + dx), []):
                    x = (lo - pl[1]) * 111320 * math.cos(math.radians(la)); y = (la - pl[0]) * 110540
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


def describe_from_osm(p):
    t = p.get("tags") or {}
    wd = p.get("_wd") or {}
    sub = p.get("sub_cat") or ""
    sent = []
    kind = TYPE_PL.get(sub, "Atrakcja")
    if sub == "memorial" and t.get("memorial") in MEMORIAL_PL:
        kind = MEMORIAL_PL[t["memorial"]]
    city = t.get("addr:city") or t.get("addr:place") or t.get("is_in:city")
    loc = p.get("_locality")
    if city and city != p.get("name"):
        first = f"{kind} w miejscowości {city}"
    elif loc and loc[1] < 700:
        first = f"{kind} w miejscowości {loc[0]}"
    elif loc:
        km = loc[1] / 1000
        kmtxt = f"{km:.0f}" if km >= 1.5 else f"{km:.1f}".replace(".", ",")
        first = f"{kind} ok. {kmtxt} km {loc[2]} od miejscowości {loc[0]}"
    else:
        first = kind
    sent.append(first + ".")
    if wd.get("desc_pl") and len(wd["desc_pl"]) > 8:
        dp = wd["desc_pl"].strip()
        sent.append(dp[0].upper() + dp[1:])
    d = None
    for k in ("start_date", "construction_date", "year_of_construction"):
        if t.get(k):
            d = _date_pl(t[k])
            if d:
                break
    if not d and wd.get("inception"):
        d = wd["inception"]
    if d:
        sent.append(f"Pochodzi z {d}")
    if t.get("heritage"):
        sent.append("Figuruje w rejestrze zabytków")
    if t.get("denomination") in DENOM_PL:
        sent.append(f"Wyznanie: {DENOM_PL[t['denomination']]}")
    elif t.get("religion") in RELIGION_PL:
        sent.append(f"Wyznanie: {RELIGION_PL[t['religion']]}")
    if t.get("architect"):
        sent.append(f"Projekt: {t['architect']}")
    artist = t.get("artist_name") or t.get("artist")
    if artist:
        sent.append(f"Autor: {artist}")
    ele = p.get("elevation") or (_num(t["ele"]) if t.get("ele") else None)
    if ele and sub in ("peak", "volcano", "viewpoint", "observation_tower", "cave_entrance", "spring", "rock", "cliff", "glacier"):
        sent.append(f"Wysokość: {int(float(ele))} m n.p.m.")
    h = _num(t.get("height", "")) if t.get("height") else None
    if h and float(h) >= 3:
        sent.append(f"Wysokość obiektu: {h.rstrip('0').rstrip('.') if '.' in h else h} m")
    if t.get("species"):
        sent.append(f"Gatunek: {t['species']}")
    if t.get("inscription"):
        ins = re.sub(r"\s+", " ", t["inscription"]).strip()
        if len(ins) <= 200:  # dłuższy napis pomijamy zamiast go urywać
            sent.append(f"Napis: „{ins}”")
    desc = t.get("description:pl") or p.get("_desc_translated") or t.get("description")
    if desc and not t.get("description:pl") and not p.get("_desc_translated") and not looks_polish(desc):
        desc = None  # opis z mapy w innym języku tylko po przetłumaczeniu (z kontrolą liczb)
    if desc and len(desc) > 15:
        desc = full_sentences(desc, 600, 4) or (desc.strip() if len(desc) <= 300 else "")
        if desc:
            sent.append(desc.rstrip("."))
    if t.get("fee") == "no":
        sent.append("Wstęp bezpłatny")
    elif t.get("fee") == "yes":
        sent.append("Wstęp płatny")
    if t.get("opening_hours") and len(t["opening_hours"]) <= 60:
        oh = opening_hours_pl(t["opening_hours"])
        if oh:
            sent.append(f"Godziny otwarcia: {oh}")
    # całe zdania; gdy za długo - pomijamy ostatnie zdania, nigdy nie tniemy w środku
    parts = [x if x.endswith((".", "”", "!", "?")) else x + "." for x in sent if x]
    out = []
    for x in parts:
        if out and len(" ".join(out)) + 1 + len(x) > DESC_MAX_CHARS:
            break
        out.append(x)
    return " ".join(out)


# ------------------------------------------------------------------ poziomy 1-2: Wikipedia
class Wiki:
    def __init__(self, contact, log=print):
        self.ua = f"VanturaTileBuilder/2.0 (offline route planner tiles; contact: {contact})"
        self.log = log

    def _get(self, url, params):
        params = dict(params, format="json", maxlag="5")
        full = url + "?" + urllib.parse.urlencode(params)
        for attempt in range(6):
            try:
                req = urllib.request.Request(full, headers={"User-Agent": self.ua, "Accept-Encoding": "identity"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = json.loads(r.read().decode("utf-8"))
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

    def sitelinks(self, qids):
        """QID -> {'pl': tytuł, 'en': tytuł, 'other': (język, tytuł), 'desc_pl': krótki opis po polsku,
        'inception': '1889 r.'} (paczki po 50). 'other' = pierwsza dostępna Wikipedia z OTHER_WIKI_LANGS."""
        out = {}
        qids = [q for q in dict.fromkeys(qids) if re.fullmatch(r"Q\d+", q or "")]
        for i in range(0, len(qids), 50):
            data = self._get("https://www.wikidata.org/w/api.php", {
                "action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": "sitelinks|descriptions|claims|labels",
                "sitefilter": "|".join(f"{l}wiki" for l in ["pl", "en"] + OTHER_WIKI_LANGS), "languages": "pl"})
            for qid, ent in (data.get("entities") or {}).items():
                sl = ent.get("sitelinks") or {}
                rec = {k[:2]: v.get("title") for k, v in sl.items() if k in ("plwiki", "enwiki")}
                for lang in OTHER_WIKI_LANGS:
                    if f"{lang}wiki" in sl:
                        rec["other"] = (lang, sl[f"{lang}wiki"].get("title"))
                        break
                d = ((ent.get("descriptions") or {}).get("pl") or {}).get("value")
                if d:
                    rec["desc_pl"] = d
                lab = ((ent.get("labels") or {}).get("pl") or {}).get("value")
                if lab:
                    rec["label_pl"] = lab
                rec["inception"] = _wikidata_date((ent.get("claims") or {}).get("P571"))
                out[qid] = rec
            time.sleep(0.2)
        return out

    def extracts(self, lang, titles):
        """tytuł -> wstęp artykułu jako zwykły tekst (paczki po 20)."""
        out = {}
        titles = list(dict.fromkeys(t for t in titles if t))
        for i in range(0, len(titles), 20):
            chunk = titles[i:i + 20]
            data = self._get(f"https://{lang}.wikipedia.org/w/api.php", {
                "action": "query", "prop": "extracts", "exintro": "1", "explaintext": "1", "redirects": "1",
                "exlimit": "20", "titles": "|".join(chunk)})
            q = data.get("query") or {}
            back = {}
            for n in q.get("normalized", []) + q.get("redirects", []):
                back[n["to"]] = back.get(n["from"], n["from"])
            for page in (q.get("pages") or {}).values():
                txt = page.get("extract")
                if txt:
                    title = page.get("title")
                    out[title] = txt
                    if title in back:
                        out[back[title]] = txt
            time.sleep(0.2)
        return out

    def article_start(self, lang, title):
        """Początek całego artykułu (do 10 zdań, także spoza wstępu) - na "Czytaj więcej".
        Nagłówki sekcji są usuwane; tekst kończy się przed przypisami/bibliografią."""
        data = self._get(f"https://{lang}.wikipedia.org/w/api.php", {
            "action": "query", "prop": "extracts", "exsentences": "10", "explaintext": "1",
            "exsectionformat": "raw", "redirects": "1", "titles": title})
        for page in ((data.get("query") or {}).get("pages") or {}).values():
            txt = page.get("extract") or ""
            txt = re.split(r"\n=+\s*(Przypisy|Bibliografia|Linki zewnętrzne|Zobacz też|References|See also|External links)\s*=+", txt)[0]
            txt = re.sub(r"\n=+[^=\n]+=+\s*", "\n", txt)
            return txt.strip()
        return ""


# Wikipedie w innych językach (tłumaczone lokalnym modelem, z kontrolą liczb) - gdy brak PL i EN.
# Kolejność = większe i rzetelniejsze wersje językowe najpierw.
OTHER_WIKI_LANGS = ["de", "fr", "es", "it", "cs", "sk", "uk", "nl", "sv", "pt", "hu", "ro", "lt", "lv", "et", "fi",
                    "no", "da", "hr", "sl", "sr", "bg", "el", "tr", "ru", "be", "ca", "eu", "gl", "ja", "zh", "ko"]
LANG_NAMES_PL = {"en": "angielskiej", "de": "niemieckiej", "fr": "francuskiej", "es": "hiszpańskiej", "it": "włoskiej",
                 "cs": "czeskiej", "sk": "słowackiej", "uk": "ukraińskiej", "nl": "niderlandzkiej", "sv": "szwedzkiej",
                 "pt": "portugalskiej", "hu": "węgierskiej", "ro": "rumuńskiej", "lt": "litewskiej", "lv": "łotewskiej",
                 "et": "estońskiej", "fi": "fińskiej", "no": "norweskiej", "da": "duńskiej", "hr": "chorwackiej",
                 "sl": "słoweńskiej", "sr": "serbskiej", "bg": "bułgarskiej", "el": "greckiej", "tr": "tureckiej",
                 "ru": "rosyjskiej", "be": "białoruskiej", "ca": "katalońskiej", "eu": "baskijskiej",
                 "gl": "galicyjskiej", "ja": "japońskiej", "zh": "chińskiej", "ko": "koreańskiej"}


def _wikidata_date(claims):
    """P571 (data powstania) -> '1889 r.' / 'XVIII w.'; None gdy brak lub niepewna precyzja."""
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
        return None  # komentarze/wyrażenia - nie zgadujemy
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
    # jaskinie
    "cave": ("Jaskinia", _CAVE), "cavern": ("Jaskinia", _CAVE), "höhle": ("Jaskinia", _CAVE),
    "barlang": ("Jaskinia", _CAVE), "zsomboly": ("Jaskinia", _CAVE), "jeskyně": ("Jaskinia", _CAVE),
    "jaskyňa": ("Jaskinia", _CAVE), "grotte": ("Grota", _CAVE), "grotta": ("Grota", _CAVE), "cueva": ("Jaskinia", _CAVE),
    "peștera": ("Jaskinia", _CAVE), "jama": ("Jaskinia", _CAVE), "špilja": ("Jaskinia", _CAVE), "pećina": ("Jaskinia", _CAVE),
    # zamki, pałace, ruiny
    "castle": ("Zamek", _CASTLE), "burg": ("Zamek", _CASTLE), "burgruine": ("Ruiny zamku", _RUINS),
    "vár": ("Zamek", _CASTLE), "várrom": ("Ruiny zamku", _RUINS), "hrad": ("Zamek", _CASTLE),
    "zřícenina": ("Ruiny", _RUINS), "zrúcanina": ("Ruiny", _RUINS), "château": ("Zamek", _PALACE),
    "castillo": ("Zamek", _CASTLE), "castello": ("Zamek", _CASTLE), "schloss": ("Pałac", _PALACE),
    "palace": ("Pałac", _PALACE), "palast": ("Pałac", _PALACE), "kastély": ("Pałac", _PALACE),
    "zámek": ("Pałac", _PALACE), "zámok": ("Pałac", _PALACE), "kaštieľ": ("Dwór", _PALACE), "kúria": ("Dwór", _PALACE),
    "manor": ("Dwór", _PALACE), "ruine": ("Ruiny", _RUINS), "ruins": ("Ruiny", _RUINS), "rom": ("Ruiny", _RUINS),
    # woda
    "waterfall": ("Wodospad", _FALL), "falls": ("Wodospad", _FALL), "wasserfall": ("Wodospad", _FALL),
    "vízesés": ("Wodospad", _FALL), "vodopád": ("Wodospad", _FALL), "cascada": ("Wodospad", _FALL),
    "cascata": ("Wodospad", _FALL), "cascade": ("Wodospad", _FALL),
    "spring": ("Źródło", _SPRING), "quelle": ("Źródło", _SPRING), "forrás": ("Źródło", _SPRING),
    "pramen": ("Źródło", _SPRING), "prameň": ("Źródło", _SPRING), "kút": ("Źródło", _SPRING),
    # widoki
    "kilátó": ("Wieża widokowa", _TOWER), "kilátótorony": ("Wieża widokowa", _TOWER),
    "aussichtsturm": ("Wieża widokowa", _TOWER), "rozhledna": ("Wieża widokowa", _TOWER),
    "rozhľadňa": ("Wieża widokowa", _TOWER), "lookout": ("Punkt widokowy", _VIEW),
    "aussichtspunkt": ("Punkt widokowy", _VIEW), "viewpoint": ("Punkt widokowy", _VIEW),
    # muzea
    "museum": ("Muzeum", _MUSEUM), "múzeum": ("Muzeum", _MUSEUM), "muzeum": ("Muzeum", _MUSEUM),
    "musée": ("Muzeum", _MUSEUM), "museo": ("Muzeum", _MUSEUM),
}
# słowa świadczące o złożonej nazwie ("Cave of the Winds", "Grotte de la ...") - wtedy nie tłumaczymy
_NAME_GLUE = {"of", "the", "de", "la", "le", "les", "del", "della", "di", "da", "der", "die", "das", "des", "im", "am",
              "an", "auf", "zu", "von", "und", "and", "és", "a", "az", "na", "pod", "nad", "u", "v", "ve", "y", "e"}


# przymiotniki pospolite przy muzeach ("Mestské múzeum" -> "Muzeum Miejskie")
MUSEUM_ADJ = {"mestské": "Miejskie", "městské": "Miejskie", "városi": "Miejskie", "city": "Miejskie", "town": "Miejskie",
              "municipal": "Miejskie", "stadt": "Miejskie", "helytörténeti": "Regionalne", "regionálne": "Regionalne",
              "regionální": "Regionalne", "vlastivedné": "Regionalne", "vlastivědné": "Regionalne", "regional": "Regionalne",
              "local": "Regionalne", "megyei": "Okręgowe", "národné": "Narodowe", "národní": "Narodowe", "nemzeti": "Narodowe",
              "national": "Narodowe", "zemské": "Krajowe", "landes": "Krajowe", "diecézne": "Diecezjalne", "egyházmegyei": "Diecezjalne"}


def polish_name(name, sub, wd_label=None):
    """Nazwa po polsku: etykieta z Wikidata albo przetłumaczone słowo pospolite + nazwa własna bez zmian.
    "Mammoth Cave" -> "Jaskinia Mammoth", "Pipa-zsomboly" -> "Jaskinia Pipa", "Burg Forchtenstein" -> "Zamek Forchtenstein".
    None = zostaw oryginał."""
    name = (name or "").strip()
    if not name:
        return None
    if wd_label and wd_label.strip() and wd_label.strip().lower() != name.lower() and not wd_label.strip().startswith("Q"):
        return wd_label.strip()
    tokens = name.split()
    # Węgierskie złożenia z łącznikiem: "Pipa-zsomboly", "Szemlő-hegyi-barlang"
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
        # w pozostałej części nieprzetłumaczone słowa pospolite (małą literą) -> zostaw oryginał
        if any(t[:1].isalpha() and t[:1].islower() for t in rest):
            return None
        if not any(ch.isupper() for ch in "".join(rest)):  # brak nazwy własnej ("cave entrance")
            return None
        return f"{w[0]} {' '.join(rest)}"
    return None


def looks_english(text):
    return len(ENGLISH_HINT.findall(text)) >= 2 and not re.search(r"[ąćęłńóśźż]", text, re.I)


PRIORITY_SUB = ["castle", "palace", "museum", "art_museum", "viewpoint", "waterfall", "ruins", "fort", "manor",
                "city_gate", "citywalls", "nature_reserve", "peak", "monument", "observation_tower", "cave_entrance",
                "archaeological_site", "theatre", "gallery", "garden", "park", "statue", "sculpture"]


_ABBR = {"św", "pw", "ul", "im", "ok", "tzw", "r", "w", "wsp", "gen", "ks", "bł", "płk", "dr", "prof", "nr", "pl",
         "al", "zm", "ur", "in", "m", "n", "p", "pn", "pd", "wsch", "zach", "woj", "pow", "gm", "par", "kpt", "por",
         "mjr", "abp", "bp", "o", "s", "hr", "ppłk", "inż", "mgr", "tj", "np", "jw", "wg", "ds", "st", "mł", "kl", "ss",
         "e", "ew", "jun", "sen", "ang", "łac", "niem", "fr", "lit", "ukr", "czes", "ros", "dz", "op", "cyt", "red", "itd", "itp", "dot"}


def _split_sentences(txt):
    """Dzieli na zdania, ale nie po skrótach (św., pw., im., ok., m.in., r.) ani inicjałach (J. Piłsudski)."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ0-9„\"])", txt)
    out = []
    for p in parts:
        if out:
            last = re.search(r"(\S+)\.$", out[-1])
            word = last.group(1).lower().split(".")[-1] if last else ""
            year_end = re.search(r"(\d{3,4}|\b[IVXL]+)\s+(r|w)\.$", out[-1])  # "w 1995 r." / "w XVI w." kończy zdanie
            if last and not year_end and (word in _ABBR or len(word) == 1 or out[-1].endswith("m.in.") or word.isdigit() and len(word) <= 2):
                out[-1] = out[-1] + " " + p
                continue
        out.append(p)
    return out


DESC_MAX_CHARS = int(os.environ.get("DESC_MAX_CHARS", "1800"))  # pełny opis ("Czytaj więcej"), tylko pełne zdania
DESC_MAX_SENTENCES = int(os.environ.get("DESC_MAX_SENTENCES", "10"))  # aplikacja pokazuje 2-3, reszta po "Czytaj więcej"
SENTENCE_HARD_CAP = 1500  # pojedyncze zdanie dłuższe niż to = śmieć (np. lista), nie bierzemy go


def full_sentences(txt, max_chars=None, max_sentences=None):
    """Tylko PEŁNE zdania (nigdy cięcia w środku zdania ani na skrócie typu "św."), do max_chars.
    Pierwsze zdanie zawsze w całości (o ile nie jest absurdalnie długie). Zwraca "" gdy nie ma pełnego zdania."""
    max_chars = max_chars or DESC_MAX_CHARS
    max_sentences = max_sentences or DESC_MAX_SENTENCES
    txt = re.sub(r"\s+", " ", txt or "").strip()
    txt = re.sub(r"\(\s*[;,]?\s*\)", "", txt)
    txt = re.sub(r"\s+([,.;:])", r"\1", txt)
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
    # ostatnie zdanie musi być zamknięte (tekst z Wikipedii bywa obcięty przez autora/API)
    if text and not text.endswith((".", "!", "?", "”", "\"", ")")):
        closed = [x for x in out if x.endswith((".", "!", "?", "”", "\""))]
        text = " ".join(closed).strip()
    return text


def short_text(txt, max_chars=None):
    """Zgodność wsteczna: opis z pełnych zdań."""
    return full_sentences(txt, max_chars)


# ------------------------------------------------------------------ tłumaczenie (Ollama) z kontrolą faktów
def numbers_ok(source, translated):
    """Każda liczba w tłumaczeniu musi występować w oryginale (ochrona przed dopisanymi datami/wymiarami)."""
    src = set(re.findall(r"\d+", source.replace(",", "").replace(" ", "")))
    for n in re.findall(r"\d+", translated.replace(" ", "")):
        if n not in src:
            return False
    return True


TR_STATS = {}


def _tr_stat(key):
    TR_STATS[key] = TR_STATS.get(key, 0) + 1


def translate_en_pl(text, name, url, model, lang="en", source="wiki"):
    text = full_sentences(text, 1600, 8) or text[:1600]
    where = f"z {LANG_NAMES_PL.get(lang, 'obcojęzycznej')} Wikipedii" if source == "wiki" else "(opis obiektu z mapy, w języku obcym)"
    prompt = (f"Przetłumacz poniższy tekst {where} na naturalny, poprawny język polski (od 3 do 10 pełnych zdań, zachowaj wszystkie ważne informacje). "
              "Używaj WYŁĄCZNIE informacji z tekstu. Nie dodawaj żadnych faktów, dat, liczb ani ocen. "
              f"Obiekt: {name}.\nTEKST:\n{text}\nTŁUMACZENIE:")
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": "10m",
                       "options": {"temperature": 0.1, "num_predict": 700, "num_ctx": 4096}}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
    except Exception as e:
        _tr_stat(f"błąd modelu ({type(e).__name__})")
        return None
    out = re.sub(r"\s+", " ", out).strip().strip('"')
    if len(out) < 20:
        _tr_stat("pusta odpowiedź")
        return None
    if not numbers_ok(text, out):
        _tr_stat("odrzucone - liczby spoza oryginału")
        return None
    res = full_sentences(out) or None
    _tr_stat("udane" if res else "bez pełnych zdań")
    return res


def proper_nouns_ok(source, text):
    """Każda nazwa własna (wielka litera nie na początku zdania) musi pochodzić z faktów.
    Odmiana polska jest dozwolona: wspólny początek >= max(3, długość-3) z którymś słowem źródła
    (Zamku/Zamek, Kudowy/Kudowa), ale nowe nazwy (np. Kazimierz Wielki) są odrzucane."""
    src_words = {w.lower() for w in re.findall(r"\w+", source)}

    def known(word):
        w = word.lower()
        need = max(3, len(w) - 3)
        return any(len(sw) >= need and sw[:need] == w[:need] for sw in src_words)

    for sent in re.split(r"(?<=[.!?])\s+", text):
        # nazwy z łącznikiem (Kudowy-Zdroju) sprawdzane część po części
        words = re.findall(r"[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]+", sent)
        for wd in (words[1:] if sent[:1].isupper() else words):
            if not known(wd):
                return False
    return True


def stylize_pl(facts, name, url, model):
    """2-3 zdania w stylu przewodnika WYŁĄCZNIE z podanych faktów; odrzuca tekst z nowymi liczbami/nazwami."""
    prompt = ("Napisz 3-5 pełnych zdań po polsku w przyjaznym stylu przewodnika turystycznego o obiekcie, "
              "używając WYŁĄCZNIE podanych faktów. Nie dodawaj żadnych dat, liczb, nazw, historii ani ocen, "
              "których nie ma w faktach. Nie zaczynaj od nazwy obiektu w cudzysłowie.\n"
              f"Obiekt: {name}\nFAKTY: {facts}\nOPIS:")
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": "1m", "options": {"temperature": 0.2}}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
    except Exception:
        return None
    out = re.sub(r"\s+", " ", out).strip().strip('"')
    source = f"{name} {facts}"
    if len(out) < 30 or not numbers_ok(source, out) or not proper_nouns_ok(source, out):
        return None
    return full_sentences(out) or None


def ollama_available(url):
    try:
        base = url.split("/api/")[0]
        with urllib.request.urlopen(base + "/api/tags", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ------------------------------------------------------------------ główna funkcja etapu
def enrich_descriptions(pois, log=print, places=None):
    """Ustawia p['description'] i p['desc_source'] ('wiki_pl' | 'wiki_en_pl' | 'osm') dla każdego POI."""
    contact = os.environ.get("WIKI_CONTACT", "").strip()
    llm_url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
    llm_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
    llm_max = int(os.environ.get("LLM_MAX_PER_TILE", "40"))
    # Opcjonalnie: przeredagowanie faktów OSM/Wikidata w styl przewodnika (0 = wyłączone; na CPU ok. 3-8 s/obiekt)
    style_max = int(os.environ.get("STYLE_MAX_PER_TILE", "0"))
    stats = {"wiki_pl": 0, "wiki_en_pl": 0, "osm": 0, "osm_styl": 0}
    attach_locality(pois, places or [])

    pl_title, en_title = {}, {}
    if contact:
        wiki = Wiki(contact, log)
        for i, p in enumerate(pois):
            wp = (p.get("tags") or {}).get("wikipedia") or p.get("wiki") or ""
            if ":" in wp:
                lang, title = wp.split(":", 1)
                if lang == "pl":
                    pl_title[i] = title.strip()
                elif lang == "en":
                    en_title[i] = title.strip()
        qids = [p.get("wikidata") for p in pois if p.get("wikidata")]
        links = wiki.sitelinks(qids) if qids else {}
        for i, p in enumerate(pois):
            sl = links.get(p.get("wikidata") or "", {})
            if sl.get("pl"):
                pl_title.setdefault(i, sl["pl"])
            if sl.get("en"):
                en_title.setdefault(i, sl["en"])
        for p in pois:
            if p.get("wikidata") in links:
                p["_wd"] = links[p["wikidata"]]
        pl_txt = wiki.extracts("pl", list(pl_title.values())) if pl_title else {}
        need_en = [i for i in en_title if i not in pl_title or pl_title[i] not in pl_txt]
        en_txt = wiki.extracts("en", [en_title[i] for i in need_en]) if need_en else {}
        # inne języki: tylko gdy nie ma ani PL, ani EN
        other_title = {}
        for i, p in enumerate(pois):
            o = (p.get("_wd") or {}).get("other")
            if o and o[1] and not (i in pl_title and pl_title[i] in pl_txt) and not (i in en_title and en_txt.get(en_title[i])):
                other_title[i] = o
        other_txt = {}
        for lang in sorted({o[0] for o in other_title.values()}):
            got = wiki.extracts(lang, [o[1] for o in other_title.values() if o[0] == lang])
            other_txt.update({(lang, t): v for t, v in got.items()})
    else:
        log("  ⚠️ Brak WIKI_CONTACT w .env - opisy tylko z danych OSM (poziom 3)")
        pl_txt, en_txt, need_en, other_title, other_txt = {}, {}, [], {}, {}

    def rank(i):
        sub = pois[i].get("sub_cat")
        return (PRIORITY_SUB.index(sub) if sub in PRIORITY_SUB else len(PRIORITY_SUB), 0 if pois[i].get("wikidata") else 1)

    has_pl = {i for i, t in pl_title.items() if pl_txt.get(t)}
    en_queue = sorted([i for i in en_title if i not in has_pl and en_txt.get(en_title[i])], key=rank)
    osm_en_queue = sorted([i for i, p in enumerate(pois) if i not in has_pl and i not in en_title
                           and (p.get("tags") or {}).get("description") and not (p.get("tags") or {}).get("description:pl")
                           and len(p["tags"]["description"]) > 25 and not looks_polish(p["tags"]["description"])], key=rank)
    other_queue = sorted([i for i, o in other_title.items() if other_txt.get(o)], key=rank)
    llm_on = llm_max > 0 and bool(en_queue or osm_en_queue or other_queue) and ollama_available(llm_url)
    translated = {}
    TR_STATS.clear()
    if llm_on:
        # jedna kolejka: najpierw najcenniejsze atrakcje (zamki, muzea...), niezależnie od języka
        jobs = [(rank(i), "en", i) for i in en_queue] + [(rank(i), "other", i) for i in other_queue]
        jobs.sort(key=lambda j: j[0])
        jobs += [(None, "osm", i) for i in osm_en_queue]
        tried = 0
        llm_seconds = float(os.environ.get("LLM_SECONDS_PER_TILE", "900"))
        t_llm = time.time()
        for _, kind, i in jobs[:llm_max]:
            if time.time() - t_llm > llm_seconds:
                log(f"  [Tłumaczenia] limit czasu {llm_seconds:.0f} s na kafel - reszta z danych mapy")
                break
            if tried >= 3 and TR_STATS.get("udane", 0) == 0 and \
                    sum(v for k, v in TR_STATS.items() if k.startswith("błąd")) == tried:
                log("  ⚠️ Model językowy nie odpowiada - tłumaczenia w tym kaflu pominięte")
                break
            tried += 1
            if kind == "en":
                tr = translate_en_pl(en_txt[en_title[i]], pois[i].get("name"), llm_url, llm_model)
                if tr:
                    translated[i] = tr
            elif kind == "other":
                lang, title = other_title[i]
                tr = translate_en_pl(other_txt[(lang, title)], pois[i].get("name"), llm_url, llm_model, lang)
                if tr:
                    translated[i] = tr
                    pois[i]["_tr_lang"] = lang
            else:
                tr = translate_en_pl(pois[i]["tags"]["description"], pois[i].get("name"), llm_url, llm_model, None, "osm")
                if tr:
                    pois[i]["_desc_translated"] = tr
        log(f"  [Tłumaczenia] kandydatów: {len(en_queue)} EN + {len(other_queue)} inne języki + {len(osm_en_queue)} opisy z mapy"
            f" | wynik: {TR_STATS or 'brak prób'} | {time.time() - t_llm:.0f} s")
    elif llm_max > 0 and (en_queue or other_queue or osm_en_queue):
        log(f"  ⚠️ Model językowy niedostępny ({llm_url}) - {len(en_queue) + len(other_queue)} artykułów bez tłumaczenia")
    # "Czytaj więcej": gdy wstęp ma < 5 zdań, dobieramy dalszą część artykułu (najcenniejsze atrakcje najpierw)
    long_max = int(os.environ.get("WIKI_LONG_MAX_PER_TILE", "200"))
    if contact and long_max > 0:
        short_ones = sorted([i for i in has_pl if len(_split_sentences(full_sentences(pl_txt[pl_title[i]]))) < 5], key=rank)
        got = 0
        for i in short_ones[:long_max]:
            try:
                longer = wiki.article_start("pl", pl_title[i])
            except Exception:
                continue
            if len(_split_sentences(full_sentences(longer))) > len(_split_sentences(full_sentences(pl_txt[pl_title[i]]))):
                pl_txt[pl_title[i]] = longer
                got += 1
            time.sleep(0.1)
        if short_ones:
            log(f"  [Opisy] dłuższe teksty z Wikipedii: {got}/{min(len(short_ones), long_max)}")
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
    # Styl przewodnika dla najcenniejszych obiektów opisanych z danych OSM (tylko gdy jest z czego pisać)
    if style_max > 0 and ollama_available(llm_url):
        cand = sorted([i for i, p in enumerate(pois) if p["desc_source"] == "osm" and p["description"].count(".") >= 2], key=rank)
        for i in cand[:style_max]:
            styled = stylize_pl(pois[i]["description"], pois[i].get("name"), llm_url, llm_model)
            if styled:
                pois[i]["description"], pois[i]["desc_source"] = styled, "osm_styl"
    # polskie nazwy (oryginał zostaje w name_local - taki napis turysta zobaczy na miejscu)
    renamed = 0
    for p in pois:
        pl = polish_name(p.get("name"), p.get("sub_cat"), (p.get("_wd") or {}).get("label_pl"))
        if pl and pl != p.get("name"):
            p["name_local"] = p["name"]
            p["name"] = pl
            renamed += 1
    if renamed:
        log(f"  [Nazwy] po polsku: {renamed} (oryginalne nazwy zachowane)")
    # nazwy z samych cyfr ("1956") -> "Miejsce pamięci 1956"
    for p in pois:
        if re.fullmatch(r"[\d\s.,/–-]+", p.get("name") or ""):
            kind = TYPE_PL.get(p.get("sub_cat") or "", "Atrakcja")
            if p.get("sub_cat") == "memorial" and (p.get("tags") or {}).get("memorial") in MEMORIAL_PL:
                kind = MEMORIAL_PL[p["tags"]["memorial"]]
            p["name"] = f"{kind} {p['name'].strip()}"
    # duplikaty: ta sama nazwa i rodzaj w promieniu ok. 60 m -> zostaje lepiej opisany
    best = {}
    for idx, p in enumerate(pois):
        key = ((p.get("name") or "").strip().lower(), p.get("sub_cat"), round(p["lat"] / 0.0006), round(p["lng"] / 0.0009))
        score = (p["desc_source"] != "osm", bool(p.get("wikidata")), len(p.get("description") or ""))
        if key not in best or score > best[key][0]:
            best[key] = (score, idx)
    keep = {idx for _, idx in best.values()}
    removed = len(pois) - len(keep)
    pois[:] = [p for idx, p in enumerate(pois) if idx in keep]
    if removed:
        log(f"  [Opisy] usunięto duplikaty: {removed}")
    for p in pois:
        key = p["desc_source"] if p["desc_source"] in stats else "wiki_en_pl"
        stats[key] += 1
    log(f"  [Opisy] Wikipedia PL: {stats['wiki_pl']} | tłumaczenia EN->PL: {stats['wiki_en_pl']} | "
        f"(w tym z innych języków: {sum(1 for p in pois if p['desc_source'].startswith('wiki_') and p['desc_source'] not in ('wiki_pl', 'wiki_en_pl'))}) | "
        f"z danych OSM: {stats['osm']} | OSM w stylu przewodnika: {stats['osm_styl']}")
    return stats
