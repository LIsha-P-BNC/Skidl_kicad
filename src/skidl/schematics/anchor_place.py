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

    # M11 AUTHORED-BLOCK AWARENESS: when the script grouped parts into
    # functional blocks (`with smart_schematic.block(...)` / auto-grouping),
    # those groups ARE the clusters -- each block is a complete function
    # (input -> process -> output), so pack IT tight around ITS own anchor and
    # lay the blocks left-to-right in AUTHOR (creation) order, which is the
    # script's signal-flow order. Re-deriving clusters from the signal graph
    # here would fight the drawn block boxes (parts placed outside their box).
    groups = {}
    ungrouped = []
    for p in real:
        g = getattr(p, "group", None)
        if g:
            groups.setdefault(str(g), set()).add(p)
        else:
            ungrouped.append(p)

    # SINGLE-ANCHOR FLOW GUARD: the anchor packer's value is packing MULTIPLE
    # clusters/blocks tight. A circuit with ONE dominant IC (a regulator or MCU
    # + its passives) and no second block has nothing to pack against -- the
    # spiral then separates the signal core from the power-only passives and
    # scatters them, while the LEGACY directional placer lays the same parts in
    # a clean left->right flow (measured buck_12v_5v_demo: anchor Y-span 163 mm
    # vs legacy 38 mm, both fully wired). So hand a single-anchor design to the
    # legacy placer. Kill switch: SKIDL_ANCHOR_SINGLE=1 forces the old spiral.
    import os as _os
    _anchor_count = sum(
        1 for p in real
        if (getattr(p, "ref_prefix", "") or "").upper() in _SEED_REF_PREFIXES
        and _pin_count(p) >= 3
    )
    if (len(groups) < 2 and _anchor_count < 2
            and _os.environ.get("SKIDL_ANCHOR_SINGLE") != "1"):
        return False

    if len(groups) >= 2:
        _, adj = detect_signal_clusters(real, nets)
        clusters = [set(c) for c in groups.values()]
        # Ungrouped strays join the block they share the most signal edges
        # with; anything with no signal edge to any block becomes one misc
        # cluster (placed last in the row).
        for p in ungrouped:
            best = max(
                clusters,
                key=lambda c: (sum(1 for q in adj.get(p, ()) if q in c), len(c)),
            )
            if sum(1 for q in adj.get(p, ()) if q in best):
                best.add(p)
        left = [p for p in ungrouped if not any(p in c for c in clusters)]
        if left:
            clusters.append(set(left))
        g_anchor = global_anchor(clusters)
        if g_anchor is None or _pin_count(g_anchor) < 3:
            return False  # no dominant anchor -> let legacy handle it
        packed = []
        for cl in clusters:
            _pack_cluster(cl, adj)
            packed.append((cl, _cluster_bbox(cl)))
        order_ix = {id(p): i for i, p in enumerate(real)}
        # 2D BLOCK-GRID (gap H1): opt-in (SKIDL_BLOCK_GRID=1). Compact 2D grid of
        # boxed blocks with the MCU centred -- the dev-board composition. Falls
        # back to the proven flow row if it declines (no anchor) or is disabled;
        # the build-level connectivity verify is the final safety net.
        if not (_os.environ.get("SKIDL_BLOCK_GRID") == "1"
                and place_block_grid(packed, g_anchor)):
            _place_cluster_flow_row(packed, order_ix)
    else:
        # M6 HIERARCHICAL CLUSTER PACKING. (1) group parts into functional
        # clusters (signal clusters + power-only decaps re-attached to their
        # IC's cluster); (2) pack each cluster TIGHT around its anchor ->
        # members share short nets, which the router wires (not labels);
        # (3) place the CLUSTERS as units with generous gaps -> only
        # inter-cluster nets are far enough to label. This "wire-in /
        # label-between" is what lets the sheet publish AND read well.
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

    net_terminals = [p for p in node.parts if _pl.is_net_terminal(p)]
    if net_terminals:
        _pl.add_placement_bboxes(net_terminals, **options)
    # ONE combined call, like the legacy path (place.py:1563-1564): a
    # terminal's pull_pins only populate when the REAL parts sharing its net
    # are in the same `parts` list (add_anchor_pull_pins filters pins by
    # `pin.part in parts`, and two separate calls also clobber net.parts).
    # Latent M10 gap: auto-hierarchy child sheets carried no terminals, so
    # the real-parts-only call never bit until a grouped root sheet (M11).
    _pl.add_anchor_pull_pins(real + net_terminals, nets, **options)
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


