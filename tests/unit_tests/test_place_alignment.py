# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl.geometry import Point, Tx
from skidl.schematics.place import (
    restore_anchor_pull_pins,
    retain_closest_anchor_pull_pins,
)


class FakePullPin:
    def __init__(self, place_pt):
        self.place_pt = place_pt
        self.part = FakePinOwner()


class FakePinOwner:
    tx = Tx()


class FakePart:
    def __init__(self):
        self.tx = Tx()
        self.anchor_pins = {}
        self.pull_pins = {}
        self.pin_ctrs = {}


def test_retain_closest_anchor_pull_pins_trims_to_nearest():
    part = FakePart()
    near = FakePullPin(Point(5, 0))
    far = FakePullPin(Point(20, 0))
    mid = FakePullPin(Point(10, 0))
    part.pin_ctrs["N"] = Point(0, 0)
    part.pull_pins["N"] = [far, near, mid]

    retain_closest_anchor_pull_pins([part])

    assert part.pull_pins["N"] == [near]


def test_retain_closest_anchor_pull_pins_then_restore_recovers_original():
    part = FakePart()
    near = FakePullPin(Point(5, 0))
    far = FakePullPin(Point(20, 0))
    part.pin_ctrs["N"] = Point(0, 0)
    original = [far, near]
    part.pull_pins["N"] = original

    retain_closest_anchor_pull_pins([part])
    assert part.pull_pins["N"] == [near]

    restore_anchor_pull_pins([part])
    assert part.pull_pins["N"] == original


def test_retain_closest_anchor_pull_pins_repeated_call_does_not_lose_original():
    """Consecutive alignment-schedule rows call this back-to-back (see
    push_and_pull()'s align_parts dispatch) -- a second call must not
    re-save the already-trimmed list over the true original."""
    part = FakePart()
    near = FakePullPin(Point(5, 0))
    far = FakePullPin(Point(20, 0))
    part.pin_ctrs["N"] = Point(0, 0)
    original = [far, near]
    part.pull_pins["N"] = original

    retain_closest_anchor_pull_pins([part])
    retain_closest_anchor_pull_pins([part])  # Second call, same alignment pass.
    assert part.pull_pins["N"] == [near]

    restore_anchor_pull_pins([part])
    assert part.pull_pins["N"] == original


def test_retain_closest_anchor_pull_pins_leaves_single_pull_pin_alone():
    part = FakePart()
    only = FakePullPin(Point(5, 0))
    part.pin_ctrs["N"] = Point(0, 0)
    part.pull_pins["N"] = [only]

    retain_closest_anchor_pull_pins([part])

    assert part.pull_pins["N"] == [only]


def test_retain_closest_anchor_pull_pins_skips_net_without_centroid():
    """A net with no computed centroid (e.g. the floating-parts
    "similarity" pseudo-net) is left untrimmed rather than raising, even
    when the part has a centroid for some OTHER net (so the per-net
    check runs, not just an early part-level bail-out)."""
    part = FakePart()
    pins = [FakePullPin(Point(5, 0)), FakePullPin(Point(20, 0))]
    part.pull_pins["similarity"] = pins
    # This part DOES have a centroid, but for a different net -- so the
    # per-net "no centroid for this net" branch is what's exercised.
    part.pin_ctrs["OTHER_NET"] = Point(0, 0)

    retain_closest_anchor_pull_pins([part])

    assert part.pull_pins["similarity"] == pins


def test_retain_closest_anchor_pull_pins_ignores_objects_without_pin_ctrs():
    """place_blocks()'s PartBlock has anchor_pins/pull_pins (for the
    "similarity" force) but no pin_ctrs attribute at all (only real
    Parts get the full add_anchor_pull_pins() machinery) -- must not
    raise AttributeError."""

    class BlockLike:
        anchor_pins = {}
        pull_pins = {"similarity": [FakePullPin(Point(0, 0))]}

    retain_closest_anchor_pull_pins([BlockLike()])  # Must not raise.
