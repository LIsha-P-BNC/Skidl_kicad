"""
src/skidl/board/place/mech.py

Mechanical-first placement support: the professional flow fixes the
board outline, mounting holes, connector locations and keepouts BEFORE
any component is placed, and the placer works INSIDE them. This module
turns the sidecar's `mechanical` section into a MechPlan, pre-places and
locks the fixed items, and gives the placer its obstacle list.

The sidecar schema (validated at init by project_init._validate_mechanical):
  "mechanical": {
    "board": {"width": 80.0, "height": 50.0, "grow": false},
    "mounting_holes": {"size": "M3", "positions": [[4,4],[76,4],...]},
    "connectors": [{"ref": "J1", "edge": "left", "offset": 25.0,
                    "rotation": 0}],
    "keepouts": [{"name": "antenna", "rect": [60,0,80,15],
                  "layers": ["*.Cu"], "no_tracks": true, "no_vias": true,
                  "no_pour": true, "no_footprints": true,
                  "reason": "chip antenna clearance"}]
  }
Board space: (0,0) is the outline's top-left (KiCad Y-down), so
edge "top" means y=0 and "bottom" means y=height.
"""

from __future__ import annotations
from dataclasses import dataclass, field

from skidl.board.model import BoardModel, Footprint, KeepoutArea

EDGE_FLUSH = 1.0    # courtyard-to-board-edge gap for fixed connectors, mm


class MechanicalFitError(RuntimeError):
    """Parts cannot legally fit inside the fixed mechanical outline.
    Raised INSTEAD of silently growing the board -- the enclosure is the
    authority, and the honest answer is the area numbers."""

    def __init__(self, msg, needed_mm2=None, board_mm2=None, worst_refs=None):
        super().__init__(msg)
        self.needed_mm2 = needed_mm2
        self.board_mm2 = board_mm2
        self.worst_refs = worst_refs or []


@dataclass
class MechPlan:
    width: float = None
    height: float = None
    grow: bool = False
    hole_size: str = ""
    hole_positions: list = field(default_factory=list)   # [(x, y)]
    connectors: dict = field(default_factory=dict)       # ref -> spec dict
    keepouts: list = field(default_factory=list)         # list[KeepoutArea]
    fixed_refs: set = field(default_factory=set)         # refs apply_fixed locked

    @property
    def obstacle_boxes(self):
        """World bboxes the placer must keep parts out of: keepouts that
        forbid footprints. (Fixed footprints are real footprints and
        participate in normal courtyard checks.)"""
        return [k.bbox() for k in self.keepouts if k.no_footprints]


def build_mech_plan(sidecar: dict, board: BoardModel):
    """MechPlan from the sidecar's `mechanical` section; None when the
    project has no mechanical spec (fully auto layout, as before)."""
    mech = (sidecar or {}).get("mechanical")
    if not mech:
        return None
    b = mech.get("board") or {}
    holes = mech.get("mounting_holes") or {}
    plan = MechPlan(
        width=b.get("width"), height=b.get("height"),
        grow=bool(b.get("grow", False)),
        hole_size=str(holes.get("size") or "M3"),
        hole_positions=[tuple(p) for p in (holes.get("positions") or [])],
        connectors={c["ref"]: c for c in (mech.get("connectors") or [])
                    if c.get("ref")},
    )
    for k in (mech.get("keepouts") or []):
        rect = k.get("rect")
        outline = ([(rect[0], rect[1]), (rect[2], rect[1]),
                    (rect[2], rect[3]), (rect[0], rect[3])] if rect
                   else [tuple(p) for p in (k.get("polygon") or [])])
        if not outline:
            continue
        plan.keepouts.append(KeepoutArea(
            name=str(k.get("name") or f"keepout{len(plan.keepouts) + 1}"),
            outline=outline,
            layers=tuple(k.get("layers") or ("*.Cu",)),
            no_tracks=bool(k.get("no_tracks", True)),
            no_vias=bool(k.get("no_vias", True)),
            no_pour=bool(k.get("no_pour", True)),
            no_footprints=bool(k.get("no_footprints", False)),
            source="mechanical",
        ))
    return plan