def flow_place_hinted(node, **options):
    """Author-driven FLOW placement. When the script tags parts with a
    `flow_x` hint (an integer column: input on the left, output on the right),
    lay the parts out in those columns left-to-right -- the exact
    input -> process -> output flow the author intends.

    This sidesteps the impossible auto-detection of input/output rails on
    switching topologies (a buck's output rail is downstream of the inductor
    with no direct IC pin). The AI writing the circuit KNOWS which part is
    input vs output, so it states it; the engine just arranges. Parts within a
    column stack vertically in `flow_y` order (default: creation order).
    No hints present -> no-op. Returns the number of parts moved."""
    from skidl.geometry import Tx, Point
    from skidl.schematics.place import is_net_terminal, snap_to_grid

    real = [p for p in getattr(node, "parts", []) if not is_net_terminal(p)]
    hinted = [p for p in real if getattr(p, "flow_x", None) is not None]
    if len({getattr(p, "flow_x") for p in hinted}) < 2:
        return 0

    order_ix = {id(p): i for i, p in enumerate(real)}

    def _wh(p):
        bb = _world_bbox(p)
        return max(getattr(bb, "w", 400), 150), max(getattr(bb, "h", 400), 150)

    def _pin_span_w(p):
        """Horizontal span of the part's PINS in world coords (NOT the text box).

        Spacing on the pin span -- plus a fixed clearance -- lets same-function
        parts sit genuinely tight (a vertical 2-pin cap has ~0 pin-span, so it
        packs to just the clearance) instead of every part reserving room for its
        wide value text. Value text may then overhang slightly between parts; a
        function boundary's larger gap keeps blocks legible. Falls back to the
        text-box width if pin coords are unavailable."""
        xs = []
        for pin in getattr(p, "pins", []):
            pt = getattr(pin, "pt", None)
            if pt is None:
                continue
            wp = pt * p.tx
            xs.append(wp.x)
        if len(xs) >= 2:
            return max(xs) - min(xs)
        w, _h = _wh(p)
        return min(w, 200.0)  # single-pin / unknown: a narrow default

    # SINGLE ROW, side-by-side: every hinted part gets its OWN x-slot (ordered
    # by flow_x, then flow_y), all on ONE horizontal baseline. No vertical
    # stacking -- so each part's power pin has a CLEAR vertical path up to a
    # top power rail and each ground pin a clear path down to a bottom GND rail
    # (the TRACKER ladder). Stacking two caps at one x blocked the lower cap's
    # rail stub; a single row removes that.
    ordered = sorted(hinted, key=lambda p: (int(getattr(p, "flow_x")),
                                            int(getattr(p, "flow_y", 0)),
                                            order_ix.get(id(p), 0)))

    # FUNCTION-AWARE gaps: neighbouring parts sit CLOSE when they belong to the
    # same function -- the same flow_x column (author's declared group, e.g. the
    # two input caps) OR sharing a routed SIGNAL net (e.g. U1-L1-D1 all on
    # SW_NODE, the switching cluster that must stay tight). A jump to a different
    # function gets a wider gap so the blocks read as separate groups. Power/GND
    # nets do NOT count as "shared" -- every part touches them, so they would
    # collapse every gap to tight and defeat the grouping.
    import re as _re
    _PWR = _re.compile(r"^(gnd\w*|agnd|dgnd|\+.*|v(cc|dd|ss|in|bat|sys|bus|out)\w*"
                       r"|\d+v\d*|.*_\d+v\d*)$", _re.I)

    def _sig_nets(p):
        s = set()
        for pin in getattr(p, "pins", []):
            net = getattr(pin, "net", None)
            for n in (net if isinstance(net, (list, tuple, set)) else [net]):
                nm = getattr(n, "name", None)
                if nm and not _PWR.match(str(nm)):
                    s.add(str(nm))
        return s

    def _pin_cx(p):
        """World-x centre of the part's pin span (what the slot is built around)."""
        xs = []
        for pin in getattr(p, "pins", []):
            pt = getattr(pin, "pt", None)
            if pt is not None:
                xs.append((pt * p.tx).x)
        return (min(xs) + max(xs)) / 2.0 if xs else _world_bbox(p).ctr.x

    # Space on the PIN span + a clearance, not the wide value-text box, so
    # same-function parts genuinely pack tight. MIN_SLOT keeps bodies apart even
    # for zero-span vertical parts. INTRA vs INTER gap makes the functional
    # blocks read as groups (the user's rule: gap depends on function).
    MIN_SLOT = 250.0    # body slot for a vertical 2-pin part
    INTRA_GAP = 220.0   # same-function neighbours sit tight
    INTER_GAP = 620.0   # a new function: a clear block boundary
    moved = 0
    x_cursor = 0.0
    base_y = _world_bbox(ordered[0]).ctr.y
    prev, prev_sig = None, set()
    for p in ordered:
        ew = max(_pin_span_w(p), MIN_SLOT)
        cur_sig = _sig_nets(p)
        if prev is not None:
            same_fn = (int(getattr(p, "flow_x")) == int(getattr(prev, "flow_x"))
                       or bool(prev_sig & cur_sig))
            x_cursor += INTRA_GAP if same_fn else INTER_GAP
        col_cx = x_cursor + ew / 2.0
        pcx = _pin_cx(p)
        pc = _world_bbox(p).ctr
        p.tx = p.tx * Tx().move(Point(col_cx - pcx, base_y - pc.y))
        snap_to_grid(p)
        x_cursor += ew
        prev, prev_sig = p, cur_sig
        moved += 1
    return moved


