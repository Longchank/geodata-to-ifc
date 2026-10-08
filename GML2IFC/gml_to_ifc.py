#!/usr/bin/env python3
"""
gml_to_ifc.py
─────────────
Converts CityGML 2.0 files exported from 3DCityDB (with the Swedish 3CIM/trecim
ADE) into ONE consolidated, georeferenced IFC 4.3.2.0 (IFC4X3_ADD2) model.
Written as a direct exporter with ifcopenshell, same approach as
Detaljplan2IFC/lm_cityjson_to_ifc.py.

CityGML->IFC class mapping, the Pset name and attribute IFC types are read from
schema.json next to this script (--schema PATH overrides it).

See README.md in this folder for usage, the mapping tables and the reasoning
behind them, georeferencing, and what the input validation refuses and why.

  Python 3.10+ · pip install ifcopenshell pyproj   (XML parsing is stdlib only)
"""

import re
import sys
import json
import math
import time
import argparse
from pathlib import Path
from collections import defaultdict
from xml.etree import ElementTree as ET

import ifcopenshell
import ifcopenshell.guid as guid
import ifcopenshell.ifcopenshell_wrapper as ifc_wrapper


# ══════════════════════════════════════════════════════════════════════════════
# Constants — GML parsing
# ══════════════════════════════════════════════════════════════════════════════

NS = {
    "core":   "http://www.opengis.net/citygml/2.0",
    "gml":    "http://www.opengis.net/gml",
    "bldg":   "http://www.opengis.net/citygml/building/2.0",
    "frn":    "http://www.opengis.net/citygml/cityfurniture/2.0",
    "tran":   "http://www.opengis.net/citygml/transportation/2.0",
    "trecim": "3CIM-Sweden/2.0",
}
CORE   = f"{{{NS['core']}}}"
GML    = f"{{{NS['gml']}}}"
BLDG   = f"{{{NS['bldg']}}}"
FRN    = f"{{{NS['frn']}}}"
TRAN   = f"{{{NS['tran']}}}"
TRECIM = f"{{{NS['trecim']}}}"

TRANSPORT_TAGS = ("Track", "Road", "TrafficArea")

SEMANTIC_SURFACE_TAGS = ("WallSurface", "RoofSurface", "GroundSurface")

SIMPLE_ATTR_TAGS = [
    (GML, "description"),
    (CORE, "creationDate"),
    (CORE, "relativeToTerrain"),
    # core:_GenericApplicationPropertyOfCityObject (trecim) — applies to every CityObject
    (TRECIM, "objektUUID"),
    (TRECIM, "version"),
    (TRECIM, "status"),
    (TRECIM, "absolutLagesosakerhetHojd"),
    (TRECIM, "absolutLagesosakerhetPlan"),
    (TRECIM, "metodIHojdTyp"),
    (TRECIM, "metodIPlanTyp"),
    (TRECIM, "metodIPlanTidsperiodFran"),
    (TRECIM, "metodIPlanTidsperiodTill"),
    (TRECIM, "metodIHojdTidsperiodFran"),
    (TRECIM, "metodIHojdTidsperiodTill"),
    (TRECIM, "tidpunktForKontrollAvGeometri"),
    (TRECIM, "tidpunktForLagesbestamning"),
    (TRECIM, "validFrom"),
    (TRECIM, "validTo"),
    # bldg:_GenericApplicationPropertyOfBuildingPart (trecim)
    (TRECIM, "facadeAppearanceColor"),
    (TRECIM, "facadeBaseAppearanceColor"),
    (TRECIM, "roofAppearanceColor"),
    (TRECIM, "windowFrameAppearanceColor"),
    # frn:_GenericApplicationPropertyOfCityFurniture (trecim)
    (TRECIM, "cityFurnitureRelativeHeight"),
    (TRECIM, "cityFurnitureWidth"),
    # tran:_GenericApplicationPropertyOfRailway (trecim) — only present on Track features
    (TRECIM, "trackGauge"),
    (BLDG, "yearOfConstruction"),
    (TRAN, "surfaceMaterial"),
]

# Element text is a coded value; the codeSpace URL attribute is dropped.
CODE_ATTR_TAGS = [
    (BLDG, "class"), (BLDG, "function"),
    (FRN, "class"), (FRN, "function"),
    (TRAN, "class"), (TRAN, "function"),
    # core:_GenericApplicationPropertyOfCityObject (trecim)
    (TRECIM, "osakertLage"),
    # bldg:_GenericApplicationPropertyOfBuildingPart (trecim)
    (TRECIM, "architecturalStyle"),
    (TRECIM, "facadeAppearanceMaterial"),
    (TRECIM, "facadeBaseAppearanceMaterial"),
    (TRECIM, "roofAppearanceMaterial"),
    (TRECIM, "parametricModuleSequenceType"),
]

# citygml_class (as dispatched in TRANSPORT_TAGS) -> trecim section/intersection
# property tag names. Road and Track each have their own distinct tag pair in the
# schema (sectionOfRoad vs sectionOfTrack) — TrafficArea has neither.
ROAD_NETWORK_TAGS = {
    "Road":  ("sectionOfRoad", "intersectionOfRoad"),
    "Track": ("sectionOfTrack", "intersectionOfTrack"),
}


def local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


# ══════════════════════════════════════════════════════════════════════════════
# Vertex store — dedupes real-world float coordinates (mm precision)
# ══════════════════════════════════════════════════════════════════════════════

class VertexStore:
    """Dedupes vertices as mm-rounded real-world floats. One store is shared
    across every input GML file, since they all feed into a single IFC model in
    one coordinate system."""

    def __init__(self):
        self.verts: list[tuple[float, float, float]] = []
        self._vmap: dict[tuple[float, float, float], int] = {}
        #: gml:posList runs rejected as unusable (see _pos_list_points), surfaced
        #: in the report rather than discarded silently.
        self.rejected_coord_lists = 0

    def add(self, x: float, y: float, z: float) -> int:
        key = (round(x, 3), round(y, 3), round(z, 3))
        idx = self._vmap.get(key)
        if idx is None:
            idx = len(self.verts)
            self._vmap[key] = idx
            self.verts.append(key)
        return idx

    def extent(self) -> list[float]:
        if not self.verts:
            return []
        xs = [v[0] for v in self.verts]
        ys = [v[1] for v in self.verts]
        zs = [v[2] for v in self.verts]
        return [min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)]


