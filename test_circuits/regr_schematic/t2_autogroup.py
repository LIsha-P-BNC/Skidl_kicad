# REGRESSION T2: medium FLAT script, NO authored blocks -> auto-grouping path.
# Two regulator stages + LED loads + input connector. Some parts end up with
# .group tags (clusters), connector/others may stay group=None -> exercises the
# MIXED grouped/ungrouped blk_ids edge in gates 3/3b/3c.
import os, sys
sys.path.insert(0, r"f:\Ki_CAD\skidl\src")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from skidl.anvil import anvil_libs  # noqa: F401
from skidl import *
from skidl.anvil import smart_schematic

set_default_tool(KICAD9)

vin = Net('+12V')
gnd = Net('GND')
v33 = Net('+3.3V')
vreg2 = Net('VREG2_OUT')

j1 = Part("Connector_Generic", "Conn_01x02", ref="J1", tag="J1", value="DC_IN",
          footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
j1[1] += vin
j1[2] += gnd

u1 = Part("UserParts", "AMS1117-3.3", ref="U1", tag="U1", value="AMS1117-3.3",
          footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2")
u1[3] += vin
u1[2] += v33
u1[1] += gnd
c1 = Part("Device", "C", ref="C1", tag="C1", value="10uF",
          footprint="Capacitor_SMD:C_0805_2012Metric")
c1[1] += vin
c1[2] += gnd
c2 = Part("Device", "C", ref="C2", tag="C2", value="22uF",
          footprint="Capacitor_SMD:C_0805_2012Metric")
c2[1] += v33
c2[2] += gnd
r1 = Part("Device", "R", ref="R1", tag="R1", value="1k",
          footprint="Resistor_SMD:R_0603_1608Metric")
d1 = Part("Device", "LED", ref="D1", tag="D1", value="GREEN",
          footprint="LED_SMD:LED_0603_1608Metric")
r1[1] += v33
r1[2] += d1[2]
d1[1] += gnd

u2 = Part("UserParts", "AMS1117-3.3", ref="U2", tag="U2", value="AMS1117-3.3",
          footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2")
u2[3] += vin
u2[2] += vreg2
u2[1] += gnd
c3 = Part("Device", "C", ref="C3", tag="C3", value="10uF",
          footprint="Capacitor_SMD:C_0805_2012Metric")
c3[1] += vin
c3[2] += gnd
c4 = Part("Device", "C", ref="C4", tag="C4", value="22uF",
          footprint="Capacitor_SMD:C_0805_2012Metric")
c4[1] += vreg2
c4[2] += gnd
r2 = Part("Device", "R", ref="R2", tag="R2", value="1k",
          footprint="Resistor_SMD:R_0603_1608Metric")
d2 = Part("Device", "LED", ref="D2", tag="D2", value="BLUE",
          footprint="LED_SMD:LED_0603_1608Metric")
r2[1] += vreg2
r2[2] += d2[2]
d2[1] += gnd

sch, pro = smart_schematic.build(rev="A", company="regr", engineer="T2")
