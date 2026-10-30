"""
src/skidl/board/place/rules.py

M2: the rule-based placer -- engineering placement rules from the plan's
Phase 3, applied to a BoardModel:

  * functional blocks ordered LEFT -> RIGHT by role
    (Power -> MCU -> Communication -> Sensor -> Drivers -> Other),
    via the schematic engine's classify_block_role/order_blocks_by_role
  * each cluster (anchor IC + satellites, from detect_clusters) is placed
    as a unit: anchor centered in its column, satellites ringed around it
  * decoupling caps snap to the SIDE of their IC where its supply pad
    actually is (pad-aware, from find_decap_affinities), rotated so the
    cap's pads face the IC
  * crystals snap beside the MCU's XTAL pads (same pad-aware logic)
  * connectors go flush to the board edge: INPUT-side connectors (feeding
    an anchor's "input" pintype pins, e.g. a barrel jack into a
    regulator's VI) on the LEFT edge, output-side on the RIGHT
  * courtyard de-overlap, grid snap, Edge.Cuts refit -- shared with the
    M1 coarse placer, which stays as the automatic fallback (pipeline.py)
    when anything here fails.
  * utilization-driven densify: a sparse board (utilization under the
    professional 35% floor) re-legalizes at tighter gaps (2.0 -> 1.0 ->
    0.5 mm) while it stays overlap-free and routable -- small circuit,
    small board. Skipped under a mechanical outline.

Same discipline as simple.py: classification is REUSED from
schematics/cluster.py + net_classify.py via the duck-typed view shims.
"""

from __future__ import annotations
import math
import os as _os
import re

from skidl.board.model import BoardModel, Footprint
from skidl.board.place.simple import (
    GRID, GAP, EDGE_MARGIN,
    _build_views, _size, _world_bbox, _copper_bbox, _overlap, _snap,
    count_courtyard_overlaps,
)

# Copper-to-board-edge pull/clamp (mm). Above the KiCad board-setup edge
# clearance (0.5mm default) AND safe against the 0.5mm placement grid snap
# (a snapped copper edge still lands >= 0.5mm from a snapped outline edge).
EDGE_CU = 1.0

from skidl.schematics.cluster import (
    classify_block_role,
    detect_clusters,
    find_anchor_parts,
    find_decap_affinities,
    order_blocks_by_role,
)
from skidl.schematics.net_classify import (
    is_connector_part, classify_net_role, is_rf_part)

_CRYSTAL_PREFIXES = ("Y", "X")
_CRYSTAL_NAME_HINTS = ("crystal", "oscillator", "resonator")

# Density knobs (12-config sweep on the 150-part tracker, 2026-07-31:
# this combo gave the smallest 0-overlap board, 207x162 vs 236x247).
BLOCK_GAP_MULT = 1.5    # spacing between functional blocks, in GAPs
LOOSE_ROW_MULT = 1.6    # loose-block shelf width vs sqrt(parts area)
GLOBAL_COMPACT = True   # tetris-pack pass across all blocks
GLOBAL_OPT = True       # connectivity-driven block optimization

# Utilization-driven densify (small circuit -> small board): the 2.0mm
# GAP is roomy ON PURPOSE so the autorouter keeps corridors, but on a
# sparse board it leaves utilization far below the professional 35-60%
# window (metrics.py) -- a small circuit lands on twice the board it
# needs. When utilization < UTIL_FLOOR, the legalization stack re-runs
# at each tighter gap; a rung is kept only while the board stays
# overlap-free, the congestion estimate stays under the ceiling, and
# the weighted HPWL does not regress.
UTIL_FLOOR = 0.35            # professional lower packing bound
DENSIFY_GAPS = (1.0, 0.5, 0.3, 0.2)   # mm courtyard gaps, tightest last
# measured: _compact at the default 2.0 mm gap is a NO-OP (holdout util
# 0.113 -> 0.113); ALL density comes from the densify ladder's tighter gaps
# (0.5 mm reached only 0.203). 0.3/0.2 mm are above the ~0.15 mm fab floor
# and still gated hard by congestion (DENSIFY_CONGESTION) + zero-overlap, so
# an unroutable rung is rejected and rolled back -- the ladder simply stops
# at the tightest rung each board can take.
DENSIFY_CONGESTION = 1.0     # peak demand/capacity ceiling while tightening
# HPWL growth a densify rung may add. TI reference boards (TIDA-00949/01447/
# 01450: ~43-78 parts/1000mm2 vs our ~10-26) pack TIGHT and explicitly trade
# a little extra wire length for a small board ("small circuit -> small
# board"). A flat +5% ceiling rejected every tightening on sparse boards
# (measured: holdout stuck at 20% util because the 1.0mm rung cost +9.6%
# HPWL). So the ceiling is UTILIZATION-AWARE: far below the floor, allow more
# HPWL growth to buy compactness; at/above the floor, hold the tight +5%.
# Congestion (DENSIFY_CONGESTION) and zero-overlap stay HARD -- they, not
# HPWL, protect routability, so relaxing wire length can't ship an unroutable
# board. Dynamic: the slack scales with each board's own deficit, no per-
# board tuning.
DENSIFY_HPWL_BASE = 1.05     # ceiling at/above the util floor
DENSIFY_HPWL_MAX = 1.40      # ceiling at zero utilization (max slack)


def _densify_hpwl_ceiling(util_before):
    """HPWL-growth multiplier allowed while densifying, scaled by how far the
    board sits below UTIL_FLOOR (pure, testable)."""
    deficit = min(1.0, max(0.0, UTIL_FLOOR - util_before) / UTIL_FLOOR)   # 0..1
    return DENSIFY_HPWL_BASE + (DENSIFY_HPWL_MAX - DENSIFY_HPWL_BASE) * deficit


def place_board_rules(board: BoardModel, mech=None) -> dict:
    """Rule placement: runs the placer with BOTH block orderings (role
    order and connectivity-affinity chain) and keeps whichever measures
    the lower weighted HPWL -- measured on the tracker, neither ordering
    wins universally; legalization interactions dominate, so choosing by
    the objective itself is the only honest selector. Deterministic.
    `mech` (a place.mech.MechPlan, already applied) makes fixed items
    immovable and the outline authoritative."""
    if not board.footprints:
        return {"placed": 0, "placer": "rules"}

    # EARLY AREA PRE-CHECK: a fixed outline the parts physically cannot
    # occupy must fail HERE with the honest area numbers, not after the
    # whole legalization stack gives up with "left N overlaps" (measured:
    # tracker asked 100x80 for parts needing ~2x that area -> 378-overlap
    # crash instead of a usable answer). Professional boards top out
    # around 60-70% courtyard utilization -- above that, routing room is
    # gone even if the rectangles tile.
    if mech and mech.width and mech.height and not mech.grow:
        from skidl.board.place.mech import MechanicalFitError
        fixed_refs0 = set(mech.fixed_refs or ())
        boxes = [fp.courtyard_world_bbox() for fp in board.footprints
                 if fp.ref not in fixed_refs0]
        needed = sum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes if b)
        avail = mech.width * mech.height - sum(
            (x2 - x1) * (y2 - y1) for (x1, y1, x2, y2) in
            (mech.obstacle_boxes or ()))
        if avail > 0 and needed > 0.70 * avail:
            side = (needed / 0.5) ** 0.5   # suggest ~50% utilization
            raise MechanicalFitError(
                f"parts need ~{needed:.0f} mm2 of courtyard but the fixed "
                f"{mech.width}x{mech.height} mm board offers only "
                f"{avail:.0f} mm2 usable ({needed / avail:.0%} utilization; "
                "practical ceiling ~70%). ASK THE USER for a bigger board "
                f"-- roughly {side:.0f}x{side:.0f} mm or an equivalent "
                "rectangle -- or to drop parts.",
                needed_mm2=needed, board_mm2=avail)

    if not GLOBAL_OPT:
        return _place_once(board, mech, use_affinity=False)

    def snapshot():
        return ({fp.ref: (fp.x, fp.y, fp.rotation) for fp in board.footprints},
                list(board.outline or []),
                list(board.keepouts))

    def restore(snap):
        pos, outline, keepouts = snap
        for fp in board.footprints:
            if fp.ref in pos:
                fp.x, fp.y, fp.rotation = pos[fp.ref]
        board.outline = outline or None
        board.keepouts = keepouts

    # ROUTABILITY-AWARE RANKING: pick the candidate that will ROUTE best, not
    # just the one with the shortest wire. "You can't judge a placement until
    # you try to route it" -- so score each candidate by weighted HPWL blended
    # with its congestion peak (the routing-demand proxy congestion_map already
    # computes): a slightly longer layout that leaves routing corridors open
    # beats a tight one the router then chokes on. SKIDL_ROUTE_AWARE=0 falls
    # back to the pure-HPWL selector.
    import os as _os
    from skidl.board.place.metrics import congestion_map
    route_aware = _os.environ.get("SKIDL_ROUTE_AWARE") != "0"
    CONGESTION_PENALTY = 0.6      # peak_ratio 1.0 -> +60% effective cost

    base_snap = snapshot()
    candidates = []
    for use_aff in (False, True):
        restore(base_snap)
        # apply_fixed added MH* footprints already; positions restore fine.
        rep = _place_once(board, mech, use_affinity=use_aff)
        hp = (rep.get("quality") or {}).get("weighted_hpwl_mm")
        hp = hp if hp is not None else float("inf")
        try:
            peak = float(congestion_map(board).get("peak_ratio", 0.0) or 0.0)
        except Exception:
            peak = 0.0
        score = hp * (1.0 + CONGESTION_PENALTY * peak) if route_aware else hp
        candidates.append((score, hp, peak,
                           rep.get("courtyard_overlaps", 99),
                           use_aff, rep, snapshot()))
    # Prefer zero overlaps, then the routability-blended score, then role order.
    candidates.sort(key=lambda c: (c[3], c[0], c[4]))
    best = candidates[0]
    restore(best[6])
    rep = best[5]
    rep["ordering"] = "affinity-chain" if best[4] else "role-order"
    rep["selected_by"] = ("hpwl+congestion" if route_aware else "hpwl")
    rep["ordering_score"] = {
        ("affinity-chain" if c[4] else "role-order"):
            {"hpwl_mm": round(c[1], 1), "congestion_peak": round(c[2], 2),
             "score": round(c[0], 1)}
        for c in candidates}
    return rep


