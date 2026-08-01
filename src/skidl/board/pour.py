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

from skidl.board.model import BoardModel, Net
from skidl.schematics.net_classify import classify_net_role
from skidl.board.width_engine import estimate_net_current, required_width


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

    Layer choice = (every layer the net's pads occupy) + a dedicated plane
    per the stackup, so each pad ties to same-layer fill. GND wins copper
    where it overlaps power (priority set in the zone emitter). Dedicated
    planes, never assumed:
      1-2 layer: no plane -- ground pours its pad layers (F.Cu/B.Cu),
                 power is rerouted (a power plane on 2 layers would fight
                 the ground it shares copper with).
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

        # Pour on every layer the pads occupy PLUS this role's dedicated
        # plane, but never on the OTHER role's plane (a THT ground pad
        # spans all layers, yet ground must not claim the power plane).
        occupied = pad_layers_of(net, board)
        my_plane = gnd_planes if role == "ground" else pwr_planes
        other_plane = pwr_planes if role == "ground" else gnd_planes
        pour_set = (occupied | my_plane) - other_plane
        pour_on = sorted(pour_set, key=lambda l: order.get(l, 99))
        if not pour_on:
            reroute.append(name)          # no resolvable pads -> route
            continue
        assignments[name] = pour_on

        # A pad is stranded only if NONE of the layers it occupies is
        # poured (a THT pad is fine as long as one of its layers is).
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
