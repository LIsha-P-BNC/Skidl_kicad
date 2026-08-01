# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

import os

import pytest

from skidl import Bus, Net, Part
from skidl.geometry import Point

if os.getenv("SKIDL_TOOL") not in (None, "KICAD9"):
    pytest.skip("Tests require KICAD9 as default tool", allow_module_level=True)

from skidl.tools.kicad9.sexp_schematic import (
    bus_entry_to_sexp,
    bus_spine_y,
    bus_to_sexp,
    detect_bus_group,
)


class FakeCircuit:
    def __init__(self, buses):
        self.buses = buses


class FakePin:
    def __init__(self, net):
        self.net = net


def test_detect_bus_group_finds_member_net():
    bus = Bus("DATA", 4)
    circuit = FakeCircuit([bus])
    pin = FakePin(bus.nets[2])

    result = detect_bus_group(pin, circuit)

    assert result == (bus, 2)


def test_detect_bus_group_none_for_non_member_net():
    bus = Bus("DATA", 4)
    circuit = FakeCircuit([bus])
    other_net = Net("UNRELATED")
    pin = FakePin(other_net)

    assert detect_bus_group(pin, circuit) is None


def test_detect_bus_group_none_for_no_net():
    circuit = FakeCircuit([Bus("DATA", 4)])
    pin = FakePin(None)

    assert detect_bus_group(pin, circuit) is None


def test_detect_bus_group_ignores_similarly_named_nets_not_in_a_bus():
    """Bus membership comes only from circuit.buses -- never inferred
    from net names, even ones that look like sequential bus bits."""
    circuit = FakeCircuit([])  # No actual Bus object.
    gpio0 = Net("GPIO0")
    pin = FakePin(gpio0)

    assert detect_bus_group(pin, circuit) is None


def test_bus_entry_to_sexp_lands_diagonally():
    entry, landing = bus_entry_to_sexp(Point(100, 100), spine_y=90)

    assert entry[0] == "bus_entry"
    # 45-degree diagonal: dx and dy have equal magnitude.
    size_entry = next(e for e in entry if isinstance(e, list) and e[0] == "size")
    assert abs(size_entry[1]) == abs(size_entry[2])
    assert landing.y == 90  # Lands exactly on the given spine.


def test_bus_entry_to_sexp_uses_shared_spine_regardless_of_pin_y():
    """Two pins at different Y both land on the SAME spine Y -- the bug
    this guards against: computing each entry's landing independently
    (e.g. a fixed offset from each pin's own Y) would give a "bus" whose
    entries don't actually share a line."""
    spine_y = bus_spine_y([Point(0, 100), Point(50, 40)])
    _e1, landing1 = bus_entry_to_sexp(Point(0, 100), spine_y)
    _e2, landing2 = bus_entry_to_sexp(Point(50, 40), spine_y)

    assert landing1.y == landing2.y == spine_y
    assert spine_y < 40  # Above the topmost (smallest-Y) pin.


def test_bus_spine_y_empty_points():
    assert bus_spine_y([]) is None


def test_bus_to_sexp_spans_all_landings():
    bus = Bus("DATA", 4)
    landings = [Point(0, 50), Point(10, 50), Point(25, 50)]

    elements = bus_to_sexp(bus, landings)

    assert len(elements) == 2  # Bus line + range label.
    bus_line = elements[0]
    assert bus_line[0] == "bus"
    pts = next(e for e in bus_line if isinstance(e, list) and e[0] == "pts")
    xs = sorted(pt[1] for pt in pts[1:])
    assert xs == [0, 25]


def test_bus_to_sexp_label_text_uses_ascending_bit_range():
    bus = Bus("DATA", 8)
    elements = bus_to_sexp(bus, [Point(0, 0), Point(10, 0)])
    range_label = elements[1]
    assert range_label[1] == "DATA[0..7]"


def test_bus_to_sexp_empty_for_single_landing():
    bus = Bus("DATA", 4)
    assert bus_to_sexp(bus, [Point(0, 0)]) == []


def test_bus_to_sexp_empty_for_no_landings():
    bus = Bus("DATA", 4)
    assert bus_to_sexp(bus, []) == []