def _place_once(board: BoardModel, mech=None, use_affinity=False) -> dict:
    """One placement run with the given block ordering strategy."""

    fixed_refs = set(mech.fixed_refs) if mech else set()

    _decap_slots.clear()   # per-run slot counters
    parts_by_ref, _nets = _build_views(board)
    views = [parts_by_ref[fp.ref] for fp in
             sorted(board.footprints, key=lambda f: f.ref)
             if fp.ref not in fixed_refs]

    # Antennas/RF-path parts are treated like edge connectors: the
    # antenna must sit at the board edge (its keepout is generated after
    # legalization). A mechanical.connectors entry for the ref wins.
    rf_views = [v for v in views if is_rf_part(v)]
    connectors = sorted((v for v in views
                         if is_connector_part(v) or v in rf_views),
                        key=lambda v: v.ref)
    non_conn = [v for v in views if v not in connectors]

    # ---- functional blocks: each detected cluster + one loose block ----
    # detect_clusters can pull in NEIGHBOR parts that weren't in its input
    # (net.pins reach every part) -- intersect with non_conn so a
    # connector never double-counts as both block member and edge part.
    non_conn_set = set(non_conn)
    clusters = [c & non_conn_set for c in detect_clusters(non_conn)]
    clusters = [c for c in clusters if c]

    # Per-CAP pairing (find_decap_pairs): the net-keyed find_decap_affinities
    # collapses to ONE cap per shared +V rail, stranding every other decap in
    # a foreign block so the supply-pad snap never fires for them (measured
    # on holdout_can_node: C5/C6/C7 landed in foreign blocks).
    from skidl.schematics.cluster import find_decap_pairs
    decap_to_ic = dict(find_decap_pairs(views))

    # Decaps link to their IC over POWER nets, which cluster detection
    # deliberately ignores -- so a decap usually lands OUTSIDE its IC's
    # cluster. Pull each decap into the block that holds its IC, else the
    # snap-beside-the-supply-pad rule can never fire.
    for decap, ic in decap_to_ic.items():
        if decap not in non_conn_set:
            continue
        for c in clusters:
            c.discard(decap)
        for c in clusters:
            if ic in c:
                c.add(decap)
                break

    clustered = set().union(*clusters) if clusters else set()
    loose = [v for v in non_conn if v not in clustered]
    blocks = [sorted(c, key=lambda v: v.ref) for c in clusters]
    if loose:
        blocks.append(sorted(loose, key=lambda v: v.ref))
    role_blocks = [(classify_block_role(b), b) for b in blocks]
    ordered_blocks = order_blocks_by_role(role_blocks)
    roles = {id(b): r for r, b in role_blocks}

    # ---- connectivity-aware ordering ----------------------------------
    # Blocks that share many weighted nets (clock/RF heavy, LEDs tiny)
    # should sit ADJACENT in the row wrap: greedy chain from the
    # heaviest pair, always appending the block with the strongest
    # affinity to the already-placed set. Deterministic (index
    # tie-break); preserves the tight shelf layout that free-space force
    # optimization destroyed (measured: forces gave 123x309mm and WORSE
    # weighted HPWL after legalization).
    opt_report = None
    if use_affinity and len(ordered_blocks) > 2:
        from skidl.schematics.cluster import block_affinity_matrix
        affinity = block_affinity_matrix(ordered_blocks)
        if affinity:
            n = len(ordered_blocks)
            aff = {}
            for (i, j), w in affinity.items():
                aff.setdefault(i, {})[j] = w
                aff.setdefault(j, {})[i] = w
            start = min(max(affinity.items(), key=lambda kv: kv[1])[0])
            order, placed = [start], {start}
            while len(order) < n:
                best = None
                for cand in range(n):
                    if cand in placed:
                        continue
                    s = sum(aff.get(cand, {}).get(p, 0.0) for p in placed)
                    if best is None or s > best[0]:
                        best = (s, cand)
                order.append(best[1])
                placed.add(best[1])
            ordered_blocks = [ordered_blocks[i] for i in order]
            opt_report = {
                "strategy": "affinity-chain ordering",
                "affinity_top": [[list(k), round(w, 1)] for k, w in sorted(
                    affinity.items(), key=lambda kv: -kv[1])[:5]],
            }

    # ---- lay blocks left-to-right, WRAPPING into rows -----------------
    # One endless row sprawls a chain circuit to 150+ mm (measured on a
    # 16-part PSU); wrap when the row passes a near-square target width
    # so the board tends toward a compact aspect.
    est_area = sum(_size(v.fp)[0] * _size(v.fp)[1] for v in non_conn) * 3.0
    target_w = max(40.0, math.sqrt(est_area) * 1.2)
    col_x = 0.0
    row_y = 0.0
    row_h = 0.0
    max_col_h = 0.0
    block_reports = []
    close_pairs = set()   # (decap/crystal, anchor) pairs allowed TIGHT
    crystal_to_ic = {}    # crystal view -> its anchor IC view (loop re-assert)
    row_idx = 0
    row_members = []   # (block, row_idx, x_start, width)
    for block in ordered_blocks:
        if col_x > target_w and col_x > 0:
            row_y += row_h + GAP * BLOCK_GAP_MULT
            col_x, row_h = 0.0, 0.0
            row_idx += 1
        w, h = _place_block(block, col_x, decap_to_ic, close_pairs,
                            crystal_to_ic)
        if row_y:
            for v in block:
                v.fp.y = v.fp.y + row_y
        block_reports.append({"role": roles.get(id(block), "Other"),
                              "parts": [v.ref for v in block]})
        row_members.append((block, row_idx, col_x, w))
        col_x += w + GAP * BLOCK_GAP_MULT
        row_h = max(row_h, h)
        max_col_h = max(max_col_h, row_y + row_h)

    # Serpentine rows: odd rows flip left-right so the block SEQUENCE
    # stays spatially continuous across row boundaries -- without this a
    # connectivity-ordered chain teleports back to x=0 every wrap. Only
    # meaningful when the order IS a connectivity chain (measured: it
    # worsens the role ordering).
    if not use_affinity:
        row_members = []
    row_widths = {}
    for _b, ri, xs, w in row_members:
        row_widths[ri] = max(row_widths.get(ri, 0.0), xs + w)
    for block, ri, xs, w in row_members:
        if ri % 2 == 1:
            new_xs = row_widths[ri] - (xs + w)
            dxs = new_xs - xs
            if abs(dxs) > 0.01:
                for v in block:
                    v.fp.x = v.fp.x + dxs

    # ---- connectors: input side LEFT, output side RIGHT ---------------
    anchors = set(find_anchor_parts(views))
    left_conn, right_conn = _split_connectors(connectors, anchors)
    # RF parts always take the RIGHT edge (away from the input side).
    for v in rf_views:
        if v in left_conn:
            left_conn.remove(v)
        if v not in right_conn:
            right_conn.append(v)
    mid_y = max_col_h / 2.0
    _stack_connectors(left_conn, x=-(GAP * 4), mid_y=mid_y)
    _stack_connectors(right_conn, x=col_x + GAP * 4, mid_y=mid_y)

    # ---- legalize: de-overlap, shift positive, snap, outline ----------
    fps = [v.fp for v in views]
    statics = []            # immovable participants in overlap checks
    if mech:
        statics = [board.footprint(r) for r in sorted(fixed_refs)]
        for i, (x1, y1, x2, y2) in enumerate(mech.obstacle_boxes, 1):
            statics.append(Footprint(
                ref=f"__KO{i}", lib_id="__keepout", x=(x1 + x2) / 2,
                y=(y1 + y2) / 2,
                courtyard=((x1 - x2) / 2, (y1 - y2) / 2,
                           (x2 - x1) / 2, (y2 - y1) / 2)))
    fixed_set = {fp.ref for fp in statics}
    all_fps = fps + statics

    # SKIDL_PLACE_TRACE=1: report utilization after each legalization stage,
    # so the weak link (block layout vs compact vs densify) is MEASURED, not
    # guessed. stderr-only, env-gated.
    import os as _os
    _ptrace = _os.environ.get("SKIDL_PLACE_TRACE")

    def _ptr(stage):
        if _ptrace:
            import sys as _sys
            from skidl.board.place.metrics import utilization as _u
            try:
                print(f"[place] {stage}: util={_u(board):.3f} "
                      f"overlaps={count_courtyard_overlaps(board)}",
                      file=_sys.stderr, flush=True)
            except Exception:
                pass

    _ptr("after block-layout+connectors")
    _deoverlap(all_fps, heavy={v.fp.ref for v in anchors},
               allowed_close=close_pairs, fixed=fixed_set)
    _ptr("after deoverlap")
    # Compaction: professional boards pack 35-50% of the area; without a
    # pull-back pass the de-overlap pushes leave a sparse layout (7%
    # utilization measured on the tracker). Tetris-pack everything
    # toward the layout min corner while staying legal, then de-overlap
    # once more for safety.
    if GLOBAL_COMPACT:
        _compact(all_fps, allowed_close=close_pairs, fixed=fixed_set)
        _deoverlap(all_fps, heavy={v.fp.ref for v in anchors},
                   allowed_close=close_pairs, fixed=fixed_set)
    _ptr("after compact")
    fixed_outline = ((mech.width, mech.height)
                     if mech and mech.width and mech.height else None)
    _shift_and_fit(board, fps,
                   left_edge={v.fp.ref for v in left_conn},
                   right_edge={v.fp.ref for v in right_conn},
                   fixed_outline=fixed_outline, statics=statics)
    if count_courtyard_overlaps(board):
        # The flush-pull in _shift_and_fit can drag an edge connector
        # onto its stack neighbour (measured: J1 pin header pulled into
        # the JAE SIM courtyard) -- resolve again, then re-fit WITHOUT
        # the pull so the fix survives.
        _deoverlap(all_fps, heavy={v.fp.ref for v in anchors},
                   allowed_close=close_pairs, fixed=fixed_set)
        _shift_and_fit(board, fps, left_edge=set(), right_edge=set(),
                       fixed_outline=fixed_outline, statics=statics)

    # Small circuit -> small board: with no mechanical spec, tighten a
    # sparse layout toward the professional utilization floor.
    density = (_densify(board, fps, anchors, close_pairs,
                        left_conn, right_conn)
               if mech is None else None)
    _ptr("after densify")

    # RE-ASSERT CRYSTAL LOOPS. The snap in _place_block pins each crystal beside
    # its IC's XTAL pads, but the GLOBAL compact/densify only honor `fixed`
    # inside _place_block -- globally the tiny crystal tetris-packs toward the
    # min corner independently of its big IC, stranding the XTAL net on a long
    # haul that crosses foreign nets (measured holdout_can_node: Y1 ended 25mm
    # from U2 -> FreeRouting shorted OSC2 across CAN_H, DRC-fatal every run).
    # Re-snap each crystal beside its IC on the FINAL geometry, then de-overlap
    # (the crystal<->IC close pair keeps them tight; other parts yield). Pure
    # geometry + net sharing -- DYNAMIC over any crystal/IC, no part names.
    reasserted = []
    if _os.environ.get("SKIDL_CRYSTAL_REASSERT", "1") == "0":
        crystal_to_ic = {}   # kill switch (A/B); leaves the stranded-crystal bug
    # Only on a FREE (non-mechanical) board: re-snapping a crystal onto its IC
    # needs slack to absorb the shove, which a board with fixed items/keepouts
    # (mech) does not have -- there it forced an unrecoverable overlap that
    # hard-failed the build (measured: stm32 U1<->U2). On mech boards the crystal
    # keeps its densified spot. The overlap rollback below is the extra guard
    # for a dense FREE board.
    if crystal_to_ic and mech is None:
        # OVERLAP-GUARDED: snapshot first. Re-snapping a crystal onto its IC on a
        # DENSE board can shove the IC's neighbours and the single de-overlap
        # cannot recover -> a courtyard overlap that hard-fails the whole build
        # (measured: stm32 U1<->U2). Roll back the re-assert if it leaves MORE
        # overlaps than before -- the tight XTAL loop is a WANT, never worth an
        # overlapping (unmanufacturable) board.
        ovl_before = count_courtyard_overlaps(board)
        snap = {fp.ref: (fp.x, fp.y, fp.rotation) for fp in fps}
        for cr_v, ic_v in sorted(crystal_to_ic.items(), key=lambda kv: kv[0].ref):
            pads = _anchor_pads_on_shared_nets(cr_v, ic_v, power_only=False)
            if pads:
                _snap_beside(cr_v.fp, ic_v.fp, pads)
                reasserted.append((cr_v.fp.ref, ic_v.fp.ref))
        if reasserted:
            _deoverlap(all_fps, heavy={v.fp.ref for v in anchors},
                       allowed_close=close_pairs, fixed=fixed_set)
            if count_courtyard_overlaps(board) > ovl_before:
                for fp in fps:
                    fp.x, fp.y, fp.rotation = snap[fp.ref]
                reasserted = []
                _ptr("crystal re-assert ROLLED BACK (would overlap)")
            else:
                _ptr("after crystal re-assert")

    # AUTO-SIZE only: grow the outline so no pad copper sits within EDGE_CU of
    # the board edge (copper_edge_clearance). Pure translate+grow -> cannot add
    # an overlap. A fixed user outline cannot grow, so it is skipped there.
    if mech is None and _os.environ.get("SKIDL_EDGE_GROW", "1") != "0":
        _grow_outline_for_copper(board, fps)
        _ptr("after edge-clearance grow")

    if fixed_outline:
        # FIT THE USER'S BOARD: when the user gave a board size, the parts must
        # go INSIDE it. If the natural (professionally-spaced) placement spills
        # past the fixed outline, COMPACT it (gap tightening, same stack as the
        # auto-size densify) to fit -- only when it genuinely cannot fit does
        # _check_mech_fit raise with the honest area numbers. (Previously
        # densify was skipped under a mechanical outline, so a placement that
        # overflowed a FEASIBLE board failed instead of being packed in.)
        def _fits():
            try:
                _check_mech_fit(board, fps, mech)
                return True
            except Exception:
                return False
        if not _fits():
            _densify(board, fps, anchors, close_pairs, left_conn, right_conn,
                     target_wh=fixed_outline)
        # Clamp <-> keepout push can ping-pong a part; a few bounded
        # resolve rounds settle it before the honest fit check.
        for _ in range(3):
            try:
                _check_mech_fit(board, fps, mech)
                if not count_courtyard_overlaps(board):
                    break
            except Exception:
                pass
            _deoverlap(all_fps, heavy={v.fp.ref for v in anchors},
                       allowed_close=close_pairs, fixed=fixed_set)
            _shift_and_fit(board, fps, left_edge=set(), right_edge=set(),
                           fixed_outline=fixed_outline, statics=statics)
        if count_courtyard_overlaps(board, ignore={r for r in fixed_set
                                                   if r.startswith("__KO")}):
            _rescue_overlaps(board, fps, fixed_set, fixed_outline)
        # SNAPPED-PAIR SEPARATION: a cluster snap (crystal/decap beside its IC,
        # an allowed_close pair) can place the satellite with its courtyard
        # OVERLAPPING the IC's -- _deoverlap deliberately skips allowed_close
        # pairs, _rescue_overlaps often can't improve the count without
        # breaking the pair, and the final count then hard-fails the whole
        # fixed-size build (measured: U1<->Y1 at EVERY size 20x18..25x22).
        # Close means close, not overlapping: push the satellite just clear.
        if count_courtyard_overlaps(board, ignore={r for r in fixed_set
                                                   if r.startswith("__KO")}):
            _separate_close_overlaps(board, fps, close_pairs, fixed_outline)
        _check_mech_fit(board, fps, mech)

    overlaps = count_courtyard_overlaps(board, ignore={r for r in fixed_set
                                                       if r.startswith("__KO")})
    # Name the colliding pairs when legalization lost: "left N overlaps" with
    # no refs is undebuggable for the user (measured: ESP32-WROOM-32's 48mm
    # antenna-keepout courtyard vs an edge header on a 70mm board). The bbox
    # is the same test count_courtyard_overlaps uses.
    overlap_pairs = []
    if overlaps:
        real = [fp for fp in board.footprints
                if not fp.ref.startswith("__KO")]
        for i in range(len(real)):
            ab = real[i].courtyard_world_bbox()
            if not ab:
                continue
            for j in range(i + 1, len(real)):
                bb = real[j].courtyard_world_bbox()
                if bb and ab[0] < bb[2] and bb[0] < ab[2] \
                        and ab[1] < bb[3] and bb[1] < ab[3]:
                    overlap_pairs.append((real[i].ref, real[j].ref))
    if fixed_outline:
        maxx, maxy = fixed_outline
    elif board.outline:
        # _shift_and_fit / _densify own the outline; report what was set
        # (the bbox+EDGE_MARGIN estimate disagrees after a densify).
        maxx = max(p[0] for p in board.outline)
        maxy = max(p[1] for p in board.outline)
    else:
        maxx = max(b[2] for b in map(_world_bbox, fps + statics)) + EDGE_MARGIN
        maxy = max(b[3] for b in map(_world_bbox, fps + statics)) + EDGE_MARGIN
    return {
        "placed": len(fps),
        "placer": "rules",
        "blocks": block_reports,
        "connectors_left": [v.ref for v in left_conn],
        "connectors_right": [v.ref for v in right_conn],
        "decaps": {d.ref: ic.ref for d, ic in decap_to_ic.items()},
        "crystals": {c.ref: ic.ref for c, ic in crystal_to_ic.items()},
        "board_size_mm": (round(maxx, 1), round(maxy, 1)),
        "courtyard_overlaps": overlaps,
        "overlap_pairs": overlap_pairs,
        "mechanical_fixed": sorted(r for r in fixed_set
                                   if not r.startswith("__KO")),
        "density": density,
        "global_opt": opt_report,
        "quality": _quality_metrics(board),
        "rf": _apply_rf_rules(board, rf_views),
    }


