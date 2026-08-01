"""
src/skidl/board/pour.py

Dynamic copper-pour planning: decide -- from the board itself, with no
hardcoded net names or layer numbers -- which nets are carried by a pour
instead of routed, and which copper layer each pour lands on.

Every parameter derives from the board: net role (net_classify), current
(width_engine), pad count, and the ACTUAL stackup (board.layer_count).
Hand it a two-IC USB board or a forty-IC controller and it adapts.

This module owns only the DECISIONS (which nets, which layers). The
geometry that realises them -- stitching vias and zone outlines -- and
the pipeline wiring live in their own steps, all fed by the (assignments,
reroute, warnings) this returns. The acceptance test for the realised pour is DRC:
`unconnected == 0 and errors == 0` on whatever board was built -- ipcd356
(verify_board) is assignment-level and cannot see whether a pour actually
ties the pads, so it is NOT the pour gate.
"""

from __future__ import annotations

from skidl.board.model import BoardModel, Net, Zone, Via
from skidl.board.geom import point_in_polygon, bbox_of, bbox_contains
from skidl.schematics.net_classify import classify_net_role
from skidl.board.width_engine import estimate_net_current, required_width

# KiCad fills the HIGHER-priority zone first and pulls lower ones back, so
# ground (a reference plane that must own contested outer copper) outranks
# power (which lives only on its own dedicated inner plane -> no overlap).
GROUND_PRIORITY = 1
POWER_PRIORITY = 0


def copper_layers(board: BoardModel) -> list:
    """Ordered copper stack F.Cu -> inner -> B.Cu for the board's ACTUAL
    layer count (Board Setup is the source of truth). Matches the stack
    dsn_export emits so pours and routing agree on layer names."""
    n = max(1, int(board.layer_count or 2))
    if n == 1:
        return ["F.Cu"]
    return ["F.Cu"] + [f"In{i}.Cu" for i in range(1, n - 1)] + ["B.Cu"]


def should_pour(net: Net, board: BoardModel, currents: dict = None) -> tuple:
    """(pour?: bool, role: "ground"|"power"|None) for one net.

    Classify-driven with current-aware promotion/demotion so the CIRCUIT
    decides, not a name list:
      * single-pad nets (test points, antennas) are never poured;
      * ground is always poured -- return-path integrity;
      * a power rail is poured only when it would need a fat trace
        (required width > 1.5x its class default) or fans out widely
        (> 6 pads); a low-current few-pad rail is cheaper to route;
      * a signal net that actually carries current (motor drive, LED
        array: required width > 2x default) promotes to a power pour.
    """
    pad_count = len(net.pad_refs)
    if pad_count < 2:
        return False, None

    role = classify_net_role(net)
    if role == "ground":
        return True, "ground"

    default_trace = _class_width(board, net.net_class)
    amps, _ = estimate_net_current(net.name, net.net_class, currents)
    min_width = required_width(amps)

    if role == "power":
        if min_width > default_trace * 1.5:
            return True, "power"
        if pad_count > 6:
            return True, "power"
        return False, None    # low-current, few-pad rail -> route it

    # Signal net: promote only if it genuinely carries current.
    if amps > 0 and min_width > default_trace * 2.0:
        return True, "power"

    return False, None


def plan_pours(board: BoardModel, currents: dict = None) -> dict:
    """{net_name: role} for every net that should be poured on this board."""
    out = {}
    for net in board.nets.values():
        do, role = should_pour(net, board, currents)
        if do:
            out[net.name] = role
    return out


def _pad_cu_layers(fp, pad, all_cu) -> set:
    """Copper layers one pad occupies: a through-hole pad spans ALL of
    them; an SMD pad sits on its footprint's side."""
    if pad.pad_type == "thru_hole" or pad.drill > 0:
        return set(all_cu)
    return {fp.layer}                # "F.Cu" (top) / "B.Cu" (bottom)


def pad_layers_of(net: Net, board: BoardModel) -> set:
    """Union of the copper layers this net's pads occupy. Pouring a net on
    every layer its pads sit on lets each pad tie to same-layer fill (no
    fragile via-to-a-distant-pad needed)."""
    all_cu = copper_layers(board)
    occupied = set()
    for ref, number in net.pad_refs:
        try:
            fp = board.footprint(ref)
        except KeyError:
            continue
        pad = _find_pad(fp, number)
        if pad is not None:
            occupied |= _pad_cu_layers(fp, pad, all_cu)
    return occupied


