"""
src/skidl/board/place/simple.py

[B] in the M0/M1 pipeline: the M1 coarse-but-legal placer. Mutates
BoardModel footprint x/y/rotation in place:

  * connectors            -> left board edge, stacked top-to-bottom
  * biggest anchor (IC)   -> board center; further anchors fan out right
  * decoupling caps       -> snapped beside the IC they decouple
  * everything else       -> ring rows under/around the center
  * courtyard overlaps    -> iterative axis push-apart until legal
  * final                 -> snap to 0.5 mm grid, refit Edge.Cuts outline

Engineering classification is NOT reimplemented here -- it reuses the
schematic engine's pure classifiers (schematics/cluster.py and
schematics/net_classify.py: find_anchor_parts, is_decoupling_cap,
find_decap_affinities, is_connector_part), which are duck-typed
(getattr-based), fed through the lightweight _PartView/_NetView shims
below. The full rule engine (regions, L-R flow, crystal-near-XTAL-pins)
is M2 (place/rules.py).
"""

from __future__ import annotations
import math
import re

from skidl.board.model import BoardModel, Footprint

from skidl.schematics.cluster import (
    find_anchor_parts,
    find_decap_affinities,
    find_functional_cap_affinities,
    is_decoupling_cap,
)
from skidl.schematics.net_classify import is_connector_part

GRID = 0.5          # mm placement grid
GAP = 2.0           # mm minimum courtyard-to-courtyard gap -- generous on
                    # purpose: tight packing walls off pads and makes the
                    # autorouter fail (measured on the NE555 board)
EDGE_MARGIN = 4.0   # mm board-edge to courtyard margin (routing corridor)

_REF_PREFIX_RE = re.compile(r"^([A-Za-z_#]+)")


# ---------------------------------------------------------------------------
# Duck-typed views feeding the schematic classifiers from a BoardModel
# ---------------------------------------------------------------------------

class _PinView:
    __slots__ = ("part", "net", "number")

    def __init__(self, part, number):
        self.part = part
        self.number = number
        self.net = None


class _PartView:
    """Just enough Part surface for cluster.py / net_classify.py."""

    def __init__(self, fp: Footprint):
        self.fp = fp
        self.ref = fp.ref
        m = _REF_PREFIX_RE.match(fp.ref)
        self.ref_prefix = m.group(1) if m else fp.ref
        self.value = fp.value
        self.name = fp.value or fp.lib_id
        # lib_id doubles as searchable metadata: "Connector_PinHeader..."
        # trips is_connector_part()'s search_text check just like the
        # symbol metadata does in the schematic engine.
        self.search_text = fp.lib_id
        self.keywords = ""
        self.pins = [_PinView(self, p.number) for p in fp.pads]

    def __repr__(self):
        return f"_PartView({self.ref})"


class _NetView:
    __slots__ = ("name", "pins", "drive", "_board_net")

    def __init__(self, name):
        self.name = name
        self.pins = []
        self.drive = None
        self._board_net = None   # back-ref to the model.Net (pin_types etc.)


def _build_views(board: BoardModel):
    """Returns (part_views_by_ref, net_views_by_name) wired together."""
    parts = {fp.ref: _PartView(fp) for fp in board.footprints}
    nets = {}
    for net in board.nets.values():
        nv = _NetView(net.name)
        nv._board_net = net
        nets[net.name] = nv
        for ref, pin_num in net.pad_refs:
            pv = parts.get(ref)
            if pv is None:
                continue
            for pin in pv.pins:
                if pin.number == pin_num and pin.net is None:
                    pin.net = nv
                    nv.pins.append(pin)
                    break
    return parts, nets


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _size(fp: Footprint):
    """Courtyard (w, h) with the GAP margin baked in."""
    if fp.courtyard:
        minx, miny, maxx, maxy = fp.courtyard
        return (maxx - minx) + GAP, (maxy - miny) + GAP
    return 5.0 + GAP, 5.0 + GAP


def _world_bbox(fp: Footprint):
    bbox = fp.courtyard_world_bbox()
    if bbox is None:
        return (fp.x - 2.5, fp.y - 2.5, fp.x + 2.5, fp.y + 2.5)
    return bbox


