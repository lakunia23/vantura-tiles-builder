#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 VANTURA WORLD GRID BUILDER v2  (MASZYNA 1 - Google Cloud)
================================================================================
Buduje kafle 1x1 stopnia dla aplikacji Vantura i wysyła je do Cloudflare R2 jako:
    tile_<ID>_<LANG>_v2.tar.gz   (zawartość: gh_graph/, pois.sqlite, tile_meta.json)

Różnice względem v1 (naprawione błędy):
 1. Każdy kafel ma ZAKŁADKĘ (domyślnie 0.4 stopnia z każdej strony) - trasy nie urywają się na krawędzi kafla
    (aplikacja wczytuje jeden kafel - ten, w którym jest start).
 2. Kafel jest wycinany ze WSZYSTKICH krajów, które na niego nachodzą, i scalany - kafle przygraniczne mają
    drogi po obu stronach granicy (v1 brał tylko kraj, który był pierwszy w kolejce).
 3. Nowa konfiguracja GraphHoppera (config.yml + modele *.json): prawo dostępu, trudność szlaków, nawierzchnia,
    wysokości SRTM z wygładzaniem, nachylenie, BEZ plików CH (aplikacja ich nie używa - tylko powiększały kafle).
 4. Nie nadpisuje istniejących kafli v2 (chyba że uruchomisz z --force) - nie kasuje pracy Maszyny 2.
 5. Klucze R2 wyłącznie z pliku .env (bez kluczy w kodzie).
 7. Kolorowe szlaki turystyczne w paczce kafla (trails_<ID>.geojson) - bez osobnej maszyny.
 6. Pliki krajów (.o5m) są trzymane na dysku (potrzebne dla kafli sąsiednich); stare usuwane, gdy brakuje miejsca.

Uruchomienie:  python3 BuildWorldGrid_v2.py            (cały świat, wznowienie od miejsca przerwania)
               python3 BuildWorldGrid_v2.py --force    (przebuduj także kafle v2 już obecne w R2)
Zmienne (.env): R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET_NAME,
                opcjonalnie: TILE_LANG=pl, BUFFER_DEG=0.4, JAVA_XMX=24g, GH_TIMEOUT_S=3600, DATA_MAX_GB=500,
                ONLY_TILES=N52_E021,N50_E015 (test), DRY_RUN=1 (bez wysyłki do R2), USE_O5M=1,
                ONLY_COUNTRIES=poland,czech-republic (tylko wybrane kraje, id z Geofabrik),
                PIN_COUNTRIES=poland (kraje budowane na start i nieusuwane; reszta świata na żądanie),
                TILE_GATEWAY_URL + ADMIN_TOKEN (brama Cloudflare Worker: kolejka kafli, o które prosi aplikacja),
                WORKERS=3 (ile kafli naraz; RAM maszyny >= WORKERS x JAVA_XMX + 8 GB),
                MAX_BUCKET_GB=9.5 (BEZPIECZNIK KOSZTÓW: budowa staje, zanim bucket przekroczy darmowe 10 GB;
                              ustaw wyżej świadomie, jeśli akceptujesz opłatę za przechowywanie)
