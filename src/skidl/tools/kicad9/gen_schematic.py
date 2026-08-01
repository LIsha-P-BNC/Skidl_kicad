# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Generate a KiCad 9 schematic from a Circuit object.

Thin wrapper around the shared sexp_schematic module.
Uses SKiDL's placement and routing infrastructure.
"""

import os
import re
import shutil
import subprocess
from collections import Counter

from skidl.geometry import BBox, Point, Tx, Vector
from skidl.schematics.net_terminal import NetTerminal
from skidl.scriptinfo import get_script_name
from skidl.utilities import export_to_all, rmv_attr

from .sexp_schematic import write_top_schematic
from .bboxes import calc_hier_label_bbox, calc_symbol_bbox
from .constants import GRID, PIN_LABEL_FONT_SIZE


__all__ = []


def _setup_kicad_env():
    """Set KiCad footprint directory if not already set.

    Auto-detects the standard KiCad footprint directory so that
    generated schematics can reference footprints for PCB layout.
    """
    from skidl import get_default_tool

    kicad_version = get_default_tool()[len("kicad"):]
    if not os.environ.get(f"KICAD{kicad_version}_FOOTPRINT_DIR"):
        for path in [
            "/usr/share/kicad/footprints",
            "/usr/local/share/kicad/footprints",
            os.path.expanduser(f"~/.local/share/kicad/{kicad_version}.0/footprints"),
        ]:
            if os.path.isdir(path):
                os.environ[f"KICAD{kicad_version}_FOOTPRINT_DIR"] = path
                break


# Suppress legacy fp-lib-table warnings from older KiCad tool modules.
import warnings
warnings.filterwarnings("ignore", message=".*fp-lib-table.*")


# Pattern matching common power net names.
_POWER_NET_RE = re.compile(
    r"^(\+\d[\d.]*V[\d]*|GND|AGND|DGND|PGND|VCC|VDD|VSS|VEE|VBUS|VBAT|AVCC|AVDD|DVCC|DVDD)$",
    re.IGNORECASE,
)

# ERC error types that can be fixed by stubbing nets.
FIXABLE_ERROR_TYPES = frozenset(
    {"pin_not_connected", "pin_not_driven", "wire_not_connected"}
)


def auto_stub_nets(circuit, **options):
    """Auto-stub power nets and high-fanout nets before generation.

    Only modifies nets that haven't been explicitly set by the user.
    Called when auto_stub=True is passed to gen_schematic().

    Args:
        circuit: The Circuit object containing nets to analyze.
        options: Dict of options. Recognizes 'auto_stub_fanout' (default 8).
    """
    import sys

    # This is only an early routing safety valve. Let the post-placement
    # classifier decide ordinary 2-3 pin local connections from geometry.
    fanout_threshold = options.get("auto_stub_fanout", 8)
    stubbed_power = []
    stubbed_fanout = []

    for net in circuit.nets:
        if getattr(net, "_stub_explicit", False):
            continue
        if not net.valid or len(net.pins) == 0:
            continue

        # Power nets: anything starting with "+" or matching common power names.
        if net.name.startswith("+") or _POWER_NET_RE.match(net.name):
            net._stub = True
            net._stub_explicit = False
            for pin in net.get_pins():
                pin.stub = True
            stubbed_power.append(f"{net.name}({len(net.pins)})")
            continue

        # High fanout nets: many pins connected to the same net.
        if len(net.pins) >= fanout_threshold:
            net._stub = True
            net._stub_explicit = False
            for pin in net.get_pins():
                pin.stub = True
            stubbed_fanout.append(f"{net.name}({len(net.pins)})")

    from skidl.logger import active_logger
    active_logger.info(
        f"  [auto_stub] power: {', '.join(stubbed_power[:10])}{'...' if len(stubbed_power) > 10 else ''}"
    )
    active_logger.info(
        f"  [auto_stub] fanout>={fanout_threshold}: {', '.join(stubbed_fanout[:10])}{'...' if len(stubbed_fanout) > 10 else ''}"
    )


def _run_erc(schematic_path):
    """Run kicad-cli ERC on a schematic file and return the report path.

    Args:
        schematic_path: Path to the .kicad_sch file.

    Returns:
        str: Path to the ERC report file, or None if kicad-cli is unavailable.
    """
    report_path = schematic_path.replace(".kicad_sch", "-erc.rpt")
    try:
        subprocess.run(
            [
                "kicad-cli",
                "sch",
                "erc",
                "--output",
                report_path,
                "--severity-all",
                schematic_path,
            ],
            capture_output=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return report_path if os.path.exists(report_path) else None


def _parse_erc_report(report_path):
    """Parse kicad-cli ERC report and return list of (error_type, symbol_ref, pin_num).

    The ERC report format has lines like:
        [pin_not_connected]: Pin not connected ...
        @(x,y): Symbol U1 Pin 3 ...

    Args:
        report_path: Path to the ERC .rpt file.

    Returns:
        list: List of (error_type, symbol_ref, pin_num) tuples.
    """
    if not report_path or not os.path.exists(report_path):
        return []

    errors = []
    current_error_type = None

    # Patterns for ERC report parsing.
    error_type_re = re.compile(r"^\[(\w+)\]")
    symbol_pin_re = re.compile(r"Symbol\s+(\S+)\s+Pin\s+(\S+)")

    with open(report_path, "r") as f:
        for line in f:
            line = line.strip()

            # Match error type header.
            m = error_type_re.match(line)
            if m:
                current_error_type = m.group(1)
                continue

            # Match symbol/pin reference in the detail line.
            if current_error_type:
                m = symbol_pin_re.search(line)
                if m:
                    errors.append((current_error_type, m.group(1), m.group(2)))
                    current_error_type = None

    return errors


def _stub_nets_for_erc_errors(circuit, errors):
    """Convert nets involved in ERC errors to stubs for regeneration.

    Args:
        circuit: The Circuit object.
        errors: List of (error_type, symbol_ref, pin_num) from _parse_erc_report.

    Returns:
        bool: True if any nets were newly stubbed.
    """
    stubbed_any = False
    for error_type, symbol_ref, pin_num in errors:
        if error_type not in FIXABLE_ERROR_TYPES:
            continue
        for part in circuit.parts:
            if part.ref == symbol_ref:
                for pin in part.pins:
                    if str(pin.num) == str(pin_num):
                        net = pin.net
                        if net and not getattr(net, "_stub_explicit", False):
                            net._stub = True
                            net._stub_explicit = False
                            for p in net.get_pins():
                                p.stub = True
                            stubbed_any = True
                break
    return stubbed_any


def _net_span_and_pins(net, node_parts):
    """Return (pins-in-node, farthest-apart-pin-pair Manhattan distance,
    a Segment spanning that pair) for a net, using each pin's absolute
    placed position (part.tx already reflects the finished placement at
    this point -- pin.place_pt/route_pt have already been stripped by
    node.rmv_placement_stuff(), so pin.pt is used instead).
    """
    from skidl.geometry import Point, Segment

    pins = [p for p in net.pins if p.part in node_parts]
    if len(pins) < 2:
        return pins, 0, None

    pts = []
    for p in pins:
        pin_pt = getattr(p, "pt", Point(p.x, p.y))
        part_tx = getattr(p.part, "tx", None)
        pts.append(pin_pt * part_tx if part_tx else pin_pt)

    max_dist = 0
    span = None
    for i, a in enumerate(pts):
        for b in pts[i + 1 :]:
            dist = abs(a.x - b.x) + abs(a.y - b.y)
            if dist > max_dist:
                max_dist = dist
                span = Segment(a, b)

    return pins, max_dist, span


def _net_pin_points(net, node_parts):
    """Absolute placed positions of this net's pins that live in node_parts."""
    from skidl.geometry import Point

    pts = []
    for p in net.pins:
        if p.part not in node_parts:
            continue
        pin_pt = getattr(p, "pt", Point(p.x, p.y))
        part_tx = getattr(p.part, "tx", None)
        pts.append(pin_pt * part_tx if part_tx else pin_pt)
    return pts