def _overlap(a, b, margin=GAP):
    """Overlap (dx, dy) of two bboxes incl. `margin`; None if clear."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = min(ax2, bx2) - max(ax1, bx1) + margin
    dy = min(ay2, by2) - max(ay1, by1) + margin
    if dx <= 0 or dy <= 0:
        return None
    return dx, dy


def _snap(v, grid=GRID):
    return round(v / grid) * grid


# ---------------------------------------------------------------------------
# The placer
# ---------------------------------------------------------------------------

def place_board(board: BoardModel) -> dict:
    """Place board.footprints in-house-style regions (see module docstring)
    and refit board.outline. Returns a small report dict (roles, board
    size) for logs/UI. Deterministic: same board -> same placement."""
    if not board.footprints:
        return {"placed": 0}

    parts, _nets = _build_views(board)
    views = [parts[fp.ref] for fp in sorted(board.footprints, key=lambda f: f.ref)]

    connectors = [v for v in views if is_connector_part(v)]
    anchors = [v for v in find_anchor_parts(views) if v not in connectors]
    decap_by_part = {}   # decap _PartView -> ic _PartView
    for _net, (decap, ic) in find_decap_affinities(views).items():
        decap_by_part.setdefault(decap, ic)
    # Functional caps (555 CONT, crystal load, comp/bootstrap): tie to an IC
    # via a SIGNAL pin, so find_decap_affinities (VCC-only) misses them and
    # they'd scatter into 'others' -- too far to route. Pull them beside their
    # IC too. Power bypass caps already claimed above win.
    funccap_by_part = {}   # functional cap _PartView -> ic _PartView
    for _net, (cap, ic) in find_functional_cap_affinities(views).items():
        if cap not in decap_by_part:
            funccap_by_part.setdefault(cap, ic)
    placed_special = (set(connectors) | set(anchors)
                      | set(decap_by_part) | set(funccap_by_part))
    others = [v for v in views if v not in placed_special]

    # Board size estimate from total courtyard area at ~20% fill --
    # roomy, so the autorouter always has corridors (M2's rule placer
    # can tighten this once routing quality metrics exist).
    area = sum(w * h for w, h in (_size(v.fp) for v in views))
    side = max(20.0, math.sqrt(area / 0.20))
    cx = cy = side / 2.0

    # 1) Anchors: biggest at dead center, further anchors fan out rightward.
    x = cx
    for v in anchors:
        w, h = _size(v.fp)
        v.fp.x, v.fp.y = _snap(x), _snap(cy)
        x += w + GAP * 2

    # 2) Decaps: snapped in a tight column at the right edge of their IC.
    per_ic_count = {}
    for decap, ic in sorted(decap_by_part.items(), key=lambda kv: kv[0].ref):
        iw, ih = _size(ic.fp)
        dw, dh = _size(decap.fp)
        n = per_ic_count.get(ic, 0)
        per_ic_count[ic] = n + 1
        decap.fp.x = _snap(ic.fp.x + iw / 2 + dw / 2)
        decap.fp.y = _snap(ic.fp.y - ih / 2 + dh / 2 + n * (dh + GAP))

    # 2b) Functional caps: tight column at the LEFT edge of their IC (decaps
    # took the right edge) so the dedicated signal trace stays short.
    per_ic_fcount = {}
    for cap, ic in sorted(funccap_by_part.items(), key=lambda kv: kv[0].ref):
        iw, ih = _size(ic.fp)
        cw, ch = _size(cap.fp)
        n = per_ic_fcount.get(ic, 0)
        per_ic_fcount[ic] = n + 1
        cap.fp.x = _snap(ic.fp.x - iw / 2 - cw / 2)
        cap.fp.y = _snap(ic.fp.y - ih / 2 + ch / 2 + n * (ch + GAP))

    # 3) Connectors: left edge, stacked top-to-bottom.
    y = EDGE_MARGIN
    for v in connectors:
        w, h = _size(v.fp)
        v.fp.x, v.fp.y = _snap(EDGE_MARGIN + w / 2), _snap(y + h / 2)
        y += h + GAP

    # 4) Everything else: rows beneath the center block.
    if others:
        row_y = cy + max((_size(a.fp)[1] for a in anchors), default=10.0)
        x = y = 0.0
        row_h = 0.0
        row_w = max(side * 0.7, 20.0)
        for v in others:
            w, h = _size(v.fp)
            if x + w > row_w and x > 0:
                x, y, row_h = 0.0, y + row_h, 0.0
            v.fp.x = _snap(cx - row_w / 2 + x + w / 2)
            v.fp.y = _snap(row_y + y + h / 2)
            x += w
            row_h = max(row_h, h)

    # 5) Courtyard de-overlap: iterative pairwise push along the smaller
    # overlap axis. Anchors are heaviest (moved least), then connectors.
    weight = {}
    for v in views:
        if v in anchors:
            weight[v.fp.ref] = 4.0
        elif v in connectors:
            weight[v.fp.ref] = 3.0
        elif v in decap_by_part or v in funccap_by_part:
            weight[v.fp.ref] = 1.0
        else:
            weight[v.fp.ref] = 2.0

    fps = [v.fp for v in views]
    for _ in range(200):
        moved = False
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                a, b = fps[i], fps[j]
                ov = _overlap(_world_bbox(a), _world_bbox(b))
                if ov is None:
                    continue
                dx, dy = ov
                wa, wb = weight[a.ref], weight[b.ref]
                share_a = wb / (wa + wb)   # heavier part moves less
                share_b = wa / (wa + wb)
                if dx <= dy:
                    # Push apart along X, away from each other's center.
                    sign = 1.0 if a.x <= b.x else -1.0
                    total = dx + GRID
                    a.x = _snap(a.x - sign * total * share_a)
                    b.x = _snap(b.x + sign * total * share_b)
                else:
                    sign = 1.0 if a.y <= b.y else -1.0
                    total = dy + GRID
                    a.y = _snap(a.y - sign * total * share_a)
                    b.y = _snap(b.y + sign * total * share_b)
                moved = True
        if not moved:
            break

    # 6) Shift everything positive and refit the Edge.Cuts outline.
    boxes = [_world_bbox(fp) for fp in fps]
    minx = min(b[0] for b in boxes) - EDGE_MARGIN
    miny = min(b[1] for b in boxes) - EDGE_MARGIN
    shift_x, shift_y = -minx, -miny
    for fp in fps:
        fp.x = _snap(fp.x + shift_x)
        fp.y = _snap(fp.y + shift_y)
    boxes = [_world_bbox(fp) for fp in fps]
    maxx = max(b[2] for b in boxes) + EDGE_MARGIN
    maxy = max(b[3] for b in boxes) + EDGE_MARGIN
    board.outline = [(0.0, 0.0), (_snap(maxx), 0.0),
                     (_snap(maxx), _snap(maxy)), (0.0, _snap(maxy))]

    overlaps = count_courtyard_overlaps(board)
    return {
        "placed": len(fps),
        "anchors": [v.ref for v in anchors],
        "connectors": [v.ref for v in connectors],
        "decaps": {d.ref: ic.ref for d, ic in decap_by_part.items()},
        "functional_caps": {c.ref: ic.ref for c, ic in funccap_by_part.items()},
        "board_size_mm": (round(maxx, 1), round(maxy, 1)),
        "courtyard_overlaps": overlaps,
    }


def count_courtyard_overlaps(board: BoardModel, ignore=None) -> int:
    """Number of overlapping courtyard pairs -- the M1 acceptance metric
    ('0 courtyard overlaps'). `ignore`: refs excluded from the count
    (placer-internal phantom obstacles)."""
    fps = board.footprints
    if ignore:
        fps = [fp for fp in fps if fp.ref not in ignore]
    n = 0
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            a = _world_bbox(fps[i])
            b = _world_bbox(fps[j])
            ax1, ay1, ax2, ay2 = a
            bx1, by1, bx2, by2 = b
            if min(ax2, bx2) > max(ax1, bx1) and min(ay2, by2) > max(ay1, by1):
                n += 1
    return n