def flow_place_block(node, **options):
    """AUTO within-block flow: lay a functional block's parts in a TIGHT single
    row (creation order == the author's input->process->output order), centred on
    the block's current location, so the block's LOCAL signal nets are short and
    the router WIRES them instead of labeling. The buck proved a tight row wires
    100%; the legacy scatter and the spiral both leave a single-anchor block's
    SW_NODE / indicator R-LED as labels. Runs ONLY on a real single-group block
    (>=3 parts) with NO manual flow_x hints (those go through flow_place_hinted).
    Power nets still render as per-pin symbols; only within-block SIGNAL nets go
    from labels to wires. Kill switch SKIDL_FLOW_BLOCK=0. Returns parts moved."""
    import os as _os
    if _os.environ.get("SKIDL_FLOW_BLOCK") == "0":
        return 0
    from skidl.geometry import Tx, Point
    from skidl.schematics.place import is_net_terminal, snap_to_grid

    real = [p for p in getattr(node, "parts", []) if not is_net_terminal(p)]
    if len(real) < 3:
        return 0
    if any(getattr(p, "flow_x", None) is not None for p in real):
        return 0  # author hints -> flow_place_hinted handles it
    _grps = {getattr(p, "group", None) for p in real}
    _single_group = len(_grps) == 1 and None not in _grps
    # A @subcircuit PAGE in hierarchy mode is itself ONE function (docs D.0:
    # one sheet = one function), so an UNGROUPED child-sheet node rows exactly
    # like an authored block -- that is what gives every hierarchy child sheet
    # the same ladder treatment as a flat block. The ROOT of a flat ungrouped
    # design (parent is None) still bails to the legacy/hug path.
    _is_page = getattr(node, "parent", None) is not None and None in _grps
    if not (_single_group or _is_page):
        return 0  # a flat/mixed root node is not a single function
    # A tight ROW only suits a SMALL, LINEAR stage (power stage, analog chain).
    # A block with a big IC (MCU/large device, many pins) or many parts fans out
    # in 2-D; rowing it strings the fan-out and the router LABELS more, not less
    # (measured: stm32 12->33, arduino 0->24 labels). Leave those to the anchor
    # hug. Threshold: no part >16 pins, <=10 parts.
    if len(real) > 10 or any(_pin_count(p) > 16 for p in real):
        return 0

    import builtins
    try:
        cidx = {id(p): i for i, p in enumerate(builtins.default_circuit.parts)}
    except Exception:
        cidx = {}
    ordered = sorted(real, key=lambda p: cidx.get(id(p), 0))

    def _pin_xs(p):
        xs = []
        for pin in getattr(p, "pins", []):
            pt = getattr(pin, "pt", None)
            if pt is not None:
                xs.append((pt * p.tx).x)
        return xs

    def _pin_span_w(p):
        xs = _pin_xs(p)
        return (max(xs) - min(xs)) if len(xs) >= 2 else min(getattr(_world_bbox(p), "w", 200.0), 200.0)

    def _pin_cx(p):
        xs = _pin_xs(p)
        return (min(xs) + max(xs)) / 2.0 if xs else _world_bbox(p).ctr.x

    # keep the block where the placer put it: centre the row on the current centroid
    cxs = [_world_bbox(p).ctr.x for p in real]
    cys = [_world_bbox(p).ctr.y for p in real]
    cen_x = sum(cxs) / len(cxs)
    base_y = sum(cys) / len(cys)

    MIN_SLOT = 200.0
    # Honor the generation retry ladder's expansion factor: the tight row is
    # exactly what starves dense blocks of routing corridors/terminals
    # (measured wlc PROBE_SENSE/PUMP_RELAY: expand-retry re-placed with 1.5x
    # then 2.25x, and this fixed GAP silently reverted both to the same tight
    # row every attempt).
    _exp = 1.0
    try:
        _exp = max(float(options.get("expansion_factor", 1.0) or 1.0), 1.0)
    except Exception:
        pass
    GAP = 150.0 * _exp

    def _sig_net_ids(p):
        return {id(_pn.net) for _pn in getattr(p, "pins", [])
                if getattr(_pn, "net", None) is not None
                and _net_power_kind(_pn.net) not in ("rail", "ground")}

    def _pair_gap(a, b):
        """Gap between two row neighbours sized to the ROUTING DEMAND of the
        corridor between them: every net shared by the pair needs terminal
        slots on the corridor's faces (GRID=50 -> ~2 slots per 100 mil).
        A fixed 150 gap gives 3 slots -- a 3-net relay->terminal bundle
        exhausts it at every seed (measured wlc PUMP_RELAY: get_next_terminal
        starvation). Nets counted from the pair's own pins -- dynamic."""
        _shared = len(_sig_net_ids(a) & _sig_net_ids(b))
        return max(GAP, (_shared + 2) * 100.0 * _exp)

    # --- CHAIN-STACK: a FAN block -- one HUB part feeding >=2 parallel
    # multi-part chains (e.g. a probe connector driving 3 identical
    # R->Q->pullup channels) -- cannot route as ONE tight row: every
    # channel's nets share the same horizontal corridor and the switchbox
    # runs out of face terminals (measured wlc PROBE_SENSE: unroutable at
    # EVERY seed and expansion). Draw it the way a human does: hub on the
    # LEFT, one ROW PER CHAIN stacked vertically, rows ordered by the hub
    # pin each chain attaches to -- a planar fan with zero forced
    # crossings. Pure connectivity+geometry (no part names, no per-circuit
    # constants). Kill switch SKIDL_CHAIN_STACK=0.
    _flow_dbg = _os.environ.get("SKIDL_FLOW_DEBUG")
    if _flow_dbg:
        print(f">>> [flow-debug] node='{getattr(node, 'name', '?')}' "
              f"real={len(real)} groups={_grps} page={_is_page} "
              f"refs={[getattr(p, 'ref', '?') for p in real]}")
    if _os.environ.get("SKIDL_CHAIN_STACK", "1") != "0" and len(real) >= 4:
        _adj = {id(p): set() for p in real}
        _by_id = {id(p): p for p in real}
        for p in real:
            for _pn in getattr(p, "pins", []):
                _nt = getattr(_pn, "net", None)
                if _nt is None or _net_power_kind(_nt) in ("rail", "ground"):
                    continue
                for _op in getattr(_nt, "pins", []):
                    _opart = getattr(_op, "part", None)
                    if (_opart is not None and id(_opart) in _adj
                            and _opart is not p):
                        _adj[id(p)].add(id(_opart))
        _hub = max(real, key=lambda p: (len(_adj[id(p)]), _pin_count(p),
                                        -cidx.get(id(p), 0)))
        if _flow_dbg:
            print(f">>> [flow-debug]   hub={getattr(_hub, 'ref', '?')} "
                  f"deg={len(_adj[id(_hub)])}")
        if len(_adj[id(_hub)]) >= 3:
            _seen = {id(_hub)}
            _comps = []
            for p in ordered:
                if id(p) in _seen:
                    continue
                _stk, _comp = [p], []
                _seen.add(id(p))
                while _stk:
                    _q = _stk.pop()
                    _comp.append(_q)
                    for _nb in sorted(_adj[id(_q)],
                                      key=lambda i: cidx.get(i, 0)):
                        if _nb not in _seen:
                            _seen.add(_nb)
                            _stk.append(_by_id[_nb])
                _comps.append(_comp)
            _multi = [c for c in _comps if len(c) >= 2]
            if len(_multi) >= 2:
                def _hub_y(comp):
                    _ids = {id(p) for p in comp}
                    _ys = []
                    for _pn in getattr(_hub, "pins", []):
                        _nt = getattr(_pn, "net", None)
                        if (_nt is None
                                or _net_power_kind(_nt) in ("rail", "ground")):
                            continue
                        if any(id(getattr(_op, "part", None)) in _ids
                               for _op in getattr(_nt, "pins", [])):
                            _ys.append((_pn.pt * _hub.tx).y)
                    return sum(_ys) / len(_ys) if _ys else 0.0

                _comps.sort(key=lambda c: (_hub_y(c), cidx.get(id(c[0]), 0)))

                def _chain_order(comp):
                    _ids = {id(p): p for p in comp}
                    _start = next(
                        (p for p in comp if id(_hub) in _adj[id(p)]), comp[0])
                    _o, _sn, _qu = [], {id(_start)}, [_start]
                    while _qu:
                        _c = _qu.pop(0)
                        _o.append(_c)
                        for _nb in sorted(_adj[id(_c)],
                                          key=lambda i: cidx.get(i, 0)):
                            if _nb in _ids and _nb not in _sn:
                                _sn.add(_nb)
                                _qu.append(_ids[_nb])
                    return _o

                _rows = [_chain_order(c) for c in _comps]
                _hub_w = max(_pin_span_w(_hub), MIN_SLOT)

                def _row_w(_r):
                    _w = sum(max(_pin_span_w(p), MIN_SLOT) for p in _r)
                    for _j in range(len(_r) - 1):
                        _w += _pair_gap(_r[_j], _r[_j + 1])
                    return _w

                _max_row_w = max(_row_w(_r) for _r in _rows)
                _pitch_y = max(max(_world_bbox(p).h for p in _r)
                               for _r in _rows) + GAP
                _hub_gap = max(_pair_gap(_hub, _r[0]) for _r in _rows)
                _total_w = _hub_w + _hub_gap + _max_row_w
                _x_hub = cen_x - _total_w / 2.0 + _hub_w / 2.0
                _x0 = _x_hub + _hub_w / 2.0 + _hub_gap
                moved = 0
                _hc = _world_bbox(_hub).ctr
                _hub.tx = _hub.tx * Tx().move(
                    Point(_x_hub - _pin_cx(_hub), base_y - _hc.y))
                snap_to_grid(_hub)
                moved += 1
                _k = len(_rows)
                for _i, _r in enumerate(_rows):
                    _ry = base_y + (_i - (_k - 1) / 2.0) * _pitch_y
                    _x = _x0
                    for _j, p in enumerate(_r):
                        _ew = max(_pin_span_w(p), MIN_SLOT)
                        _pc = _world_bbox(p).ctr
                        p.tx = p.tx * Tx().move(
                            Point(_x + _ew / 2.0 - _pin_cx(p), _ry - _pc.y))
                        snap_to_grid(p)
                        _x += _ew + (_pair_gap(_r[_j], _r[_j + 1])
                                     if _j < len(_r) - 1 else 0.0)
                        moved += 1
                print(f">>> chain-stack: block '{getattr(node, 'name', '?')}' "
                      f"fanned {_k} chain row(s) off hub "
                      f"{getattr(_hub, 'ref', '?')}")
                return moved

    widths = [max(_pin_span_w(p), MIN_SLOT) for p in ordered]
    gaps = [_pair_gap(ordered[i], ordered[i + 1])
            for i in range(len(ordered) - 1)]
    total_w = sum(widths) + sum(gaps)
    x = cen_x - total_w / 2.0
    moved = 0
    for i, (p, ew) in enumerate(zip(ordered, widths)):
        col_cx = x + ew / 2.0
        pc = _world_bbox(p).ctr
        p.tx = p.tx * Tx().move(Point(col_cx - _pin_cx(p), base_y - pc.y))
        snap_to_grid(p)
        p._flow_rowed = True   # rail-draw key: this block is ladder-ready
        x += ew + (gaps[i] if i < len(gaps) else 0.0)
        moved += 1
    return moved


