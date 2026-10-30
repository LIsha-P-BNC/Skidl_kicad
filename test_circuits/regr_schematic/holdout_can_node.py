# HOLDOUT: CAN sensor node -- UNSEEN topology for determinism/grid validation.
# Never used during engine development (WI-1..WI-5 tuning): proves the fixes
# are dynamic, not corpus-fitted. Star/hub topology (MCP2515 = anchor hub:
# SPI host + CAN PHY + crystal; LM75B on I2C) with 5 authored blocks -- the
# shape the 2D block-grid targets, NOT a 1-D chain.
# All parts/pins grounded against the real libraries (pins confirmed by
# instantiation): MCP2515-IST (VDD20/VSS10/SCK14/SI16/SO17/CS18/INT13/
# TXCAN1/RXCAN2/OSC1-9/OSC2-8/RESET19), MCP2551-I-SN (TXD1/VSS2/VDD3/RXD4/
# Vref5/CANL6/CANH7/Rs8), LM75B (SDA1/SCL2/OS3/GND4/A2-5/A1-6/A0-7/+Vs8),
# L7805 (IN1/GND2/OUT3), Device:Crystal (1/2).
import os, sys
sys.path.insert(0, r"f:\Ki_CAD\skidl\src")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from skidl.anvil import anvil_libs  # noqa: F401
from skidl import *
from skidl.anvil import smart_schematic

set_default_tool(KICAD9)

# Shared nets -- created ONCE at module level (spec section 9.1).
v12 = Net('+12V')
v5 = Net('+5V')
gnd = Net('GND')
spi_sck = Net('SPI_SCK')
spi_mosi = Net('SPI_MOSI')
spi_miso = Net('SPI_MISO')
can_cs_n = Net('CAN_CS_N')
can_int_n = Net('CAN_INT_N')
can_tx = Net('CAN_TX')
can_rx = Net('CAN_RX')
can_h = Net('CAN_H')
can_l = Net('CAN_L')
i2c_sda = Net('I2C_SDA')
i2c_scl = Net('I2C_SCL')