def _separate_close_overlaps(board, fps, close_pairs, outline_wh):
    """Minimal separation for allowed_close pairs whose courtyards OVERLAP.
    allowed_close means 'may sit closer than GAP', never 'may overlap' -- but
    the snap passes can land a satellite (crystal/decap) ON its IC's
    courtyard, and _deoverlap skips these pairs by design. Push the lighter
    part (fewer pins) of each overlapping close pair outward along the axis
    needing the smallest move (+0.3mm clearance), clamped inside the fixed
    outline. Pure geometry; runs only when overlaps remain after rescue."""
    by_ref = {fp.ref: fp for fp in fps}
    W, H = (outline_wh or (None, None))
    if _os.environ.get("SKIDL_PLACE_DEBUG"):
        print(f">>> [sep-debug] close_pairs={sorted(tuple(sorted(p)) for p in (close_pairs or []))}")
    for pair in (close_pairs or []):
        refs = list(pair) if not isinstance(pair, dict) else list(pair)
        if len(refs) != 2:
            continue
        a, b = by_ref.get(refs[0]), by_ref.get(refs[1])
        if a is None or b is None:
            continue
        ab, bb = _world_bbox(a), _world_bbox(b)
        ox = min(ab[2], bb[2]) - max(ab[0], bb[0])
        oy = min(ab[3], bb[3]) - max(ab[1], bb[1])
        if _os.environ.get("SKIDL_PLACE_DEBUG"):
            print(f">>> [sep-debug] {refs}: ox={ox:.2f} oy={oy:.2f} a={ab} b={bb}")
        if ox <= 0 or oy <= 0:
            continue                       # close but not overlapping: fine
        mover, other = (a, b) if len(getattr(a, "pads", [])) <= \
                                 len(getattr(b, "pads", [])) else (b, a)

        def _clamp(fp):
            if not (W and H):
                return
            x1, y1, x2, y2 = _world_bbox(fp)
            if x1 < 0.5: fp.x += 0.5 - x1
            if y1 < 0.5: fp.y += 0.5 - y1
            x1, y1, x2, y2 = _world_bbox(fp)
            if x2 > W - 0.5: fp.x -= x2 - (W - 0.5)
            if y2 > H - 0.5: fp.y -= y2 - (H - 0.5)

        n0 = count_courtyard_overlaps(board)

        def _pair_clear():
            # The pair must separate AND the board's TOTAL overlap count must
            # improve -- an escape that dumps the satellite onto ANOTHER part
            # (measured: Y1 cleared U1 then landed on C1+R1) is no escape.
            a1, b1 = _world_bbox(mover), _world_bbox(other)
            pair_ok = (min(a1[2], b1[2]) - max(a1[0], b1[0]) <= 0
                       or min(a1[3], b1[3]) - max(a1[1], b1[1]) <= 0)
            return pair_ok and count_courtyard_overlaps(board) < n0

        # Try every escape direction (smallest move first); a push toward a
        # near edge gets clamped straight back into the overlap (measured:
        # crystal below a DIP on a 20mm-tall board), so test the RESULT and
        # keep the first direction that actually clears -- also trying a
        # 50/50 split where the anchor gives way half the distance.
        home_m = (mover.x, mover.y, mover.rotation)
        home_o = (other.x, other.y)
        done = False
        # Try the mover in BOTH orientations: a long satellite (12mm HC49
        # crystal) that cannot fit beside its anchor lying down often fits
        # standing up (rotated 90) -- the pair passes never try rotation.
        for extra_rot in (0, 90):
            mover.x, mover.y, mover.rotation = home_m
            mover.rotation = (home_m[2] + extra_rot) % 360
            # Directional ESCAPE distances (full penetration, not the
            # symmetric overlap): to clear in +x the mover's LEFT edge must
            # pass the other's RIGHT edge, etc. Recomputed per orientation.
            mb_, ob_ = _world_bbox(mover), _world_bbox(other)
            cands = sorted([
                (ob_[2] - mb_[0] + 0.3, 1, 1),   # +x
                (mb_[2] - ob_[0] + 0.3, 1, -1),  # -x
                (ob_[3] - mb_[1] + 0.3, 0, 1),   # +y
                (mb_[3] - ob_[1] + 0.3, 0, -1),  # -y
            ])
            for dist, axis, sgn in cands:
                for split in (False, True):
                    mover.x, mover.y = home_m[0], home_m[1]
                    other.x, other.y = home_o
                    dm = dist if not split else dist / 2 + 0.15
                    if axis == 1:
                        mover.x += sgn * dm
                        if split: other.x -= sgn * dm
                    else:
                        mover.y += sgn * dm
                        if split: other.y -= sgn * dm
                    _clamp(mover); _clamp(other)
                    if _pair_clear():
                        done = True
                        break
                if done:
                    break
            if done:
                break
        if not done:                       # genuinely no room: restore all
            mover.x, mover.y, mover.rotation = home_m
            other.x, other.y = home_o


