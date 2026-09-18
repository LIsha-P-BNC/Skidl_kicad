"""
tests/unit_tests/test_pcb_reverse.py

PCB-only reverse flow (import_pcb): parse a .anvil_pcb -> infer symbols ->
synthesize a KiCad netlist that round-trips through netlist_to_skidl. Uses a
hand-written synthetic board fixture (no app, no real routed board needed).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.board import pcb_reverse  # noqa: E402

# Synthetic board: R1 (0805), C1 (electrolytic CP), J1 (2-pin conn), U1 (SOIC-8).
# Nets: VIN, VOUT, GND. Enough to exercise every inference branch.
_FIXTURE = '''(kicad_pcb (version 20240108) (generator "test")
  (footprint "Resistor_SMD:R_0805_2012Metric" (layer "F.Cu")
    (property "Reference" "R1" (at 0 0))
    (property "Value" "10k" (at 0 1))
    (pad "1" smd roundrect (at -1 0) (net 1 "VIN"))
    (pad "2" smd roundrect (at 1 0) (net 2 "VOUT")))
  (footprint "Capacitor_SMD:CP_Elec_5x5.4" (layer "F.Cu")
    (property "Reference" "C1" (at 5 0))
    (property "Value" "10uF" (at 5 1))
    (pad "1" smd roundrect (at 4 0) (net 2 "VOUT"))
    (pad "2" smd roundrect (at 6 0) (net 3 "GND")))
  (footprint "Connector_PinHeader:PinHeader_1x02" (layer "F.Cu")
    (property "Reference" "J1" (at 10 0))
    (property "Value" "Conn" (at 10 1))
    (pad "1" thru_hole circle (at 9 0) (net 1 "VIN"))
    (pad "2" thru_hole circle (at 11 0) (net 3 "GND")))
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm" (layer "F.Cu")
    (property "Reference" "U1" (at 20 0))
    (property "Value" "MYIC123" (at 20 1))
    (pad "1" smd roundrect (at 19 0) (net 1 "VIN"))
    (pad "2" smd roundrect (at 19 1) (net 3 "GND"))
    (pad "3" smd roundrect (at 19 2) (net 2 "VOUT"))
    (pad "4" smd roundrect (at 19 3) (net 3 "GND"))
    (pad "5" smd roundrect (at 21 3))
    (pad "6" smd roundrect (at 21 2) (net 2 "VOUT"))
    (pad "7" smd roundrect (at 21 1) (net 1 "VIN"))
    (pad "8" smd roundrect (at 21 0) (net 3 "GND")))
  (footprint "MountingHole:MountingHole_3.2mm" (layer "F.Cu")
    (pad "" np_thru_hole circle (at 30 0)))
)
'''


def _write_fixture(tmp_path):
    p = tmp_path / "board_in.anvil_pcb"
    p.write_text(_FIXTURE, encoding="utf-8")
    return p


def test_parse_components_and_nets(tmp_path):
    parsed = pcb_reverse.parse_pcb(_write_fixture(tmp_path))
    refs = sorted(c["ref"] for c in parsed["components"])
    assert refs == ["C1", "J1", "R1", "U1"], "mounting hole must be skipped, 4 parts"
    nets = {n["name"]: n for n in parsed["nets"]}
    assert set(nets) == {"VIN", "VOUT", "GND"}
    # GND ties C1.2, J1.2, U1.2, U1.4, U1.8
    gnd_nodes = {tuple(x) for x in nets["GND"]["nodes"]}
    assert ("C1", "2") in gnd_nodes and ("J1", "2") in gnd_nodes
    assert ("U1", "2") in gnd_nodes and ("U1", "8") in gnd_nodes
    # U1 pad 5 has no net -> not in any net
    for n in parsed["nets"]:
        assert ("U1", "5") not in {tuple(x) for x in n["nodes"]}


def test_infer_symbol_branches(tmp_path):
    comps = {c["ref"]: c for c in pcb_reverse.parse_pcb(_write_fixture(tmp_path))["components"]}
    r = pcb_reverse.infer_symbol(comps["R1"])
    assert (r["lib"], r["part"], r["confidence"]) == ("Device", "R", "exact")
    c = pcb_reverse.infer_symbol(comps["C1"])
    assert (c["lib"], c["part"], c["confidence"]) == ("Device", "CP", "exact")  # electrolytic
    j = pcb_reverse.infer_symbol(comps["J1"])
    assert (j["lib"], j["part"], j["confidence"]) == ("Connector_Generic", "Conn_01x02", "exact")
    u = pcb_reverse.infer_symbol(comps["U1"])
    assert u["lib"] == "ReversePCB" and u["confidence"] == "generic"
    assert len(u["generic_pins"]) == 8, "IC gets a generic 8-pin box"


def test_analyze_and_netlist_roundtrip(tmp_path):
    a = pcb_reverse.analyze(_write_fixture(tmp_path))
    assert a["counts"] == {"parts": 4, "nets": 3, "generic": 1}
    assert len(a["generic_symbols"]) == 1 and a["generic_symbols"][0]["ref"] == "U1"
    # the synthesized netlist must round-trip through the real converter
    from skidl.netlist_to_skidl import netlist_to_skidl
    code = netlist_to_skidl(a["netlist_text"], output_dir=None)
    assert isinstance(code, str) and code.strip(), "converter produced no code"
    # every part must appear as a Part(...) in the generated SKiDL
    for ref in ("R1", "C1", "J1", "U1"):
        assert ref in code, f"{ref} missing from generated SKiDL"
    assert 'Part("Device", "R"' in code or "Device" in code
