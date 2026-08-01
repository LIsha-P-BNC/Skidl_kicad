# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl import Part, Net

from skidl.schematics.cluster import (
    classify_block_role,
    compute_net_affinity_weights,
    detect_clusters,
    find_anchor_parts,
    find_decap_affinities,
    is_decoupling_cap,
    order_blocks_by_role,
)


class FakePin:
    def __init__(self, part):
        self.part = part
        self.net = None


class FakePart:
    def __init__(self, name, ref_prefix="U", value="", search_text=""):
        self.name = name
        self.pins = []
        self.ref_prefix = ref_prefix
        self.value = value
        self.search_text = search_text

    def __repr__(self):
        return f"FakePart({self.name})"


class FakeNet:
    def __init__(self, name, drive=None):
        self.name = name
        self.pins = []
        self.drive = drive


def connect(net, *parts):
    """Attach one new pin per part to net, both directions."""
    for part in parts:
        pin = FakePin(part)
        pin.net = net
        part.pins.append(pin)
        net.pins.append(pin)


def make_decap(name="C1", value="100nF", gnd_net=None):
    """A 2-pin decoupling cap: one pin left for the caller to connect to
    a power rail, the other tied to a (shared, unless overridden) GND net.
    """
    decap = FakePart(name, ref_prefix="C", value=value)
    connect(gnd_net or FakeNet("GND"), decap)
    return decap


# --- find_anchor_parts -----------------------------------------------------


def test_find_anchor_parts_filters_and_ranks_by_pin_count():
    mcu = FakePart("U1", ref_prefix="U")
    q1 = FakePart("Q1", ref_prefix="Q")
    r1 = FakePart("R1", ref_prefix="R")
    connect(FakeNet("A"), mcu)
    connect(FakeNet("B"), mcu)
    connect(FakeNet("C"), mcu)
    connect(FakeNet("D"), q1)

    anchors = find_anchor_parts([mcu, q1, r1])
    assert anchors == [mcu, q1]


# --- detect_clusters --------------------------------------------------------


def test_detect_clusters_pulls_in_low_fanout_neighbor():
    mcu = FakePart("U1", ref_prefix="U")
    crystal = FakePart("Y1", ref_prefix="Y")
    unrelated = FakePart("R1", ref_prefix="R")

    connect(FakeNet("OSC_IN"), mcu, crystal)
    connect(FakeNet("OSC_OUT"), mcu, crystal)
    # Unrelated part on its own net -- shouldn't join the cluster.
    connect(FakeNet("SOME_SIGNAL"), unrelated)

    clusters = detect_clusters([mcu, crystal, unrelated])
    assert len(clusters) == 1
    assert clusters[0] == {mcu, crystal}


def test_detect_clusters_excludes_power_net_neighbors():
    mcu = FakePart("U1", ref_prefix="U")
    far_part = FakePart("R1", ref_prefix="R")
    # Only connection is through a power net -- must not be pulled in,
    # since a shared rail would otherwise merge unrelated parts.
    connect(FakeNet("VDD"), mcu, far_part)

    clusters = detect_clusters([mcu, far_part])
    assert clusters == [{mcu}]


def test_detect_clusters_does_not_swallow_another_ic():
    u1 = FakePart("U1", ref_prefix="U")
    u2 = FakePart("U2", ref_prefix="U")
    connect(FakeNet("LINK"), u1, u2)

    clusters = detect_clusters([u1, u2])
    assert {frozenset(c) for c in clusters} == {frozenset({u1}), frozenset({u2})}


def test_detect_clusters_respects_max_hops():
    mcu = FakePart("U1", ref_prefix="U")
    hop1 = FakePart("R1", ref_prefix="R")
    hop2 = FakePart("R2", ref_prefix="R")
    hop3 = FakePart("R3", ref_prefix="R")
    connect(FakeNet("A"), mcu, hop1)
    connect(FakeNet("B"), hop1, hop2)
    connect(FakeNet("C"), hop2, hop3)

    clusters = detect_clusters([mcu, hop1, hop2, hop3], max_hops=1)
    assert clusters[0] == {mcu, hop1}