def orient_to_neighbors(node, **options):
    """Rotate a multi-pin part (3..16 pins) so its pins FACE the parts they
    connect to -- the way a human orients a relay (contacts toward the
    output terminal, coil toward the driver). The placer keeps library
    orientation, so a part whose bundle edge points AWAY from its partner
    leaves the router no reachable faces: measured wlc PUMP_RELAY, where
    K1's COM/NO/NC sit on the TOP edge while J3 is to the RIGHT and the
    coil pins sit on the BOTTOM while the driver is to the LEFT --
    unroutable at every seed/gap. Scoring: total manhattan distance from
    each signal pin to the centroid of its net's other in-node pins, over
    the 4 rotations; apply the best when it clearly wins (>8%% better).
    Pure geometry+netlist; caller should re-run the row/stack layout after
    a rotation (pin spans change). Kill switch SKIDL_ORIENT_MULTI=0.
    Returns the number of parts rotated."""
    import os as _os
    if _os.environ.get("SKIDL_ORIENT_MULTI") == "0":
        return 0
    import builtins
    from skidl.geometry import Tx, Point, tx_rot_90, tx_rot_180, tx_rot_270
    from skidl.schematics.place import is_net_terminal, snap_to_grid

    real = [p for p in getattr(node, "parts", []) if not is_net_terminal(p)]
    if len(real) < 2:
        return 0
    _rid = {id(p) for p in real}

    def _sig_pins(p):
        return [pn for pn in getattr(p, "pins", [])
                if getattr(pn, "net", None) is not None
                and _net_power_kind(pn.net) not in ("rail", "ground")]

    def _score(p):
        s = 0.0
        n_ref = 0
        for pn in _sig_pins(p):
            others = [(op.pt * op.part.tx)
                      for op in getattr(pn.net, "pins", [])
                      if getattr(op, "part", None) is not None
                      and op.part is not p and id(op.part) in _rid]
            if not others:
                continue
            cx = sum(o.x for o in others) / len(others)
            cy = sum(o.y for o in others) / len(others)
            w = pn.pt * p.tx
            s += abs(w.x - cx) + abs(w.y - cy)
            n_ref += 1
        return s if n_ref else None

    def _all_stubbed(p):
        """True when every signal pin of the part is label-stubbed -- a
        label-mode (all-label / partial-label) block. Rotating or mirroring
        such a part moves its pins under the ALREADY-DECIDED label geometry
        and the emitter drops dangling labels -> missing net groups
        (measured pump: all-label tier verified BEFORE these passes, then
        MISMATCH 3 missing after a mid-tier mirror). Leave label-mode
        parts exactly where the placer put them."""
        _sig = [pn for pn in getattr(p, "pins", [])
                if getattr(pn, "net", None) is not None
                and _net_power_kind(pn.net) not in ("rail", "ground")]
        if not _sig:
            return True
        return all(pn.__dict__.get("stub", False)
                   or pn.__dict__.get("_stub_val", False)
                   or pn.net.__dict__.get("stub", False)
                   or pn.net.__dict__.get("_stub", False)
                   for pn in _sig)

    rotated = 0
    for p in real:
        if not (3 <= _pin_count(p) <= 16):
            continue
        if _all_stubbed(p):
            continue
        base = _score(p)
        if base is None:
            continue
        best_tx, best_s = None, base
        old_tx = p.tx
        old_ctr = _world_bbox(p).ctr
        for rot in (tx_rot_90, tx_rot_180, tx_rot_270):
            p.tx = rot * old_tx
            new_ctr = _world_bbox(p).ctr
            p.tx = p.tx * Tx().move(old_ctr - new_ctr)  # rotate about center
            s = _score(p)
            if s is not None and s < best_s:
                best_s, best_tx = s, p.tx
            p.tx = old_tx
        if best_tx is not None and best_s < 0.92 * base:
            p.tx = best_tx
            snap_to_grid(p)
            rotated += 1
            print(f">>> orient: rotated {getattr(p, 'ref', '?')} to face its "
                  f"neighbors (wire est. {base:.0f} -> {best_s:.0f})")
    return rotated