def _rescue_overlaps(board, fps, fixed_set, outline_wh):
    """LAST-RESORT legal relocation inside a FIXED outline: the push-based
    de-overlap/clamp passes can ping-pong a part wedged between a locked
    edge connector and its block (measured: C2 vs edge-pinned J1 on a
    7-part 70x50 board -- a size-independent 1-overlap loop). For each
    movable part still in an overlap, spiral outward from its position
    and take the NEAREST spot that reduces the global overlap count.
    Runs only when the normal passes have already failed -- a slower
    success beats a fast honest failure."""
    W, H = outline_wh
    ko = {r for r in fixed_set if r.startswith("__KO")}

    def _ring(r):
        for dx in range(-r, r + 1):
            yield dx, -r
            yield dx, r
        for dy in range(-r + 1, r):
            yield -r, dy
            yield r, dy

    n = count_courtyard_overlaps(board, ignore=ko)
    for fp in fps:
        if not n:
            break
        if fp.ref in fixed_set:
            continue
        if count_courtyard_overlaps(board, ignore=ko | {fp.ref}) >= n:
            continue                      # not involved in any overlap
        ox, oy = fp.x, fp.y
        moved = False
        for r in range(1, int(max(W, H)) + 1):
            for dx, dy in _ring(r):
                fp.x, fp.y = ox + dx, oy + dy
                b = _world_bbox(fp)
                if b[0] < 0.5 or b[1] < 0.5 or b[2] > W - 0.5 \
                        or b[3] > H - 0.5:
                    continue
                new_n = count_courtyard_overlaps(board, ignore=ko)
                if new_n < n:
                    n = new_n
                    moved = True
                    break
            if moved:
                break
        if not moved:
            fp.x, fp.y = ox, oy
    return n