Koszty R2: wysyłka jednym zapytaniem PUT (1 operacja klasy A na kafel zamiast ~40 przy uploadzie wieloczęściowym).
"""
import os
import sys
import gc
import gzip
import math
import json
import random
import time
import shutil
import sqlite3
import tarfile
import subprocess
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ .env
env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
LANG = os.environ.get("TILE_LANG", "pl").lower()
BUFFER_DEG = float(os.environ.get("BUFFER_DEG", "0.4"))
JAVA_EXE = os.environ.get("JAVA_EXE", "java")
JAVA_XMX = os.environ.get("JAVA_XMX", "24g")
OSMCONVERT = os.environ.get("OSMCONVERT", "osmconvert")
GH_TIMEOUT_S = int(os.environ.get("GH_TIMEOUT_S", "3600"))
DATA_MAX_GB = float(os.environ.get("DATA_MAX_GB", "500"))
USE_O5M = os.environ.get("USE_O5M", "1") == "1"
ONLY_TILES = {t.strip() for t in os.environ.get("ONLY_TILES", "").split(",") if t.strip()}
ONLY_COUNTRIES = [c.strip() for c in os.environ.get("ONLY_COUNTRIES", "").split(",") if c.strip()]
MAX_BUCKET_GB = float(os.environ.get("MAX_BUCKET_GB", "9.5"))
# Kraje "przypięte": budowane na start i NIGDY nieusuwane przy zwalnianiu miejsca (np. poland,czech-republic)
PIN_COUNTRIES = [c.strip() for c in os.environ.get("PIN_COUNTRIES", "").split(",") if c.strip()]
# Brama (Cloudflare Worker): kolejka kafli, o które prosi aplikacja
TILE_GATEWAY_URL = os.environ.get("TILE_GATEWAY_URL", "").rstrip("/")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
QUEUE_POLL_S = int(os.environ.get("QUEUE_POLL_S", "900"))
# GitHub Actions: jeden przebieg kolejki z limitem czasu (zadanie może trwać maks. ok. 6 h)
QUEUE_ONCE = os.environ.get("QUEUE_ONCE", "0") == "1"
MAX_RUNTIME_S = int(os.environ.get("MAX_RUNTIME_S", "0"))
START_TIME = time.time()
# Gdzie trzymać kafle: "r2" (Cloudflare R2, darmowe 10 GB) albo "github" (wydania GitHub w publicznym
# repozytorium: bez limitu łącznego rozmiaru i transferu; max 1000 plików na wydanie, 2 GiB na plik)
UPLOAD_TARGET = os.environ.get("UPLOAD_TARGET", "r2").lower()
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")
OSMCONVERT_HASH_MB = int(os.environ.get("OSMCONVERT_HASH_MB", "1000"))
BLOCK = int(os.environ.get("BLOCK", "3"))            # bok bloku kafli jednego zadania (3 -> do 9 kafli)
BATCH = int(os.environ.get("BATCH", "1"))            # numer partii (budowa świata w częściach)
BATCH_BLOCKS = int(os.environ.get("BATCH_BLOCKS", "250"))  # bloków na partię (limit GitHub: 256 zadań)
SKIP_EXISTING = os.environ.get("SKIP_EXISTING", "0") == "1"
SHARD_ENABLE = os.environ.get("SHARD_ENABLE", "1") == "1"
SHARD_THRESHOLD = int(os.environ.get("SHARD_THRESHOLD", "6"))
SHARD_MAX = int(os.environ.get("SHARD_MAX", "12"))
SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_TOTAL = int(os.environ.get("SHARD_TOTAL", "1"))
TRANSLATE_ONLY = os.environ.get("TRANSLATE_ONLY", "0") == "1"
EVICT_LOCK = threading.Lock()
PROTECTED = set()  # nazwy plików kafli, których nie wolno usuwać (kraje przypięte)
CLIP_LOCK = threading.Lock()
WORKERS = max(1, int(os.environ.get("WORKERS", "1")))  # ile kafli budować naraz (każdy używa JAVA_XMX pamięci)
TILE_FORMAT = "v2"

GH_JAR = os.environ.get("GH_JAR", os.path.join(BASE_DIR, "graphhopper-web-11.1.jar"))
CONFIG_YML = os.environ.get("CONFIG_YML", os.path.join(BASE_DIR, "config.yml"))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
WORK_DIR = os.environ.get("WORK_DIR", os.path.join(BASE_DIR, "grid_work_v2"))
TEMP_DIR = os.path.join(WORK_DIR, "temp")
for d in (DATA_DIR, WORK_DIR, TEMP_DIR):
    os.makedirs(d, exist_ok=True)
os.environ["_JAVA_OPTIONS"] = "-Djava.io.tmpdir=" + TEMP_DIR.replace("\\", "/")

PROCESSED_LOG = os.path.join(WORK_DIR, "processed_tiles_v2.txt")
FAILED_LOG = os.path.join(WORK_DIR, "failed_tiles_v2.txt")
PAUSED_LOG = os.path.join(WORK_DIR, "paused_tiles_v2.txt")
BUILDER_LOG = os.path.join(WORK_DIR, "builder_v2.log")
STATUS_FILE = os.path.join(WORK_DIR, "current_status.json")

try:
    from shapely.geometry import shape, box
    from shapely.prepared import prep
    from shapely.ops import unary_union
    from shapely.validation import make_valid
except ImportError:
    print("X BŁĄD: brak biblioteki shapely. Zainstaluj: pip3 install shapely")
    sys.exit(1)

s3 = None
R2_BUCKET = os.environ.get("R2_BUCKET_NAME", "vantura-maps")
if not DRY_RUN and os.environ.get("UPLOAD_TARGET", "r2").lower() == "r2":
    try:
        import boto3
        from botocore.config import Config
        from boto3.s3.transfer import TransferConfig
    except ImportError:
        print("X BŁĄD: brak biblioteki boto3. Zainstaluj: pip3 install boto3")
        sys.exit(1)
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY", "R2_SECRET_KEY"):
        if not os.environ.get(k):
            print(f"X BŁĄD: brak {k} w pliku .env")
            sys.exit(1)
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 10}),
        region_name="auto",
    )

sys.path.insert(0, BASE_DIR)
from enrich_pois_v2 import RichTilePoiScanner, enrich_descriptions  # noqa: E402
from trails_v2 import build_trails  # noqa: E402


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(BUILDER_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def status(country, tile, stage):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump({"country": country, "current_tile": tile, "stage": stage, "timestamp": time.time()}, f, ensure_ascii=False)
    except Exception:
        pass


def format_tile_id(lat, lon):
    ns = f"N{lat:02d}" if lat >= 0 else f"S{abs(lat):02d}"
    ew = f"E{lon:03d}" if lon >= 0 else f"W{abs(lon):03d}"
    return f"{ns}_{ew}"


def archive_name(tile_id):
    return f"tile_{tile_id}_{LANG}_{TILE_FORMAT}.tar.gz"


# ------------------------------------------------------------------ katalog Geofabrik (kraje + kształty)
def load_catalog():
    index_file = os.path.join(DATA_DIR, "geofabrik_index.json")
    if not os.path.exists(index_file) or os.path.getsize(index_file) < 1000:
        log("Pobieranie indeksu Geofabrik...")
        last_err = None
        for attempt in range(1, 6):
            try:
                urllib.request.urlretrieve("https://download.geofabrik.de/index-v1.json", index_file)
                if os.path.exists(index_file) and os.path.getsize(index_file) >= 1000:
                    last_err = None
                    break
            except Exception as e:
                last_err = e
                log(f"  ⚠️ Błąd pobierania indeksu Geofabrik (próba {attempt}/5): {e}")
                time.sleep(5 * attempt)
        if last_err is not None:
            raise last_err
    with open(index_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    continents = {"europe", "north-america", "south-america", "asia", "africa", "australia-oceania", "central-america"}
    out = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        pbf = p.get("urls", {}).get("pbf")
        geom = feat.get("geometry")
        if not pbf or not geom:
            continue
        if p.get("parent") in continents or p.get("id") in ("russia", "antarctica"):
            try:
                g = shape(geom)
            except Exception:
                continue
            minx, miny, maxx, maxy = g.bounds
            out.append({
                "id": p.get("id"), "name": p.get("name"), "pbf_url": pbf, "geom": prep(g),
                "bbox": (int(math.floor(miny)), int(math.floor(minx)), int(math.ceil(maxy)), int(math.ceil(maxx))),
            })
    return out


def load_regions():
    """Najmniejsze regiony Geofabrik (bez podregionów): województwa, landy, stany, okręgi... - do pobierania
    danych kafla. Dzięki temu kafel przy Kaliningradzie pobiera region 'kaliningrad', a nie całą Rosję."""
    with open(os.path.join(DATA_DIR, "geofabrik_index.json"), "r", encoding="utf-8") as f:
        data = json.load(f)
    parents = {feat.get("properties", {}).get("parent") for feat in data.get("features", [])}
    out = []
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        pbf = p.get("urls", {}).get("pbf")
        if not pbf or not feat.get("geometry") or p.get("id") in parents or not p.get("parent"):
            continue
        try:
            g = make_valid(shape(feat["geometry"]))
        except Exception:
            continue
        out.append({"id": p["id"], "name": p.get("name"), "pbf_url": pbf, "shape": g, "area": g.area})
    out.sort(key=lambda r: r["area"])
    return out


def pick_regions(regions, area):
    """Regiony nachodzące na obszar; pomija region, którego część w obszarze pokrywają już mniejsze
    (tak odpadają regiony zbiorcze typu 'alps', 'dach', 'us-midwest')."""
    chosen, parts = [], []
    for r in regions:
        if not r["shape"].intersects(area):
            continue
        part = r["shape"].intersection(area)
        if part.is_empty or part.area <= 0:
            continue
        if parts and part.difference(unary_union(parts)).area <= 0.03 * part.area:
            continue
        chosen.append(r)
        parts.append(part)
    return chosen


# ------------------------------------------------------------------ dane krajów (pobieranie, .o5m, sprzątanie)
def download(url, target):
    tmp = target + ".tmp"
    for attempt in range(1, 6):
        res = subprocess.run(["curl", "-L", "-C", "-", "-sS", "--retry", "5", "--retry-delay", "5",
                              "--retry-all-errors", "-A", "VanturaMapsBuilder/2.0", "-o", tmp, url])
        if res.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1024 * 1024:
            os.replace(tmp, target)
            return True
        log(f"  ⚠️ Pobieranie nieudane ({attempt}/5): {url}")
        time.sleep(attempt * 15)
    return False


def data_size_gb():
    total = 0
    for fn in os.listdir(DATA_DIR):
        if fn.endswith((".o5m", ".osm.pbf")):
            total += os.path.getsize(os.path.join(DATA_DIR, fn))
    return total / 1024 ** 3


def free_space(keep_ids):
    """Usuwa najdawniej używane pliki krajów, gdy dane przekraczają DATA_MAX_GB (oprócz potrzebnych teraz)."""
    while data_size_gb() > DATA_MAX_GB:
        files = []
        for fn in os.listdir(DATA_DIR):
            if fn.endswith((".o5m", ".osm.pbf")):
                cid = fn.split(".")[0]
                if cid not in keep_ids:
                    path = os.path.join(DATA_DIR, fn)
                    files.append((os.path.getatime(path), path))
        if not files:
            return
        files.sort()
        log(f"  [Dysk] Usuwam najdawniej używany plik: {os.path.basename(files[0][1])}")
        os.remove(files[0][1])


def ensure_source(country):
    """Zwraca ścieżkę do danych kraju gotowych do wycinania (.o5m lub .osm.pbf)."""
    cid = country["id"].replace("/", "_")
    o5m = os.path.join(DATA_DIR, f"{cid}.o5m")
    pbf = os.path.join(DATA_DIR, f"{cid}.osm.pbf")
    if USE_O5M and os.path.exists(o5m) and os.path.getsize(o5m) > 1024:
        os.utime(o5m, None)
        return o5m
    if not os.path.exists(pbf) or os.path.getsize(pbf) < 1024 * 1024:
        log(f"  [Pobieranie] {country['name']} ({cid})")
        if not download(country["pbf_url"], pbf):
            return None
    if not USE_O5M:
        os.utime(pbf, None)
        return pbf
    log(f"  [o5m] Konwersja {cid} (szybsze wycinanie kafli)...")
    res = subprocess.run([OSMCONVERT, pbf, f"-o={o5m}"])
    if res.returncode == 0 and os.path.exists(o5m):
        os.remove(pbf)
        return o5m
    log(f"  ⚠️ Konwersja o5m nieudana dla {cid} - używam .osm.pbf")
    return pbf


# ------------------------------------------------------------------ R2
# Jedno zapytanie PUT do 4 GiB (R2 przyjmuje pojedynczy upload do ~5 GiB) - każda część uploadu
# wieloczęściowego to osobna płatna operacja klasy A, a domyślnie boto3 dzieli plik co 8 MB.
SINGLE_PUT = None


def single_put_config():
    global SINGLE_PUT
    if SINGLE_PUT is None:
        SINGLE_PUT = TransferConfig(multipart_threshold=4 * 1024 ** 3, multipart_chunksize=512 * 1024 ** 2)
    return SINGLE_PUT


def list_bucket():
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=R2_BUCKET):
        out += page.get("Contents", [])
    return out


def bucket_size_gb():
    """Suma rozmiaru obiektów w buckecie (1 operacja klasy A na 1000 obiektów)."""
    return sum(o.get("Size", 0) for o in list_bucket()) / 1024 ** 3


def ensure_space(extra_bytes, context):
    """Pilnuje darmowego limitu R2: gdy bucket + nowy kafel przekroczyłby MAX_BUCKET_GB, usuwa najpierw
    stare kafle v1, potem najdawniej zbudowane kafle v2 spoza krajów przypiętych (odbudują się, gdy
    ktoś o nie poprosi). Gdy nie da się zwolnić miejsca - zatrzymuje budowę (kod 2)."""
    if s3 is None:
        return
    with EVICT_LOCK:
        objs = list_bucket()
        limit = MAX_BUCKET_GB * 1024 ** 3
        total = sum(o.get("Size", 0) for o in objs)
        if total + extra_bytes <= limit:
            return
        v1 = sorted([o for o in objs if o["Key"].startswith("tile_") and not o["Key"].endswith("_v2.tar.gz")],
                    key=lambda o: o["LastModified"])
        v2 = sorted([o for o in objs if o["Key"].endswith("_v2.tar.gz") and o["Key"] not in PROTECTED],
                    key=lambda o: o["LastModified"])
        other = sorted([o for o in objs if o["Key"].startswith(("trails_", "vantura_server"))], key=lambda o: o["LastModified"])
        freed = 0
        for o in other + v1 + v2:
            if total - freed + extra_bytes <= limit:
                break
            s3.delete_object(Bucket=R2_BUCKET, Key=o["Key"])
            freed += o.get("Size", 0)
            log(f"  [Miejsce] usunięto {o['Key']} ({o.get('Size', 0) / 1024 ** 2:.1f} MB) - darmowy limit R2")
        if total - freed + extra_bytes > limit:
            log(f"  🛑 STOP ({context}): kafle krajów przypiętych nie mieszczą się w {MAX_BUCKET_GB} GB. "
                "Zmniejsz PIN_COUNTRIES albo świadomie podnieś MAX_BUCKET_GB w .env.")
            os._exit(2)


def storage_guard(context):
    if UPLOAD_TARGET == "r2":
        ensure_space(0, context)


def release_tag(lat, lon):
    """Wydanie = blok 10x10 stopni (maks. 100 kafli; limit GitHub to 1000 plików na wydanie)."""
    la, lo = (lat // 10) * 10, (lon // 10) * 10
    ns = f"N{la:02d}" if la >= 0 else f"S{abs(la):02d}"
    ew = f"E{lo:03d}" if lo >= 0 else f"W{abs(lo):03d}"
    return f"kafle-{ns}_{ew}"


_RELEASE_ASSETS = {}
_GH_LOCK = threading.Lock()


def _gh(args, check=True):
    return subprocess.run(["gh"] + args + ["--repo", GITHUB_REPOSITORY], capture_output=True, text=True, check=check)


def github_assets(tag, refresh=False):
    with _GH_LOCK:
        if refresh or tag not in _RELEASE_ASSETS:
            res = _gh(["release", "view", tag, "--json", "assets", "-q", ".assets[].name"], check=False)
            _RELEASE_ASSETS[tag] = set(res.stdout.split()) if res.returncode == 0 else set()
        return _RELEASE_ASSETS[tag]


def list_all_github_assets():
    """Szybka lista wszystkich plików ze wszystkich wydań kafle-* w repozytorium (1-3 zapytania API)."""
    all_assets = set()
    if not GITHUB_REPOSITORY:
        return all_assets
    res = subprocess.run(
        ["gh", "api", "--paginate", f"repos/{GITHUB_REPOSITORY}/releases?per_page=100"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return all_assets
    try:
        # gh api --paginate może zwrócić kilka tablic JSON sklejonych po sobie: [...][...]
        dec = json.JSONDecoder()
        s = res.stdout.strip()
        pos = 0
        rels = []
        while pos < len(s):
            while pos < len(s) and s[pos].isspace():
                pos += 1
            if pos >= len(s):
                break
            chunk, end = dec.raw_decode(s, pos)
            if isinstance(chunk, list):
                rels.extend(chunk)
            pos = end
    except Exception:
        return all_assets
    for r in rels:
        tag = r.get("tag_name") or ""
        if not tag.startswith("kafle-"):
            continue
        names = {a.get("name") for a in (r.get("assets") or []) if a.get("name")}
        if len(names) >= 95 and r.get("id"):
            r2 = subprocess.run(
                ["gh", "api", "--paginate", f"repos/{GITHUB_REPOSITORY}/releases/{r['id']}/assets?per_page=100", "-q", ".[].name"],
                capture_output=True, text=True, check=False,
            )
            if r2.returncode == 0 and r2.stdout.strip():
                names |= set(r2.stdout.split())
        with _GH_LOCK:
            _RELEASE_ASSETS[tag] = names
        all_assets |= names
    return all_assets


def github_delete_asset(tag, name):
    res = _gh(["release", "delete-asset", tag, name, "--yes"], check=False)
    if res.returncode == 0:
        with _GH_LOCK:
            if tag in _RELEASE_ASSETS:
                _RELEASE_ASSETS[tag].discard(name)
    return res.returncode == 0


def github_upload(path, name, lat, lon):
    tag = release_tag(lat, lon)
    for attempt in range(1, 7):
        try:
            with _GH_LOCK:
                if _gh(["release", "view", tag], check=False).returncode != 0:
                    _gh(["release", "create", tag, "--title", f"Kafle {tag[6:]}",
                         "--notes", "Kafle map Vantura (graf dróg, atrakcje z opisami, szlaki). "
                                    "Dane: © OpenStreetMap contributors (ODbL); opisy: Wikipedia (CC BY-SA), Wikidata (CC0)."],
                        check=False)
            res = _gh(["release", "upload", tag, f"{path}#{name}", "--clobber"], check=False)
            if res.returncode == 0:
                with _GH_LOCK:
                    _RELEASE_ASSETS.setdefault(tag, set()).add(name)
                return True
            log(f"  ⚠️ Wysyłka do wydania {tag} (próba {attempt}): {res.stderr.strip()[:200]}")
        except Exception as e:
            log(f"  ⚠️ Wysyłka do wydania {tag} (próba {attempt}): {e}")
        time.sleep(8 * attempt + random.uniform(0.5, 3.5))
    return False


def tile_exists_in_r2(name):
    if s3 is None:
        return False
    try:
        return s3.head_object(Bucket=R2_BUCKET, Key=name).get("ContentLength", 0) > 2048
    except Exception:
        return False


def upload_verified(path, name):
    size = os.path.getsize(path)
    for attempt in range(1, 11):
        try:
            s3.upload_file(path, R2_BUCKET, name, ExtraArgs={"Metadata": {"vantura-format": TILE_FORMAT}},
                           Config=single_put_config())
            remote = s3.head_object(Bucket=R2_BUCKET, Key=name).get("ContentLength", 0)
            if remote == size:
                return True
            log(f"  ⚠️ Rozmiar w R2 {remote} != {size} (próba {attempt})")
        except Exception as e:
            log(f"  ⚠️ Błąd wysyłki (próba {attempt}): {e}")
        time.sleep(5 * attempt)
    return False


def tile_already_exists(lat, lon):
    name = archive_name(format_tile_id(lat, lon))
    if DRY_RUN:
        return False
    return (name in github_assets(release_tag(lat, lon))) if UPLOAD_TARGET == "github" else tile_exists_in_r2(name)


def tr_cache_name(tile_id, shard_index=None):
    if shard_index is not None:
        return f"tr_{tile_id}_{LANG}.s{int(shard_index):02d}.json.gz"
    return f"tr_{tile_id}_{LANG}.json.gz"


def _download_gh_json_gz(tag, cname):
    tmp = os.path.join(TEMP_DIR, f"dl_{cname}")
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        res = _gh(["release", "download", tag, "-p", cname, "-O", tmp], check=False)
        if res.returncode == 0 and os.path.exists(tmp):
            with gzip.open(tmp, "rt", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    return {}


def load_tr_cache(lat, lon, include_shards=True):
    """Pobiera pamięć tłumaczeń (własną z poprzedniej rundy + shardy .sXX + z 8 sąsiednich kafli),
    aby wznawiać gęste kafle i nie tłumaczyć tych samych obiektów w zakładkach."""
    merged = {}
    shard_files = []
    if DRY_RUN:
        return merged, shard_files
    # najpierw sąsiedzi, na końcu własny kafel i jego shardy (mają pierwszeństwo)
    offsets = [(dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if not (dy == 0 and dx == 0)] + [(0, 0)]
    for dy, dx in offsets:
        la, lo = lat + dy, lon + dx
        if la < -90 or la >= 90 or lo < -180 or lo >= 180:
            continue
        tid = format_tile_id(la, lo)
        cname = tr_cache_name(tid)
        if UPLOAD_TARGET == "github":
            tag = release_tag(la, lo)
            assets = github_assets(tag, refresh=(dy == 0 and dx == 0))
            if cname in assets:
                merged.update(_download_gh_json_gz(tag, cname))
            if include_shards and dy == 0 and dx == 0:
                prefix = f"tr_{tid}_{LANG}.s"
                for aname in sorted(assets):
                    if aname.startswith(prefix) and aname.endswith(".json.gz"):
                        d = _download_gh_json_gz(tag, aname)
                        if d:
                            merged.update(d)
                        shard_files.append(aname)
        else:
            local_p = os.path.join(WORK_DIR, cname)
            if os.path.exists(local_p):
                try:
                    with gzip.open(local_p, "rt", encoding="utf-8") as f:
                        d = json.load(f)
                    if isinstance(d, dict):
                        merged.update(d)
                except Exception:
                    pass
    return merged, shard_files


def save_tr_cache(lat, lon, tile_id, tr_dict, shard_index=None):
    if DRY_RUN or not tr_dict:
        return False
    cname = tr_cache_name(tile_id, shard_index=shard_index)
    tmp = os.path.join(WORK_DIR, cname)
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(tr_dict, f, ensure_ascii=False)
        if UPLOAD_TARGET == "github":
            return github_upload(tmp, cname, lat, lon)
        return True
    except Exception as e:
        log(f"  ⚠️ Nie udało się zapisać pamięci tłumaczeń {cname}: {e}")
        return False


def cleanup_shard_caches(lat, lon, shard_files):
    if DRY_RUN or UPLOAD_TARGET != "github" or not shard_files:
        return
    tag = release_tag(lat, lon)
    for sf in shard_files:
        github_delete_asset(tag, sf)


# ------------------------------------------------------------------ budowa jednego kafla
def build_tile(lat, lon, sources, force):
    tile_id = format_tile_id(lat, lon)
    name = archive_name(tile_id)
    if not force and tile_already_exists(lat, lon):
        log(f"  ✓ {name} już istnieje - pomijam")
        return True

    b = BUFFER_DEG
    min_lat, min_lon, max_lat, max_lon = lat - b, lon - b, lat + 1 + b, lon + 1 + b
    clip_locked = False
    work = os.path.join(WORK_DIR, tile_id)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    merged = os.path.join(work, f"{tile_id}.pbf")
    poi_db = os.path.join(work, "pois.sqlite")
    gh_dir = os.path.join(work, "gh_graph")
    arch = os.path.join(WORK_DIR, name)
    try:
        # 1. Wycięcie obszaru z każdego kraju, który na niego nachodzi, i scalenie (kafle przygraniczne!)
        #    Wycinanie po kolei (blokada): równoczesne czytanie tego samego pliku kraju przez kilka osmconvertów
        #    kończyło się błędem. Wycinanie trwa krótko - równolegle idzie budowa grafu (najdłuższy etap).
        CLIP_LOCK.acquire()
        clip_locked = True
        parts = []
        for i, (cid, src) in enumerate(sources):
            part = os.path.join(work, f"part{i}.o5m")
            res = subprocess.run([OSMCONVERT, src, f"-b={min_lon},{min_lat},{max_lon},{max_lat}",
                                  "--drop-broken-refs", f"--hash-memory={OSMCONVERT_HASH_MB}",
                                  f"-t={os.path.join(TEMP_DIR, 'osmc_' + tile_id)}", f"-o={part}"],
                                 capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(part):
                log(f"  X Wycinanie z {cid} nieudane (kod {res.returncode}): {(res.stderr or res.stdout).strip()[:300]}")
                return False
            if os.path.getsize(part) > 200:
                parts.append(part)
        if not parts:
            log(f"  🌊 {tile_id}: brak danych OSM - pomijam")
            return True
        res = subprocess.run([OSMCONVERT] + parts + [f"-o={merged}"], capture_output=True, text=True)
        if res.returncode != 0 or not os.path.exists(merged):
            log(f"  X Scalanie nieudane (kod {res.returncode}): {(res.stderr or res.stdout).strip()[:300]}")
            return False
        for p in parts:
            os.remove(p)
        CLIP_LOCK.release()
        clip_locked = False
        osm_size = os.path.getsize(merged)

        # 2. POI (cały obszar z zakładką - aplikacja szuka atrakcji tylko w bazie kafla startowego)
        scanner = RichTilePoiScanner(min_lat, min_lon, max_lat, max_lon)
        try:
            scanner.apply_file(merged, locations=True)
        except Exception as e:
            # Nie wysyłamy kafla z niepełną listą atrakcji - próba zostanie powtórzona
            log(f"  X Błąd odczytu POI ({e}) - ponowię kafel")
            return False
        poi_count = len(scanner.pois)
        if poi_count == 0 and osm_size < 100 * 1024:
            log(f"  🌊 {tile_id}: morze/pustkowie (0 POI, {osm_size} B) - pomijam")
            return True
        # Opisy: Wikipedia PL -> obcojęzyczna Wikipedia/OSM/Wikidata (priorytet Gemini API, potem TranslateGemma 4B,
        # do 5 zdań, z pamięcią tłumaczeń tr_<KAFEL>_pl.json.gz współdzieloną z sąsiadami i dzieloną na shardy).
        tr_max_s = int(os.environ.get("TRANSLATE_MAX_S", "0"))
        deadline_ts = (START_TIME + tr_max_s) if tr_max_s > 0 else None
        tr_cache, shard_files = load_tr_cache(lat, lon, include_shards=(not TRANSLATE_ONLY))
        if tr_cache:
            sh_msg = f" (w tym {len(shard_files)} plików z równoległych maszyn)" if shard_files else ""
            log(f"  [Cache tłumaczeń] wczytano {len(tr_cache)} wpisów{sh_msg}")

        merged_saved = {"ok": False}

        def _save_cache_cb(d):
            if TRANSLATE_ONLY and SHARD_TOTAL > 1:
                save_tr_cache(lat, lon, tile_id, d, shard_index=SHARD_INDEX)
            else:
                if save_tr_cache(lat, lon, tile_id, d):
                    merged_saved["ok"] = True

        enrich_stats = enrich_descriptions(
            scanner.pois, log,
            places=scanner.places,
            geo_wiki=scanner.geo_wiki,
            protected_areas=scanner.protected_areas,
            tr_cache=tr_cache,
            save_cache=_save_cache_cb,
            deadline_ts=deadline_ts,
            shard_index=SHARD_INDEX if TRANSLATE_ONLY else 0,
            shard_total=SHARD_TOTAL if TRANSLATE_ONLY else 1,
            translate_only=TRANSLATE_ONLY,
        )
        if not TRANSLATE_ONLY and shard_files and merged_saved["ok"]:
            cleanup_shard_caches(lat, lon, shard_files)
            log(f"  [Cache tłumaczeń] scalono i posprzątano {len(shard_files)} tymczasowych plików shardów")
        if TRANSLATE_ONLY:
            if enrich_stats.get("incomplete"):
                log(f"  ⏸️ {tile_id} [shard {SHARD_INDEX + 1}/{SHARD_TOTAL}]: limit czasu — postęp zapisany w {tr_cache_name(tile_id, SHARD_INDEX if SHARD_TOTAL > 1 else None)}")
                return "paused"
            log(f"  ✓ {tile_id} [shard {SHARD_INDEX + 1}/{SHARD_TOTAL}]: tłumaczenia gotowe")
            return True
        if enrich_stats.get("incomplete"):
            log(f"  ⏸️ {tile_id}: tłumaczenia w toku (postęp zapisany w {tr_cache_name(tile_id)}) — kolejna runda dokończy automatycznie")
            return "paused"
        conn = sqlite3.connect(poi_db)
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS pois (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, category TEXT NOT NULL,
            sub_category TEXT, elevation REAL, wikidata_id TEXT, image_url TEXT, description TEXT,
            lang TEXT DEFAULT 'pl', rating REAL DEFAULT 0.0, desc_source TEXT, name_local TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pois_coords ON pois (lat, lng)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pois_cat ON pois (category)")
        for p in scanner.pois:
            cur.execute("INSERT OR REPLACE INTO pois VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p["id"], p["name"], p["lat"], p["lng"], p["category"], p["sub_cat"], p["elevation"],
                         p["wikidata"], None, p["description"], LANG, 0.0, p.get("desc_source"), p.get("name_local")))
        conn.commit()
        cur.execute("VACUUM")
        conn.close()
        scanner.pois = []
        del scanner
        gc.collect()

        # 2b. Kolorowe szlaki turystyczne (dawna Maszyna 4) - w paczce kafla, nie jako osobne pliki w R2
        trails_name = f"trails_{tile_id}.geojson"
        trails_path = os.path.join(work, trails_name)
        try:
            n_trails, trails_size = build_trails(merged, min_lat, min_lon, max_lat, max_lon, trails_path)
            log(f"  [Szlaki] {n_trails} szlaków ({trails_size // 1024} KB)")
        except Exception as e:
            log(f"  X Błąd szlaków ({e!r}) - ponowię kafel")
            return False

        # 3. Graf GraphHopper (nowa konfiguracja: dostęp, trudność, wysokości, bez CH)
        cmd = [JAVA_EXE, f"-Xmx{JAVA_XMX}",
               "-Ddw.graphhopper.datareader.file=" + merged.replace("\\", "/"),
               "-Ddw.graphhopper.graph.location=" + gh_dir.replace("\\", "/"),
               "-jar", GH_JAR, "import", CONFIG_YML.replace("\\", "/")]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log(f"  ⏰ GraphHopper przekroczył {GH_TIMEOUT_S} s na {tile_id}")
            return False
        has_graph = True
        if res.returncode != 0:
            err = (res.stderr or "") + (res.stdout or "")
            if "must not be empty" in err:
                has_graph = False
                log(f"  🌊 {tile_id}: brak dróg (sam POI)")
            else:
                key = [l for l in err.splitlines() if any(k in l for k in ("Exception", "Caused by", "Error", "error:"))]
                log(f"  X GraphHopper błąd {res.returncode}: " + (" | ".join(key[:4])[:600] if key else err.strip()[-400:]))
                return False
        if has_graph and not os.path.exists(os.path.join(gh_dir, "properties")):
            log("  X Graf niekompletny (brak pliku properties)")
            return False
        if not has_graph:
            # aplikacja wymaga grafu - kafel bez dróg nie ma sensu (nie wysyłamy)
            return True

        # 4. Paczka
        meta = {"format": TILE_FORMAT, "tile": tile_id, "buffer_deg": BUFFER_DEG,
                "sources": [c for c, _ in sources], "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "poi_count": poi_count, "trails": n_trails}
        with open(os.path.join(work, "tile_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
        with tarfile.open(arch, "w:gz", compresslevel=9) as tar:
            tar.add(poi_db, arcname="pois.sqlite")
            tar.add(gh_dir, arcname="gh_graph")
            tar.add(os.path.join(work, "tile_meta.json"), arcname="tile_meta.json")
            tar.add(trails_path, arcname=trails_name)
        size_mb = os.path.getsize(arch) / 1024 ** 2
        log(f"  [Paczka] {name}: {size_mb:.1f} MB | OSM {osm_size / 1024 ** 2:.1f} MB | POI {poi_count} | źródła {meta['sources']}")

        # 5. Wysyłka
        if DRY_RUN:
            keep = os.path.join(WORK_DIR, "dry_run_" + name)
            shutil.copy(arch, keep)
            log(f"  [DRY_RUN] zapisano lokalnie: {keep}")
            return True
        if UPLOAD_TARGET == "github":
            if not github_upload(arch, name, lat, lon):
                log(f"  X Wysyłka {name} do wydania GitHub nieudana")
                return False
            log(f"  ✓ Wysłano {name} do wydania {release_tag(lat, lon)}")
            return True
        ensure_space(os.path.getsize(arch), name)
        if not upload_verified(arch, name):
            log(f"  X Wysyłka {name} nieudana")
            return False
        log(f"  ✓ Wysłano i zweryfikowano {name}")
        return True
    except Exception as e:
        log(f"  X Wyjątek przy {tile_id}: {e!r}")
        return False
    finally:
        if clip_locked:
            CLIP_LOCK.release()
        shutil.rmtree(work, ignore_errors=True)
        if os.path.exists(arch):
            try:
                os.remove(arch)
            except Exception:
                pass


# ------------------------------------------------------------------ pętla świata
PRIORITY = ["poland", "czech-republic", "slovakia", "germany", "lithuania", "ukraine", "belarus", "austria",
            "switzerland", "italy", "france", "spain", "portugal", "netherlands", "belgium", "denmark", "norway",
            "sweden", "finland", "hungary", "romania", "croatia", "slovenia", "greece", "great-britain",
            "ireland-and-northern-ireland"]


REGIONS = []


def sources_for(catalog, la, lo, tile_id):
    area = box(lo - BUFFER_DEG, la - BUFFER_DEG, lo + 1 + BUFFER_DEG, la + 1 + BUFFER_DEG)
    if not REGIONS:
        REGIONS.extend(load_regions())
    src_countries = pick_regions(REGIONS, area)
    sources = []
    for c in src_countries:
        path = ensure_source(c)
        if path:
            sources.append((c["id"], path))
        else:
            log(f"  ⚠️ Brak danych {c['id']} - kafel {tile_id} bez tego kraju")
    return sources, {c["id"].replace("/", "_") for c in src_countries}


def country_tiles(country):
    min_lat, min_lon, max_lat, max_lon = country["bbox"]
    return [(la, lo) for la in range(min_lat, max_lat) for lo in range(min_lon, max_lon)
            if country["geom"].intersects(box(lo, la, lo + 1, la + 1))]


def gateway(method, path):
    req = urllib.request.Request(f"{TILE_GATEWAY_URL}/{path}", method=method,
                                 headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "User-Agent": "VanturaBuilder/2.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8")


def queue_loop(catalog):
    """Tryb na żądanie: buduje kafle, o które poprosiła aplikacja (przez bramę), i czeka na kolejne."""
    import re as _re
    pat = _re.compile(r"^tile_([NS])(\d{2})_([EW])(\d{3})_([a-z]{2})_v2\.tar\.gz$")
    log(f"[Kolejka] Tryb na żądanie - sprawdzam prośby co {QUEUE_POLL_S // 60} min")
    while True:
        try:
            keys = json.loads(gateway("GET", "admin/requests")).get("requests", [])
        except Exception as e:
            log(f"  ⚠️ Brama niedostępna ({e}) - ponowię za {QUEUE_POLL_S // 60} min")
            keys = []
        for key in keys:
            m = pat.match(key)
            if not m or m.group(5) != LANG:
                continue
            la = int(m.group(2)) * (1 if m.group(1) == "N" else -1)
            lo = int(m.group(4)) * (1 if m.group(3) == "E" else -1)
            tile_id = format_tile_id(la, lo)
            log(f"--- [Kolejka] {tile_id} (prośba z aplikacji)")
            sources, _ = sources_for(catalog, la, lo, tile_id)
            ok = False
            for attempt in range(1, 4):
                ok = build_tile(la, lo, sources, force=True)
                if ok:
                    break
                time.sleep(10)
            if ok:
                try:
                    gateway("DELETE", f"admin/requests/{key}")
                except Exception as e:
                    log(f"  ⚠️ Nie usunięto prośby {key}: {e}")
            else:
                with open(FAILED_LOG, "a", encoding="utf-8") as f:
                    f.write(tile_id + "\n")
            if MAX_RUNTIME_S and time.time() - START_TIME > MAX_RUNTIME_S:
                log("[Kolejka] Limit czasu przebiegu - reszta w następnym uruchomieniu")
                return
        if QUEUE_ONCE:
            log(f"[Kolejka] Przebieg zakończony ({len(keys)} próśb)")
            return
        if not keys:
            time.sleep(QUEUE_POLL_S)


def main():
    force = "--force" in sys.argv
    catalog = load_catalog()
    by_id = {c["id"]: c for c in catalog}
    if "--prepare-sources" in sys.argv:
        # Pobiera raz wszystkie regiony potrzebne dla kafli krajów przypiętych (pamięć podręczna GitHub Actions)
        ids = PIN_COUNTRIES or ONLY_COUNTRIES
        need = {}
        for cid in ids:
            for la, lo in country_tiles(by_id[cid]):
                area = box(lo - BUFFER_DEG, la - BUFFER_DEG, lo + 1 + BUFFER_DEG, la + 1 + BUFFER_DEG)
                if not REGIONS:
                    REGIONS.extend(load_regions())
                for r in pick_regions(REGIONS, area):
                    need[r["id"]] = r
        log(f"[Źródła] regionów do pobrania: {len(need)}: {sorted(need)}")
        for r in need.values():
            if not ensure_source(r):
                log(f"X Nie udało się pobrać {r['id']}")
                sys.exit(1)
        log(f"[Źródła] gotowe, razem {data_size_gb():.2f} GB")
        return
    if "--queue-once" in sys.argv:
        if not (TILE_GATEWAY_URL and ADMIN_TOKEN):
            log("X Brak TILE_GATEWAY_URL lub ADMIN_TOKEN")
            sys.exit(1)
        if PIN_COUNTRIES:
            for cid in PIN_COUNTRIES:
                for la, lo in country_tiles(by_id[cid]):
                    PROTECTED.add(archive_name(format_tile_id(la, lo)))
        global QUEUE_ONCE
        QUEUE_ONCE = True
        queue_loop(catalog)
        return
    order = [cid for cid in PRIORITY if cid in by_id] + [c["id"] for c in catalog if c["id"] not in PRIORITY]
    if "--list-tiles" in sys.argv:
        # Lista kafli krajów przypiętych (dla równoległych zadań GitHub Actions).
        # Bloki BLOCK x BLOCK sąsiednich kafli ułożone szachownicowo (mniejsze dublowanie tłumaczeń w zakładkach).
        # Przy SKIP_EXISTING=1 pomija kafle już wgrane do GitHub Releases i automatycznie włącza tryb "sharded"
        # (wiele maszyn na 1 kafel), gdy w bieżącej partii zostało <= SHARD_THRESHOLD kafli (np. Liechtenstein!).
        ids = PIN_COUNTRIES or ONLY_COUNTRIES
        if ids == ["world"]:
            ids = [c for c in order]
        existing = set()
        if SKIP_EXISTING and UPLOAD_TARGET == "github" and GITHUB_REPOSITORY:
            existing = list_all_github_assets()
        seen, blocks, block_order = set(), {}, []
        for cid in ids:
            for la, lo in sorted(country_tiles(by_id[cid])):
                if (la, lo) in seen:
                    continue
                seen.add((la, lo))
                key = (la // BLOCK, lo // BLOCK)
                if key not in blocks:
                    blocks[key] = []
                    block_order.append(key)
                blocks[key].append(format_tile_id(la, lo))

        total = max(1, (len(block_order) + BATCH_BLOCKS - 1) // BATCH_BLOCKS)

        def _unbuilt_blocks(batch_num):
            raw_keys = block_order[(batch_num - 1) * BATCH_BLOCKS: batch_num * BATCH_BLOCKS]
            # Szachownica w obrębie partii: najpierw nie-sąsiadujące bloki, aby sąsiedzi korzystali z cache tłumaczeń
            raw_keys = sorted(raw_keys, key=lambda k: ((k[0] % 2) * 2 + (k[1] % 2)))
            out_b = []
            for k in raw_keys:
                rem = [tid for tid in blocks[k] if archive_name(tid) not in existing]
                if rem:
                    out_b.append(rem)
            return out_b

        chosen_batch = None
        chosen_blocks = []
        for bnum in range(max(1, BATCH), total + 1):
            ub = _unbuilt_blocks(bnum)
            if ub:
                chosen_batch = bnum
                chosen_blocks = ub
                break

        has_more_batches = False
        if chosen_batch is not None:
            for bnum in range(chosen_batch + 1, total + 1):
                if _unbuilt_blocks(bnum):
                    has_more_batches = True
                    break
        else:
            # Sprawdź wcześniejsze partie (gdyby jakieś gęste kafle wymagały kolejnej rundy)
            earlier_has_work = any(_unbuilt_blocks(bnum) for bnum in range(1, min(BATCH, total + 1)))
            if earlier_has_work:
                print(f"PARTIE={total} KAFLE={len(seen)} BLOKI={len(block_order)}")
                print("DONE=0")
                print("MODE=wrap")
                print("NEXT_BATCH=0")
                print("HAS_MORE_BATCHES=0")
                print("REMAINING=1")
                print("GEMINI_WORKERS=1")
                print("CHECK_TILES=")
                print('SHARDS={"include": []}')
                print('{"include": []}')
                return
            print(f"PARTIE={total} KAFLE={len(seen)} BLOKI={len(block_order)}")
            print("DONE=1")
            print("MODE=done")
            print(f"NEXT_BATCH={total}")
            print("HAS_MORE_BATCHES=0")
            print("REMAINING=0")
            print("GEMINI_WORKERS=1")
            print("CHECK_TILES=")
            print('SHARDS={"include": []}')
            print('{"include": []}')
            return

        batch_tiles = [tid for blk in chosen_blocks for tid in blk]
        if SHARD_ENABLE and 1 <= len(batch_tiles) <= SHARD_THRESHOLD:
            mode = "sharded"
            per_tile = min(SHARD_MAX, max(2, 12 // len(batch_tiles)))
            shards_mat = {
                "include": [
                    {"tile": tid, "shard_index": s, "shard_total": per_tile}
                    for tid in batch_tiles for s in range(per_tile)
                ]
            }
            bloki_mat = {"include": [{"tiles": tid} for tid in batch_tiles]}
            gem_workers = min(18, len(shards_mat["include"]))
        else:
            mode = "normal"
            shards_mat = {"include": []}
            bloki_mat = {"include": [{"tiles": ",".join(blk)} for blk in chosen_blocks]}
            gem_workers = min(18, max(1, len(bloki_mat["include"])))

        check_tiles = ",".join(batch_tiles) if (not has_more_batches and len(batch_tiles) <= 20) else ""
        print(f"PARTIE={total} KAFLE={len(seen)} BLOKI={len(block_order)}")
        print("DONE=0")
        print(f"MODE={mode}")
        print(f"NEXT_BATCH={chosen_batch}")
        print(f"HAS_MORE_BATCHES={1 if has_more_batches else 0}")
        print(f"REMAINING={len(batch_tiles)}")
        print(f"GEMINI_WORKERS={gem_workers}")
        print(f"CHECK_TILES={check_tiles}")
        print("SHARDS=" + json.dumps(shards_mat))
        print(json.dumps(bloki_mat))
        return
    if ONLY_COUNTRIES:
        unknown = [c for c in ONLY_COUNTRIES if c not in by_id]
        if unknown:
            log(f"X Nieznane id krajów: {unknown} (sprawdź nazwy w data/geofabrik_index.json)")
            sys.exit(1)
        order = [c for c in order if c in ONLY_COUNTRIES]
    if PIN_COUNTRIES == ["world"]:
        PIN_COUNTRIES.clear()  # cały świat: budujemy po kolei kraje z listy (ONLY_TILES zawęża do bloku)
    if PIN_COUNTRIES:
        unknown = [c for c in PIN_COUNTRIES if c not in by_id]
        if unknown:
            log(f"X Nieznane id krajów w PIN_COUNTRIES: {unknown}")
            sys.exit(1)
        for cid in PIN_COUNTRIES:
            for la, lo in country_tiles(by_id[cid]):
                PROTECTED.add(archive_name(format_tile_id(la, lo)))
        if not ONLY_COUNTRIES:
            order = [c for c in PIN_COUNTRIES]  # najpierw (i tylko) kraje przypięte; reszta świata na żądanie
    storage_guard("start")
    tiles_since_check = 0
    done = set()
    if os.path.exists(PROCESSED_LOG):
        with open(PROCESSED_LOG, encoding="utf-8") as f:
            done = {l.strip() for l in f if l.strip()}
    log(f"START v2 | równolegle {WORKERS} | język {LANG} | zakładka {BUFFER_DEG}° | krajów {len(order)} | gotowych kafli {len(done)} | force={force} | DRY_RUN={DRY_RUN}")

    lock = threading.Lock()
    in_flight = {}   # tile_id -> (future, zbiór id krajów-źródeł)

    def run_tile(la, lo, tile_id, sources):
        res = False
        tr_max_s = int(os.environ.get("TRANSLATE_MAX_S", "0"))
        for attempt in range(1, 4):
            res = build_tile(la, lo, sources, force)
            if res is True or res == "paused":
                break
            if (tr_max_s and time.time() >= START_TIME + tr_max_s) or (MAX_RUNTIME_S and time.time() - START_TIME > MAX_RUNTIME_S):
                log(f"  ⏸️ {tile_id}: zatrzymano z powodu limitu czasu zadania — dokończy kolejna runda")
                res = "paused"
                break
            log(f"  ❌ {tile_id} próba {attempt}/3 nieudana")
            time.sleep(10)
        with lock:
            if res == "paused":
                with open(PAUSED_LOG, "a", encoding="utf-8") as f:
                    f.write(tile_id + "\n")
            elif res is not True:
                with open(FAILED_LOG, "a", encoding="utf-8") as f:
                    f.write(tile_id + "\n")
            with open(PROCESSED_LOG, "a", encoding="utf-8") as f:
                f.write(tile_id + "\n")
        return res is True

    def wait_for_slot(limit):
        while True:
            for tid in [t for t, (fu, _) in in_flight.items() if fu.done()]:
                fu, _ = in_flight.pop(tid)
                fu.result()  # ewentualny wyjątek trafi do logu konsoli
            if len(in_flight) < limit:
                return
            time.sleep(2)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for idx, cid in enumerate(order, 1):
            country = by_id[cid]
            min_lat, min_lon, max_lat, max_lon = country["bbox"]
            tiles = [(la, lo) for la in range(min_lat, max_lat) for lo in range(min_lon, max_lon)
                     if country["geom"].intersects(box(lo, la, lo + 1, la + 1))]
            if ONLY_TILES:
                tiles = [t for t in tiles if format_tile_id(*t) in ONLY_TILES]
            tiles = [t for t in tiles if format_tile_id(*t) not in done]
            if not tiles:
                continue
            log(f"### [{idx}/{len(order)}] {country['name']} ({cid}) - kafli do zrobienia: {len(tiles)}")
            for n, (la, lo) in enumerate(tiles, 1):
                tile_id = format_tile_id(la, lo)
                if tile_id in done or tile_id in in_flight:
                    continue
                if MAX_RUNTIME_S and time.time() - START_TIME > MAX_RUNTIME_S:
                    log(f"[Czas] Limit czasu zadania - {tile_id} i kolejne zbudują się w następnym uruchomieniu")
                    break
                if not force and tile_already_exists(la, lo):
                    log(f"  ✓ {archive_name(tile_id)} już istnieje - pomijam (bez pobierania map)")
                    done.add(tile_id)
                    continue
                wait_for_slot(WORKERS)
                tiles_since_check += 1
                if tiles_since_check >= 25:
                    wait_for_slot(1)  # bezpiecznik liczy bucket, gdy nic nie jest w trakcie wysyłki
                    storage_guard(f"po kolejnych 25 kaflach ({country['name']})")
                    tiles_since_check = 0
                status(country["name"], tile_id, f"{n}/{len(tiles)} przygotowanie danych")
                sources, src_ids = sources_for(catalog, la, lo, tile_id)
                busy = set(src_ids)
                for _, (_, ids) in in_flight.items():
                    busy |= ids
                free_space(busy)  # nie usuwa danych krajów używanych przez kafle w trakcie budowy
                log(f"--- {tile_id} ({n}/{len(tiles)}) źródła: {[s[0] for s in sources]} | równolegle: {len(in_flight) + 1}/{WORKERS}")
                status(country["name"], tile_id, f"{n}/{len(tiles)} budowa")
                done.add(tile_id)
                in_flight[tile_id] = (pool.submit(run_tile, la, lo, tile_id, sources), src_ids)
        wait_for_slot(1)
    if TILE_GATEWAY_URL and ADMIN_TOKEN and not ONLY_TILES:
        log("✓ Kraje z listy gotowe - przechodzę w tryb na żądanie (kolejka z aplikacji)")
        queue_loop(catalog)
    log("🎉 KONIEC - wszystkie kafle świata przetworzone. Nieudane: " + FAILED_LOG)


if __name__ == "__main__":
    main()
