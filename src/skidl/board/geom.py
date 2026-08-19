"""
src/skidl/board/geom.py

Small pure-geometry helpers on plain (x, y) tuples / [(x,y), ...] polygons,
shared by the pour planner (stitching-via legality, zone extents) and any
later DRC-style checks. No KiCad types; mm throughout.
"""

from __future__ import annotations

import math


def point_segment_distance(px, py, x1, y1, x2, y2) -> float:
    """Shortest distance from point (px,py) to the segment (x1,y1)-(x2,y2).

    Used to keep a stitching via clear of a routed track: the via's centre
    must stay at least (via_radius + track_half_width + clearance) from every
    other-net track segment, or KiCad reports a copper short / clearance error.
    """
    dx, dy = x2 - x1, y2 - y1
    seg2 = dx * dx + dy * dy
    if seg2 == 0.0:                      # degenerate segment -> point
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / seg2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    cx, cy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - cx, py - cy)


def point_in_polygon(pt, polygon) -> bool:
    """True if `pt` is inside `polygon` ([(x,y), ...], implicitly closed).

    Winding-number test, so it is correct for concave AND self-intersecting
    outlines (KiCad permits both). Points exactly on an edge count as inside
    (conservative for keep-in checks). O(n) in the vertex count.
    """
    if not polygon or len(polygon) < 3:
        return False
    x, y = pt
    wn = 0
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        # On-edge (collinear + within the segment bbox) -> inside.
        if _on_segment(x, y, x1, y1, x2, y2):
            return True
        if y1 <= y:
            if y2 > y and _is_left(x1, y1, x2, y2, x, y) > 0:
                wn += 1
        else:
            if y2 <= y and _is_left(x1, y1, x2, y2, x, y) < 0:
                wn -= 1
    return wn != 0


def bbox_of(polygon):
    """(minx, miny, maxx, maxy) of a polygon / point list, or None if empty."""
    if not polygon:
        return None
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def bbox_contains(bbox, pt, margin: float = 0.0) -> bool:
    """True if `pt` is within `bbox` (minx,miny,maxx,maxy), grown by margin."""
    if bbox is None:
        return False
    minx, miny, maxx, maxy = bbox
    x, y = pt
    return (minx - margin) <= x <= (maxx + margin) and \
           (miny - margin) <= y <= (maxy + margin)


def bboxes_overlap(a, b, gap: float = 0.0) -> bool:
    """True if two (minx,miny,maxx,maxy) boxes overlap when each is grown by
    gap/2 (i.e. their centers are closer than gap apart on both axes)."""
    if a is None or b is None:
        return False
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 + gap <= bx0 or bx1 + gap <= ax0 or
                ay1 + gap <= by0 or by1 + gap <= ay0)


def _is_left(x1, y1, x2, y2, px, py) -> float:
    """>0 if (px,py) is left of the directed edge (x1,y1)->(x2,y2)."""
    return (x2 - x1) * (py - y1) - (px - x1) * (y2 - y1)


def _on_segment(px, py, x1, y1, x2, y2, eps: float = 1e-9) -> bool:
    if abs(_is_left(x1, y1, x2, y2, px, py)) > eps:
        return False
    return (min(x1, x2) - eps <= px <= max(x1, x2) + eps and
            min(y1, y2) - eps <= py <= max(y1, y2) + eps)
