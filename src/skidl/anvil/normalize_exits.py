"""Pin-exit normalization -- RULE_ENGINE Phase 5 (wire geometry, safe subset).

A CONNECTIVITY-PRESERVING post-route pass on a .kicad_sch: every wire that
leaves a component PIN should run a minimum straight stub before its first bend
(the "no immediate bend at a pin" rule). Where a pin's exit stub is too short,
we lengthen it by rigidly SLIDING the rest of that wire's chain -- and the
movable label / power symbol at its far end -- outward along the exit axis by the
same delta. Because the whole downstream chain translates by one vector along the
exit axis, every segment keeps its shape and axis-alignment (still Manhattan) and
every connection is preserved (the chain stays intact, the label moves with the
wire end it sits on). The pin end never moves, so the electrical connection point
is unchanged.

ONLY the safe case is touched: a NON-branching chain from a pin to a MOVABLE free
end (a label or a power symbol). A pin-to-pin wire (both ends fixed) is left alone
-- lengthening it would need a jog the router must add, not a post-pass. The
caller runs this inside the per-step connectivity revert-guard, so even the rare
bad case (e.g. a slid label overlapping something) can only be reverted, never
shipped as a short. DYNAMIC: pure geometry over the flat sheet, any circuit.
"""
import re

GRID_MM = 1.27
_NUM = r"[-+]?\d+(?:\.\d+)?"


def _fmt(v):
    return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _r(v):
    return round(float(v), 2)


def normalize(sch_path, min_exit_mm=2.54):
    """Lengthen too-short pin exits. Returns the number of exits lengthened."""
    try:
        return _normalize(sch_path, min_exit_mm)
    except Exception:
        return 0  # never break a build over geometry cosmetics


def _spans(text, tag):
    for m in re.finditer(r"\(" + tag + r"\b", text):
        i = m.start()
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    yield i, j + 1
                    break


def _wire_pts(text):
    """[(s, e, x1, y1, x2, y2)] for each top-level wire (skips lib_symbols)."""
    lib = list(_spans(text, "lib_symbols"))

    def in_lib(p):
        return any(a <= p < b for a, b in lib)

    out = []
    for s, e in _spans(text, "wire"):
        if in_lib(s):
            continue
        m = re.search(
            r"\(pts\s*\(xy (%s) (%s)\)\s*\(xy (%s) (%s)\)" % (_NUM, _NUM, _NUM, _NUM),
            text[s:e],
        )
        if m:
            out.append((s, e, *[_r(x) for x in m.groups()]))
    return out


def _movable_anchors(text):
    """{(x,y): (start,end)} for each label / power-symbol whose (at) can be
    slid to follow a wire end. Labels: global/hierarchical/local. Power symbol:
    its instance (at) is the pin/connection point."""
    anchors = {}
    for tag in ("global_label", "hierarchical_label", "label"):
        for s, e in _spans(text, tag):
            m = re.search(r"\(at (%s) (%s)" % (_NUM, _NUM), text[s:e])
            if m:
                anchors[(_r(m.group(1)), _r(m.group(2)))] = (s, e)
    # power symbols: (symbol (lib_id "power:..") (at x y a) ...)
    for s, e in _spans(text, "symbol"):
        blk = text[s:e]
        if '(lib_id "power:' not in blk:
            continue
        m = re.search(r'\(lib_id "power:[^"]+"\)\s*\(at (%s) (%s)' % (_NUM, _NUM), blk)
        if m:
            anchors[(_r(m.group(1)), _r(m.group(2)))] = (s, e)
    return anchors


