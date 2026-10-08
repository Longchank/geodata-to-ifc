#!/usr/bin/env python3
"""
lm_detaljplan_to_cityjson.py
────────────────────────────
Stage 1 of 2. Converts a Lantmäteriet detaljplan file
(vnd.lm.detaljplan.v4+json) to CityJSON 1.0, extracting every geometry type the
schema defines. Stage 2 is lm_cityjson_to_ifc.py.

Feature-type mapping and which attributes are copied through are read from
schema.json next to this script (--schema PATH overrides it) — the same file
stage 2 reads, so the two stages always agree on what each feature:typ means.

See README.md in this folder for usage, the geometry/type mapping tables, CRS
handling, and why this targets CityJSON 1.0 rather than 1.1.

  Python 3.10+   (no third-party packages needed)
"""

import json
import sys
import argparse
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# Horizontal CRS is derived per file by detect_crs() below from the source data's
# own "koordinatsystemPlan" — see there for why it can't be a constant: the schema
# allows 13 different SWEREF99 zones, not just whichever one a given plan uses.
#
# Z is stored as RH2000 metres (EPSG:5613) — the schema's "hojdsystem" enum has
# exactly one allowed value nationwide, so this one genuinely is safe to fix.
#
# FME's CityJSON reader only recognises CityJSON 1.0.1-style URN CRS identifiers
# (urn:ogc:def:crs:EPSG::<code>), not the 1.1 opengis.net/def/crs URL form — using
# the URL form makes FME report "No definition was found for coordinate system"
# even though the file is spec-valid. This is also why the output below declares
# "version": "1.0" rather than "1.1": this URN pattern, "GenericCityObject" as a
# CityObject type, and metadata's "datasetCharacterSet" key are all 1.0.1-only —
# none of them validate under the 1.1 schema (whose metadata schema is
# additionalProperties:false, and which dropped GenericCityObject from its type
# enum entirely).

# Quantisation scale: store vertices as millimetre integers
# real_coord = (integer * SCALE) + translate
SCALE = 0.001

DEFAULT_SCHEMA_PATH = Path(__file__).parent / "schema.json"


# ══════════════════════════════════════════════════════════════════════════════
# Schema (feature_types / attributes) — shared with lm_cityjson_to_ifc.py
# ══════════════════════════════════════════════════════════════════════════════

def load_schema(path: Path) -> dict:
    """Loads schema.json — feature:typ -> CityJSON type mapping and the list of
    simple attribute keys to copy through. Shared with lm_cityjson_to_ifc.py
    (which reads the same file for IFC class/color/Pset mapping), so both
    scripts always agree on what each feature:typ means without editing code.
    """
    if not path.exists():
        raise ValueError(f"Schema file not found: {path}")
    try:
        with open(path, encoding="utf-8-sig") as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Schema file {path} is not valid JSON ({e}).")

    if "feature_types" not in schema or "default" not in schema:
        raise ValueError(f"Schema file {path} is missing required top-level "
                          f"key 'feature_types' or 'default'.")
    if "cityjson_type" not in schema["default"]:
        raise ValueError(f"Schema file {path}: 'default' entry is missing 'cityjson_type'.")
    attrs = schema.get("attributes", {})
    if "simple_keys" not in attrs:
        raise ValueError(f"Schema file {path} is missing required key 'attributes.simple_keys'.")
    return schema


def cityjson_type_for(ftype: str, schema: dict) -> str:
    entry = schema["feature_types"].get(ftype)
    if entry is not None:
        return entry["cityjson_type"]
    return schema["default"]["cityjson_type"]


# ══════════════════════════════════════════════════════════════════════════════
# CRS detection
# ══════════════════════════════════════════════════════════════════════════════

