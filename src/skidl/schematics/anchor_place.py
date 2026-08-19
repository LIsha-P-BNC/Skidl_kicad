# -*- coding: utf-8 -*-
"""Anchor-centric schematic placement -- see docs/anchor_placer_design.md.

MILESTONE 5, increment 1: ANALYSIS foundation only (pipeline stages 1-3 +
topology). This module is PURE / READ-ONLY here -- it computes the placement
structure (signal graph -> functional clusters -> per-cluster + global anchor ->
hub/chain topology) WITHOUT mutating any part's ``.tx``. That lets the hardest,
most design-sensitive half be validated in isolation before the coordinate-
assigning stages (4-7) and the ``placement_mode="anchor"`` flag wiring land.

Design principle P1 (the keystone): the placement graph is built from SIGNAL
nets only -- power/ground nets are excluded, because on a power-dense board
GND/VCC touch nearly every part and would merge everything into one blob. Power
is resolved later as symbols, never as a placement edge.

Everything here is generic: decisions come from ref-prefix class, pin count,
signal-graph degree, and net role -- never from a part name.
"""

from collections import defaultdict, deque

# Active devices that can anchor a cluster (ICs, transistors). Generic, not
# per-part: an anchor is "the highest-pin-count active device in its cluster".
_ANCHOR_REF_PREFIXES = {"U", "IC", "Q", "A"}


def _part_set(parts):
    return {id(p): p for p in parts}


def signal_nets(nets):
    """P1: signal nets only -- drop power/ground (classify_net_role != None)."""
    from skidl.schematics.net_classify import classify_net_role
    return [n for n in nets if classify_net_role(n) is None]


def build_signal_graph(parts, nets):
    """Undirected adjacency over SIGNAL nets only. -> {part: set(neighbour parts)}.

    Power/ground edges are excluded (P1), so an MCU board no longer collapses
    into a single connected component."""
    members = _part_set(parts)
    adj = defaultdict(set)
    for p in parts:
        adj[p]  # ensure isolated (power-only) parts appear
    for net in signal_nets(nets):
        net_parts = []
        for pin in getattr(net, "pins", []):
            part = getattr(pin, "part", None)
            if part is not None and id(part) in members:
                net_parts.append(part)
        uniq = list({id(p): p for p in net_parts}.values())
        for i, a in enumerate(uniq):
            for b in uniq[i + 1:]:
                adj[a].add(b)
                adj[b].add(a)
    return adj


def detect_signal_clusters(parts, nets):
    """Connected components of the SIGNAL graph = functional clusters.

    Returns (clusters, adj). A part connected to the rest ONLY through power
    (e.g. a lone decoupling cap) becomes its own singleton cluster here; it is
    re-attached to its nearest anchor during placement (stage 4)."""
    adj = build_signal_graph(parts, nets)
    seen, clusters = set(), []
    for p in parts:
        if id(p) in seen:
            continue
        comp, dq = set(), deque([p])
        seen.add(id(p))
        while dq:
            x = dq.popleft()
            comp.add(x)
            for y in sorted(adj[x], key=_refkey):   # stable order (determinism)
                if id(y) not in seen:
                    seen.add(id(y))
                    dq.append(y)
        clusters.append(comp)
    return clusters, adj


def _pin_count(part):
    return len(getattr(part, "pins", []) or [])


def _refkey(part):
    """Stable per-part sort key (reference designator). Sets of Part OBJECTS
    iterate in id()/memory order, which varies between runs -> non-deterministic
    placement -> flaky publish. Sorting by ref makes every stage deterministic."""
    return str(getattr(part, "ref", "") or "")


def _rank(part):
    """Deterministic ordering key: most pins first, then ref."""
    return (-_pin_count(part), _refkey(part))


def cluster_anchor(cluster):
    """Anchor of a cluster = highest-pin-count active device (U/Q/IC...),
    else the highest-pin part of any kind. Deterministic tiebreak by ref."""
    actives = [
        p for p in cluster
        if (getattr(p, "ref_prefix", "") or "").upper() in _ANCHOR_REF_PREFIXES
    ]
    pool = actives or list(cluster)
    return min(pool, key=_rank) if pool else None


