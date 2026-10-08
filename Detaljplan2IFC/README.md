# Detaljplan2IFC — Lantmäteriet detaljplan → IFC

Converts a Swedish **detaljplan** (municipal zoning plan) from Lantmäteriet's
`vnd.lm.detaljplan.v4+json` format into a georeferenced IFC 4.3.2.0 (`IFC4X3_ADD2`) reference
model, so architects can load the plan's **legal building envelopes** as context geometry with
the plan's provisions attached as inspectable IFC properties.

Two stages, run in order:

| Stage | Script | In → Out |
|---|---|---|
| 1 | [`lm_detaljplan_to_cityjson.py`](lm_detaljplan_to_cityjson.py) | detaljplan JSON → `<name>.city.json` (CityJSON 1.0) |
| 2 | [`lm_cityjson_to_ifc.py`](lm_cityjson_to_ifc.py) | `<name>.city.json` → `<name>.ifc` |

Both stages read the **same** [`schema.json`](schema.json), so they can never disagree about what
a given plan feature type means. See the [root README](../README.md) for the shared
georeferencing approach and why IFC 4.3.

## Usage

Each stage takes its input path as an argument. Output is written next to the input unless
`--output` says otherwise.

```bash
# Stage 1
python lm_detaljplan_to_cityjson.py input.json
python lm_detaljplan_to_cityjson.py input.json --output result.city.json

# Stage 2
python lm_cityjson_to_ifc.py input.city.json
python lm_cityjson_to_ifc.py input.city.json --output result.ifc
python lm_cityjson_to_ifc.py input.city.json --false-origin E N [Z]
```

| Option | Stage | Purpose |
|---|---|---|
| `--output, -o PATH` | both | Output path. Default: `<input>.city.json` / `<input>.ifc`. |
| `--schema PATH` | both | Use a different `schema.json`. |
| `--dry-run` | both | Convert and report without writing anything. |
| `--project-name NAME` | 2 | `IfcProject` name. Default: the CityJSON metadata title. |
| `--false-origin E N [Z]` | 2 | Pin the false origin, in the source file's own CRS zone. Default: the input's `transform.translate`. |

**Requirements:** Python 3.10+. Stage 1 needs no third-party packages; stage 2 needs
`pip install ifcopenshell pyproj`.

## Stage 1 — detaljplan JSON → CityJSON

Handles every geometry type Lantmäteriet's schema defines — the full `geometri.typ` enum from
`geometri-2.0.json`:

| Source `geometri.typ` | CityJSON |
|---|---|
| `kropp` (polyhedral body) | `Solid` LoD2 |
| `multikropp` (multiple bodies) | `MultiSolid` LoD2 |
| `yta` (footprint, holes preserved) | `MultiSurface` LoD1 |
| `multiyta` | `MultiSurface` LoD1 |
| `linje` (line / access route) | `MultiLineString` LoD1 |
| `multilinje` | `MultiLineString` LoD1 |
| `punkt` | `MultiPoint` LoD1 |
| `multipunkt` | `MultiPoint` LoD1 |
| `multigeometri` (mixed collection) | dispatched per member, as above |
| the `detaljplan` boundary polygon | `LandUse` / `MultiSurface` LoD1 |

Which feature types are recognised and which attributes are copied through is read from
`schema.json` (`feature_types`, `attributes.simple_keys`).

**Coordinate reference system**
- *Horizontal:* detected per file from the source's own `koordinatsystemPlan` — SWEREF 99 TM or
  one of its 12 regional zones (`EPSG:3006`–`EPSG:3018`). The source says which; this is never
  guessed. A file with no CRS is **rejected**, not defaulted.
- *Vertical:* `EPSG:5613` (RH 2000) — fixed, because the schema allows exactly one value
  nationwide.

`transform.translate` is computed per run as that file's own minimum vertex corner, so any
detaljplan converts correctly regardless of where in Sweden it is.

### Why CityJSON 1.0 and not 1.1

The declared version is **`"1.0"`**, deliberately. Two things require it:

- **URN-style CRS identifiers.** FME's CityJSON reader only accepts the 1.0.1-style
  `urn:ogc:def:crs:EPSG::3011`; given the 1.1 URL form
  (`https://www.opengis.net/def/crs/EPSG/0/3011`) it reports *"No definition was found for
  coordinate system"*, even though that form is spec-valid. Emitting 1.0 keeps the output usable
  in an FME-based workflow.