def _iter_koordinatsystem_plan(src: dict):
    """Yields (feature, koordinatsystemPlan value) for every geometry object in
    the file that carries one — it's a required field per the schema, present on
    both bestammelsegeometri (bestämmelse features) and plangeometri (the
    detaljplan feature itself)."""
    for feat in src.get("features", []):
        props = feat.get("properties", {})
        for bg in props.get("bestammelsegeometri", []):
            v = bg.get("geometri", {}).get("koordinatsystemPlan")
            if v:
                yield feat, v
        for pg in props.get("plangeometri", []):
            v = pg.get("geometri", {}).get("koordinatsystemPlan")
            if v:
                yield feat, v


def detect_crs(src: dict) -> str:
    """Derives the CityJSON referenceSystem URN from the source file's own
    koordinatsystemPlan values, instead of assuming one hardcoded zone. The
    schema's koordinatsystemPlan enum spans EPSG:3006 (SWEREF99 TM) through
    EPSG:3018 — SWEREF99 TM plus all 12 of its regional zones — so a single
    fixed CRS is wrong for any detaljplan outside whichever one zone was
    originally assumed. No lookup table needed: the source already gives the
    EPSG code directly, in the same "EPSG:<code>" form CityJSON's URN wants.

    Raises ValueError if no koordinatsystemPlan value can be found anywhere in
    the file — deliberately not silently defaulting to some other plan's zone.
    """
    found: dict[str, int] = {}
    detaljplan_value = None
    for feat, v in _iter_koordinatsystem_plan(src):
        found[v] = found.get(v, 0) + 1
        if feat.get("properties", {}).get("feature:typ") == "detaljplan":
            detaljplan_value = v

    if not found:
        raise ValueError("No coordinates found in file — could not determine "
                          "koordinatsystemPlan from any feature's geometry.")

    if len(found) == 1:
        chosen = next(iter(found))
    else:
        chosen = detaljplan_value or max(found, key=found.get)
        source = ("the detaljplan boundary feature" if detaljplan_value
                  else "the most common value")
        print(f"Warning: multiple koordinatsystemPlan values found in source "
              f"file ({sorted(found)}) — using {chosen} ({source}).",
              file=sys.stderr)

    code = chosen.split(":")[-1]
    return f"urn:ogc:def:crs:EPSG::{code}"


# ══════════════════════════════════════════════════════════════════════════════
# Vertex store  (shared, deduplicated)
# ══════════════════════════════════════════════════════════════════════════════

class VertexStore:
    """Dedupes vertices as mm-rounded real-world floats while parsing (the
    translate needed to quantize to CityJSON's final integers isn't known until
    the whole file's extent has been seen — see finalize())."""

    def __init__(self):
        self._verts: list[tuple[float, float, float]] = []
        self._vmap:  dict[tuple[float, float, float], int] = {}

    def add(self, x: float, y: float, z: float) -> int:
        key = (round(x, 3), round(y, 3), round(z, 3))
        idx = self._vmap.get(key)
        if idx is None:
            idx = len(self._verts)
            self._vmap[key] = idx
            self._verts.append(key)
        return idx

    def extent(self) -> list[float]:
        if not self._verts:
            return []
        xs = [v[0] for v in self._verts]
        ys = [v[1] for v in self._verts]
        zs = [v[2] for v in self._verts]
        return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]

    def finalize(self) -> tuple[list[list[int]], list[float]]:
        """Quantizes to CityJSON's integer vertices, with translate set to this
        file's own minimum corner — so integers stay small and every conversion
        is self-contained regardless of where in the world the data is, instead
        of relying on one hardcoded site-specific offset."""
        if not self._verts:
            return [], [0.0, 0.0, 0.0]
        xs = [v[0] for v in self._verts]
        ys = [v[1] for v in self._verts]
        zs = [v[2] for v in self._verts]
        translate = [min(xs), min(ys), min(zs)]
        verts = [
            [round((v[0] - translate[0]) / SCALE),
             round((v[1] - translate[1]) / SCALE),
             round((v[2] - translate[2]) / SCALE)]
            for v in self._verts
        ]
        return verts, translate


# ══════════════════════════════════════════════════════════════════════════════
# Geometry extractors
# ══════════════════════════════════════════════════════════════════════════════

def _strip_closing_point(ring: list) -> list:
    """GeoJSON rings repeat their first point as the last; CityJSON rings don't."""
    return ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring


