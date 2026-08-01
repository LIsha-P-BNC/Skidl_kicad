# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl.geometry import BBox, Point, Tx
from skidl.schematics import place as place_module
from skidl.schematics.place import layout_blocks_by_role
from skidl.tools.kicad9 import constants as kicad9_constants

# layout_blocks_by_role() -> snap_to_grid() needs GRID/BLK_EXT_PAD, which
# are normally injected into this module's globals by Placer.place() at
# the start of a real placement run (see place.py's "Inject the constants
# for the backend tool into this module"). Since these tests call
# layout_blocks_by_role() directly without going through place() first,
# inject them here the same way.
place_module.__dict__.update(kicad9_constants.__dict__)


class FakeBlock:
    def __init__(self, name, w=100, h=100):
        self.name = name
        self.place_bbox = BBox(Point(0, 0), Point(w, h))
        self.tx = Tx()

    def __repr__(self):
        return f"FakeBlock({self.name})"


def test_layout_blocks_by_role_orders_left_to_right():
    power = FakeBlock("power")
    mcu = FakeBlock("mcu")
    conn = FakeBlock("conn")
    # Deliberately mis-positioned (and out of role order in the input
    # list) so a no-op would be distinguishable from a real layout.
    power.tx = Tx().move(Point(500, 7))
    mcu.tx = Tx().move(Point(10, 3))
    conn.tx = Tx().move(Point(250, 9))

    roles = {power: "Power", mcu: "MCU", conn: "Connector"}
    blocks = [conn, power, mcu]

    changed = layout_blocks_by_role(blocks, roles)

    assert changed is True
    assert power.tx.origin.x < mcu.tx.origin.x < conn.tx.origin.x


def test_layout_blocks_by_role_noop_with_no_recognized_roles():
    a = FakeBlock("a")
    b = FakeBlock("b")
    a.tx = Tx().move(Point(42, 1))
    b.tx = Tx().move(Point(99, 2))
    roles = {a: "Other", b: "Other"}

    changed = layout_blocks_by_role([a, b], roles)

    assert changed is False
    assert a.tx.origin == Point(42, 1)
    assert b.tx.origin == Point(99, 2)


def test_layout_blocks_by_role_noop_with_single_recognized_role():
    a = FakeBlock("a")
    b = FakeBlock("b")
    a.tx = Tx().move(Point(42, 1))
    b.tx = Tx().move(Point(99, 2))
    roles = {a: "MCU", b: "Other"}

    changed = layout_blocks_by_role([a, b], roles)

    assert changed is False
    assert a.tx.origin == Point(42, 1)
    assert b.tx.origin == Point(99, 2)


def test_layout_blocks_by_role_stable_within_same_role():
    mcu1 = FakeBlock("mcu1")
    mcu2 = FakeBlock("mcu2")
    power = FakeBlock("power")

    roles = {mcu1: "MCU", mcu2: "MCU", power: "Power"}
    layout_blocks_by_role([mcu1, mcu2, power], roles)

    assert power.tx.origin.x < mcu1.tx.origin.x
    assert power.tx.origin.x < mcu2.tx.origin.x
    # Original relative order between the two same-role blocks preserved.
    assert mcu1.tx.origin.x < mcu2.tx.origin.x


def test_layout_blocks_by_role_spaces_by_block_width():
    narrow = FakeBlock("narrow", w=100)
    wide = FakeBlock("wide", w=400)
    roles = {narrow: "Power", wide: "MCU"}

    layout_blocks_by_role([narrow, wide], roles, role_layout_pad=50)

    assert narrow.tx.origin.x == 0
    assert wide.tx.origin.x == 150  # 100 (narrow's width) + 50 (pad).
