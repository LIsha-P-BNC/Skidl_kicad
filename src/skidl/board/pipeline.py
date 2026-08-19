"""
src/skidl/board/pipeline.py

One-call driver for the whole board pipeline the MCP server (or any
script) can invoke in a subprocess:

    .net -> populate -> place -> .anvil_pcb
         -> .dsn -> FreeRouting (invisible) -> .ses -> routed .anvil_pcb

Everything here is pure python + the FreeRouting subprocess. kicad-cli
steps (DRC, ipcd356 verify, manufacturing exports) are NOT run here --
the MCP server owns those so their reports live in the tool responses.

HARD RULE honored throughout: no window is ever shown to the user --
routing runs headless (see route/freerouting.py); the user only ever
sees the finished board in KiCad.
"""

from __future__ import annotations
from pathlib import Path

from skidl.board.model import BoardModel
from skidl.board.adapter.pymodel import PyModelBackend
from skidl.board.place.simple import place_board, count_courtyard_overlaps
from skidl.board.place.rules import place_board_rules
from skidl.board.pcb_writer import write_pcb, update_project_net_classes
from skidl.board.route import export_dsn, route_with_freerouting, import_ses


def build_pcb(netlist_path, out_dir=None, name=None, layers=None,
              route=True, passes=30, route_timeout_s=300,
              net_classes=None, placement="rules",
              progress_file=None, reuse_ses=False, kicad_cli=None) -> dict:
    """Run the full board pipeline. Returns a report dict; 'ok' means a
    legal .anvil_pcb exists (routed if route=True and an engine ran to
    completion; placed-but-unrouted otherwise, flagged in 'routing').

    placement: "rules" (M2 placer, auto-falls back to "simple" on
    failure) or "simple" (M1 coarse placer directly -- the roomier
    layout create_pcb retries with when routing can't finish)."""
    netlist_path = Path(netlist_path)
    out_dir = Path(out_dir) if out_dir else netlist_path.parent
    base = name or netlist_path.stem

    # LEARN FIRST: SAVED KiCad files are the source of truth when the USER
    # saved them (fingerprint arbitration in board_setup); otherwise the
    # sidecar -- including pending update_board_setup requests -- is what
    # this build applies. Both survive regeneration.
    import json as _json
    from skidl.board.board_setup import (load_sidecar, read_saved_state,
                                         board_edit_status)
    sidecar_path = out_dir / f"{base}.board_config.json"
    sidecar = load_sidecar(base, out_dir)
    saved = read_saved_state(base, out_dir)
    user_edited = bool(saved["exists"]) and \
        board_edit_status(base, out_dir)["machine_generated"] is False

    truth_arbitration = {"user_edited_board": user_edited}
    if user_edited and saved.get("layers"):
        eff_layers = int(saved["layers"])
        if sidecar.get("layers") and int(sidecar["layers"]) != eff_layers:
            truth_arbitration["layers"] = (
                f"user-saved board has {eff_layers} copper layers; sidecar "
                f"asked {sidecar['layers']} -- the SAVED board wins")
    else:
        eff_layers = int(sidecar.get("layers") or layers or 2)
    if user_edited and saved.get("thickness"):
        eff_thickness = float(saved["thickness"])
        if sidecar.get("thickness") and \
                abs(float(sidecar["thickness"]) - eff_thickness) > 1e-6:
            truth_arbitration["thickness"] = (
                f"user-saved board thickness {eff_thickness}; sidecar asked "
                f"{sidecar['thickness']} -- the SAVED board wins")
    else:
        eff_thickness = float(sidecar.get("thickness") or 1.6)

    board = BoardModel(name=base, layer_count=eff_layers,
                       thickness=eff_thickness)

    # DYNAMIC rule pickup: whatever the user set in KiCad's net-class /
    # Board Setup dialogs lives in the project's .anvil_pro -- read it
    # back so user-tuned clearances/widths drive BOTH the board file and
    # the router. Precedence: explicit net_classes arg > user's
    # .anvil_pro > built-in defaults.
    from skidl.board.pcb_writer import read_project_rules, _DEFAULT_NET_CLASS
    user_rules = read_project_rules(out_dir / f"{base}.anvil_pro")
    merged = {}
    for cls_name, rules in user_rules.items():
        merged[cls_name] = {**_DEFAULT_NET_CLASS, **rules}
    for cls_name, rules in (net_classes or {}).items():
        merged[cls_name] = {**merged.get(cls_name, dict(_DEFAULT_NET_CLASS)), **rules}
    if merged:
        board.net_classes = merged

    # Board Setup carry-forward: ONLY from a user-edited board (fingerprint
    # mismatch / foreign generator, decided by board_edit_status -- the
    # old generator-token + mtime dance lives there now). Our own machine
    # boards are re-templated fresh from the sidecar, so a pending
    # update_board_setup thickness/layers request actually applies.
    carry_forward = None
    prev_pcb = out_dir / f"{base}.anvil_pcb"
    if user_edited and prev_pcb.is_file():
        from skidl.board.rule_discovery import read_board_setup
        cf = read_board_setup(prev_pcb)
        if cf.get("setup_text") or cf.get("layers_text"):
            carry_forward = cf
            # layers/thickness were already adopted from the SAVED file
            # above -- the carried blocks describe exactly that stack.

    # Mask clearance the user set via update_board_setup (sidecar) is
    # applied to regenerated boards too.
    # User's explicit mask clearance wins; else the IPC-class expansion.
    mask_clearance = (sidecar.get("pad_to_mask_clearance")
                      if sidecar.get("pad_to_mask_clearance") is not None
                      else sidecar.get("mask_expansion"))

    backend = PyModelBackend()
    backend.populate(board, netlist_path)

    # Net-class assignments decided at init time (auto Power/USB etc.).
    # Class names resolve CASE-INSENSITIVELY with the USER's spelling
    # winning: if the user renamed "Power" to "power" in the Board Setup
    # dialog, assignments follow THEIR class (with their rules) instead
    # of silently falling back to defaults on a phantom duplicate.
    by_lower = {}
    for cls_name in board.net_classes:
        by_lower.setdefault(cls_name.lower(), cls_name)
    assignments = sidecar.get("net_class_assignments") or {}
    for net in board.nets.values():
        cls = assignments.get(net.name)
        if cls:
            canon = by_lower.get(cls.lower(), cls)
            net.net_class = canon
            board.net_classes.setdefault(canon, dict(_DEFAULT_NET_CLASS))
    # Netlist-declared classes get the same resolution.
    for net in board.nets.values():
        canon = by_lower.get(net.net_class.lower())
        if canon and canon != net.net_class:
            net.net_class = canon

    # SAFETY NET: guarantee power nets route at their IPC-2152-informed width
    # even when initialize_pcb_project never ran (create_pcb called directly).
    # Without this a power rail routes at the 0.25mm Default -- the width plan
    # only lived in project_init. Idempotent: a net already in a wide-enough
    # class doesn't appear in the plan.
    width_guarantee = _guarantee_power_widths(
        board, sidecar.get("currents"),
        copper_oz=sidecar.get("copper_oz") or 1)

    # EDGE.CUTS VERBATIM CARRY: a user-drawn outline is the user's own
    # work. Its extents become a FIXED (grow=False) outline for placement;
    # a NON-rectangular shape is additionally carried verbatim -- at write
    # time the user's gr_* blocks replace the generated rectangle, so the
    # drawn shape survives regeneration. Parts that don't fit fail
    # honestly via the mechanical-fit path.
    uec_stored = sidecar.get("user_edge_cuts")
    if user_edited and saved.get("edge_cuts"):
        from skidl.board.board_setup import (read_edge_cuts_blocks,
                                             edge_cuts_is_rectangle)
        ec = saved["edge_cuts"]
        _ec_blocks = read_edge_cuts_blocks(
            prev_pcb.read_text(encoding="utf-8", errors="replace"))
        mech_cfg = dict(sidecar.get("mechanical") or {})
        mb = dict(mech_cfg.get("board") or {})
        mb["width"], mb["height"] = ec["width_mm"], ec["height_mm"]
        mb.setdefault("grow", False)
        mech_cfg["board"] = mb
        sidecar = {**sidecar, "mechanical": mech_cfg,
                   "board_width": ec["width_mm"],
                   "board_height": ec["height_mm"]}
        if _ec_blocks and not edge_cuts_is_rectangle(_ec_blocks):
            uec = {"blocks": _ec_blocks,
                   "extents": [ec["minx"], ec["miny"], ec["maxx"],
                               ec["maxy"]]}
            carry_forward = carry_forward or {}
            carry_forward["user_edge_cuts"] = uec
            # Persisted (sidecar is written at the end of the build) so
            # the shape ALSO survives later machine rebuilds -- until the
            # user redraws the outline.
            sidecar = {**sidecar, "user_edge_cuts": uec}
            truth_arbitration["edge_cuts"] = (
                "user-drawn NON-rectangular outline carried VERBATIM; "
                f"placement constrained to its {ec['width_mm']}x"
                f"{ec['height_mm']} mm extents")
        else:
            if "user_edge_cuts" in sidecar:
                sidecar = {k: v for k, v in sidecar.items()
                           if k != "user_edge_cuts"}
            truth_arbitration["edge_cuts"] = (
                f"user outline adopted as fixed {ec['width_mm']}x"
                f"{ec['height_mm']} mm rectangle")
    elif uec_stored:
        # The user's previously adopted drawn shape keeps carrying through
        # machine rebuilds until the user redraws the outline.
        ext = uec_stored["extents"]
        w, h = round(ext[2] - ext[0], 2), round(ext[3] - ext[1], 2)
        mech_cfg = dict(sidecar.get("mechanical") or {})
        mb = dict(mech_cfg.get("board") or {})
        mb["width"], mb["height"] = w, h
        mb.setdefault("grow", False)
        mech_cfg["board"] = mb
        sidecar = {**sidecar, "mechanical": mech_cfg,
                   "board_width": w, "board_height": h}
        carry_forward = carry_forward or {}
        carry_forward["user_edge_cuts"] = uec_stored
        truth_arbitration["edge_cuts"] = (
            "user-drawn outline (previously adopted) carried VERBATIM")

    # MECHANICAL-FIRST: enclosure constraints (fixed outline, hole map,
    # edge connectors, keepouts) are applied BEFORE placement -- the
    # professional order. The placer works INSIDE them.
    from skidl.board.place.mech import (
        build_mech_plan, apply_fixed, MechanicalFitError)
    mech = build_mech_plan(sidecar, board)
    if mech:
        notes = apply_fixed(board, mech)
        if notes:
            pass   # surfaced below in the placement report

    # M2 rule placer first; on ANY failure (or overlapping result) fall
    # back to the proven M1 coarse placer -- mirroring the schematic
    # engine's anchor->legacy placement fallback. A fallback is reported,
    # never silent. Under mechanical constraints there is NO fallback:
    # the simple placer can't honor fixed items, and violating the
    # user's enclosure silently would be worse than failing honestly.
    place_report = None
    if placement == "rules" or mech:
        try:
            place_report = place_board_rules(board, mech=mech)
            if place_report.get("courtyard_overlaps", 0) > 0:
                raise RuntimeError(
                    f"rules placer left {place_report['courtyard_overlaps']} overlaps")
        except MechanicalFitError as exc:
            return {"ok": False, "status": "mechanical_fit_failed",
                    "error": str(exc),
                    "needed_mm2": exc.needed_mm2, "board_mm2": exc.board_mm2,
                    "worst_refs": exc.worst_refs}
        except Exception as exc:
            if mech:
                return {"ok": False, "status": "placement_failed",
                        "error": ("rules placer failed under mechanical "
                                  f"constraints: {exc!r} -- the simple "
                                  "fallback cannot honor fixed items")}
            for fp in board.footprints:
                fp.x = fp.y = fp.rotation = 0.0
            place_report = place_board(board)
            place_report["placer"] = "simple"
            place_report["rules_fallback_reason"] = repr(exc)
    if place_report is None:
        place_report = place_board(board)
        place_report["placer"] = "simple"
    if mech:
        place_report["mechanical"] = {
            "board": [mech.width, mech.height], "grow": mech.grow,
            "fixed": sorted(mech.fixed_refs),
            "keepouts": [k.name for k in mech.keepouts],
            "notes": notes,
        }

    # User-requested board dimensions: the outline can only GROW to meet
    # them (parts can't be shrunk); report honestly when parts exceed.
    # Skipped under a mechanical outline -- the enclosure is authoritative.
    want_w, want_h = sidecar.get("board_width"), sidecar.get("board_height")
    if board.outline and (want_w or want_h) and not (mech and mech.width):
        auto_w = max(p[0] for p in board.outline)
        auto_h = max(p[1] for p in board.outline)
        W = max(auto_w, float(want_w or 0))
        H = max(auto_h, float(want_h or 0))
        board.outline = [(0.0, 0.0), (W, 0.0), (W, H), (0.0, H)]
        place_report["outline_note"] = (
            f"requested {want_w}x{want_h} mm; parts need {auto_w:.1f}x{auto_h:.1f} -> "
            f"final {W:.1f}x{H:.1f}")

    holes_size = sidecar.get("mounting_holes")
    if holes_size and not (mech and mech.hole_positions):
        place_report["mounting_holes"] = _add_mounting_holes(board, str(holes_size))

    # Drawing-sheet discipline: the whole board must sit INSIDE the page
    # frame (the same concept the schematic generator applies). Pick the
    # smallest paper that fits the real outline, then shift everything
    # so the board is centered on that sheet. Runs BEFORE routing, so
    # tracks/vias/zones inherit the shifted coordinates.
    from skidl.board.pcb_writer import _pick_paper, PAPER_SIZES
    board.paper = _pick_paper(board, carry_forward)
    dims = {n: (w, h) for n, w, h in PAPER_SIZES}
    if board.outline and board.paper in dims:
        pw, ph = dims[board.paper]
        xs = [p[0] for p in board.outline]
        ys = [p[1] for p in board.outline]
        dx = (pw - (max(xs) - min(xs))) / 2.0 - min(xs)
        dy = (ph - (max(ys) - min(ys))) / 2.0 - min(ys)
        dx, dy = round(dx, 2), round(dy, 2)
        for fp in board.footprints:
            fp.x += dx
            fp.y += dy
        board.outline = [(x + dx, y + dy) for x, y in board.outline]
        for ko in board.keepouts:
            ko.outline = [(x + dx, y + dy) for x, y in ko.outline]
        place_report["paper"] = board.paper
        place_report["sheet_offset"] = (dx, dy)

    report = {
        "ok": True,
        "footprints": len(board.footprints),
        "nets": len(board.nets),
        "placement": place_report,
        "courtyard_overlaps": count_courtyard_overlaps(board),
    }
    if width_guarantee:
        report["power_width_guarantee"] = width_guarantee

    # --- DYNAMIC POWER POURS (option b) -------------------------------
    # Decide pours from the board, emit zones + stitching vias, and get the
    # nets to keep OUT of the routing problem. Ground pours on every layer
    # its pads occupy and is excluded from the DSN; power pours only on its
    # dedicated inner plane and STAYS in the DSN so the router connects its
    # outer pads to plane vias (Specctra has no per-layer net exclusion).
    # `ground_pour` sidecar toggle disables the whole thing.
    pour_exclude, pour_zones, pour_vias = set(), [], []
    if sidecar.get("ground_pour", True) and board.outline:
        from skidl.board.pour import prepare_power_pours
        pour_exclude, pour_zones, pour_vias, pour_overrides, pour_warns = \
            prepare_power_pours(board, currents=sidecar.get("currents"))
        board.warnings.extend(pour_warns)
        board.pad_overrides.extend(pour_overrides)
        report["power_pours"] = {
            "zones": len(pour_zones), "stitch_vias": len(pour_vias),
            "excluded_from_routing": sorted(pour_exclude),
            "warnings": pour_warns,
        }

    routing = {"requested": bool(route)}
    if route:
        dsn = export_dsn(board, out_dir / f"{base}.dsn", exclude=pour_exclude)
        ses = out_dir / f"{base}.ses"
        if reuse_ses and ses.is_file():
            # Experimentation path: import an EXISTING session (e.g. a
            # route re-run with different passes on the same DSN)
            # without spending another routing sitting. Placement must
            # be deterministic-identical to the one that produced it.
            r = {"ok": True, "complete": None, "jar": "reused .ses"}
        else:
            r = route_with_freerouting(dsn, ses, passes=passes,
                                       timeout_s=route_timeout_s,
                                       progress_file=progress_file)
        routing.update({k: r.get(k) for k in
                        ("ok", "complete", "jar", "error")})
        if r.get("ok"):
            stats = import_ses(board, ses)
            routing.update(stats)
            # RE-PLAN stitching vias now that routed copper exists. The
            # pre-route plan only knew component courtyards, so its vias got
            # stamped ON TOP of signal tracks (copper shorts) and stranded
            # outside the ground fill (dangling vias). Replanning against the
            # real tracks/vias keeps every stitch via clear of other-net
            # copper and inside solid ground -> no shorts, no dangling.
            if sidecar.get("ground_pour", True) and board.outline and pour_zones:
                from skidl.board.pour import plan_stitching_vias_for
                pour_vias, revwarns = plan_stitching_vias_for(
                    board, sidecar.get("currents"))
                board.warnings.extend(revwarns)
                report["power_pours"]["stitch_vias"] = len(pour_vias)
                report["power_pours"]["stitch_warnings"] = revwarns
        else:
            routing["note"] = ("board saved PLACED but UNROUTED -- routing "
                               "engine unavailable or failed; open in KiCad "
                               "to route manually, or fix the engine and "
                               "re-run create_pcb")
    report["routing"] = routing

    # Add the pour zones + stitching vias computed above (replaces the old
    # single ad-hoc B.Cu GND pour). kicad-cli computes the actual fill on
    # refill; the DRC gate in pcb_job proves unconnected == 0.
    board.zones.extend(pour_zones)
    board.vias.extend(pour_vias)

    pcb = write_pcb(board, out_dir / f"{base}.anvil_pcb", carry_forward=carry_forward,
                    mask_clearance=mask_clearance)

    # GROUND-ISLAND HEAL: a dense route can pinch the ground pour into copper
    # regions the fill can't tie together (every PIN is still connected -- it
    # is floating ground, an EMI nit + a DRC 'unconnected zone'). KiCad's fill
    # tells us exactly where; drop a ground via at a nearby CLEAR spot to bond
    # each island to the plane, then rewrite. Needs kicad_cli (to compute the
    # fill) and only runs on a routed board with ground zones.
    if kicad_cli and route and pour_zones and board.outline:
        try:
            from skidl.board.pour import (find_ground_islands, plan_island_vias,
                                          plan_pours, assign_pour_layers)
            gnd_names = sorted(pour_exclude) or [
                n for n, r in plan_pours(board, sidecar.get("currents")).items()
                if r == "ground"]
            assigns, _r, _w = assign_pour_layers(
                board, plan_pours(board, sidecar.get("currents")))
            healed = 0
            for _round in range(3):
                pts = find_ground_islands(pcb, kicad_cli, gnd_names)
                if not pts:
                    break
                added = []
                for gname in gnd_names:
                    layers_g = assigns.get(gname)
                    if layers_g and len(layers_g) >= 2:
                        added += plan_island_vias(board, pts, gname, layers_g)
                if not added:
                    break
                board.vias.extend(added)
                healed += len(added)
                pcb = write_pcb(board, out_dir / f"{base}.anvil_pcb",
                                carry_forward=carry_forward,
                                mask_clearance=mask_clearance)
            if healed:
                report["power_pours"]["island_vias_added"] = healed
        except Exception as _exc:
            report.setdefault("power_pours", {})["island_heal_error"] = repr(_exc)

    pro = update_project_net_classes(board, out_dir / f"{base}.anvil_pro")
    report["kicad_pcb"] = str(pcb)
    report["kicad_pro"] = str(pro)
    report["setup_carried_forward"] = bool(carry_forward)
    report["truth_arbitration"] = truth_arbitration

    # Surface the FINAL board size so the caller can tell the user "your board
    # is W x H mm" and where that came from -- otherwise the dimension is only
    # implied by the outline and never reported. Only axis-aligned rectangular
    # outlines are produced (no rotated/diagonal boards), so shape is fixed;
    # a real angled enclosure would need a mechanical outline, not a size.
    if board.outline:
        xs = [p[0] for p in board.outline]
        ys = [p[1] for p in board.outline]
        bw = round(max(xs) - min(xs), 2)
        bh = round(max(ys) - min(ys), 2)
        if mech and mech.width:
            size_source = "enclosure/mechanical spec (authoritative)"
        elif want_w or want_h:
            size_source = "user-requested (grown to fit parts where needed)"
        else:
            size_source = "auto-sized from placed parts (no dimensions given)"
        report["board_dimensions"] = {
            "width_mm": bw,
            "height_mm": bh,
            "area_cm2": round(bw * bh / 100.0, 2),
            "shape": "rectangular",
            "source": size_source,
        }

    # Record the setup hash this build consumed, so later operations can
    # tell the user when Board Setup changed in the meantime. Stamp the
    # fingerprint FIRST: a machine write without a fresh fingerprint would
    # be mis-read as the user's manual edit by the arbitration.
    try:
        from skidl.board.board_setup import (get_board_setup as _gbs,
                                             stamp_board_fingerprint)
        stamp_board_fingerprint(base, out_dir)
        snap = _gbs(base, out_dir)
        sidecar["setup_hash"] = snap["setup_hash"]
        sidecar["state_hash"] = snap["state_hash"]
        sidecar_path.write_text(_json.dumps(sidecar, indent=2) + "\n",
                                encoding="utf-8")
    except Exception:
        pass
    return report