# ══════════════════════════════════════════════════════════════════════════════
# Geometry parsing  (GML boundary representation → boundary index lists)
# ══════════════════════════════════════════════════════════════════════════════

def _pos_list_points(posList_text: str, vs: VertexStore) -> list[tuple]:
    """Parses a gml:posList into 3D points, rejecting the whole run (returning
    []) unless it is a triple-aligned list of finite numbers.

    The finiteness check is not cosmetic: float() accepts "nan"/"inf"/"1e400",
    and a single non-finite value would reach VertexStore.extent(), where
    min()/max() propagate nan into the false origin — and from there into every
    coordinate in the output file, which would still be written and reported as
    a clean success. Malformed numbers are rejected the same way instead of
    aborting the whole conversion for one bad ring; the count is reported."""
    try:
        nums = [float(n) for n in posList_text.split()]
    except ValueError:
        vs.rejected_coord_lists += 1
        return []
    if not nums or len(nums) % 3 != 0 or not all(math.isfinite(n) for n in nums):
        vs.rejected_coord_lists += 1
        return []
    pts = [tuple(nums[i:i + 3]) for i in range(0, len(nums), 3)]
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def ring_indices(ring_elem, vs: VertexStore) -> list[int]:
    pl = ring_elem.find(f"{GML}posList")
    if pl is None or not pl.text:
        return []
    pts = _pos_list_points(pl.text, vs)
    return [vs.add(*p) for p in pts]


def polygon_rings(poly_elem, vs: VertexStore) -> list[list[int]]:
    """gml:Polygon -> [exterior_ring, interior_ring, ...], each a list of vertex indices."""
    rings = []
    ext = poly_elem.find(f"{GML}exterior/{GML}LinearRing")
    if ext is None:
        return rings
    idx = ring_indices(ext, vs)
    if len(idx) < 3:
        return rings
    rings.append(idx)
    for interior in poly_elem.findall(f"{GML}interior/{GML}LinearRing"):
        ii = ring_indices(interior, vs)
        if len(ii) >= 3:
            rings.append(ii)
    return rings


def surface_member_polygons(container_elem, vs: VertexStore) -> list[list[list[int]]]:
    """Collects polygon-rings from every gml:surfaceMember/gml:Polygon under container_elem."""
    faces = []
    for member in container_elem.findall(f"{GML}surfaceMember"):
        poly = member.find(f"{GML}Polygon")
        if poly is None:
            continue
        rings = polygon_rings(poly, vs)
        if rings:
            faces.append(rings)
    return faces


def multisurface_to_geom(ms_elem, vs: VertexStore, lod: str) -> dict | None:
    faces = surface_member_polygons(ms_elem, vs)
    if not faces:
        return None
    return {"type": "MultiSurface", "lod": lod, "boundaries": faces}


def solid_to_geom(solid_elem, vs: VertexStore, lod: str) -> dict | None:
    cs = solid_elem.find(f"{GML}exterior/{GML}CompositeSurface")
    if cs is None:
        return None
    faces = surface_member_polygons(cs, vs)
    if not faces:
        return None
    return {"type": "Solid", "lod": lod, "boundaries": [faces]}


def linestring_to_geom(ls_elem, vs: VertexStore, lod: str) -> dict | None:
    pl = ls_elem.find(f"{GML}posList")
    if pl is None or not pl.text:
        return None
    pts = _pos_list_points(pl.text, vs)
    if len(pts) < 2:
        return None
    idx = [vs.add(*p) for p in pts]
    return {"type": "MultiLineString", "lod": lod, "boundaries": [idx]}


def semantic_solid_from_boundedby(building_elem, vs: VertexStore) -> dict | None:
    """
    Assembles a Solid LoD2 from bldg:boundedBy WallSurface/RoofSurface/
    GroundSurface children, which 3DCityDB exports commonly populate instead of
    bldg:lod2Solid, tagging each face with its semantic surface type.
    """
    faces: list[list[list[int]]] = []
    face_surface_idx: list[int] = []
    semantics_surfaces: list[dict] = []
    type_to_idx: dict[str, int] = {}

    for bb in building_elem.findall(f"{BLDG}boundedBy"):
        for child in bb:
            tag = local(child.tag)
            if tag not in SEMANTIC_SURFACE_TAGS:
                continue
            ms = child.find(f"{BLDG}lod2MultiSurface/{GML}MultiSurface")
            if ms is None:
                continue
            for member in ms.findall(f"{GML}surfaceMember"):
                poly = member.find(f"{GML}Polygon")
                if poly is None:
                    continue
                rings = polygon_rings(poly, vs)
                if not rings:
                    continue
                faces.append(rings)
                if tag not in type_to_idx:
                    type_to_idx[tag] = len(semantics_surfaces)
                    semantics_surfaces.append({"type": tag})
                face_surface_idx.append(type_to_idx[tag])

    if not faces:
        return None

    geom = {"type": "Solid", "lod": "2", "boundaries": [faces]}
    geom["semantics"] = {"surfaces": semantics_surfaces, "values": [face_surface_idx]}
    return geom


# ══════════════════════════════════════════════════════════════════════════════
# Attribute extraction
# ══════════════════════════════════════════════════════════════════════════════

def extract_attributes(elem) -> dict:
    attrs: dict = {}
    for ns, tag in SIMPLE_ATTR_TAGS + CODE_ATTR_TAGS:
        child = elem.find(f"{ns}{tag}")
        if child is not None and child.text and child.text.strip():
            attrs[tag] = child.text.strip()

    for ext in elem.findall(f"{CORE}externalReference"):
        info = ext.find(f"{CORE}informationSystem")
        name = ext.find(f"{CORE}externalObject/{CORE}name")
        if info is not None and name is not None and info.text and name.text:
            key = "ext_" + re.sub(r"[^A-Za-z0-9]+", "_", info.text.strip()).strip("_")
            if key in attrs:
                attrs[key] += "; " + name.text.strip()
            else:
                attrs[key] = name.text.strip()

    return attrs


# ══════════════════════════════════════════════════════════════════════════════
# Per-type processing
# ══════════════════════════════════════════════════════════════════════════════

