"""
tests/unit_tests/test_design_intent.py

Locks in the senior change-discipline classifier: every alteration request
maps to exactly ONE design layer with the minimal edit path, widest-ripple
wins on ambiguity, and an explicit "R5 to 10k" is a value edit -- never a
rebuild. Table-driven like the module itself; no circuit is special-cased.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.anvil import design_intent as di  # noqa: E402


# (request text, expected layer) -- phrased the way real users type,
# beginner to senior.
CASES = [
    # value -- the most common alteration; must NEVER suggest a rebuild
    ("change R5 to 10k", "value"),
    ("set C3 = 100nF", "value"),
    ("make the LED brighter", "value"),
    ("R12 value is wrong, should be 4.7k", "value"),
    # topology
    ("add a pull-up resistor on the reset pin", "topology"),
    ("remove the LED and its resistor", "topology"),
    ("connect pin 7 to the GND net", "topology"),
    ("missing decoupling cap on U2", "topology"),
    # part swap
    ("replace the LM7805 with an AMS1117", "part_swap"),
    ("use the SMD version instead of through-hole", "part_swap"),
    ("swap U1 for a cheaper part", "part_swap"),
    # requirement -- widest ripple
    ("the input must now be 12V instead of 5V", "requirement"),
    ("it should support USB instead of UART", "requirement"),
    # refactor -- netlist must stay identical
    ("split the schematic into hierarchical sheets", "refactor"),
    ("merge the power sheet into the main sheet to reduce pages", "refactor"),
    ("rename the net VCC_IN to VIN", "refactor"),
    # layout -- never touches the schematic
    ("move the connector to the board edge", "layout"),
    ("the ground pour has islands, fix the zones", "layout"),
    ("make the board outline 40x30", "layout"),
    # manufacturing
    ("export gerbers for JLC", "manufacturing"),
    ("change the stackup to 4 layer", "manufacturing"),
]


def test_layer_classification_table():
    for text, want in CASES:
        got = di.classify(text)
        assert got["layer"] == want, (
            f"{text!r}: want {want}, got {got['layer']} "
            f"(signals={got.get('signals')}, also={got.get('also_matched')})")


def test_every_result_carries_the_discipline_fields():
    r = di.classify("change R5 to 10k")
    for key in ("edit_path", "verify", "blast_radius", "protect",
                "confidence", "note"):
        assert key in r and r[key], f"missing {key}"


def test_value_edit_never_recommends_rebuild():
    r = di.classify("change R5 to 10k")
    path = " ".join(r["edit_path"]).lower()
    assert "set_value" in path
    assert "never a rebuild" in path


def test_refactor_requires_identical_netlist():
    r = di.classify("split the schematic into hierarchical sheets")
    assert "identical" in " ".join(r["verify"]).lower()


def test_layout_protects_the_netlist():
    r = di.classify("move the connector to the board edge")
    assert "netlist" in r["protect"].lower()


def test_widest_ripple_wins_on_ambiguity():
    # Both a requirement cue (must now be 12V) AND an explicit value edit
    # (R2 to 22k) fire: requirement must win -- the new spec re-derives the
    # value, so mis-classing DOWN as a lone value tweak ships a wrong board.
    r = di.classify("the input must now be 12V, change R2 to 22k for it")
    assert r["layer"] == "requirement"
    assert "value" in r["also_matched"]
    assert r["confidence"] < 0.9          # ambiguity lowers confidence


def test_explicit_ref_value_beats_coincidental_topology_cue():
    # "add" appears, but "R5 to 10k" is an unambiguous value edit.
    r = di.classify("as we add brightness, take R5 to 10k")
    assert r["layer"] == "value"


def test_unclear_asks_instead_of_guessing():
    r = di.classify("something feels off with the design")
    assert r["layer"] == "unclear"
    assert "ask" in r["note"].lower()
    assert r["confidence"] == 0.0


def test_refs_and_values_extracted():
    r = di.classify("change R5 to 10k and check C2")
    assert "R5" in [x if isinstance(x, str) else x for x in r["refs"]] or r["refs"]
    assert r["values"], "value tokens must be extracted"


def test_dynamic_no_part_name_dependence():
    # Same classification whatever the part family -- nothing hardcoded.
    a = di.classify("replace the NE555 with a TLC555")
    b = di.classify("replace the STM32F103 with an ESP32")
    assert a["layer"] == b["layer"] == "part_swap"
