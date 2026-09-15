# REGRESSION T3: @subcircuit pages + authored blocks (<50 parts -> flattened
# to one sheet with boxed blocks). Exercises: hiertuple in blk_ids, shared Net
# objects across pages, cross-page nets -> must LABEL (gate 3c), page-internal
# nets -> wire.
import os, sys
sys.path.insert(0, r"f:\Ki_CAD\skidl\src")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from skidl.anvil import anvil_libs  # noqa: F401
from skidl import *
from skidl.anvil import smart_schematic

set_default_tool(KICAD9)

vin = Net('+12V')
v33 = Net('+3.3V')
gnd = Net('GND')
en_sig = Net('LOAD_EN')


@subcircuit
def psu(vin, v33, gnd, en_sig):
    j1 = Part("Connector_Generic", "Conn_01x03", ref="J1", tag="J1", value="DC_IN",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
    j1[1] += vin
    j1[2] += en_sig      # enable line travels to the load page
    j1[3] += gnd
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


@subcircuit
def load(v33, gnd, en_sig):
    # repeated LED chains via a PLAIN helper (the repetition rule)
    def led_chain(idx, sig=None):
        r = Part("Device", "R", ref=f"R{idx}", tag=f"R{idx}", value="1k",
                 footprint="Resistor_SMD:R_0603_1608Metric")
        d = Part("Device", "LED", ref=f"D{idx}", tag=f"D{idx}", value="GREEN",
                 footprint="LED_SMD:LED_0603_1608Metric")
        r[1] += sig if sig is not None else v33
        r[2] += d[2]
        d[1] += gnd

    led_chain(1)
    led_chain(2)
    led_chain(3, sig=en_sig)   # EN-driven chain -> uses the cross-page net
    c3 = Part("Device", "C", ref="C3", tag="C3", value="100nF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c3[1] += v33
    c3[2] += gnd


psu(vin, v33, gnd, en_sig, tag="psu_pg")
load(v33, gnd, en_sig, tag="load_pg")

sch, pro = smart_schematic.build(rev="A", company="regr", engineer="T3")
