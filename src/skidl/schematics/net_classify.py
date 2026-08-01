# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Net/part role classification for schematic placement.

Kept separate from place.py (which is tool-agnostic and shared by every
KiCad version) and from any single tools/kicadN/gen_schematic.py (which
each already duplicate a power-net regex for their own label/power-symbol
rendering). These are pure functions over Net/Part objects with no
dependency on the live force-directed solver, so they're independently
testable and reusable by both placement (net_classify) and clustering
(cluster.py, which builds on top of these).
"""

import re
from collections import deque

from skidl.utilities import export_to_all

# Ground-like net names bias toward the bottom of a placement.
_GROUND_NET_RE = re.compile(
    r"^(GND|AGND|DGND|PGND|VSS|VEE)\d*$",
    re.IGNORECASE,
)

# Power-rail-like net names bias toward the top of a placement.
_POWER_RAIL_NET_RE = re.compile(
    r"^(\+\d[\d.]*V[\d]*|VCC|VDD|VBUS|VBAT|AVCC|AVDD|DVCC|DVDD)\d*$",
    re.IGNORECASE,
)

# Net-name keywords for common bidirectional/feedback buses. These are
# excluded as BFS edges in part_depth_map() since they routinely form
# cycles (e.g. an I2C sensor driving an interrupt back to the MCU that
# configured it), which would otherwise stall a plain BFS depth
# assignment or produce a meaningless "signal flow direction".
_CYCLIC_BUS_NET_RE = re.compile(
    r"(I2C|IIC|SPI|CAN|UART|USART|RS485|RS232|SMBUS|ONEWIRE|1WIRE)",
    re.IGNORECASE,
)

# Ref-prefixes recognized as connectors, beyond just J/P/CN -- library
# authors use a wide variety (crystals sometimes use X too, so it's only
# consulted as a fallback below metadata matching).
_CONNECTOR_REF_PREFIXES = frozenset(
    {"J", "P", "CN", "X", "K", "TB", "USB", "RJ45", "HDR"}
)

# Net-name patterns for FUNCTIONAL categories that a professional designer
# almost always draws as a WIRE (never a label), regardless of distance.
# See classify_net_function(). Kept deliberately specific so an ordinary
# GPIO/comms net is NOT swept up (e.g. a bare "CLK" is too ambiguous -- it
# could be an SPI clock that belongs on a labelled bus -- so only crystal
# oscillator pins / explicit XTAL/OSC names count as a clock here).
_CLOCK_NET_RE = re.compile(
    r"(XTAL|OSC(IN|OUT)?|OSC\d|MCLK|CLKIN|CLKOUT)", re.IGNORECASE
)
_CLOCK_PART_NAME_RE = re.compile(
    r"(crystal|oscillator|resonator|ceramic)", re.IGNORECASE
)
_RESET_NET_RE = re.compile(
    r"(RESET|NRST|MCLR|POR|(^|_)~?RST_?N?(\d*)(_|$))", re.IGNORECASE
)
# USB differential pair: USB_DP/USB_DM (+ _CONN variants), bare D+/D-, DP/DM.
_USB_DIFF_NET_RE = re.compile(
    r"(USB.*D[PM]|(^|_)D[PM](_|\d|$)|D\+|D-|DPLUS|DMINUS)", re.IGNORECASE
)
# RF feed / antenna nets: must stay short; treated near-mechanically.
_RF_NET_RE = re.compile(
    r"((^|_)ANT(ENNA)?(_|\d|$)|(^|_)RF(_|\d|$)|_FEED$|(^|_)FEED(_|$)"
    r"|GHZ|2G4|_24G|LORA|GNSS_?(ANT|IN)|GPS_?(ANT|IN)|GSM_?(ANT|IN))",
    re.IGNORECASE)
_RF_PART_NAME_RE = re.compile(
    r"(antenna|balun|u\.?fl|ipex|sma\b|rf.?switch|lna\b|saw.?filter)",
    re.IGNORECASE)


@export_to_all
def classify_net_role(net):
    """
    Classify a net's role for placement biasing.

    Args:
        net: Net to classify.

    Returns:
        str or None: "power" (bias toward top), "ground" (bias toward
        bottom), or None if the net isn't recognized as either.
    """
    name = getattr(net, "name", "") or ""

    if _GROUND_NET_RE.match(name):
        return "ground"
    if _POWER_RAIL_NET_RE.match(name) or name.startswith("+"):
        return "power"

    # Fall back to the net's drive strength for unnamed/oddly-named power
    # nets (e.g. net.drive set explicitly to POWER by user code).
    from skidl.pin import pin_drives

    if getattr(net, "drive", None) == pin_drives.POWER:
        return "power"

    return None


@export_to_all
def is_rf_part(part) -> bool:
    """True for antenna/RF-path parts (name/keyword/ref based, honest --
    a manual mechanical.keepouts entry is the override when detection
    misses). AE is the IEC ref prefix for antennas."""
    prefix = (getattr(part, "ref_prefix", "") or "").upper()
    if prefix in ("AE", "ANT"):
        return True
    hay = " ".join(str(getattr(part, f, "") or "") for f in
                   ("name", "value", "search_text", "keywords"))
    return bool(_RF_PART_NAME_RE.search(hay))


def _touches_rf_part(net):
    for pin in getattr(net, "pins", []):
        part = getattr(pin, "part", None)
        if part is not None and is_rf_part(part):
            return True
    return False


def _touches_clock_part(net):
    """True if any pin on the net belongs to a crystal / oscillator part."""
    for pin in getattr(net, "pins", []):
        part = getattr(pin, "part", None)
        if part is None:
            continue
        if _CLOCK_PART_NAME_RE.search(str(getattr(part, "name", "") or "")):
            return True
        if (getattr(part, "ref_prefix", "") or "").upper() == "Y":
            return True
    return False


def _is_decoupling_net(net):
    """True if the net links a decoupling cap to an IC (rare on a non-power
    net -- most decaps sit on VCC/GND, which classify as 'power' first)."""
    try:
        from skidl.schematics.cluster import is_decoupling_cap
    except Exception:
        return False
    has_decap = False
    has_ic = False
    for pin in getattr(net, "pins", []):
        part = getattr(pin, "part", None)
        if part is None:
            continue
        if is_decoupling_cap(part):
            has_decap = True
        elif (getattr(part, "ref_prefix", "") or "").upper() in ("U", "Q"):
            has_ic = True
    return has_decap and has_ic


@export_to_all
def classify_net_function(net):
    """
    Classify a net's FUNCTIONAL category for the wire-vs-label decision.

    A professional designer draws certain functional nets as WIRES no matter
    the distance -- the local circuitry around them must read as directly
    connected. This returns the category so the label engine can force a wire.

    Returns one of:
        "power"       -- a power/ground rail (normally already a power symbol)
        "usb_diff"    -- a USB D+/D- differential-pair net
        "clock"       -- a crystal/oscillator net (XTAL/OSC or a crystal pin)
        "reset"       -- a reset/NRST/MCLR net
        "decoupling"  -- a decoupling-cap-to-IC link (uncommon on a signal net)
        None          -- an ordinary signal net (decide by distance/fanout/etc.)

    DYNAMIC: pure net/part inspection (connected-part type + net-name keyword),
    no per-circuit hardcoding.
    """
    if classify_net_role(net) is not None:
        return "power"

    name = getattr(net, "name", "") or ""

    # RF before clock: an "RF_CLK" style name is deliberately RF here --
    # the feed/antenna net must stay short above all else.
    if _RF_NET_RE.search(name) or _touches_rf_part(net):
        return "rf"
    if _USB_DIFF_NET_RE.search(name):
        return "usb_diff"
    if _CLOCK_NET_RE.search(name) or _touches_clock_part(net):
        return "clock"
    if _RESET_NET_RE.search(name):
        return "reset"
    if _is_decoupling_net(net):
        return "decoupling"
    return None


# Per-class placement attraction weights: how strongly a net PULLS its
# parts together during board placement. Clocks/RF must be short (huge
# weight); ground is plane-served on multilayer and must never drag
# placement; LEDs are cosmetic. Used by board/place/global_opt.
NET_PLACEMENT_WEIGHTS = {
    "rf": 10.0,
    "clock": 8.0,
    "usb_diff": 6.0,
    "decoupling": 6.0,
    "bus": 3.0,
    "reset": 2.0,
    "power": 1.5,
    "gpio": 1.0,       # the default for ordinary signals
    "led": 0.3,
    "ground": 0.0,
}

_LED_NET_RE = re.compile(r"(?:^|_)LEDS?(?:_|$)|_LED$|^LED", re.I)


@export_to_all
def placement_weight(net) -> float:
    """Attraction weight of a net for placement optimization (see
    NET_PLACEMENT_WEIGHTS). Ground is 0 -- served by planes."""
    if classify_net_role(net) == "ground":
        return NET_PLACEMENT_WEIGHTS["ground"]
    fn = classify_net_function(net)
    if fn in NET_PLACEMENT_WEIGHTS:
        return NET_PLACEMENT_WEIGHTS[fn]
    name = getattr(net, "name", "") or ""
    if _LED_NET_RE.search(name):
        return NET_PLACEMENT_WEIGHTS["led"]
    if is_cyclic_bus_net(net):
        return NET_PLACEMENT_WEIGHTS["bus"]
    return NET_PLACEMENT_WEIGHTS["gpio"]


# Functional categories that must ALWAYS render as a wire (never a label).
_ALWAYS_WIRE_FUNCTIONS = frozenset({"clock", "reset", "decoupling", "usb_diff"})


@export_to_all
def is_always_wire_net(net):
    """True if the net's function (clock/reset/decoupling/usb_diff) means it
    should be drawn as a WIRE regardless of distance/fanout (power is handled
    separately as a symbol)."""
    return classify_net_function(net) in _ALWAYS_WIRE_FUNCTIONS


@export_to_all
def is_system_wide_net_name(name):
    """
    True if a net NAME denotes a design-wide signal that a professional
    schematic ties together with a GLOBAL label wherever it is labelled --
    a power/ground rail, a comms bus (I2C/SPI/UART/CAN/...), or a reset.

    Pure string test (no live Net object needed) so a post-processor that
    only sees net names (e.g. tools/inject_labels) can share the same rule
    as the live generator.
    """
    name = name or ""
    if _POWER_RAIL_NET_RE.match(name) or _GROUND_NET_RE.match(name) or name.startswith("+"):
        return True
    if _CYCLIC_BUS_NET_RE.search(name):
        return True
    if _RESET_NET_RE.search(name):
        return True
    return False


def _hier_modules(net):
    """Set of distinct hierarchy paths touched by the net's REAL pins.

    NetTerminal pins (synthetic label-only parts, ref-prefix NT) and any
    part without a resolvable hierarchy are skipped, so the result reflects
    only real circuit parts. Used to tell a single-sheet net from a
    cross-sheet one (see classify_label_scope)."""
    modules = set()
    for pin in getattr(net, "pins", []):
        part = getattr(pin, "part", None)
        if part is None:
            continue
        if (getattr(part, "ref_prefix", "") or "").upper() == "NT":
            continue
        try:
            ht = part.hiertuple
        except Exception:
            continue
        if ht is not None:
            modules.add(tuple(ht))
    return modules


@export_to_all
def classify_label_scope(net):
    """
    Decide whether a net rendered as a LABEL should be a GLOBAL label
    (design-wide) or a LOCAL label (this sheet only).

    A signal confined to one sheet uses a LOCAL label. A signal that crosses
    sheets uses a GLOBAL label so every occurrence ties together. Power nets
    are normally emitted as KiCad power symbols before this decision is made.
    Keeping ordinary same-sheet labels local prevents accidental connections
    between unrelated sheets that happen to reuse a signal name.

    Returns:
        str: "global" when real pins span hierarchy modules; otherwise
        "local".

    DYNAMIC: pure net/part inspection (net name + connected-part hierarchy),
    no per-circuit hardcoding.
    """
    # Only a net whose real pins span more than one hierarchy module leaves
    # this sheet. Every other label stays local, including a one-pin label.
    if len(_hier_modules(net)) > 1:
        return "global"

    return "local"


@export_to_all
def is_cyclic_bus_net(net):
    """
    Return True if a net's name suggests a bidirectional/feedback bus
    (I2C, SPI, CAN, UART, ...) that routinely forms cycles in the
    part-connectivity graph.

    Args:
        net: Net to check.

    Returns:
        bool: True if the net looks like a cyclic/bidirectional bus.
    """
    name = getattr(net, "name", "") or ""
    return bool(_CYCLIC_BUS_NET_RE.search(name))


@export_to_all
def is_connector_part(part):
    """
    Return True if a part looks like a physical connector.

    Metadata (the part's search_text -- filename + symbol name +
    description + KiCad keywords, already parsed by each tool's
    lib.py) takes priority over ref-prefix guessing since it's reliable
    across third-party libraries that don't follow the J/P/CN convention.

    Args:
        part: Part to check.

    Returns:
        bool: True if the part is classified as a connector.
    """
    search_text = getattr(part, "search_text", "") or ""
    if "connector" in search_text.lower():
        return True

    keywords = getattr(part, "keywords", "") or ""
    if "connector" in keywords.lower():
        return True

    ref_prefix = (getattr(part, "ref_prefix", "") or "").upper()
    return ref_prefix in _CONNECTOR_REF_PREFIXES


def _net_adjacency(parts):
    """Build a part -> set(part) adjacency map from shared, non-cyclic,
    non-power/ground nets. Internal helper for part_depth_map()."""

    adjacency = {part: set() for part in parts}
    part_set = set(parts)

    nets_seen = set()
    for part in parts:
        for pin in getattr(part, "pins", []):
            net = getattr(pin, "net", None)
            if net is None or id(net) in nets_seen:
                continue
            nets_seen.add(id(net))

            # Power/ground nets are excluded as BFS edges: a shared GND
            # net would otherwise collapse every part in the circuit to
            # the same depth.
            if classify_net_role(net) is not None:
                continue
            # Cyclic/bidirectional buses are excluded as edges too, so
            # they can't create false cycles that stall depth assignment.
            if is_cyclic_bus_net(net):
                continue

            net_parts = {p.part for p in net.pins if p.part in part_set}
            for a in net_parts:
                for b in net_parts:
                    if a is not b:
                        adjacency[a].add(b)

    return adjacency


@export_to_all
def part_depth_map(parts):
    """
    Compute a left-to-right signal-flow depth for each part via BFS over
    the net-adjacency graph, seeded from connector parts (or, if there
    are none, from the earliest-created part as a proxy for "input side").

    Power/ground nets and cyclic/bidirectional buses (I2C, SPI, CAN, ...)
    are excluded as BFS edges (see _net_adjacency()) so a shared ground
    plane or a feedback/interrupt line can't collapse the graph to one
    depth or create a cycle that stalls the traversal.

    Args:
        parts (list): Parts to compute depths for.

    Returns:
        dict: Mapping of part -> non-negative integer depth. Parts
        unreachable from any seed (isolated components) get depth 0.
    """
    parts = list(parts)
    adjacency = _net_adjacency(parts)

    seeds = [part for part in parts if is_connector_part(part)]
    if not seeds and parts:
        seeds = [parts[0]]

    depth = {}
    queue = deque()
    for seed in seeds:
        if seed not in depth:
            depth[seed] = 0
            queue.append(seed)

    while queue:
        part = queue.popleft()
        for neighbor in adjacency.get(part, ()):
            if neighbor not in depth:
                depth[neighbor] = depth[part] + 1
                queue.append(neighbor)

    # Parts never reached (isolated, or connected only via excluded nets)
    # default to depth 0 rather than being left out entirely.
    for part in parts:
        depth.setdefault(part, 0)

    return depth