def _min_pin_distance(pts):
    """Closest-pair Manhattan distance among placed pin points (0 if <2).

    The wire-vs-label decision keys on the CLOSEST connected pins, not the
    farthest (span) and not symbol centers: an MCU whose XTAL pins sit right
    beside the crystal reads as "adjacent" and must wire even if the two symbol
    bodies' centers are ~1200 mil apart.
    """
    if len(pts) < 2:
        return 0
    best = None
    for i, a in enumerate(pts):
        for b in pts[i + 1:]:
            d = abs(a.x - b.x) + abs(a.y - b.y)
            if best is None or d < best:
                best = d
    return best or 0


def _all_pin_points(node_parts):
    """(point, part) for every pin of every part -- input to congestion."""
    from skidl.geometry import Point

    pts = []
    for part in node_parts:
        for p in getattr(part, "pins", []):
            pin_pt = getattr(p, "pt", Point(p.x, p.y))
            part_tx = getattr(part, "tx", None)
            pts.append((pin_pt * part_tx if part_tx else pin_pt, part))
    return pts


def _pin_congestion(net_pts, own_parts, all_pin_pts, radius):
    """Max, over the net's pins, of the number of OTHER parts' pins within
    `radius` (Manhattan). A short net whose pin sits in a dense pin field (a
    many-pin IC edge with several satellites crowding it) is hard to read as a
    wire -> bias to a label. Counts pins of parts NOT on this net only."""
    best = 0
    for a in net_pts:
        c = 0
        for b, part in all_pin_pts:
            if part in own_parts:
                continue
            if abs(a.x - b.x) + abs(a.y - b.y) <= radius:
                c += 1
        if c > best:
            best = c
    return best


