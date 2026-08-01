"""Wire Beautification Pass -- Phase G / SCHEMATIC_ENGINE_RULES S5 (safe subset).

A CONNECTIVITY-PRESERVING post-route cleanup on a .kicad_sch:
  * diagonal wire segment  -> Manhattan L (two H/V segments via a corner)
  * merge COLLINEAR segments that meet at a plain 2-way breakpoint
    (never at a junction dot -> real taps are preserved)
  * drop zero-length segments

Only redundant interior breakpoints are removed and diagonals are squared; no
wire ENDPOINT that could sit on a pin/junction is deleted -- a straight line
that passes through a point still connects a pin there -- so the drawn
connectivity is identical (the caller re-runs the connectivity check to prove
it). DYNAMIC: pure geometry, no circuit-specific assumptions -> works on any
.kicad_sch. Fail-safe: on any error it leaves the file untouched, returns 0.

NOT handled here (needs router-level re-routing, not a post-pass): equal
pin-exit length, parallel-wire spacing, wire-to-body clearance. Those move
endpoints and must be done during routing with pin positions known.
"""
import re


def _r(v):
    return round(float(v), 2)


def _fmt(v):
    # KiCad writes plain decimals; keep it tidy (2dp, no trailing .0 noise kept as-is)
    return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def beautify(sch_path):
    """Return the number of wire segments removed (>=0), 0 if unchanged/failed."""
    try:
        return _beautify(sch_path)
    except Exception:
        return 0  # never break a build over cosmetics