# --- is_decoupling_cap -------------------------------------------------------


def test_is_decoupling_cap_true_for_typical_value_spellings():
    for value in ["100nF", "100n", "0.1uF", "0.1u"]:
        decap = make_decap(value=value)
        connect(FakeNet("VDD"), decap)  # Second pin, mirrors real usage.
        assert is_decoupling_cap(decap), value


def test_is_decoupling_cap_false_for_wrong_prefix():
    part = FakePart("R1", ref_prefix="R", value="100nF")
    assert not is_decoupling_cap(part)


def test_is_decoupling_cap_false_for_wrong_value():
    decap = make_decap(value="10uF")
    connect(FakeNet("VDD"), decap)
    assert not is_decoupling_cap(decap)


def test_is_decoupling_cap_false_for_wrong_pin_count():
    part = make_decap()
    part.pins = [FakePin(part)]  # Only 1 pin.
    assert not is_decoupling_cap(part)


# --- find_decap_affinities --------------------------------------------------


def test_find_decap_affinities_links_decap_to_ic_power_pin():
    mcu = FakePart("U1", ref_prefix="U")
    decap = make_decap()
    unrelated_cap = make_decap(name="C2")

    vdd = FakeNet("VDD")
    connect(vdd, mcu, decap)
    # Not connected to any IC -- shouldn't produce an affinity.
    connect(FakeNet("GND"), unrelated_cap)

    affinities = find_decap_affinities([mcu, decap, unrelated_cap])
    assert affinities == {vdd: (decap, mcu)}


def test_find_decap_affinities_ignores_non_power_net():
    mcu = FakePart("U1", ref_prefix="U")
    decap = make_decap()
    connect(FakeNet("GPIO0"), mcu, decap)

    assert find_decap_affinities([mcu, decap]) == {}


# --- compute_net_affinity_weights ------------------------------------------


def test_compute_net_affinity_weights_boosts_cluster_net():
    mcu = FakePart("U1", ref_prefix="U")
    crystal = FakePart("Y1", ref_prefix="Y")
    osc = FakeNet("OSC_IN")
    connect(osc, mcu, crystal)
    connect(FakeNet("OSC_OUT"), mcu, crystal)

    weights = compute_net_affinity_weights([mcu, crystal], cluster_boost=3.0)
    assert weights[osc] == 3.0


def test_compute_net_affinity_weights_boosts_decap_net():
    mcu = FakePart("U1", ref_prefix="U")
    decap = make_decap()
    vdd = FakeNet("VDD")
    connect(vdd, mcu, decap)

    weights = compute_net_affinity_weights([mcu, decap], decap_boost=6.0)
    assert weights[vdd] == 6.0


def test_compute_net_affinity_weights_empty_for_unrelated_parts():
    r1 = FakePart("R1", ref_prefix="R")
    r2 = FakePart("R2", ref_prefix="R")
    connect(FakeNet("SIG"), r1, r2)

    assert compute_net_affinity_weights([r1, r2]) == {}


# --- classify_block_role -----------------------------------------------------


def test_classify_block_role_communication():
    mcu = FakePart("U1", ref_prefix="U")
    connect(FakeNet("CAN_H"), mcu)
    assert classify_block_role([mcu]) == "Communication"


def test_classify_block_role_sensor():
    mcu = FakePart("U1", ref_prefix="U")
    connect(FakeNet("ADC_IN0"), mcu)
    assert classify_block_role([mcu]) == "Sensor"


def test_classify_block_role_power_by_inductor():
    reg = FakePart("U1", ref_prefix="U")
    inductor = FakePart("L1", ref_prefix="L")
    connect(FakeNet("SW"), reg, inductor)
    assert classify_block_role([reg, inductor]) == "Power"


