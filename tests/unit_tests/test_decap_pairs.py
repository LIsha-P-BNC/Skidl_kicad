"""
tests/unit_tests/test_decap_pairs.py

find_decap_pairs (shared-rail decap -> IC pairing). The net-keyed
find_decap_affinities collapses to ONE cap per shared +V rail (measured on
holdout_can_node: C5/C6/C7 stranded in foreign blocks); the per-cap variant
must pair EVERY bypass cap, in natural ref order per rail, and must ignore
ground rails (where every cap touches every IC).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.schematics.cluster import find_decap_pairs  # noqa: E402


class _Net:
    def __init__(self, name):
        self.name = name
        self.pins = []


class _Pin:
    def __init__(self, part, net):
        self.part = part
        self.net = net
        net.pins.append(self)


class _Part:
    def __init__(self, ref, ref_prefix, value="100nF"):
        self.ref = ref
        self.ref_prefix = ref_prefix
        self.value = value
        self.pins = []

    def pin(self, net):
        self.pins.append(_Pin(self, net))
        return self


def _shared_rail_board():
    """4 ICs + 4 bypass caps, ALL on one +5V rail + GND (the holdout shape)."""
    v5 = _Net("+5V")
    gnd = _Net("GND")
    ics = [_Part(f"U{i}", "U").pin(v5).pin(gnd) for i in (1, 2, 3, 4)]
    caps = [_Part(ref, "C").pin(v5).pin(gnd)
            for ref in ("C2", "C5", "C6", "C7")]
    return ics, caps


def test_every_cap_paired_on_shared_rail():
    ics, caps = _shared_rail_board()
    pairs = find_decap_pairs(ics + caps)
    assert len(pairs) == 4, f"all 4 caps must pair, got {len(pairs)}"
    # ref-order 1:1 -- the numbering-follows-block convention.
    want = {"C2": "U1", "C5": "U2", "C6": "U3", "C7": "U4"}
    got = {c.ref: ic.ref for c, ic in pairs.items()}
    assert got == want, got


def test_extra_caps_go_to_last_ic():
    v5 = _Net("+5V")
    gnd = _Net("GND")
    ics = [_Part("U1", "U").pin(v5).pin(gnd)]
    caps = [_Part(r, "C").pin(v5).pin(gnd) for r in ("C1", "C2", "C3")]
    pairs = find_decap_pairs(ics + caps)
    assert {c.ref: ic.ref for c, ic in pairs.items()} == {
        "C1": "U1", "C2": "U1", "C3": "U1"}


def test_dedicated_rails_pair_directly():
    gnd = _Net("GND")
    vdd_a = _Net("+3.3V")      # only U1 + C1
    vdd_b = _Net("+5V")        # only U2 + C2
    u1 = _Part("U1", "U").pin(vdd_a).pin(gnd)
    u2 = _Part("U2", "U").pin(vdd_b).pin(gnd)
    c1 = _Part("C1", "C").pin(vdd_a).pin(gnd)
    c2 = _Part("C2", "C").pin(vdd_b).pin(gnd)
    pairs = find_decap_pairs([u1, u2, c1, c2])
    assert {c.ref: ic.ref for c, ic in pairs.items()} == {
        "C1": "U1", "C2": "U2"}


def test_natural_ref_order():
    # C10 must sort AFTER C2 (natural order, not lexicographic).
    v5 = _Net("+5V")
    gnd = _Net("GND")
    ics = [_Part(f"U{i}", "U").pin(v5).pin(gnd) for i in (1, 2)]
    caps = [_Part("C10", "C").pin(v5).pin(gnd),
            _Part("C2", "C").pin(v5).pin(gnd)]
    pairs = find_decap_pairs(ics + caps)
    assert {c.ref: ic.ref for c, ic in pairs.items()} == {
        "C2": "U1", "C10": "U2"}


def test_deterministic():
    ics, caps = _shared_rail_board()
    a = {c.ref: ic.ref for c, ic in find_decap_pairs(ics + caps).items()}
    b = {c.ref: ic.ref for c, ic in find_decap_pairs(ics + caps).items()}
    assert a == b
