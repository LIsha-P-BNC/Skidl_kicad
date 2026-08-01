# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from collections import defaultdict

from skidl.geometry import BBox, Point, Tx
from skidl.schematics.place import (
    _count_virtual_crossings,
    _virtual_net_segments,
    reduce_crossings_by_orientation,
)


class FakePin:
    def __init__(self, part, place_pt):
        self.part = part
        self.place_pt = place_pt


class FakePart:
    def __init__(self, x):
        self.tx = Tx().move(Point(x, 0))
        self.place_bbox = BBox(Point(-50, -50), Point(50, 50))
        self.orientation_locked = False
        self.anchor_pins = defaultdict(list)
        self.pull_pins = defaultdict(list)
        self.pin_ctrs = {}


def _crossed_pair():
    """Two parts with pins wired in an X pattern (a classic crossing):
    A.top -- B.bottom and A.bottom -- B.top. Rotating either part 180
    degrees swaps its top/bottom pin positions and un-crosses the wires.
    """
    a = FakePart(x=0)
    b = FakePart(x=300)

    pin_a_top = FakePin(a, Point(0, 50))
    pin_a_bot = FakePin(a, Point(0, -50))
    pin_b_top = FakePin(b, Point(0, 50))
    pin_b_bot = FakePin(b, Point(0, -50))

    a.anchor_pins["N1"] = [pin_a_top]
    a.pull_pins["N1"] = [pin_b_bot]
    b.anchor_pins["N1"] = [pin_b_bot]
    b.pull_pins["N1"] = [pin_a_top]

    a.anchor_pins["N2"] = [pin_a_bot]
    a.pull_pins["N2"] = [pin_b_top]
    b.anchor_pins["N2"] = [pin_b_top]
    b.pull_pins["N2"] = [pin_a_bot]

    return a, b


def test_virtual_crossings_detects_x_pattern():
    a, b = _crossed_pair()
    assert _count_virtual_crossings(_virtual_net_segments([a, b])) > 0


def test_reduce_crossings_by_orientation_is_noop_without_option():
    a, b = _crossed_pair()
    changed = reduce_crossings_by_orientation([a, b])  # No minimize_crossings kwarg.
    assert changed == 0
    # Still crossed -- nothing was touched.
    assert _count_virtual_crossings(_virtual_net_segments([a, b])) > 0


def test_reduce_crossings_by_orientation_untangles_x_pattern():
    a, b = _crossed_pair()
    assert _count_virtual_crossings(_virtual_net_segments([a, b])) > 0

    changed = reduce_crossings_by_orientation([a, b], minimize_crossings=True)

    assert changed > 0
    assert _count_virtual_crossings(_virtual_net_segments([a, b])) == 0


def test_reduce_crossings_by_orientation_skips_locked_parts():
    a, b = _crossed_pair()
    a.orientation_locked = True
    b.orientation_locked = True

    changed = reduce_crossings_by_orientation([a, b], minimize_crossings=True)

    assert changed == 0
    assert _count_virtual_crossings(_virtual_net_segments([a, b])) > 0