def uncross_parallel_bundles(node, **options):
    """Vertical-mirror a part whose >=2-net parallel BUNDLE to a neighbor is
    CROSSED -- e.g. a relay's COM/NO/NC feeding a 1x3 terminal whose pin
    order runs the other way. Crossed bundle wires overlap at points, the
    router's cross-net short guard raises RoutingFailure at EVERY seed, and
    the whole block falls back to labels (measured wlc PUMP_RELAY: the
    PUMP_COM x PUMP_NC touch repeated on every attempt). Mirroring the
    LIGHTER part (fewer outside commitments) reverses its pin order so the
    bundle routes as parallel straight wires -- the classic schematic
    component-flip uncrossing. Pure geometry+netlist, any circuit. Runs
    pre-route; the text-orientation post-pass re-normalizes labels. Kill
    switch SKIDL_UNCROSS=0. Returns the number of parts mirrored."""
    import os as _os
    if _os.environ.get("SKIDL_UNCROSS") == "0":
        return 0
    import builtins
    from skidl.geometry import Tx, Point, tx_flip_y
    from skidl.schematics.place import is_net_terminal, snap_to_grid

    real = [p for p in getattr(node, "parts", []) if not is_net_terminal(p)]
    if len(real) < 2:
        return 0
    try:
        cidx = {id(p): i for i, p in enumerate(builtins.default_circuit.parts)}
    except Exception:
        cidx = {}
    _rid = {id(p): p for p in real}

    # bundles: (partA, partB) -> [(pinA, pinB)] -- nets with exactly ONE pin
    # on each of exactly TWO in-node parts (extra pins outside the node, e.g.
    # a NetTerminal, don't disqualify the pair).
    bundles = {}
    _seen_nets = set()
    for p in real:
        for _pn in getattr(p, "pins", []):
            _nt = getattr(_pn, "net", None)
            if _nt is None or id(_nt) in _seen_nets:
                continue
            _seen_nets.add(id(_nt))
            if _net_power_kind(_nt) in ("rail", "ground"):
                continue
            _sides = {}
            for _op in getattr(_nt, "pins", []):
                _prt = getattr(_op, "part", None)
                if _prt is not None and id(_prt) in _rid:
                    _sides.setdefault(id(_prt), []).append(_op)
            if len(_sides) != 2 or any(len(v) != 1 for v in _sides.values()):
                continue
            (_ia, _pa), (_ib, _pb) = sorted(
                _sides.items(), key=lambda kv: cidx.get(kv[0], 0))
            bundles.setdefault((_ia, _ib), []).append((_pa[0], _pb[0]))

    flipped = 0
    _done = set()
    for (_ia, _ib), _prs in sorted(bundles.items(),
                                   key=lambda kv: (-len(kv[1]),
                                                   cidx.get(kv[0][0], 0))):
        if len(_prs) < 2 or _ia in _done or _ib in _done:
            continue
        # a STUBBED bundle renders as labels -- no wires, no crossing to
        # unwind; mirroring the part under already-decided label geometry
        # drops dangling labels in the label tiers (same failure class the
        # orient pass guards against).
        if any(_pa.net.__dict__.get("stub", False)
               or _pa.net.__dict__.get("_stub", False)
               or _pa.__dict__.get("stub", False)
               or _pa.__dict__.get("_stub_val", False)
               for _pa, _ in _prs):
            continue
        _a, _b = _rid[_ia], _rid[_ib]

        def _inversions():
            _ys = sorted(((_pa.pt * _a.tx).y, (_pb.pt * _b.tx).y)
                         for _pa, _pb in _prs)
            _seq = [y2 for _, y2 in _ys]
            return sum(1 for i in range(len(_seq))
                       for j in range(i + 1, len(_seq)) if _seq[i] > _seq[j])

        _n = len(_prs)
        _tot = _n * (_n - 1) // 2
        _inv = _inversions()
        if _inv * 2 <= _tot:
            continue  # already (mostly) parallel -- nothing to unwind
        _bundle_nets = {id(_pa.net) for _pa, _ in _prs}

        def _outside(p):
            _k = 0
            for _pn in getattr(p, "pins", []):
                _nt = getattr(_pn, "net", None)
                if (_nt is not None and id(_nt) not in _bundle_nets
                        and _net_power_kind(_nt) not in ("rail", "ground")):
                    _k += 1
            return _k

        _tgt = min((_b, _a), key=lambda p: (_outside(p), _pin_count(p),
                                            cidx.get(id(p), 0)))
        if _pin_count(_tgt) < 3:
            continue  # 2-pin passives belong to _orient_passives_rail_up
        _old_tx = _tgt.tx
        _cy = _world_bbox(_tgt).ctr.y
        _tgt.tx = _tgt.tx * tx_flip_y * Tx().move(Point(0.0, 2.0 * _cy))
        snap_to_grid(_tgt)
        if _inversions() >= _inv:
            _tgt.tx = _old_tx  # no improvement -> revert
            continue
        _done.add(id(_tgt))
        flipped += 1
        _oth = _a if _tgt is _b else _b
        print(f">>> uncross: mirrored {getattr(_tgt, 'ref', '?')} to unwind "
              f"a {_n}-net bundle with {getattr(_oth, 'ref', '?')}")
    return flipped