def _guarantee_power_widths(board: BoardModel, currents: dict = None,
                            copper_oz: float = 1) -> dict:
    """Ensure every net whose IPC-2152 required width exceeds its class width
    routes in a synthesized PWR_* class carrying that width -- the same plan
    project_init makes, applied here so create_pcb is safe WITHOUT init.
    Idempotent: net_width_plan only lists nets that need MORE than their
    current class, so a net already in a wide class is untouched. Returns
    {class_name: width_mm} for the classes it added/assigned."""
    from skidl.board.width_engine import net_width_plan, bucket_classes
    classes = board.net_classes or {}
    default_w = (classes.get("Default") or {}).get("width", 0.25)
    nets_by_class = {}
    for net in board.nets.values():
        nets_by_class.setdefault(net.net_class, []).append(net.name)
    try:
        plan = net_width_plan(
            nets_by_class,
            {"classes": classes, "copper_oz": copper_oz,
             "rules": {"min_track_width": default_w}},
            currents=currents or {})
    except Exception:
        return {}
    applied = {}
    for cname, spec in bucket_classes(plan).items():
        base = dict(classes.get("Power") or classes.get("Default") or {})
        base["width"] = spec["width"]
        classes[cname] = base
        for netname in spec["nets"]:
            if netname in board.nets:
                board.nets[netname].net_class = cname
        applied[cname] = spec["width"]
    board.net_classes = classes
    return applied


