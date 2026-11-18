import os
import re
import sys
import json
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime
import osmium

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# ==============================================================================
# VANTURA UNIVERSAL POI ENGINE (Global OSM Taxonomy -> Nature, History, Art)
# ==============================================================================

def parse_elevation(ele_str):
    """
    Ekstrahuje wartość numeryczną wysokości w metrach z tagu OSM 'ele'.
    """
    if not ele_str:
        return None
    match = re.search(r"[-+]?\d*\.\d+|\d+", str(ele_str).replace(",", "."))
    return float(match.group(0)) if match else None

def calculate_polygon_centroid(nodes):
    """
    Oblicza środek geometryczny (centroid) dla obszaru/wielokąta OSM (Way)
    ze zbioru jego węzłów brzegowych. Zwraca (lat, lon) lub None.
    """
    try:
        valid_nodes = [n for n in nodes if n.location.valid()]
        if not valid_nodes:
            return None
        lat = sum(n.lat for n in valid_nodes) / len(valid_nodes)
        lon = sum(n.lon for n in valid_nodes) / len(valid_nodes)
        return lat, lon
    except Exception:
        return None

def classify_osm_tags(tags):
    """
    Uniwersalny klasyfikator tagów OpenStreetMap dla całego świata.
    Dopasowuje obiekt do jednej z 3 głównych kategorii Vantura:
      - 'nature'  (Natura i krajobraz)
      - 'history' (Historia i dziedzictwo)
      - 'art'     (Sztuka i kultura)
    Zwraca krotkę (kategoria, podkategoria) lub (None, None).
    """
    name = tags.get("name")
    if not name or len(name.strip()) < 3:
        return None, None

    # Tagi kluczowe OpenStreetMap
    tour = tags.get("tourism")
    hist = tags.get("historic")
    amen = tags.get("amenity")
    leis = tags.get("leisure")
    nat = tags.get("natural")
    bnd = tags.get("boundary")
    land = tags.get("landuse")
    art_t = tags.get("artwork_type")
    man = tags.get("man_made")
    place = tags.get("place")
    m_type = tags.get("museum") or tags.get("museum:type") or tags.get("subject") or tags.get("theme")

    # Pomocnicza mała litera nazwy do wykrywania specyficznych instytucji
    nl = name.lower()

    # --------------------------------------------------------------------------
    # 1. SZTUKA I KULTURA (ART)
    # --------------------------------------------------------------------------
    # Dzieła sztuki w przestrzeni publicznej (rzeźby, murale, instalacje, pomniki sztuki)
    if tour == "artwork" or art_t:
        sub = art_t if art_t in ['sculpture', 'statue', 'mural', 'installation', 'mosaic', 'street_art'] else (tour or "artwork")
        return "art", sub

    # Galerie sztuki i wystawy
    if tour == "gallery":
        return "art", "gallery"

    # Teatry, filharmonie, opery, sale koncertowe, planetaria, centra sztuki
    if amen in ['theatre', 'arts_centre', 'planetarium', 'concert_hall']:
        return "art", amen

    # Muzea sztuki (np. Muzeum Narodowe, Muzeum Sztuki Nowoczesnej, Musée d'Art, Pinakothek)
    if tour == "museum":
        if m_type == "art" or any(k in nl for k in ['sztuk', 'galeri', 'art', 'nowoczesn', 'współczesn', 'plakat', 'beaux-arts', 'fine arts', 'kunst', 'arte', 'modern art']):
            return "art", "art_museum"

    # --------------------------------------------------------------------------
    # 2. HISTORIA I DZIEDZICTWO (HISTORY)
    # --------------------------------------------------------------------------
    # Zamki, pałace, dworki, ruiny, forty, bunkry, archeologia, kopalnie zabytkowe
    if hist in ['castle', 'palace', 'manor', 'ruins', 'fort', 'monument', 'memorial',
                'archaeological_site', 'mine', 'city_gate', 'citywalls', 'battlefield',
                'heritage', 'pillbox', 'bunker']:
        return "history", hist

    # Wszystkie pozostałe muzea (ogólne, historyczne, techniczne, regionalne)
    if tour == "museum":
        return "history", "museum"

    # Historyczne rynki miejskie i place
    if place in ['square', 'townsquare'] or (hist == "city_square"):
        return "history", "city_square"

    # Obiekty wpisane do rejestru zabytków (heritage)
    if tags.get("heritage") or hist == "yes":
        return "history", "heritage"

    # --------------------------------------------------------------------------
    # 3. NATURA I KRAJOBRAZ (NATURE)
    # --------------------------------------------------------------------------
    # Punkty widokowe i wieże obserwacyjne
    if tour == "viewpoint":
        return "nature", "viewpoint"
    if man == "tower" and tags.get("tower:type") == "observation":
        return "nature", "observation_tower"

    # Formacje naturalne: szczyty, wodospady, jaskinie, skały, wąwozy, źródła, plaże, wydmy
    if nat in ['peak', 'volcano', 'waterfall', 'cave_entrance', 'cliff', 'rock', 'stone',
                'gorge', 'spring', 'beach', 'wood', 'wetland', 'dune', 'glacier', 'cape']:
        return "nature", nat
    if tags.get("waterway") == "waterfall":
        return "nature", "waterfall"

    # Parki, ogrody, skwery zielone, rezerwaty i parki narodowe
    if leis in ['park', 'garden', 'village_green', 'nature_reserve']:
        return "nature", leis
    if bnd in ['national_park', 'protected_area']:
        return "nature", "nature_reserve"
    if land in ['forest', 'meadow', 'grass', 'village_green', 'nature_reserve'] and any(k in nl for k in ['park', 'las', 'puszcza', 'rezerwat', 'zieleniec', 'skwer', 'bory', 'gaj', 'nature']):
        return "nature", "park"

    return None, None

