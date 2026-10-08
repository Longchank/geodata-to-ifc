# GML2IFC — CityGML (3CIM) → IFC

Converts CityGML 2.0 files exported from 3DCityDB with the Swedish 3CIM/trecim ADE into one
consolidated, georeferenced IFC 4.3.2.0 (`IFC4X3_ADD2`) model. Several heterogeneous export files
covering one site (Building LOD0, Building LOD2, CityFurniture, Transport) are merged into a
single output.

See the [root README](../README.md) for the shared georeferencing approach and why IFC 4.3.

## Usage

At least one input path is required — a `.gml` file, several, or a folder. Output is written next
to the script as `model.ifc` unless `--output` says otherwise.

```bash
python gml_to_ifc.py a.gml b.gml c.gml               # explicit files
python gml_to_ifc.py raw\GML                         # every *.gml in a folder
python gml_to_ifc.py --output result.ifc raw\GML
python gml_to_ifc.py --dry-run raw\GML               # convert + report, write nothing
```

| Option | Purpose |
|---|---|
| `--output, -o PATH` | Output IFC path. Default: `model.ifc` next to the script. |
| `--project-name NAME` | `IfcProject`/`IfcSite` name. Default: `GML referensmodell`. |
| `--false-origin E N [Z]` | Pin the false origin, in the inputs' own CRS. Default: the combined extent's minimum corner. |
| `--schema PATH` | Use a different `schema.json`. |
| `--dry-run` | Convert and report without writing output. |

**Requirements:** Python 3.10+, `pip install ifcopenshell pyproj`. XML parsing is stdlib-only.

## What it extracts

### `bldg:Building` + `bldg:BuildingPart`

Buildings are deduplicated by their 3CIM `trecim:objektUUID`, which is stable across separate
LOD0 and LOD2 source files. **The LOD2 solid wins whenever it exists anywhere in the inputs**;
the LOD0 footprint is used only as a fallback for buildings that have no LOD2 geometry at all.

| Source | Becomes |
|---|---|
| `bldg:lod0FootPrint` | LoD0 footprint (fallback geometry) |
| `bldg:boundedBy` → Wall/Roof/GroundSurface | assembled closed LoD2 solid, per-face semantic surface type |

The LoD2 solid is assembled from the `boundedBy` surfaces rather than read from
`bldg:lod2Solid`, which 3DCityDB exports commonly leave empty.

> Attributes travel with the geometry they came from: a building's LOD2 attributes stay bound to
> its LOD2 geometry, LOD0 to LOD0, and the LOD2 record wins wholesale. Merging attributes across
> both would pull LOD0-only survey metadata (`absolutLagesosakerhetHojd`, `metodIPlanTyp`, …)
> onto LOD2-sourced buildings.

### `frn:CityFurniture`

Each object appears twice in the source — an `_LOD1` line variant and an `_LOD2` solid variant
sharing one `objektUUID`. Both are merged into one object carrying a MultiLineString LoD1
geometry *and* a Solid LoD2 geometry.

### `tran:Track` / `tran:Road` / `tran:TrafficArea`

`lod1MultiSurface` / `lod2MultiSurface` geometries. The original CityGML class is preserved as a
`citygmlClass` attribute. Nested `trecim:Section`/`Intersection` elements carry no geometry of
their own and are folded into a `roadNetworkRole` attribute — Road and Track each have their own
tag pair in the schema (`sectionOfRoad` vs `sectionOfTrack`); `TrafficArea` has neither.

### Attributes

Common attributes (`gml:description`, `core:creationDate`, `core:externalReference`,
`core:relativeToTerrain`, the `trecim:*` ADE fields, class/function/surfaceMaterial code
values, …) are attached to every converted object as a **`Pset_CityGMLAttributes`** property set.

The list of GML tags that get extracted is intentionally **hardcoded** in `gml_to_ifc.py`
(`SIMPLE_ATTR_TAGS` / `CODE_ATTR_TAGS`) rather than living in `schema.json`, because it is tied
to the parsing logic. A useful side effect: every `elem.find()` path is built from those
constants, so `schema.json` can never become an XPath-injection surface.

## CityGML → IFC mapping

The mapping is read from [`schema.json`](schema.json) — edit that file, not the code.
[`schema.xlsx`](schema.xlsx) is a readable matrix of the same information (Overview / Class
mapping / Attributes sheets).

| GML source | Match key | IFC class | Spatial relation |
|---|---|---|---|
| `bldg:Building` / `BuildingPart` | always | `IfcBuilding` | `IfcRelAggregates` |
| `frn:CityFurniture` | `class\|function` = `50000\|51100` (*Mur*) | `IfcWall` | `IfcRelContainedInSpatialStructure` |
| `frn:CityFurniture` | `class\|function` = `50000\|51101` (*Stödmur*) | `IfcWall` | `IfcRelContainedInSpatialStructure` |
| `tran:Road` | `citygmlClass` = `Road` | `IfcRoad` | `IfcRelAggregates` |
| `tran:Track` | `citygmlClass` = `Track` | `IfcRoad` | `IfcRelAggregates` |
| `tran:TrafficArea` | `citygmlClass` = `TrafficArea` | `IfcPavement` | `IfcRelContainedInSpatialStructure` |
| anything else | *(default)* | `IfcCivilElement` | `IfcRelContainedInSpatialStructure` |