def hug_power_satellites(node, **options):
    """M12: pull each power-only decoupling cap up beside the IC it decouples.

    A 2-pin cap whose BOTH pins sit on power/ground rails has no signal edge to
    its IC (the rails are power symbols, not routed nets), so the placer leaves
    it stranded in a row far from the part it belongs to. This post-placement
    pass finds, for each such cap, an anchor IC that shares one of its rail
    nets and re-seats the cap just outside that IC's bbox -- caps on the same
    IC stack in a tidy column -- so the sheet reads as a tight decoupling
    cluster (the hand-layout / TRACKER look). Rails still render as symbols;
    only the cap POSITIONS change. Runs AFTER place(), BEFORE wire/label
    classification, so distance-based decisions see the tightened geometry.
    Kill switch: SKIDL_HUG_DECAPS=0. Returns the number of caps moved."""
    import os as _os
    if _os.environ.get("SKIDL_HUG_DECAPS") == "0":
        return 0
    from skidl.geometry import Tx, Point
    from skidl.schematics.place import is_net_terminal, snap_to_grid

    real = [p for p in getattr(node, "parts", []) if not is_net_terminal(p)]
    anchors = [
        p for p in real
        if (getattr(p, "ref_prefix", "") or "").upper() in _SEED_REF_PREFIXES
        and _pin_count(p) >= 3
    ]
    # ONLY single-anchor designs. With >=2 ICs the anchor packer (or the
    # per-block structure) already clusters each IC's decaps; hugging then
    # fights that placement and strands OTHER nets into labels (measured
    # stm32: 72 wired -> 49 with core nets labeled). A lone regulator/MCU is
    # exactly where decaps get stranded, so that is where hugging helps.
    if len(anchors) != 1:
        return 0

    def _rails(p, kinds=("rail",)):
        return {
            getattr(pp.net, "name", "") for pp in getattr(p, "pins", [])
            if getattr(pp, "net", None) is not None
            and _net_power_kind(pp.net) in kinds
        }

    def _is_decap(p):
        if (getattr(p, "ref_prefix", "") or "").upper() != "C":
            return False
        pins = [pp for pp in getattr(p, "pins", [])
                if getattr(pp, "net", None) is not None]
        return len(pins) == 2 and all(
            _net_power_kind(pp.net) in ("rail", "ground") for pp in pins)

    # SIGNAL SATELLITES (crystal on OSC, reset R-C on NRST, boot Rs, indicator
    # R on a GPIO) hug their anchor PIN too -- but ONLY in an UN-ROWED block
    # (a big-IC node flow_place_block skipped). On a rowed block this exact
    # move was measured to DESTROY the row (power_board 0->5, arduino 0->24
    # labels) -- the row already seats everything; satellites-hug is for the
    # dense-MCU case where the placer strands them and they LABEL (stm32 33).
    _rowed = any(getattr(p, "_flow_rowed", False) for p in real)

    def _is_sig_satellite(p):
        if p in anchors or _pin_count(p) > 2:
            return False
        pins = [pp for pp in getattr(p, "pins", [])
                if getattr(pp, "net", None) is not None]
        return any(_net_power_kind(pp.net) not in ("rail", "ground")
                   for pp in pins)

    decaps = [p for p in real if _is_decap(p)]
    if not _rowed:
        anet = {getattr(pp.net, "name", "") for a in anchors
                for pp in getattr(a, "pins", [])
                if getattr(pp, "net", None) is not None}
        decaps += sorted(
            (p for p in real if _is_sig_satellite(p)
             and not getattr(p, "_hugged", False)
             and any(getattr(pp.net, "name", "") in anet
                     for pp in getattr(p, "pins", [])
                     if getattr(pp, "net", None) is not None)),
            key=_refkey)
    if not decaps:
        return 0

    def _ppins(anchor):
        """(net_name, world_pt, kind) for EVERY connected anchor pin
        (kind: rail/ground/signal) -- decaps seat on power pins, signal
        satellites on the signal pin they serve."""
        out = []
        for pp in getattr(anchor, "pins", []):
            net = getattr(pp, "net", None)
            if net is None:
                continue
            k = _net_power_kind(net) or "signal"
            try:
                out.append((getattr(net, "name", ""), pp.pt * anchor.tx, k))
            except Exception:
                pass
        return out

    def _dnets(p, kinds=None):
        s = set()
        for pp in getattr(p, "pins", []):
            net = getattr(pp, "net", None)
            if net is None:
                continue
            k = _net_power_kind(net) or "signal"
            if kinds is None or k in kinds:
                s.add(getattr(net, "name", ""))
        return s

    moved = 0
    for anchor in sorted(anchors, key=_refkey):
        a_rails = _rails(anchor)
        if not a_rails:
            continue
        abb = _world_bbox(anchor)
        ac = abb.ctr
        ppins = _ppins(anchor)
        a_all = {nm for nm, _pt, _k in ppins}
        mine = [d for d in decaps
                if (_dnets(d) & a_all) and getattr(d, "_hugged", False) is False]
        if not mine or not ppins:
            continue
        mine.sort(key=_refkey)
        gap = 300.0
        stacked = {}
        for d in mine:
            d_all = _dnets(d)
            d_sig = _dnets(d, kinds=("signal",))
            cands = [(nm, pt, k) for nm, pt, k in ppins if nm in d_all]
            if not cands:
                continue

            def _key(c, _stacked=stacked, _sig=d_sig):
                pk = round(c[1].x) * 100003 + round(c[1].y)
                # a signal satellite seats on ITS signal pin; a decap on a
                # rail pin; ground last.
                rank = (0 if (c[0] in _sig and c[2] == "signal")
                        else (1 if c[2] == "rail" else 2))
                return (rank, _stacked.get(pk, 0))

            nm, pt, k = min(cands, key=_key)
            pk = round(pt.x) * 100003 + round(pt.y)
            j = stacked.get(pk, 0)
            stacked[pk] = j + 1
            dvec = Point(pt.x - ac.x, pt.y - ac.y)
            L = max((dvec.x ** 2 + dvec.y ** 2) ** 0.5, 1.0)
            ux, uy = dvec.x / L, dvec.y / L
            pitch = max(_world_bbox(d).h * 1.2, 300.0)
            reach = gap + j * pitch
            target = Point(pt.x + ux * reach, pt.y + uy * reach)
            dc = _world_bbox(d).ctr
            d.tx = d.tx * Tx().move(Point(target.x - dc.x, target.y - dc.y))
            snap_to_grid(d)
            d._hugged = True
            moved += 1
    return moved


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


