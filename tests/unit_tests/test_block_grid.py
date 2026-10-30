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

from skidl.schematics.anchor_place import (  # noqa: E402
    _affinity_slot_map,
    _grid_assign,
    _grid_coords,
    _is_chain_graph,
)


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


# ---- WI-5: chain guard + affinity-aware slot assignment -------------------

def test_chain_graph_detected():
    # 4 blocks in a line (PSU chain): 0-1-2-3 -> the grid must decline.
    aff = {(0, 1): 5.0, (1, 2): 3.0, (2, 3): 4.0}
    assert _is_chain_graph(4, aff)


def test_empty_affinity_is_chain():
    # No inter-block nets: nothing for a grid to optimize -> decline.
    assert _is_chain_graph(3, {})


def test_hub_graph_is_not_chain():
    # MCU hub touching 3 blocks (degree 3) -> grid engages.
    aff = {(0, 1): 5.0, (0, 2): 3.0, (0, 3): 4.0}
    assert not _is_chain_graph(4, aff)


def test_cycle_is_not_chain():
    # A ring (cycle) has n edges: not a path -> grid may engage.
    aff = {(0, 1): 1.0, (1, 2): 1.0, (2, 3): 1.0, (0, 3): 1.0}
    assert not _is_chain_graph(4, aff)


def test_affinity_map_anchor_keeps_centre():
    rows, cols, slots = _grid_assign(5)
    aff = {(0, 1): 5.0, (0, 2): 3.0, (0, 3): 4.0, (0, 4): 1.0}
    m = _affinity_slot_map(5, slots, aff)
    assert m[0] == slots[0], "anchor must keep the centre cell"
    assert len(set(m)) == 5, "every block needs a distinct cell"


def test_affinity_map_strong_partner_adjacent():
    # Block 3 is the anchor's strongest partner -> it must sit in a cell
    # ADJACENT (Manhattan distance 1) to the anchor's centre cell.
    rows, cols, slots = _grid_assign(5)
    aff = {(0, 3): 10.0, (0, 1): 1.0, (0, 2): 1.0, (1, 2): 1.0, (0, 4): 1.0}
    m = _affinity_slot_map(5, slots, aff)
    d = abs(m[3][0] - m[0][0]) + abs(m[3][1] - m[0][1])
    assert d == 1, f"strongest partner at distance {d}, expected 1"


def test_affinity_map_empty_degrades_to_ring_order():
    # No affinity: the map must equal today's behaviour (block i -> slots[i]).
    rows, cols, slots = _grid_assign(6)
    m = _affinity_slot_map(6, slots, {})
    assert m == slots[:6]


def test_affinity_map_deterministic():
    rows, cols, slots = _grid_assign(7)
    aff = {(0, 1): 2.0, (0, 2): 2.0, (1, 3): 1.5, (2, 4): 1.5, (0, 5): 0.5}
    assert _affinity_slot_map(7, slots, aff) == _affinity_slot_map(7, slots, aff)


# ---- WI-5: skip_power affinity (rails must not hide the chain) ------------

class _MockPin:
    def __init__(self, part, net):
        self.part = part
        self.net = net
        net.pins.append(self)


class _MockNet:
    def __init__(self, name):
        self.name = name
        self.pins = []


class _MockPart:
    def __init__(self):
        self.pins = []


def _mock_blocks_chain_plus_rail():
    """3 blocks: signal chain 0-1-2 PLUS a +5V rail touching all three --
    the buck shape that defeated the chain guard."""
    from skidl.schematics.cluster import block_affinity_matrix

    parts = [_MockPart() for _ in range(3)]
    sig01 = _MockNet("REG_IN")
    sig12 = _MockNet("REG_OUT")
    rail = _MockNet("+5V")
    for net, members in ((sig01, (0, 1)), (sig12, (1, 2)), (rail, (0, 1, 2))):
        for i in members:
            parts[i].pins.append(_MockPin(parts[i], net))
    blocks = [{parts[0]}, {parts[1]}, {parts[2]}]
    return block_affinity_matrix, blocks


def test_rail_hides_chain_without_skip_power():
    bam, blocks = _mock_blocks_chain_plus_rail()
    aff = bam(blocks)                      # default: rail included
    assert (0, 2) in aff, "rail edge expected in default affinity"
    assert not _is_chain_graph(3, aff), "rail makes the graph non-chain"


def test_skip_power_restores_chain():
    bam, blocks = _mock_blocks_chain_plus_rail()
    aff = bam(blocks, skip_power=True)     # rail excluded
    assert (0, 2) not in aff, "+5V edge must be gone with skip_power"
    assert _is_chain_graph(3, aff), "signal-only graph is the 0-1-2 chain"
