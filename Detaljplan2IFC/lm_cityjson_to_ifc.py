#!/usr/bin/env python3
"""
lm_cityjson_to_ifc.py
─────────────────────
Stage 2 of 2. Converts the CityJSON 1.0 output of lm_detaljplan_to_cityjson.py
into a georeferenced IFC 4.3.2.0 (IFC4X3_ADD2) reference model, so architects
can load the legal building envelopes of a detaljplan (Swedish zoning plan) as
context geometry.

A direct exporter rather than a wrapper around the 3DGI/TU Delft cityjson2ifc
CLI, which supports no custom CRS/georeferencing, no false origin and no custom
property sets. This reuses that tool's type table but builds the IFC itself, so
it can also write IfcProjectedCRS/IfcMapConversion, a false project origin and
Pset_DetaljplanBestammelse.

IFC class, colour and Pset mapping are read from schema.json next to this
script (--schema PATH overrides it) — the same file stage 1 reads.

See README.md in this folder for usage, the mapping tables, georeferencing and
known limitations.

  Python 3.10+ · pip install ifcopenshell pyproj
"""

import re
import json
import sys
import time
import argparse
from pathlib import Path

import ifcopenshell
import ifcopenshell.guid as guid


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

IFC_SCHEMA = "IFC4X3_ADD2"  # = IFC 4.3.2.0

# False project origin, in the source file's own CRS map coordinates.
# All IFC geometry is stored relative to this point; IfcMapConversion
# carries the offset back to true map coordinates. It is per-dataset: by
# default it's derived from the input CityJSON's own transform.translate
# (see main()), but can be overridden with --false-origin for a specific
# file.
#
# The horizontal CRS itself is also per-dataset — detect_crs_from_cityjson()
# below reads it from the input's own metadata.referenceSystem, since
# lm_detaljplan_to_cityjson.py auto-detects one of 13 possible SWEREF99
# zones (EPSG:3006-3018) from the source data rather than assuming one
# fixed zone. IfcProjectedCRS.Name carries the "EPSG:<code>" string itself —
# that's what CAD/BIM software resolves the projection from — so no
# EPSG-to-zone-name lookup table is needed.
CRS_DATUM       = "SWEREF99"
CRS_VERTICAL    = "RH2000"  # EPSG:5613

DEFAULT_SCHEMA_PATH = Path(__file__).parent / "schema.json"


# ══════════════════════════════════════════════════════════════════════════════
# Schema (feature_types / attributes) — shared with lm_detaljplan_to_cityjson.py
# ══════════════════════════════════════════════════════════════════════════════