def test_classify_block_role_power_by_keyword():
    reg = FakePart("U1", ref_prefix="U", search_text="3A buck regulator")
    connect(FakeNet("VOUT"), reg)
    assert classify_block_role([reg]) == "Power"


def test_classify_block_role_connector():
    j1 = FakePart("J1", ref_prefix="J")
    j2 = FakePart("J2", ref_prefix="J")
    connect(FakeNet("PIN1"), j1)
    connect(FakeNet("PIN2"), j2)
    assert classify_block_role([j1, j2]) == "Connector"


def test_classify_block_role_mcu_fallback():
    mcu = FakePart("U1", ref_prefix="U")
    r1 = FakePart("R1", ref_prefix="R")
    connect(FakeNet("GPIO0"), mcu, r1)
    assert classify_block_role([mcu, r1]) == "MCU"


def test_classify_block_role_other_for_ambiguous_group():
    r1 = FakePart("R1", ref_prefix="R")
    r2 = FakePart("R2", ref_prefix="R")
    connect(FakeNet("SIG"), r1, r2)
    assert classify_block_role([r1, r2]) == "Other"


def test_classify_block_role_empty_parts():
    assert classify_block_role([]) == "Other"


# --- order_blocks_by_role ----------------------------------------------------


def test_order_blocks_by_role_sorts_power_mcu_comm_sensor_connector_other():
    blocks = [
        ("Other", "block_other"),
        ("Connector", "block_conn"),
        ("Sensor", "block_sensor"),
        ("Communication", "block_comm"),
        ("MCU", "block_mcu"),
        ("Power", "block_power"),
    ]
    assert order_blocks_by_role(blocks) == [
        "block_power",
        "block_mcu",
        "block_comm",
        "block_sensor",
        "block_conn",
        "block_other",
    ]


def test_order_blocks_by_role_stable_within_same_role():
    blocks = [("Other", "first"), ("Other", "second"), ("MCU", "mcu")]
    assert order_blocks_by_role(blocks) == ["mcu", "first", "second"]


# --- Integration against real SKiDL Part/Net objects ------------------------
#
# The tests above use lightweight fakes to isolate the pure logic. These
# confirm the same functions behave correctly against real Part/Pin/Net
# objects from an actual KiCad library, where attribute access (value,
# ref_prefix, pins, net.pins) goes through the real classes instead of a
# hand-built stand-in.


def test_real_parts_decap_affinity_and_cluster_weighting():
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="a")
    decap = Part("Device", "C", value="100nF", footprint="b")
    bulk_cap = Part("Device", "C", value="10uF", footprint="c")

    vdd_pin = next(p for p in mcu.pins if "VCC" in p.name.upper())
    gnd_pin = next(p for p in mcu.pins if "GND" in p.name.upper())

    vdd = Net("VDD")
    gnd = Net("GND")
    vdd += vdd_pin, decap[1], bulk_cap[1]
    gnd += gnd_pin, decap[2], bulk_cap[2]

    assert is_decoupling_cap(decap)
    assert not is_decoupling_cap(bulk_cap)  # Wrong value class.

    # Only the 100nF cap should register -- the 10uF bulk cap on the same
    # net doesn't match the decoupling-cap value class.
    affinities = find_decap_affinities([mcu, decap, bulk_cap])
    assert affinities[vdd] == (decap, mcu)

    weights = compute_net_affinity_weights([mcu, decap, bulk_cap])
    assert weights[vdd] == 6.0  # Default decap_boost.


def test_real_parts_classify_block_role_mcu_and_connector():
    mcu = Part("MCU_Microchip_ATmega", "ATmega328P-P", footprint="a")
    header = Part("Connector_Generic", "Conn_01x02", footprint="b")

    assert classify_block_role([mcu]) == "MCU"
    assert classify_block_role([header]) == "Connector"