class TilePoiScanner(osmium.SimpleHandler):
    """
    Skaner kafli OpenStreetMap oparty na bibliotece osmium.
    Przetwarza zarówno pojedyncze węzły punktowe (Node), jak i obszary wieloboczne (Way),
    automatycznie rzutując obszary na pojedynczy punkt (geometryczny centroid).
    """
    def __init__(self, min_lat, min_lon, max_lat, max_lon):
        super().__init__()
        self.min_lat, self.min_lon = min_lat, min_lon
        self.max_lat, self.max_lon = max_lat, max_lon
        self.pois = []

    def node(self, n):
        lat, lon = n.location.lat, n.location.lon
        if not (self.min_lat <= lat < self.max_lat and self.min_lon <= lon < self.max_lon):
            return

        cat, sub = classify_osm_tags(n.tags)
        if cat:
            self.pois.append({
                "id": f"N_{n.id}",
                "name": n.tags.get("name"),
                "lat": lat,
                "lng": lon,
                "category": cat,
                "sub_cat": sub,
                "elevation": parse_elevation(n.tags.get("ele")),
                "wiki": n.tags.get("wikipedia"),
                "wikidata": n.tags.get("wikidata")
            })

    def way(self, w):
        cat, sub = classify_osm_tags(w.tags)
        if not cat:
            return

        centroid = calculate_polygon_centroid(w.nodes)
        if not centroid:
            return

        lat, lon = centroid
        if not (self.min_lat <= lat < self.max_lat and self.min_lon <= lon < self.max_lon):
            return

        self.pois.append({
            "id": f"W_{w.id}",
            "name": w.tags.get("name"),
            "lat": lat,
            "lng": lon,
            "category": cat,
            "sub_cat": sub,
            "elevation": parse_elevation(w.tags.get("ele")),
            "wiki": w.tags.get("wikipedia"),
            "wikidata": w.tags.get("wikidata")
        })

# ==============================================================================
# OPISY I WIKIPEDIA DLA JĘZYKA POLSKIEGO
# ==============================================================================

