"""
src/skidl/board/conformance.py

Config-conformance VERIFY gate: parse the BUILT .anvil_pcb and compare
it against the resolved configuration. "The board matches what the user
configured" is a claim that must be CHECKED, not assumed -- DRC-clean
and netlist-complete say nothing about layer count, board size, mounting
holes or track widths.

Verdict policy (user's honesty principle): a mismatch on a value with NO
recorded engine override is a pipeline bug -> ok=False (hard fail,
status config_mismatch upstream). A mismatch WITH a recorded override
(engine_overrides in the sidecar, e.g. the 2->4 layer escalation) is
expected -- reported prominently, not failed.
"""

from __future__ import annotations
import re
from pathlib import Path


def count_npth_holes(board_text: str) -> int:
    """np_thru_hole pads inside MountingHole footprints. Deliberately
    scoped: connectors carry their own NPTH alignment pegs, which are
    not mounting holes."""
    from skidl.board.pcb_writer import _match_paren
    count = 0
    for m in re.finditer(r'\(footprint\s+"[^"]*MountingHole[^"]*"',
                         board_text):
        try:
            end = _match_paren(board_text, m.start())
        except Exception:
            continue
        count += board_text[m.start():end].count("np_thru_hole")
    return count


def check_config_conformance(base: str, out_dir) -> dict:
    """Diff the BUILT board against the resolved configuration.
    Returns {ok, checks: {name: {requested, built, ok, note}},
    mismatches, mismatches_with_override, overridden_user_values}."""
    out_dir = Path(out_dir)
    pcb = out_dir / f"{base}.anvil_pcb"
    if not pcb.is_file():
        return {"ok": False, "error": "no board file to verify"}
    from skidl.board.rule_discovery import resolve_board_config
    from skidl.board.board_setup import read_saved_state, load_sidecar
    from skidl.board.review import _net_track_widths

    rc = resolve_board_config(base, out_dir)
    saved = read_saved_state(base, out_dir)
    sc = load_sidecar(base, out_dir)
    overrides = sc.get("engine_overrides") or {}
    text = pcb.read_text(encoding="utf-8", errors="replace")
    checks = {}

    def _chk(name, requested, built, ok, note=""):
        checks[name] = {"requested": requested, "built": built,
                        "ok": bool(ok), "note": note}

    # layers: built copper count vs resolved
    req_layers = rc["layers"]["value"]
    if req_layers and saved["layers"]:
        _chk("layers", int(req_layers), saved["layers"],
             saved["layers"] == int(req_layers))

    # board size: Edge.Cuts extents vs resolved (exact when mech-fixed,
    # >= requested when growing is allowed)
    bsz = rc["board_size"]["value"]
    ec = saved["edge_cuts"]
    if bsz and ec:
        mb = (sc.get("mechanical") or {}).get("board") or {}
        mech_fixed = bool(mb.get("width")) and not mb.get("grow", False)
        w_req, h_req = float(bsz["width_mm"]), float(bsz["height_mm"])
        if mech_fixed:
            ok = (abs(ec["width_mm"] - w_req) <= 0.5
                  and abs(ec["height_mm"] - h_req) <= 0.5)
            note = "mechanical outline: exact within 0.5mm"
        else:
            ok = (ec["width_mm"] >= w_req - 0.5
                  and ec["height_mm"] >= h_req - 0.5)
            note = "growth allowed: built must be >= requested"
        _chk("board_size", bsz,
             {"width_mm": ec["width_mm"], "height_mm": ec["height_mm"]},
             ok, note)

    # mounting holes: NPTH count in MountingHole footprints
    holes = rc["mounting_holes"]["value"]
    if holes:
        pos = ((sc.get("mechanical") or {}).get("mounting_holes")
               or {}).get("positions")
        want = len(pos) if pos else 4
        got = count_npth_holes(text)
        _chk("mounting_holes", f"{want} x {holes}", got, got >= want)

    # ground pour: a zone must exist when the pour is enabled
    if rc["ground_pour"]["value"]:
        _chk("ground_pour", True, "(zone" in text, "(zone" in text)

    # track widths: every routed net's minimum used width must reach its
    # class width (the router may only widen, never narrow)
    classes = rc["net_classes"]["value"] or {}
    assignments = sc.get("net_class_assignments") or {}
    violations, checked = [], 0
    for net, cls in assignments.items():
        c = classes.get(cls) or {}
        cw = c.get("width") or c.get("track_width")
        if not cw:
            continue
        widths = _net_track_widths(text, net)
        if not widths:
            continue
        checked += 1
        if min(widths) < float(cw) - 1e-3:
            violations.append({"net": net, "class": cls,
                               "class_width": cw,
                               "min_built": min(widths)})
    if checked:
        _chk("track_widths", "min used width >= class width",
             {"nets_checked": checked, "violations": violations},
             not violations)

    mismatches, with_override = [], []
    for name, c in checks.items():
        if c["ok"]:
            continue
        # board_size covers two override keys (grow escalation)
        keys = (name, "board_size") if name == "board_size" else (name,)
        if any(k in overrides for k in keys):
            c["note"] = (c["note"] + " -- engine override recorded, "
                         "expected mismatch").strip(" -")
            with_override.append(name)
        else:
            mismatches.append(name)

    user_srcs = ("user", "argument", "board-user", "system-user",
                 "project-user")
    overridden_user = [
        {"field": f, **(v if isinstance(v, dict) else {"detail": v})}
        for f, v in overrides.items()
        if isinstance(v, dict) and v.get("was_source") in user_srcs]

    return {"ok": not mismatches, "checks": checks,
            "mismatches": mismatches,
            "mismatches_with_override": with_override,
            "overridden_user_values": overridden_user}