def _apply_rf_rules(board, rf_views):
    """RF placement rules, applied AFTER legalization: an all-copper
    keepout from each antenna's grown courtyard to the nearest board
    edge (ground cutout under the antenna falls out of the rule area on
    refill), plus honest advisory checks: feed-net length and aggressor
    (crystal/switcher) distance. Returns None when the design has no RF
    parts."""
    if not rf_views or not board.outline:
        return None
    from skidl.board.model import KeepoutArea
    xs = [p[0] for p in board.outline]
    ys = [p[1] for p in board.outline]
    W, H = max(xs), max(ys)
    report = {"antennas": [], "keepouts": [], "warnings": []}
    for v in rf_views:
        x1, y1, x2, y2 = _world_bbox(v.fp)
        gx1, gy1, gx2, gy2 = x1 - 2.0, y1 - 2.0, x2 + 2.0, y2 + 2.0
        # Extend to the NEAREST edge so no copper sneaks behind the
        # antenna.
        d = {"left": x1, "right": W - x2, "top": y1, "bottom": H - y2}
        edge = min(d, key=lambda k: d[k])
        if edge == "left":
            gx1 = 0.0
        elif edge == "right":
            gx2 = W
        elif edge == "top":
            gy1 = 0.0
        else:
            gy2 = H
        gx1, gy1 = max(0.0, gx1), max(0.0, gy1)
        gx2, gy2 = min(W, gx2), min(H, gy2)
        board.keepouts.append(KeepoutArea(
            name=f"rf_{v.ref}", source="rf",
            outline=[(gx1, gy1), (gx2, gy1), (gx2, gy2), (gx1, gy2)],
            layers=("*.Cu",), no_footprints=False))
        report["antennas"].append(v.ref)
        report["keepouts"].append([v.ref, round(gx1, 1), round(gy1, 1),
                                   round(gx2, 1), round(gy2, 1)])
        # Aggressor separation: crystals and switching-regulator
        # inductors inside a 10mm halo couple into the antenna.
        for fp in board.footprints:
            if fp.ref == v.ref:
                continue
            ref_pfx = "".join(c for c in fp.ref if c.isalpha()).upper()
            if not (_is_crystal_ref(fp) or ref_pfx == "L"):
                continue
            ox1, oy1, ox2, oy2 = _world_bbox(fp)
            ddx = max(gx1 - ox2, ox1 - gx2, 0.0)
            ddy = max(gy1 - oy2, oy1 - gy2, 0.0)
            dist = (ddx ** 2 + ddy ** 2) ** 0.5
            if dist < 10.0:
                report["warnings"].append(
                    f"{fp.ref} (clock/inductor) is {dist:.1f}mm from the "
                    f"{v.ref} antenna zone -- keep noisy parts >=10mm away")
    return report


def _is_crystal_ref(fp):
    pfx = "".join(c for c in fp.ref if c.isalpha()).upper()
    return pfx in ("Y", "X") or "crystal" in (fp.lib_id or "").lower()


def _quality_metrics(board):
    """Placement-quality numbers for the report (all advisory)."""
    try:
        from skidl.board.place.metrics import (
            weighted_hpwl, utilization, congestion_map)
        return {"weighted_hpwl_mm": weighted_hpwl(board),
                "utilization": utilization(board),
                "congestion": congestion_map(board)}
    except Exception as exc:
        return {"error": repr(exc)}


def _check_mech_fit(board, fps, mech):
    """Every movable part must sit inside the fixed outline and clear of
    no_footprints keepouts; otherwise the honest answer is an error with
    the area numbers, never a silently grown board."""
    W, H = mech.width, mech.height
    outside = []
    for fp in fps:
        x1, y1, x2, y2 = _world_bbox(fp)
        if x1 < 0 or y1 < 0 or x2 > W or y2 > H:
            outside.append(fp.ref)
        else:
            for (kx1, ky1, kx2, ky2) in mech.obstacle_boxes:
                if x1 < kx2 and kx1 < x2 and y1 < ky2 and ky1 < y2:
                    outside.append(fp.ref)
                    break
    if outside:
        if mech.grow:
            return   # caller grows the outline and reports
        from skidl.board.place.mech import MechanicalFitError
        needed = sum((b[2] - b[0]) * (b[3] - b[1])
                     for b in map(_world_bbox, fps))
        raise MechanicalFitError(
            f"{len(outside)} part(s) do not fit the fixed {W}x{H} mm board "
            f"(parts need ~{needed:.0f} mm2 of {W * H:.0f} mm2, plus routing "
            f"room): {', '.join(outside[:8])}",
            needed_mm2=needed, board_mm2=W * H, worst_refs=outside[:8])


def _densify(board, fps, anchors, close_pairs, left_conn, right_conn,
             target_wh=None):
    """Utilization-driven gap tightening (small circuit -> small board).
    Re-runs the legalization stack (_compact -> _deoverlap ->
    _shift_and_fit) at each DENSIFY_GAPS rung; a rung is accepted only
    when the board stays overlap-free, the congestion estimate stays
    under DENSIFY_CONGESTION, and the weighted HPWL does not regress --
    a tighter board that routes worse is not an improvement. Snapped
    decap/crystal pairs keep their closeness via allowed_close. Stops at
    the first rung that reaches UTIL_FLOOR; a failed rung restores the
    previous state and stops.

    `target_wh=(W, H)` switches it to FIT-THE-BOARD mode: the user gave a
    fixed outline and the parts overflow it, so pack them to fit INSIDE
    (W, H). In that mode it keeps tightening past UTIL_FLOOR until the
    parts fit, and accepts a rung even if the weighted HPWL grows (fitting
    the user's enclosure outranks wire length) -- it still never accepts an
    overlapping rung. Returns a report dict."""
    from skidl.board.place.metrics import (
        congestion_map, utilization, weighted_hpwl)

    fit_mode = target_wh is not None
    before = utilization(board)
    report = {"utilization_before": before, "utilization_after": before,
              "target": UTIL_FLOOR, "gap_mm": GAP,
              "mode": "fit-board" if fit_mode else "util-floor"}
    if not fit_mode and before >= UTIL_FLOOR:
        return report

    heavy = {v.fp.ref for v in anchors}
    left = {v.fp.ref for v in left_conn}
    right = {v.fp.ref for v in right_conn}
    hpwl0 = weighted_hpwl(board)

    def _within_target():
        W, H = target_wh
        for fp in fps:
            x1, y1, x2, y2 = _world_bbox(fp)
            if x1 < -0.01 or y1 < -0.01 or x2 > W + 0.01 or y2 > H + 0.01:
                return False
        return True

    def snapshot():
        return ({fp.ref: (fp.x, fp.y, fp.rotation) for fp in fps},
                list(board.outline or []))

    def restore(state):
        pos, outline = state
        for fp in fps:
            fp.x, fp.y, fp.rotation = pos[fp.ref]
        board.outline = outline or None

    good = snapshot()
    report["rungs"] = []
    for gap in DENSIFY_GAPS:
        _compact(fps, allowed_close=close_pairs, gap=gap)
        _deoverlap(fps, heavy=heavy, allowed_close=close_pairs, gap=gap)
        # Edge keepout shrinks with the gap but never below the ~2mm
        # (80mil) manufacturing floor.
        _shift_and_fit(board, fps, left_edge=left, right_edge=right,
                       gap=gap, edge=max(2.0, gap * 2))
        peak = congestion_map(board).get("peak_ratio", 0.0)
        # Always reject an overlapping rung. Off fit-mode also reject on
        # congestion / HPWL regression; in fit-mode those are tolerated
        # because the goal is to get the parts inside the user's board.
        ovl = count_courtyard_overlaps(board)
        hpwl = weighted_hpwl(board)
        hpwl_ceiling = hpwl0 * _densify_hpwl_ceiling(before)
        bad = ovl > 0
        if not fit_mode:
            bad = bad or peak > DENSIFY_CONGESTION or hpwl > hpwl_ceiling
        # WHY a rung was rejected, not just that it was -- without this the
        # report says "stopped at 2.0mm" and the cause is invisible
        # (measured holdout: stuck at 14.9% util with no clue which guard).
        report["rungs"].append({
            "gap": gap, "overlaps": ovl, "congestion_peak": round(peak, 2),
            "hpwl": round(hpwl, 1), "hpwl_limit": round(hpwl_ceiling, 1),
            "utilization": round(utilization(board), 3),
            "accepted": not bad})
        if bad:
            restore(good)
            break
        good = snapshot()
        report["gap_mm"] = gap
        if fit_mode:
            if _within_target():
                break            # fits now -> done (keep tightening otherwise)
        elif utilization(board) >= UTIL_FLOOR:
            break
    report["utilization_after"] = utilization(board)
    return report


