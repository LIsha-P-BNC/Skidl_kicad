# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Objective readability metrics for generated schematics.

These are diagnostic/regression-tracking functions, not rendering features:
they measure wire length, label density, part density, whitespace, and
(once available) wire crossings so placement/routing quality changes can be
validated against a fixed test corpus instead of relying on eyeballing
screenshots. Each function takes plain data (segments, bboxes, nets) rather
than a live SchNode so it can be unit-tested with small hand-built fixtures.
"""

from skidl.utilities import export_to_all


# ---------------------------------------------------------------------------
# TI-readability sub-metrics (WI-1)
#
# Each is a PURE function over plain geometry -- segments as ((x1,y1),(x2,y2))
# tuples and boxes as (x1,y1,x2,y2) tuples -- so it unit-tests with small
# hand-built fixtures (see tests/unit_tests/test_schematic_metrics.py) and is
# reused by the regression harness (test_circuits/regr_schematic/check_all.py),
# which reads the same geometry back out of a saved .anvil_sch.
#
# They are reported SEPARATELY (never conflated into one TI-match scalar): a
# single wire-crossing count does not prove TI quality -- pin-to-pin crossings,
# label overlap, text/symbol collision and block-spacing consistency are
# distinct defects and are measured as distinct numbers.
# ---------------------------------------------------------------------------

_EPS = 0.01


def _as_xy(p):
    """Accept a plain (x, y) tuple OR an object with .x/.y (e.g. Segment.p1)."""
    if hasattr(p, "x") and hasattr(p, "y"):
        return float(p.x), float(p.y)
    return float(p[0]), float(p[1])


def _seg_points(seg):
    """Normalize a segment to ((x1,y1),(x2,y2)); accepts a tuple pair or a
    Segment object (with .p1/.p2)."""
    if hasattr(seg, "p1") and hasattr(seg, "p2"):
        return _as_xy(seg.p1), _as_xy(seg.p2)
    return _as_xy(seg[0]), _as_xy(seg[1])


def _orient(ax, ay, bx, by, cx, cy):
    return (by - ay) * (cx - bx) - (bx - ax) * (cy - by)


def _on_seg(ax, ay, bx, by, px, py):
    return (min(ax, bx) - _EPS <= px <= max(ax, bx) + _EPS
            and min(ay, by) - _EPS <= py <= max(ay, by) + _EPS)


def _shares_endpoint(a, b):
    (a1, a2), (b1, b2) = a, b
    for pa in (a1, a2):
        for pb in (b1, b2):
            if abs(pa[0] - pb[0]) < 0.5 and abs(pa[1] - pb[1]) < 0.5:
                return True
    return False


def _proper_cross(a, b):
    """True iff segments a and b intersect at a point interior to BOTH --
    i.e. a genuine wire-over-wire crossing, NOT a shared endpoint (that is a
    junction / connection and is explicitly excluded)."""
    if _shares_endpoint(a, b):
        return False
    (a1, a2), (b1, b2) = a, b
    ax, ay = a1; bx, by = a2
    cx, cy = b1; dx, dy = b2
    d1 = _orient(cx, cy, dx, dy, ax, ay)
    d2 = _orient(cx, cy, dx, dy, bx, by)
    d3 = _orient(ax, ay, bx, by, cx, cy)
    d4 = _orient(ax, ay, bx, by, dx, dy)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        # Strict straddle on both sides -> interior crossing.
        if abs(d1) > _EPS and abs(d2) > _EPS and abs(d3) > _EPS and abs(d4) > _EPS:
            return True
    return False


def _boxes_overlap(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    return (min(ax2, bx2) - max(ax1, bx1) > _EPS
            and min(ay2, by2) - max(ay1, by1) > _EPS)


@export_to_all
def count_wire_crossings(segments):
    """Count pin-to-pin wire crossings: pairs of wire segments that cross at
    an interior point (a shared endpoint / junction does NOT count).

    Args:
        segments (iterable): wire segments, each ((x1,y1),(x2,y2)) or a
            Segment object with .p1/.p2.

    Returns:
        int: number of crossing pairs.
    """
    segs = [_seg_points(s) for s in segments]
    n = len(segs)
    crossings = 0
    for i in range(n):
        for j in range(i + 1, n):
            if _proper_cross(segs[i], segs[j]):
                crossings += 1
    return crossings


@export_to_all
def label_overlap_count(label_boxes):
    """Count pairs of net-label bounding boxes that overlap (unreadable
    stacked text).

    Args:
        label_boxes (iterable): boxes as (x1,y1,x2,y2).

    Returns:
        int: number of overlapping label pairs.
    """
    boxes = [tuple(map(float, b)) for b in label_boxes]
    n = len(boxes)
    overlaps = 0
    for i in range(n):
        for j in range(i + 1, n):
            if _boxes_overlap(boxes[i], boxes[j]):
                overlaps += 1
    return overlaps


@export_to_all
def text_symbol_collisions(text_boxes, symbol_boxes):
    """Count text boxes (RefDes / Value / net label) that collide with any
    symbol body box -- IMAGE 4 rule 5, "never place text inside the body".

    Args:
        text_boxes (iterable): boxes as (x1,y1,x2,y2).
        symbol_boxes (iterable): body boxes as (x1,y1,x2,y2).

    Returns:
        int: number of text boxes overlapping at least one symbol body.
    """
    sboxes = [tuple(map(float, b)) for b in symbol_boxes]
    hits = 0
    for t in text_boxes:
        tb = tuple(map(float, t))
        if any(_boxes_overlap(tb, s) for s in sboxes):
            hits += 1
    return hits


@export_to_all
def block_spacing_stats(block_boxes):
    """Nearest-neighbour gap statistics between functional-block boxes --
    TI layouts use CONSISTENT spacing, so a low stdev (and a non-negative
    min) matters more than the absolute gap.

    Args:
        block_boxes (iterable): block bounding boxes as (x1,y1,x2,y2).

    Returns:
        dict: {"min_gap", "mean_gap", "stdev_gap", "overlaps"} in input
        units; empty-safe (all 0.0 for < 2 blocks).
    """
    boxes = [tuple(map(float, b)) for b in block_boxes]
    if len(boxes) < 2:
        return {"min_gap": 0.0, "mean_gap": 0.0, "stdev_gap": 0.0, "overlaps": 0}

    def gap(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        dx = max(bx1 - ax2, ax1 - bx2, 0.0)
        dy = max(by1 - ay2, ay1 - by2, 0.0)
        if dx == 0.0 and dy == 0.0:
            return -1.0           # overlapping blocks
        return (dx * dx + dy * dy) ** 0.5

    nearest = []
    overlaps = 0
    for i, a in enumerate(boxes):
        gaps = [gap(a, b) for j, b in enumerate(boxes) if j != i]
        if any(g < 0 for g in gaps):
            overlaps += 1
        pos = [g for g in gaps if g >= 0]
        nearest.append(min(pos) if pos else 0.0)

    mean = sum(nearest) / len(nearest)
    var = sum((g - mean) ** 2 for g in nearest) / len(nearest)
    return {"min_gap": min(nearest), "mean_gap": mean,
            "stdev_gap": var ** 0.5, "overlaps": overlaps // 2}


@export_to_all
def sheet_extent(points):
    """Bounding-box compactness of a sheet, from any point cloud (symbol
    positions + wire endpoints). The 2D block-grid's claimed advantage is
    COMPOSITION -- near-square, compact -- which crossing counts cannot see;
    this measures it directly.

    Args:
        points (iterable): (x, y) tuples.

    Returns:
        dict: {"w", "h", "area", "aspect_dev"} where aspect_dev =
        |log10(w/h)| (0.0 = perfectly square; ~0.48 = 3:1). All 0.0 for
        fewer than 2 points or a degenerate (zero-size) extent.
    """
    import math as _math

    pts = [(float(x), float(y)) for x, y in points]
    if len(pts) < 2:
        return {"w": 0.0, "h": 0.0, "area": 0.0, "aspect_dev": 0.0}
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    if w <= 0 or h <= 0:
        return {"w": w, "h": h, "area": 0.0, "aspect_dev": 0.0}
    return {"w": w, "h": h, "area": w * h,
            "aspect_dev": abs(_math.log10(w / h))}


@export_to_all
def left_to_right_flow(ordered_xs):
    """Fraction of adjacent stages whose x advances left->right, for parts
    listed in signal order. 1.0 = perfect left-to-right flow.

    Args:
        ordered_xs (list): x-centre of each part, in signal (net) order.

    Returns:
        float: monotonic-forward fraction in [0,1] (1.0 for < 2 parts).
    """
    xs = [float(x) for x in ordered_xs]
    if len(xs) < 2:
        return 1.0
    forward = sum(1 for a, b in zip(xs, xs[1:]) if b >= a - _EPS)
    return forward / (len(xs) - 1)


@export_to_all
def total_wire_length(wires):
    """
    Sum the Manhattan length of every routed wire segment.

    Args:
        wires (dict): Mapping of net -> list of Segment, e.g. SchNode.wires.

    Returns:
        float: Total length of all wire segments.
    """
    length = 0.0
    for segments in wires.values():
        for seg in segments:
            length += abs(seg.p2.x - seg.p1.x) + abs(seg.p2.y - seg.p1.y)
    return length


@export_to_all
def label_count(nets):
    """
    Count the number of net-label glyphs a set of nets will render as.

    A stubbed net emits one label per connected pin on it (mirroring how
    net_label_to_sexp() in sexp_schematic.py is called once per stubbed,
    connected pin), so this is a glyph count, not a net count.

    Args:
        nets (iterable): Net objects to inspect.

    Returns:
        int: Total label instance count.
    """
    count = 0
    for net in nets:
        if getattr(net, "stub", False):
            count += sum(1 for pin in net.pins if pin.is_connected())
    return count


@export_to_all
def component_density(part_bboxes, node_bbox):
    """
    Return parts per unit area within a node's bounding box.

    Args:
        part_bboxes (list): BBox objects for each placed part.
        node_bbox (BBox): The overall node bounding box.

    Returns:
        float: Number of parts per unit area (0 if node_bbox has no area).
    """
    area = node_bbox.area
    if area <= 0:
        return 0.0
    return len(part_bboxes) / area


@export_to_all
def whitespace_ratio(part_bboxes, node_bbox):
    """
    Return the fraction of a node's bounding box not covered by any part.

    Args:
        part_bboxes (list): BBox objects for each placed part.
        node_bbox (BBox): The overall node bounding box.

    Returns:
        float: 1.0 = fully empty, 0.0 = fully covered (clamped to [0,1]).
    """
    total_area = node_bbox.area
    if total_area <= 0:
        return 0.0
    covered = sum(bbox.area for bbox in part_bboxes)
    return max(0.0, min(1.0, 1.0 - (covered / total_area)))


@export_to_all
def readability_score(
    wires,
    nets,
    part_bboxes,
    node_bbox,
    crossings=None,
    weights=None,
):
    """
    Combine the individual metrics into a single readability score.

    Lower is better. This is a regression-tracking aid (compare a score
    before/after a placement or routing change on the same circuit), not
    an absolute quality target -- the weights are a starting point and are
    expected to be tuned against visual QA over time.

    Args:
        wires (dict): Mapping of net -> list of Segment, e.g. SchNode.wires.
        nets (iterable): Net objects to inspect for label density.
        part_bboxes (list): BBox objects for each placed part.
        node_bbox (BBox): The overall node bounding box.
        crossings (int, optional): Wire-crossing count from
            route.count_wire_crossings(). Omitted (None) until that
            function is available; contributes 0 to the score if omitted.
        weights (dict, optional): Override weights for any of
            "wire_length", "labels", "density", "whitespace", "crossings".

    Returns:
        dict: The individual metric values plus the combined "score".
    """
    default_weights = {
        "wire_length": 1.0,
        "labels": 50.0,
        "density": 1.0,
        "whitespace": 100.0,
        "crossings": 200.0,
    }
    if weights:
        default_weights.update(weights)

    metrics = {
        "wire_length": total_wire_length(wires),
        "labels": label_count(nets),
        "density": component_density(part_bboxes, node_bbox),
        "whitespace": whitespace_ratio(part_bboxes, node_bbox),
        "crossings": crossings if crossings is not None else 0,
    }

    score = sum(default_weights[key] * value for key, value in metrics.items())
    metrics["score"] = score
    return metrics
