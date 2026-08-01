"""Regression suite: the anvil schematic pipeline must produce a KiCad-ERC-clean,
connectivity-verified schematic for representative circuit shapes -- small to big.

Every class of bug found in the field gets a circuit here, so it can never
silently return:
  * simple 2-pin circuits            (wires mode, junction handling)
  * multi-unit ICs (dual op-amp)     (unit refs "U1" not "U1.uA"; unit 2/3
                                      sub-symbol style "_2_1" not "_2_2")
  * standard power rails             (auto power symbols + PWR_FLAG)
  * non-standard/switched rails      (PWR_FLAG label-fallback, e.g. "+15V_ON")

Each test builds a real .kicad_sch via smart_schematic.build() in a temp dir,
then asserts:
  1. drawn connectivity == intended netlist (verify_connectivity)
  2. kicad-cli sch erc reports ZERO errors  (ipc_check)

Skips cleanly on machines without the CAD app's kicad-cli.
"""
import os
import sys

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src"))
)

from skidl.anvil import anvil_libs  # noqa: F401  (sets KICAD*_SYMBOL_DIR)
import skidl
from skidl import KICAD9, Net, Part, set_default_tool

from skidl.anvil import smart_schematic  # noqa: E402
import ipc_check  # noqa: E402  (anvil helpers are on sys.path via skidl.anvil)
import verify_connectivity  # noqa: E402

pytestmark = pytest.mark.skipif(
    not verify_connectivity.KICAD_CLI,
    reason="kicad-cli (CAD app) not installed on this machine",
)


@pytest.fixture
def build_dir(request, monkeypatch):
    """Fresh circuit + working dir for every test.

    Must live on the SAME DRIVE as the repo (skidl computes relative paths),
    so pytest's C:-drive tmp_path can't be used on a repo checked out to F:.
    """
    import shutil

    d = os.path.join(os.path.dirname(__file__), ".pipeline_tmp", request.node.name)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    monkeypatch.chdir(d)
    skidl.reset()
    set_default_tool(KICAD9)
    yield d
    skidl.reset()


def _nc_unused_pins():
    """Give every unconnected pin a no-connect (same as generated scripts do)."""
    import builtins

    NC = builtins.NC  # skidl injects the no-connect net into builtins

    for part in builtins.default_circuit.parts:
        for pin in part.pins:
            try:
                if not pin.is_connected():
                    pin += NC
            except Exception:
                pass


def _build_and_check(name):
    """Build the current default circuit; assert verified + ERC error-free."""
    _nc_unused_pins()
    sch, _pro = smart_schematic.build(name=name)
    assert os.path.exists(sch), "schematic not produced"

    ok, msg, unwanted, missing = verify_connectivity.verify(name)
    assert ok, f"connectivity mismatch: {msg}\nunwanted={unwanted}\nmissing={missing}"

    result = ipc_check.check(name, verify_connectivity.KICAD_CLI)
    errs = result.get("erc_errors")
    assert errs is not None, "kicad-cli ERC did not run"
    assert errs == [], f"KiCad ERC errors: {errs}"
    return sch


def test_simple_led_circuit(build_dir):
    """Smallest case: rail -> resistor -> LED -> ground, all wires."""
    vcc, gnd = Net("+5V"), Net("GND")
    r = Part("Device", "R", value="1k")
    d = Part("Device", "LED", value="LED")
    vcc += r[1]
    r[2] += d[2]      # anode
    d[1] += gnd       # cathode
    _build_and_check("t_simple_led")


def test_multiunit_opamp(build_dir):
    """Dual op-amp: BOTH units + the hidden power unit must be drawn correctly.

    Regression for the two multi-unit writer bugs (2026-07-24):
      * unit Reference must be the parent's ("U1"), never "U1.uA"
      * unit sub-symbols must be NAME_<unit>_1 so units 2+ have pins
    """
    vcc, gnd = Net("+5V"), Net("GND")
    vin, mid, out = Net("VIN"), Net("A_OUT"), Net("B_OUT")

    r1 = Part("Device", "R", value="10k")
    r2 = Part("Device", "R", value="10k")
    u = Part("Amplifier_Operational", "LM2904", ref="U1")

    vcc += r1[1]
    r1[2] += vin
    vin += r2[1]
    r2[2] += gnd

    u[3] += vin      # A +in
    u[1] += mid      # A out
    u[2] += mid      # A -in (follower)
    u[5] += mid      # B +in
    u[7] += out      # B out
    u[6] += out      # B -in (follower)
    u[8] += vcc      # V+
    u[4] += gnd      # V-

    sch = _build_and_check("t_multiunit")

    text = open(sch, encoding="utf-8", errors="replace").read()
    assert '"U1.uA"' not in text, "unit written with its own ref instead of parent's"
    assert '"LM2904_2_1"' in text, "unit 2 sub-symbol must use body style 1"
    assert '"LM2904_2_2"' not in text, "style-2 sub-symbol is never rendered by KiCad"


def test_switched_rail_pwr_flag(build_dir):
    """A power-in pin on a NON-standard rail name ("+15V_ON") still gets a
    PWR_FLAG (attached at the net's label -- there is no power symbol for it)."""
    rail, gnd = Net("+15V_ON"), Net("GND")
    vin, out = Net("VIN"), Net("OUT")

    r1 = Part("Device", "R", value="10k")
    r2 = Part("Device", "R", value="10k")
    u = Part("Amplifier_Operational", "LM2904", ref="U1")

    rail += r1[1]
    r1[2] += vin
    vin += r2[1]
    r2[2] += gnd

    u[3] += vin
    u[1] += out
    u[2] += out
    u[8] += rail     # V+ on the switched rail
    u[4] += gnd

    sch = _build_and_check("t_switched_rail")
    text = open(sch, encoding="utf-8", errors="replace").read()
    assert "PWR_FLAG" in text, "switched rail must still receive a PWR_FLAG"
