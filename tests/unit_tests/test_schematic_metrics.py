"""
tests/unit_tests/test_schematic_metrics.py

WI-1 -- pure-geometry guarantees of the TI-readability sub-metrics in
schematics/metrics.py. These lock the coordinate math so the regression
harness (test_circuits/regr_schematic/check_all.py) can trust the numbers it
reports. Each metric is a DISTINCT defect measure -- verified in isolation
with small hand-built fixtures, exactly as test_block_grid.py does for the
grid placer.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.schematics.metrics import (  # noqa: E402
    count_wire_crossings,
    label_overlap_count,
    text_symbol_collisions,
    block_spacing_stats,
    left_to_right_flow,
    sheet_extent,
)


# ---- count_wire_crossings -------------------------------------------------

def test_crossing_interior():
    # A horizontal and a vertical wire crossing at (5,0): one crossing.
    h = ((0, 0), (10, 0))
    v = ((5, -5), (5, 5))
    assert count_wire_crossings([h, v]) == 1


def test_no_crossing_parallel():
    a = ((0, 0), (10, 0))
    b = ((0, 2), (10, 2))
    assert count_wire_crossings([a, b]) == 0


def test_shared_endpoint_is_not_a_crossing():
    # Two wires meeting at (10,0) is a junction/connection, not a crossing.
    a = ((0, 0), (10, 0))
    b = ((10, 0), (10, 10))
    assert count_wire_crossings([a, b]) == 0


def test_t_junction_endpoint_on_other_wire_is_not_crossing():
    # b ENDS on the interior of a -> a T (connection), not a wire-over-wire.
    a = ((0, 0), (10, 0))
    b = ((5, 0), (5, 5))
    assert count_wire_crossings([a, b]) == 0


def test_multiple_crossings_counted():
    h1 = ((0, 0), (10, 0))
    h2 = ((0, 4), (10, 4))
    v = ((5, -2), (5, 6))          # crosses both h1 and h2
    assert count_wire_crossings([h1, h2, v]) == 2


# ---- label_overlap_count --------------------------------------------------

def test_label_overlap():
    a = (0, 0, 4, 1)
    b = (2, 0, 6, 1)               # overlaps a in x
    assert label_overlap_count([a, b]) == 1


def test_label_no_overlap_touching_edges():
    a = (0, 0, 4, 1)
    b = (4, 0, 8, 1)               # share only an edge -> not overlapping
    assert label_overlap_count([a, b]) == 0


# ---- text_symbol_collisions ----------------------------------------------

def test_text_inside_body_collides():
    body = [(0, 0, 10, 10)]
    text = (3, 3, 6, 4)            # fully inside the symbol body
    assert text_symbol_collisions([text], body) == 1


def test_text_outside_body_ok():
    body = [(0, 0, 10, 10)]
    text = (11, 3, 14, 4)          # to the right of the body
    assert text_symbol_collisions([text], body) == 0


# ---- block_spacing_stats --------------------------------------------------

def test_block_spacing_consistent_has_zero_stdev():
    # Three equal boxes evenly spaced in a row: uniform nearest-gap -> stdev 0.
    boxes = [(0, 0, 10, 10), (20, 0, 30, 10), (40, 0, 50, 10)]
    s = block_spacing_stats(boxes)
    assert s["overlaps"] == 0
    assert abs(s["min_gap"] - 10.0) < 1e-6
    assert s["stdev_gap"] < 1e-6


def test_block_spacing_detects_overlap():
    boxes = [(0, 0, 10, 10), (5, 0, 15, 10)]   # overlapping pair
    s = block_spacing_stats(boxes)
    assert s["overlaps"] == 1


# ---- left_to_right_flow ---------------------------------------------------

def test_flow_monotonic_is_one():
    assert left_to_right_flow([0, 10, 20, 30]) == 1.0


def test_flow_backward_penalized():
    # 3 steps, one goes backward (20 -> 5): 2/3 forward.
    assert abs(left_to_right_flow([0, 20, 5, 30]) - (2 / 3)) < 1e-6


# ---- sheet_extent ---------------------------------------------------------

def test_sheet_extent_square_is_zero_dev():
    s = sheet_extent([(0, 0), (100, 0), (0, 100), (100, 100)])
    assert s["w"] == 100 and s["h"] == 100 and s["area"] == 10000
    assert s["aspect_dev"] < 1e-9


def test_sheet_extent_wide_row_penalized():
    # A 300x50 flow-row sprawl vs a 120x120 grid of the same content:
    # the grid must score a LOWER aspect_dev and lower area.
    row = sheet_extent([(0, 0), (300, 50)])
    grid = sheet_extent([(0, 0), (120, 120)])
    assert grid["aspect_dev"] < row["aspect_dev"]
    assert grid["area"] < row["area"]


def test_sheet_extent_degenerate_safe():
    assert sheet_extent([])["area"] == 0.0
    assert sheet_extent([(5, 5)])["area"] == 0.0
    assert sheet_extent([(0, 0), (10, 0)])["area"] == 0.0  # zero height
