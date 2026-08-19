# ============================================================================
#  SKiDL circuit TEMPLATE  (Anvil CAD)   --  copy this file, rename, fill in.
# ============================================================================
#  The OUTPUT files take THIS FILE'S name.  So:
#     copy circuit_template.py  ->  led_blinker.py   (whatever your project is)
#
#  WORKFLOW (this is how you keep name / pin / value / component CORRECT):
#     1) Find real parts:   python find_parts.py <word>        e.g.  resistor  / LED / lm2596
#     2) Check its pins:     python find_parts.py --show Lib Name   e.g.  --show Device R
#     3) Fill the circuit below using ONLY the Lib/Name/pins you saw.
#     4) Run it:             python led_blinker.py
#        -> makes  <name>.net + <name>.anvil_sch + <name>.anvil_pro  and opens Anvil CAD.
#     5) In Anvil CAD: open the schematic; for the board open Pcbnew ->
#        "Update PCB from Schematic" (F8)  (footprints are already assigned).
# ============================================================================

import os, sys
# Deterministic output (same schematic every run). Re-exec once with a fixed hash seed.
if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
os.chdir(os.path.dirname(os.path.abspath(__file__)))   # outputs land next to this file

# Engine ships WITH skidl (skidl.anvil) -> portable, multi-user, no local path.
# anvil_libs first: it sets KICAD*_SYMBOL_DIR (skidl reads it lazily at Part()-time).
from skidl.anvil import anvil_libs          # noqa: F401
from skidl import *
from skidl.anvil import smart_schematic, open_anvilcad

set_default_tool(KICAD9)

# ---------- 1) NETS (the wires / signals) ----------
vcc = Net("VCC")
gnd = Net("GND")

# ---------- 2) PARTS (components) ----------
# Rule for every part:  Part("Lib", "Name", ref=, tag=, value=, footprint=)
#   - "Lib","Name"  come from   python find_parts.py <word>     (never invent them!)
#   - ref / tag     = a stable ID like R1, U1  (stops "Missing tag" warnings)
#   - value         = 330, 10k, 4.7uF, ...     (leave out for ICs/LEDs)
#   - footprint     = "FpLib:FpName"           (REQUIRED so the PCB has a real part)
r = Part("Device", "R", ref="R1", tag="R1", value="330",
         footprint="Resistor_SMD:R_0805_2012Metric")
led = Part("Device", "LED", ref="D1", tag="D1",
           footprint="LED_SMD:LED_0805_2012Metric")

# ---------- 3) CONNECT ----------
# Two styles (mix freely):
#   series chain :  vcc & r & led & gnd
#   pin-by-pin   :  r[1] += vcc   ;   r[2] += led[1]   ;   led[2] += gnd
# Use the pin NUMBERS you confirmed with  find_parts.py --show .
vcc & r & led & gnd

# ---------- 4) GENERATE everything + OPEN in Anvil CAD ----------
# smart_schematic.build() runs ERC, writes <name>.net, <name>.anvil_sch and <name>.anvil_pro.
sch, pro = smart_schematic.build()
open_anvilcad.open_project(pro)      # <- comment this line out if you don't want the GUI to pop up
print("\n>>> DONE. netlist + schematic + project ready, Anvil CAD opening. <<<")