def load_schema(path: Path) -> dict:
    """Loads schema.json — feature:typ -> IFC class/color mapping and the
    Pset_DetaljplanBestammelse property map. Shared with
    lm_detaljplan_to_cityjson.py (which reads the same file for CityJSON
    type/attribute mapping), so both scripts always agree on what each
    feature:typ means without editing code.
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
    if "ifc_class" not in schema["default"]:
        raise ValueError(f"Schema file {path}: 'default' entry is missing 'ifc_class'.")
    attrs = schema.get("attributes", {})
    if "pset_name" not in attrs or "properties" not in attrs:
        raise ValueError(f"Schema file {path} is missing required key "
                          f"'attributes.pset_name' or 'attributes.properties'.")
    return schema


def schema_entry_for(cityobject: dict, schema: dict) -> dict:
    """The feature_types entry for this CityObject's own feature:typ attribute,
    or schema['default'] if it's missing/unrecognized (the schema's chosen
    fallback IFC class, no color, unless a color is added to 'default' too)."""
    ftype = cityobject["attributes"].get("feature:typ", "")
    return schema["feature_types"].get(ftype) or schema["default"]


# ══════════════════════════════════════════════════════════════════════════════
# Small helpers
# ══════════════════════════════════════════════════════════════════════════════

def deg_to_compound(deg: float) -> list[int]:
    """Decimal degrees -> IfcCompoundPlaneAngleMeasure [deg, min, sec, µsec]."""
    sign = -1 if deg < 0 else 1
    deg = abs(deg)
    d = int(deg)
    m_full = (deg - d) * 60
    m = int(m_full)
    s_full = (m_full - m) * 60
    s = int(s_full)
    micro = round((s_full - s) * 1_000_000)
    if micro == 1_000_000:  # rounding can carry a full second, e.g. s_full=59.9999997
        micro = 0
        s += 1
        if s == 60:
            s = 0
            m += 1
            if m == 60:
                m = 0
                d += 1
    return [sign * d, m, s, micro]


def parse_bool(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes", "ja")


# ══════════════════════════════════════════════════════════════════════════════
# IFC boilerplate (project, units, context, CRS, site)
# ══════════════════════════════════════════════════════════════════════════════

def create_owner_history(f: ifcopenshell.file):
    org = f.create_entity("IfcOrganization", Name="BEGEES")
    person = f.create_entity("IfcPerson", Identification="unknown", FamilyName="unknown")
    person_org = f.create_entity("IfcPersonAndOrganization",
                                  ThePerson=person, TheOrganization=org)
    app = f.create_entity("IfcApplication",
                           ApplicationDeveloper=org,
                           Version="1.0",
                           ApplicationFullName="lm_cityjson_to_ifc.py",
                           ApplicationIdentifier="lm_cityjson_to_ifc")
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


def detect_crs_from_cityjson(cj: dict) -> tuple[int, str]:
    """Reads the horizontal EPSG code out of the CityJSON's own
    metadata.referenceSystem (written per-file by lm_detaljplan_to_cityjson.py,
    which auto-detects one of 13 possible SWEREF99 zones — EPSG:3006 through
    3018 — from the source data, not a single fixed zone). Raises ValueError
    if the field is missing or unparseable, rather than silently assuming
    EPSG:3011 — which would georeference the IFC output to the wrong zone
    for any detaljplan outside Stockholm.

    Returns (epsg_code, crs_name e.g. "EPSG:3011"). IfcProjectedCRS.Name
    carries that same "EPSG:<code>" string — the standard buildingSMART
    convention CAD/BIM software resolves the projection from — so it also
    doubles as the human-readable label; no separate zone-name lookup needed.
    """
    ref = cj.get("metadata", {}).get("referenceSystem", "")
    m = re.search(r"EPSG::?(\d{4,5})", ref)
    if not m:
        raise ValueError(
            f"Could not find an EPSG code in metadata.referenceSystem "
            f"({ref!r}) — expected e.g. 'urn:ogc:def:crs:EPSG::3011'."
        )
    code = int(m.group(1))
    return code, f"EPSG:{code}"


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


def ref_lat_lon_elev(false_origin, epsg_code: int):
    """WGS84 lat/lon of the false origin, for IfcSite's human-readable ref."""
    try:
        from pyproj import Transformer
        t = Transformer.from_crs(f"EPSG:{epsg_code}", "EPSG:4326", always_xy=True)
        lon, lat = t.transform(false_origin[0], false_origin[1])
        return lat, lon, false_origin[2]
    except Exception as e:
        print(f"Warning: could not compute RefLatitude/RefLongitude ({e})", file=sys.stderr)
        return None, None, None


# ══════════════════════════════════════════════════════════════════════════════
# Geometry: CityJSON boundaries -> IFC Brep / SurfaceModel / Curve3D
# ══════════════════════════════════════════════════════════════════════════════

