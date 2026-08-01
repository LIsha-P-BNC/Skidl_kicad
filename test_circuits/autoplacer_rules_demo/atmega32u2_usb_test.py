"""Autoplacer test circuit transcribed from the supplied ATmega32U2 image.

This deliberately keeps short, same-sheet connections as ordinary nets so
generate_schematic(auto_stub=True) can decide whether to draw a wire or label.
"""

from pathlib import Path

from skidl import *


set_default_tool(KICAD9)


# Named rails and signals make the generated schematic easy to inspect.
vcc = Net("+5V")
gnd = Net("GND")
xtal1 = Net("XTAL1")
xtal2 = Net("XTAL2")
usb_dp = Net("USB_DP")
usb_dm = Net("USB_DM")
reset_n = Net("RESET_N")
led_pc7 = Net("LED_PC7")


# ATmega32U2-A is the TQFP-32 library symbol corresponding to ATmega32U2-AU.
u1 = Part("MCU_Microchip_ATmega", "ATmega32U2-A", tag="u1")

# USB power and USB 2.0 data path.
j1 = Part("Connector", "USB_B", ref="J1", footprint="Connector_USB:USB_B_Molex_67068", tag="usb")
r1 = Part("Device", "R", value="22", footprint="Resistor_SMD:R_0603_1608Metric", tag="usb_dp_r")
r2 = Part("Device", "R", value="22", footprint="Resistor_SMD:R_0603_1608Metric", tag="usb_dm_r")
c4 = Part("Device", "C", value="10uF", footprint="Capacitor_SMD:C_0805_2012Metric", tag="usb_bulk")

j1["VBUS"] += vcc
j1["GND"] += gnd
j1["Shield"] += NC  # The supplied source shows only the four USB signal/power pins.
j1["D+"] & r1 & usb_dp & u1[29]
j1["D-"] & r2 & usb_dm & u1[30]
vcc & c4 & gnd

# Oscillator and the required UCAP capacitor shown in the source schematic.
y1 = Part("Device", "Crystal", ref="Q1", value="8MHz", footprint="Crystal_SMD:Crystal_SMD_3225-4Pin_3.2x2.5mm", tag="crystal")
c1 = Part("Device", "C", value="1uF", footprint="Capacitor_SMD:C_0603_1608Metric", tag="ucap")
c2 = Part("Device", "C", value="22pF", footprint="Capacitor_SMD:C_0603_1608Metric", tag="xtal2_load")
c3 = Part("Device", "C", value="22pF", footprint="Capacitor_SMD:C_0603_1608Metric", tag="xtal1_load")

u1[1] += xtal1
u1[2] += xtal2
xtal1 & y1[1]
xtal2 & y1[2]
xtal1 & c3 & gnd
xtal2 & c2 & gnd
u1[27] & c1 & gnd

# Reset pull-up and pushbutton.
r4 = Part("Device", "R", value="10k", footprint="Resistor_SMD:R_0603_1608Metric", tag="reset_pullup")
sw1 = Part("Switch", "SW_SPST", ref="S1", footprint="Button_Switch_THT:SW_PUSH_6mm_H5mm", tag="reset_switch")
u1[24] += reset_n
vcc & r4 & reset_n & sw1 & gnd

# PC7 status LED.
d1 = Part("Device", "LED", ref="LED1", value="LED", footprint="LED_SMD:LED_0603_1608Metric", tag="status_led")
r3 = Part("Device", "R", value="200", footprint="Resistor_SMD:R_0603_1608Metric", tag="status_led_r")
u1[22] & led_pc7 & d1[2]  # LED pin 2 is the anode; pin 1 is the cathode.
d1[1] & r3 & gnd

# 8-pin I/O headers from the source image.
j2 = Part("Connector_Generic", "Conn_01x08", ref="JP2", footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical", tag="port_b_header")
j3 = Part("Connector_Generic", "Conn_01x08", ref="JP3", footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical", tag="port_d_header")
for header_pin, mcu_pin in enumerate(range(14, 22), start=1):
    u1[mcu_pin] & j2[header_pin]
for header_pin, mcu_pin in enumerate(range(6, 14), start=1):
    u1[mcu_pin] & j3[header_pin]

# All three supply pins and both grounds are tied exactly as shown.
vcc += u1[4], u1[31], u1[32]
gnd += u1[3], u1[28]
for unused_pin in (5, 23, 25, 26):
    u1[unused_pin] += NC


if __name__ == "__main__":
    project_dir = Path(__file__).parent
    ERC()
    generate_netlist(file_=str(project_dir / "atmega32u2_usb_test.net"))
    generate_schematic(
        filepath=project_dir,
        top_name="atmega32u2_usb_test",
        title="ATmega32U2 USB Autoplacer Rules Test",
        flatness=1.0,
        auto_stub=True,
        auto_stub_fanout=8,
        auto_stub_max_wire_pins=3,
        auto_stub_far_label_dist=1500,
        auto_stub_fallback="labels",
    )