# ---------------------------------------------------------------------------
# Block internals
# ---------------------------------------------------------------------------

def _place_block(block, col_x, decap_to_ic, close_pairs=None,
                 crystal_to_ic=None):
    """Place one functional block in a column starting at col_x. The
    biggest anchor sits at the column center; decaps/crystals snap
    pad-aware beside it; remaining satellites ring around. Returns the
    (width, height) the column consumed. `crystal_to_ic` (out-param): records
    each snapped crystal->anchor so the loop can be RE-ASSERTED after the
    global densify, which would otherwise strand the crystal (see _place_once)."""
    anchors = [v for v in find_anchor_parts(block)]
    satellites = [v for v in block if v not in anchors]

    # SMALLEST-CAP-CLOSEST: a decoupling cap's high-frequency job wants the
    # SMALLEST value nearest the IC power pin (lowest ESL/loop area). Order
    # each IC's decaps by capacitance ascending so the earliest -- closest,
    # un-stacked -- slots go to the smallest caps; unknown values sort last.
    satellites.sort(key=lambda v: (
        v not in decap_to_ic,                                  # decaps first
        decap_to_ic[v].fp.ref if v in decap_to_ic else "",     # grouped by IC
        _cap_value(v.fp) if v in decap_to_ic else 0.0))        # smallest first

    widest = max(_size(v.fp)[0] for v in block)
    col_center = col_x + widest / 2.0 + GAP * 2

    # Anchors stack vertically down the column, biggest first at top.
    y = GAP * 4
    for a in anchors:
        aw, ah = _size(a.fp)
        a.fp.x, a.fp.y = _snap(col_center), _snap(y + ah / 2.0)
        y += ah + GAP * 4

    if not anchors:
        # Loose block: shelf-pack toward a near-square footprint. Rows of
        # height-sorted parts waste far less shelf headroom than arrival
        # order, and a square block packs into the board grid far better
        # than a tall strip (measured: 68 loose parts went 52x159 mm with
        # the old fixed 3*widest row width).
        area = sum(_size(v.fp)[0] * _size(v.fp)[1] for v in satellites)
        row_w = max(widest + GAP, math.sqrt(area) * LOOSE_ROW_MULT)
        x_off = row_h = 0.0
        for v in sorted(satellites, key=lambda v: -_size(v.fp)[1]):
            w, h = _size(v.fp)
            if x_off + w > row_w and x_off > 0:
                x_off, y, row_h = 0.0, y + row_h + GAP, 0.0
            v.fp.x = _snap(col_x + x_off + w / 2.0)
            v.fp.y = _snap(y + h / 2.0)
            x_off += w + GAP
            row_h = max(row_h, h)
        y += row_h
        width = max(row_w, widest)
        return width, y

    main = anchors[0]

    # Pad-aware snapping: decaps beside their IC's supply pad, crystals
    # beside the XTAL pads. Both use the same side-of-anchor helper.
    ring = []
    for v in satellites:
        target_pads = None
        if v in decap_to_ic and decap_to_ic[v] in block:
            ic = decap_to_ic[v]
            target_pads = _anchor_pads_on_shared_nets(v, ic, power_only=True)
            if target_pads:
                # One decap per supply PIN: the k-th decap of this IC
                # targets the k-th supply pad, so multiple decaps spread
                # across VDD pins instead of piling on one spot (piling
                # made de-overlap shove the loser 12mm out -- measured).
                slot = _decap_slots.setdefault(ic.fp.ref, 0)
                _decap_slots[ic.fp.ref] += 1
                _snap_beside(v.fp, ic.fp, target_pads, slot=slot)
                if close_pairs is not None:
                    close_pairs.add(frozenset((v.fp.ref, ic.fp.ref)))
                continue
        if _is_crystal(v):
            target_pads = _anchor_pads_on_shared_nets(v, main, power_only=False)
            if target_pads:
                _snap_beside(v.fp, main.fp, target_pads)
                if close_pairs is not None:
                    close_pairs.add(frozenset((v.fp.ref, main.fp.ref)))
                if crystal_to_ic is not None:
                    crystal_to_ic[v] = main
                continue
        ring.append(v)

    # Remaining satellites: hug the anchor's perimeter -- E/W sides stack
    # vertically alternating up/down from the anchor's centerline, N/S
    # sides spread horizontally alternating left/right. Slots never run
    # away in one direction, so even ~20 satellites stay packed around a
    # big QFP; the de-overlap pass resolves any residual contact.
    ring.sort(key=lambda v: (-len([p for p in v.pins if p.net]), v.ref))
    aw, ah = _size(main.fp)
    for i, v in enumerate(ring):
        w, h = _size(v.fp)
        side, k = i % 4, i // 4
        alt = ((k + 1) // 2) * (1 if k % 2 == 0 else -1)
        if side == 0:    # east
            v.fp.x = _snap(main.fp.x + aw / 2 + w / 2)
            v.fp.y = _snap(main.fp.y + alt * (h + GAP))
        elif side == 1:  # west
            v.fp.x = _snap(main.fp.x - aw / 2 - w / 2)
            v.fp.y = _snap(main.fp.y + alt * (h + GAP))
        elif side == 2:  # north
            v.fp.x = _snap(main.fp.x + alt * (w + GAP))
            v.fp.y = _snap(main.fp.y - ah / 2 - h / 2)
        else:            # south
            v.fp.x = _snap(main.fp.x + alt * (w + GAP))
            v.fp.y = _snap(main.fp.y + ah / 2 + h / 2)

    # Densify the block internally: snapped decaps/crystals and the
    # anchor hold position (their pad-relative spots are design intent);
    # ring satellites tetris-pack toward the block's own min corner.
    snapped = {r for pair in (close_pairs or set()) for r in pair}
    _compact([v.fp for v in block], allowed_close=close_pairs,
             fixed=snapped | {main.fp.ref})

    height = max(_world_bbox(v.fp)[3] for v in block) + GAP * 2
    width = (max(_world_bbox(v.fp)[2] for v in block)
             - min(_world_bbox(v.fp)[0] for v in block)) + GAP * 2
    return max(width, widest), height


def _anchor_pads_on_shared_nets(sat, anchor, power_only):
    """Anchor pads (Pad objects) on nets shared with satellite `sat`;
    power/ground nets only when power_only (decap case), any net for the
    crystal case (XTAL nets are signal-class)."""
    sat_nets = {p.net for p in sat.pins if p.net is not None}
    if power_only:
        sat_nets = {n for n in sat_nets if classify_net_role(n) is not None}
    shared = {n.name for n in sat_nets
              if any(pin.part is anchor for pin in n.pins)}
    return [p for p in anchor.fp.pads if p.net in shared] or None


_DECAP_CLEAR = 0.5   # mm courtyard-to-courtyard for decap/crystal snapping --
                     # TIGHT on purpose: using the general GAP margins put
                     # decaps 7-9mm from their IC pin (review-flagged on
                     # every board); a decoupling cap's whole job is to be
                     # close. De-overlap is told to allow this closeness.


_decap_slots = {}   # ic ref -> next slot; reset per placement run


_CAP_RE = re.compile(r'^\s*([\d.]+)\s*([pnuµmPNUM]?)', re.I)
_CAP_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}


