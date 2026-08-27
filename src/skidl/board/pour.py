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

import math

from skidl.board.model import BoardModel, Net, Zone, Via, PadConnectOverride
from skidl.board.geom import (point_in_polygon, bbox_of, bbox_contains,
                              point_segment_distance)
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

    Returns (exclude, zones, vias, overrides, warnings)."""
    decisions = plan_pours(board, currents)
    assignments, _reroute, warns = assign_pour_layers(board, decisions)
    zones = emit_zones(board, assignments, decisions)
    vias, vwarns = plan_stitching_vias(board, assignments, spacing_mm)
    overrides = compute_pad_overrides(board, assignments)
    exclude = {name for name, role in decisions.items()
               if role == "ground" and name in assignments}
    return exclude, zones, vias, overrides, warns + vwarns


def compute_pad_overrides(board: BoardModel, assignments: dict) -> list:
    """Per-pad zone-connect overrides for poured nets. Zones connect pads
    SOLID by default (the starvation lesson); this overrides THROUGH-HOLE
    pads to thermal relief (zone_connect 1) so they stay hand-solderable --
    a solid pour wicks heat off a THT joint. SMD pads keep the solid
    default (no override). Returns list[PadConnectOverride]."""
    overrides = []
    for name in assignments:                     # poured nets only
        net = board.nets.get(name)
        if net is None:
            continue
        for ref, number in net.pad_refs:
            try:
                fp = board.footprint(ref)
            except KeyError:
                continue
            pad = _find_pad(fp, number)
            if pad is None:
                continue
            if pad.pad_type == "thru_hole" or pad.drill > 0:
                overrides.append(PadConnectOverride(ref, number, 1))   # thermal
    return overrides


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
    keepouts, each other AND every piece of OTHER-net copper -- routed tracks,
    routed vias and pads -- by the via's finished radius + that copper's own
    half-size + the clearance rule. Same-net (the pour net) copper is NOT an
    obstacle: the via is meant to bond to it. Call this AFTER routing so the
    routed geometry is present; a via placed blind (pre-route) gets stamped on
    top of a signal track and shows up as a short + a dangling via (the pour
    then carves around the signal and strands the via). `spacing_mm` default
    5 mm suits return-path inductance at typical board frequencies; drop to
    2-3 mm for RF / high-speed digital. Returns (vias, warnings)."""
    outline = board.outline or []
    vias, warnings = [], []
    if not outline:
        return vias, warnings

    default = (board.net_classes or {}).get("Default", {})
    via_size = float(default.get("via_size", 0.8))     # FINISHED outer diameter
    via_drill = float(default.get("via_drill", 0.4))
    clearance = float(default.get("clearance", 0.15))
    via_r = via_size / 2.0
    margin = via_r + clearance                         # via edge -> obstacle

    # Coarse obstacles (whole part / keepout) -- keep a via out of any
    # footprint's courtyard and every rule area, regardless of net.
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

    # BOUNDED, DENSITY-DRIVEN stitching (NOT an area carpet-bomb). Return-path
    # integrity needs a SPARSE set of ties -- a handful even on a big board --
    # not one via per grid node (that gave ~W*H/25 vias: 240 on a 74x100 mm
    # board). We budget vias by board AREA at ~1 per MM2_PER_VIA, hard-capped,
    # then widen the grid so it yields about that many. Anywhere the ground
    # pour ACTUALLY fragments is caught separately + need-based by the
    # post-route island healer (find_ground_islands / plan_island_vias), so a
    # sparse grid here can never strand a pin. Fully dynamic -- scales with the
    # board, identical rule on a 20 mm or 300 mm board.
    MM2_PER_VIA = 400.0        # ~1 stitch via per 4 cm2 of pour
    MAX_STITCH_VIAS = 48       # absolute ceiling regardless of board size
    MIN_STITCH_VIAS = 4        # a 2-layer pour still wants a few ties
    area = max(1.0, (maxx - minx) * (maxy - miny))
    budget = max(MIN_STITCH_VIAS, min(MAX_STITCH_VIAS, int(area / MM2_PER_VIA)))
    # Widen the grid so ~budget nodes fall inside the outline; never tighter
    # than the caller's spacing_mm (RF/high-speed can still ask for dense).
    eff_spacing = max(spacing_mm, (area / budget) ** 0.5)
    min_via_gap = eff_spacing * 0.5

    for name, layers in assignments.items():
        if len(layers) < 2:
            continue                                   # nothing to stitch
        via_layers = (layers[0], layers[-1])           # span outer pours -> ties inner
        placed = 0
        y = miny + eff_spacing / 2.0
        while y <= maxy and placed < budget:
            x = minx + eff_spacing / 2.0
            while x <= maxx and placed < budget:
                pt = (x, y)
                if (point_in_polygon(pt, outline)
                        and not any(bbox_contains(bb, pt, margin) for bb in obstacles)
                        and not _too_close(pt, vias, min_via_gap)
                        and _via_clears_copper(board, pt, name, via_r, clearance)):
                    vias.append(Via(net=name, at=pt, size=via_size,
                                    drill=via_drill, layers=via_layers))
                    placed += 1
                x += eff_spacing
            y += eff_spacing
        if placed == 0:
            warnings.append(
                f"stitch {name}: no legal via location on the "
                f"{eff_spacing:.1f}mm grid (dense board) -- island healer will "
                "add ties where the pour actually splits")
    return vias, warnings