def _faces_from_multipolygon(coordinates: list, vs: VertexStore) -> list:
    """coordinates: [face][ring][point] = [x, y, z] (a GeoJSON MultiPolygon, one
    polygon per face) → a CityJSON Solid/MultiSolid shell: [surface][ring][vertex_index].
    Shared by kropp_to_solid (one shell) and multikropp_to_multisolid (one shell
    per GeometryCollection member)."""
    shell = []
    for face_rings in coordinates:
        surface = []
        for ring in face_rings:
            pts = _strip_closing_point(ring)
            if len(pts) < 3:
                continue
            indices = [vs.add(p[0], p[1], p[2]) for p in pts]
            surface.append(indices)
        if surface:
            shell.append(surface)
    return shell


def kropp_to_solid(position: dict, vs: VertexStore) -> dict | None:
    """'kropp' = polyhedral body stored as a MultiPolygon where each polygon
    is one face of the closed solid."""
    shell = _faces_from_multipolygon(position["coordinates"], vs)
    if not shell:
        return None
    # lod is a number under CityJSON 1.0 (a string under 1.1) — see module docstring.
    return {"type": "Solid", "lod": 2, "boundaries": [shell]}


def multikropp_to_multisolid(position: dict, vs: VertexStore) -> dict | None:
    """'multikropp' = GeoJSON GeometryCollection of MultiPolygons, one member
    per distinct solid body (e.g. several separate building volumes under one
    bestämmelse). Each member is converted the same way a single 'kropp' is."""
    shells = []
    for member in position.get("geometries", []):
        if member.get("type") != "MultiPolygon":
            continue
        shell = _faces_from_multipolygon(member.get("coordinates", []), vs)
        if shell:
            shells.append(shell)
    if not shells:
        return None
    return {"type": "MultiSolid", "lod": 2, "boundaries": shells}


def multisurface_to_geom(polygons: list, vs: VertexStore, lod: int) -> dict | None:
    """polygons: a list of GeoJSON Polygon coordinate arrays ([ring][point]) —
    i.e. a GeoJSON MultiPolygon's 'coordinates' ('multiyta'), or a single
    polygon wrapped in a list-of-one ('yta', the detaljplan boundary). Keeps
    every ring per polygon (exterior + any interior holes), not just the first."""
    default_z = 0.0
    for polygon in polygons:
        if polygon and polygon[0] and len(polygon[0][0]) > 2:
            default_z = polygon[0][0][2]
            break

    boundaries = []
    for polygon in polygons:
        surface = []
        for ring in polygon:
            pts = _strip_closing_point(ring)
            if len(pts) < 3:
                continue
            indices = [vs.add(p[0], p[1], p[2] if len(p) > 2 else default_z) for p in pts]
            surface.append(indices)
        if surface:
            boundaries.append(surface)
    if not boundaries:
        return None
    return {"type": "MultiSurface", "lod": lod, "boundaries": boundaries}


def multilinestring_to_geom(lines: list, vs: VertexStore) -> dict | None:
    """lines: a list of coordinate arrays ([point][x,y,z]) — i.e. a GeoJSON
    MultiLineString's 'coordinates' ('multilinje'), or a single line's
    coordinates wrapped in a list-of-one ('linje')."""
    boundaries = []
    for coords in lines:
        if len(coords) < 2:
            continue
        z = coords[0][2] if len(coords[0]) > 2 else 0.0
        indices = [vs.add(p[0], p[1], p[2] if len(p) > 2 else z) for p in coords]
        boundaries.append(indices)
    if not boundaries:
        return None
    return {"type": "MultiLineString", "lod": 1, "boundaries": boundaries}


def multipoint_to_geom(points: list, vs: VertexStore) -> dict | None:
    """points: a list of [x,y,(z)] coordinates — i.e. a GeoJSON MultiPoint's
    'coordinates' ('multipunkt'), or a single point wrapped in a list-of-one
    ('punkt'). CityJSON has no singular Point type; MultiPoint covers both."""
    pts = [p for p in points if p]
    if not pts:
        return None
    z = pts[0][2] if len(pts[0]) > 2 else 0.0
    indices = [vs.add(p[0], p[1], p[2] if len(p) > 2 else z) for p in pts]
    return {"type": "MultiPoint", "lod": 1, "boundaries": indices}


