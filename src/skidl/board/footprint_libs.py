"""
src/skidl/board/footprint_libs.py

Resolves a SKiDL/KiCad "Lib:Name" footprint reference (e.g.
"Package_SO:SOIC-8_3.9x4.9mm_P1.27mm") to an on-disk .kicad_mod file
under KICAD9_FOOTPRINT_DIR -- env var name preserved exactly as measured
on the target machine (Part 2 of the M0/M1 spec: 155 .pretty libraries
present with KICAD9_FOOTPRINT_DIR set, despite the machine actually
running a KiCad-10 dev fork) -- and parses out exactly what pcb_writer
needs: pads, in the footprint's own local coordinate frame, and a
courtyard bounding box.
"""

from __future__ import annotations
import os
from pathlib import Path
from typing import Optional

from skidl.board import sexp
from skidl.board.model import Pad


class FootprintNotFoundError(Exception):
    def __init__(self, lib_name: str, searched: Path):
        super().__init__(f'footprint "{lib_name}" not found (looked for {searched})')
        self.lib_name = lib_name


def _footprint_root() -> Path:
    root = os.environ.get("KICAD9_FOOTPRINT_DIR")
    if not root:
        raise EnvironmentError(
            "KICAD9_FOOTPRINT_DIR is not set -- footprint_libs can't resolve "
            "any Lib:Name reference without it (per Part 2 of the spec: "
            "155 .pretty libraries were measured under this var on the "
            "target machine)."
        )
    return Path(root)


def resolve_path(lib_colon_name: str) -> Path:
    """"Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" ->
    .../Package_SO.pretty/SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod"""
    if ":" not in lib_colon_name:
        raise ValueError(f'expected "Lib:Name", got {lib_colon_name!r}')
    lib, name = lib_colon_name.split(":", 1)
    path = _footprint_root() / f"{lib}.pretty" / f"{name}.kicad_mod"
    if not path.is_file():
        raise FootprintNotFoundError(lib_colon_name, path)
    return path


def load_pads_and_courtyard(lib_colon_name: str) -> tuple:
    """Returns (pads: list[Pad], courtyard: (minx,miny,maxx,maxy) | None),
    both in the footprint's own local coordinate frame (footprint origin
    at 0,0, no rotation applied yet) -- Footprint.pad_world_xy /
    .courtyard_world_bbox apply placement on top of these later."""
    path = resolve_path(lib_colon_name)
    tree = sexp.read_file(path)
    fp_expr = tree[0]  # top-level is (footprint "..." ...)

    pads = [_parse_pad(pad_expr) for pad_expr in sexp.find_all(fp_expr, "pad")]
    courtyard = _parse_courtyard(fp_expr)
    return pads, courtyard


def load_footprint_source(lib_colon_name: str) -> str:
    """The RAW .kicad_mod text of the footprint -- pcb_writer embeds this
    verbatim in the board (with position/ref/net surgically injected) so
    the board keeps the library's real silkscreen, fab drawings, courtyard
    polygons and text styling instead of a pads-only reconstruction."""
    return resolve_path(lib_colon_name).read_text(encoding="utf-8", errors="replace")


def _parse_pad(pad_expr) -> Pad:
    # (pad "1" smd roundrect (at -2.4 -1.905) (size 1.5 0.6)
    #   (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) ...)
    number = sexp.atom_at(pad_expr, 1, "")
    pad_type = sexp.atom_at(pad_expr, 2, "smd")
    shape = sexp.atom_at(pad_expr, 3, "rect")

    # (at x y [ROT]) -- the optional third element is the pad's own local
    # rotation, common on rotated pins (e.g. QFP side pads).
    at = sexp.find_one(pad_expr, "at") or ["at", "0", "0"]
    x, y = float(at[1]), float(at[2])
    rotation = float(at[3]) if len(at) > 3 else 0.0

    size = sexp.find_one(pad_expr, "size") or ["size", "1", "1"]
    size_x, size_y = float(size[1]), float(size[2])

    # (drill D) circular, or (drill oval DX DY) for slots.
    drill_expr = sexp.find_one(pad_expr, "drill")
    drill = drill_y = 0.0
    if drill_expr:
        if drill_expr[1] == "oval":
            drill = float(drill_expr[2])
            drill_y = float(drill_expr[3]) if len(drill_expr) > 3 else drill
        else:
            drill = float(drill_expr[1])

    layers_expr = sexp.find_one(pad_expr, "layers")
    layers = tuple(layers_expr[1:]) if layers_expr else ("F.Cu",)

    rratio_expr = sexp.find_one(pad_expr, "roundrect_rratio")
    rratio = float(rratio_expr[1]) if rratio_expr else 0.25

    # A bare .kicad_mod library file never carries net assignments --
    # pcb_writer sets pad.net from the schematic netlist by matching
    # (ref, pad number) once the footprint is attached to a component.
    return Pad(
        number=str(number), net=None,
        x=x, y=y, size_x=size_x, size_y=size_y,
        shape=shape, pad_type=pad_type, layers=layers,
        drill=drill, drill_y=drill_y, rotation=rotation,
        roundrect_rratio=rratio,
    )


def _parse_courtyard(fp_expr) -> Optional[tuple]:
    """Courtyard is drawn as one or more fp_line / fp_rect / fp_poly
    entries on F.CrtYd (or B.CrtYd) -- M0 only needs an axis-aligned bbox
    over all of them, not the exact polygon (place/simple.py's
    courtyard-overlap check in M1 is bbox-based too)."""
    xs, ys = [], []

    def _on_courtyard_layer(entry) -> bool:
        layer_expr = sexp.find_one(entry, "layer")
        return bool(layer_expr) and layer_expr[1] in ("F.CrtYd", "B.CrtYd")

    # fp_arc included: its start/mid/end control points all lie ON the
    # arc, so skipping them (as this parser originally did) understates
    # the courtyard of arc-drawn footprints (e.g. Crystal_HC35-U bulges
    # 6 mm out via an arc) -> the placer packs neighbors INTO the part
    # (measured: SW1 shorted against Y1).
    for head in ("fp_line", "fp_rect", "fp_arc"):
        for entry in sexp.find_all(fp_expr, head):
            if not _on_courtyard_layer(entry):
                continue
            for point_head in ("start", "mid", "end"):
                pt = sexp.find_one(entry, point_head)
                if pt:
                    xs.append(float(pt[1]))
                    ys.append(float(pt[2]))

    for entry in sexp.find_all(fp_expr, "fp_poly"):
        if not _on_courtyard_layer(entry):
            continue
        pts_expr = sexp.find_one(entry, "pts")
        if pts_expr:
            for xy in sexp.find_all(pts_expr, "xy"):
                xs.append(float(xy[1]))
                ys.append(float(xy[2]))

    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))