def _place_cluster_flow_row(packed, order_ix, gap=1200.0):
    """Lay packed clusters in AUTHOR creation order (scripts define blocks
    input -> processing -> output, so creation order IS the signal-flow order),
    wrapped into a PAGE-SHAPED grid: left-to-right on a common baseline, then a
    new row when the current row would overflow the page width, and a bigger
    page only when the content genuinely overflows -- the human decision (fill a
    real A4/A3, wrap when full) instead of one unbounded row that the collision
    resolver then scatters. Used for authored-block (M11) designs; the spiral
    (_place_cluster_units) stays for signal-derived clusters with no author
    order."""
    ordered = sorted(
        packed, key=lambda pc: min(order_ix.get(id(p), 0) for p in pc[0])
    )
    x = 0.0
    for cluster, _bb in ordered:
        bb = _cluster_bbox(cluster)
        _move_cluster(cluster, x - bb.min.x, -bb.ctr.y)
        x += bb.w + gap


def _grid_assign(n):
    """Pure grid slot assignment (gap H1). Returns (rows, cols, slots) where
    slots[i] = (row, col) for the i-th block IN PLACEMENT ORDER. Slot 0 is the
    CENTRE cell (the anchor/MCU owns it); the rest spiral out from the centre by
    Manhattan ring distance (deterministic tie-break) so role-ordered blocks land
    closest-first around the anchor. A near-square grid keeps the sheet compact."""
    import math as _math
    n = max(int(n), 1)
    cols = _math.ceil(_math.sqrt(n))
    rows = _math.ceil(n / cols)
    cr, cc = rows // 2, cols // 2
    ring = sorted(
        [(r, c) for r in range(rows) for c in range(cols) if (r, c) != (cr, cc)],
        key=lambda rc: (abs(rc[0] - cr) + abs(rc[1] - cc), rc[0], rc[1]),
    )
    return rows, cols, [(cr, cc)] + ring


