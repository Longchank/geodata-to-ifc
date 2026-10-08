# BEGEES — Swedish geodata → IFC reference models

Two self-contained Python pipelines that convert Swedish open/municipal geodata into
**georeferenced IFC 4.3.2.0 reference models**, plus a small viewer for sanity-checking the
result.

The point of these models is context, not design: an architect loads one alongside their own
model to see **what is legally buildable on a site** and what already exists around it, with the
source data's own attributes attached as inspectable IFC properties instead of staying locked in
a proprietary format.

| Folder | Input | Output | Docs |
|---|---|---|---|
| [`Detaljplan2IFC/`](Detaljplan2IFC/) | Lantmäteriet detaljplan JSON (`vnd.lm.detaljplan.v4+json`) | CityJSON 1.0 → IFC4X3_ADD2 | [README](Detaljplan2IFC/README.md) |
| [`GML2IFC/`](GML2IFC/) | CityGML 2.0 from 3DCityDB, Swedish 3CIM ADE | IFC4X3_ADD2 | [README](GML2IFC/README.md) |
| `ifc_viewer.py` | any IFC files | 3D window | [below](#ifc_viewerpy) |

Both pipelines write to the **same georeferencing convention**, so their outputs load together in
one coordinate system.

## Quick start

Python 3.10 or newer.

```bash
python -m venv .venv
.venv\Scripts\pip install ifcopenshell pyproj pyvista   # pyvista only for the viewer
```

All scripts are command-line tools and take their input as arguments — see each folder's README.
Run any of them with `--help` for the full option list.

> On Windows, the console report uses box-drawing characters. In a shell with a cp1252 code page,
> set `PYTHONIOENCODING=utf-8` or the report will fail with a `UnicodeEncodeError`.

## Why IFC, and why this way

- **IFC 4.3.2.0 (`IFC4X3_ADD2`)**, not IFC4 — 4.3 is the first version with real infrastructure
  classes (`IfcRoad`, `IfcPavement`, `IfcFacility`), which is what municipal street and
  plan-boundary data actually needs. The entity attribute shapes these pipelines use are
  unchanged between IFC4 and 4.3, so the upgrade was a schema-string change only.
- **Direct export with `ifcopenshell`, not a converter wrapper.** The obvious candidate,
  [3DGI/cityjson2ifc](https://github.com/3DGI/cityjson2ifc), is a thin CLI with a hardcoded
  type table, no CRS/georeferencing support and no custom property sets. Post-processing its
  output to bolt those on is more fragile than writing the export directly — so these scripts use
  its type table as a reference and build the IFC themselves.
- **Georeferencing follows the buildingSMART method**: geometry is stored in project-local
  coordinates relative to a *false origin*, and `IfcMapConversion` + `IfcProjectedCRS` carry the
  offset back to true map coordinates. This keeps coordinates small (CAD tools lose precision
  with raw SWEREF99 values in the millions) while remaining unambiguously georeferenced.
- **Nothing about the location is hardcoded.** Both pipelines read the CRS from each input file's
  own metadata and derive the false origin from the data's own extent. A file from anywhere in
  Sweden converts correctly.
- **Mapping lives in `schema.json`, not in code.** Each pipeline folder has one, so which source
  type becomes which IFC class — and which attributes land in which property — can be changed by
  editing a file. Each also ships a `schema.xlsx` as a readable matrix of the same information.

### Fail-fast behaviour

These scripts deliberately **refuse to produce a plausible-looking wrong answer** — a run aborts
rather than guessing, and every failure prints a message naming the file and the reason. See
[GML2IFC's input validation](GML2IFC/README.md#input-validation) for the specific rules.

## Data specifications and schemas used

**Lantmäteriet detaljplan v4.1** — the national specification for Swedish zoning plans, consumed
by `Detaljplan2IFC/`:
- [JSON Schema](https://namespace.lantmateriet.se/distribution/geodatakatalog/detaljplan/v4/detaljplan-4.1.json)
  — the authoritative definition the type and attribute maps are derived from
- [National data product specification (PDF)](https://www.lantmateriet.se/globalassets/temawebbar/smartare-samhallsbyggnadsprocess/nationella-specifikationer/natspec-dps-t-detaljplan-v4.1.pdf)
- [Guidance document (PDF)](https://www.lantmateriet.se/globalassets/temawebbar/smartare-samhallsbyggnadsprocess/nationella-specifikationer/vagledning-till-nationell-informationsspecifikation-detaljplan.pdf)
- [Dataset landing page](https://www.lantmateriet.se/sv/nationella-geodataplattformen/datamangder/detaljplan/)

**3CIM 2.0** — the Swedish CityGML Application Domain Extension used by the three largest
municipalities, consumed by `GML2IFC/`:
- [3CIM/3CIM2.0 on GitHub](https://github.com/3CIM/3CIM2.0) (`CityGML-3CIM.xsd`, plus the
  `CityFurniture_class.xml` / `CityFurniture_function.xml` code lists the furniture mapping is
  grounded in)

**CityJSON** — the intermediate format in `Detaljplan2IFC/`:
- [1.0.1 schema](https://3d.bk.tudelft.nl/schemas/cityjson/1.0.1/) ·
  [1.1.3 schema](https://3d.bk.tudelft.nl/schemas/cityjson/1.1.3/)
- This pipeline targets **1.0, deliberately**, not 1.1 — see
  [Detaljplan2IFC/README.md](Detaljplan2IFC/README.md#why-cityjson-10-and-not-11).

**IFC** — the output format:
- [IFC 4.3.2.0 documentation](https://ifc43-docs.standards.buildingsmart.org/)
- [Georeferencing in IFC (buildingSMART)](https://www.buildingsmart.org/standards/bsi-standards/)

**Coordinate reference systems** — SWEREF 99 TM and its 12 regional zones
(`EPSG:3006`–`EPSG:3018`) horizontally, **RH 2000** (`EPSG:5613`) vertically. The horizontal zone
is read from each input; the vertical datum is fixed because the Lantmäteriet schema allows
exactly one value nationwide.

## ifc_viewer.py

A minimal multi-file IFC viewer whose real job is **verifying georeferencing**. Each file's
geometry is transformed out of its own local project space into true map coordinates using that
file's own `IfcMapConversion`, then all files are drawn in one scene, one colour per file. Each
file's local origin is marked with a labelled dot, so you can see how far apart two models'
false origins actually are.

If two models that should coincide line up here, their georeferencing agrees — and any
misalignment you see in another BIM tool is that tool's multi-model handling, not the data.

```bash
python ifc_viewer.py a.ifc b.ifc     # open with these models
python ifc_viewer.py                 # open an empty window
```

Use the **Add files…** button (bottom-left) to load more models into the scene at any time,
including into an empty window. Needs `pyvista` in addition to `ifcopenshell`.
