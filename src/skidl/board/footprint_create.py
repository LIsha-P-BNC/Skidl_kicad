"""
src/skidl/board/footprint_create.py

Footprint CREATION -- the capability the library tools lacked (twice
blocked the real tracker board: EC200U's 144-pad LGA and TPS2117's
SOT-5X3 have no stock footprints).

Two modes, both landing in a user-writable library the entire pipeline
already resolves (KICAD9_FOOTPRINT_DIR/<lib>.pretty/<name>.anvil_mod;
the board writer embeds footprints verbatim, so the app renders them
without any fp-lib-table registration):

  install_footprint(...)  -- take a REAL .kicad_mod (vendor file,
      community library, converted CAD) and install it after validation.
  generate_footprint(...) -- build the geometry from an explicit pad
      table (numbers, positions, sizes taken from the datasheet's land
      pattern -- the human confirms the dimensions, the code does the
      drawing). Enough for LGA/QFN grids and simple leaded packages.

Discipline: NOTHING is invented. install validates the provided file;
generate only draws what the caller's dimension table states.
"""

from __future__ import annotations
import os
import re
from pathlib import Path

from skidl.board import sexp


DEFAULT_LIB = "UserFootprints"


def _footprint_root() -> Path:
    root = os.environ.get("KICAD9_FOOTPRINT_DIR")
    if not root:
        raise EnvironmentError("KICAD9_FOOTPRINT_DIR not set (import anvil_libs first)")
    return Path(root)


def install_footprint(name: str, mod_content: str = "", mod_path: str = "",
                       lib: str = DEFAULT_LIB, expected_pads: int = None) -> dict:
    """Validate + install a provided .kicad_mod. Returns pad statistics
    so the caller can confirm against the datasheet."""
    if mod_path and not mod_content:
        mod_content = Path(mod_path).read_text(encoding="utf-8", errors="replace")
    if not mod_content.strip().startswith("(footprint") and \
       not mod_content.strip().startswith("(module"):
        return {"ok": False,
                "error": "content is not a KiCad footprint (must start with "
                         "(footprint ...) -- legacy (module ...) also accepted)"}
    try:
        tree = sexp.read(mod_content)
        fp = tree[0]
        pads = [p for p in fp if isinstance(p, list) and p and p[0] == "pad"]
    except Exception as exc:
        return {"ok": False, "error": f"footprint failed to parse: {exc!r}"}
    pad_nums = [str(p[1]).strip('"') for p in pads if len(p) > 1]
    numbered = [n for n in pad_nums if n]
    if expected_pads is not None and len(numbered) != expected_pads:
        return {"ok": False,
                "error": f"pad-count mismatch: footprint has {len(numbered)} "
                         f"numbered pads, expected {expected_pads} -- wrong "
                         "file or wrong variant; NOT installed"}
    # Rename the footprint's internal name to match the requested name.
    mod_content = re.sub(r'^(\s*\((?:footprint|module)\s+)("(?:[^"\\]|\\.)*"|\S+)',
                         lambda m: m.group(1) + f'"{name}"', mod_content, count=1)
    out = _write(lib, name, mod_content)
    return {"ok": True, "installed": str(out), "lib_id": f"{lib}:{name}",
            "pads": len(pads), "numbered_pads": len(numbered),
            "unique_numbers": len(set(numbered))}