def assign_pour_layers(board: BoardModel, pour_decisions: dict) -> tuple:
    """Map poured nets to copper layers, derived from the board itself.

    Returns (assignments, reroute, warnings):
      assignments {net_name: [layer, ...]} -- ordered F->B.
      reroute     [net_name, ...]          -- poured-role nets this stackup
                    can't host (2-layer/odd power); go BACK into the DSN so
                    no net falls between "excluded" and "not poured".
      warnings    [str, ...]               -- a poured net with a pad on a
                    layer NOT in its pour set (rare: no pour reaches it).

    Ground vs power is ASYMMETRIC (ground = reference plane, power =
    distribution plane):
      * ground pours on every layer its pads occupy + its dedicated
        plane(s), minus the power plane -- each ground pad ties to
        same-layer fill.
      * power pours ONLY on its dedicated inner plane, never on shared
        outer layers; its outer pads reach the plane through normal vias
        (routed), so ground owns the outer copper uncontested. This avoids
        the overlap conflict and the via-in-pad (VIPPO) manufacturing tier.

    Dedicated planes, never assumed:
      1-2 layer: no plane -- ground pours its pad layers (F.Cu/B.Cu),
                 power is rerouted.
      4 layer  : ground plane In1.Cu, power plane In2.Cu.
      6+ layer : ground planes In1.Cu + second-from-back, power plane In2.Cu.
      odd (3,5): no plane -- ground pours pad layers, power rerouted.
    """
    all_cu = copper_layers(board)
    planes = _dedicated_planes(all_cu)
    order = {name: i for i, name in enumerate(all_cu)}
    gnd_planes = set(planes["ground"])
    pwr_planes = set(planes["power"] or [])

    assignments, reroute, warnings = {}, [], []
    for name, role in pour_decisions.items():
        net = board.nets.get(name)
        if net is None:
            continue

        if role == "power" and planes["power"] is None:
            reroute.append(name)          # 2-layer/odd: route power
            continue

        # GROUND is a REFERENCE plane -> pour on every layer its pads
        # occupy (so each pad ties to same-layer fill) plus the dedicated
        # ground plane(s), but never the power plane.
        # POWER is a DISTRIBUTION plane -> pour ONLY on its dedicated inner
        # plane, NOT on shared outer layers. Its outer-layer pads reach the
        # plane through normal vias (router/stubs), so ground can own the
        # outer copper uncontested -- no overlap conflict, no via-in-pad
        # (VIPPO) requirement, no stranded pads.
        if role == "ground":
            occupied = pad_layers_of(net, board)
            pour_set = (occupied | gnd_planes) - pwr_planes
        else:
            pour_set = set(pwr_planes)    # dedicated plane only
        pour_on = sorted(pour_set, key=lambda l: order.get(l, 99))
        if not pour_on:
            reroute.append(name)          # no resolvable pour target -> route
            continue
        assignments[name] = pour_on

        # Warn only about GROUND pads with no poured layer (a real open).
        # Power's outer-layer pads are expected off-pour -- they reach the
        # plane by routing, handled at export_dsn time.
        if role == "ground":
            stranded = []
            for ref, number in net.pad_refs:
                try:
                    fp = board.footprint(ref)
                except KeyError:
                    continue
                pad = _find_pad(fp, number)
                if pad is None:
                    continue
                if _pad_cu_layers(fp, pad, all_cu).isdisjoint(pour_set):
                    stranded.append(f"{ref}.{number}")
            if stranded:
                warnings.append(
                    f"pour {name}: pad(s) {stranded} on no poured layer -- "
                    "add a pour there or route this net manually")
    return assignments, reroute, warnings


def prepare_power_pours(board: BoardModel, currents: dict = None,
                        spacing_mm: float = 5.0) -> tuple:
    """One call from pipeline.build_pcb (after placement, before routing):
    decide pours, assign layers, emit zones + stitching vias, and return the
    nets to EXCLUDE from the DSN.

    Only GROUND is excluded -- it pours on every layer its pads occupy, so
    the router has nothing to add. POWER stays IN the DSN: it pours only on
    its dedicated inner plane, and its outer-layer pads are routed to vias
    that land on that plane (option b -- Specctra has no per-layer net
    exclusion; confirmed against the .dsn grammar, every layer is
    (type signal) and net classes are global). The asymmetry in
    assign_pour_layers makes the exclusion set fall out for free: exactly
    the ground nets, whose every pad already sits on a poured layer.

    Returns (exclude, zones, vias, warnings)."""
    decisions = plan_pours(board, currents)
    assignments, _reroute, warns = assign_pour_layers(board, decisions)
    zones = emit_zones(board, assignments, decisions)
    vias, vwarns = plan_stitching_vias(board, assignments, spacing_mm)
    exclude = {name for name, role in decisions.items()
               if role == "ground" and name in assignments}
    return exclude, zones, vias, warns + vwarns


