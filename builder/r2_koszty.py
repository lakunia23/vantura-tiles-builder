#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VANTURA - KONTROLA KOSZTÓW CLOUDFLARE R2 (raport + bezpieczne sprzątanie)

Domyślnie TYLKO RAPORT (nic nie usuwa): ile miejsca zajmują kafle v1, v2, pliki szlaków i inne,
ile to kosztuje miesięcznie ponad darmowe 10 GB, czy wiszą niedokończone uploady.

Sprzątanie (każda opcja wymaga dodatkowo --tak, inaczej pokazuje tylko, co BY usunęła):
  --usun-szlaki            usuń trails_*.geojson (aplikacja ich nie pobiera)
  --usun-v1-zastapione     usuń kafel v1 TYLKO gdy dla tego samego kafla i języka jest już v2
  --usun-paczki-serwera    usuń vantura_server*.tar.gz (paczki instalacyjne maszyn)
  --przerwij-multipart     przerwij niedokończone uploady wieloczęściowe (zajmują miejsce)

Przykład:  python3 r2_koszty.py                       (raport)
           python3 r2_koszty.py --usun-szlaki         (pokaże, co usunie)
           python3 r2_koszty.py --usun-szlaki --tak   (usunie)
Klucze: plik .env obok skryptu (R2_ACCOUNT_ID, R2_ACCESS_KEY, R2_SECRET_KEY, R2_BUCKET_NAME).
Koszt samego raportu: 1 operacja klasy A na 1000 obiektów (pomijalny). Usuwanie obiektów jest w R2 darmowe.
"""
import os
import re
import sys

FREE_STORAGE_GB = 10.0
PRICE_GB_MONTH = 0.015      # USD, Standard storage ponad darmowy limit (cennik R2)
PRICE_CLASS_A_M = 4.50      # USD za milion operacji klasy A ponad 1 mln/miesiąc
TILE_RE = re.compile(r"^tile_(?P<id>[NS]\d{2}_[EW]\d{3})_(?P<lang>[a-z]{2})(?P<v2>_v2)?\.tar\.gz$")


def load_env():
    env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env):
        for line in open(env, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def make_client():
    import boto3
    from botocore.config import Config
    for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY", "R2_SECRET_KEY"):
        if not os.environ.get(k):
            print(f"X BŁĄD: brak {k} w pliku .env")
            sys.exit(1)
    return boto3.client("s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ["R2_ACCESS_KEY"], aws_secret_access_key=os.environ["R2_SECRET_KEY"],
                        config=Config(signature_version="s3v4"), region_name="auto")


def classify(key):
    m = TILE_RE.match(key)
    if m:
        return "kafle_v2" if m.group("v2") else "kafle_v1"
    if key.startswith("trails_") and key.endswith(".geojson"):
        return "szlaki"
    if key.startswith("vantura_server"):
        return "paczki_serwera"
    return "inne"


def run(s3, bucket, args, out=print):
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        objs += page.get("Contents", [])
    groups = {}
    for o in objs:
        g = classify(o["Key"])
        n, size = groups.get(g, (0, 0))
        groups[g] = (n + 1, size + o.get("Size", 0))
    total_gb = sum(s for _, s in groups.values()) / 1024 ** 3
    out("=" * 70)
    out(f" BUCKET {bucket}: {len(objs)} obiektów, {total_gb:.2f} GB")
    out("=" * 70)
    for g in ("kafle_v1", "kafle_v2", "szlaki", "paczki_serwera", "inne"):
        n, size = groups.get(g, (0, 0))
        out(f"  {g:16s} {n:8d} plików  {size / 1024 ** 3:10.2f} GB")
    over = max(0.0, total_gb - FREE_STORAGE_GB)
    out(f"\n  Darmowe przechowywanie: {FREE_STORAGE_GB:.0f} GB/miesiąc. Ponad limit: {over:.2f} GB")
    out(f"  Szacowany koszt przechowywania: {over * PRICE_GB_MONTH:.2f} USD/miesiąc "
        f"(+ operacje ponad darmowe limity - patrz panel Cloudflare -> R2 -> Metrics)")

    uploads = []
    try:
        for page in s3.get_paginator("list_multipart_uploads").paginate(Bucket=bucket):
            uploads += page.get("Uploads", [])
    except Exception as e:
        out(f"  (nie udało się sprawdzić uploadów wieloczęściowych: {e})")
    out(f"  Niedokończone uploady wieloczęściowe: {len(uploads)} (R2 i tak usuwa je po 7 dniach)")

    keys = {o["Key"] for o in objs}
    to_delete = []
    if "--usun-szlaki" in args:
        to_delete += [k for k in keys if classify(k) == "szlaki"]
    if "--usun-paczki-serwera" in args:
        to_delete += [k for k in keys if classify(k) == "paczki_serwera"]
    if "--usun-v1-zastapione" in args:
        for k in keys:
            m = TILE_RE.match(k)
            if m and not m.group("v2") and f"tile_{m.group('id')}_{m.group('lang')}_v2.tar.gz" in keys:
                to_delete.append(k)
    confirm = "--tak" in args
    if to_delete:
        freed = sum(o.get("Size", 0) for o in objs if o["Key"] in set(to_delete)) / 1024 ** 3
        out(f"\n  Do usunięcia: {len(to_delete)} plików, zwolni {freed:.2f} GB")
        for k in sorted(to_delete)[:10]:
            out(f"    - {k}")
        if len(to_delete) > 10:
            out(f"    ... i {len(to_delete) - 10} więcej")
        if not confirm:
            out("  (TRYB PODGLĄDU - nic nie usunięto. Dodaj --tak, aby usunąć.)")
        else:
            for i, k in enumerate(sorted(to_delete), 1):
                s3.delete_object(Bucket=bucket, Key=k)
                if i % 500 == 0:
                    out(f"    usunięto {i}/{len(to_delete)}")
            out(f"  ✓ Usunięto {len(to_delete)} plików")
    if "--przerwij-multipart" in args and uploads:
        if not confirm:
            out(f"  (TRYB PODGLĄDU - przerwałbym {len(uploads)} uploadów. Dodaj --tak.)")
        else:
            for u in uploads:
                s3.abort_multipart_upload(Bucket=bucket, Key=u["Key"], UploadId=u["UploadId"])
            out(f"  ✓ Przerwano {len(uploads)} uploadów")
    return groups, to_delete


if __name__ == "__main__":
    load_env()
    run(make_client(), os.environ.get("R2_BUCKET_NAME", "vantura-maps"), sys.argv[1:])
