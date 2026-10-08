#!/usr/bin/env python3
"""
ifc_viewer.py
─────────────
Multi-file IFC viewer for sanity-checking georeferencing. Each input file's
geometry is transformed out of its own local project space into TRUE map
coordinates using that file's own IfcMapConversion/IfcProjectedCRS (the same
buildingSMART method the Detaljplan2IFC/GML2IFC pipelines write), then every
file is drawn in one scene, one solid colour per file. Each file's own local
origin is marked with a labelled dot, so you can see how far apart two models'
false origins actually are.

If two files that should coincide line up here, their georeferencing agrees,
and any misalignment elsewhere is that viewer's multi-model handling, not the
data.

See README.md at the repository root for usage.

  pip install ifcopenshell pyvista
"""

import sys
import argparse
from pathlib import Path

import numpy as np
import ifcopenshell
import ifcopenshell.geom
import pyvista as pv

COLORS = ["red", "royalblue", "limegreen", "orange", "magenta", "cyan", "gold"]


def get_map_conversion(f: ifcopenshell.file) -> dict:
    """Reads the file's own IfcMapConversion (Eastings/Northings/Height +
    rotation + scale). Falls back to identity (no offset) with a warning if
    the file has none — geometry is then shown in raw local coordinates."""
    mcs = f.by_type("IfcMapConversion")
    if not mcs:
        print(f"Warning: no IfcMapConversion found — showing local (un-georeferenced) "
              f"coordinates for this file.", file=sys.stderr)
        return dict(e=0.0, n=0.0, h=0.0, a=1.0, b=0.0, scale=1.0)
    mc = mcs[0]
    return dict(
        e=mc.Eastings, n=mc.Northings, h=mc.OrthogonalHeight or 0.0,
        a=mc.XAxisAbscissa if mc.XAxisAbscissa is not None else 1.0,
        b=mc.XAxisOrdinate if mc.XAxisOrdinate is not None else 0.0,
        scale=mc.Scale if mc.Scale is not None else 1.0,
    )


def to_true_coords(local_xyz: np.ndarray, mc: dict) -> np.ndarray:
    """local_xyz: (N, 3) array in the IFC's local project space (world coords
    within that file, i.e. after each product's own placement is applied).
    Applies the buildingSMART IfcMapConversion formula to get true map
    coordinates, so geometry from different files becomes directly comparable."""
    x, y, z = local_xyz[:, 0], local_xyz[:, 1], local_xyz[:, 2]
    e = mc["e"] + mc["scale"] * (x * mc["a"] - y * mc["b"])
    n = mc["n"] + mc["scale"] * (x * mc["b"] + y * mc["a"])
    h = mc["h"] + mc["scale"] * z
    return np.column_stack([e, n, h])


def load_mesh(path: Path) -> tuple[pv.PolyData, np.ndarray] | tuple[None, None]:
    """Tessellates every product with geometry in the file, in true map
    coordinates, into one combined PyVista mesh. Also returns that file's own
    local zero (0,0,0) converted to true map coordinates — i.e. where its
    false origin/IfcMapConversion actually places it — so the viewer can mark
    it directly and make each file's georeferencing basis visible, not just
    its geometry."""
    f = ifcopenshell.open(str(path))
    mc = get_map_conversion(f)
    local_zero_true = to_true_coords(np.zeros((1, 3)), mc)[0]

    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)

    all_verts, all_faces, skipped = [], [], 0
    vert_offset = 0
    for product in f.by_type("IfcProduct"):
        if not getattr(product, "Representation", None):
            continue
        try:
            shape = ifcopenshell.geom.create_shape(settings, product)
        except Exception:
            skipped += 1
            continue
        verts = np.array(shape.geometry.verts, dtype=float).reshape(-1, 3)
        faces = np.array(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
        all_verts.append(to_true_coords(verts, mc))
        all_faces.append(faces + vert_offset)
        vert_offset += len(verts)

    if not all_verts:
        print(f"Warning: {path.name} has no renderable geometry.", file=sys.stderr)
        return None, None
    if skipped:
        print(f"Note: {path.name} — {skipped} product(s) skipped (no triangulable geometry).",
              file=sys.stderr)

    verts = np.vstack(all_verts)
    faces = np.vstack(all_faces)
    # PyVista wants faces as [3, i0, i1, i2, 3, i0, i1, i2, ...]
    pv_faces = np.column_stack([np.full(len(faces), 3), faces]).ravel()
    return pv.PolyData(verts, pv_faces), local_zero_true


def pick_files_with_dialog() -> list[Path]:
    """Backs the in-window "Add files…" button (see SceneLoader)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.lift()
        root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(
            title="Select one or more IFC files to view together",
            filetypes=[("IFC files", "*.ifc"), ("All files", "*.*")],
        )
        root.destroy()
        return [Path(p) for p in paths]
    except Exception as e:
        print(f"Could not open file picker: {e}", file=sys.stderr)
        return []


class SceneLoader:
    """Tracks what's currently in the plotter so the "Add files…" button can
    load more on top of whatever was passed on the command line, cycling
    colors and keeping the legend in sync with every add."""

    def __init__(self, plotter: pv.Plotter):
        self.plotter = plotter
        self.legend_entries: list[tuple[str, str]] = []
        self.add_button_widget = None  # set after creation, see main()

    def add_file(self, path: Path):
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            return
        print(f"Loading {path.name}…")
        mesh, local_zero_true = load_mesh(path)
        if mesh is None:
            return
        color = COLORS[len(self.legend_entries) % len(COLORS)]
        self.plotter.add_mesh(mesh, color=color, opacity=0.6, show_edges=True,
                               edge_color="black", line_width=0.5, label=path.name)
        self.plotter.add_point_labels(
            [local_zero_true], [f"{path.name}\nlocal (0,0,0)"],
            point_color=color, text_color=color, point_size=18,
            render_points_as_spheres=True, font_size=12, shape=None,
            always_visible=True)
        self.legend_entries.append((path.name, color))
        self._refresh_legend()
        self.plotter.render()

    def add_files_via_dialog(self, _state=None):
        """Callback for the "Add files…" checkbox-button widget — a click
        toggles it on, we handle it, then flip it straight back off so it
        behaves like a momentary button rather than a persistent switch."""
        for path in pick_files_with_dialog():
            self.add_file(path)
        if self.add_button_widget is not None:
            self.add_button_widget.representation.SetState(0)

    def _refresh_legend(self):
        self.plotter.remove_legend()
        if self.legend_entries:
            self.plotter.add_legend(self.legend_entries, bcolor="white")


def main():
    parser = argparse.ArgumentParser(
        description="View multiple IFC files together, aligned by their own IfcMapConversion.")
    parser.add_argument("files", nargs="*",
                        help="Path(s) to .ifc file(s). Optional — the window opens empty and "
                             "files can be loaded with the \"Add files…\" button.")
    args = parser.parse_args()

    plotter = pv.Plotter()
    plotter.set_background("white")
    loader = SceneLoader(plotter)
    for path in args.files:
        loader.add_file(Path(path))

    loader.add_button_widget = plotter.add_checkbox_button_widget(
        loader.add_files_via_dialog, value=False,
        position=(10, 10), size=40,
        color_on="green", color_off="lightgray", background_color="white")
    plotter.add_text("<- Add files…", position=(58, 18), font_size=10, color="black")

    plotter.add_axes()
    plotter.show_grid()
    plotter.show()


if __name__ == "__main__":
    main()