def global_anchor(clusters):
    """The single dominant anchor across all clusters (the MCU / FPGA / SoC) =
    the highest-pin-count cluster anchor. Deterministic tiebreak by ref."""
    anchors = [a for a in (cluster_anchor(c) for c in clusters) if a is not None]
    return min(anchors, key=_rank) if anchors else None


def classify_topology(parts, adj, g_anchor):
    """'hub' when one anchor dominates the signal graph (MCU boards); 'chain'
    when signal flow is roughly linear (sensor -> amp -> adc). Generic: uses the
    global anchor's signal degree vs. the graph, plus BFS depth spread."""
    n = max(len(parts), 1)
    hub_degree = len(adj.get(g_anchor, ())) if g_anchor is not None else 0
    hub_frac = hub_degree / n

    # BFS signal-flow depth spread (deep+narrow => chain).
    try:
        from skidl.schematics.net_classify import part_depth_map
        depths = part_depth_map(parts) or {}
        max_depth = max(depths.values()) if depths else 0
    except Exception:
        max_depth = 0

    # One node wired to a large fraction of the board => hub. A long flow with
    # no dominant node => chain.
    if hub_frac >= 0.34 and hub_degree >= 3:
        return "hub"
    if max_depth >= 4 and hub_frac < 0.34:
        return "chain"
    return "hub" if hub_frac >= 0.25 else "chain"


def analyze(circuit=None):
    """Read-only structural analysis for the current (or given) circuit.

    Returns a dict with the derived placement structure. No coordinates are
    assigned and nothing is mutated -- this is the validation surface for the
    analysis half of the anchor placer.
    """
    if circuit is None:
        import builtins
        circuit = getattr(builtins, "default_circuit", None)
    parts = [p for p in getattr(circuit, "parts", [])]
    nets = [n for n in getattr(circuit, "nets", [])]

    clusters, adj = detect_signal_clusters(parts, nets)
    g_anchor = global_anchor(clusters)
    topo = classify_topology(parts, adj, g_anchor)

    def _ref(p):
        return getattr(p, "ref", "?") if p is not None else None

    cluster_info = []
    for c in clusters:
        a = cluster_anchor(c)
        cluster_info.append({
            "anchor": _ref(a),
            "size": len(c),
            "members": sorted(_ref(p) for p in c),
        })
    # Biggest clusters first (the ones that shape the sheet).
    cluster_info.sort(key=lambda d: -d["size"])

    n_sig = len(signal_nets(nets))
    return {
        "n_parts": len(parts),
        "n_nets": len(nets),
        "n_signal_nets": n_sig,
        "n_power_nets": len(nets) - n_sig,
        "global_anchor": _ref(g_anchor),
        "topology": topo,
        "n_clusters": len(clusters),
        "clusters": cluster_info,
    }


# --- Milestone 5 inc-2/3: coordinate assignment ---------------------------------------
# Anchor at sheet centre; every other part placed in an outward spiral in
# BFS-signal-adjacency order (connected parts stay near the centre and each
# other), then a bbox-aware collision resolver spreads any overlaps. This
# directly targets the measured failures: anchor centrality, empty centre
# (density), and overlap count -- generically, from the graph only.


def _bfs_order(parts, adj, anchor):
    """Parts in BFS order from the anchor over the signal graph (unreached last)."""
    seen, order, dq = {id(anchor)}, [anchor], deque([anchor])
    while dq:
        x = dq.popleft()
        for y in sorted(adj.get(x, ()), key=lambda p: -_pin_count(p)):
            if id(y) not in seen:
                seen.add(id(y))
                order.append(y)
                dq.append(y)
    for p in parts:
        if id(p) not in seen:
            order.append(p)
            seen.add(id(p))
    return order