def multigeometri_to_geoms(position: dict, vs: VertexStore) -> list:
    """'multigeometri' = a GeoJSON GeometryCollection whose members can be any
    mix of geometry types. Dispatches each member by its own GeoJSON 'type'
    and returns every resulting geometry — a CityObject can carry more than
    one geometry entry, the same way a feature with both a kropp and a yta
    already does."""
    geoms = []
    for member in position.get("geometries", []):
        mtype  = member.get("type")
        coords = member.get("coordinates")
        if mtype == "Point":
            geom = multipoint_to_geom([coords] if coords else [], vs)
        elif mtype == "MultiPoint":
            geom = multipoint_to_geom(coords or [], vs)
        elif mtype == "LineString":
            geom = multilinestring_to_geom([coords] if coords else [], vs)
        elif mtype == "MultiLineString":
            geom = multilinestring_to_geom(coords or [], vs)
        elif mtype == "Polygon":
            geom = multisurface_to_geom([coords] if coords else [], vs, lod=1)
        elif mtype == "MultiPolygon":
            geom = multisurface_to_geom(coords or [], vs, lod=1)
        else:
            geom = None
        if geom:
            geoms.append(geom)
    return geoms


def _wrap(geom: dict | None) -> list:
    return [geom] if geom else []


# Lantmäteriet geometri.typ -> (stats key, builder(position, vs) -> list[geom dict]).
# This is the complete, closed set of 9 typ values defined by the schema
# (geometri-2.0.json) — not just the 3 that happened to show up in test files.
# Source: http://namespace.lantmateriet.se/distribution/geodatakatalog/geometri/geometri-2.0.json
GEOMETRY_BUILDERS = {
    "kropp":         ("solid_kropp",      lambda pos, vs: _wrap(kropp_to_solid(pos, vs))),
    "multikropp":    ("solid_multikropp", lambda pos, vs: _wrap(multikropp_to_multisolid(pos, vs))),
    "yta":           ("surface_yta",      lambda pos, vs: _wrap(multisurface_to_geom([pos.get("coordinates", [])], vs, lod=1))),
    "multiyta":      ("surface_multiyta", lambda pos, vs: _wrap(multisurface_to_geom(pos.get("coordinates", []), vs, lod=1))),
    "linje":         ("line",             lambda pos, vs: _wrap(multilinestring_to_geom([pos.get("coordinates", [])], vs))),
    "multilinje":    ("line_multilinje",  lambda pos, vs: _wrap(multilinestring_to_geom(pos.get("coordinates", []), vs))),
    "punkt":         ("point_punkt",      lambda pos, vs: _wrap(multipoint_to_geom([pos.get("coordinates")], vs))),
    "multipunkt":    ("point_multipunkt", lambda pos, vs: _wrap(multipoint_to_geom(pos.get("coordinates", []), vs))),
    "multigeometri": ("geometrycollection", lambda pos, vs: multigeometri_to_geoms(pos, vs)),
}


# ══════════════════════════════════════════════════════════════════════════════
# Attribute helpers
# ══════════════════════════════════════════════════════════════════════════════

def detect_title(src: dict) -> str:
    """Builds the dataset title from the detaljplan feature's own namn/beteckning.
    The FeatureCollection itself has no top-level 'title' field anywhere in the
    schema, so a plain src.get('title', ...) never matches anything real — every
    file ended up with the same generic fallback string. The plan's actual name
    lives on its detaljplan feature instead."""
    for feat in src.get("features", []):
        props = feat.get("properties", {})
        if props.get("feature:typ") != "detaljplan":
            continue
        namn = props.get("namn")
        beteckning = props.get("beteckning")
        if namn and beteckning:
            return f"{namn} ({beteckning})"
        if namn:
            return namn
    return "Detaljplan"