- **`GenericCityObject`**, which this pipeline uses for non-physical plan provisions, does not
  exist in CityJSON 1.1 — it was dropped from the type enum with no equivalent.

Everything else is kept consistent with 1.0 accordingly:

| Aspect | CityJSON 1.0.1 (used here) | CityJSON 1.1 |
|---|---|---|
| `version` | `"1.0"` | `"1.1"` |
| `metadata.referenceSystem` | `urn:ogc:def:crs:EPSG::<code>` | `https://www.opengis.net/def/crs/…` |
| title key | `datasetTitle` | `title` |
| geometry `lod` | a **number** (`2`) | a **string** (`"2"`) |
| `GenericCityObject` | present | removed |

`metadata.geographicalExtent` must have exactly 6 items if present at all — it is omitted
entirely, not written as `[]`, when a conversion produces no vertices.

## Stage 2 — CityJSON → IFC

Builds the IFC directly with `ifcopenshell` and adds what a reference model needs:

- **`IfcProjectedCRS` + `IfcMapConversion`** — in whichever SWEREF 99 zone the input itself
  declares (`metadata.referenceSystem`). `IfcProjectedCRS.Name` carries the `EPSG:<code>` string,
  which is what CAD/BIM software resolves the projection from, so no zone-name lookup table is
  needed.
- **A false project origin** — defaults to the input CityJSON's own `transform.translate`, so each
  file georeferences correctly with no edits. `--false-origin E N [Z]` pins a specific point.
- **`IfcSite.RefLatitude` / `RefLongitude`** — the human-readable approximate location, via
  `pyproj`.
- **`Pset_DetaljplanBestammelse`** on every converted object, with typed Swedish plan attributes
  (`Bestammelsevarde` as `IfcText`, `SekundarEgenskapsgrans` as `IfcBoolean`, and so on).
- **Per-type display colour**, via `IfcStyledItem`/`IfcSurfaceStyle`, one shared style entity per
  distinct colour rather than one per object.

### Type mapping

Read from [`schema.json`](schema.json), keyed by `feature:typ` directly.
[`schema.xlsx`](schema.xlsx) is a readable matrix of the same information.

| `feature:typ` | CityJSON | IFC class | Notes |
|---|---|---|---|
| `detaljplan` | `LandUse` | **`IfcSite`** | special-cased — the plan boundary becomes the site itself |
| `användningsbestämmelse` | `LandUse` | `IfcGeographicElement` | `PredefinedType=USERDEFINED`, `ObjectType="LandUse"` |
| `administrativ bestämmelse` | `GenericCityObject` | `IfcCivilElement` | |
| `egenskapsbestämmelse` | `GenericCityObject` | `IfcCivilElement` | |
| *(unrecognised)* | `GenericCityObject` | `IfcCivilElement` | `default` entry |

Keying by `feature:typ` rather than by CityJSON type is deliberate: `administrativ bestämmelse`
and `egenskapsbestämmelse` currently share `IfcCivilElement`, but keeping them as separate
entries means either can be restyled or reclassified independently later.

`colour` is given as `rgb` + `alpha` in the normal graphics sense (`0` = invisible, `1` = opaque)
and is flipped to IFC's inverted `Transparency` measure in code, so the file itself stays
intuitive to edit.

> `schema.xlsx` is **generated from `schema.json` and is not read by any code.** If you edit the
> spreadsheet, carry the change back into `schema.json` by hand.

## Known limitations

- **Height provisions are not turned into geometry.** `yta` footprints stay flat LoD1 surfaces;
  only `kropp` bodies carry real 3D volume. The free-text `bestammelsevarde` provision can
  express a maximum height, a minimum height, a basement depth or an unrelated numeric value,
  with no reliable way to tell which apart, so it is copied verbatim into the `Bestammelsevarde`
  property (`IfcText`, never truncated) to be read rather than interpreted as a height.
- **Polygons with holes are unverified.** Every ring — outer boundary and holes alike — is
  written with `Orientation=True`. That should be correct, since CityJSON's right-hand-rule
  convention (exterior CCW, holes CW) matches what IFC's B-rep expects, but it has not been
  confirmed against real data containing a hole. If you convert a plan that has one, check the
  hole is actually subtracted before trusting the result.
- Attributes that are nested structures — `planbeskrivning`, `planeringsunderlag`,
  `ursprungligGeometri` — are not extracted. Flat and single-level-nested fields are.
- The console report uses box-drawing characters; see the root README's note about
  `PYTHONIOENCODING=utf-8`.