class GeometryBuilder:
    """Builds IfcCartesianPoint entities lazily, deduplicated by the
    CityJSON vertex index (the CityJSON vertex list is already globally
    deduplicated, so this keeps entity count minimal for free)."""

    def __init__(self, f: ifcopenshell.file, cj_vertices: list, scale: list, translate: list,
                 false_origin: tuple):
        self.f = f
        self.cj_vertices = cj_vertices
        self.scale = scale
        self.translate = translate
        self.false_origin = false_origin
        self._point_cache: dict[int, object] = {}
        self._style_cache: dict[tuple, object] = {}

    def style_for(self, color: dict | None):
        """One IfcSurfaceStyle per distinct (rgb, alpha) pair, shared across
        every object that uses it — keeps the file from growing an identical
        style entity per object. color is a schema.json feature_types[...]
        ["color"] dict ({"rgb": [r,g,b], "alpha": a}), or None for no styling
        (the object is left to the viewing application's own default color)."""
        if not color:
            return None
        key = (tuple(color["rgb"]), color.get("alpha", 0.0))
        if key not in self._style_cache:
            r, g, b = color["rgb"]
            # schema.json's "alpha" follows the usual graphics convention
            # (0 = invisible, 1 = fully opaque); IFC's Transparency measure is
            # the inverse (0 = opaque, 1 = invisible), so it's flipped here —
            # keeps the schema file itself intuitive for whoever edits colors.
            alpha = color.get("alpha", 1.0)
            colour = self.f.create_entity("IfcColourRgb", Red=r, Green=g, Blue=b)
            shading = self.f.create_entity("IfcSurfaceStyleShading",
                                            SurfaceColour=colour,
                                            Transparency=1.0 - alpha)
            self._style_cache[key] = self.f.create_entity(
                "IfcSurfaceStyle", Side="BOTH", Styles=[shading])
        return self._style_cache[key]

    def _apply_style(self, items: list, style):
        if style is not None:
            for item in items:
                self.f.create_entity("IfcStyledItem", Item=item, Styles=[style])

    def _real_coord(self, idx: int) -> tuple[float, float, float]:
        vx, vy, vz = self.cj_vertices[idx]
        return (
            vx * self.scale[0] + self.translate[0] - self.false_origin[0],
            vy * self.scale[1] + self.translate[1] - self.false_origin[1],
            vz * self.scale[2] + self.translate[2] - self.false_origin[2],
        )

    def point(self, idx: int):
        if idx not in self._point_cache:
            self._point_cache[idx] = self.f.create_entity(
                "IfcCartesianPoint", Coordinates=self._real_coord(idx))
        return self._point_cache[idx]

    def build_face(self, surface: list):
        """surface = list of rings; ring[0] = outer boundary, rest = holes."""
        bounds = []
        for i, ring in enumerate(surface):
            points = [self.point(idx) for idx in ring]
            poly_loop = self.f.create_entity("IfcPolyLoop", Polygon=points)
            bound_cls = "IfcFaceOuterBound" if i == 0 else "IfcFaceBound"
            bounds.append(self.f.create_entity(bound_cls, Bound=poly_loop, Orientation=True))
        return self.f.create_entity("IfcFace", Bounds=bounds)

    def solid_to_breps(self, geom: dict) -> list:
        breps = []
        for shell in geom["boundaries"]:
            faces = [self.build_face(surface) for surface in shell]
            closed_shell = self.f.create_entity("IfcClosedShell", CfsFaces=faces)
            breps.append(self.f.create_entity("IfcFacetedBrep", Outer=closed_shell))
        return breps

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

    def multipoint_to_pointset(self, geom: dict):
        """IfcGeometricCurveSet (used above for lines) only accepts curves —
        bare points need the more general IfcGeometricSet, whose Elements can
        be IfcPoint/IfcCurve/IfcSurface."""
        points = [self.point(idx) for idx in geom["boundaries"]]
        return self.f.create_entity("IfcGeometricSet", Elements=points)

    def build_shape(self, body_context, geoms: list, color: dict | None = None):
        """Groups a CityObject's geometry entries by their actual CityJSON
        type and builds one IfcShapeRepresentation per group present. A
        single feature can legitimately carry more than one geometry type
        at once (e.g. a kropp Solid plus a linje MultiLineString, from two
        separate bestammelsegeometri entries on the same feature) — so
        unlike assuming one dominant type for the whole object, every
        group present gets its own representation instead of being
        silently dropped or misinterpreted as the wrong type.

        "Body" goes to whichever solid/surface representation is primary;
        a MultiLineString representation is tagged "Axis" when paired with
        one (IFC's standard body+axis convention, e.g. beams/columns) or
        "Body" if it's the only geometry. MultiPoint gets "Body" if alone,
        else a distinct identifier so it doesn't collide with a real Body.

        color, if given (schema.json feature_types[...]["color"]), is applied
        as an IfcStyledItem to every representation item built here, via
        style_for()'s shared-per-color-value IfcSurfaceStyle cache.
        """
        if not geoms:
            return None

        style = self.style_for(color)

        by_type: dict[str, list] = {}
        for g in geoms:
            by_type.setdefault(g["type"], []).append(g)

        representations = []
        body_claimed = False

        solids = by_type.get("Solid", []) + by_type.get("MultiSolid", [])
        if solids:
            items = []
            for g in solids:
                items.extend(self.solid_to_breps(g))
            if items:
                self._apply_style(items, style)
                representations.append(("Body", "Brep", items))
                body_claimed = True

        surfaces = by_type.get("MultiSurface", [])
        if surfaces:
            items = [self.multisurface_to_surfacemodel(g) for g in surfaces]
            self._apply_style(items, style)
            identifier = "Body-Surface" if body_claimed else "Body"
            representations.append((identifier, "SurfaceModel", items))
            body_claimed = True

        lines = by_type.get("MultiLineString", [])
        if lines:
            items = [self.multilinestring_to_curveset(g) for g in lines]
            self._apply_style(items, style)
            identifier = "Axis" if body_claimed else "Body"
            representations.append((identifier, "Curve3D", items))
            body_claimed = True

        points = by_type.get("MultiPoint", [])
        if points:
            items = [self.multipoint_to_pointset(g) for g in points]
            self._apply_style(items, style)
            identifier = "Body-Point" if body_claimed else "Body"
            representations.append((identifier, "GeometricSet", items))
            body_claimed = True

        known_types = {"Solid", "MultiSolid", "MultiSurface", "MultiLineString", "MultiPoint"}
        unrecognized = sorted({g["type"] for g in geoms if g["type"] not in known_types})
        if unrecognized:
            print(f"Warning: CityObject geometry type(s) not recognised by this "
                  f"exporter, skipped entirely: {unrecognized}", file=sys.stderr)

        if not representations:
            return None

        shape_reps = [
            self.f.create_entity("IfcShapeRepresentation", ContextOfItems=body_context,
                                  RepresentationIdentifier=ident, RepresentationType=rep_type,
                                  Items=items)
            for ident, rep_type, items in representations
        ]
        return self.f.create_entity("IfcProductDefinitionShape", Representations=shape_reps)