def _grid_coords(rows, cols, slots, dims, gap, align):
    """Pure cell geometry (gap H1). COLUMN WIDTH = max block width in that column,
    ROW HEIGHT = max block height in that row -> a true aligned grid with uniform
    gaps. Returns [(x_left, y_top), ...] per block (block tops align within a row,
    block lefts within a column), snapped to the ALIGN grid. Y grows downward."""
    colW = [0.0] * cols
    rowH = [0.0] * rows
    for (r, c), (w, h) in zip(slots, dims):
        colW[c] = max(colW[c], w)
        rowH[r] = max(rowH[r], h)
    colX = []
    x = 0.0
    for c in range(cols):
        colX.append(x)
        x += colW[c] + gap
    rowYtop = []
    y = 0.0
    for r in range(rows):
        rowYtop.append(y)
        y -= rowH[r] + gap          # next row sits BELOW (schematic Y grows up)
    out = []
    for (r, c), _wh in zip(slots, dims):
        X = round(colX[c] / align) * align
        Ytop = round(rowYtop[r] / align) * align
        out.append((X, Ytop))
    return out, colW, rowH


def place_block_grid(packed, g_anchor, gap=1000.0, align=50.0):
    """2D BLOCK-GRID placement (gap H1; SCHEMATIC_LAYOUT_LOGIC.md spec).

    Lay already-internally-packed blocks as a compact 2D GRID of boxed cells --
    MCU/anchor CENTRED, small function blocks around it, uniform 500-1000 mil
    gaps, column/row edges aligned -- the ATmega/STM dev-board composition. This
    replaces the spiral packer (which scatters) and the flow_x 1-D row (correct
    but over-wide) for multi-block boards. Returns True on success, False to let
    the caller fall back to the flow row. OPT-IN (SKIDL_BLOCK_GRID=1) and guarded
    downstream by the build-level connectivity verify, so it can never ship a
    worse sheet than today's default.
    """
    blocks = [cl for cl, _bb in packed]
    if len(blocks) < 2:
        return False
    anchor_block = next((cl for cl in blocks if g_anchor in cl), None)
    if anchor_block is None:
        return False
    try:
        from skidl.schematics.cluster import (
            classify_block_role, order_blocks_by_role,
        )
    except Exception:
        return False
    others = [cl for cl in blocks if cl is not anchor_block]
    try:
        ordered_others = order_blocks_by_role(
            [(classify_block_role(cl), cl) for cl in others]
        )
    except Exception:
        ordered_others = others
    placement = [anchor_block] + list(ordered_others)
    dims = []
    for cl in placement:
        bb = _cluster_bbox(cl)
        dims.append((bb.w, bb.h))
    rows, cols, slots = _grid_assign(len(placement))
    coords, _cw, _rh = _grid_coords(rows, cols, slots, dims, gap, align)
    for cl, (X, Ytop) in zip(placement, coords):
        bb = _cluster_bbox(cl)
        # align this block's TOP-LEFT to its cell origin
        _move_cluster(cl, X - bb.min.x, Ytop - bb.max.y)
    return True


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
