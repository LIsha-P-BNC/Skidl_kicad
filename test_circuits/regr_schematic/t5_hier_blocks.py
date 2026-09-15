# REGRESSION T5: HIERARCHY pages that each CONTAIN authored blocks.
# Verifies the user's rule at every level: each child sheet must render its
# blocks as boxed sections whose INTERNAL connections are WIRES (ladder rails
# per block), labels only between blocks / across sheets.
# Run with SKIDL_HIER_MIN_PARTS=5 to force one sheet per page.
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
sig = Net('STATUS_SIG')   # cross-page net -> must become a port/label


@subcircuit
def power_pg(v5, v33, gnd, sig):
    with smart_schematic.block("5V INPUT"):
        j1 = Part("Connector_Generic", "Conn_01x02", ref="J1", tag="J1",
                  value="5V_IN",
                  footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
        j1[1] += v5
        j1[2] += gnd
        c1 = Part("Device", "CP", ref="C1", tag="C1", value="22uF/16V",
                  footprint="Capacitor_SMD:CP_Elec_6.3x7.7")
        c1[1] += v5
        c1[2] += gnd
    with smart_schematic.block("3V3 REG"):
        u1 = Part("UserParts", "AMS1117-3.3", ref="U1", tag="U1",
                  value="AMS1117-3.3",
                  footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2")
        u1[3] += v5
        u1[2] += v33
        u1[1] += gnd
        c2 = Part("Device", "C", ref="C2", tag="C2", value="100nF/50V",
                  footprint="Capacitor_SMD:C_0603_1608Metric")
        c2[1] += v5
        c2[2] += gnd
        c3 = Part("Device", "CP", ref="C3", tag="C3", value="22uF/16V",
                  footprint="Capacitor_SMD:CP_Elec_6.3x7.7")
        c3[1] += v33
        c3[2] += gnd
        r1 = Part("Device", "R", ref="R1", tag="R1", value="10k",
                  footprint="Resistor_SMD:R_0603_1608Metric")
        r1[1] += v33
        r1[2] += sig          # pulled-up status line leaves this page


@subcircuit
def io_pg(v33, gnd, sig):
    with smart_schematic.block("STATUS LED"):
        r2 = Part("Device", "R", ref="R2", tag="R2", value="300",
                  footprint="Resistor_SMD:R_0603_1608Metric")
        d1 = Part("Device", "LED", ref="D1", tag="D1", value="GREEN",
                  footprint="LED_SMD:LED_0805_2012Metric")
        r2[1] += sig          # driven from the power page
        r2[2] += d1[2]
        d1[1] += gnd
    with smart_schematic.block("3V3 OUT"):
        j2 = Part("Connector_Generic", "Conn_01x03", ref="J2", tag="J2",
                  value="3V3_OUT",
                  footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
        j2[1] += v33
        j2[2] += sig
        j2[3] += gnd
        c4 = Part("Device", "C", ref="C4", tag="C4", value="100nF/50V",
                  footprint="Capacitor_SMD:C_0603_1608Metric")
        c4[1] += v33
        c4[2] += gnd


power_pg(v5, v33, gnd, sig, tag="power_pg")
io_pg(v33, gnd, sig, tag="io_pg")

sch, pro = smart_schematic.build(rev="A", company="BNC Energy", engineer="Anvil AI")