# ══════════════════════════════════════════════════════════════════════════════
# Pset_DetaljplanBestammelse
# ══════════════════════════════════════════════════════════════════════════════

def attach_pset(f: ifcopenshell.file, owner_history, product, attributes: dict, schema: dict):
    pset_name = schema["attributes"]["pset_name"]
    property_map = schema["attributes"]["properties"]
    props = []
    for key, prop_def in property_map.items():
        if key not in attributes:
            continue
        prop_name, ifc_type = prop_def["name"], prop_def["type"]
        raw = attributes[key]
        if ifc_type == "IfcBoolean":
            value = f.create_entity(ifc_type, parse_bool(raw))
        else:
            value = f.create_entity(ifc_type, str(raw))
        props.append(f.create_entity(
            "IfcPropertySingleValue", Name=prop_name, NominalValue=value))
    if not props:
        return
    pset = f.create_entity("IfcPropertySet", GlobalId=guid.new(), OwnerHistory=owner_history,
                            Name=pset_name, HasProperties=props)
    f.create_entity("IfcRelDefinesByProperties", GlobalId=guid.new(), OwnerHistory=owner_history,
                     RelatedObjects=[product], RelatingPropertyDefinition=pset)


# ══════════════════════════════════════════════════════════════════════════════
# Main conversion
# ══════════════════════════════════════════════════════════════════════════════


