"""
tests/unit_tests/test_densify_ceiling.py

Utilization-aware HPWL ceiling for the PCB densify ladder. Far below the
util floor the ceiling opens up (buy compactness with a little wire length,
the TI "small circuit -> small board" trade); at/above the floor it holds the
tight +5%. Monotonic and bounded so no board can densify unboundedly.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.board.place.rules import (  # noqa: E402
    DENSIFY_HPWL_BASE, DENSIFY_HPWL_MAX, UTIL_FLOOR, _densify_hpwl_ceiling,
)


def test_at_floor_is_base():
    assert abs(_densify_hpwl_ceiling(UTIL_FLOOR) - DENSIFY_HPWL_BASE) < 1e-9


def test_above_floor_clamps_to_base():
    # A board already denser than the floor gets no extra slack.
    assert abs(_densify_hpwl_ceiling(0.60) - DENSIFY_HPWL_BASE) < 1e-9


def test_zero_util_is_max():
    assert abs(_densify_hpwl_ceiling(0.0) - DENSIFY_HPWL_MAX) < 1e-9


def test_monotonic_decreasing_with_util():
    vals = [_densify_hpwl_ceiling(u) for u in (0.0, 0.1, 0.2, 0.3, 0.35)]
    assert vals == sorted(vals, reverse=True), vals


def test_bounded():
    for u in (-0.5, 0.0, 0.15, 0.35, 0.9, 2.0):
        c = _densify_hpwl_ceiling(u)
        assert DENSIFY_HPWL_BASE - 1e-9 <= c <= DENSIFY_HPWL_MAX + 1e-9, (u, c)


def test_holdout_15pct_opens_enough_for_the_rung():
    # holdout sat at ~15% util; the 1.0mm rung cost +9.6% HPWL and was
    # rejected by the old flat +5%. The util-aware ceiling must now admit it.
    assert _densify_hpwl_ceiling(0.15) > 1.096
