# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl.geometry import Point, Segment
from skidl.schematics.route import Router


class FakeNode:
    def __init__(self, wires):
        self.wires = wires


def test_count_wire_crossings_counts_different_net_intersections():
    net_a = "A"
    net_b = "B"
    wires = {
        net_a: [Segment(Point(0, 5), Point(10, 5))],  # Horizontal.
        net_b: [Segment(Point(5, 0), Point(5, 10))],  # Vertical, crosses A.
    }
    node = FakeNode(wires)

    assert Router.count_wire_crossings(node) == 1


def test_count_wire_crossings_ignores_same_net_intersections():
    """Same-net intersections are real junctions, not visual crossings."""
    net_a = "A"
    wires = {
        net_a: [
            Segment(Point(0, 5), Point(10, 5)),
            Segment(Point(5, 0), Point(5, 10)),
        ],
    }
    node = FakeNode(wires)

    assert Router.count_wire_crossings(node) == 0


def test_count_wire_crossings_no_crossing_is_zero():
    wires = {
        "A": [Segment(Point(0, 0), Point(10, 0))],
        "B": [Segment(Point(0, 5), Point(10, 5))],
    }
    node = FakeNode(wires)

    assert Router.count_wire_crossings(node) == 0


def test_count_wire_crossings_counts_multiple_pairs():
    wires = {
        "A": [Segment(Point(0, 5), Point(10, 5))],
        "B": [Segment(Point(5, 0), Point(5, 10))],
        "C": [Segment(Point(3, 0), Point(3, 10))],
    }
    node = FakeNode(wires)

    # A crosses B and A crosses C -- B and C are parallel verticals, no
    # crossing between them.
    assert Router.count_wire_crossings(node) == 2


def test_count_wire_crossings_empty_wires():
    assert Router.count_wire_crossings(FakeNode({})) == 0