def _cluster_id_map(node_parts):
    """Map each part to an id shared by every part in its detected
    functional cluster (see schematics.cluster.detect_clusters()), so
    _classify_and_stub_complex_nets() can bias same-cluster nets toward
    staying wired. Parts outside any cluster get their own unique id
    (i.e. never considered "same cluster" as anything else).
    """
    from skidl.schematics.cluster import detect_clusters

    cluster_of = {}
    for cluster_id, cluster in enumerate(detect_clusters(list(node_parts))):
        for part in cluster:
            cluster_of[part] = cluster_id
    for part in node_parts:
        cluster_of.setdefault(part, object())
    return cluster_of


def _classify_and_stub_complex_nets(circuit, node, **options):
    """Classify nets after placement: keep local ones as wires, stub the rest.

    Called after placement succeeds, before routing. Power/ground nets are
    already power symbols (stubbed pre-placement) and never reach here. Each
    remaining candidate internal net is decided by a set of INDEPENDENT
    short-circuit gates -- NOT an additive score -- in this order (the first
    gate that matches wins):

        1. < 2 pins in this node        -> skip (left as-is).
        2. FUNCTION -> WIRE: classify_net_function(net) is not None
           (clock / reset / usb_diff / decoupling). These read as direct
           local connections regardless of distance.
        3. AFFINITY -> WIRE: all pins in ONE detected functional cluster
           AND fanout <= auto_stub_cluster_wire_max_pins.
        4. CROSSINGS -> LABEL: net span intersects
           >= auto_stub_crossing_label_threshold other nets' spans.
        5. CONGESTION -> LABEL: _pin_congestion (other-part pins within
           auto_stub_congestion_radius) >= auto_stub_congestion_label_threshold.
        6. DISTANCE -> LABEL: CLOSEST pin-to-pin Manhattan distance
           >= auto_stub_far_label_dist (closest pins, not the symbol span).
        7. FANOUT -> LABEL: fanout > auto_stub_max_wire_pins.
        8. otherwise                    -> WIRE.

    label == wire electrically; smart_schematic re-verifies every seed, so a
    borderline call can only look worse, never short.

    Args:
        circuit: The Circuit object.
        node: The placed SchNode.
        options: Dict of options (all with dynamic defaults):
            auto_stub_max_wire_pins (int): fanout above which a net labels
                (gate 7); also the cluster-wire fanout cap base. Default 3.
            auto_stub_far_label_dist (int): closest pin-to-pin distance, in
                mils, at/above which a net labels (gate 6). Default 1500.
            auto_stub_crossing_label_threshold (int): span-crossing count
                at/above which a net labels (gate 4). Default 4.
            auto_stub_congestion_label_threshold (int): nearby other-part
                pin count at/above which a net labels (gate 5). Default 16.
            auto_stub_congestion_radius (int): radius in mils for the
                congestion count (gate 5). Default 300.
            auto_stub_cluster_wire_max_pins (int): fanout cap for the
                single-cluster wire gate (gate 3). Default max_wire_pins = 3.
    """
    # --- DYNAMIC wire-vs-label decision (RULE_ENGINE Phase 7) ---------------
    # Decide like an experienced designer: FUNCTION first (a net whose local
    # circuitry must read as directly connected always wires), then avoid
    # CLUTTER (label only when crossings / pin-congestion / distance / fanout
    # would hurt readability). Distance is ONE input, never the sole gate, and
    # it is the CLOSEST pin-to-pin distance, not the symbol span. Power nets are
    # already power symbols (stubbed pre-placement) and never reach here; SKiDL
    # Bus members render as a bus overlay (sexp_schematic). label==wire
    # electrically, and smart_schematic re-verifies every seed, so a wrong call
    # can only look worse, never short.
    from skidl.schematics.net_classify import classify_net_function

    max_wire_pins = options.get("auto_stub_max_wire_pins", 3)
    far_label_dist = options.get("auto_stub_far_label_dist", 1500)  # mil, closest-pin
    cross_label_at = options.get("auto_stub_crossing_label_threshold", 4)
    congest_at = options.get("auto_stub_congestion_label_threshold", 16)
    congest_radius = options.get("auto_stub_congestion_radius", 300)  # mil
    cluster_wire_max_pins = options.get("auto_stub_cluster_wire_max_pins", max_wire_pins)

    node_parts = set(node.parts)
    candidate_nets = [
        net
        for net in node.get_internal_nets()
        if not getattr(net, "_stub_explicit", False)
        and not getattr(net, "_stub", False)
    ]

    spans = {}
    pins_by_net = {}
    pts_by_net = {}
    mindist_by_net = {}
    for net in candidate_nets:
        pins, _max_dist, span = _net_span_and_pins(net, node_parts)
        pins_by_net[net] = pins
        pts = _net_pin_points(net, node_parts)
        pts_by_net[net] = pts
        mindist_by_net[net] = _min_pin_distance(pts)
        if span is not None:
            spans[net] = span

    all_pin_pts = _all_pin_points(node_parts)
    cluster_of = _cluster_id_map(node_parts)

    def _stub(net):
        net._stub = True
        net._stub_explicit = False
        for p in net.get_pins():
            p.stub = True

    stubbed_count = 0
    for net in candidate_nets:
        pins = pins_by_net[net]
        if len(pins) < 2:
            continue
        fanout = len(pins)

        # (3) FUNCTION priority -> WIRE. Clock (crystal/XTAL/OSC), reset, USB
        #     differential pair, and decoupling nets must read as direct local
        #     connections -- never a label, whatever the distance. (Power is
        #     already a symbol and returns "power" here, but power nets don't
        #     reach this loop, so the only categories seen are the wire ones.)
        if classify_net_function(net) is not None:
            continue

        # (4) AFFINITY -> WIRE: all pins in one functional cluster (crystal/
        #     decap/reset/USB-R/LED grouped around an anchor IC), low fanout.
        if fanout <= cluster_wire_max_pins and len(
            {cluster_of[p.part] for p in pins}
        ) == 1:
            continue

        # Clutter metrics (only computed for ordinary signal nets).
        net_span = spans.get(net)
        crossings = (
            sum(
                1
                for other_net, other_span in spans.items()
                if other_net is not net and net_span.intersects(other_span)
            )
            if net_span is not None
            else 0
        )
        congestion = _pin_congestion(
            pts_by_net[net], {p.part for p in pins}, all_pin_pts, congest_radius
        )
        eff_dist = mindist_by_net[net]

        # (5) many CROSSINGS -> LABEL (a wire would tangle the drawing).
        if crossings >= cross_label_at:
            _stub(net)
            stubbed_count += 1
            continue
        # (6) high pin-CONGESTION -> LABEL (unreadable even if short).
        if congestion >= congest_at:
            _stub(net)
            stubbed_count += 1
            continue
        # (7) FAR (closest pins beyond far_label_dist) -> LABEL.
        if eff_dist >= far_label_dist:
            _stub(net)
            stubbed_count += 1
            continue
        # (8) high FANOUT -> LABEL (bus-like clutter; a real Bus renders as one).
        if fanout > max_wire_pins:
            _stub(net)
            stubbed_count += 1
            continue
        # (9) otherwise -> WIRE (near, low fanout, few crossings, uncongested).

    if stubbed_count:
        from skidl.logger import active_logger
        active_logger.info(
            f"  [selective_routing] Stubbed {stubbed_count} complex nets after placement"
        )