def apply_fixed(board: BoardModel, plan: MechPlan) -> list:
    """Pre-place and LOCK the mechanically-fixed items: explicit mounting
    holes and edge connectors. Returns notes for the report. Keepouts
    are attached to the board so writer/DSN/refill all see them."""
    notes = []
    board.keepouts.extend(plan.keepouts)

    for i, (x, y) in enumerate(plan.hole_positions, 1):
        ref = f"MH{i}"
        fp = make_mounting_hole(plan.hole_size, x, y, ref)
        if fp is None:
            notes.append(f"mounting-hole footprint for {plan.hole_size} "
                         "not in libraries -- skipped")
            break
        board.footprints.append(fp)
        plan.fixed_refs.add(ref)

    W, H = plan.width, plan.height
    for ref, spec in plan.connectors.items():
        try:
            fp = board.footprint(ref)
        except KeyError:
            notes.append(f"connector {ref} not on board -- skipped")
            continue
        if spec.get("rotation") is not None:
            fp.rotation = float(spec["rotation"])
        cx1, cy1, cx2, cy2 = _courtyard_local(fp)
        edge = spec.get("edge")
        if edge is None and spec.get("rotation") is not None:
            # Rotation-only override: the part stays in the normal
            # placement flow, just turned -- used to steer the router
            # away from a bad local solution (measured: FreeRouting's
            # sliver artifact at the SIM socket is deterministic to the
            # pad-approach geometry).
            notes.append(f"{ref}: rotation set to {fp.rotation:g} "
                         "(not position-locked)")
            continue
        off = float(spec.get("offset") or 0.0)
        if edge == "left":
            fp.x = EDGE_FLUSH - cx1
            fp.y = off
        elif edge == "right" and W is not None:
            fp.x = W - EDGE_FLUSH - cx2
            fp.y = off
        elif edge == "top":
            fp.x = off
            fp.y = EDGE_FLUSH - cy1
        elif edge == "bottom" and H is not None:
            fp.x = off
            fp.y = H - EDGE_FLUSH - cy2
        else:
            notes.append(f"connector {ref}: edge {edge!r} needs a fixed "
                         "board dimension -- skipped")
            continue
        fp.locked = True
        plan.fixed_refs.add(ref)
    return notes


def make_mounting_hole(size: str, x: float, y: float, ref: str):
    """One locked NPTH mounting-hole footprint at (x, y), or None when
    the library lacks the footprint. Shared by the auto corner-slide
    path (pipeline._add_mounting_holes) and explicit mechanical
    positions."""
    from skidl.board.profiles import MOUNTING_HOLES
    from skidl.board.footprint_libs import (
        load_pads_and_courtyard, load_footprint_source, FootprintNotFoundError,
    )
    lib_id = MOUNTING_HOLES.get(size, MOUNTING_HOLES["M3"])
    try:
        pads, courtyard = load_pads_and_courtyard(lib_id)
        source = load_footprint_source(lib_id)
    except FootprintNotFoundError:
        return None
    return Footprint(
        ref=ref, lib_id=lib_id, value=size, x=x, y=y,
        pads=[type(p)(**{f: getattr(p, f) for f in (
            "number", "net", "x", "y", "size_x", "size_y", "shape",
            "pad_type", "layers", "drill", "drill_y", "rotation",
            "roundrect_rratio")}) for p in pads],
        courtyard=courtyard, locked=True, source_text=source,
    )


def _courtyard_local(fp: Footprint):
    """Local courtyard bbox, falling back to the pad extents."""
    if fp.courtyard:
        return fp.courtyard
    xs, ys = [], []
    for pad in fp.pads:
        xs += [pad.x - pad.size_x / 2, pad.x + pad.size_x / 2]
        ys += [pad.y - pad.size_y / 2, pad.y + pad.size_y / 2]
    if not xs:
        return (-2.0, -2.0, 2.0, 2.0)
    return (min(xs), min(ys), max(xs), max(ys))