def process_building(elem, ctx: dict) -> None:
    """Accumulates a Building (+ its BuildingParts) into ctx["buildings"],
    keyed by trecim:objektUUID so the same physical building appearing in
    both the LOD0 and LOD2 source files gets merged into one entry (see
    module docstring: LOD2 solid wins, LOD0 footprint is a fallback).

    Attrs and geoms from one call travel together as a unit (lod2_attrs go
    with lod2_geoms, lod0_attrs with lod0_geoms) and the LOD2 side wins
    wholesale when both exist for the same UUID — matching the old
    gather_buildings()'s behavior of discarding the ENTIRE LOD0-file record
    (attrs included) whenever an LOD2 version exists, rather than blending
    metadata across both source files' records for the same building."""
    vs, stats = ctx["vs"], ctx["stats"]
    fid = elem.get(f"{GML}id") or f"Building_{len(ctx['buildings'])}"
    attrs = extract_attributes(elem)
    geoms = []

    fp = elem.find(f"{BLDG}lod0FootPrint/{GML}MultiSurface")
    if fp is not None:
        g = multisurface_to_geom(fp, vs, "0")
        if g:
            geoms.append(g)

    solid = semantic_solid_from_boundedby(elem, vs)
    if solid:
        geoms.append(solid)

    part_attrs: dict = {}
    n_parts = 0
    for cp in elem.findall(f"{BLDG}consistsOfBuildingPart/{BLDG}BuildingPart"):
        pattrs = extract_attributes(cp)
        part_attrs.update(pattrs)
        n_parts += 1

        pfp = cp.find(f"{BLDG}lod0FootPrint/{GML}MultiSurface")
        if pfp is not None:
            g = multisurface_to_geom(pfp, vs, "0")
            if g:
                geoms.append(g)

        psolid = semantic_solid_from_boundedby(cp, vs)
        if psolid:
            geoms.append(psolid)

    if not geoms:
        stats["skipped_no_geom"] += 1
        ctx["skipped_no_geom_detail"]["Building"] += 1
        return

    # Building's own attributes win over BuildingPart's on overlap (matches
    # the old gather_buildings() precedence).
    merged_attrs = {**part_attrs, **attrs}
    uuid = merged_attrs.get("objektUUID", fid)
    has_lod2 = any(g["lod"] == "2" for g in geoms)

    entry = ctx["buildings"].setdefault(
        uuid, {"lod2_attrs": {}, "lod2_geoms": [], "lod0_attrs": {}, "lod0_geoms": [], "fid": fid})
    if has_lod2:
        entry["lod2_attrs"].update(merged_attrs)
        entry["lod2_geoms"].extend(geoms)
    else:
        entry["lod0_attrs"].update(merged_attrs)
        entry["lod0_geoms"].extend(geoms)

    stats["building"] += 1
    stats["buildingpart"] += n_parts


def process_cityfurniture(elem, ctx: dict) -> None:
    vs, stats = ctx["vs"], ctx["stats"]

    uuid_el = elem.find(f"{TRECIM}objektUUID")
    fid = (uuid_el.text.strip() if uuid_el is not None and uuid_el.text
           else elem.get(f"{GML}id") or f"CityFurniture_{len(ctx['furniture'])}")

    attrs = extract_attributes(elem)
    geoms = []

    ls = elem.find(f"{FRN}lod1Geometry/{GML}LineString")
    if ls is not None:
        g = linestring_to_geom(ls, vs, "1")
        if g:
            geoms.append(g)

    sol = elem.find(f"{FRN}lod2Geometry/{GML}Solid")
    if sol is not None:
        g = solid_to_geom(sol, vs, "2")
        if g:
            geoms.append(g)

    if not geoms:
        stats["skipped_no_geom"] += 1
        ctx["skipped_no_geom_detail"]["CityFurniture"] += 1
        return

    entry = ctx["furniture"].setdefault(fid, {"attrs": {}, "geoms": []})
    entry["attrs"].update(attrs)
    entry["geoms"].extend(geoms)


def process_transport(elem, ctx: dict, citygml_class: str) -> None:
    vs, objs, stats = ctx["vs"], ctx["objs"], ctx["stats"]
    fid = elem.get(f"{GML}id") or f"{citygml_class}_{len(objs)}"

    attrs = extract_attributes(elem)
    attrs["citygmlClass"] = citygml_class
    section_tag, intersection_tag = ROAD_NETWORK_TAGS.get(citygml_class, (None, None))
    if section_tag and elem.find(f"{TRECIM}{section_tag}/{TRECIM}Section") is not None:
        attrs["roadNetworkRole"] = "Section"
    elif intersection_tag and elem.find(f"{TRECIM}{intersection_tag}/{TRECIM}Intersection") is not None:
        attrs["roadNetworkRole"] = "Intersection"

    geoms = []
    ms1 = elem.find(f"{TRAN}lod1MultiSurface/{GML}MultiSurface")
    if ms1 is not None:
        g = multisurface_to_geom(ms1, vs, "1")
        if g:
            geoms.append(g)

    ms2 = elem.find(f"{TRAN}lod2MultiSurface/{GML}MultiSurface")
    if ms2 is not None:
        g = multisurface_to_geom(ms2, vs, "2")
        if g:
            geoms.append(g)

    if not geoms:
        stats["skipped_no_geom"] += 1
        ctx["skipped_no_geom_detail"][citygml_class] += 1
        return

    objs[fid] = {"type": "Road", "citygmlType": citygml_class, "attributes": attrs, "geometry": geoms}
    stats["road"] += 1


def process_cityobjectmember(member_elem, ctx: dict) -> None:
    for child in member_elem:
        tag = local(child.tag)
        if tag == "Building":
            process_building(child, ctx)
        elif tag == "CityFurniture":
            process_cityfurniture(child, ctx)
        elif tag in TRANSPORT_TAGS:
            process_transport(child, ctx, citygml_class=tag)
        else:
            ctx["stats"]["skipped_type"] += 1
            ctx["skipped_type_detail"][tag] += 1


# ══════════════════════════════════════════════════════════════════════════════
# CRS detection
# ══════════════════════════════════════════════════════════════════════════════

_EPSG_RE = re.compile(r"EPSG[:/](?:0/)?:?(\d{4,5})", re.IGNORECASE)