def _cap_value(fp) -> float:
    """Capacitance of a cap footprint's value ("100nF", "0.1uF", "10u") in
    farads, for smallest-first ordering. Unknown / unit-less -> +inf so it
    sorts last (farthest), never ahead of a known small cap."""
    m = _CAP_RE.match(fp.value or "")
    if not m:
        return float("inf")
    try:
        num = float(m.group(1))
    except ValueError:
        return float("inf")
    mult = _CAP_MULT.get(m.group(2).lower())
    if mult is None:
        return float("inf")            # a bare number's unit is ambiguous
    return num * mult


def _snap_beside(fp: Footprint, anchor_fp: Footprint, target_pads, slot=0):
    """Place fp TIGHT against anchor_fp beside its slot-th supply pad,
    pads facing the anchor (0/90 rotation for 2-pin passives). Raw
    courtyard extents + _DECAP_CLEAR -- not the padded _size() -- so the
    supply pad lands mm-close to the IC pin. When more decaps than
    supply pads exist, extras stack outward along the same side."""
    pads_sorted = sorted(target_pads, key=lambda p: (p.x, p.y))
    pad = pads_sorted[slot % len(pads_sorted)]
    stack = slot // len(pads_sorted)
    px, py = pad.x, pad.y

    a_minx, a_miny, a_maxx, a_maxy = (anchor_fp.courtyard or (-3, -3, 3, 3))
    fminx, fminy, fmaxx, fmaxy = (fp.courtyard or (-2, -2, 2, 2))

    # Which anchor side is the pad group nearest?
    d_east, d_west = a_maxx - px, px - a_minx
    d_south, d_north = a_maxy - py, py - a_miny
    side = min((d_east, "E"), (d_west, "W"), (d_south, "S"), (d_north, "N"))[1]

    depth = (fmaxx - fminx) + _DECAP_CLEAR   # outward stacking pitch
    if side == "E":
        fp.rotation = 0.0
        fp.x = _snap(anchor_fp.x + a_maxx + _DECAP_CLEAR - fminx + stack * depth)
        fp.y = _snap(anchor_fp.y + py)
    elif side == "W":
        fp.rotation = 0.0
        fp.x = _snap(anchor_fp.x + a_minx - _DECAP_CLEAR - fmaxx - stack * depth)
        fp.y = _snap(anchor_fp.y + py)
    elif side == "S":
        # 90deg: the part's x-extent becomes its vertical extent
        # (KiCad rotation matrix -- see model.pad_world_xy).
        fp.rotation = 90.0
        fp.x = _snap(anchor_fp.x + px)
        fp.y = _snap(anchor_fp.y + a_maxy + _DECAP_CLEAR + fmaxx + stack * depth)
    else:
        fp.rotation = 90.0
        fp.x = _snap(anchor_fp.x + px)
        fp.y = _snap(anchor_fp.y + a_miny - _DECAP_CLEAR + fminx - stack * depth)

    # FACE the rail pad toward the IC: with a fixed 0/90 the cap's supply
    # pad points AWAY half the time, wasting the body length (~3-5mm of
    # the measured decap distance). Try the 180-flip and keep whichever
    # puts the rail pad nearer the target pin.
    rail_net = getattr(pad, "net", None)
    rail = next((p for p in fp.pads if p.net == rail_net), None)
    if rail is not None:
        tx = anchor_fp.x + px
        ty = anchor_fp.y + py
        best_rot, best_d = fp.rotation, None
        for rot in (fp.rotation, (fp.rotation + 180.0) % 360.0):
            fp.rotation = rot
            wx, wy = fp.pad_world_xy(rail)
            d = (wx - tx) ** 2 + (wy - ty) ** 2
            if best_d is None or d < best_d:
                best_rot, best_d = rot, d
        fp.rotation = best_rot


def _is_crystal(v):
    if (v.ref_prefix or "").upper() in _CRYSTAL_PREFIXES:
        return True
    text = f"{v.name} {v.search_text}".lower()
    return any(hint in text for hint in _CRYSTAL_NAME_HINTS)


# ---------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------

def _split_connectors(connectors, anchors):
    """(left, right) split: a connector whose nets feed an anchor's
    "input"-pintype pin is input-side (LEFT); one on an anchor's
    "output" pin is RIGHT. Undecided connectors alternate L/R."""
    left, right, undecided = [], [], []
    for c in connectors:
        score = 0
        for pin in c.pins:
            net = pin.net
            if net is None or classify_net_role(net) == "ground":
                continue
            for other in net.pins:
                if other.part in anchors:
                    # SKiDL pintypes: INPUT / OUTPUT / POWER-IN / POWER-OUT
                    # / PASSIVE / BIDIRECTIONAL ... "out" before "in"
                    # ("input" contains no "out"; "power-out" no "in").
                    ptype = _pintype_of(other)
                    if "out" in ptype:
                        score += 1
                    elif "in" in ptype and "bidir" not in ptype:
                        score -= 1
        if score < 0:
            left.append(c)
        elif score > 0:
            right.append(c)
        else:
            undecided.append(c)
    for i, c in enumerate(undecided):
        if len(left) < len(right):
            left.append(c)
        elif len(right) < len(left):
            right.append(c)
        else:
            (left if i % 2 == 0 else right).append(c)
    return left, right


def _pintype_of(pin_view):
    """The netlist pintype for a _PinView, via its net's pin_types map."""
    net = pin_view.net
    board_net = getattr(net, "_board_net", None)
    if board_net is not None:
        return (board_net.pin_types.get(
            (pin_view.part.ref, pin_view.number), "") or "").lower()
    return ""


def _stack_connectors(connectors, x, mid_y):
    total_h = sum(_size(c.fp)[1] + GAP for c in connectors)
    y = mid_y - total_h / 2.0
    for c in connectors:
        w, h = _size(c.fp)
        c.fp.x = _snap(x)
        c.fp.y = _snap(max(y, 0.0) + h / 2.0)
        y += h + GAP


# ---------------------------------------------------------------------------
# Legalization (shared logic with simple.py, parameterized weights)
# ---------------------------------------------------------------------------

def _compact(fps, allowed_close=None, rounds=12, fixed=None, gap=GAP):
    """Tetris-pack: slide every part as far toward the layout's min-x
    then min-y as legality allows, repeating until stable. `fixed` refs
    (anchors, snapped decaps/crystals) never move -- their pad-relative
    positions are design intent, not slack. Pairs in allowed_close keep
    their tighter margin. `gap` is the courtyard margin between parts
    (the densify ladder passes tighter values than the default)."""
    allowed_close = allowed_close or set()
    fixed = fixed or set()

    def push(axis):
        # Slide every part as far toward 0 on `axis` as legality allows
        # (tetris-pack). Direct edge computation, no stepping.
        lo = 0 if axis == "x" else 1
        boxes = {fp.ref: _world_bbox(fp) for fp in fps}
        # Pack toward the group's own min edge (NOT absolute 0 -- a block
        # sitting at col_x=100 must stay a block at col_x=100).
        baseline = min(b[lo] for b in boxes.values())
        moved = False
        for fp in sorted(fps, key=lambda f: boxes[f.ref][lo]):
            if fp.ref in fixed:
                continue
            b = boxes[fp.ref]
            limit = baseline
            for o in fps:
                if o is fp:
                    continue
                ob = boxes[o.ref]
                margin = (0.0 if frozenset((fp.ref, o.ref)) in allowed_close
                          else gap)
                if axis == "x":
                    if ob[1] < b[3] + margin and b[1] < ob[3] + margin:
                        if ob[2] + margin <= b[0] + 1e-6:
                            limit = max(limit, ob[2] + margin)
                else:
                    if ob[0] < b[2] + margin and b[0] < ob[2] + margin:
                        if ob[3] + margin <= b[1] + 1e-6:
                            limit = max(limit, ob[3] + margin)
            shift = (b[lo] - limit)
            if shift > 0.01:
                if axis == "x":
                    fp.x = _snap(fp.x - shift)
                else:
                    fp.y = _snap(fp.y - shift)
                boxes[fp.ref] = _world_bbox(fp)
                moved = True
        return moved

    for _ in range(rounds):
        mx = push("x")
        my = push("y")
        if not (mx or my):
            break


