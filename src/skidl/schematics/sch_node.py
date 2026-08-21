# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

import os
import re
from collections import defaultdict
from itertools import chain

from skidl.utilities import export_to_all
from skidl.geometry import BBox, Point, Tx, Vector
from .place import Placer
from .route import Router


# ---------------------------------------------------------------------------
# Wire policy
# ---------------------------------------------------------------------------

def _wire_max_local_pins():
    """
    Maximum number of pins for which a local same-sheet net is allowed to
    attempt explicit wiring.

    IMPORTANT:
        This is only a FANOUT limit.
        It is NOT the final decision.

    The final decision is also based on topology / placement / routing safety.
    """
    try:
        return max(
            2,
            int(os.environ.get("SKIDL_WIRE_MAX_LOCAL_PINS", "5")),
        )
    except (TypeError, ValueError):
        return 5


def _wire_min_local_pins():
    """
    Minimum number of real pins required before an explicit wire is useful.

    1-pin nets are never routed as wires.
    """
    return 2


def _is_real_pin(pin):
    """Return True when pin belongs to a real circuit part."""
    if pin is None:
        return False

    part = getattr(pin, "part", None)
    if part is None:
        return False

    ref_prefix = (getattr(part, "ref_prefix", "") or "").upper()

    # NetTerminal is synthetic and is not a real circuit pin.
    if ref_prefix == "NT":
        return False

    return True


def _real_pins(net):
    """Return only real pins from a net."""
    return [
        pin
        for pin in getattr(net, "pins", [])
        if _is_real_pin(pin)
        and not getattr(pin, "stub", False)
    ]


def _net_pin_count(net):
    """Number of real, non-stubbed pins on a net."""
    return len(_real_pins(net))


def _safe_pin_position(pin):
    """
    Best-effort extraction of a pin position.

    Different SKiDL/KiCad versions can expose geometry slightly differently,
    so this function intentionally fails soft.
    """
    try:
        pos = getattr(pin, "position", None)
        if pos is not None:
            return pos
    except Exception:
        pass

    try:
        pos = getattr(pin, "pos", None)
        if pos is not None:
            return pos
    except Exception:
        pass

    return None


def _xy(obj):
    """Best-effort conversion of a geometry object to (x, y)."""
    if obj is None:
        return None

    try:
        return float(obj.x), float(obj.y)
    except Exception:
        pass

    try:
        return float(obj[0]), float(obj[1])
    except Exception:
        pass

    return None


def _manhattan_distance(a, b):
    """Return Manhattan distance between two points, or None."""
    pa = _xy(a)
    pb = _xy(b)

    if pa is None or pb is None:
        return None

    return abs(pa[0] - pb[0]) + abs(pa[1] - pb[1])


def _net_span(net):
    """
    Return approximate Manhattan span of the net.

    This is deliberately conservative. If geometry isn't available, return
    None and let the router decide.
    """
    pins = _real_pins(net)

    points = []
    for pin in pins:
        p = _safe_pin_position(pin)
        xy = _xy(p)
        if xy is not None:
            points.append(xy)

    if len(points) < 2:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]

    return (max(xs) - min(xs)) + (max(ys) - min(ys))


def _net_is_locally_compact(net):
    """
    Decide whether a multi-pin net is geometrically compact enough to make an
    explicit wire attempt worthwhile.

    This is NOT a proof that routing is collision-free.
    It is only an early safety filter.
    """
    span = _net_span(net)

    if span is None:
        # No geometry information -> don't reject.
        # Router remains authoritative.
        return True

    try:
        max_span = float(
            os.environ.get("SKIDL_MAX_LOCAL_NET_SPAN", "1200")
        )
    except (TypeError, ValueError):
        max_span = 1200.0

    return span <= max_span


def _net_has_duplicate_pin_objects(net):
    """
    Detect malformed topology where the same physical pin appears more than
    once in the net.
    """
    ids = []
    for pin in _real_pins(net):
        ids.append(id(pin))

    return len(ids) != len(set(ids))


def _net_wire_candidate(net):
    """
    Topology-level decision.

    Returns:
        True  -> explicit wire is a candidate.
        False -> label/terminal is preferred.

    This function NEVER claims that routing is collision-free.
    """
    pins = _real_pins(net)
    count = len(pins)

    if count < _wire_min_local_pins():
        return False

    if _net_has_duplicate_pin_objects(net):
        return False

    if count > _wire_max_local_pins():
        return False

    if not _net_is_locally_compact(net):
        return False

    return True


# ---------------------------------------------------------------------------
# Schematic node
# ---------------------------------------------------------------------------

