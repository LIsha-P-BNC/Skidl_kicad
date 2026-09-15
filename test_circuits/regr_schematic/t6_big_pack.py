# REGRESSION T6: BIG flat multi-block design (56 parts, 8 blocks, no
# @subcircuit) -> the SHEET PACKER must split it into multiple sheets by the
# per-sheet fill budget (whole blocks, flow order), with ports for cross-sheet
# nets and every page rendering its blocks as boxed sections. ERC must be 0.
import os, sys
sys.path.insert(0, r"f:\Ki_CAD\skidl\src")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from skidl.anvil import anvil_libs  # noqa: F401
from skidl import *
from skidl.anvil import smart_schematic

set_default_tool(KICAD9)

v5 = Net('+5V')
v33 = Net('+3V3')
gnd = Net('GND')

with smart_schematic.block("POWER IN"):
    j0 = Part("Connector_Generic", "Conn_01x02", ref="J0", value="5V_IN",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
    j0[1] += v5
    j0[2] += gnd
    cp0 = Part("Device", "CP", ref="C90", value="22uF/16V",
               footprint="Capacitor_SMD:CP_Elec_6.3x7.7")
    cp0[1] += v5
    cp0[2] += gnd
    u0 = Part("UserParts", "AMS1117-3.3", ref="U0", value="AMS1117-3.3",
              footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2")
    u0[3] += v5
    u0[2] += v33
    u0[1] += gnd
    c91 = Part("Device", "C", ref="C91", value="100nF/50V",
               footprint="Capacitor_SMD:C_0603_1608Metric")
    c91[1] += v5
    c91[2] += gnd
    c92 = Part("Device", "CP", ref="C92", value="22uF/16V",
               footprint="Capacitor_SMD:CP_Elec_6.3x7.7")
    c92[1] += v33
    c92[2] += gnd
    r90 = Part("Device", "R", ref="R90", value="300",
               footprint="Resistor_SMD:R_0603_1608Metric")
    d90 = Part("Device", "LED", ref="D90", value="GREEN",
               footprint="LED_SMD:LED_0805_2012Metric")
    r90[1] += v33
    r90[2] += d90[2]
    d90[1] += gnd


def channel(n):
    """One input-conditioning channel: connector -> RC filter -> LED indicator
    + pull-up + decap. A complete small function, 7 parts."""
    sig_in = Net(f'CH{n}_IN')
    sig = Net(f'CH{n}_SIG')
    with smart_schematic.block(f"CHANNEL {n}"):
        j = Part("Connector_Generic", "Conn_01x02", ref=f"J{n}", value=f"CH{n}",
                 footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
        j[1] += sig_in
        j[2] += gnd
        rs = Part("Device", "R", ref=f"R{n}0", value="1k",
                  footprint="Resistor_SMD:R_0603_1608Metric")
        rs[1] += sig_in
        rs[2] += sig
        cf = Part("Device", "C", ref=f"C{n}0", value="100nF/50V",
                  footprint="Capacitor_SMD:C_0603_1608Metric")
        cf[1] += sig
        cf[2] += gnd
        rp = Part("Device", "R", ref=f"R{n}1", value="10k",
                  footprint="Resistor_SMD:R_0603_1608Metric")
        rp[1] += v33
        rp[2] += sig
        ri = Part("Device", "R", ref=f"R{n}2", value="300",
                  footprint="Resistor_SMD:R_0603_1608Metric")
        di = Part("Device", "LED", ref=f"D{n}", value="GREEN",
                  footprint="LED_SMD:LED_0805_2012Metric")
        ri[1] += sig
        ri[2] += di[2]
        di[1] += gnd
        cd = Part("Device", "C", ref=f"C{n}1", value="100nF/50V",
                  footprint="Capacitor_SMD:C_0603_1608Metric")
        cd[1] += v33
        cd[2] += gnd


for n in range(1, 8):
    channel(n)

sch, pro = smart_schematic.build(rev="A", company="BNC Energy", engineer="Anvil AI")