def convert(cj: dict, project_name: str, false_origin: tuple,
            epsg_code: int, crs_name: str, schema: dict) -> tuple[ifcopenshell.file, dict]:
    f = ifcopenshell.file(schema=IFC_SCHEMA)
    owner_history = create_owner_history(f)
    units = create_units(f)
    context, body_context = create_context(f)
    create_projected_crs_and_map_conversion(f, context, false_origin, crs_name)

    project = f.create_entity("IfcProject", GlobalId=guid.new(), OwnerHistory=owner_history,
                               Name=project_name, RepresentationContexts=[context],
                               UnitsInContext=units)

    scale, translate = cj["transform"]["scale"], cj["transform"]["translate"]
    gb = GeometryBuilder(f, cj["vertices"], scale, translate, false_origin)

    # Keyed dynamically by whatever IFC class(es) schema.json actually
    # produces, rather than a fixed 3-class dict — so adding/renaming an
    # ifc_class in the schema doesn't also need a code change here.
    stats: dict[str, int] = {}

    site = None
    site_name = project_name
    non_site_products = []

    for fid, cityobject in cj["CityObjects"].items():
        entry = schema_entry_for(cityobject, schema)
        ifc_class = entry["ifc_class"]
        shape = gb.build_shape(body_context, cityobject["geometry"], entry.get("color"))
        placement = identity_placement(f)
        name = cityobject["attributes"].get("namn") or cityobject["attributes"].get(
            "bestammelseformulering") or fid

        if ifc_class == "IfcSite":
            if site is not None:
                # A detaljplan file should have exactly one plan-boundary
                # feature. A second one is a data-quality problem, not
                # something to silently reconcile — the earlier IfcSite is
                # still written to the file but is orphaned (never attached
                # to IfcProject), so flag it instead of losing it quietly.
                print(f"Warning: more than one detaljplan (plan-boundary) feature "
                      f"found in source file — only the last one ({fid!r}) will be "
                      f"linked into the IFC spatial structure; the earlier IfcSite "
                      f"is written but orphaned. Fix the duplicate at the source.",
                      file=sys.stderr)
            lat, lon, elev = ref_lat_lon_elev(false_origin, epsg_code)
            kwargs = dict(GlobalId=guid.new(), OwnerHistory=owner_history,
                          Name=name, ObjectPlacement=placement, Representation=shape,
                          CompositionType="ELEMENT")
            if lat is not None:
                kwargs.update(RefLatitude=deg_to_compound(lat),
                              RefLongitude=deg_to_compound(lon),
                              RefElevation=elev)
            site = f.create_entity("IfcSite", **kwargs)
            attach_pset(f, owner_history, site, cityobject["attributes"], schema)
            stats[ifc_class] = stats.get(ifc_class, 0) + 1
            site_name = name
            continue

        kwargs = dict(GlobalId=guid.new(), OwnerHistory=owner_history,
                      Name=name, ObjectPlacement=placement, Representation=shape)
        if "ifc_predefined_type" in entry:
            kwargs["PredefinedType"] = entry["ifc_predefined_type"]
        if "ifc_object_type" in entry:
            kwargs["ObjectType"] = entry["ifc_object_type"]
        product = f.create_entity(ifc_class, **kwargs)
        attach_pset(f, owner_history, product, cityobject["attributes"], schema)
        non_site_products.append(product)
        stats[ifc_class] = stats.get(ifc_class, 0) + 1

    if site is None:
        # No detaljplan-boundary feature found — a data-quality problem at
        # the source (every detaljplan file should have exactly one), not
        # something to guess around. Still create an empty site so the
        # spatial structure and CRS are valid, but tell the human.
        print("Warning: no detaljplan (plan-boundary) feature found in source file "
              "— the IFC output has an empty IfcSite with no georeferenced boundary "
              "geometry. Check the source file.", file=sys.stderr)
        site = f.create_entity("IfcSite", GlobalId=guid.new(), OwnerHistory=owner_history,
                                Name=site_name, ObjectPlacement=identity_placement(f),
                                CompositionType="ELEMENT")
        stats["IfcSite"] = stats.get("IfcSite", 0) + 1

    f.create_entity("IfcRelAggregates", GlobalId=guid.new(), OwnerHistory=owner_history,
                     RelatingObject=project, RelatedObjects=[site])
    if non_site_products:
        f.create_entity("IfcRelContainedInSpatialStructure", GlobalId=guid.new(),
                         OwnerHistory=owner_history, RelatingStructure=site,
                         RelatedElements=non_site_products)

    return f, stats


