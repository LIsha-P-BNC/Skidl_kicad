"""
src/skidl/board/place/metrics.py

Placement-quality metrics, all REPORT-ONLY and honestly labeled:
weighted HPWL (the optimizer's objective), area utilization (the
professional 35-60% packing target), and a pin-density congestion
heuristic (an estimate, not a global router).
"""

from __future__ import annotations


def weighted_hpwl(board) -> float:
    """Sum over nets of half-perimeter wirelength (pad world bbox) times
    the net's placement weight -- the objective the global optimizer
    minimizes. mm."""
    from skidl.schematics.net_classify import placement_weight
    from skidl.board.place.simple import _build_views

    _parts, nets = _build_views(board)
    total = 0.0
    by_ref = {fp.ref: fp for fp in board.footprints}
    for net in nets.values():
        bnet = getattr(net, "_board_net", None)
        if bnet is None or len(bnet.pad_refs) < 2:
            continue
        w = placement_weight(net)
        if w <= 0:
            continue
        xs, ys = [], []
        for ref, pad_no in bnet.pad_refs:
            fp = by_ref.get(ref)
            if fp is None:
                continue
            for pad in fp.pads:
                if pad.number == pad_no:
                    wx, wy = fp.pad_world_xy(pad)
                    xs.append(wx)
                    ys.append(wy)
                    break
        if len(xs) >= 2:
            total += w * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return round(total, 1)


def utilization(board) -> float:
    """Sum of courtyard areas / board outline area, 0..1."""
    from skidl.board.place.simple import _world_bbox
    if not board.outline or not board.footprints:
        return 0.0
    xs = [p[0] for p in board.outline]
    ys = [p[1] for p in board.outline]
    area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if area <= 0:
        return 0.0
    parts = sum((b[2] - b[0]) * (b[3] - b[1])
                for b in map(_world_bbox, board.footprints))
    return round(parts / area, 3)


def congestion_map(board, cell=5.0) -> dict:
    """Pin-density routing-demand heuristic per cell: demand = pins in
    the cell + 0.5 per net whose bbox crosses it; capacity = routing
    tracks that fit through the cell across all layers. HONEST LABEL:
    this is an estimate, not a global router -- peak_ratio > 1 predicts
    router strain, it does not prove failure."""
    if not board.outline:
        return {"estimate": "no outline"}
    default = board.net_classes.get("Default", {})
    pitch = (default.get("width", 0.25) or 0.25) + \
            (default.get("clearance", 0.15) or 0.15)
    cap = max(1.0, cell / pitch) * max(1, board.layer_count)

    demand = {}

    def cell_of(x, y):
        return (int(x // cell), int(y // cell))

    for fp in board.footprints:
        for pad in fp.pads:
            wx, wy = fp.pad_world_xy(pad)
            demand[cell_of(wx, wy)] = demand.get(cell_of(wx, wy), 0.0) + 1.0

    for net in board.nets.values():
        if len(net.pad_refs) < 2:
            continue
        xs, ys = [], []
        by_ref = {fp.ref: fp for fp in board.footprints}
        for ref, pad_no in net.pad_refs:
            fp = by_ref.get(ref)
            if fp is None:
                continue
            for pad in fp.pads:
                if pad.number == pad_no:
                    wx, wy = fp.pad_world_xy(pad)
                    xs.append(wx)
                    ys.append(wy)
                    break
        if len(xs) < 2:
            continue
        for cx in range(int(min(xs) // cell), int(max(xs) // cell) + 1):
            for cy in range(int(min(ys) // cell), int(max(ys) // cell) + 1):
                demand[(cx, cy)] = demand.get((cx, cy), 0.0) + 0.5

    if not demand:
        return {"estimate": "no demand"}
    ratios = {k: v / cap for k, v in demand.items()}
    peak_cell = max(ratios, key=lambda k: ratios[k])
    hot = sorted(((k, round(r, 2)) for k, r in ratios.items() if r > 0.8),
                 key=lambda kv: -kv[1])[:5]
    return {
        "estimate": "pin-density heuristic, not a global router",
        "cell_mm": cell,
        "peak_ratio": round(ratios[peak_cell], 2),
        "peak_cell": [peak_cell[0] * cell, peak_cell[1] * cell],
        "hot_cells": [[k[0] * cell, k[1] * cell, r] for k, r in hot],
    }
