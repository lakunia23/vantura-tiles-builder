#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VANTURA - OPISY ATRAKCJI (etap budowy kafla, zastępuje dawną Maszynę 2)

Zasada: opis ma być PRAWDZIWY i ciekawy. Trzy poziomy, od najlepszego:
  1. Polska Wikipedia  - wstęp artykułu (2-3 zdania). Artykuł wskazany przez tag wikidata/wikipedia
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
        ins = t["inscription"].strip()
        if len(ins) > 160:
            ins = ins[:157].rsplit(" ", 1)[0].rstrip(",;:- ") + "..."
        sent.append(f"Napis: „{ins}”")
    desc = t.get("description:pl") or p.get("_desc_translated") or t.get("description")
    if desc and looks_english(desc) and not t.get("description:pl"):
        desc = None  # angielski opis z OSM tylko po przetłumaczeniu (z kontrolą liczb)
    if desc and len(desc) > 15:
        desc = desc.strip()
        if len(desc) > 220:
            desc = desc[:217].rsplit(" ", 1)[0].rstrip(",;:- ") + "..."
        sent.append(desc.rstrip("."))
    if t.get("fee") == "no":
        sent.append("Wstęp bezpłatny")
    elif t.get("fee") == "yes":
        sent.append("Wstęp płatny")
    if t.get("opening_hours") and len(t["opening_hours"]) <= 60:
        sent.append(f"Godziny otwarcia: {t['opening_hours']}")
    out = " ".join(s if s.endswith((".", "”", "...")) else s + "." for s in sent)
    return out[:600]


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
        """QID -> {'pl': tytuł, 'en': tytuł, 'desc_pl': krótki opis po polsku, 'inception': '1889 r.'} (paczki po 50)."""
        out = {}
        qids = [q for q in dict.fromkeys(qids) if re.fullmatch(r"Q\d+", q or "")]
        for i in range(0, len(qids), 50):
            data = self._get("https://www.wikidata.org/w/api.php", {
                "action": "wbgetentities", "ids": "|".join(qids[i:i + 50]), "props": "sitelinks|descriptions|claims",
                "sitefilter": "plwiki|enwiki", "languages": "pl"})
            for qid, ent in (data.get("entities") or {}).items():
                sl = ent.get("sitelinks") or {}
                rec = {k[:2]: v.get("title") for k, v in sl.items() if k in ("plwiki", "enwiki")}
                d = ((ent.get("descriptions") or {}).get("pl") or {}).get("value")
                if d:
                    rec["desc_pl"] = d
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


def looks_english(text):
    return len(ENGLISH_HINT.findall(text)) >= 2 and not re.search(r"[ąćęłńóśźż]", text, re.I)


PRIORITY_SUB = ["castle", "palace", "museum", "art_museum", "viewpoint", "waterfall", "ruins", "fort", "manor",
                "city_gate", "citywalls", "nature_reserve", "peak", "monument", "observation_tower", "cave_entrance",
                "archaeological_site", "theatre", "gallery", "garden", "park", "statue", "sculpture"]


def short_text(txt, max_chars=420):
    """Pierwsze zdania (2-3), bez wymowy w nawiasach i pustych nawiasów, do max_chars."""
    txt = re.sub(r"\s+", " ", txt or "").strip()
    txt = re.sub(r"\(\s*[;,]?\s*\)", "", txt)
    sentences = re.split(r"(?<=[.!?])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ0-9„\"])", txt)
    out = ""
    for s in sentences:
        if len(out) + len(s) + 1 > max_chars and out:
            break
        out = (out + " " + s).strip()
        if out.count(".") >= 3:
            break
    return out[:max_chars]


# ------------------------------------------------------------------ tłumaczenie (Ollama) z kontrolą faktów
def numbers_ok(source, translated):
    """Każda liczba w tłumaczeniu musi występować w oryginale (ochrona przed dopisanymi datami/wymiarami)."""
    src = set(re.findall(r"\d+", source.replace(",", "").replace(" ", "")))
    for n in re.findall(r"\d+", translated.replace(" ", "")):
        if n not in src:
            return False
    return True


def translate_en_pl(text, name, url, model):
    prompt = ("Przetłumacz poniższy tekst z angielskiej Wikipedii na naturalny język polski i skróć do 2-3 zdań. "
              "Używaj WYŁĄCZNIE informacji z tekstu. Nie dodawaj żadnych faktów, dat, liczb ani ocen. "
              f"Obiekt: {name}.\nTEKST:\n{text}\nTŁUMACZENIE:")
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": "1m", "options": {"temperature": 0.1}}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read().decode("utf-8")).get("response", "").strip()
    except Exception:
        return None
    out = re.sub(r"\s+", " ", out).strip().strip('"')
    if len(out) < 20 or not numbers_ok(text, out):
        return None
    return out[:450]


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
    prompt = ("Napisz 2-3 zdania po polsku w przyjaznym stylu przewodnika turystycznego o obiekcie, "
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
    return out[:450]


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
    llm_max = int(os.environ.get("LLM_MAX_PER_TILE", "20"))
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
    else:
        log("  ⚠️ Brak WIKI_CONTACT w .env - opisy tylko z danych OSM (poziom 3)")
        pl_txt, en_txt, need_en = {}, {}, []

    def rank(i):
        sub = pois[i].get("sub_cat")
        return (PRIORITY_SUB.index(sub) if sub in PRIORITY_SUB else len(PRIORITY_SUB), 0 if pois[i].get("wikidata") else 1)

    has_pl = {i for i, t in pl_title.items() if pl_txt.get(t)}
    en_queue = sorted([i for i in en_title if i not in has_pl and en_txt.get(en_title[i])], key=rank)
    osm_en_queue = sorted([i for i, p in enumerate(pois) if i not in has_pl and i not in en_title
                           and (p.get("tags") or {}).get("description") and looks_english(p["tags"]["description"])], key=rank)
    llm_on = llm_max > 0 and bool(en_queue or osm_en_queue) and ollama_available(llm_url)
    translated = {}
    if llm_on:
        budget = llm_max
        for i in en_queue:
            if budget <= 0:
                break
            budget -= 1
            tr = translate_en_pl(short_text(en_txt[en_title[i]], 600), pois[i].get("name"), llm_url, llm_model)
            if tr:
                translated[i] = tr
        for i in osm_en_queue:
            if budget <= 0:
                break
            budget -= 1
            tr = translate_en_pl(pois[i]["tags"]["description"][:600], pois[i].get("name"), llm_url, llm_model)
            if tr:
                pois[i]["_desc_translated"] = tr
    for i, p in enumerate(pois):
        desc, src = None, None
        t = pl_title.get(i)
        if i in has_pl:
            desc, src = short_text(pl_txt[t]), "wiki_pl"
        elif i in translated:
            desc, src = translated[i], "wiki_en_pl"
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
    for p in pois:
        stats[p["desc_source"]] += 1
    log(f"  [Opisy] Wikipedia PL: {stats['wiki_pl']} | tłumaczenia EN->PL: {stats['wiki_en_pl']} | "
        f"z danych OSM: {stats['osm']} | OSM w stylu przewodnika: {stats['osm_styl']}")
    return stats