# ══════════════════════════════════════════════════════════════════════════════
# Report
# ══════════════════════════════════════════════════════════════════════════════

def print_report(path_in: Path, path_out: Path, stats: dict, dry_run: bool, false_origin: tuple,
                  crs_name: str):
    width = 60
    print("─" * width)
    print("  lm_cityjson_to_ifc.py")
    print("─" * width)
    print(f"  Input  : {path_in.name}")
    if not dry_run:
        size_kb = path_out.stat().st_size // 1024
        print(f"  Output : {path_out.name}  ({size_kb} KB)")
    print()
    print(f"  Schema      : {IFC_SCHEMA}  (IFC 4.3.2.0)")
    print(f"  CRS         : {crs_name}, vertical {CRS_VERTICAL}")
    print(f"  False origin: E={false_origin[0]}  N={false_origin[1]}  Z={false_origin[2]}")
    print()
    print("  IFC entities created:")
    label_width = max((len(cls) for cls in stats), default=0)
    for cls, count in sorted(stats.items()):
        print(f"    {cls.ljust(label_width)} : {count}")
    print()
    if dry_run:
        print("  DRY RUN — no file written.")
    else:
        print("  ✓ Done.")
    print("─" * width)


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Convert a CityJSON 1.0 detaljplan file to a georeferenced IFC 4.3.2.0 model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="The output is saved next to the input as <name>.ifc",
    )
    parser.add_argument("input", help="Path to the CityJSON file")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path (default: <input>.ifc)")
    parser.add_argument("--project-name", default=None,
                        help="IfcProject name (default: CityJSON metadata title)")
    parser.add_argument("--false-origin", type=float, nargs="+", metavar=("E", "N"),
                        help="Override the false project origin as 'EASTING NORTHING [HEIGHT]' "
                             "in the source file's own CRS zone (see metadata.referenceSystem). "
                             "Default: the input file's transform.translate.")
    parser.add_argument("--schema", default=None,
                        help="Path to schema.json (default: schema.json next to this script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Convert and report without writing any output")
    args = parser.parse_args()

    if args.false_origin and len(args.false_origin) not in (2, 3):
        parser.error("--false-origin takes 2 or 3 values: EASTING NORTHING [HEIGHT]")

    path_in = Path(args.input)
    if not path_in.exists():
        print(f"Error: file not found: {path_in}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        path_out = Path(args.output)
    else:
        path_out = path_in.with_suffix("").with_suffix(".ifc")
        if path_out.suffix != ".ifc":
            path_out = path_in.parent / (path_in.name.split(".city.json")[0] + ".ifc")

    print(f"Reading {path_in.name}…")
    with open(path_in, encoding="utf-8-sig") as f:
        try:
            cj = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error: {path_in.name} is not valid JSON ({e}).", file=sys.stderr)
            sys.exit(1)

    if cj.get("type") != "CityJSON":
        print("Warning: top-level 'type' is not 'CityJSON' — "
              "expected the output of lm_detaljplan_to_cityjson.py.", file=sys.stderr)

    metadata = cj.get("metadata", {})
    project_name = args.project_name or metadata.get("datasetTitle") or metadata.get("title") or "Detaljplan"

    try:
        epsg_code, crs_name = detect_crs_from_cityjson(cj)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.false_origin:
        e, n, *rest = args.false_origin
        false_origin = (e, n, rest[0] if rest else 0.0)
    else:
        t = cj["transform"]["translate"]
        false_origin = (t[0], t[1], t[2] if len(t) > 2 else 0.0)

    schema_path = Path(args.schema) if args.schema else DEFAULT_SCHEMA_PATH
    try:
        schema = load_schema(schema_path)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print("Converting…")
    ifc_file, stats = convert(cj, project_name, false_origin, epsg_code, crs_name, schema)

    if not args.dry_run:
        ifc_file.write(str(path_out))

    print_report(path_in, path_out, stats, args.dry_run, false_origin, crs_name)


if __name__ == "__main__":
    main()
