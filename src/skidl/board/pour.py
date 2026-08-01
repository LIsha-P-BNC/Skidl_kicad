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
reroute) this returns. The acceptance test for the realised pour is DRC:
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


def assign_pour_layers(board: BoardModel, pour_decisions: dict) -> tuple:
    """Map poured nets to copper layers from the ACTUAL stackup.

    Returns (assignments, reroute):
      assignments {net_name: [layer, ...]} -- where each net is poured.
      reroute     [net_name, ...]          -- poured-role nets this stackup
                    can't host as a plane; they go BACK into the DSN so no
                    net falls through the crack between "excluded" and
                    "not poured".

    Layer policy, derived from layer_count (never assumed):
      1 layer : ground pours front; power rerouted.
      2 layer : ground pours B.Cu; power rerouted (two planes on one
                reference would fight for copper).
      4 layer : ground -> In1.Cu, power -> In2.Cu (classic sig-gnd-pwr-sig).
      6+ layer: ground -> In1.Cu and the second-from-back copper; power ->
                In2.Cu; remaining inner layers stay signal.
      odd/other: pour nothing new -- everything routes (safe fallback).
    """
    layers = copper_layers(board)
    n = len(layers)
    gnd = [name for name, role in pour_decisions.items() if role == "ground"]
    pwr = [name for name, role in pour_decisions.items() if role == "power"]

    assignments = {}
    if n == 1:
        for name in gnd:
            assignments[name] = [layers[0]]
        return assignments, list(pwr)
    if n == 2:
        for name in gnd:
            assignments[name] = [layers[-1]]          # B.Cu
        return assignments, list(pwr)
    if n == 4:
        for name in gnd:
            assignments[name] = [layers[1]]           # In1.Cu
        for name in pwr:
            assignments[name] = [layers[2]]           # In2.Cu
        return assignments, []
    if n >= 6:
        gnd_layers = [layers[1], layers[-2]]
        for name in gnd:
            assignments[name] = list(gnd_layers)
        for name in pwr:
            assignments[name] = [layers[2]]
        return assignments, []

    # n == 3, 5, or any non-standard count: don't invent a plane split --
    # route everything so nothing is silently left open.
    return assignments, list(gnd) + list(pwr)


def _class_width(board: BoardModel, class_name: str) -> float:
    ncs = board.net_classes or {}
    cls = ncs.get(class_name) or ncs.get("Default") or {}
    return float(cls.get("width", 0.25))