def detect_epsg_from_gml(root, path: Path) -> int:
    """Reads the horizontal EPSG code out of the source GML's own srsName —
    first gml:boundedBy/gml:Envelope (the document-level declaration), falling
    back to the first srsName found anywhere in the tree (per-geometry
    declarations) — rather than assuming a fixed zone, which would silently
    mislabel any input outside that zone. srsName appears in several forms in
    the wild ('EPSG:3011', 'urn:ogc:def:crs:EPSG::3011',
    'http://www.opengis.net/def/crs/EPSG/0/3011') — _EPSG_RE extracts the
    numeric code from any of them.

    Raises ValueError if no srsName exists anywhere in the file. There is
    deliberately no default zone: guessing one mis-georeferences the entire
    model, potentially by hundreds of kilometres, while still producing a file
    that looks correct. Same rule as detect_crs() in the sibling
    Detaljplan2IFC/lm_detaljplan_to_cityjson.py.
    """
    envelope = root.find(f"{GML}boundedBy/{GML}Envelope")
    srs = envelope.get("srsName") if envelope is not None else None
    if not srs:
        for elem in root.iter():
            srs = elem.get("srsName")
            if srs:
                break
    m = _EPSG_RE.search(srs) if srs else None
    if not m:
        if srs:
            raise ValueError(
                f"{path.name}: srsName '{srs}' carries no recognisable EPSG code — "
                f"refusing to guess a coordinate system.")
        raise ValueError(
            f"{path.name}: no srsName found anywhere in the file — refusing to guess "
            f"a coordinate system. Re-export the file with its CRS declared.")
    return int(m.group(1))


# ══════════════════════════════════════════════════════════════════════════════
# Constants — IFC mapping
# ══════════════════════════════════════════════════════════════════════════════

IFC_SCHEMA = "IFC4X3_ADD2"  # = IFC 4.3.2.0

# Datum/vertical-datum have no CityGML-standard field to detect from, so these
# two stay as a documented Sweden-specific residual (see detect_epsg_from_gml
# for the horizontal EPSG code, which IS detected per-file, not hardcoded).
CRS_DATUM       = "SWEREF99"
CRS_VERTICAL    = "RH2000"  # EPSG:5613

# CityGML-type -> IFC-class mapping, the Pset name, and attribute IFC types all
# live in schema.json (sibling file, --schema PATH overrides) — see load_schema.
DEFAULT_SCHEMA_PATH = Path(__file__).parent / "schema.json"