def plan_stitching_vias_for(board: BoardModel, currents: dict = None,
                            spacing_mm: float = 5.0) -> tuple:
    """Recompute pour-layer assignments and plan collision-aware stitching
    vias against the board's CURRENT geometry. Call AFTER routing (so
    board.tracks / board.vias exist) to place vias that avoid every signal
    trace/via -- the fix for the post-route 'stamp vias on top' shorts and
    dangling vias. Returns (vias, warnings)."""
    decisions = plan_pours(board, currents)
    assignments, _reroute, _warns = assign_pour_layers(board, decisions)
    return plan_stitching_vias(board, assignments, spacing_mm)


def _too_close(pt, vias, min_d) -> bool:
    md2 = min_d * min_d
    for v in vias:
        dx = pt[0] - v.at[0]
        dy = pt[1] - v.at[1]
        if dx * dx + dy * dy < md2:
            return True
    return False


def _via_clears_copper(board, pt, net, via_r, clearance) -> bool:
    """True if a via of radius `via_r` on `net` at `pt` keeps `clearance` from
    every OTHER-net copper -- routed tracks, routed vias and pads. Same-net
    copper is fine (the via bonds to it). Shared by the grid stitcher and the
    ground-island healer."""
    px, py = pt
    for tr in board.tracks:
        if tr.net == net:
            continue
        need = via_r + tr.width / 2.0 + clearance
        if point_segment_distance(px, py, tr.start[0], tr.start[1],
                                  tr.end[0], tr.end[1]) < need:
            return False
    for ov in board.vias:
        if ov.net == net:
            continue
        need = via_r + ov.size / 2.0 + clearance
        if (px - ov.at[0]) ** 2 + (py - ov.at[1]) ** 2 < need * need:
            return False
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net is None or pad.net == net:
                continue
            cx, cy = fp.pad_world_xy(pad)
            need = via_r + max(pad.size_x, pad.size_y) / 2.0 + clearance
            if (px - cx) ** 2 + (py - cy) ** 2 < need * need:
                return False
    return True


def find_ground_islands(pcb_path, kicad_cli, gnd_nets) -> list:
    """Refill the board and return the (x, y) points where a GROUND pour has
    split into copper regions the fill did not tie together. Each is a KiCad
    'unconnected_items' ratsnest whose endpoints are ALL Zone copper of a
    ground net (no pad -> every pin is still connected; it is floating pour).
    Returns a de-duplicated list of positions to stitch."""
    import subprocess, json as _j, tempfile, os
    rpt = str(pcb_path) + ".island.drc.json"
    try:
        subprocess.run([str(kicad_cli), "pcb", "drc", "--format", "json",
                        "--severity-all", "--refill-zones", "--save-board",
                        "--output", rpt, str(pcb_path)],
                       stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=300)
        rep = _j.loads(open(rpt, encoding="utf-8", errors="replace").read())
    except Exception:
        return []
    finally:
        try:
            os.remove(rpt)
        except OSError:
            pass
    gset = set(gnd_nets)
    pts, seen = [], set()
    for u in rep.get("unconnected_items", []):
        items = u.get("items", [])
        if not items:
            continue
        descs = [it.get("description") or "" for it in items]
        if not all("Zone" in d for d in descs):
            continue                                   # a real pad ratsnest
        if not any(f"[{g}]" in d for d in descs for g in gset):
            continue                                   # not a ground net
        for it in items:
            p = it.get("pos") or {}
            if "x" in p:
                key = (round(p["x"], 2), round(p["y"], 2))
                if key not in seen:
                    seen.add(key)
                    pts.append((float(p["x"]), float(p["y"])))
    return pts


def plan_island_vias(board, positions, gnd_net, layers,
                     search_mm=6.0, step_mm=0.5, edge_mm=1.0) -> list:
    """For each islanded-ground position, find a nearby spot that clears all
    other-net copper AND the board edge, and return a ground via there tying
    the island to the plane. The DRC point itself sits on the island BOUNDARY
    (against a signal / the edge -- exactly why no stitch via landed), so we
    spiral outward for the first clear spot. Skips a position when the whole
    neighbourhood is blocked (reported to the caller, not forced)."""
    outline = board.outline or []
    if not outline or len(layers) < 2:
        return []
    default = (board.net_classes or {}).get("Default", {})
    via_size = float(default.get("via_size", 0.8))
    via_drill = float(default.get("via_drill", 0.4))
    clearance = float(default.get("clearance", 0.15))
    via_r = via_size / 2.0
    via_layers = (layers[0], layers[-1])
    new = []
    # concentric ring offsets, nearest first
    rings = [(0.0, 0.0)]
    r = step_mm
    while r <= search_mm:
        k = max(8, int(2 * math.pi * r / step_mm))
        for i in range(k):
            a = 2 * math.pi * i / k
            rings.append((r * math.cos(a), r * math.sin(a)))
        r += step_mm
    for (ox, oy) in positions:
        for (dx, dy) in rings:
            pt = (ox + dx, oy + dy)
            if not _point_inside_with_margin(pt, outline, via_r + edge_mm):
                continue
            if _too_close(pt, board.vias, via_r * 2 + clearance):
                continue
            if _too_close(pt, new, via_r * 2 + clearance):
                continue
            if _via_clears_copper(board, pt, gnd_net, via_r, clearance):
                new.append(Via(net=gnd_net, at=pt, size=via_size,
                               drill=via_drill, layers=via_layers))
                break
    return new


def _point_inside_with_margin(pt, outline, margin) -> bool:
    """Inside the outline AND at least `margin` from every edge (keeps a via
    off the board edge -> no copper_edge_clearance error)."""
    if not point_in_polygon(pt, outline):
        return False
    px, py = pt
    n = len(outline)
    for i in range(n):
        x1, y1 = outline[i]
        x2, y2 = outline[(i + 1) % n]
        if point_segment_distance(px, py, x1, y1, x2, y2) < margin:
            return False
    return True


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
