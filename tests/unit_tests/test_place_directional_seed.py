# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

import random

from skidl import Net, Part
from skidl.geometry import BBox, Point, Tx
from skidl.schematics.place import define_placement_bbox, directional_seed_placement


def _resistor(value="1K"):
    part = Part("Device", "R", footprint="a", value=value)
    # directional_seed_placement()/define_placement_bbox() only need
    # place_bbox's area/w/h and an initial tx -- skip
    # add_placement_bboxes()'s realistic (but here irrelevant)
    # label/routing-padding pipeline.
    part.place_bbox = BBox(Point(0, 0), Point(100, 100))
    part.tx = Tx()
    return part


def test_directional_seed_placement_biases_power_and_ground_y():
    random.seed(42)

    pwr_part = _resistor()
    gnd_part = _resistor()
    sig_part = _resistor()

    vdd = Net("VDD")
    vdd += pwr_part[1], pwr_part[2]  # Both pins on a power-classified net.

    gnd = Net("GND")
    gnd += gnd_part[1], gnd_part[2]  # Both pins on a ground-classified net.

    sig = Net("SIGNAL")
    sig += sig_part[1], sig_part[2]  # Neither power nor ground.

    parts = [pwr_part, gnd_part, sig_part]
    directional_seed_placement(parts)

    bbox_h = define_placement_bbox(parts).h

    # Power-role part seeded in the top quarter (large internal Y, which
    # renders toward the top of the KiCad sheet -- see
    # directional_seed_placement()'s docstring for the Y-flip math).
    assert pwr_part.tx.origin.y >= bbox_h * 0.75
    # Ground-role part seeded in the bottom quarter (small internal Y).
    assert gnd_part.tx.origin.y <= bbox_h * 0.25


def test_directional_seed_placement_orders_x_by_depth():
    random.seed(7)

    connector = Part("Connector_Generic", "Conn_01x02", footprint="a")
    connector.place_bbox = BBox(Point(0, 0), Point(100, 100))
    connector.tx = Tx()
    mid = _resistor()
    leaf = _resistor()

    sig_a = Net("SIG_A")
    sig_a += connector[1], mid[1]
    sig_b = Net("SIG_B")
    sig_b += mid[2], leaf[1]

    parts = [connector, mid, leaf]
    directional_seed_placement(parts)

    assert connector.tx.origin.x < mid.tx.origin.x < leaf.tx.origin.x


def test_directional_seed_placement_empty_list_is_a_noop():
    directional_seed_placement([])  # Must not raise.