with smart_schematic.block("POWER INPUT"):
    j1 = Part("Connector_Generic", "Conn_01x02", ref="J1", tag="J1",
              value="DC_12V_IN",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
    j1[1] += v12
    j1[2] += gnd
    c1 = Part("Device", "C", ref="C1", tag="C1", value="330nF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c1[1] += v12
    c1[2] += gnd
    u1 = Part("Regulator_Linear", "L7805", ref="U1", tag="U1", value="L7805",
              footprint="Package_TO_SOT_THT:TO-220-3_Vertical")
    u1['IN'] += v12
    u1['GND'] += gnd
    u1['OUT'] += v5
    c2 = Part("Device", "C", ref="C2", tag="C2", value="100nF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c2[1] += v5
    c2[2] += gnd
    r5 = Part("Device", "R", ref="R5", tag="R5", value="1k",
              footprint="Resistor_SMD:R_0603_1608Metric")
    d1 = Part("Device", "LED", ref="D1", tag="D1", value="GREEN",
              footprint="LED_SMD:LED_0603_1608Metric")
    r5[1] += v5
    r5[2] += d1[2]        # LED pin 2 = anode
    d1[1] += gnd          # LED pin 1 = cathode

with smart_schematic.block("CAN CONTROLLER"):
    u2 = Part("Interface_CAN_LIN", "MCP2515-IST", ref="U2", tag="U2",
              value="MCP2515", footprint="Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm")
    u2[20] += v5          # VDD
    u2[10] += gnd         # VSS
    u2[14] += spi_sck
    u2[16] += spi_mosi    # SI
    u2[17] += spi_miso    # SO
    u2[18] += can_cs_n    # ~CS
    u2[13] += can_int_n   # ~INT
    u2[1] += can_tx       # TXCAN
    u2[2] += can_rx       # RXCAN
    r1 = Part("Device", "R", ref="R1", tag="R1", value="10k",
              footprint="Resistor_SMD:R_0603_1608Metric")
    r1[1] += v5
    r1[2] += u2[19]       # ~RESET pull-up
    y1 = Part("Device", "Crystal", ref="Y1", tag="Y1", value="16MHz",
              footprint="Crystal:Crystal_HC49-4H_Vertical")
    osc1 = Net('OSC1')
    osc2 = Net('OSC2')
    y1[1] += osc1
    y1[2] += osc2
    u2[9] += osc1         # OSC1
    u2[8] += osc2         # OSC2
    c3 = Part("Device", "C", ref="C3", tag="C3", value="22pF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c3[1] += osc1
    c3[2] += gnd
    c4 = Part("Device", "C", ref="C4", tag="C4", value="22pF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c4[1] += osc2
    c4[2] += gnd
    c5 = Part("Device", "C", ref="C5", tag="C5", value="100nF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c5[1] += v5           # decap at U2 VDD
    c5[2] += gnd
    # Unused controller pins: RTS inputs have internal pull-ups; BF/CLKOUT
    # are outputs -- explicit no-connects so nothing floats silently.
    u2[3] += NC           # CLKOUT/SOF
    u2[4] += NC           # ~TX0RTS
    u2[5] += NC           # ~TX1RTS
    u2[7] += NC           # ~TX2RTS
    u2[11] += NC          # ~RX1BF
    u2[12] += NC          # ~RX0BF
    u2[6] += NC           # NC
    u2[15] += NC          # NC

with smart_schematic.block("CAN TRANSCEIVER"):
    u3 = Part("Interface_CAN_LIN", "MCP2551-I-SN", ref="U3", tag="U3",
              value="MCP2551", footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    u3[1] += can_tx       # TXD
    u3[4] += can_rx       # RXD
    u3[3] += v5           # VDD
    u3[2] += gnd          # VSS
    u3[7] += can_h        # CANH
    u3[6] += can_l        # CANL
    r2 = Part("Device", "R", ref="R2", tag="R2", value="10k",
              footprint="Resistor_SMD:R_0603_1608Metric")
    r2[1] += u3[8]        # Rs slope control
    r2[2] += gnd
    u3[5] += NC           # Vref unused
    c6 = Part("Device", "C", ref="C6", tag="C6", value="100nF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c6[1] += v5
    c6[2] += gnd
    r3 = Part("Device", "R", ref="R3", tag="R3", value="120R",
              footprint="Resistor_SMD:R_0603_1608Metric")
    r3[1] += can_h        # bus termination
    r3[2] += can_l
    j2 = Part("Connector_Generic", "Conn_01x03", ref="J2", tag="J2",
              value="CAN_BUS",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical")
    j2[1] += can_h
    j2[2] += can_l
    j2[3] += gnd

with smart_schematic.block("TEMP SENSOR"):
    u4 = Part("Sensor_Temperature", "LM75B", ref="U4", tag="U4", value="LM75B",
              footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")
    u4[8] += v5           # +Vs
    u4[4] += gnd
    u4[1] += i2c_sda
    u4[2] += i2c_scl
    u4[7] += gnd          # A0: address 0
    u4[6] += gnd          # A1
    u4[5] += gnd          # A2
    u4[3] += NC           # O.S. unused
    c7 = Part("Device", "C", ref="C7", tag="C7", value="100nF",
              footprint="Capacitor_SMD:C_0603_1608Metric")
    c7[1] += v5
    c7[2] += gnd
    r6 = Part("Device", "R", ref="R6", tag="R6", value="4.7k",
              footprint="Resistor_SMD:R_0603_1608Metric")
    r6[1] += v5
    r6[2] += i2c_sda
    r7 = Part("Device", "R", ref="R7", tag="R7", value="4.7k",
              footprint="Resistor_SMD:R_0603_1608Metric")
    r7[1] += v5
    r7[2] += i2c_scl

with smart_schematic.block("HOST INTERFACE"):
    j3 = Part("Connector_Generic", "Conn_01x08", ref="J3", tag="J3",
              value="HOST_SPI_I2C",
              footprint="Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical")
    j3[1] += spi_sck
    j3[2] += spi_mosi
    j3[3] += spi_miso
    j3[4] += can_cs_n
    j3[5] += can_int_n
    j3[6] += i2c_sda
    j3[7] += i2c_scl
    j3[8] += gnd

sch, pro = smart_schematic.build(rev="A", company="regr", engineer="HOLDOUT")
