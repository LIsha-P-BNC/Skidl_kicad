# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

from skidl.schematics.net_classify import (
    classify_net_role,
    is_connector_part,
    is_cyclic_bus_net,
    part_depth_map,
)


class FakePin:
    def __init__(self, part):
        self.part = part
        self.net = None


class FakePart:
    def __init__(self, name, ref_prefix="U", search_text="", keywords=""):
        self.name = name
        self.pins = []
        self.ref_prefix = ref_prefix
        self.search_text = search_text
        self.keywords = keywords

    def __repr__(self):
        return f"FakePart({self.name})"


class FakeNet:
    def __init__(self, name, drive=None):
        self.name = name
        self.pins = []
        self.drive = drive


def connect(net, *parts):
    """Attach one new pin per part to net, both directions."""
    for part in parts:
        pin = FakePin(part)
        pin.net = net
        part.pins.append(pin)
        net.pins.append(pin)


# --- classify_net_role -------------------------------------------------


def test_classify_net_role_ground_names():
    for name in ["GND", "gnd", "AGND", "DGND", "PGND", "VSS", "VEE", "GND1"]:
        assert classify_net_role(FakeNet(name)) == "ground", name


def test_classify_net_role_power_names():
    for name in ["VCC", "VDD", "+3.3V", "+5V", "VBAT", "AVDD", "DVCC"]:
        assert classify_net_role(FakeNet(name)) == "power", name


def test_classify_net_role_unrelated_name():
    assert classify_net_role(FakeNet("SPI_MOSI")) is None


def test_classify_net_role_drive_fallback():
    from skidl.pin import pin_drives

    net = FakeNet("MY_RAIL", drive=pin_drives.POWER)
    assert classify_net_role(net) == "power"


# --- is_cyclic_bus_net ---------------------------------------------------


def test_is_cyclic_bus_net_true_for_bidirectional_buses():
    for name in ["I2C_SDA", "SPI_MOSI", "CAN_H", "UART_TX", "SMBUS_CLK"]:
        assert is_cyclic_bus_net(FakeNet(name)), name


def test_is_cyclic_bus_net_false_for_plain_signal():
    assert not is_cyclic_bus_net(FakeNet("RESET"))


# --- is_connector_part ---------------------------------------------------


def test_is_connector_part_by_ref_prefix():
    assert is_connector_part(FakePart("header", ref_prefix="J"))
    assert is_connector_part(FakePart("header", ref_prefix="HDR"))


def test_is_connector_part_by_metadata_overrides_odd_prefix():
    part = FakePart("weird", ref_prefix="U", search_text="usb type-c connector")
    assert is_connector_part(part)


def test_is_connector_part_false_for_ic():
    part = FakePart("mcu", ref_prefix="U", search_text="32-bit microcontroller")
    assert not is_connector_part(part)


# --- part_depth_map --------------------------------------------------------


def test_part_depth_map_linear_chain_seeded_from_connector():
    conn = FakePart("J1", ref_prefix="J")
    mid = FakePart("U1", ref_prefix="U")
    leaf = FakePart("U2", ref_prefix="U")

    connect(FakeNet("SIG_A"), conn, mid)
    connect(FakeNet("SIG_B"), mid, leaf)

    depths = part_depth_map([conn, mid, leaf])
    assert depths[conn] == 0
    assert depths[mid] == 1
    assert depths[leaf] == 2


def test_part_depth_map_ground_net_does_not_collapse_depths():
    conn = FakePart("J1", ref_prefix="J")
    mid = FakePart("U1", ref_prefix="U")
    leaf = FakePart("U2", ref_prefix="U")

    connect(FakeNet("SIG_A"), conn, mid)
    connect(FakeNet("SIG_B"), mid, leaf)
    # All three also share a ground net -- this must NOT collapse them
    # all to the same depth via a direct conn<->leaf "shortcut" edge.
    connect(FakeNet("GND"), conn, mid, leaf)

    depths = part_depth_map([conn, mid, leaf])
    assert depths[conn] == 0
    assert depths[mid] == 1
    assert depths[leaf] == 2


def test_part_depth_map_cyclic_bus_does_not_stall():
    conn = FakePart("J1", ref_prefix="J")
    mcu = FakePart("U1", ref_prefix="U")
    sensor = FakePart("U2", ref_prefix="U")

    connect(FakeNet("SIG_A"), conn, mcu)
    connect(FakeNet("I2C_SDA"), mcu, sensor)
    # Feedback/interrupt line back from the sensor to the MCU -- a plain
    # BFS edge here would still be fine (BFS handles cycles), but this
    # net is deliberately excluded as an edge entirely, so the sensor's
    # depth comes only from the (also excluded from being a shortcut)
    # non-bus path through mcu below.
    connect(FakeNet("I2C_INT"), sensor, mcu)
    connect(FakeNet("SENSOR_TRIGGER"), mcu, sensor)

    depths = part_depth_map([conn, mcu, sensor])
    assert depths[conn] == 0
    assert depths[mcu] == 1
    assert depths[sensor] == 2


def test_part_depth_map_no_connectors_seeds_from_first_part():
    a = FakePart("U1", ref_prefix="U")
    b = FakePart("U2", ref_prefix="U")
    connect(FakeNet("SIG"), a, b)

    depths = part_depth_map([a, b])
    assert depths[a] == 0
    assert depths[b] == 1


def test_part_depth_map_isolated_part_defaults_to_zero():
    isolated = FakePart("U1", ref_prefix="U")
    depths = part_depth_map([isolated])
    assert depths[isolated] == 0