def load_schema(path: Path) -> dict:
    """Loads schema.json — CityGML-type -> IFC-class mapping (building/
    cityfurniture/transport/default), the Pset name, and attribute IFC types.
    Mirrors Detaljplan2IFC/lm_cityjson_to_ifc.py's load_schema."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"Schema file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Schema file is not valid JSON: {path} ({e})")

    if "mapping" not in schema or "default" not in schema:
        raise ValueError(f"Schema file missing required 'mapping'/'default' sections: {path}")
    if "ifc_class" not in schema["default"]:
        raise ValueError(f"Schema file's 'default' section missing 'ifc_class': {path}")
    if "ifc_class" not in schema["mapping"].get("building", {}):
        raise ValueError(f"Schema file's 'mapping.building' section missing 'ifc_class': {path}")
    attrs = schema.get("attributes", {})
    for required in ("pset_name", "default_type", "ext_prefix_type", "value_types"):
        if required not in attrs:
            raise ValueError(f"Schema file's 'attributes' section missing '{required}': {path}")

    _validate_ifc_names(schema, path)
    return schema


def _validate_ifc_names(schema: dict, path: Path) -> None:
    """Checks every IFC entity/value-type name in schema.json against the real
    IFC schema, so a typo fails immediately with a clear message naming the
    offending key — instead of a bare RuntimeError/TypeError out of
    create_entity() after the whole GML parse has already run."""
    decls = ifc_wrapper.schema_by_name(IFC_SCHEMA)

    def declaration(name):
        if not isinstance(name, str):
            return None
        try:
            return decls.declaration_by_name(name)
        except RuntimeError:
            return None

    mapping = schema["mapping"]
    entity_names = [("mapping.building.ifc_class", mapping["building"]["ifc_class"]),
                    ("default.ifc_class", schema["default"]["ifc_class"])]
    for key, entry in mapping.get("cityfurniture", {}).get("by_class_function", {}).items():
        entity_names.append((f"mapping.cityfurniture.by_class_function['{key}'].ifc_class",
                             entry.get("ifc_class")))
    for key, entry in mapping.get("transport", {}).get("by_citygml_class", {}).items():
        entity_names.append((f"mapping.transport.by_citygml_class['{key}'].ifc_class",
                             entry.get("ifc_class")))
    # A typo here silently nests the object under the wrong spatial relation
    # rather than erroring, so it is worth validating too.
    for name in schema.get("facility_ifc_classes", []):
        entity_names.append(("facility_ifc_classes", name))

    for where, name in entity_names:
        decl = declaration(name)
        entity = decl.as_entity() if decl is not None else None
        if entity is None:
            raise ValueError(
                f"{path}: {where} = {name!r} is not an entity in {IFC_SCHEMA}")
        if entity.is_abstract():
            raise ValueError(
                f"{path}: {where} = {name!r} is abstract in {IFC_SCHEMA} "
                f"and cannot be instantiated")

    attrs = schema["attributes"]
    type_names = [("attributes.default_type", attrs["default_type"]),
                  ("attributes.ext_prefix_type", attrs["ext_prefix_type"])]
    type_names += [(f"attributes.value_types.{k}", v) for k, v in attrs["value_types"].items()]

    for where, name in type_names:
        decl = declaration(name)
        if decl is None or decl.as_entity() is not None:
            raise ValueError(
                f"{path}: {where} = {name!r} is not an IFC value type in {IFC_SCHEMA}")


# IFC 4.3 top-level infrastructure IfcFacility subtypes — these decompose from IfcSite via
# IfcRelAggregates (like IfcBuilding), not IfcRelContainedInSpatialStructure (like a plain
# IfcElement). Which classes count as facilities is schema-driven (schema["facility_ifc_classes"]),
# this is just the type-check helper.
def facility_ifc_classes(schema: dict) -> set:
    return set(schema.get("facility_ifc_classes", []))


def resolve_ifc_class(cityobject_type: str, attrs: dict, schema: dict) -> tuple[str, bool]:
    """Maps one parsed object to an IFC entity name using schema["mapping"].
    Returns (ifc_class, is_fallback) — is_fallback is True whenever nothing more
    specific matched, so the caller can preserve the original CityGML type as an
    attribute (see schema["default"])."""
    mapping = schema["mapping"]
    ifc_class = None
    if cityobject_type == "CityFurniture":
        key = "|".join((attrs.get("class") or "", attrs.get("function") or ""))
        entry = mapping.get("cityfurniture", {}).get("by_class_function", {}).get(key)
        ifc_class = entry["ifc_class"] if entry else None
    elif cityobject_type == "Road":
        # process_transport collapses CityGML Track/Road/TrafficArea into type
        # "Road" — citygmlClass carries which.
        entry = mapping.get("transport", {}).get("by_citygml_class", {}).get(attrs.get("citygmlClass"))
        ifc_class = entry["ifc_class"] if entry else None

    if ifc_class is None:
        return schema["default"]["ifc_class"], True
    return ifc_class, False


def ifc_value_type_for(key: str, schema: dict) -> str:
    attrs = schema["attributes"]
    if key.startswith("ext_"):
        return attrs["ext_prefix_type"]
    return attrs["value_types"].get(key, attrs["default_type"])


# ══════════════════════════════════════════════════════════════════════════════
# IFC boilerplate (project, units, context, CRS, site) — mirrors lm_cityjson_to_ifc.py
# ══════════════════════════════════════════════════════════════════════════════

def create_owner_history(f: ifcopenshell.file):
    org = f.create_entity("IfcOrganization", Name="BEGEES")
    person = f.create_entity("IfcPerson", Identification="unknown", FamilyName="unknown")
    person_org = f.create_entity("IfcPersonAndOrganization",
                                  ThePerson=person, TheOrganization=org)
    app = f.create_entity("IfcApplication",
                           ApplicationDeveloper=org,
                           Version="1.0",
                           ApplicationFullName="gml_to_ifc.py",
                           ApplicationIdentifier="gml_to_ifc")
    return f.create_entity("IfcOwnerHistory",
                            OwningUser=person_org, OwningApplication=app,
                            ChangeAction="ADDED", CreationDate=int(time.time()))


def create_units(f: ifcopenshell.file):
    length = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    area   = f.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE")
    volume = f.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE")
    angle  = f.create_entity("IfcSIUnit", UnitType="PLANEANGLEUNIT", Name="RADIAN")
    return f.create_entity("IfcUnitAssignment", Units=[length, area, volume, angle])


def identity_placement(f: ifcopenshell.file):
    origin = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    z = f.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))
    x = f.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0))
    axis2placement = f.create_entity("IfcAxis2Placement3D", Location=origin, Axis=z, RefDirection=x)
    return f.create_entity("IfcLocalPlacement", RelativePlacement=axis2placement)


def create_context(f: ifcopenshell.file):
    origin = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    z = f.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))
    x = f.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0))
    axis2placement = f.create_entity("IfcAxis2Placement3D", Location=origin, Axis=z, RefDirection=x)
    true_north = f.create_entity("IfcDirection", DirectionRatios=(0.0, 1.0))
    context = f.create_entity("IfcGeometricRepresentationContext",
                               ContextType="Model", CoordinateSpaceDimension=3,
                               Precision=1e-5, WorldCoordinateSystem=axis2placement,
                               TrueNorth=true_north)
    body = f.create_entity("IfcGeometricRepresentationSubContext",
                            ContextIdentifier="Body", ContextType="Model",
                            ParentContext=context, TargetView="MODEL_VIEW")
    return context, body


def create_projected_crs_and_map_conversion(f: ifcopenshell.file, context, false_origin,
                                             crs_name: str):
    crs = f.create_entity("IfcProjectedCRS",
                           Name=crs_name, Description=crs_name,
                           GeodeticDatum=CRS_DATUM, VerticalDatum=CRS_VERTICAL)
    f.create_entity("IfcMapConversion",
                     SourceCRS=context, TargetCRS=crs,
                     Eastings=false_origin[0], Northings=false_origin[1],
                     OrthogonalHeight=false_origin[2],
                     XAxisAbscissa=1.0, XAxisOrdinate=0.0, Scale=1.0)
    return crs


def deg_to_compound(deg: float) -> list[int]:
    sign = -1 if deg < 0 else 1
    deg = abs(deg)
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s_full = (m_full - m) * 60
    s = int(s_full)
    micro = round((s_full - s) * 1_000_000)
    return [sign * d, m, s, micro]


def ref_lat_lon_elev(false_origin, epsg_code: int):
    try:
        from pyproj import Transformer
        t = Transformer.from_crs(f"EPSG:{epsg_code}", "EPSG:4326", always_xy=True)
        lon, lat = t.transform(false_origin[0], false_origin[1])
        return lat, lon, false_origin[2]
    except Exception as e:
        print(f"Warning: could not compute RefLatitude/RefLongitude ({e})", file=sys.stderr)
        return None, None, None


# ══════════════════════════════════════════════════════════════════════════════
# Geometry: parsed boundaries -> IFC Brep / SurfaceModel / Curve3D
# ══════════════════════════════════════════════════════════════════════════════

class GeometryBuilder:
    """One instance for the whole conversion — vertex indices in a parsed
    object's geometry are indices into the single shared VertexStore built
    while parsing the GML, holding real-world floats that need no
    dequantization before use."""

    def __init__(self, f: ifcopenshell.file, vertices: list, false_origin: tuple,
                 skipped_kinds: dict | None = None):
        self.f = f
        self.vertices = vertices
        self.false_origin = false_origin
        self._point_cache: dict[int, object] = {}
        self.skipped_kinds = skipped_kinds if skipped_kinds is not None else {}

    def _real_coord(self, idx: int) -> tuple[float, float, float]:
        vx, vy, vz = self.vertices[idx]
        return (
            vx - self.false_origin[0],
            vy - self.false_origin[1],
            vz - self.false_origin[2],
        )

    def point(self, idx: int):
        if idx not in self._point_cache:
            self._point_cache[idx] = self.f.create_entity(
                "IfcCartesianPoint", Coordinates=self._real_coord(idx))
        return self._point_cache[idx]

    def build_face(self, surface: list):
        bounds = []
        for i, ring in enumerate(surface):
            points = [self.point(idx) for idx in ring]
            poly_loop = self.f.create_entity("IfcPolyLoop", Polygon=points)
            bound_cls = "IfcFaceOuterBound" if i == 0 else "IfcFaceBound"
            bounds.append(self.f.create_entity(bound_cls, Bound=poly_loop, Orientation=True))
        return self.f.create_entity("IfcFace", Bounds=bounds)

    def _shells_to_breps(self, shells: list) -> list:
        breps = []
        for shell in shells:
            faces = [self.build_face(surface) for surface in shell]
            closed_shell = self.f.create_entity("IfcClosedShell", CfsFaces=faces)
            breps.append(self.f.create_entity("IfcFacetedBrep", Outer=closed_shell))
        return breps

    def solid_to_breps(self, geom: dict) -> list:
        return self._shells_to_breps(geom["boundaries"])

    def multisurface_to_surfacemodel(self, geom: dict):
        faces = [self.build_face(surface) for surface in geom["boundaries"]]
        open_shell = self.f.create_entity("IfcOpenShell", CfsFaces=faces)
        return self.f.create_entity("IfcShellBasedSurfaceModel", SbsmBoundary=[open_shell])

    def multilinestring_to_curveset(self, geom: dict):
        boundaries = geom["boundaries"]
        lines = [boundaries] if boundaries and isinstance(boundaries[0], int) else boundaries
        curves = []
        for line in lines:
            points = [self.point(idx) for idx in line]
            curves.append(self.f.create_entity("IfcPolyline", Points=points))
        return self.f.create_entity("IfcGeometricCurveSet", Elements=curves)

    #: geometry "type" values this builder can turn into an IFC representation.
    _SOLID_KINDS = ("Solid",)
    _SURFACE_KINDS = ("MultiSurface",)

    def build_shape(self, body_context, geoms: list):
        """An object's geometry entries may mix types (e.g. a LOD0-fallback
        building has only MultiSurface, a merged CityFurniture object has both
        a MultiLineString and a Solid) — group by type and emit EVERY
        representation kind actually present as its own IfcShapeRepresentation,
        rather than picking one kind and discarding the rest. Solid takes
        priority over MultiSurface for the "Body" slot when both are present,
        since a solid is strictly more complete than its own bounding
        surfaces; MultiLineString (Curve3D) always gets its own representation,
        using the "Body" identifier only when it's the sole geometry present,
        or "Axis" when co-present with a Body representation so the two don't
        collide."""
        if not geoms:
            return None
        by_kind: dict[str, list] = {}
        for g in geoms:
            by_kind.setdefault(g["type"], []).append(g)

        representations = []

        body_items = []
        body_rep_type = None
        if "Solid" in by_kind:
            for g in by_kind["Solid"]:
                body_items.extend(self.solid_to_breps(g))
            body_rep_type = "Brep"
        elif "MultiSurface" in by_kind:
            for g in by_kind["MultiSurface"]:
                body_items.append(self.multisurface_to_surfacemodel(g))
            body_rep_type = "SurfaceModel"

        if body_items:
            representations.append(self.f.create_entity(
                "IfcShapeRepresentation", ContextOfItems=body_context,
                RepresentationIdentifier="Body", RepresentationType=body_rep_type,
                Items=body_items))

        if "MultiLineString" in by_kind:
            curve_items = [self.multilinestring_to_curveset(g) for g in by_kind["MultiLineString"]]
            identifier = "Body" if not representations else "Axis"
            representations.append(self.f.create_entity(
                "IfcShapeRepresentation", ContextOfItems=body_context,
                RepresentationIdentifier=identifier, RepresentationType="Curve3D",
                Items=curve_items))

        handled = set(self._SOLID_KINDS) | set(self._SURFACE_KINDS) | {"MultiLineString"}
        for kind in set(by_kind) - handled:
            self.skipped_kinds[kind] = self.skipped_kinds.get(kind, 0) + len(by_kind[kind])

        if not representations:
            return None
        return self.f.create_entity("IfcProductDefinitionShape", Representations=representations)


# ══════════════════════════════════════════════════════════════════════════════
# Pset_CityGMLAttributes
# ══════════════════════════════════════════════════════════════════════════════

def attach_pset(f: ifcopenshell.file, owner_history, product, attributes: dict, schema: dict):
    props = []
    for key, raw in attributes.items():
        if raw is None or str(raw).strip() == "":
            continue
        ifc_type = ifc_value_type_for(key, schema)
        value = f.create_entity(ifc_type, str(raw))
        props.append(f.create_entity("IfcPropertySingleValue", Name=key, NominalValue=value))
    if not props:
        return
    pset = f.create_entity("IfcPropertySet", GlobalId=guid.new(), OwnerHistory=owner_history,
                            Name=schema["attributes"]["pset_name"], HasProperties=props)
    f.create_entity("IfcRelDefinesByProperties", GlobalId=guid.new(), OwnerHistory=owner_history,
                     RelatedObjects=[product], RelatingPropertyDefinition=pset)


# ══════════════════════════════════════════════════════════════════════════════
# Input validation — run on every file before any parsing starts
# ══════════════════════════════════════════════════════════════════════════════

# An XML prolog may only contain whitespace, <?...?> declarations/PIs and
# <!--...--> comments before the DOCTYPE (if any) and the root element.
_XML_PROLOG_NOISE = re.compile(rb"\s+|<\?.*?\?>|<!--.*?-->", re.DOTALL)

PROLOG_SCAN_BYTES = 65536


def reject_dtd(path: Path) -> None:
    """Refuses any input whose XML prolog declares a DOCTYPE, before parsing it.

    xml.etree expands entities declared in an internal DTD subset, so a ~1 KB
    file of nested entity declarations can expand to gigabytes and exhaust
    memory (the "billion laughs" attack). Entity declarations can only appear in
    a DTD, and ElementTree already refuses *undefined* entities, so rejecting
    the DOCTYPE closes that whole class without needing a third-party parser.
    CityGML is XML-Schema based (xsi:schemaLocation) and never needs a DTD, so
    no legitimate export is lost.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(PROLOG_SCAN_BYTES)
    except OSError as e:
        raise ValueError(f"Could not read {path.name}: {e}") from e

    pos = 0
    while True:
        m = _XML_PROLOG_NOISE.match(head, pos)
        if not m:
            break
        pos = m.end()

    rest = head[pos:pos + 9].upper()
    if rest.startswith(b"<!DOCTYPE"):
        raise ValueError(
            f"{path.name} declares a DOCTYPE/DTD, which this converter refuses: a DTD "
            f"can declare entities whose expansion exhausts memory. CityGML needs no "
            f"DTD — re-export the file without one.")
    if rest.startswith(b"<!") or rest.startswith(b"<?"):
        # Unterminated comment/PI, i.e. the prolog does not resolve inside the
        # scan window — refuse rather than parse something we could not inspect.
        raise ValueError(
            f"{path.name}: could not resolve the XML prolog within the first "
            f"{PROLOG_SCAN_BYTES} bytes — refusing to parse it unchecked.")