def get_fallback_desc_pl(sub_cat, ele):
    """
    Generuje naturalny opis po polsku w oparciu o podkategorię i wysokość n.p.m.
    """
    elev_str = f" na wysokości {int(ele)} m n.p.m." if ele else ""
    mapping = {
        # Natura
        "observation_tower": f"Wieża widokowa{elev_str}, oferująca panoramę okolicznych terenów.",
        "viewpoint": f"Punkt widokowy{elev_str} z szerokim widokiem na okoliczny krajobraz.",
        "peak": f"Szczyt górski{elev_str}, cel wędrówek szlakami turystycznymi.",
        "volcano": f"Szczyt wulkaniczny{elev_str} o unikalnym ukształtowaniu geologicznym.",
        "waterfall": "Naturalny wodospad i kaskada wodna.",
        "cave_entrance": "Naturalna jaskinia krasowa i grota.",
        "cliff": "Malownicze urwisko i formacja skalna.",
        "rock": "Charakterystyczna formacja skalna i ostaniec.",
        "gorge": "Malowniczy wąwóz i przełom krajobrazowy.",
        "spring": "Naturalne źródło wody.",
        "beach": "Plaża i strefa rekreacyjna nad wodą.",
        "park": "Zadbany park miejski z alejkami spacerowymi i terenami rekreacyjnymi.",
        "garden": "Ogród tematyczny i spacerowy z bogatą roślinnością.",
        "village_green": "Urokliwy miejski skwer i zieleniec idealny na odpoczynek.",
        "nature_reserve": "Rezerwat przyrody chroniący unikalne walory przyrodnicze i krajobrazowe.",
        # Historia
        "castle": "Zabytkowy zamek o dużym znaczeniu obronnym i krajobrazowym.",
        "palace": "Zabytkowy pałac z otaczającym go kompleksem parkowym.",
        "manor": "Zabytkowy dwór z historyczną zabudową.",
        "ruins": "Malownicze ruiny historycznej budowli.",
        "fort": "Historyczny obiekt fortyfikacyjny i militarny.",
        "city_gate": "Zabytkowa brama miejska w pierścieniu dawnych murów obronnych.",
        "city_square": "Reprezentacyjny plac miejski z historyczną zabudową.",
        "museum": "Muzeum prezentujące zbiory historyczne i kulturowe regionu.",
        "monument": "Pomnik upamiętniający ważne postacie i wydarzenia historyczne.",
        "memorial": "Miejsce pamięci narodowej i historycznej.",
        "archaeological_site": "Stanowisko archeologiczne o wysokiej wartości historycznej.",
        "mine": "Zabytkowa kopalnia i obiekt dziedzictwa przemysłowego.",
        "heritage": "Cenny obiekt zabytkowy objęty ochroną dziedzictwa kulturowego.",
        # Sztuka
        "art_museum": "Muzeum sztuki prezentujące zbiory artystyczne i wystawy.",
        "gallery": "Galeria sztuki prezentująca dzieła artystyczne i wystawy czasowe.",
        "artwork": "Dzieło sztuki w przestrzeni publicznej, rzeźba lub instalacja artystyczna.",
        "sculpture": "Rzeźba artystyczna w przestrzeni publicznej.",
        "statue": "Posąg i rzeźba pamiątkowa.",
        "mural": "Wielkoformatowy mural artystyczny na ścianie budynku.",
        "theatre": "Teatr miejski i scena widowiskowa.",
        "arts_centre": "Centrum kultury i sztuki organizujące wydarzenia artystyczne.",
        "concert_hall": "Sala koncertowa i filharmonia goszcząca występy muzyczne.",
        "planetarium": "Planetarium z seansami astronomicznymi i popularyzującymi naukę."
    }
    return mapping.get(sub_cat, f"Ciekawy punkt turystyczny i cel wycieczek{elev_str}.")

def get_wikipedia_summary_pl(wiki_tag, wikidata_id=None):
    """
    Pobiera oficjalny 1-2 zdaniowy opis po polsku oraz miniaturkę zdjęcia z Wikipedii.
    """
    title = None
    if wiki_tag:
        parts = wiki_tag.split(":", 1)
        if len(parts) == 2 and parts[0].lower() == "pl":
            title = parts[1]
        elif len(parts) == 1:
            title = parts[0]

    if not title and wikidata_id:
        try:
            wd_url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={wikidata_id}&props=sitelinks/urls&format=json"
            req = urllib.request.Request(wd_url, headers={'User-Agent': 'VanturaApp/1.0'})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                sitelinks = data.get("entities", {}).get(wikidata_id, {}).get("sitelinks", {})
                if "plwiki" in sitelinks:
                    title = sitelinks["plwiki"].get("title")
        except Exception:
            pass

    if not title:
        return None, None

    try:
        url = f"https://pl.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title.replace(' ', '_'))}"
        req = urllib.request.Request(url, headers={'User-Agent': 'VanturaApp/1.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            extract = data.get("extract", "")
            thumb = data.get("thumbnail", {}).get("source", None)
            if extract:
                sentences = re.split(r'(?<=[.!?])\s+', extract.strip())
                short = " ".join(sentences[:2]).strip()
                return short, thumb
    except Exception:
        pass

    return None, None

def get_polish_description(name, category, sub_category, elevation, wiki_tag, wikidata_id):
    """
    Tworzy zwięzły opis po polsku: najpierw z Wikipedii, a jeśli brak - z inteligentnego szablonu.
    """
    desc, img = get_wikipedia_summary_pl(wiki_tag, wikidata_id)
    if desc:
        return desc, img
    return get_fallback_desc_pl(sub_category, elevation), None

print("[OK] Modul ekstraktora POI (Vantura Universal POI Engine) gotowy do uzycia.")
