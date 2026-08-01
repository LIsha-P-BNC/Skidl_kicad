# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Tests for kicad9's weighted net-stubbing score (_classify_and_stub_complex_nets()
and its helpers in tools/kicad9/gen_schematic.py). Imported via importlib
since `skidl.tools.kicad9`'s __init__.py re-exports the gen_schematic
*function* under the same name as the gen_schematic *module*, which
shadows a plain `import skidl.tools.kicad9.gen_schematic`.
"""

import importlib
import os

import pytest

from skidl import Net, Part
from skidl.geometry import Point, Tx
from skidl.schematics.sch_node import SchNode
from skidl.tools import tool_modules

if os.getenv("SKIDL_TOOL") not in (None, "KICAD9"):
    pytest.skip("Tests require KICAD9 as default tool", allow_module_level=True)

gs = importlib.import_module("skidl.tools.kicad9.gen_schematic")


def _placed_resistor(x, y, value="1K"):
    part = Part("Device", "R", footprint="a", value=value)
    part.tx = Tx().move(Point(x, y))
    return part


def _place(parts_and_xy):
    for part, x, y in parts_and_xy:
        part.tx = Tx().move(Point(x, y))


def _node_with_parts(parts):
    node = SchNode(tool_module=tool_modules["kicad9"])
    node.parts = list(parts)
    return node


def test_net_span_and_pins_computes_manhattan_distance():
    a = _placed_resistor(0, 0)
    b = _placed_resistor(1000, 500)
    net = Net("N")
    net += a[1], b[1]

    pins, max_dist, span = gs._net_span_and_pins(net, {a, b})

    assert len(pins) == 2
    assert max_dist == 1500  # |1000-0| + |500-0|
    assert span is not None


def test_net_span_and_pins_ignores_pins_outside_node():
    a = _placed_resistor(0, 0)
    b = _placed_resistor(1000, 0)
    outside = _placed_resistor(5000, 0)
    net = Net("N")
    net += a[1], b[1], outside[1]

    pins, max_dist, span = gs._net_span_and_pins(net, {a, b})

    assert len(pins) == 2
    assert max_dist == 1000


def test_net_span_and_pins_single_pin_has_no_span():
    a = _placed_resistor(0, 0)
    net = Net("N")
    net += a[1]

    pins, max_dist, span = gs._net_span_and_pins(net, {a})

    assert len(pins) == 1
    assert max_dist == 0
    assert span is None


def test_cluster_id_map_groups_clustered_parts_together():
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="c")
    crystal = Part("Device", "Crystal", footprint="y")
    unrelated = Part("Device", "R", footprint="r")

    osc = Net("OSC1")
    osc += mcu[9], crystal[1]

    cluster_of = gs._cluster_id_map({mcu, crystal, unrelated})

    assert cluster_of[mcu] == cluster_of[crystal]
    assert cluster_of[unrelated] != cluster_of[mcu]


def test_classify_and_stub_leaves_short_clustered_net_wired():
    """A short net fully within one functional cluster should not be
    stubbed even though it has more than 2 pins."""
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="c")
    crystal = Part("Device", "Crystal", footprint="y")
    cap = Part("Device", "C", value="22pF", footprint="ca")

    osc = Net("OSC1")
    osc += mcu[9], crystal[1], cap[1]

    _place([(mcu, 0, 0), (crystal, 50, 0), (cap, 25, 50)])
    node = _node_with_parts([mcu, crystal, cap])

    gs._classify_and_stub_complex_nets(default_circuit, node, auto_stub=True)

    assert getattr(osc, "_stub", False) is False


def test_classify_and_stub_stubs_far_apart_high_fanout_net():
    """A net with more pins than max_wire_pins AND pins far apart should
    still be stubbed -- the weighted score should agree with the old
    hard limits when a net is unambiguously complex."""
    parts = [Part("Device", "R", footprint=str(i), value="1K") for i in range(5)]
    net = Net("BUSY")
    net += [p[1] for p in parts]

    _place([(part, i * 3000, 0) for i, part in enumerate(parts)])
    node = _node_with_parts(parts)

    gs._classify_and_stub_complex_nets(default_circuit, node, auto_stub=True)

    assert getattr(net, "_stub", False) is True


def test_min_pin_distance_uses_closest_pair():
    """_min_pin_distance returns the CLOSEST pair (adjacency), not the span."""
    pts = [Point(0, 0), Point(50, 0), Point(5000, 0)]
    assert gs._min_pin_distance(pts) == 50  # closest pair, not the 5000 span
    assert gs._min_pin_distance([Point(0, 0)]) == 0  # <2 pins -> 0


def test_classify_clock_net_wired_even_when_far():
    """A clock net (crystal / XTAL name) is a WIRE regardless of distance."""
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="c")
    crystal = Part("Device", "Crystal", footprint="y")
    xtal = Net("XTAL1")
    xtal += mcu[9], crystal[1]

    _place([(mcu, 0, 0), (crystal, 5000, 5000)])  # far apart on purpose
    node = _node_with_parts([mcu, crystal])

    gs._classify_and_stub_complex_nets(default_circuit, node, auto_stub=True)

    assert getattr(xtal, "_stub", False) is False


def test_classify_reset_net_wired_even_when_far():
    """A reset net stays a WIRE (function priority) even placed far apart."""
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="c")
    r = Part("Device", "R", footprint="r", value="10k")
    rst = Net("RESET")
    rst += mcu[1], r[1]

    _place([(mcu, 0, 0), (r, 5000, 0)])
    node = _node_with_parts([mcu, r])

    gs._classify_and_stub_complex_nets(default_circuit, node, auto_stub=True)

    assert getattr(rst, "_stub", False) is False


def test_classify_usb_diff_net_wired_even_when_far():
    """A USB differential-pair net stays a WIRE regardless of distance."""
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="c")
    r = Part("Device", "R", footprint="r", value="22")
    dp = Net("USB_DP")
    dp += mcu[2], r[1]

    _place([(mcu, 0, 0), (r, 5000, 0)])
    node = _node_with_parts([mcu, r])

    gs._classify_and_stub_complex_nets(default_circuit, node, auto_stub=True)

    assert getattr(dp, "_stub", False) is False