# ══════════════════════════════════════════════════════════════════════════════
# GML parsing entry point
# ══════════════════════════════════════════════════════════════════════════════

def parse_gml_files(paths: list[Path]) -> tuple[dict, int, str]:
    """Parses every input GML file into one shared ctx (vertices + buildings +
    furniture + generic objs), checking all files agree on the same EPSG code.
    Returns (ctx, epsg_code, crs_name)."""
    ctx = {
        "vs": VertexStore(),
        "buildings": {},
        "furniture": {},
        "objs": {},
        "stats": defaultdict(int),
        "skipped_type_detail": defaultdict(int),
        "skipped_no_geom_detail": defaultdict(int),
    }

    # Validate the whole input set up front, so a bad file in the batch fails
    # before any of them is parsed rather than partway through.
    for path in paths:
        reject_dtd(path)

    detected: dict[Path, int] = {}
    for path in paths:
        print(f"Reading {path.name}…")
        try:
            tree = ET.parse(path)
        except ET.ParseError as e:
            # ParseError is a SyntaxError, not a ValueError, so without this it
            # escapes main()'s handler as a raw traceback.
            raise ValueError(f"{path.name} is not well-formed XML: {e}") from e
        except OSError as e:
            raise ValueError(f"Could not read {path.name}: {e}") from e
        root = tree.getroot()
        detected[path] = detect_epsg_from_gml(root, path)
        for member in root.findall(f"{CORE}cityObjectMember"):
            process_cityobjectmember(member, ctx)

    epsg_codes = set(detected.values())
    if len(epsg_codes) > 1:
        mismatch = ", ".join(f"{p.name}=EPSG:{c}" for p, c in detected.items())
        raise ValueError(
            f"Input files disagree on CRS — {mismatch}. A single consolidated "
            f"model needs all inputs in the same CRS.")
    epsg_code = next(iter(epsg_codes))

    for fid, entry in ctx["furniture"].items():
        ctx["objs"][fid] = {
            "type": "CityFurniture",
            "attributes": entry["attrs"],
            "geometry": entry["geoms"],
        }
        ctx["stats"]["cityfurniture"] += 1

    ctx["stats"]["skipped_type_detail"] = dict(ctx["skipped_type_detail"])
    ctx["stats"]["skipped_no_geom_detail"] = dict(ctx["skipped_no_geom_detail"])

    return ctx, epsg_code, f"EPSG:{epsg_code}"