`IfcFacility` subtypes (`facility_ifc_classes` in `schema.json`) are aggregated into `IfcSite`
via `IfcRelAggregates`; everything else is contained via `IfcRelContainedInSpatialStructure`.
`IfcBuilding` is also an `IfcFacility` but has its own code path, so it isn't listed there.

### Why these mappings

- **`tran:Track` → `IfcRoad`.** CityGML 2.0's `tran:Track` is a foot/cycle/horse path, *not* a
  rail track — `tran:Railway` is the separate class for rail. Track typically carries
  `class=10000` *Vägtrafik* (road traffic) with `function=10400` *GC-väg* (combined
  pedestrian/bicycle path), which is road infrastructure.
- **CityFurniture → `IfcWall`.** "CityFurniture" is a CityGML *module* name, not a guarantee the
  content is movable furniture. The mapped codes are `class=50000` *Teknik- och miljödetalj* with
  `function=51100`/`51101` *Mur*/*Stödmur* (wall / retaining wall) — mapped to `IfcWall`, the
  standard IFC entity for wall-like structures including retaining walls. Code values come from
  [3CIM 2.0](https://github.com/3CIM/3CIM2.0)'s `CityFurniture_class.xml` /
  `CityFurniture_function.xml`.
- **Unmatched objects fall through to `IfcCivilElement` rather than being force-fit** into a
  wrong-domain class. `class=10000`/`function=10600` *Terrängtrappa* (terrain stairs) is one
  deliberate example — IFC has no outdoor-stairs infrastructure entity, and `IfcStair` is
  vocabulary for building-internal stairs. Anything routed to the default keeps its original
  CityGML element tag as a `citygmlType` property, so it stays traceable.

Any `class`/`function` combination not listed in `schema.json` lands in `IfcCivilElement` and is
counted in the report's fallback line — that count is the signal to add a mapping.

### IFC 4.3 facts worth knowing before editing `schema.json`

- **`IfcTunnel` does not exist in IFC 4.3** — it arrives in IFC 4.4. The real `IfcFacility`
  subtypes are `IfcRoad`, `IfcBridge`, `IfcRailway`, `IfcMarineFacility` and `IfcBuilding`.
- `IfcCivilElement` has no `PredefinedType` attribute.
- Names are validated at load time, so a typo fails immediately with the offending JSON key
  named — including abstract classes, which cannot be instantiated.

## Georeferencing

Geometry is stored relative to a false origin; `IfcMapConversion` carries the offset back to true
map coordinates. The horizontal CRS (EPSG code) is read from **each input file's own**
`gml:boundedBy/gml:Envelope` `srsName`, so this is not tied to any one zone. All provided inputs
must agree on the same EPSG code or the run aborts naming the mismatch.

The false origin defaults to the combined extent's own minimum corner across every input, so
re-running on a different site stays self-contained. Override with `--false-origin E N [Z]`.

Datum (`SWEREF99`) and vertical datum (`RH2000`, EPSG:5613) have no CityGML-standard field to
detect from and remain documented Sweden-specific constants.

## Input validation

Every input is checked **before any of them is parsed**, so a bad file in a batch fails at the
start rather than halfway through. The console names the file and the reason in each case.

A run **aborts** when:

- an input **declares a DOCTYPE/DTD** — a DTD can declare entities whose expansion exhausts
  memory, and CityGML (being XML-Schema based) never needs one;
- an input has **no readable `srsName`/EPSG code** — there is deliberately no default zone,
  because guessing one mis-georeferences the whole model while still producing a file that looks
  correct;
- the inputs **disagree on their EPSG code** — one consolidated model needs one CRS;
- **`schema.json` names an IFC class or value type that doesn't exist** in IFC 4.3, is abstract,
  or is the wrong kind.

Non-finite or malformed coordinates don't abort the run: the offending `gml:posList` is rejected
and **counted in the report**, because a single `nan` would otherwise reach the false origin and
spread to every coordinate in a file that still looked like a clean success.

## Editing `schema.json`

Class mapping lives under `mapping` (`building`, `cityfurniture.by_class_function` keyed by
`"class|function"`, `transport.by_citygml_class`) with a `default` fallback, and
`facility_ifc_classes` controls which classes aggregate into the site.

Under `attributes`, `value_types` sets the IFC type per Pset property; anything unlisted uses
`default_type`, and keys built from `core:externalReference` (`ext_*`) use `ext_prefix_type`.

> `schema.xlsx` is **generated from `schema.json` and is not read by any code**. If you edit the
> spreadsheet, the change must be carried back into `schema.json` by hand.
