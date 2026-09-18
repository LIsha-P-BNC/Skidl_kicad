"""
tests/unit_tests/test_block_grid.py

Gap H1 (2D block-grid placement) -- pure-geometry guarantees of the new
place_block_grid helpers in anchor_place.py: the grid is compact, the anchor
owns the centre cell, columns/rows align, and NO two block cells ever overlap
(the invariant a scatter/over-wide layout violates). The part-level placement is
opt-in (SKIDL_BLOCK_GRID=1) and verified end-to-end by the build connectivity
gate; here we lock in the coordinate math that makes it safe.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.schematics.anchor_place import _grid_assign, _grid_coords  # noqa: E402


def _rects(coords, dims):
    """(x_left, x_right, y_bottom, y_top) for each block; Ytop is the top edge,
    the block extends downward by its height h."""
    out = []
    for (x, ytop), (w, h) in zip(coords, dims):
        out.append((x, x + w, ytop - h, ytop))
    return out


def _overlap(a, b, eps=1e-6):
    ax0, ax1, ay0, ay1 = a
    bx0, bx1, by0, by1 = b
    return (min(ax1, bx1) - max(ax0, bx0) > eps
            and min(ay1, by1) - max(ay0, by0) > eps)


def test_anchor_owns_centre_cell():
    for n in range(2, 13):
        rows, cols, slots = _grid_assign(n)
        assert slots[0] == (rows // 2, cols // 2), f"n={n}: anchor not centred"


def test_slots_unique_and_enough():
    for n in range(2, 13):
        rows, cols, slots = _grid_assign(n)
        assert len(slots) >= n
        assert len(set(slots[:n])) == n, f"n={n}: duplicate cell assigned"


def test_no_block_cells_overlap():
    # Varied block sizes (incl. a big central IC) across several counts.
    size_cycle = [(80, 60), (40, 30), (120, 90), (30, 50), (60, 60), (25, 25)]
    for n in range(2, 13):
        dims = [size_cycle[i % len(size_cycle)] for i in range(n)]
        rows, cols, slots = _grid_assign(n)
        coords, colW, rowH = _grid_coords(rows, cols, slots[:n], dims,
                                          gap=1000.0, align=50.0)
        rects = _rects(coords, dims)
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                assert not _overlap(rects[i], rects[j]), (
                    f"n={n}: blocks {i},{j} overlap ({rects[i]} vs {rects[j]})")


def test_coords_snapped_to_align_grid():
    n = 6
    dims = [(83, 61), (37, 29), (121, 91), (33, 51), (59, 63), (27, 24)]
    rows, cols, slots = _grid_assign(n)
    coords, _cw, _rh = _grid_coords(rows, cols, slots[:n], dims, gap=1000.0, align=50.0)
    for x, y in coords:
        assert abs(x / 50.0 - round(x / 50.0)) < 1e-9, f"x={x} not on 50-grid"
        assert abs(y / 50.0 - round(y / 50.0)) < 1e-9, f"y={y} not on 50-grid"


def test_columns_and_rows_align():
    # Blocks sharing a column share x_left; blocks sharing a row share y_top.
    n = 9  # 3x3
    dims = [(80, 60)] * n
    rows, cols, slots = _grid_assign(n)
    coords, _cw, _rh = _grid_coords(rows, cols, slots[:n], dims, gap=1000.0, align=50.0)
    by_col, by_row = {}, {}
    for (r, c), (x, ytop) in zip(slots[:n], coords):
        by_col.setdefault(c, set()).add(round(x, 3))
        by_row.setdefault(r, set()).add(round(ytop, 3))
    for c, xs in by_col.items():
        assert len(xs) == 1, f"column {c} lefts not aligned: {xs}"
    for r, ys in by_row.items():
        assert len(ys) == 1, f"row {r} tops not aligned: {ys}"