class LabelsOnlyWarning(UserWarning):
    """Warning raised when schematic falls back to labels-only output."""

    pass


def _handle_fallback(circuit, tool_module, filepath, top_name, title, flatness,
                     options, logger, reason=""):
    """Handle routing failure fallback according to the auto_stub_fallback policy.

    Args:
        circuit: The Circuit object.
        tool_module: The KiCad tool module.
        filepath, top_name, title, flatness: Schematic generation parameters.
        options: Dict of options including auto_stub_fallback policy.
        logger: The active logger.
        reason: Human-readable explanation of why we're falling back.
    """
    import warnings

    from skidl.schematics.sch_node import SchNode
    from skidl.tools.kicad9.sexp_schematic import write_top_schematic

    fallback = options.get("auto_stub_fallback", "labels")

    if fallback == "raise":
        finalize_parts_and_nets(circuit, **options)
        from skidl.schematics.route import RoutingFailure

        raise RoutingFailure(
            f"{reason}. Set auto_stub_fallback='labels' to produce "
            "labels-only output instead of crashing."
        )

    # Produce labels-only output.
    stubbed_nets = []
    for net in circuit.nets:
        if not getattr(net, "_stub_explicit", False) and not net._stub:
            stubbed_nets.append(net.name)
    _stub_all_non_explicit(circuit)

    preprocess_circuit(circuit, **options)
    node = SchNode(circuit, tool_module, filepath, top_name, title, flatness)
    node.place(expansion_factor=1.0, **options)
    node.route(**options)
    output_file = write_top_schematic(
        circuit, node, filepath, top_name, title, version=20230409
    )
    finalize_parts_and_nets(circuit, **options)

    msg = (
        f"{reason}. Produced labels-only schematic at {output_file}. "
        f"Nets converted to labels: {', '.join(stubbed_nets[:10])}"
        f"{'...' if len(stubbed_nets) > 10 else ''}. "
        "This may mask routing issues that could be fixed by improving "
        "the circuit layout. Set auto_stub_fallback='raise' to get the "
        "original error instead."
    )
    logger.warning(msg)

    if fallback == "warn":
        warnings.warn(msg, LabelsOnlyWarning, stacklevel=4)