# The list of simple scalar properties (string/number/boolean/enum) worth
# surfacing as flat CityJSON attributes now lives in schema.json
# (attributes.simple_keys) — see load_schema() above. Deliberately still NOT
# included there — genuinely nested objects/arrays that need real structured
# extraction, not a one-line whitelist entry (same call as coClass/marking in
# the GML2IFC audit): ursprungligGeometri, planbeskrivning, planeringsunderlag.


def extract_attributes(feat: dict, schema: dict) -> dict:
    """Collect all relevant Lantmäteriet properties as a flat dict."""
    props = feat.get("properties", {})
    attrs: dict = {}

    for key in schema["attributes"]["simple_keys"]:
        v = props.get(key)
        if v is not None:
            attrs[key] = str(v)

    bv_texts = [
        str(b.get("variabelvarde", ""))
        for b in props.get("bestammelsevarde", [])
        if b.get("variabelvarde")
    ]
    if bv_texts:
        attrs["bestammelsevarde"] = "; ".join(bv_texts)

    reglerar = [str(u) for u in props.get("reglerarAnvandningsbestammelse", []) if u]
    if reglerar:
        attrs["reglerarAnvandningsbestammelse"] = "; ".join(reglerar)

    motiv = props.get("planbestammelsebeskrivning", {}).get("motiv")
    if motiv:
        attrs["motiv"] = str(motiv)

    # kvalitetsbeskrivning: a small, always-flat 4-field object in practice
    # (confirmed against a real export) — not worth deferring like the
    # genuinely nested fields above.
    kvalitet = props.get("kvalitetsbeskrivning") or {}
    for sub_key, attr_key in [
        ("digitaliseringsniva", "kvalitetDigitaliseringsniva"),
        ("beskrivningNiva", "kvalitetBeskrivningNiva"),
        ("korrigeradeGranser", "kvalitetKorrigeradeGranser"),
        ("kontrolleratPlaneringsunderlag", "kvalitetKontrolleratPlaneringsunderlag"),
    ]:
        v = kvalitet.get(sub_key)
        if v is not None:
            attrs[attr_key] = str(v)

    # beslutsinformation (detaljplan feature only): a plan can have more than
    # one decision on record (original adoption, later amendments) — prefer
    # the original "antagande av ny detaljplan" entry if present, else the
    # first one. Skips the 3 sub-fields that are themselves arrays of nested
    # document/UUID references (beslutshandling, grundkarta, planbestammelse).
    beslut_list = props.get("beslutsinformation", [])
    beslut = next(
        (b for b in beslut_list if b.get("beslutstyp") == "antagande av ny detaljplan"),
        beslut_list[0] if beslut_list else None,
    )
    if beslut:
        for sub_key in [
            "instansInomKommunen", "diarienummerKommun", "diarienummerFullmaktige",
            "beslutstyp", "datumPaborjat", "datumAntagande",
            "genomforandetid", "genomforandetidStartar", "arkividentitetKommun",
        ]:
            v = beslut.get(sub_key)
            if v is not None:
                attrs[sub_key] = str(v)
        for sub_key in ["datumLagakraft", "foregaendePlansBeteckning", "berordDomsMalnummer"]:
            values = [str(v) for v in beslut.get(sub_key, []) if v]
            if values:
                attrs[sub_key] = "; ".join(values)

    # Geodetic metadata from first geometry entry
    for bg in props.get("bestammelsegeometri", []):
        g = bg.get("geometri", {})
        if "koordinatsystemPlan" in g:
            attrs["koordinatsystemPlan"] = g["koordinatsystemPlan"]
        if "hojdsystem" in g:
            attrs["hojdsystem"] = g["hojdsystem"]
        break

    return attrs


# ══════════════════════════════════════════════════════════════════════════════
# Main conversion
# ══════════════════════════════════════════════════════════════════════════════