def emit_zones(board: BoardModel, assignments: dict,
               pour_decisions: dict) -> list:
    """One Zone per (net, layer) filling the board outline. Ground gets the
    higher priority so it wins outer-layer overlaps; pads connect SOLID
    (thermal reliefs starve on auto-routed boards). Returns list[Zone]."""
    outline = board.outline or []
    if not outline:
        board.warnings.append("pour: board has no outline -- no zones emitted")
        return []
    default = (board.net_classes or {}).get("Default", {})
    clearance = max(float(default.get("clearance", 0.15)), 0.25)
    zones = []
    for name, layers in assignments.items():
        role = pour_decisions.get(name)
        priority = GROUND_PRIORITY if role == "ground" else POWER_PRIORITY
        for layer in layers:
            zones.append(Zone(
                net=name, layer=layer, outline=list(outline),
                priority=priority, connect_pads="yes", clearance=clearance,
            ))
    return zones


def plan_stitching_vias(board: BoardModel, assignments: dict,
                        spacing_mm: float = 5.0) -> tuple:
    """Area-grid vias tying a net's MULTIPLE pour layers together for
    return-path integrity. Only nets poured on >= 2 layers are stitched --
    a single-layer pour has nothing to tie (and a via to one layer is a DRC
    error). Vias land on a grid inside the outline, kept clear of courtyards,
    keepouts and each other by the via's FINISHED outer diameter (via_size,
    not drill). `spacing_mm` default 5 mm suits return-path inductance at
    typical board frequencies; drop to 2-3 mm for RF / high-speed digital.
    Returns (vias, warnings)."""
    outline = board.outline or []
    vias, warnings = [], []
    if not outline:
        return vias, warnings

    default = (board.net_classes or {}).get("Default", {})
    via_size = float(default.get("via_size", 0.8))     # FINISHED outer diameter
    via_drill = float(default.get("via_drill", 0.4))
    clearance = float(default.get("clearance", 0.15))
    margin = via_size / 2.0 + clearance                # via edge -> obstacle

    obstacles = []
    for fp in board.footprints:
        bb = fp.courtyard_world_bbox()
        if bb is None and fp.pads:
            pts = [fp.pad_world_xy(p) for p in fp.pads]
            bb = (min(x for x, _ in pts), min(y for _, y in pts),
                  max(x for x, _ in pts), max(y for _, y in pts))
        if bb:
            obstacles.append(bb)
    for ko in board.keepouts:
        obstacles.append(ko.bbox())

    minx, miny, maxx, maxy = bbox_of(outline)
    min_via_gap = spacing_mm * 0.5
    for name, layers in assignments.items():
        if len(layers) < 2:
            continue                                   # nothing to stitch
        via_layers = (layers[0], layers[-1])           # span outer pours -> ties inner
        placed = 0
        y = miny + spacing_mm / 2.0
        while y <= maxy:
            x = minx + spacing_mm / 2.0
            while x <= maxx:
                pt = (x, y)
                if (point_in_polygon(pt, outline)
                        and not any(bbox_contains(bb, pt, margin) for bb in obstacles)
                        and not _too_close(pt, vias, min_via_gap)):
                    vias.append(Via(net=name, at=pt, size=via_size,
                                    drill=via_drill, layers=via_layers))
                    placed += 1
                x += spacing_mm
            y += spacing_mm
        if placed == 0:
            warnings.append(
                f"stitch {name}: no legal via location on the {spacing_mm}mm "
                "grid (dense board) -- return path may be weak")
    return vias, warnings


def _too_close(pt, vias, min_d) -> bool:
    md2 = min_d * min_d
    for v in vias:
        dx = pt[0] - v.at[0]
        dy = pt[1] - v.at[1]
        if dx * dx + dy * dy < md2:
            return True
    return False


def _dedicated_planes(all_cu: list) -> dict:
    """{"ground": [layer...], "power": [layer...] | None} for the stackup.
    None power => power is rerouted, not poured."""
    n = len(all_cu)
    if n <= 2:
        return {"ground": [], "power": None}
    if n == 4:
        return {"ground": [all_cu[1]], "power": [all_cu[2]]}
    if n >= 6:
        return {"ground": [all_cu[1], all_cu[-2]], "power": [all_cu[2]]}
    return {"ground": [], "power": None}         # n == 3, 5


def _find_pad(fp, number):
    for p in fp.pads:
        if p.number == number:
            return p
    return None


def _class_width(board: BoardModel, class_name: str) -> float:
    ncs = board.net_classes or {}
    cls = ncs.get(class_name) or ncs.get("Default") or {}
    return float(cls.get("width", 0.25))