def _normalize(sch_path, min_exit_mm):
    text = open(sch_path, encoding="utf-8").read()
    wires = _wire_pts(text)
    if not wires:
        return 0
    anchors = _movable_anchors(text)
    junctions = {
        (_r(m.group(1)), _r(m.group(2)))
        for m in re.finditer(r"\(junction\s*\(at (%s) (%s)\)" % (_NUM, _NUM), text)
    }

    # segment list as mutable coords + endpoint adjacency
    segs = [[x1, y1, x2, y2] for _, _, x1, y1, x2, y2 in wires]

    def endpoints():
        d = {}
        for k, (x1, y1, x2, y2) in enumerate(segs):
            d.setdefault((x1, y1), []).append(k)
            d.setdefault((x2, y2), []).append(k)
        return d

    moved = 0
    label_moves = {}  # (old_x,old_y) -> (new_x,new_y)
    changed = True
    guard = 0
    while changed and guard < 200:
        changed = False
        guard += 1
        deg = endpoints()
        for P, ks in deg.items():
            # a PIN end = a degree-1 wire end that is NOT a movable anchor
            # (labels/power are the OTHER, movable end) and not a junction.
            if len(ks) != 1 or P in anchors or P in junctions:
                continue
            k = ks[0]
            seg = segs[k]
            A = (seg[2], seg[3]) if (seg[0], seg[1]) == P else (seg[0], seg[1])
            exit_len = abs(A[0] - P[0]) + abs(A[1] - P[1])
            if exit_len >= min_exit_mm - 1e-6 or exit_len == 0:
                continue
            # exit axis (unit) from P outward to A
            ax = (1 if A[0] > P[0] else -1) if abs(A[0] - P[0]) > 1e-6 else 0
            ay = (1 if A[1] > P[1] else -1) if abs(A[1] - P[1]) > 1e-6 else 0
            if ax == 0 and ay == 0:
                continue
            delta = min_exit_mm - exit_len

            # walk the non-branching chain from A to a movable free end
            chain_nodes = {A}
            chain_segs = {k}
            cur, prev = A, P
            ok = True
            while True:
                if cur in junctions or cur in anchors:
                    break  # reached a movable anchor (good) or a junction (stop)
                cks = deg.get(cur, [])
                nxt = [c for c in cks if c not in chain_segs]
                if len(cks) > 2:
                    ok = False
                    break  # branch -> unsafe
                if not nxt:
                    # dead end that isn't an anchor -> it's another pin, unsafe
                    ok = cur in anchors
                    break
                cseg = segs[nxt[0]]
                other = (cseg[2], cseg[3]) if (cseg[0], cseg[1]) == cur else (cseg[0], cseg[1])
                chain_segs.add(nxt[0])
                chain_nodes.add(other)
                prev, cur = cur, other
                if cur in anchors:
                    break
                if len(chain_segs) > 64:
                    ok = False
                    break
            end = cur
            if not ok or end not in anchors:
                continue  # only lengthen when the far end is movable

            # translate every chain node (not P) by delta along the exit axis
            dv = (delta * ax, delta * ay)

            def tr(pt):
                return (round(pt[0] + dv[0], 2), round(pt[1] + dv[1], 2))

            for si in chain_segs:
                s0 = (segs[si][0], segs[si][1])
                s1 = (segs[si][2], segs[si][3])
                if s0 != P:
                    segs[si][0], segs[si][1] = tr(s0)
                if s1 != P:
                    segs[si][2], segs[si][3] = tr(s1)
            label_moves[end] = tr(end)
            moved += 1
            changed = True
            break

    if not moved:
        return 0

    # rewrite: replace wire pts in place (reverse order to keep offsets valid)
    new_text = text
    for (s, e, *_), seg in sorted(zip(wires, segs), key=lambda z: -z[0][0]):
        block = new_text[s:e]
        block2 = re.sub(
            r"(\(pts\s*\(xy )%s %s(\)\s*\(xy )%s %s(\))" % (_NUM, _NUM, _NUM, _NUM),
            lambda m: "%s%s %s%s%s %s%s"
            % (m.group(1), _fmt(seg[0]), _fmt(seg[1]), m.group(2),
               _fmt(seg[2]), _fmt(seg[3]), m.group(3)),
            block,
            count=1,
        )
        new_text = new_text[:s] + block2 + new_text[e:]

    # move the labels/power symbols at chain far-ends
    for (ox, oy), (nx, ny) in label_moves.items():
        if (ox, oy) not in anchors:
            continue
        s, e = anchors[(ox, oy)]
        # offsets shifted by the wire rewrite above; re-find the (at) near it by value
        pat = re.compile(r"(\(at )%s %s" % (re.escape(_fmt(ox)), re.escape(_fmt(oy))))
        new_text, n = pat.subn(r"\g<1>%s %s" % (_fmt(nx), _fmt(ny)), new_text, count=1)

    # SAFETY: never write a structurally-broken sheet (a corrupt file makes the
    # kicad-cli connectivity check silently "skip", so the revert-guard wouldn't
    # catch it). Bail if the paren balance or the element counts changed.
    if new_text.count("(") != text.count("(") or new_text.count(")") != text.count(")"):
        return 0
    if new_text.count("(wire") != text.count("(wire") or new_text.count(
        "(global_label"
    ) != text.count("(global_label"):
        return 0

    open(sch_path, "w", encoding="utf-8").write(new_text)
    return moved


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "circuit.kicad_sch"
    if not name.endswith(".kicad_sch"):
        name += ".kicad_sch"
    print("exits lengthened:", normalize(name))
