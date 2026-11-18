#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VANTURA - KOLOROWE SZLAKI TURYSTYCZNE (etap budowy kafla, zastępuje dawną Maszynę 4)

Ze szlaków OpenStreetMap (relacje route=hiking/foot/bicycle/mtb/inline_skates/horse/nordic_walking/running)
tworzy plik trails_<KAFEL>.geojson, który trafia DO PACZKI KAFLA (aplikacja szuka go w katalogu kafla).
Każdy szlak = jedna cecha (MultiLineString) z właściwościami:
   color   - kolor szlaku (#RRGGBB) z tagu colour lub osmc:symbol (np. czerwony szlak PTTK),
   type    - hiking / foot / bicycle / mtb / inline_skates / horse / nordic_walking / running,
   name, ref, network (lwn/rwn/nwn/iwn - lokalny/regionalny/krajowy/międzynarodowy).
Geometria: przycięta do obszaru kafla (z zakładką), uproszczona do ~5 m, współrzędne 5 miejsc po przecinku
-> mały plik. Naprawia błędy dawnej Maszyny 4: kafle przygraniczne mają szlaki z obu krajów (dane są już
scalone), a szlak przechodzący przez kilka kafli jest w każdym z nich.
"""
import json

import osmium
from shapely.geometry import LineString, MultiLineString, box, mapping
from shapely.ops import linemerge

ROUTE_TYPES = {"hiking", "foot", "bicycle", "mtb", "inline_skates", "horse", "nordic_walking", "running"}
COLOR_MAP = {
    "red": "#EF4444", "blue": "#3B82F6", "green": "#10B981", "yellow": "#F59E0B", "black": "#1F2937",
    "white": "#F9FAFB", "orange": "#F97316", "purple": "#8B5CF6", "brown": "#78350F", "gray": "#9CA3AF",
    "grey": "#9CA3AF",
}
TYPE_DEFAULT_COLOR = {"bicycle": "#2563EB", "mtb": "#7C3AED", "inline_skates": "#0EA5E9", "horse": "#92400E"}


def trail_color(tags, rtype):
    c = (tags.get("colour") or "").lower().strip()
    if c in COLOR_MAP:
        return COLOR_MAP[c]
    if c.startswith("#") and len(c) in (4, 7):
        return c
    sym = (tags.get("osmc:symbol") or "").lower()
    if sym:
        base = sym.split(":")[0].strip()
        if base in COLOR_MAP:
            return COLOR_MAP[base]
    return TYPE_DEFAULT_COLOR.get(rtype, "#9CA3AF")


class _Relations(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.routes = []      # (id, tags, [way ids])
        self.need = set()

    def relation(self, r):
        rt = r.tags.get("route")
        if r.tags.get("type") != "route" or rt not in ROUTE_TYPES:
            return
        ways = [m.ref for m in r.members if m.type == "w"]
        if not ways:
            return
        tags = {k: r.tags.get(k) for k in ("route", "name", "ref", "network", "colour", "osmc:symbol") if r.tags.get(k)}
        self.routes.append((r.id, tags, ways))
        self.need.update(ways)


class _Ways(osmium.SimpleHandler):
    def __init__(self, need):
        super().__init__()
        self.need = need
        self.coords = {}

    def way(self, w):
        if w.id in self.need:
            pts = [(n.lon, n.lat) for n in w.nodes if n.location.valid()]
            if len(pts) >= 2:
                self.coords[w.id] = pts


def build_trails(pbf_path, min_lat, min_lon, max_lat, max_lon, out_path):
    """Zapisuje GeoJSON szlaków dla obszaru; zwraca (liczba szlaków, rozmiar pliku w bajtach)."""
    rels = _Relations()
    rels.apply_file(pbf_path)
    if not rels.routes:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": []}, f)
        return 0, 0
    ways = _Ways(rels.need)
    ways.apply_file(pbf_path, locations=True)
    area = box(min_lon, min_lat, max_lon, max_lat)
    features = []
    for rid, tags, way_ids in rels.routes:
        lines = [LineString(ways.coords[w]) for w in way_ids if w in ways.coords]
        if not lines:
            continue
        geom = linemerge(MultiLineString(lines)) if len(lines) > 1 else lines[0]
        geom = geom.intersection(area)
        if geom.is_empty:
            continue
        geom = geom.simplify(0.00005, preserve_topology=False)
        if geom.is_empty or geom.length == 0:
            continue
        parts = [geom] if geom.geom_type == "LineString" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "LineString"]
        parts = [p for p in parts if len(p.coords) >= 2]
        if not parts:
            continue
        coords = [[[round(x, 5), round(y, 5)] for x, y in p.coords] for p in parts]
        rtype = tags.get("route")
        props = {"color": trail_color(tags, rtype), "type": rtype, "name": tags.get("name", ""),
                 "ref": tags.get("ref", ""), "network": tags.get("network", ""), "osm_id": rid}
        features.append({"type": "Feature", "properties": props,
                         "geometry": {"type": "MultiLineString", "coordinates": coords}})
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, ensure_ascii=False, separators=(",", ":"))
    import os
    return len(features), os.path.getsize(out_path)