# ══════════════════════════════════════════════════════════════════════════════
# Main conversion
# ══════════════════════════════════════════════════════════════════════════════

def convert(ctx: dict, project_name: str, false_origin: tuple,
            epsg_code: int, crs_name: str, schema: dict) -> tuple[ifcopenshell.file, dict]:
    f = ifcopenshell.file(schema=IFC_SCHEMA)
    owner_history = create_owner_history(f)
    units = create_units(f)
    context, body_context = create_context(f)
    create_projected_crs_and_map_conversion(f, context, false_origin, crs_name)

    project = f.create_entity("IfcProject", GlobalId=guid.new(), OwnerHistory=owner_history,
                               Name=project_name, RepresentationContexts=[context],
                               UnitsInContext=units)

    lat, lon, elev = ref_lat_lon_elev(false_origin, epsg_code)
    site_kwargs = dict(GlobalId=guid.new(), OwnerHistory=owner_history,
                        Name=project_name, ObjectPlacement=identity_placement(f),
                        CompositionType="ELEMENT")
    if lat is not None:
        site_kwargs.update(RefLatitude=deg_to_compound(lat),
                            RefLongitude=deg_to_compound(lon),
                            RefElevation=elev)
    site = f.create_entity("IfcSite", **site_kwargs)
    f.create_entity("IfcRelAggregates", GlobalId=guid.new(), OwnerHistory=owner_history,
                     RelatingObject=project, RelatedObjects=[site])

    stats = {"IfcBuilding": 0, "IfcBuilding_lod0_fallback": 0,
              "fallback_count": 0, "skipped_geometry_kinds": {}}

    gb = GeometryBuilder(f, ctx["vs"].verts, false_origin,
                          skipped_kinds=stats["skipped_geometry_kinds"])

    # ── Buildings: LOD2 solid preferred, LOD0 footprint fallback ──────────────
    building_ifc_class = schema["mapping"]["building"]["ifc_class"]
    building_products = []
    for uuid, b in ctx["buildings"].items():
        is_fallback = not b["lod2_geoms"]
        geoms = b["lod2_geoms"] if b["lod2_geoms"] else b["lod0_geoms"]
        attrs = b["lod2_attrs"] if b["lod2_geoms"] else b["lod0_attrs"]
        shape = gb.build_shape(body_context, geoms)
        name = attrs.get("description") or uuid
        building = f.create_entity(
            building_ifc_class, GlobalId=guid.new(), OwnerHistory=owner_history,
            Name=name, ObjectPlacement=identity_placement(f), Representation=shape,
            CompositionType="ELEMENT")
        attach_pset(f, owner_history, building, attrs, schema)
        building_products.append(building)
        stats["IfcBuilding"] += 1
        if is_fallback:
            stats["IfcBuilding_lod0_fallback"] += 1

    if building_products:
        f.create_entity("IfcRelAggregates", GlobalId=guid.new(), OwnerHistory=owner_history,
                         RelatingObject=site, RelatedObjects=building_products)

    # ── CityFurniture + Transport: one generic per-object dispatch ─────────
    facility_classes = facility_ifc_classes(schema)
    contained_products = []
    facility_products = []
    for fid, o in ctx["objs"].items():
        shape = gb.build_shape(body_context, o.get("geometry", []))
        attrs = dict(o.get("attributes", {}))
        cityobject_type = o.get("type")
        name = attrs.get("description") or fid

        ifc_class, is_fallback = resolve_ifc_class(cityobject_type, attrs, schema)
        if is_fallback:
            # Never silently mislabel: keep the original CityGML type traceable
            # on whatever fell through to schema["default"]["ifc_class"].
            attrs["citygmlType"] = o.get("citygmlType", cityobject_type)
            stats["fallback_count"] += 1

        if ifc_class in facility_classes:
            product = f.create_entity(
                ifc_class, GlobalId=guid.new(), OwnerHistory=owner_history,
                Name=name, ObjectPlacement=identity_placement(f), Representation=shape,
                CompositionType="ELEMENT")
            facility_products.append(product)
        else:
            product = f.create_entity(
                ifc_class, GlobalId=guid.new(), OwnerHistory=owner_history,
                Name=name, ObjectPlacement=identity_placement(f), Representation=shape)
            contained_products.append(product)

        attach_pset(f, owner_history, product, attrs, schema)
        stats[ifc_class] = stats.get(ifc_class, 0) + 1

    if facility_products:
        f.create_entity("IfcRelAggregates", GlobalId=guid.new(), OwnerHistory=owner_history,
                         RelatingObject=site, RelatedObjects=facility_products)

    if contained_products:
        f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=guid.new(),
                         OwnerHistory=owner_history, RelatingStructure=site,
                         RelatedElements=contained_products)

    return f, stats


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def _format_detail(detail: dict | None) -> str:
    if not detail:
        return ""
    return "  {" + ", ".join(f"{k}: {v}" for k, v in sorted(detail.items())) + "}"