def _stub_all_non_explicit(circuit):
    """Stub all nets that weren't explicitly set by the user (labels-only fallback).

    Args:
        circuit: The Circuit object.
    """
    for net in circuit.nets:
        if not getattr(net, "_stub_explicit", False):
            net._stub = True
            for pin in net.get_pins():
                pin.stub = True


def preprocess_circuit(circuit, **options):
    """Add stuff to parts & nets for doing placement and routing of schematics."""

    def units(part):
        if len(part.unit) == 0:
            return [part]
        else:
            return part.unit.values()

    def initialize(part):
        """Initialize part or its part units."""

        pin_limit = options.get("orientation_pin_limit", 44)

        # KiCad 6+ stores pin orientation as integer degrees; normalize to string.
        deg_to_orient = {0: "R", 90: "U", 180: "L", 270: "D"}

        for part_unit in units(part):
            part_unit.tx = Tx.from_symtx(getattr(part_unit, "symtx", ""))

            num_pins = len(part_unit.pins)
            part_unit.orientation_locked = getattr(part_unit, "symtx", False) or not (
                1 < num_pins <= pin_limit
            )

            part_unit.grab_pins()

            for pin in part_unit:
                # Normalize pin orientation from integer degrees to string direction.
                if isinstance(pin.orientation, int):
                    pin.orientation = deg_to_orient.get(pin.orientation % 360, "R")
                # Pin coords from KiCad 9 libs are in mm; convert to mils
                # so the placement/routing engine works in consistent units.
                MM_TO_MILS = 1 / 0.0254
                pin.pt = Point(pin.x * MM_TO_MILS, pin.y * MM_TO_MILS)
                pin.routed = False

    def rotate_power_pins(part):
        """Seed a part's orientation so power pins point up and ground pins point
        down (professional convention: power at top, ground at bottom).

        Runs before placement as an INITIAL orientation; the tension/crossing
        optimizer may still adjust it. A part with no power or ground pins tallies
        nothing and is left untouched, so this only reorients power-bearing parts
        (ICs, regulators, decoupling caps) -- pure signal parts are unaffected.
        Manual ``symtx`` always wins and skips this seed.
        """

        if getattr(part, "symtx", ""):
            return

        def is_pwr(net_name):
            return net_name.startswith("+")

        def is_gnd(net_name):
            return "gnd" in net_name.lower()

        dont_rotate_pin_cnt = options.get("dont_rotate_pin_count", 10000)

        for part_unit in units(part):
            if len(part_unit) > dont_rotate_pin_cnt:
                return

            rotation_tally = Counter()
            for pin in part_unit:
                net_name = getattr(pin.net, "name", "").lower()
                if is_gnd(net_name):
                    if pin.orientation == "U":
                        rotation_tally[0] += 1
                    if pin.orientation == "D":
                        rotation_tally[180] += 1
                    if pin.orientation == "L":
                        rotation_tally[90] += 1
                    if pin.orientation == "R":
                        rotation_tally[270] += 1
                elif is_pwr(net_name):
                    if pin.orientation == "D":
                        rotation_tally[0] += 1
                    if pin.orientation == "U":
                        rotation_tally[180] += 1
                    if pin.orientation == "L":
                        rotation_tally[270] += 1
                    if pin.orientation == "R":
                        rotation_tally[90] += 1

            try:
                rotation = rotation_tally.most_common()[0][0]
            except IndexError:
                pass
            else:
                tx_cw_90 = Tx(a=0, b=-1, c=1, d=0)
                for _ in range(int(round(rotation / 90))):
                    part_unit.tx = part_unit.tx * tx_cw_90

    def calc_part_bbox(part):
        """Calculate the labeled bounding boxes and store it in the part."""

        bare_bboxes = calc_symbol_bbox(part)[1:]

        for part_unit, bare_bbox in zip(units(part), bare_bboxes):
            resize_wh = Vector(0, 0)
            if bare_bbox.w < 100:
                resize_wh.x = (100 - bare_bbox.w) / 2
            if bare_bbox.h < 100:
                resize_wh.y = (100 - bare_bbox.h) / 2
            bare_bbox = bare_bbox.resize(resize_wh)

            part_unit.lbl_bbox = BBox()
            part_unit.lbl_bbox.add(bare_bbox)
            for pin in part_unit:
                if pin.stub:
                    hlbl_bbox = calc_hier_label_bbox(pin.net.name, pin.orientation)
                    hlbl_bbox *= Tx().move(pin.pt)
                    part_unit.lbl_bbox.add(hlbl_bbox)

            # Reserve space for the Reference/Value property text that gets
            # written at a fixed +/-2*GRID offset from the part origin (see
            # part_to_sexp() in sexp_schematic.py). Without this, the placer
            # only avoids overlapping symbol bodies and stub labels, so
            # tightly-packed layouts end up with Reference/Value text
            # overlapping neighboring parts even when the parts themselves
            # don't collide.
            half_h = PIN_LABEL_FONT_SIZE / 2
            ref_len = len(str(getattr(part, "ref", "") or "")) * PIN_LABEL_FONT_SIZE
            val_len = len(str(getattr(part, "value", "") or "")) * PIN_LABEL_FONT_SIZE
            part_unit.lbl_bbox.add(
                BBox(Point(0, -2 * GRID - half_h), Point(ref_len, -2 * GRID + half_h))
            )
            part_unit.lbl_bbox.add(
                BBox(Point(0, 2 * GRID - half_h), Point(val_len, 2 * GRID + half_h))
            )

            part_unit.bbox = part_unit.lbl_bbox

    for part in circuit.parts:
        initialize(part)
        rotate_power_pins(part)
        calc_part_bbox(part)