def _wire_spans(text):
    """Yield (start, end, (x1,y1,x2,y2)) for every top-level (wire ...) block."""
    for m in re.finditer(r"\(wire\b", text):
        s = m.start()
        depth = 0
        e = None
        for i in range(s, len(text)):
            c = text[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    e = i + 1
                    break
        if e is None:
            continue
        blk = text[s:e]
        pm = re.search(
            r"\(pts\s*\(xy ([\d.\-]+) ([\d.\-]+)\)\s*\(xy ([\d.\-]+) ([\d.\-]+)\)", blk
        )
        if pm:
            yield s, e, tuple(_r(x) for x in pm.groups())


def _gen_uuid(seg):
    import hashlib

    h = hashlib.md5(("beauty:%s" % (seg,)).encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _wire_sexp(seg):
    x1, y1, x2, y2 = seg
    return (
        "\t(wire\n"
        "\t\t(pts\n"
        f"\t\t\t(xy {_fmt(x1)} {_fmt(y1)})\n"
        f"\t\t\t(xy {_fmt(x2)} {_fmt(y2)})\n"
        "\t\t)\n"
        "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
        f'\t\t(uuid "{_gen_uuid(seg)}")\n'
        "\t)\n"
    )


def _axis_snap(dx, dy):
    """Nearest pure-axis unit direction of (dx,dy); (0,0) if degenerate."""
    if dx == 0 and dy == 0:
        return (0, 0)
    if abs(dx) >= abs(dy):
        return (1 if dx > 0 else -1, 0)
    return (0, 1 if dy > 0 else -1)


def _flip_l_exits(segs, junctions, centers):
    """Re-shape 2-segment L-wires so the segment leaving a PIN goes AWAY from the
    symbol body (its exit direction) with the long arm at the pin, instead of a
    short stub that immediately bends. Pin end + far end stay fixed (connectivity
    preserved); only the corner moves. Pin exit direction is inferred robustly as
    "away from the nearest symbol center" -- no lib-pin-geometry parsing needed.
    (S5 B-1/B-2 pin-exit, safe post-process subset.)
    """
    if not centers:
        return segs

    def nearest_center(p):
        return min(centers, key=lambda c: abs(c[0] - p[0]) + abs(c[1] - p[1]))

    changed = True
    guard = 0
    while changed and guard < 500:
        changed = False
        guard += 1
        deg = {}
        idx = {}
        for k, (x1, y1, x2, y2) in enumerate(segs):
            for pt in ((x1, y1), (x2, y2)):
                deg[pt] = deg.get(pt, 0) + 1
                idx.setdefault(pt, []).append(k)
        for Q, ks in idx.items():
            if Q in junctions or len(ks) != 2 or ks[0] == ks[1]:
                continue
            i, j = ks
            a, b = segs[i], segs[j]
            a_h, b_h = a[1] == a[3], b[1] == b[3]
            if a_h == b_h:  # need one horizontal + one vertical (a real L)
                continue
            P = (a[2], a[3]) if (a[0], a[1]) == Q else (a[0], a[1])
            R = (b[2], b[3]) if (b[0], b[1]) == Q else (b[0], b[1])
            done = False
            for pin_end, other in ((P, R), (R, P)):
                if deg.get(pin_end, 0) != 1:
                    continue  # only re-shape at a true wire END (pin/label)
                C = nearest_center(pin_end)
                ex, ey = _axis_snap(pin_end[0] - C[0], pin_end[1] - C[1])
                if ex == 0 and ey == 0:
                    continue
                # corner that makes pin_end exit along its exit axis:
                new_corner = (other[0], pin_end[1]) if ex != 0 else (pin_end[0], other[1])
                if new_corner == Q:
                    continue  # already exits the right way
                sx, sy = new_corner[0] - pin_end[0], new_corner[1] - pin_end[1]
                # the exit arm must actually go in +exit direction (not backward/zero)
                if ex != 0:
                    if sx == 0 or (sx > 0) != (ex > 0):
                        continue
                else:
                    if sy == 0 or (sy > 0) != (ey > 0):
                        continue
                new_i = (pin_end[0], pin_end[1], new_corner[0], new_corner[1])
                new_j = (new_corner[0], new_corner[1], other[0], other[1])
                if new_i[:2] == new_i[2:] or new_j[:2] == new_j[2:]:
                    continue  # would create a zero-length segment
                segs = [s for m, s in enumerate(segs) if m not in (i, j)]
                segs.extend([new_i, new_j])
                changed = done = True
                break
            if done:
                break
    return segs


def _beautify(sch_path):
    text = open(sch_path, encoding="utf-8").read()

    junctions = {
        (_r(m.group(1)), _r(m.group(2)))
        for m in re.finditer(r"\(junction\s*\(at ([\d.\-]+) ([\d.\-]+)\)", text)
    }
    centers = [
        (_r(m.group(1)), _r(m.group(2)))
        for m in re.finditer(
            r'\(symbol\s*\(lib_id "[^"]+"\)\s*\(at ([\d.\-]+) ([\d.\-]+)', text
        )
    ]

    blocks = list(_wire_spans(text))
    if not blocks:
        return 0
    orig = [c for _, _, c in blocks]

    # 1) split diagonals into L, drop zero-length
    segs = []
    for x1, y1, x2, y2 in orig:
        if x1 == x2 and y1 == y2:
            continue
        if abs(x1 - x2) > 0.01 and abs(y1 - y2) > 0.01:
            cx, cy = x2, y1  # corner: horizontal first, then vertical
            if (cx, cy) != (x1, y1):
                segs.append((x1, y1, cx, cy))
            if (cx, cy) != (x2, y2):
                segs.append((cx, cy, x2, y2))
        else:
            segs.append((x1, y1, x2, y2))

    # 2) iterative collinear merge at plain 2-way, non-junction breakpoints
    def endpoints(sl):
        pts = {}
        for k, (x1, y1, x2, y2) in enumerate(sl):
            pts.setdefault((x1, y1), []).append(k)
            pts.setdefault((x2, y2), []).append(k)
        return pts

    changed = True
    while changed:
        changed = False
        for p, idxs in endpoints(segs).items():
            if p in junctions or len(idxs) != 2 or idxs[0] == idxs[1]:
                continue
            i, j = idxs
            a, b = segs[i], segs[j]
            a_h, a_v = a[1] == a[3], a[0] == a[2]
            b_h, b_v = b[1] == b[3], b[0] == b[2]
            collinear = (a_h and b_h and a[1] == b[1]) or (a_v and b_v and a[0] == b[0])
            if not collinear:
                continue
            fa = (a[2], a[3]) if (a[0], a[1]) == p else (a[0], a[1])
            fb = (b[2], b[3]) if (b[0], b[1]) == p else (b[0], b[1])
            merged = (fa[0], fa[1], fb[0], fb[1])
            segs = [s for k, s in enumerate(segs) if k not in (i, j)]
            if merged[0] != merged[2] or merged[1] != merged[3]:
                segs.append(merged)
            changed = True
            break

    # 2b) re-shape L-wires so pins exit AWAY from their body (long arm at pin)
    segs = _flip_l_exits(segs, junctions, centers)

    # normalize + compare; bail if nothing improved
    def norm(s):
        return tuple(sorted([(s[0], s[1]), (s[2], s[3])]))

    new_set = {norm(s) for s in segs}
    old_set = {norm(c) for c in orig if not (c[0] == c[2] and c[1] == c[3])}
    if new_set == old_set:
        return 0

    # 3) rewrite: strip all old wire blocks, reinsert the new segment set once
    first = blocks[0][0]
    for s, e, _ in sorted(blocks, key=lambda w: -w[0]):
        text = text[:s] + text[e:]
    ins = "".join(_wire_sexp(s) for s in segs)
    text = text[:first] + ins + text[first:]
    open(sch_path, "w", encoding="utf-8").write(text)
    return len(old_set) - len(new_set)


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "circuit.kicad_sch"
    if not name.endswith(".kicad_sch"):
        name += ".kicad_sch"
    print("wire segments removed:", beautify(name))
