# REGRESSION T1: tiny FLAT ungrouped circuit (no blocks, no @subcircuit)
# -> exercises the ungrouped path: blk_ids collapse to ONE id, gates 3/3b/3c
#    must behave exactly as before (wires everywhere, no cross-block stubs).
import os, sys
sys.path.insert(0, r"f:\Ki_CAD\skidl\src")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from skidl.anvil import anvil_libs  # noqa: F401  (must precede skidl)
from skidl import *
from skidl.anvil import smart_schematic

set_default_tool(KICAD9)

v5 = Net('+5V')
gnd = Net('GND')

j1 = Part("Connector_Generic", "Conn_01x02", ref="J1", tag="J1", value="PWR_IN",
          footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
j1[1] += v5
j1[2] += gnd

r1 = Part("Device", "R", ref="R1", tag="R1", value="1k",
          footprint="Resistor_SMD:R_0603_1608Metric")
d1 = Part("Device", "LED", ref="D1", tag="D1", value="RED",
          footprint="LED_SMD:LED_0603_1608Metric")
r1[1] += v5
r1[2] += d1[2]
d1[1] += gnd

c1 = Part("Device", "C", ref="C1", tag="C1", value="100nF",
          footprint="Capacitor_SMD:C_0603_1608Metric")
c1[1] += v5
c1[2] += gnd

sch, pro = smart_schematic.build(rev="A", company="regr", engineer="T1")