def _spiral_cells(n):
    """First n integer (col,row) cells of an outward square spiral, (0,0) first."""
    out = [(0, 0)]
    x = y = 0
    dx, dy = 1, 0
    seg_len, steps, turns = 1, 0, 0
    while len(out) < n:
        x += dx
        y += dy
        out.append((x, y))
        steps += 1
        if steps == seg_len:
            steps = 0
            dx, dy = -dy, dx           # turn left
            turns += 1
            if turns == 2:
                turns = 0
                seg_len += 1
    return out[:n]


def _pitch(parts):
    """Spiral cell pitch = a compact multiple of the median part size, on grid."""
    dims = []
    for p in parts:
        bb = getattr(p, "place_bbox", None)
        if bb is not None:
            dims.append(max(getattr(bb, "w", 0), getattr(bb, "h", 0)))
    dims.sort()
    med = dims[len(dims) // 2] if dims else 400
    # Give the router room to fit wires + labels between packed parts. Too tight
    # and the wired route fails (falls to all-label, where adjacent different-net
    # labels then collide -> MISMATCH). Roomier packing keeps intra-cluster nets
    # short enough to wire while leaving routing channels.
    return max(med * 2.0, 900)


def _world_bbox(part):
    return part.place_bbox * part.tx


def _resolve_collisions(order, anchor, iters=400):
    """Push overlapping parts apart (anchor stays fixed at centre). Uses
    place_bbox, which already includes labels stubbed BEFORE placement; labels
    stubbed AFTER placement (by _classify_and_stub_complex_nets) are handled by
    the tool-level M6 pass gen_schematic._relax_label_collisions, which runs
    for every placer -- including this one -- before routing. Deterministic,
    grid-snapped."""
    from skidl.geometry import Tx, Point

    def vbox(p):
        return _world_bbox(p)

    pad = 2.54  # one 100-mil cell of air between items
    for _ in range(iters):
        moved = False
        boxes = [(p, vbox(p)) for p in order]
        for i in range(len(boxes)):
            pi, bi = boxes[i]
            for j in range(i + 1, len(boxes)):
                pj, bj = boxes[j]
                ox = min(bi.max.x, bj.max.x) - max(bi.min.x, bj.min.x) + pad
                oy = min(bi.max.y, bj.max.y) - max(bi.min.y, bj.min.y) + pad
                if ox <= 0 or oy <= 0:
                    continue  # no overlap
                # Move the lower-priority part (anchor never moves; then fewer pins).
                if pi is anchor:
                    mover, other = pj, pi
                elif pj is anchor:
                    mover, other = pi, pj
                else:
                    mover = pi if _pin_count(pi) <= _pin_count(pj) else pj
                    other = pj if mover is pi else pi
                mb = vbox(mover)
                ob = vbox(other)
                # push along the smaller-overlap axis (least displacement)
                if ox <= oy:
                    step = ox if mb.ctr.x >= ob.ctr.x else -ox
                    mover.tx = mover.tx * Tx().move(Point(step, 0))
                else:
                    step = oy if mb.ctr.y >= ob.ctr.y else -oy
                    mover.tx = mover.tx * Tx().move(Point(0, step))
                moved = True
                boxes[order.index(mover)] = (mover, vbox(mover))
        if not moved:
            break


def _net_power_kind(net):
    """Classify a net by name as a power 'rail', a 'ground', or None (signal).

    Uses the same regexes the label/role classifier uses so the notion of
    "power rail" and "ground" is shared across the codebase (no local list)."""
    from skidl.schematics.net_classify import _POWER_RAIL_NET_RE, _GROUND_NET_RE
    name = getattr(net, "name", "") or ""
    if _GROUND_NET_RE.match(name):
        return "ground"
    if _POWER_RAIL_NET_RE.match(name):
        return "rail"
    return None


# Passive ref prefixes whose vertical orientation we normalise. Deliberately
# EXCLUDES D/LED (a 180 flip would reverse the drawn diode direction) and any
# active/3-pin part; caps, resistors, inductors, ferrite beads and networks are
# non-polarised in the drawn sense, so flipping only swaps which end is up.
_PASSIVE_PREFIXES = ("C", "R", "L", "FB", "RN")


def _is_passive_ref(ref):
    ref = str(ref or "")
    for pre in _PASSIVE_PREFIXES:
        if ref[: len(pre)].upper() == pre and ref[len(pre) : len(pre) + 1].isdigit():
            return True
    return False


def _orient_passives_rail_up(parts):
    """Orient vertical 2-pin passives so the power-RAIL pin is the UPPER pin and
    the GROUND pin the LOWER pin -- the universal schematic convention.

    Why: a power symbol is stubbed off its pin in the pin's away-from-body
    direction, and the symbol's heading is fixed (rail arrow UP, ground arrow
    DOWN). If a decap's +V pin faces DOWN, the +V symbol lands BELOW the part
    and its up-arrow points back INTO the circuit. Flipping the part 180 puts
    the rail pin up so the arrow exits the circuit -- for ANY circuit, from the
    connectivity alone, no per-device rules.

    A 180 rotation preserves the symmetric 2-pin bbox (so collision state is
    unchanged) and merely swaps the two pins; routing runs afterward on the
    swapped geometry. Returns the number of parts flipped."""
    from skidl.geometry import Tx, Point
    import os as _os
    # Escape hatch: set SKIDL_NO_ORIENT=1 to disable this heuristic entirely
    # (parts keep the placer's raw orientation).
    if _os.environ.get("SKIDL_NO_ORIENT"):
        return 0

    flipped = 0
    for part in parts:
        if not _is_passive_ref(getattr(part, "ref", "")):
            continue
        pins = [
            p for p in getattr(part, "pins", []) if getattr(p, "net", None) is not None
        ]
        if len(pins) != 2:
            continue
        ptx = getattr(part, "tx", None) or Tx()

        def _world(pin):
            lp = getattr(pin, "pt", None)
            if lp is None:
                lp = Point(getattr(pin, "x", 0.0), getattr(pin, "y", 0.0))
            return lp * ptx

        a, b = pins
        wa, wb = _world(a), _world(b)
        # Only VERTICAL passives: a horizontal passive stubs its power symbol
        # sideways (arrow up, symbol beside the pin), which already reads fine.
        if abs(wa.y - wb.y) <= abs(wa.x - wb.x):
            continue
        upper = a if wa.y >= wb.y else b
        lower = b if upper is a else a
        kinds = {id(a): _net_power_kind(a.net), id(b): _net_power_kind(b.net)}
        rails = [p for p in pins if kinds[id(p)] == "rail"]
        grounds = [p for p in pins if kinds[id(p)] == "ground"]

        need_flip = False
        if len(rails) == 1 and kinds[id(upper)] != "rail":
            need_flip = True          # rail belongs on top
        elif not rails and len(grounds) == 1 and kinds[id(lower)] != "ground":
            need_flip = True          # ground belongs on the bottom

        if need_flip:
            part.tx = Tx().rot(180) * ptx
            flipped += 1
    return flipped


def place_node(node, **options):
    """Anchor-centric placement of a node's parts. Returns True on success,
    False to fall back to the legacy placer (caller handles the fallback).

    Assigns each real part a .tx; reuses the legacy bbox / anchor-pin / net-
    terminal helpers so the router sees exactly the shape it expects."""
    from skidl.schematics import place as _pl
    from skidl.geometry import Tx, Point

    real = [p for p in node.parts if not _pl.is_net_terminal(p)]
    if len(real) < 4:
        return False  # tiny nodes: legacy places them fine
    try:
        nets = node.get_internal_nets()
    except Exception:
        nets = list(getattr(node, "nets", []))

    _pl.add_placement_bboxes(real, **options)

    # M6 HIERARCHICAL CLUSTER PACKING. (1) group parts into functional clusters
    # (signal clusters + power-only decaps re-attached to their IC's cluster);
    # (2) pack each cluster TIGHT around its anchor -> members share short nets,
    # which the router wires (not labels); (3) place the CLUSTERS as units with
    # generous gaps -> only inter-cluster nets are far enough to label. This
    # "wire-in / label-between" is what lets the sheet publish AND read well.
    clusters, adj = build_clusters(real, nets)
    g_anchor = global_anchor(clusters)
    if g_anchor is None or _pin_count(g_anchor) < 3:
        return False  # no dominant anchor -> let legacy handle it

    packed = []
    for cl in clusters:
        _pack_cluster(cl, adj)
        packed.append((cl, _cluster_bbox(cl)))
    anchor_cluster = next((cl for cl in clusters if g_anchor in cl), clusters[0])
    _place_cluster_units(packed, anchor_cluster)

    for part in real:
        _pl.snap_to_grid(part)

    # Normalise 2-pin passive orientation (rail pin up / ground pin down) so
    # each part's power symbol lands on the away side and its arrow exits the
    # circuit. Done after snap (flip is about the grid-aligned centre) and
    # before routing so the router sees the final pin geometry.
    _orient_passives_rail_up(real)

    _pl.add_anchor_pull_pins(real, nets, **options)
    net_terminals = [p for p in node.parts if _pl.is_net_terminal(p)]
    if net_terminals:
        _pl.place_net_terminals(
            net_terminals, real, nets, _pl.total_part_force, **options
        )
    return True


# --- M6: cluster building, packing, and cluster-unit placement ------------------------


# Ref prefixes that may SEED a sub-cluster (active multi-pin devices). Q is
# deliberately excluded from seeding (a board full of discrete transistors must
# not explode into one sheet per transistor); Q can still anchor a cluster that
# forms around it via the fallback path.
_SEED_REF_PREFIXES = {"U", "IC", "A"}


def build_clusters(parts, nets):
    """Functional clusters: PER-ANCHOR sub-clustering (M8b).

    Connected-components under-segments: on an MCU board every comms/memory IC
    is signal-linked to the MCU, so everything collapses into ONE giant cluster
    (benchmark: 3 boards x "2 sheets instead of 4-6"). Instead, every active IC
    (U/IC/A prefix, >=3 pins) SEEDS its own cluster and a multi-source BFS over
    the SIGNAL graph assigns each remaining part to its graph-nearest anchor
    (ties: anchor rank order, deterministic). Power-only parts (no signal edge):
    decoupling caps join the anchor nearest by CREATION ORDER (SKiDL authors
    create a block's decaps next to its IC -- the only proximity signal a flat
    netlist has); the rest (regulator, input caps, power connector, rail test
    points) form one power cluster. Tiny clusters (<3) merge into the cluster
    they share the most signal edges with, so no meaningless 1-2 part sheets."""
    from collections import deque as _dq

    signal_clusters, adj = detect_signal_clusters(parts, nets)

    anchors = sorted(
        (p for p in parts
         if (getattr(p, "ref_prefix", "") or "").upper() in _SEED_REF_PREFIXES
         and _pin_count(p) >= 3
         and adj.get(p)),   # must have >=1 SIGNAL edge: a power-only device
                            # (e.g. a linear regulator) belongs to the power
                            # cluster, not an empty seed that then gets
                            # size-merged into the MCU sheet (observed: C1_C
                            # sheet + regulator absorbed into U1)
        key=_rank,
    )
    if len(anchors) <= 1:
        # Single-anchor / anchor-less board: connected components are fine.
        multi = [set(c) for c in signal_clusters if len(c) >= 2]
        singles = {next(iter(c)) for c in signal_clusters if len(c) == 1}
        if singles:
            multi.append(singles)
        return multi, adj

    # Multi-source BFS: each anchor claims its graph-nearest satellites.
    # LEVEL-SYNCHRONOUS so distance ties are resolved deterministically by
    # CREATION-ORDER proximity (SKiDL authors create a block's satellites next
    # to its IC -- e.g. I2C pullups written beside the EEPROM belong to the
    # EEPROM cluster even though the shared bus also touches the MCU). A plain
    # first-come BFS gave every tied satellite to the biggest anchor, bloating
    # the MCU sheet past routability (observed).
    order_ix = {id(p): i for i, p in enumerate(parts)}
    owner = {id(a): a for a in anchors}
    frontier = list(anchors)
    while frontier:
        claims = {}
        for x in frontier:
            for y in adj.get(x, ()):
                if id(y) not in owner:
                    claims.setdefault(id(y), (y, set()))[1].add(owner[id(x)])
        nxt = []
        for _yid, (y, cands) in sorted(claims.items(),
                                       key=lambda kv: _refkey(kv[1][0])):
            a = min(cands, key=lambda A: (abs(order_ix[id(A)] - order_ix[id(y)]),
                                          _refkey(A)))
            owner[id(y)] = a
            nxt.append(y)
        frontier = nxt

    clusters_by_anchor = {id(a): {a} for a in anchors}
    unclaimed = []
    for p in parts:
        if p in anchors:
            continue
        a = owner.get(id(p))
        if a is not None:
            clusters_by_anchor[id(a)].add(p)
        else:
            unclaimed.append(p)          # power-only: no signal path to any anchor

    # Power-only parts: decaps follow their IC by creation order; the rest is
    # the power cluster.
    try:
        from skidl.schematics.cluster import is_decoupling_cap
    except Exception:
        def is_decoupling_cap(_p):
            return False
    power_misc = set()
    for p in unclaimed:
        if is_decoupling_cap(p):
            a = min(anchors, key=lambda x: (abs(order_ix[id(x)] - order_ix[id(p)]),
                                            _refkey(x)))
            clusters_by_anchor[id(a)].add(p)
        else:
            power_misc.add(p)

    clusters = [clusters_by_anchor[id(a)] for a in anchors]
    if power_misc:
        clusters.append(power_misc)

    # Merge tiny clusters into their most-connected sibling (never emit a
    # 1-2 part sheet). Signal-edge count decides; ties by target size then rank.
    def _edges_between(c1, c2):
        return sum(1 for p in c1 for q in adj.get(p, ()) if q in c2)

    merged = True
    while merged and len(clusters) > 1:
        merged = False
        for i, c in enumerate(clusters):
            if len(c) >= 3:
                continue
            best_j, best_score = None, None
            for j, other in enumerate(clusters):
                if j == i:
                    continue
                score = (_edges_between(c, other), len(other))
                if best_score is None or score > best_score:
                    best_j, best_score = j, score
            if best_j is not None:
                clusters[best_j] |= c
                del clusters[i]
                merged = True
                break

    return clusters, adj


def _cluster_bbox(cluster):
    from skidl.geometry import BBox
    bb = BBox()
    for p in cluster:
        bb.add(_world_bbox(p))
    return bb


def _move_cluster(cluster, dx, dy):
    from skidl.geometry import Tx, Point
    for p in cluster:
        p.tx = p.tx * Tx().move(Point(dx, dy))


def _anchor_pin_geometry(anchor):
    """{net_name: [(pin_local_pt, exit_unit_dir), ...]} for the anchor's pins.

    exit_dir = the away-from-body direction (from part origin toward the pin),
    which is where a satellite on that pin should sit. Returns {} if pin geometry
    is unavailable (caller then falls back to a spiral)."""
    import math as _m
    from skidl.geometry import Point
    geom = {}
    for pin in getattr(anchor, "pins", []):
        net = getattr(pin, "net", None)
        if net is None:
            continue
        pt = getattr(pin, "pt", None)
        if pt is None:
            x = getattr(pin, "x", None)
            y = getattr(pin, "y", None)
            if x is None or y is None:
                continue
            pt = Point(x, y)
        d = _m.hypot(pt.x, pt.y)
        if d == 0:
            continue
        geom.setdefault(getattr(net, "name", "") or "", []).append(
            (pt, Point(pt.x / d, pt.y / d))
        )
    return geom


def _pack_cluster(cluster, adj=None):
    """Pack a cluster's parts around its anchor (anchor at the local origin).

    Anchor's direct signal-neighbours (crystal, reset, the parts it talks to)
    get the innermost spiral cells so they land adjacent; power-only satellites
    (decaps) fill in after. Applied PER CHILD SHEET (M10), this pulls each IC's
    satellites in tight around it (measured: decap->IC 69mm legacy -> 59mm).

    NOTE: a true "1-grid hug" (pin-adjacent ring) was tried and regressed --
    a big IC's LABEL bbox (~60 mm for an LQFP-48) means "just outside the symbol"
    is already ~40 mm from centre, and the collision resolver pushes small parts
    further off the large bbox. The spiral is the best available without
    body-bbox-aware constraint placement (a deeper M12-tier item)."""
    from skidl.geometry import Tx, Point
    anchor = cluster_anchor(cluster)
    nbrs = adj.get(anchor, set()) if adj else set()
    order = [anchor] + sorted(
        (p for p in cluster if p is not anchor),
        key=lambda p: (0 if p in nbrs else 1, _rank(p)),
    )
    pitch = _pitch(list(cluster))
    for part, (cx, cy) in zip(order, _spiral_cells(len(order))):
        part.tx = Tx().move(Point(cx * pitch, cy * pitch))
    _resolve_collisions(order, anchor)


def _place_cluster_units(packed, anchor_cluster, gap=1200.0):
    """Place whole clusters on a spiral (anchor cluster at centre), then push
    overlapping cluster bboxes apart. Generous gap so inter-cluster nets label
    while intra-cluster nets (now local) stay wired."""
    ordered = [pc for pc in packed if pc[0] is anchor_cluster]
    ordered += sorted((pc for pc in packed if pc[0] is not anchor_cluster),
                      key=lambda pc: (-len(pc[0]), _refkey(cluster_anchor(pc[0]))))
    dims = [max(bb.w, bb.h) for _, bb in ordered]
    pitch = (max(dims) if dims else 500) + gap
    for (cluster, _bb), (cx, cy) in zip(ordered, _spiral_cells(len(ordered))):
        ctr = _cluster_bbox(cluster).ctr
        _move_cluster(cluster, cx * pitch - ctr.x, cy * pitch - ctr.y)
    _resolve_cluster_collisions([c for c, _ in ordered], anchor_cluster, gap)


def _resolve_cluster_collisions(clusters, anchor_cluster, gap, iters=200):
    """Push overlapping cluster bboxes apart (anchor cluster fixed at centre)."""
    for _ in range(iters):
        moved = False
        boxes = [(c, _cluster_bbox(c)) for c in clusters]
        for i in range(len(boxes)):
            ci, bi = boxes[i]
            for j in range(i + 1, len(boxes)):
                cj, bj = boxes[j]
                ox = min(bi.max.x, bj.max.x) - max(bi.min.x, bj.min.x) + gap
                oy = min(bi.max.y, bj.max.y) - max(bi.min.y, bj.min.y) + gap
                if ox <= 0 or oy <= 0:
                    continue
                mover = cj if ci is anchor_cluster else (
                    ci if cj is anchor_cluster else (ci if len(ci) <= len(cj) else cj))
                mb = _cluster_bbox(mover)
                other = ci if mover is cj else cj
                ob = _cluster_bbox(other)
                if ox <= oy:
                    _move_cluster(mover, ox if mb.ctr.x >= ob.ctr.x else -ox, 0)
                else:
                    _move_cluster(mover, 0, oy if mb.ctr.y >= ob.ctr.y else -oy)
                moved = True
                boxes[clusters.index(mover)] = (mover, _cluster_bbox(mover))
        if not moved:
            break


# --- M8: AUTO HIERARCHY BUILDER --------------------------------------------------------
# Turn detected functional clusters into REAL SKiDL hierarchy (one Node child of
# circuit.root per cluster) so the schematic generator emits one hierarchical
# sheet per cluster with correct cross-sheet connectivity -- exactly as if the
# user had written @subcircuit blocks.
#
# Grounded facts (verified against the code, 2026-07-28):
#   * part.hiertuple is DERIVED from part.node (part.py:1171); part.node is the
#     ONLY persisted hierarchy link (circuit.py:376). Moving a part post-hoc =
#     remove from old node.parts + append to new node.parts + set part.node.
#   * SchNode.add_circuit partitions sheets and creates cross-sheet NetTerminals
#     purely by comparing part.hiertuple across each net's pins
#     (sch_node.py:183-186); net.node is never consulted. So moving PARTS alone
#     yields a correct multi-sheet netlist with auto-generated ports.
#   * part.group does NOT do this (group-derived children get no NetTerminals ->
#     broken multi-sheet netlists) -- which is why this builder exists.


def auto_hierarchy(circuit=None, min_cluster=3):
    """Build real hierarchy nodes from detected functional clusters (M8).

    Only acts on a FLAT design (root has no children -- explicit @subcircuit
    structure always wins). Parts in clusters smaller than ``min_cluster`` stay
    in the root and render on the top sheet alongside the cluster sheets.

    Returns the number of hierarchy nodes created (0 = nothing done).
    """
    import re as _re

    if circuit is None:
        import builtins
        circuit = getattr(builtins, "default_circuit", None)
    root = getattr(circuit, "root", None)
    if root is None or getattr(root, "children", None):
        return 0  # already hierarchical -- never fight explicit structure
    parts = [
        p for p in getattr(circuit, "parts", [])
        if not str(getattr(p, "ref", "") or "").startswith("#")
        and getattr(p, "node", None) is root
    ]
    if len(parts) < 8:
        return 0  # tiny designs stay single-sheet
    nets = list(getattr(circuit, "nets", []))

    clusters, _adj = build_clusters(parts, nets)
    real_clusters = [c for c in clusters if len(c) >= min_cluster]
    if len(real_clusters) < 2:
        return 0  # no meaningful decomposition -> stay flat

    from skidl.node import Node

    made = 0
    for cl in sorted(real_clusters, key=lambda c: _refkey(cluster_anchor(c))):
        a = cluster_anchor(cl)
        raw = "{}_{}".format(getattr(a, "ref", "blk"),
                             getattr(a, "name", "") or "")
        name = _re.sub(r"[^\w.+-]+", "_", raw).strip("_") or "block"
        node = Node(name, tag=name, circuit=circuit)
        circuit.nodes.add(node)
        root.add_child(node)          # uniquifies the name + sets parent
        for p in sorted(cl, key=_refkey):
            try:
                p.node.parts.remove(p)
            except (ValueError, AttributeError):
                pass
            node.parts.append(p)
            p.node = node             # hiertuple/hiername now derive from here
        made += 1
    return made


# --- benchmark (shared metric scorer for legacy vs anchor A/B) -------------------------


def benchmark(sch_path):
    """Score a generated .anvil_sch on the design-doc metrics (span, density,
    overlap count, anchor centrality). Pure post-hoc read of the sheet."""
    import re
    import math as _m
    t = open(sch_path, encoding="utf-8", errors="replace").read()
    syms = []
    for m in re.finditer(r'\(symbol\s+\(lib_id\s+"([^"]+)"\)\s*\(at\s+([-\d.]+)\s+([-\d.]+)', t):
        seg = t[m.start():m.start() + 900]
        rm = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', seg)
        ref = rm.group(1) if rm else "?"
        if re.match(r"^[A-Za-z]+\d", ref):
            syms.append((ref, float(m.group(2)), float(m.group(3))))
    if not syms:
        return {}
    xs = [s[1] for s in syms]
    ys = [s[2] for s in syms]
    span_x, span_y = max(xs) - min(xs), max(ys) - min(ys)
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    # overlap proxy: symbol-origin pairs closer than one 5.08mm cell
    overlaps = sum(
        1
        for i in range(len(syms))
        for j in range(i + 1, len(syms))
        if _m.dist(syms[i][1:], syms[j][1:]) < 12.0
    )
    return {
        "n_symbols": len(syms),
        "span_x_mm": round(span_x, 1),
        "span_y_mm": round(span_y, 1),
        "sheet_center": (round(cx, 1), round(cy, 1)),
        "overlap_pairs_lt12mm": overlaps,
    }