def finalize_parts_and_nets(circuit, **options):
    """Restore parts and nets after place & route is done."""

    net_terminals = (p for p in circuit.parts if isinstance(p, NetTerminal))
    circuit.rmv_parts(*net_terminals)

    for part in circuit.parts:
        part.grab_pins()

    rmv_attr(circuit.parts, ("force", "bbox", "lbl_bbox", "tx"))


@export_to_all
def gen_schematic(
    circuit,
    filepath=".",
    top_name=get_script_name(),
    title="SKiDL-Generated Schematic",
    flatness=0.0,
    retries=2,
    **options,
):
    """Create a KiCad 9 schematic file from a Circuit object.

    Args:
        circuit (Circuit): The Circuit object that will have a schematic generated for it.
        filepath (str, optional): The directory where the schematic files are placed. Defaults to ".".
        top_name (str, optional): The name for the top of the circuit hierarchy. Defaults to get_script_name().
        title (str, optional): The title of the schematic. Defaults to "SKiDL-Generated Schematic".
        flatness (float, optional): Determines how much the hierarchy is flattened in the schematic.
            Defaults to 0.0 (completely hierarchical). Use 1.0 to flatten everything into one sheet.
        retries (int, optional): Number of times to re-try if routing fails. Defaults to 2.
        options (dict, optional): Dict of options and values, usually for drawing/debugging.

    Auto-stub options (pass as keyword arguments):
        auto_stub (bool): Enable auto-stubbing for large/complex circuits. Converts nets that
            would fail routing into global labels, and runs a KiCad ERC correction loop to
            iteratively fix remaining issues. Power nets (GND, VCC, etc.) are automatically
            emitted as proper KiCad power symbols. Default False.
        auto_stub_fanout (int): Nets with more pins than this are stubbed pre-routing. Default 3.
        auto_stub_max_wire_pins (int): Max pins on a net before selective routing stubs it
            post-placement. Default 3.
        auto_stub_far_label_dist (int): Closest pin-to-pin Manhattan distance (mils) at/above
            which selective routing stubs the net to a label. Default 1500.
        erc_max_iterations (int): Max ERC correction loop passes. Default 8.
        auto_stub_fallback (str): What to do when routing fails with auto_stub enabled.
            "labels" (default) — fall back to labels-only schematic.
            "raise" — raise the RoutingFailure so the caller sees it.
            "warn" — produce labels-only but also raise a warning exception.

    Tips for best results with auto_stub:
        - Use @subcircuit to group related parts (e.g. power supply, MCU, amplifier).
          Each subcircuit gets placed and routed independently, producing more wired
          connections and cleaner hierarchical sheets.
        - Keep subcircuits to 5-15 parts for best wire routing results.
        - Power nets are automatically detected and emitted as KiCad power symbols
          (e.g. power:GND, power:VCC) which display correctly in the schematic editor.

    Example::

        from skidl import *

        @subcircuit
        def power_supply(vin, vout, gnd):
            ldo = Part("Regulator_Linear", "AP2112K-3.3")
            ldo["VIN"] += vin
            ldo["VOUT"] += vout
            ldo["GND"] += gnd
            ldo["EN"] += vin
            for val in ["1uF", "1uF"]:
                c = Part("Device", "C", value=val)
                c[1] += vout if val == "1uF" else vin
                c[2] += gnd

        vcc = Net("VCC"); vcc.drive = POWER
        gnd = Net("GND"); gnd.drive = POWER
        vin = Net("VIN")

        power_supply(vin, vcc, gnd)

        generate_schematic(auto_stub=True)
    """

    from skidl import get_default_tool
    from skidl.logger import active_logger
    from skidl.schematics.place import PlacementFailure
    from skidl.schematics.route import RoutingFailure
    from skidl.schematics.sch_node import SchNode
    from skidl.tools import tool_modules

    tool_module = tool_modules[get_default_tool()]

    _setup_kicad_env()

    # Part placement options that should always be turned on.
    options["use_push_pull"] = True
    options["rotate_parts"] = True
    options["pt_to_pt_mult"] = 5
    options["pin_normalize"] = True

    # Phase 1: Heuristic auto-stubbing before first generation pass.
    if options.get("auto_stub", False):
        auto_stub_nets(circuit, **options)

    expansion_factor = 1.0
    failure_type = None

    for attempt in range(retries):
        preprocess_circuit(circuit, **options)

        node = SchNode(
            circuit, tool_module, filepath, top_name, title, flatness
        )

        try:
            node.place(expansion_factor=expansion_factor, **options)
            if options.get("auto_stub", False):
                _classify_and_stub_complex_nets(circuit, node, **options)
            node.route(**options)

        except PlacementFailure as e:
            finalize_parts_and_nets(circuit, **options)
            failure_type = e
            active_logger.warning(
                f"Placement failed on attempt {attempt + 1}/{retries}: {e}"
            )
            continue

        except RoutingFailure as e:
            finalize_parts_and_nets(circuit, **options)
            expansion_factor *= 1.5
            failure_type = e
            active_logger.warning(
                f"Routing failed on attempt {attempt + 1}/{retries}, expanding area by 1.5x: {e}"
            )
            continue

        # Generate S-expression schematic using shared module.
        # KiCad 8/9 use version 20230409.
        output_file = write_top_schematic(
            circuit, node, filepath, top_name, title, version=20230409
        )

        active_logger.info(f"Schematic written to {output_file}")

        finalize_parts_and_nets(circuit, **options)

        # Phase 2: ERC correction loop (only when auto_stub is enabled).
        if options.get("auto_stub", False) and shutil.which("kicad-cli"):
            max_erc_iterations = options.get("erc_max_iterations", 3)
            for erc_attempt in range(max_erc_iterations):
                erc_report = _run_erc(output_file)
                errors = _parse_erc_report(erc_report)
                fixable = [e for e in errors if e[0] in FIXABLE_ERROR_TYPES]

                if not fixable:
                    active_logger.info(
                        f"ERC clean after {erc_attempt + 1} iteration(s)"
                    )
                    break

                if not _stub_nets_for_erc_errors(circuit, fixable):
                    active_logger.info(
                        f"ERC: {len(fixable)} unfixable errors remain after {erc_attempt + 1} iteration(s)"
                    )
                    break

                active_logger.info(
                    f"ERC correction: stubbed nets for {len(fixable)} errors, regenerating (iteration {erc_attempt + 1})"
                )

                # Full regeneration — try with expansion before giving up.
                erc_regen_ok = False
                for erc_expansion in [1.0, 1.5, 2.25]:
                    try:
                        preprocess_circuit(circuit, **options)
                        node = SchNode(
                            circuit,
                            tool_module,
                            filepath,
                            top_name,
                            title,
                            flatness,
                        )
                        node.place(expansion_factor=erc_expansion, **options)
                        if options.get("auto_stub", False):
                            _classify_and_stub_complex_nets(circuit, node, **options)
                        node.route(**options)
                        output_file = write_top_schematic(
                            circuit, node, filepath, top_name, title, version=20230409
                        )
                        finalize_parts_and_nets(circuit, **options)
                        erc_regen_ok = True
                        break
                    except (RoutingFailure, PlacementFailure) as inner_e:
                        finalize_parts_and_nets(circuit, **options)
                        if erc_expansion < 2.25:
                            active_logger.info(
                                f"ERC regeneration routing failed at {erc_expansion}x, "
                                f"trying {erc_expansion * 1.5}x expansion"
                            )
                        else:
                            active_logger.warning(
                                f"ERC regeneration routing failed after all expansion attempts: {inner_e}"
                            )

                if not erc_regen_ok:
                    # Routing failed even with expansion — handle per fallback policy.
                    _handle_fallback(
                        circuit, tool_module, filepath, top_name,
                        title, flatness, options, active_logger,
                        reason=f"ERC correction regeneration failed after expansion attempts",
                    )
                    break

        return

    # All retries exhausted.
    if failure_type and options.get("auto_stub", False):
        _handle_fallback(
            circuit, tool_module, filepath, top_name,
            title, flatness, options, active_logger,
            reason=f"Routing failed after all {retries} retries",
        )
        return

    finalize_parts_and_nets(circuit, **options)

    if failure_type:
        raise failure_type
    else:
        raise RuntimeError("Schematic generation failed for unknown reasons")