def print_report(in_paths: list[Path], path_out: Path, ctx: dict, stats: dict,
                  dry_run: bool, false_origin: tuple, crs_name: str, schema: dict) -> None:
    width = 60
    print("─" * width)
    print("  gml_to_ifc.py")
    print("─" * width)
    for p in in_paths:
        print(f"  input : {p.name}")
    if not dry_run:
        size_kb = path_out.stat().st_size // 1024
        print(f"  Output: {path_out.name}  ({size_kb} KB)")
    print()
    print(f"  Schema      : {IFC_SCHEMA}  (IFC 4.3.2.0)")
    print(f"  CRS         : {crs_name}, vertical {CRS_VERTICAL}")
    print(f"  False origin: E={false_origin[0]}  N={false_origin[1]}  Z={false_origin[2]}")
    print()

    cstats = ctx["stats"]
    no_geom_detail = _format_detail(cstats.get("skipped_no_geom_detail"))
    type_detail = _format_detail(cstats.get("skipped_type_detail"))
    print("  Extracted from GML:")
    print(f"    Building                        : {cstats['building']}")
    print(f"    BuildingPart                     : {cstats['buildingpart']}")
    print(f"    CityFurniture                    : {cstats['cityfurniture']}")
    print(f"    Road (Track/Road/TrafficArea)    : {cstats['road']}")
    print(f"    Skipped (no usable geometry)     : {cstats['skipped_no_geom']}{no_geom_detail}")
    print(f"    Skipped (unhandled element type) : {cstats['skipped_type']}{type_detail}")
    rejected = ctx["vs"].rejected_coord_lists
    if rejected:
        print(f"    Rejected coordinate lists        : {rejected}"
              f"  (non-finite or malformed numbers)")
    print()

    print("  IFC entities created:")
    print(f"    {'IfcBuilding':<38}: {stats['IfcBuilding']}"
          f"  ({stats['IfcBuilding_lod0_fallback']} LOD0-footprint fallback)")
    other_classes = sorted(
        k for k in stats if k.startswith("Ifc") and k not in ("IfcBuilding", "IfcBuilding_lod0_fallback")
    )
    for ifc_class in other_classes:
        print(f"    {ifc_class:<38}: {stats[ifc_class]}")
    print(f"    (unrecognised type -> {schema['default']['ifc_class']}, w/ citygmlType): "
          f"{stats['fallback_count']}")
    skipped = stats.get("skipped_geometry_kinds") or {}
    if skipped:
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(skipped.items()))
        print(f"    Skipped geometry (no IFC target)       : {{{detail}}}")
    print()
    if dry_run:
        print("  DRY RUN — no file written.")
    else:
        print("  ✓ Done.")
    print("─" * width)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def resolve_inputs(raw_inputs: list[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_inputs:
        p = Path(raw)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.gml")))
        else:
            paths.append(p)
    return paths


def main():
    parser = argparse.ArgumentParser(
        description="Convert CityGML 2.0 (.gml) files directly to one consolidated IFC4 model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "A folder argument converts every *.gml file inside it.\n"
            "Output defaults to model.ifc next to this script."
        ),
    )
    parser.add_argument("inputs", nargs="+", help="Path(s) to .gml file(s) or a folder of them")
    parser.add_argument("--output", "-o", default=None,
                         help="Output IFC path (default: model.ifc next to this script)")
    parser.add_argument("--project-name", default="GML referensmodell",
                         help="IfcProject/IfcSite name")
    parser.add_argument("--false-origin", type=float, nargs="+", metavar=("E", "N"),
                         help="Override the false project origin as 'EASTING NORTHING [HEIGHT]', "
                              "in the input files' own CRS. Default: the combined extent's "
                              "own minimum corner.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Convert and report without writing any output")
    parser.add_argument("--schema", default=None,
                         help="Path to schema.json (default: schema.json next to this script)")
    args = parser.parse_args()

    if args.false_origin and len(args.false_origin) not in (2, 3):
        parser.error("--false-origin takes 2 or 3 values: EASTING NORTHING [HEIGHT]")

    schema_path = Path(args.schema) if args.schema else DEFAULT_SCHEMA_PATH
    try:
        schema = load_schema(schema_path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    in_paths = resolve_inputs(args.inputs)
    if not in_paths:
        print(f"Error: no .gml files found in {', '.join(args.inputs)}", file=sys.stderr)
        sys.exit(1)

    missing = [p for p in in_paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"Error: file not found: {p}", file=sys.stderr)
        sys.exit(1)

    try:
        ctx, epsg_code, crs_name = parse_gml_files(in_paths)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.false_origin:
        e, n, *rest = args.false_origin
        false_origin = (e, n, rest[0] if rest else 0.0)
    else:
        extent = ctx["vs"].extent()
        false_origin = (extent[0], extent[1], extent[2]) if extent else (0.0, 0.0, 0.0)

    path_out = Path(args.output) if args.output else Path(__file__).resolve().parent / "model.ifc"

    print("\nConverting…")
    ifc_file, stats = convert(ctx, args.project_name, false_origin, epsg_code, crs_name, schema)

    if not args.dry_run:
        ifc_file.write(str(path_out))

    print_report(in_paths, path_out, ctx, stats, args.dry_run, false_origin, crs_name, schema)


if __name__ == "__main__":
    main()