def generate_footprint(name: str, pads: list, body: dict = None,
                        lib: str = DEFAULT_LIB, smd: bool = True) -> dict:
    """Draw a footprint from an explicit pad table:
      pads: [{"num": "1", "x": mm, "y": mm, "w": mm, "h": mm,
              "shape": "rect"|"roundrect"|"circle"|"oval" (default rect),
              "type": "smd"|"thru_hole" (default smd), "drill": mm}, ...]
      body: {"w": mm, "h": mm} -- silkscreen/courtyard outline (optional;
             derived from pad extents + margin when omitted).
    Every number comes from the CALLER (datasheet land pattern) -- this
    function draws, it never guesses."""
    if not pads:
        return {"ok": False, "error": "pads table is required"}
    lines = [f'(footprint "{name}"', '\t(version 20240108)',
             '\t(generator "skidl_board")', '\t(layer "F.Cu")',
             f'\t(attr {"smd" if smd else "through_hole"})']

    xs = [float(p["x"]) for p in pads]
    ys = [float(p["y"]) for p in pads]
    ws = [float(p.get("w", 1)) for p in pads]
    hs = [float(p.get("h", 1)) for p in pads]
    if body and body.get("w") and body.get("h"):
        bw, bh = float(body["w"]) / 2, float(body["h"]) / 2
    else:
        bw = (max(xs) - min(xs)) / 2 + max(ws) / 2 + 0.5
        bh = (max(ys) - min(ys)) / 2 + max(hs) / 2 + 0.5

    # Reference/Value with sane sizes (the giant-text lesson, applied).
    lines += [
        f'\t(property "Reference" "REF**" (at 0 {-bh - 1:.2f} 0) '
        '(layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
        f'\t(property "Value" "{name}" (at 0 {bh + 1:.2f} 0) '
        '(layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))',
    ]
    # Courtyard + fab body + pin-1 silk dot.
    for layer, margin, width in (("F.CrtYd", 0.25, 0.05), ("F.Fab", 0.0, 0.1)):
        x, y = bw + margin, bh + margin
        lines.append(
            f'\t(fp_rect (start {-x:.3f} {-y:.3f}) (end {x:.3f} {y:.3f}) '
            f'(stroke (width {width}) (type solid)) (fill no) (layer "{layer}"))')
    p1 = min(pads, key=lambda p: (float(p["y"]), float(p["x"])))
    lines.append(
        f'\t(fp_circle (center {float(p1["x"]):.3f} {float(p1["y"]) - float(p1.get("h",1))/2 - 0.4:.3f}) '
        f'(end {float(p1["x"]) + 0.1:.3f} {float(p1["y"]) - float(p1.get("h",1))/2 - 0.4:.3f}) '
        '(stroke (width 0.2) (type solid)) (fill yes) (layer "F.SilkS"))')

    for p in pads:
        num = str(p.get("num", "")).strip()
        ptype = p.get("type", "smd" if smd else "thru_hole")
        shape = p.get("shape", "rect")
        w, h = float(p.get("w", 1)), float(p.get("h", 1))
        drill = f' (drill {float(p["drill"]):.3f})' if p.get("drill") else ""
        layers = '"*.Cu" "*.Mask"' if ptype == "thru_hole" else '"F.Cu" "F.Paste" "F.Mask"'
        rr = ' (roundrect_rratio 0.25)' if shape == "roundrect" else ""
        lines.append(
            f'\t(pad "{num}" {ptype} {shape} (at {float(p["x"]):.3f} '
            f'{float(p["y"]):.3f}) (size {w:.3f} {h:.3f}){drill} '
            f'(layers {layers}){rr})')
    lines.append(")")
    content = "\n".join(lines) + "\n"

    # Self-check through the same parser the pipeline uses.
    try:
        tree = sexp.read(content)
        n = len([p for p in tree[0] if isinstance(p, list) and p and p[0] == "pad"])
    except Exception as exc:
        return {"ok": False, "error": f"generated footprint failed self-parse: {exc!r}"}
    out = _write(lib, name, content)
    return {"ok": True, "installed": str(out), "lib_id": f"{lib}:{name}",
            "pads": n, "body_mm": [round(bw * 2, 2), round(bh * 2, 2)]}


def grid_pads(rows: int, cols: int, pitch: float, pad_w: float, pad_h: float,
               start_num: int = 1, shape: str = "roundrect") -> list:
    """Helper: a full LGA/BGA-style grid pad table, numbered row-major.
    Centered on origin. Callers can concatenate several of these (e.g.
    EC200U = edge LCC ring + inner LGA grid) and renumber per the
    datasheet's pin map."""
    out = []
    n = start_num
    x0 = -(cols - 1) * pitch / 2
    y0 = -(rows - 1) * pitch / 2
    for r in range(rows):
        for c in range(cols):
            out.append({"num": str(n), "x": round(x0 + c * pitch, 3),
                        "y": round(y0 + r * pitch, 3),
                        "w": pad_w, "h": pad_h, "shape": shape})
            n += 1
    return out


def _write(lib: str, name: str, content: str) -> Path:
    # Save with the Anvil-native ".anvil_mod" extension so a generated
    # custom footprint matches the rest of the store (the app reads both
    # .anvil_mod and .kicad_mod, but the store convention is .anvil_mod).
    pretty = _footprint_root() / f"{lib}.pretty"
    pretty.mkdir(parents=True, exist_ok=True)
    out = pretty / f"{name}.anvil_mod"
    out.write_text(content, encoding="utf-8")
    return out