@export_to_all
class SchNode(Placer, Router):
    """
    Data structure for holding information about a node in the circuit
    hierarchy.

    Layout philosophy:

        INPUT / CONNECTOR
                |
                v
        SIGNAL PROCESSING
                |
                v
              OUTPUT

        POWER -> TOP
        GND   -> BOTTOM

    Net philosophy:

        1 pin      -> stub / label
        2 pins     -> direct wire
        3 pins     -> T junction
        4 pins     -> 4-way junction
        5 pins     -> multi-drop wire when safe
        6+ pins    -> label unless explicitly safe

    The actual wire geometry remains the responsibility of Router.
    """

    filename_sz = 20
    name_sz = 40

    def __init__(
        self,
        circuit=None,
        tool_module=None,
        filepath=".",
        top_name="",
        title="",
        flatness=0.0,
    ):
        self.parts = []

        self.filepath = filepath
        self.top_name = top_name

        self.parent = None

        self.children = defaultdict(
            lambda: type(self)(
                None,
                tool_module,
                filepath,
                top_name,
                title,
                flatness,
            )
        )

        self.sheet_name = None
        self.sheet_filename = None

        self.title = title
        self.flatness = flatness

        self.flattened = False

        # Functional block.
        self.is_group = False

        self.tool_module = tool_module

        # Routed wire segments.
        self.wires = defaultdict(list)

        # Junction points.
        self.junctions = defaultdict(list)

        self.tx = Tx()
        self.bbox = BBox()

        if circuit:
            self.add_circuit(circuit)

    # -----------------------------------------------------------------------
    # Hierarchy
    # -----------------------------------------------------------------------

    def get_or_add_child(self, name):
        """Get or create a child node and attach it to this node."""
        child = self.children[name]
        child.parent = self
        return child

    def _part_hierarchy_key(self, part):
        """
        Return hierarchy key for a part.

        Functional block tags are treated as an additional hierarchy level.
        """
        names = list(part.hiertuple)

        group = getattr(part, "group", None)

        if group:
            group = re.sub(
                r"[^\w.+-]+",
                "_",
                str(group),
            ).strip("_")

            if group:
                names.append(group)

        return tuple(names)

    def find_node_with_part(self, part):
        """Find the node that owns the given part."""
        level_names = list(part.hiertuple)

        group = getattr(part, "group", None)

        if group:
            group = re.sub(
                r"[^\w.+-]+",
                "_",
                str(group),
            ).strip("_")

            if group:
                level_names.append(group)

        node = self

        for level_name in level_names[1:]:
            node = node.children[level_name]

        assert part in node.parts

        return node

    def add_part(self, part, level=0):
        """Add part to the appropriate hierarchy level."""

        level_names = list(part.hiertuple)

        group = getattr(part, "group", None)

        if group:
            group = re.sub(
                r"[^\w.+-]+",
                "_",
                str(group),
            ).strip("_")

            if group:
                level_names.append(group)

        part_level = len(level_names) - 1

        assert part_level >= level

        self.name = level_names[level]

        base_filename = "_".join(
            [self.top_name] + level_names[1 : level + 1]
        ) + ".sch"

        self.sheet_filename = base_filename

        if part_level == level:

            if not part.unit:
                self.parts.append(part)

            else:
                for p in part.unit.values():
                    self.parts.append(p)

        else:

            child_node = self.get_or_add_child(
                level_names[level + 1]
            )

            if group and (
                level + 1 == len(level_names) - 1
            ):
                child_node.is_group = True

            child_node.add_part(
                part,
                level + 1,
            )

    # -----------------------------------------------------------------------
    # Net topology helpers
    # -----------------------------------------------------------------------

    def _node_key_for_pin(self, pin):
        """Hierarchy key for a pin's owning part."""
        return self._part_hierarchy_key(pin.part)

    def _net_crosses_nodes(self, net):
        """True if real pins of a net belong to different hierarchy nodes."""
        pins = _real_pins(net)

        if not pins:
            return False

        first_key = self._node_key_for_pin(pins[0])

        for pin in pins[1:]:
            if self._node_key_for_pin(pin) != first_key:
                return True

        return False

    def _count_pins_per_node(self, net):
        """Count real net pins grouped by hierarchy node."""
        counts = {}

        for pin in _real_pins(net):

            key = self._node_key_for_pin(pin)

            counts[key] = counts.get(key, 0) + 1

        return counts

    # -----------------------------------------------------------------------
    # Net handling
    # -----------------------------------------------------------------------

    def _stub_single_pin_net(self, net):
        """
        A one-pin named net does not need a NetTerminal.

        Keep its intent as a pin-attached label/stub.
        """
        net._stub = True

        for pin in getattr(net, "pins", []):
            pin.stub = True

    def _should_wire_local_net(self, net):
        """
        Main decision point for local same-sheet nets.

        This is deliberately topology-aware.

        Example:

            A -- R1 -- B
                    |
                    C
                    |
                   GND

        B has 3 real connections.

        It is still one net and therefore should become a junction, NOT three
        independent labels.
        """
        pins = _real_pins(net)

        count = len(pins)

        if count < 2:
            return False

        if self._net_crosses_nodes(net):
            return False

        return _net_wire_candidate(net)

    def _add_boundary_terminals(self, net):
        """
        Add one terminal per hierarchy node that contains at least two pins.

        A node containing one pin receives a direct stub/label instead.
        """
        pin_count = self._count_pins_per_node(net)

        visited = set()

        for pin in _real_pins(net):

            key = self._node_key_for_pin(pin)

            if pin_count.get(key, 0) < 2:
                pin.stub = True
                continue

            if key in visited:
                continue

            self.find_node_with_part(
                pin.part
            ).add_terminal(net)

            visited.add(key)

    def add_circuit(self, circuit):
        """
        Add circuit parts and prepare nets.

        This method intentionally does NOT manually construct every wire
        segment. Router remains responsible for actual geometry.

        The job here is:

            topology
              ->
            wire/label decision
              ->
            terminals
              ->
            Router
        """

        # ---------------------------------------------------------------
        # Parts
        # ---------------------------------------------------------------

        for part in circuit.parts:
            self.add_part(part)

        from skidl.net import NCNet

        # ---------------------------------------------------------------
        # Nets
        # ---------------------------------------------------------------

        for net in circuit.nets:

            # Already stubbed.
            if getattr(net, "stub", False):
                continue

            # No-connect net.
            if isinstance(net, NCNet):
                continue

            real_pins = _real_pins(net)

            # -----------------------------------------------------------
            # 1-pin net
            # -----------------------------------------------------------

            if len(real_pins) == 1:

                self._stub_single_pin_net(net)

                continue

            # -----------------------------------------------------------
            # Cross hierarchy net
            # -----------------------------------------------------------

            if self._net_crosses_nodes(net):

                self._add_boundary_terminals(net)

                continue

            # -----------------------------------------------------------
            # Local net
            # -----------------------------------------------------------

            if self._should_wire_local_net(net):

                # IMPORTANT:
                #
                # Do NOT add a NetTerminal here.
                #
                # Router sees all pins of the same net and creates:
                #
                # 2 pins -> line
                # 3 pins -> T
                # 4 pins -> junction
                # 5 pins -> multi-drop
                #
                # subject to Router collision handling.
                continue

            # -----------------------------------------------------------
            # Named / large / unsafe local net
            # -----------------------------------------------------------

            # A large local net should not generate one label per pin.
            #
            # If there are multiple pins, make one terminal for the node.
            #
            # This gives:
            #
            #     local wires -> one label
            #
            # rather than:
            #
            #     LABEL LABEL LABEL LABEL
            #
            self._add_boundary_terminals(net)

        # ---------------------------------------------------------------
        # Hierarchy flattening
        # ---------------------------------------------------------------

        self.flatten(self.flatness)

    # -----------------------------------------------------------------------
    # Terminals
    # -----------------------------------------------------------------------

    def add_terminal(self, net):
        """Add one synthetic NetTerminal for a net."""
        from .net_terminal import NetTerminal

        nt = NetTerminal(
            net,
            self.tool_module,
        )

        self.parts.append(nt)

    # -----------------------------------------------------------------------
    # Internal nets
    # -----------------------------------------------------------------------

    def get_internal_nets(self):
        """Return nets containing at least one real pin in this node."""

        processed_nets = []
        internal_nets = []

        for part in self.parts:

            for part_pin in part:

                if getattr(part_pin, "stub", False):
                    continue

                if not part_pin.is_connected():
                    continue

                net = part_pin.net

                if net in processed_nets:
                    continue

                processed_nets.append(net)

                if getattr(net, "stub", False):
                    continue

                for net_pin in net.pins:

                    if net_pin.part in self.parts:

                        internal_nets.append(net)

                        break

        return internal_nets

    def get_internal_pins(self, net):
        """Return non-stubbed pins belonging to this node."""

        if getattr(net, "stub", False):
            return []

        return [
            pin
            for pin in net.pins
            if not getattr(pin, "stub", False)
            and pin.part in self.parts
        ]

    # -----------------------------------------------------------------------
    # Boundary nets
    # -----------------------------------------------------------------------

    def get_boundary_nets(self):
        """
        Return nets that connect this node to something outside this node.
        """

        node_part_ids = {
            id(part)
            for part in self.parts
        }

        boundary = []
        seen = set()

        for part in self.parts:

            for pin in part:

                if not pin.is_connected():
                    continue

                net = pin.net

                if id(net) in seen:
                    continue

                seen.add(id(net))

                external_pins = [
                    p
                    for p in net.pins
                    if id(p.part) not in node_part_ids
                ]

                if external_pins:
                    boundary.append(net)

        return boundary

    # -----------------------------------------------------------------------
    # Bounding boxes
    # -----------------------------------------------------------------------

    def external_bbox(self):
        """Return bounding box for hierarchical sheet representation."""

        bbox = BBox(
            Point(0, 0),
            Point(500, 500),
        )

        bbox.add(
            Point(
                len(
                    "File: " + self.sheet_filename
                ) * self.filename_sz,
                0,
            )
        )

        bbox.add(
            Point(
                len(
                    "Sheet: " + self.name
                ) * self.name_sz,
                0,
            )
        )

        return bbox.resize(
            Vector(100, 100)
        )

    def internal_bbox(self):
        """Return bounding box of actual circuitry."""

        bbox = BBox()

        for obj in chain(
            self.parts,
            self.children.values(),
        ):

            tx_bbox = obj.bbox * obj.tx

            bbox.add(tx_bbox)

        return bbox.resize(
            Vector(100, 100)
        )

    def calc_bbox(self):
        """Compute current node bounding box."""

        if self.flattened:
            self.bbox = self.internal_bbox()

        else:
            self.bbox = self.external_bbox()

        return self.bbox

    # -----------------------------------------------------------------------
    # Hierarchy flattening
    # -----------------------------------------------------------------------

    def flatten(self, flatness=0.0):
        """
        Flatten hierarchy according to flatness.

        Functional groups stay inline as boxes.
        Real subcircuits can become hierarchical sheets.
        """

        # First flatten children.
        for child in self.children.values():
            child.flatten(flatness)

        # Functional groups stay on current sheet.
        for child in self.children.values():

            if getattr(child, "is_group", False):
                child.flattened = True

        # Only actual subcircuits participate in sheet decision.
        sheet_children = {
            child_id: child
            for child_id, child in self.children.items()
            if not getattr(child, "is_group", False)
        }

        # ---------------------------------------------------------------
        # Complexity
        # ---------------------------------------------------------------

        self.complexity = sum(
            len(part)
            for part in self.parts
        )

        for child in self.children.values():

            if getattr(child, "is_group", False):

                self.complexity += child.complexity

        # ---------------------------------------------------------------
        # Slack
        # ---------------------------------------------------------------

        child_complexity = sum(
            child.complexity
            for child in sheet_children.values()
        )

        slack = child_complexity * flatness

        # ---------------------------------------------------------------
        # Group child types
        # ---------------------------------------------------------------

        child_types = defaultdict(list)

        for child_id, child in sheet_children.items():

            child_type = re.sub(
                r"\d+$",
                "",
                child_id,
            )

            child_types[child_type].append(child)

        child_type_sizes = {}

        for child_type, children in child_types.items():

            child_type_sizes[child_type] = sum(
                child.complexity
                for child in children
            )

        sorted_child_type_sizes = sorted(
            child_type_sizes.items(),
            key=lambda item: item[1],
        )

        # ---------------------------------------------------------------
        # Flatten
        # ---------------------------------------------------------------

        for child_type, child_type_size in sorted_child_type_sizes:

            if child_type_size <= slack:

                for child in child_types[child_type]:

                    child.flattened = True

                slack -= child_type_size

            else:

                for child in child_types[child_type]:

                    child.flattened = False

    # -----------------------------------------------------------------------
    # Statistics
    # -----------------------------------------------------------------------

    def collect_stats(self, **options):
        """Return total routed wire length."""

        def get_wire_length(node):

            wire_length = 0

            # Child sheets.
            for child in node.children.values():

                wire_length += get_wire_length(child)

            # Current node.
            for wire_segments in node.wires.values():

                for seg in wire_segments:

                    len_x = abs(
                        seg.p1.x - seg.p2.x
                    )

                    len_y = abs(
                        seg.p1.y - seg.p2.y
                    )

                    wire_length += (
                        len_x + len_y
                    )

            return wire_length

        return f"{get_wire_length(self)}\n"