def _deoverlap(fps, heavy, iterations=500, allowed_close=None, fixed=None,
               gap=GAP):
    """allowed_close: pairs (frozenset of two refs) permitted to sit at
    _DECAP_CLEAR instead of the general GAP -- decaps/crystals hug their
    IC by DESIGN; pushing them back out defeats the snap rule. `fixed`
    refs never move (mechanically-fixed connectors/holes/keepouts): when
    one of a pair is fixed the free part takes the whole push; when BOTH
    are fixed the conflict is a mechanical-spec error, left for the fit
    check to report."""
    allowed_close = allowed_close or set()
    fixed = fixed or set()
    weight = {fp.ref: (4.0 if fp.ref in heavy else 2.0) for fp in fps}
    for _ in range(iterations):
        moved = False
        for i in range(len(fps)):
            for j in range(i + 1, len(fps)):
                a, b = fps[i], fps[j]
                if a.ref in fixed and b.ref in fixed:
                    continue
                margin = 0.0 if frozenset((a.ref, b.ref)) in allowed_close else gap
                ov = _overlap(_world_bbox(a), _world_bbox(b), margin)
                if ov is None:
                    continue
                dx, dy = ov
                if a.ref in fixed or b.ref in fixed:
                    mover = b if a.ref in fixed else a
                    other = a if mover is b else b
                    if dx <= dy:
                        sign = 1.0 if mover.x >= other.x else -1.0
                        mover.x = _snap(mover.x + sign * (dx + GRID))
                    else:
                        sign = 1.0 if mover.y >= other.y else -1.0
                        mover.y = _snap(mover.y + sign * (dy + GRID))
                    moved = True
                    continue
                wa, wb = weight[a.ref], weight[b.ref]
                share_a, share_b = wb / (wa + wb), wa / (wa + wb)
                if dx <= dy:
                    sign = 1.0 if a.x <= b.x else -1.0
                    total = dx + GRID
                    a.x = _snap(a.x - sign * total * share_a)
                    b.x = _snap(b.x + sign * total * share_b)
                else:
                    sign = 1.0 if a.y <= b.y else -1.0
                    total = dy + GRID
                    a.y = _snap(a.y - sign * total * share_a)
                    b.y = _snap(b.y + sign * total * share_b)
                moved = True
        if not moved:
            break
    else:
        # Main loop hit the iteration cap: symmetric shared pushes can
        # oscillate (A<->B<->C rings) and leave thin slivers forever
        # (measured: 150-part tracker kept 1-10 sliver overlaps, failing
        # the placer and losing its compact layout to the fallback).
        # Escape: ONLY the lighter part moves, full overlap at once, and
        # a pair seen repeatedly flips to the other axis to break rings.
        seen = {}
        for _ in range(200):
            resolved = True
            for i in range(len(fps)):
                for j in range(i + 1, len(fps)):
                    a, b = fps[i], fps[j]
                    margin = (0.0 if frozenset((a.ref, b.ref)) in allowed_close
                              else gap)
                    ov = _overlap(_world_bbox(a), _world_bbox(b), margin)
                    if ov is None:
                        continue
                    resolved = False
                    if a.ref in fixed and b.ref in fixed:
                        continue        # mechanical-spec conflict, reported later
                    key = frozenset((a.ref, b.ref))
                    seen[key] = seen.get(key, 0) + 1
                    if a.ref in fixed or b.ref in fixed:
                        mover = b if a.ref in fixed else a
                    else:
                        mover = b if weight[a.ref] >= weight[b.ref] else a
                    other = a if mover is b else b
                    dx, dy = ov
                    along_x = (dx <= dy) if seen[key] % 4 < 2 else (dx > dy)
                    if along_x:
                        sign = 1.0 if mover.x >= other.x else -1.0
                        mover.x = _snap(mover.x + sign * (dx + GRID))
                    else:
                        sign = 1.0 if mover.y >= other.y else -1.0
                        mover.y = _snap(mover.y + sign * (dy + GRID))
            if resolved:
                break


def _shift_and_fit(board, fps, left_edge, right_edge,
                   fixed_outline=None, statics=None,
                   gap=GAP, edge=EDGE_MARGIN):
    """Shift the MOVABLE parts positive, refit the outline, then pull
    edge connectors flush (courtyard 1mm from the board edge). With
    `fixed_outline` (mechanical-first) the outline is authoritative:
    statics never move, movable parts shift into the fixed frame, and
    the flush pull targets the fixed edges. `gap`/`edge` are the
    part-to-part and part-to-edge margins (the densify ladder passes
    tighter values than the defaults)."""
    boxes = [_world_bbox(fp) for fp in fps]
    minx = min(b[0] for b in boxes) - edge
    miny = min(b[1] for b in boxes) - edge
    for fp in fps:
        fp.x = _snap(fp.x - minx)
        fp.y = _snap(fp.y - miny)

    if fixed_outline:
        # The user's board size is AUTHORITATIVE and can be tight -- do NOT
        # force a copper edge-clearance here (it over-constrains a dense fixed
        # board into overlaps; the honest "enlarge the board" error fires
        # instead). Edge clearance on a fixed board is the auto-grow path's job.
        W, H = fixed_outline
        for fp in fps:
            bbox = _world_bbox(fp)
            if fp.ref in left_edge:
                fp.x = _snap(fp.x - (bbox[0] - 1.0))
            elif fp.ref in right_edge:
                fp.x = _snap(fp.x + (W - 1.0 - bbox[2]))
        # Clamp every movable part inside the authoritative outline --
        # de-overlap pushes (especially away from keepout obstacles) can
        # shove a part past the edge; the retry round then resolves any
        # overlap the clamp re-introduces.
        for fp in fps:
            x1, y1, x2, y2 = _world_bbox(fp)
            if x1 < gap:
                fp.x = _snap(fp.x + (gap - x1))
            elif x2 > W - gap:
                fp.x = _snap(fp.x - (x2 - (W - gap)))
            if y1 < gap:
                fp.y = _snap(fp.y + (gap - y1))
            elif y2 > H - gap:
                fp.y = _snap(fp.y - (y2 - (H - gap)))
        board.outline = [(0.0, 0.0), (W, 0.0), (W, H), (0.0, H)]
        return

    # Board width from the INTERIOR parts only, then edge connectors are
    # pulled flush against it -- computing width including the connectors'
    # own pre-nudge positions inflates the board with dead space.
    interior = [fp for fp in fps if fp.ref not in right_edge]
    maxx_int = max(_world_bbox(fp)[2] for fp in interior) + gap
    if right_edge:
        conn_w = max(_world_bbox(fp)[2] - _world_bbox(fp)[0]
                     for fp in fps if fp.ref in right_edge)
        maxx = maxx_int + conn_w + 2.0
    else:
        maxx = maxx_int + edge - gap
    maxy = max(_world_bbox(fp)[3] for fp in fps) + edge

    for fp in fps:
        bbox = _world_bbox(fp)
        if fp.ref in left_edge:
            fp.x = _snap(fp.x - (bbox[0] - 1.0))
        elif fp.ref in right_edge:
            fp.x = _snap(fp.x + (maxx - 1.0 - bbox[2]))

    board.outline = [(0.0, 0.0), (_snap(maxx), 0.0),
                     (_snap(maxx), _snap(maxy)), (0.0, _snap(maxy))]


def _grow_outline_for_copper(board, fps, clearance=EDGE_CU):
    """Final AUTO-SIZE pass: guarantee no PAD COPPER sits within `clearance` of
    the board edge -- KiCad copper_edge_clearance (measured: stm32 J2/J3 headers,
    whose pins overhang their courtyard, hung over the Edge.Cuts a courtyard-
    fitted outline drew).

    This ONLY ever TRANSLATES all parts uniformly and GROWS the rectangular
    outline -- it never moves a part relative to another, so it can NEVER create
    a courtyard overlap (the reason _shift_and_fit must not be made copper-aware:
    its per-part edge pulls shoved dense boards like stm32 into U1<->U2 overlaps).
    Auto-size only; a fixed user outline cannot grow. Returns mm grown (dx,dy,...)."""
    if not board.outline or not fps:
        return None
    ox = max(p[0] for p in board.outline)
    oy = max(p[1] for p in board.outline)
    cbs = [_copper_bbox(fp) for fp in fps]
    cminx = min(b[0] for b in cbs)
    cminy = min(b[1] for b in cbs)
    cmaxx = max(b[2] for b in cbs)
    cmaxy = max(b[3] for b in cbs)
    # Translate so the left/top-most copper clears the origin edge.
    dx = max(0.0, clearance - cminx)
    dy = max(0.0, clearance - cminy)
    if dx or dy:
        for fp in fps:
            fp.x = _snap(fp.x + dx)
            fp.y = _snap(fp.y + dy)
        cmaxx += dx
        cmaxy += dy
    W = _snap(max(ox, cmaxx + clearance))
    H = _snap(max(oy, cmaxy + clearance))
    board.outline = [(0.0, 0.0), (W, 0.0), (W, H), (0.0, H)]
    return (dx, dy, W, H)
