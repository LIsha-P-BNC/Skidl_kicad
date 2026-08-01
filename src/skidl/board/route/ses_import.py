"""
src/skidl/board/route/ses_import.py

[C] last leg: read a FreeRouting .ses session file and fold its
(wire (path ...)) and (via ...) records back into BoardModel.tracks /
BoardModel.vias, ready for pcb_writer.write_pcb() to re-emit the board
with real copper.

Same coordinate conventions as dsn_export.py in reverse: the session's
(resolution <unit> <value>) is honored, and Specctra's Y-up flips back
to KiCad's Y-down.
"""

from __future__ import annotations
from pathlib import Path

from skidl.board import sexp
from skidl.board.model import BoardModel, Track, Via


def import_ses(board: BoardModel, ses_path: Path) -> dict:
    """Parse ses_path into board.tracks / board.vias (replacing any prior
    routing). Returns {"tracks": n, "vias": n, "nets_routed": n}."""
    ses_path = Path(ses_path)
    tree = sexp.read_file(ses_path)
    session = tree[0]

    routes = _find_deep(session, "routes")
    if routes is None:
        raise ValueError(f"{ses_path.name}: no (routes ...) section")

    # Coordinate scale: FreeRouting's (resolution ...) statement is NOT
    # trustworthy (2.x declares "um 10" while writing 100-units-per-um
    # coordinates). But the session's (placement ...) section echoes the
    # component positions we ourselves set, so calibrate units-per-mm
    # against the board's own footprints and fall back to the declared
    # resolution only if no placement echo matches.
    scale = _calibrate_scale(session, board)
    if scale is None:
        res = sexp.find_one(routes, "resolution") or ["resolution", "um", "10"]
        unit, per = res[1], float(res[2])
        per_mm = {"um": 1e3, "mil": 1 / 0.0254, "cm": 0.1, "mm": 1.0, "inch": 1 / 25.4}[unit] * per
        scale = per_mm
    to_mm = 1.0 / scale

    default_rules = board.net_classes.get("Default", {})
    via_size_dflt = default_rules.get("via_size", 0.8)
    via_drill_dflt = default_rules.get("via_drill", 0.4)

    board.tracks = []
    board.vias = []
    nets_routed = set()

    network_out = sexp.find_one(routes, "network_out")
    for net_expr in (sexp.find_all(network_out, "net") if network_out else []):
        net_name = net_expr[1] if len(net_expr) > 1 and isinstance(net_expr[1], str) else ""
        for wire_expr in sexp.find_all(net_expr, "wire"):
            path = sexp.find_one(wire_expr, "path")
            if not path or len(path) < 4:
                continue
            layer = path[1]
            width = float(path[2]) * to_mm
            coords = [float(v) * to_mm for v in path[3:]]
            pts = [(coords[i], -coords[i + 1]) for i in range(0, len(coords) - 1, 2)]
            for a, b in zip(pts, pts[1:]):
                if a == b:
                    continue
                board.tracks.append(Track(net=net_name, layer=layer,
                                          start=a, end=b, width=width))
                nets_routed.add(net_name)
        for via_expr in sexp.find_all(net_expr, "via"):
            # (via "Via[0-1]_800:400_um" x y ...)
            if len(via_expr) < 4:
                continue
            ps_name = via_expr[1]
            x = float(via_expr[2]) * to_mm
            y = -float(via_expr[3]) * to_mm
            size, drill = _via_dims_from_name(ps_name, via_size_dflt, via_drill_dflt)
            board.vias.append(Via(net=net_name, at=(x, y), size=size, drill=drill))
            nets_routed.add(net_name)

    return {"tracks": len(board.tracks), "vias": len(board.vias),
            "nets_routed": len(nets_routed)}


def _calibrate_scale(session, board: BoardModel):
    """Units-per-mm from the session's placement echo: for every
    (place REF x y ...) whose REF we know, ratio ses-coord / known-mm.
    Median of all usable ratios; None if nothing matched."""
    placement = _find_deep(session, "placement")
    if placement is None:
        return None
    known = {fp.ref: fp for fp in board.footprints}
    ratios = []
    for comp in sexp.find_all(placement, "component"):
        for place in sexp.find_all(comp, "place"):
            if len(place) < 4:
                continue
            ref = place[1]
            fp = known.get(ref)
            if fp is None:
                continue
            try:
                sx, sy = float(place[2]), float(place[3])
            except ValueError:
                continue
            for ses_v, mm_v in ((sx, fp.x), (sy, -fp.y)):
                if abs(mm_v) > 1.0 and abs(ses_v) > 0:
                    ratios.append(ses_v / mm_v)
    if not ratios:
        return None
    ratios.sort()
    return ratios[len(ratios) // 2]


def _via_dims_from_name(ps_name: str, dflt_size: float, dflt_drill: float):
    """Recover (size, drill) mm from a "Via[0-1]_800:400_um" padstack name;
    fall back to the board's default via when the name doesn't parse."""
    try:
        dims = ps_name.split("_")[1]          # "800:400"
        size_um, drill_um = dims.split(":")
        return int(size_um) / 1000.0, int(drill_um) / 1000.0
    except Exception:
        return dflt_size, dflt_drill


def _find_deep(expr, head):
    """First (head ...) list anywhere below expr (depth-first)."""
    if not isinstance(expr, list):
        return None
    if expr and expr[0] == head:
        return expr
    for child in expr:
        hit = _find_deep(child, head)
        if hit is not None:
            return hit
    return None
