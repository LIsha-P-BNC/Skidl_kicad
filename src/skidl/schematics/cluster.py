# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Functional clustering and block-role classification for schematic placement.

Groups tightly-associated parts (an IC and its crystal, decoupling caps,
boot/reset/SWD headers) so the placer favors keeping them close, and
classifies higher-level blocks (Power/MCU/Communication/Sensor/Connector)
so sibling hierarchical blocks can be ordered left-to-right sensibly
instead of by arbitrary tag order.

These are pure data-in/data-out functions with no dependency on the live
force-directed solver: detect_clusters()/find_decap_affinities() return
part groupings, and compute_net_affinity_weights() returns a net -> extra
force multiplier dict that place.py's net_force_dist() consumes as an
opt-in weight on top of its existing net-force calculation. A circuit
with no recognizable clusters gets an empty weights dict and places
exactly as it did before this module existed.
"""

import re

from skidl.utilities import export_to_all
from .net_classify import classify_net_role, is_connector_part

_ANCHOR_REF_PREFIXES = frozenset({"U", "Q"})
_DECAP_REF_PREFIX = "C"
_DECAP_VALUE_RE = re.compile(r"^(100n|0?\.1u|100000p)f?$", re.IGNORECASE)

_MAX_CLUSTER_HOPS = 2
_LOW_FANOUT_MAX_PINS = 4
# A neighbor sharing an anchor ref-prefix (U/Q) is only kept OUT of the cluster
# if it is itself a *substantial* IC. A 2-pin crystal often carries ref-prefix
# Q or Y and is a satellite, not a rival anchor -- it must be allowed to join
# the MCU's cluster (crystal beside the XTAL pins). (RULE_ENGINE Phase 4.)
_MIN_RIVAL_ANCHOR_PINS = 5
# A LARGE connector (multi-pin header / receptacle) is never swallowed into a
# cluster: its wide breakout bus (port pins -> header, USB shell) belongs as
# LABELS, not a fan of parallel wires. A small connector (e.g. a 4-pin reset
# switch or a 2-pin jumper) still clusters with its anchor.
_MAX_CLUSTER_CONNECTOR_PINS = 4


def _low_fanout_neighbors(part, max_fanout=_LOW_FANOUT_MAX_PINS):
    """Return parts one hop from `part` via low-fanout, non-power nets.

    Power/ground nets and high-fanout nets are excluded since they would
    otherwise pull in unrelated parts from across the whole circuit
    (e.g. every part sharing the ground plane).
    """
    neighbors = set()
    for pin in getattr(part, "pins", []):
        net = getattr(pin, "net", None)
        if net is None:
            continue
        if classify_net_role(net) is not None:
            continue
        if len(net.pins) > max_fanout:
            continue
        for other_pin in net.pins:
            if other_pin.part is not part:
                neighbors.add(other_pin.part)
    return neighbors


@export_to_all
def find_anchor_parts(parts):
    """
    Return parts likely to anchor a functional cluster (ICs, transistors),
    ranked by pin count (highest first) so the largest/most-central parts
    claim their neighborhoods before smaller ones.

    Args:
        parts (list): Parts to search.

    Returns:
        list: Anchor-eligible parts, sorted by descending pin count.
    """
    anchors = [
        part
        for part in parts
        if (getattr(part, "ref_prefix", "") or "").upper() in _ANCHOR_REF_PREFIXES
    ]
    return sorted(anchors, key=lambda p: -len(getattr(p, "pins", [])))


@export_to_all
def detect_clusters(parts, max_hops=_MAX_CLUSTER_HOPS):
    """
    Group each anchor IC with its tightly-associated low-fanout neighbors
    (crystal, boot/reset/SWD headers, a single decoupling cap, etc.).

    Anchors are visited largest-first so an already-claimed part isn't
    re-assigned to a smaller anchor found later. Each returned cluster is
    a *soft* grouping meant to bias placement forces, not a rigid bbox
    merge, so a misdetected cluster only weakens attraction, it can't
    break placement.

    Args:
        parts (list): Parts to group.
        max_hops (int): Maximum hop distance from an anchor for a part
            to be pulled into its cluster.

    Returns:
        list[set]: One set of parts per detected cluster. Parts not
        within max_hops of any anchor aren't included in any cluster.
    """
    anchors = find_anchor_parts(parts)
    claimed = set()
    clusters = []

    for anchor in anchors:
        if anchor in claimed:
            continue
        cluster = {anchor}
        frontier = {anchor}
        for _ in range(max_hops):
            next_frontier = set()
            for part in frontier:
                for neighbor in _low_fanout_neighbors(part):
                    if neighbor in claimed or neighbor in cluster:
                        continue
                    neighbor_prefix = (getattr(neighbor, "ref_prefix", "") or "").upper()
                    neighbor_pins = len(getattr(neighbor, "pins", []))
                    if (
                        neighbor_prefix in _ANCHOR_REF_PREFIXES
                        and neighbor is not anchor
                        and (
                            neighbor_prefix == "U"
                            or neighbor_pins >= _MIN_RIVAL_ANCHOR_PINS
                        )
                    ):
                        # Don't swallow another IC's own cluster. A "U" neighbor is
                        # always a real IC (never swallowed). A "Q" neighbor is only
                        # kept out if it is a substantial part -- a 2-pin crystal
                        # (ref-prefix Q or Y) is a satellite and joins the cluster.
                        continue
                    if (
                        is_connector_part(neighbor)
                        and neighbor_pins > _MAX_CLUSTER_CONNECTOR_PINS
                    ):
                        # Keep large headers/receptacles OUT -- their breakout
                        # bus stays LABELS, not a fan of parallel wires.
                        continue
                    next_frontier.add(neighbor)
            cluster |= next_frontier
            frontier = next_frontier
            if not frontier:
                break
        claimed |= cluster
        clusters.append(cluster)

    return clusters


@export_to_all
def is_decoupling_cap(part):
    """
    Return True if a part looks like a decoupling capacitor: a 2-pin
    part with ref_prefix "C" and a value in the ~100nF class.

    Args:
        part: Part to check.

    Returns:
        bool: True if the part is classified as a decoupling cap.
    """
    ref_prefix = (getattr(part, "ref_prefix", "") or "").upper()
    if ref_prefix != _DECAP_REF_PREFIX:
        return False
    if len(getattr(part, "pins", [])) != 2:
        return False
    value = str(getattr(part, "value", "") or "").strip().replace(" ", "")
    return bool(_DECAP_VALUE_RE.match(value))


@export_to_all
def find_decap_affinities(parts):
    """
    Find nets that directly link a decoupling cap to an IC's power pin.

    Args:
        parts (list): Parts to inspect.

    Returns:
        dict: Mapping of net -> (decap_part, ic_part) for every net that
        is a decap-to-IC power-pin link.
    """
    affinities = {}
    for part in parts:
        if not is_decoupling_cap(part):
            continue
        for pin in part.pins:
            net = getattr(pin, "net", None)
            if net is None or classify_net_role(net) is None:
                continue
            ic_parts = [
                p.part
                for p in net.pins
                if p.part is not part
                and (getattr(p.part, "ref_prefix", "") or "").upper()
                in _ANCHOR_REF_PREFIXES
            ]
            if ic_parts:
                affinities[net] = (part, ic_parts[0])
    return affinities


@export_to_all
def compute_net_affinity_weights(parts, cluster_boost=3.0, decap_boost=6.0):
    """
    Combine cluster membership and decap affinity into a per-net force
    multiplier for placement to use, so tightly-associated parts are
    pulled together more strongly than generic net connectivity alone
    would manage.

    Args:
        parts (list): Parts to analyze.
        cluster_boost (float): Multiplier for nets connecting two parts
            within the same detected functional cluster.
        decap_boost (float): Multiplier for a decap-to-IC power-pin net
            (takes priority over the cluster boost when both apply).

    Returns:
        dict: Mapping of net -> force multiplier. Nets absent from this
        dict should be treated as an implicit multiplier of 1.0.
    """
    weights = {}

    for cluster in detect_clusters(parts):
        for part in cluster:
            for pin in getattr(part, "pins", []):
                net = getattr(pin, "net", None)
                if net is None:
                    continue
                net_parts = {p.part for p in net.pins}
                if len(net_parts) > 1 and net_parts <= cluster:
                    weights[net] = max(weights.get(net, 1.0), cluster_boost)

    for net in find_decap_affinities(parts):
        weights[net] = decap_boost

    return weights


# ---------------------------------------------------------------------------
# Block-role classification
# ---------------------------------------------------------------------------

_ROLE_ORDER = (
    "Power", "MCU", "Communication", "Sensor", "Drivers", "Connector", "Other"
)

_COMMUNICATION_NET_RE = re.compile(
    r"(CAN|I2C|IIC|SPI|UART|USART|USB|RS485|RS232|SMBUS|ETHERNET|MDIO)",
    re.IGNORECASE,
)
_SENSOR_NET_RE = re.compile(r"(ADC|SENSE|SENSOR)", re.IGNORECASE)
_POWER_KEYWORD_RE = re.compile(
    r"(regulator|buck|boost|ldo|charger|battery|protection)", re.IGNORECASE
)
# Output/driver stage: motor drivers, gate drivers, H-bridges, relays, solenoids,
# power MOSFET switches. Signals flow out of the MCU to these, so they sit toward
# the RIGHT of the sheet, just before the output connectors.
_DRIVER_NET_RE = re.compile(
    r"(MOTOR|RELAY|SOLENOID|HBRIDGE|H_BRIDGE|SERVO|GATE_?DRV|PWM_?OUT|"
    r"\bDRV\b|COIL|BUZZER|HEATER)",
    re.IGNORECASE,
)
_DRIVER_KEYWORD_RE = re.compile(
    r"(gate.?driver|motor.?driver|h.?bridge|mosfet.?driver|half.?bridge|"
    r"\bDRV8\d|relay|solenoid|\bIGBT\b|power.?stage)",
    re.IGNORECASE,
)


def _internal_nets(parts):
    nets = []
    seen = set()
    for part in parts:
        for pin in getattr(part, "pins", []):
            net = getattr(pin, "net", None)
            if net is not None and id(net) not in seen:
                seen.add(id(net))
                nets.append(net)
    return nets


@export_to_all
def classify_block_role(parts, nets=None):
    """
    Classify a hierarchical block (or any group of parts) by its
    dominant function, for ordering sibling blocks left-to-right.

    This is a heuristic, not a guarantee: an ambiguous or misclassified
    block falls into "Other", which sorts last and preserves its
    original relative position (see order_blocks_by_role()) -- i.e. it
    degrades to today's tag-order behavior rather than being placed
    somewhere actively wrong.

    Args:
        parts (list): Parts belonging to the block.
        nets (list, optional): Nets internal to the block. If omitted,
            inferred from the parts' pins.

    Returns:
        str: One of "Power", "MCU", "Communication", "Sensor",
        "Connector", or "Other".
    """
    parts = list(parts)
    if not parts:
        return "Other"

    if nets is None:
        nets = _internal_nets(parts)

    net_names = " ".join(getattr(net, "name", "") or "" for net in nets)
    search_texts = " ".join(getattr(p, "search_text", "") or "" for p in parts)

    connector_frac = sum(1 for p in parts if is_connector_part(p)) / len(parts)
    anchor_frac = sum(
        1
        for p in parts
        if (getattr(p, "ref_prefix", "") or "").upper() in _ANCHOR_REF_PREFIXES
    ) / len(parts)
    power_frac = sum(1 for net in nets if classify_net_role(net) is not None) / max(
        len(nets), 1
    )
    has_inductor = any(
        (getattr(p, "ref_prefix", "") or "").upper() == "L" for p in parts
    )

    if _COMMUNICATION_NET_RE.search(net_names):
        return "Communication"
    if _SENSOR_NET_RE.search(net_names):
        return "Sensor"
    # Driver/output stage BEFORE the Power and MCU tests: a gate/motor driver
    # block carries transistor (Q) anchors, so without this it would fall through
    # to the anchor_frac>0 "MCU" branch and be ordered as if it were the controller.
    if _DRIVER_NET_RE.search(net_names) or _DRIVER_KEYWORD_RE.search(search_texts):
        return "Drivers"
    if (
        _POWER_KEYWORD_RE.search(search_texts)
        or has_inductor
        or (power_frac >= 0.5 and anchor_frac == 0)
    ):
        return "Power"
    if connector_frac >= 0.5:
        return "Connector"
    if anchor_frac > 0:
        return "MCU"

    return "Other"


@export_to_all
def block_affinity_matrix(blocks):
    """Inter-block connectivity for placement optimization: for every
    unordered block pair (i, j), the sum of placement weights of the
    nets that span both blocks (see net_classify.NET_PLACEMENT_WEIGHTS).
    `blocks` is a list of part-view collections. Returns
    {(i, j): weight} with i < j; pairs with zero weight are absent."""
    from .net_classify import placement_weight

    part_block = {}
    for bi, block in enumerate(blocks):
        for v in block:
            part_block[id(v)] = bi

    seen_nets = {}
    for block in blocks:
        for v in block:
            for pin in getattr(v, "pins", []):
                net = getattr(pin, "net", None)
                if net is None or id(net) in seen_nets:
                    continue
                seen_nets[id(net)] = net

    affinity = {}
    for net in seen_nets.values():
        span = {part_block[id(p.part)] for p in getattr(net, "pins", [])
                if id(p.part) in part_block}
        if len(span) < 2:
            continue
        w = placement_weight(net)
        if w <= 0:
            continue
        span = sorted(span)
        for a in range(len(span)):
            for b in range(a + 1, len(span)):
                key = (span[a], span[b])
                affinity[key] = affinity.get(key, 0.0) + w
    return affinity


@export_to_all
def order_blocks_by_role(role_blocks):
    """
    Sort blocks by functional role (Power, MCU, Communication, Sensor,
    Connector, Other), preserving original relative order within a role
    (stable sort) so ambiguous/"Other" blocks degrade to today's
    tag-order behavior rather than being reshuffled.

    Args:
        role_blocks (list): Sequence of (role, block) tuples, e.g. as
            produced by pairing classify_block_role() output with the
            block objects being ordered.

    Returns:
        list: The block objects (not the (role, block) tuples), reordered.
    """
    role_index = {role: i for i, role in enumerate(_ROLE_ORDER)}
    ordered = sorted(
        role_blocks,
        key=lambda role_block: role_index.get(role_block[0], len(_ROLE_ORDER)),
    )
    return [block for _role, block in ordered]
