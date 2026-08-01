"""board/pour.py -- the dynamic pour DECISIONS (which nets, which layers),
derived from the board with no hardcoded names or layer numbers.

Pure-python, always runs. The realised geometry (stitching vias, zones)
and its DRC acceptance gate are exercised separately once built.
"""
from skidl.board.model import BoardModel, Net, Footprint, Pad
from skidl.board import pour


def _net(name, code, npads, net_class="Default"):
    return Net(name, code, net_class=net_class,
               pad_refs=[(f"U{code}", str(i)) for i in range(npads)])


def _board(layer_count=2, nets=None):
    b = BoardModel(name="t", layer_count=layer_count)
    b.net_classes = {"Default": {"width": 0.25}, "power": {"width": 0.4}}
    b.nets = {n.name: n for n in (nets or [])}
    return b


def _pad(num, pad_type="smd", drill=0.0):
    return Pad(number=num, net=None, x=0, y=0, size_x=1, size_y=1,
               pad_type=pad_type, drill=drill)


def _layer_board(layer_count):
    """A board with real footprints: GND on an SMD F.Cu pad (U1.1) AND a
    THT pad (J1.1, spans all layers); VBUS on an SMD F.Cu pad (U1.2)."""
    b = BoardModel(name="t", layer_count=layer_count)
    b.net_classes = {"Default": {"width": 0.25}, "power": {"width": 0.4}}
    b.footprints = [
        Footprint(ref="U1", lib_id="x", side="top",
                  pads=[_pad("1"), _pad("2")]),
        Footprint(ref="J1", lib_id="x", side="top",
                  pads=[_pad("1", pad_type="thru_hole", drill=0.8)]),
    ]
    b.nets = {
        "GND":  Net("GND", 1, pad_refs=[("U1", "1"), ("J1", "1")]),
        "VBUS": Net("VBUS", 2, net_class="power", pad_refs=[("U1", "2")]),
    }
    return b


# ----- should_pour: classify-driven with current promotion/demotion -----

def test_ground_always_poured():
    b = _board()
    do, role = pour.should_pour(_net("GND", 1, 4), b)
    assert (do, role) == (True, "ground")


def test_single_pad_net_never_poured():
    b = _board()
    do, role = pour.should_pour(_net("GND", 1, 1), b)   # only 1 pad
    assert do is False


def test_low_current_few_pad_rail_is_routed_not_poured():
    # a 3V3 rail feeding two pads: cheap to route, so NOT poured
    b = _board()
    do, role = pour.should_pour(_net("+3V3", 2, 2, "power"), b)
    assert do is False


def test_wide_fanout_power_rail_poured():
    # same rail feeding many pads (>6) -> poured
    b = _board()
    do, role = pour.should_pour(_net("+3V3", 2, 8, "power"), b)
    assert (do, role) == (True, "power")


def test_declared_high_current_rail_poured():
    # a user-declared 3 A rail needs ~1.4 mm, far over the 0.4 mm power
    # class default -> poured. (A 1.5 A name-heuristic rail would NOT be:
    # ~0.53 mm is under the 1.5x threshold, so it stays routed -- the
    # intended demotion of low-current rails.)
    b = _board()
    do, role = pour.should_pour(_net("VBUS", 3, 3, "power"), b,
                                currents={"VBUS": 3.0})
    assert (do, role) == (True, "power")


def test_modest_current_rail_routed():
    # 1.5 A supply-input heuristic on a 0.4 mm class -> ~0.53 mm < 0.6 mm
    # threshold -> stays routed, not poured
    b = _board()
    do, role = pour.should_pour(_net("VIN", 3, 3, "power"), b)
    assert do is False


def test_high_current_signal_promotes_to_power():
    # a motor-drive net (heuristic 2.0 A) promotes even without a power role
    b = _board()
    do, role = pour.should_pour(_net("MOTOR_A", 4, 2), b)
    assert (do, role) == (True, "power")


def test_plain_signal_not_poured():
    b = _board()
    do, role = pour.should_pour(_net("SCL", 5, 3), b)
    assert do is False


# ----- assign_pour_layers: pad-derived layers + dedicated planes -----

def test_two_layer_ground_both_sides_power_rerouted():
    # GND has an F.Cu SMD pad AND a THT pad -> pours both outer layers;
    # power has no plane on 2-layer -> rerouted (avoids the zone fight).
    a, reroute, warns = pour.assign_pour_layers(
        _layer_board(2), {"GND": "ground", "VBUS": "power"})
    assert a == {"GND": ["F.Cu", "B.Cu"]}
    assert reroute == ["VBUS"]
    assert warns == []


def test_four_layer_ground_skips_power_plane():
    # GND pours its pad layers + In1 plane, but NOT In2 (the power plane);
    # power pours its pad layer + In2, not In1.
    a, reroute, warns = pour.assign_pour_layers(
        _layer_board(4), {"GND": "ground", "VBUS": "power"})
    assert a["GND"] == ["F.Cu", "In1.Cu", "B.Cu"]      # In2 excluded
    assert a["VBUS"] == ["F.Cu", "In2.Cu"]             # In1 excluded
    assert reroute == [] and warns == []


def test_six_layer_planes_and_pad_layers():
    a, reroute, warns = pour.assign_pour_layers(
        _layer_board(6), {"GND": "ground", "VBUS": "power"})
    assert "In2.Cu" not in a["GND"]                    # power plane excluded
    assert {"F.Cu", "In1.Cu", "B.Cu"}.issubset(set(a["GND"]))
    assert a["VBUS"] == ["F.Cu", "In2.Cu"]
    assert reroute == []


def test_odd_stackup_pours_ground_reroutes_power():
    # 3 layers: no dedicated planes -> GND still pours its pad layers,
    # power rerouted. Nothing falls through the crack.
    a, reroute, warns = pour.assign_pour_layers(
        _layer_board(3), {"GND": "ground", "VBUS": "power"})
    assert a["GND"] == ["F.Cu", "In1.Cu", "B.Cu"]      # THT pad spans all 3
    assert reroute == ["VBUS"]


def test_copper_layers_from_count():
    assert pour.copper_layers(_board(2)) == ["F.Cu", "B.Cu"]
    assert pour.copper_layers(_board(4)) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
