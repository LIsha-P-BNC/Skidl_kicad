# REGRESSION T4: 12V low-side MOSFET motor driver -- a COMPLETELY different
# circuit class (12V domain, 2A motor net, transistor + flyback) proving the
# engines are DYNAMIC. Component values are COMPUTED by design_calc at build
# time (not hand-typed), so the generated netlist itself proves
# "calculation padi correct-ah varudhu".
import os, sys
sys.path.insert(0, r"f:\Ki_CAD\skidl\src")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from skidl.anvil import anvil_libs  # noqa: F401  (must precede skidl)
from skidl import *
from skidl.anvil import smart_schematic
from skidl.anvil.design_calc import led_series_resistor

set_default_tool(KICAD9)


def _fmt_ohm(v):
    if v >= 1e6:
        return f"{v/1e6:g}M"
    if v >= 1e3:
        return f"{v/1e3:g}k"
    return f"{v:g}"


# --- CALCULATED value: 12 V RED indicator @ 10 mA (design_calc, not hand-math)
_led = led_series_resistor(12.0, i_ma=10, color="RED")
LED_R = _fmt_ohm(_led["r_standard_ohm"])
print(f">>> t4 calc: LED R = {LED_R} ({_led['formula']}; {_led['basis']})")

v12   = Net('+12V')
gnd   = Net('GND')
en    = Net('MOTOR_EN')
mot_a = Net('MOTOR_A')     # switched low side -- width engine: motor -> 2 A

with smart_schematic.block("MOTOR DRIVER"):
    j1 = Part("Connector_Generic", "Conn_01x02", ref="J1", tag="J1",
              value="12V_IN",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
    j1[1] += v12
    j1[2] += gnd
    j3 = Part("Connector_Generic", "Conn_01x02", ref="J3", tag="J3",
              value="CTRL_IN",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
    j3[1] += en
    j3[2] += gnd

    q1 = Part("Transistor_FET", "2N7002", ref="Q1", tag="Q1", value="2N7002",
              footprint="Package_TO_SOT_SMD:SOT-23")
    r1 = Part("Device", "R", ref="R1", tag="R1", value="100",
              footprint="Resistor_SMD:R_0603_1608Metric")
    r2 = Part("Device", "R", ref="R2", tag="R2", value="10k",
              footprint="Resistor_SMD:R_0603_1608Metric")
    gate = Net('Q1_GATE')
    r1[1] += en          # gate series R
    r1[2] += gate
    q1[1] += gate        # G
    q1[2] += gnd         # S
    q1[3] += mot_a       # D  (low-side switch)
    r2[1] += en          # gate pulldown -- FET off when ctrl floats
    r2[2] += gnd

    j2 = Part("Connector_Generic", "Conn_01x02", ref="J2", tag="J2",
              value="MOTOR_OUT",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
    j2[1] += v12         # motor high side
    j2[2] += mot_a       # motor low side -> drain
    d1 = Part("Device", "D", ref="D1", tag="D1", value="1N4148",
              footprint="Diode_SMD:D_SOD-123")
    d1[1] += v12         # K  -- flyback across the motor
    d1[2] += mot_a       # A

    r3 = Part("Device", "R", ref="R3", tag="R3", value=LED_R,
              footprint="Resistor_SMD:R_0603_1608Metric")
    d2 = Part("Device", "LED", ref="D2", tag="D2", value="RED",
              footprint="LED_SMD:LED_0805_2012Metric")
    r3[1] += v12
    r3[2] += d2[2]       # A
    d2[1] += gnd         # K

sch, pro = smart_schematic.build(rev="A", company="regr", engineer="T4")