def convert(src: dict, schema: dict) -> tuple[dict, dict]:
    crs_uri = detect_crs(src)  # raises ValueError if the file has no usable CRS

    vs    = VertexStore()
    objs  = {}
    stats = {key: 0 for key, _ in GEOMETRY_BUILDERS.values()}
    stats["surface_plan"] = 0
    stats["skipped"] = 0
    unknown_types: set[str] = set()
    unknown_geom_types: set[str] = set()
    duplicate_fids: set[str] = set()

    for feat in src.get("features", []):
        props  = feat.get("properties", {})
        fid    = feat.get("id") or f"feature_{len(objs)}"
        ftype  = props.get("feature:typ", "")
        if ftype and ftype not in schema["feature_types"]:
            unknown_types.add(ftype)
        cj_type = cityjson_type_for(ftype, schema)
        attrs  = extract_attributes(feat, schema)
        geoms  = []

        bg_list = props.get("bestammelsegeometri", [])

        # ── bestämmelsegeometri: dispatch by geometri.typ ──────────────────────
        for bg in bg_list:
            geometri = bg.get("geometri", {})
            typ = geometri.get("typ")
            builder = GEOMETRY_BUILDERS.get(typ)
            if builder is None:
                if typ:
                    unknown_geom_types.add(typ)
                continue
            stat_key, build_fn = builder
            new_geoms = build_fn(geometri.get("position", {}), vs)
            if new_geoms:
                geoms.extend(new_geoms)
                stats[stat_key] += 1

        # ── detaljplan / plangeometri (top-level GeoJSON polygon) ─────────────
        if not geoms:
            top = feat.get("geometry")
            if top and top.get("type") == "Polygon":
                geom = multisurface_to_geom([top["coordinates"]], vs, lod=1)
                if geom:
                    geoms.append(geom)
                    stats["surface_plan"] += 1
                    cj_type = "LandUse"

            for pg in props.get("plangeometri", []):
                pos = pg.get("geometri", {}).get("position", {})
                if pos.get("type") == "Polygon":
                    geom = multisurface_to_geom([pos["coordinates"]], vs, lod=1)
                    if geom:
                        geoms.append(geom)
                        stats["surface_plan"] += 1
                        cj_type = "LandUse"

        if not geoms:
            stats["skipped"] += 1
            continue

        if fid in objs:
            duplicate_fids.add(fid)
        objs[fid] = {"type": cj_type, "attributes": attrs, "geometry": geoms}

    if unknown_types:
        print(f"Warning: unrecognized feature:typ value(s) not listed in schema.json's "
              f"feature_types — mapped using the schema's 'default' entry "
              f"({schema['default']['cityjson_type']}), double-check these are handled "
              f"correctly: {sorted(unknown_types)}",
              file=sys.stderr)
    if unknown_geom_types:
        print(f"Warning: unrecognized geometri.typ value(s) not in Lantmäteriet's "
              f"geometri-2.0 schema — skipped entirely: {sorted(unknown_geom_types)}",
              file=sys.stderr)
    if duplicate_fids:
        print(f"Warning: duplicate feature id(s) found in source file — each later "
              f"feature silently overwrote the earlier CityObject with the same id, "
              f"losing its geometry and attributes. Fix the duplicate id(s) at the "
              f"source: {sorted(duplicate_fids)}",
              file=sys.stderr)

    vertices, translate = vs.finalize()
    metadata = {
        "referenceSystem":     crs_uri,
        "datasetTitle":        detect_title(src),
        "datasetCharacterSet": "UTF-8",
    }
    extent = vs.extent()
    if extent:
        metadata["geographicalExtent"] = extent

    cityjson = {
        "type":    "CityJSON",
        "version": "1.0",
        "transform": {
            "scale":     [SCALE, SCALE, SCALE],
            "translate": translate,
        },
        "metadata": metadata,
        "CityObjects": objs,
        "vertices":    vertices,
    }

    return cityjson, stats


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def print_report(path_in: Path, path_out: Path, cj: dict, stats: dict, dry_run: bool):
    objs  = cj["CityObjects"]
    width = 60
    print("─" * width)
    print("  lm_detaljplan_to_cityjson.py")
    print("─" * width)
    print(f"  Input  : {path_in.name}")
    if not dry_run:
        size_kb = path_out.stat().st_size // 1024
        print(f"  Output : {path_out.name}  ({size_kb} KB)")
    print()
    print(f"  CityObjects : {len(objs)}")
    print(f"  Vertices    : {len(cj['vertices'])}  (deduplicated, mm precision)")
    crs_code = cj["metadata"]["referenceSystem"].rsplit(":", 1)[-1]
    print(f"  CRS         : EPSG:{crs_code}, Z = RH2000 metres")
    print()
    print("  Geometry extracted:")
    geometry_labels = [
        ("solid_kropp",      "Solid LoD2  – kropp (true 3D polyhedral body)"),
        ("solid_multikropp", "MultiSolid LoD2  – multikropp (multiple solid bodies)"),
        ("surface_yta",      "MultiSurface LoD1 – yta (footprint)"),
        ("surface_multiyta", "MultiSurface LoD1 – multiyta (multiple footprints)"),
        ("surface_plan",     "MultiSurface LoD1 – detaljplan boundary"),
        ("line",             "MultiLineString LoD1 – linje"),
        ("line_multilinje",  "MultiLineString LoD1 – multilinje"),
        ("point_punkt",      "MultiPoint LoD1 – punkt"),
        ("point_multipunkt", "MultiPoint LoD1 – multipunkt"),
        ("geometrycollection", "Mixed geometries – multigeometri"),
        ("skipped",          "Skipped (no usable geometry)"),
    ]
    label_width = max(len(label) for _, label in geometry_labels)
    for key, label in geometry_labels:
        print(f"    {label.ljust(label_width)} : {stats[key]}")
    print()
    type_counts: dict = {}
    for o in objs.values():
        type_counts[o["type"]] = type_counts.get(o["type"], 0) + 1
    print("  CityJSON object types:")
    for t, c in sorted(type_counts.items()):
        print(f"    {t}: {c}")
    print()
    if dry_run:
        print("  DRY RUN — no file written.")
    else:
        print(f"  ✓ Done.")
    print("─" * width)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Convert a Lantmäteriet detaljplan JSON file to CityJSON 1.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The output is saved next to the input as <name>.city.json",
    )
    parser.add_argument("input", help="Path to the Lantmäteriet JSON file")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path  (default: <input>.city.json)")
    parser.add_argument("--schema", default=None,
                        help="Path to schema.json (default: schema.json next to this script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Convert and report without writing any output")
    args = parser.parse_args()

    # ── Resolve input ──────────────────────────────────────────────────────────
    path_in = Path(args.input)
    if not path_in.exists():
        print(f"Error: file not found: {path_in}", file=sys.stderr)
        sys.exit(1)

    # ── Resolve output ─────────────────────────────────────────────────────────
    if args.output:
        path_out = Path(args.output)
    else:
        path_out = path_in.with_suffix("").with_suffix(".city.json")

    # ── Load ───────────────────────────────────────────────────────────────────
    print(f"Reading {path_in.name}…")
    with open(path_in, encoding="utf-8-sig") as f:
        src = json.load(f)

    if src.get("type") != "FeatureCollection":
        print("Warning: top-level 'type' is not 'FeatureCollection' — "
              "may not be a valid Lantmäteriet detaljplan file.", file=sys.stderr)

    # ── Convert ────────────────────────────────────────────────────────────────
    schema_path = Path(args.schema) if args.schema else DEFAULT_SCHEMA_PATH
    print("Converting…")
    try:
        schema = load_schema(schema_path)
        cityjson, stats = convert(src, schema)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Write ──────────────────────────────────────────────────────────────────
    if not args.dry_run:
        with open(path_out, "w", encoding="utf-8") as f:
            json.dump(cityjson, f, ensure_ascii=False, indent=2)

    # ── Report ─────────────────────────────────────────────────────────────────
    print_report(path_in, path_out, cityjson, stats, args.dry_run)


if __name__ == "__main__":
    main()