def _ground_net(board: BoardModel):
    """The board's ground net (most-connected net classifying as ground)."""
    from skidl.schematics.net_classify import classify_net_role

    class _N:
        def __init__(self, name):
            self.name = name
            self.pins = []
            self.drive = None

    best, best_pads = None, 0
    for net in board.nets.values():
        if classify_net_role(_N(net.name)) == "ground" and len(net.pad_refs) > best_pads:
            best, best_pads = net.name, len(net.pad_refs)
    return best


def _add_mounting_holes(board: BoardModel, size: str) -> dict:
    """Append 4 corner NPTH mounting-hole footprints (locked, no net).
    Placed inset from the final outline corners; the DSN export emits
    keepouts so the router can't cross them."""
    from skidl.board.model import Footprint
    from skidl.board.profiles import MOUNTING_HOLES
    from skidl.board.footprint_libs import (
        load_pads_and_courtyard, load_footprint_source, FootprintNotFoundError,
    )

    lib_id = MOUNTING_HOLES.get(size, MOUNTING_HOLES["M3"])
    try:
        pads, courtyard = load_pads_and_courtyard(lib_id)
        source = load_footprint_source(lib_id)
    except FootprintNotFoundError:
        return {"note": f"mounting-hole footprint {lib_id} not in libraries -- skipped"}

    from skidl.board.place.simple import _world_bbox, GAP

    radius = max(abs(v) for c in (courtyard or (-2, -2, 2, 2)) for v in (c,))
    inset = radius + 1.0
    maxx = max(p[0] for p in board.outline)
    maxy = max(p[1] for p in board.outline)

    part_boxes = [_world_bbox(fp) for fp in board.footprints]

    def _free(x, y):
        """No courtyard within GAP of a hole centered at (x, y)."""
        for bx1, by1, bx2, by2 in part_boxes:
            if (x + radius + GAP > bx1 and x - radius - GAP < bx2 and
                    y + radius + GAP > by1 and y - radius - GAP < by2):
                return False
        return True

    def _spot(corner_x, corner_y, dx, dy):
        """First free spot at the corner, sliding inward along the edge
        in 2 mm steps (corners are premium: an edge connector may own
        one -- real designers slide the hole along, so do we)."""
        for step in range(0, 40):
            for ox, oy in ((dx * 2 * step, 0), (0, dy * 2 * step)):
                x, y = corner_x + ox, corner_y + oy
                if inset <= x <= maxx - inset and inset <= y <= maxy - inset \
                        and _free(x, y):
                    return x, y
        return None

    corners = [(inset, inset, 1, 1), (maxx - inset, inset, -1, 1),
               (maxx - inset, maxy - inset, -1, -1), (inset, maxy - inset, 1, -1)]
    refs, skipped = [], 0
    for i, (cx, cy, dx, dy) in enumerate(corners, 1):
        spot = _spot(cx, cy, dx, dy)
        if spot is None:
            skipped += 1
            continue
        x, y = spot
        ref = f"MH{i}"
        board.footprints.append(Footprint(
            ref=ref, lib_id=lib_id, value=size, x=x, y=y,
            pads=[type(p)(**{f: getattr(p, f) for f in (
                "number", "net", "x", "y", "size_x", "size_y", "shape",
                "pad_type", "layers", "drill", "drill_y", "rotation",
                "roundrect_rratio")}) for p in pads],
            courtyard=courtyard, locked=True, source_text=source,
        ))
        part_boxes.append(_world_bbox(board.footprints[-1]))
        refs.append(ref)
    out = {"size": size, "lib": lib_id, "refs": refs}
    if skipped:
        out["note"] = f"{skipped} hole(s) skipped -- no free spot near those corners"
    return out
