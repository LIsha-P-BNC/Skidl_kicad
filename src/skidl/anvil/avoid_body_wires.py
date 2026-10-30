"""avoid_body_wires.py -- POST-route, CONNECTIVITY-PRESERVING wire cleanup.

Defect (bms_1s): a routed net wire was drawn straight ACROSS an IC symbol body
(U1 / DW01A / FS8205A) -- and one ground wire ran over a transistor's NC pin --
so it read as if it electrically touched the part. Ugly, not wrong, but it looks
broken.

Fix: for every axis-aligned wire SEGMENT that passes THROUGH a multi-pin symbol's
body rectangle (a part the segment does NOT connect to at either endpoint),
reshape the MIDDLE of that segment to detour AROUND the nearest body edge with two
extra Manhattan jogs. Both segment ENDPOINTS stay EXACTLY where they were -- a
reshape never moves a pin/label/connection point, so connectivity cannot change
(the caller's revert-guard re-proves it with the real connectivity check). If no
clean detour exists (it would clip another body, or the segment doesn't extend far
enough past the body to jog), the segment is LEFT AS-IS -- a wire over a body is
cosmetic; a moved endpoint would be a real bug.

Only symbols with >=3 pins and a genuine body rectangle are treated as obstacles
(ICs, big multi-pin parts). 2-pin passives, power symbols and connectors are never
obstacles. DYNAMIC: pure geometry from lib_symbols + instance transforms, no
circuit-specific assumptions. Fail-safe: any error -> file untouched, returns 0.

Kill switch: SKIDL_WIRE_AVOID_BODY=0.
"""
import math
import os
import re

MARGIN_MM = 2.54          # detour clearance past a body edge (2 * 1.27mm grid)
EP = 0.01                 # float tolerance (mm)


def _r(v):
    return round(float(v), 2)


def _fmt(v):
    return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _blocks(text, head_re):
    spans = []
    for mo in re.finditer(head_re, text):
        depth, i, n, instr = 0, mo.start(), len(text), False
        while i < n:
            c = text[i]
            if instr:
                if c == '"' and text[i - 1] != "\\":
                    instr = False
            elif c == '"':
                instr = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    spans.append((mo.start(), i + 1))
                    break
            i += 1
    return spans


def _lib_body_rects(text):
    """lib symbol name -> (body_rect (x0,y0,x1,y1) in lib coords, pin_count).

    body_rect = largest rectangle graphic; None if the symbol has no rectangle."""
    out = {}
    m = re.search(r"\(lib_symbols", text)
    if not m:
        return out
    s, e = _blocks(text, r"\(lib_symbols")[0]
    lib = text[s:e]
    for (bs, be) in _blocks(lib, r'\n    \(symbol "'):
        blk = lib[bs:be]
        nm = re.search(r'\(symbol "([^"]+)"', blk)
        if not nm:
            continue
        name = nm.group(1)
        best = None
        for rm in re.finditer(
                r"\(rectangle\s*\(start ([\d.\-]+) ([\d.\-]+)\)\s*"
                r"\(end ([\d.\-]+) ([\d.\-]+)\)", blk):
            x0, y0, x1, y1 = map(float, rm.groups())
            area = abs(x1 - x0) * abs(y1 - y0)
            if best is None or area > best[0]:
                best = (area, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        npins = len(re.findall(r"\(pin \w+ \w+\s*\(at ", blk))
        rect = None if (best is None or best[0] < 1.0) else best[1:]
        out[name] = (rect, npins)
    return out


def _world_bboxes(text):
    """List of body bounding boxes (x0,y0,x1,y1) in WORLD coords, one per placed
    multi-pin (>=3) symbol that has a real body rectangle."""
    bodies = _lib_body_rects(text)
    boxes = []
    for (s, e) in _blocks(text, r"\(symbol\s*\n\s*\(lib_id"):
        span = text[s:e]
        lib = re.search(r'\(lib_id "([^"]*)"', span)
        at = re.search(r"\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\)", span)
        if not (lib and at):
            continue
        lib_id = lib.group(1)
        if lib_id.startswith("power:") or lib_id not in bodies:
            continue
        rect, npins = bodies[lib_id]
        if rect is None or npins < 3:
            continue
        bx0, by0, bx1, by1 = rect
        x0, y0 = float(at.group(1)), float(at.group(2))
        ang = math.radians(float(at.group(3)) % 360)
        mx = re.search(r"\(mirror ([xy])\)", span)
        ca, sa = math.cos(ang), math.sin(ang)
        wx, wy = [], []
        for px, py in ((bx0, by0), (bx0, by1), (bx1, by0), (bx1, by1)):
            if mx:
                if mx.group(1) == "x":
                    py = -py
                else:
                    px = -px
            rx, ry = px * ca - py * sa, px * sa + py * ca
            wx.append(x0 + rx)
            wy.append(y0 - ry)          # sheet Y grows downward
        boxes.append((_r(min(wx)), _r(min(wy)), _r(max(wx)), _r(max(wy))))
    return boxes


def _wire_spans(text):
    for m in re.finditer(r"\(wire\b", text):
        s = m.start()
        depth, e = 0, None
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
        pm = re.search(
            r"\(pts\s*\(xy ([\d.\-]+) ([\d.\-]+)\)\s*\(xy ([\d.\-]+) ([\d.\-]+)\)",
            text[s:e])
        if pm:
            yield s, e, tuple(_r(x) for x in pm.groups())


def _gen_uuid(seg):
    import hashlib
    h = hashlib.md5(("avoidbody:%s" % (seg,)).encode()).hexdigest()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _wire_sexp(seg):
    x1, y1, x2, y2 = seg
    return (
        "\t(wire\n\t\t(pts\n"
        f"\t\t\t(xy {_fmt(x1)} {_fmt(y1)})\n"
        f"\t\t\t(xy {_fmt(x2)} {_fmt(y2)})\n\t\t)\n"
        "\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
        f'\t\t(uuid "{_gen_uuid(seg)}")\n\t)\n'
    )


def _pt_in_box(px, py, box, grow=0.0):
    x0, y0, x1, y1 = box
    return (x0 - grow - EP) <= px <= (x1 + grow + EP) and \
           (y0 - grow - EP) <= py <= (y1 + grow + EP)


def _seg_hits_body(seg, box):
    """True if axis-aligned seg passes through box INTERIOR (strict on the
    perpendicular axis; real overlap on the parallel axis)."""
    x1, y1, x2, y2 = seg
    bx0, by0, bx1, by1 = box
    if abs(y1 - y2) <= EP:                       # horizontal
        y = y1
        if not (by0 + EP < y < by1 - EP):
            return False
        lo, hi = min(x1, x2), max(x1, x2)
        return max(lo, bx0) + EP < min(hi, bx1) - EP
    if abs(x1 - x2) <= EP:                        # vertical
        x = x1
        if not (bx0 + EP < x < bx1 - EP):
            return False
        lo, hi = min(y1, y2), max(y1, y2)
        return max(lo, by0) + EP < min(hi, by1) - EP
    return False                                  # diagonal -> ignore


def _jog_lohi(lo, hi, b0, b1):
    """Pick the two jog coordinates for a segment spanning [lo,hi] that must clear
    a body spanning [b0,b1]. Jogs land on the body EDGES (b0, b1): the wire hugs
    each edge and goes up/over -- so any pin sitting on that edge (pins live on the
    body boundary, never its interior) stays on the wire at the jog CORNER, which
    is what keeps multi-pin nets connected. Returns (j0, j1) or None if an endpoint
    is inside the body interior (then the wire genuinely terminates in the part and
    must not be moved)."""
    if lo > b0 + EP:
        return None                    # low endpoint is inside the body interior
    if hi < b1 - EP:
        return None                    # high endpoint is inside the body interior
    if b1 - b0 <= EP:
        return None
    return b0, b1


def _detour(seg, box):
    """Return a list of new segments routing seg around box, or None if no clean
    geometry (both endpoints preserved EXACTLY)."""
    x1, y1, x2, y2 = seg
    bx0, by0, bx1, by1 = box
    m = MARGIN_MM
    if abs(y1 - y2) <= EP:                        # horizontal segment
        y = y1
        lo, hi = min(x1, x2), max(x1, x2)
        jl = _jog_lohi(lo, hi, bx0, bx1)
        if jl is None:
            return None
        xa, xb = jl
        clear = (by0 - m) if (y - by0) <= (by1 - y) else (by1 + m)
        if x1 < x2:
            pts = [(x1, y), (xa, y), (xa, clear), (xb, clear), (xb, y), (x2, y)]
        else:
            pts = [(x1, y), (xb, y), (xb, clear), (xa, clear), (xa, y), (x2, y)]
    elif abs(x1 - x2) <= EP:                      # vertical segment
        x = x1
        lo, hi = min(y1, y2), max(y1, y2)
        jl = _jog_lohi(lo, hi, by0, by1)
        if jl is None:
            return None
        ya, yb = jl
        clear = (bx0 - m) if (x - bx0) <= (bx1 - x) else (bx1 + m)
        if y1 < y2:
            pts = [(x, y1), (x, ya), (clear, ya), (clear, yb), (x, yb), (x, y2)]
        else:
            pts = [(x, y1), (x, yb), (clear, yb), (clear, ya), (x, ya), (x, y2)]
    else:
        return None
    out = []
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        if abs(a[0] - b[0]) <= EP and abs(a[1] - b[1]) <= EP:
            continue
        out.append((_r(a[0]), _r(a[1]), _r(b[0]), _r(b[1])))
    return out or None


def avoid(sch_path):
    if os.environ.get("SKIDL_WIRE_AVOID_BODY", "").lower() in ("0", "false", "no", "off"):
        return 0
    try:
        return _avoid(sch_path)
    except Exception as exc:
        try:
            from skidl.schematics.net_classify import log_swallowed
            log_swallowed("avoid_body_wires", exc)
        except Exception:
            pass
        return 0


def _avoid(sch_path):
    text = open(sch_path, encoding="utf-8").read()
    boxes = _world_bboxes(text)
    if not boxes:
        return 0
    junctions = {
        (_r(m.group(1)), _r(m.group(2)))
        for m in re.finditer(r"\(junction\s*\(at ([\d.\-]+) ([\d.\-]+)\)", text)
    }
    blocks = list(_wire_spans(text))
    if not blocks:
        return 0
    orig = [c for _, _, c in blocks]

    def lifted_tap(seg, box):
        """True if a junction (another wire's tap) sits on the part of the segment
        that the detour LIFTS off -- i.e. strictly inside the body span. Taps at or
        outside the body edges stay on the detour (its outer arms keep the original
        coordinate + the jog corners land on the edges), so they are safe."""
        x1, y1, x2, y2 = seg
        bx0, by0, bx1, by1 = box
        for jx, jy in junctions:
            if abs(y1 - y2) <= EP and abs(jy - y1) <= EP and bx0 + EP < jx < bx1 - EP:
                return True
            if abs(x1 - x2) <= EP and abs(jx - x1) <= EP and by0 + EP < jy < by1 - EP:
                return True
        return False

    final = []
    rerouted = 0
    for seg in orig:
        x1, y1, x2, y2 = seg
        if abs(x1 - x2) <= EP and abs(y1 - y2) <= EP:
            final.append(seg)
            continue
        # A body whose INTERIOR contains a segment endpoint is unavoidable (the
        # wire genuinely terminates inside it) -> never reroute around it. A pin
        # tip sits OUTSIDE the body, so a wire leaving its own part's pin is still
        # detoured off that part's body.
        interior = [b for b in boxes
                    if _pt_in_box(x1, y1, b) or _pt_in_box(x2, y2, b)]
        hit = next((b for b in boxes
                    if b not in interior and _seg_hits_body(seg, b)), None)
        if hit is None or lifted_tap(seg, hit):
            final.append(seg)
            continue
        new_segs = _detour(seg, hit)
        if not new_segs:
            final.append(seg)
            continue
        # the detour must not run through ANY body (other than one it terminates
        # inside) -- never trade one crossing for another.
        bad = False
        for ns in new_segs:
            for b in boxes:
                if b in interior:
                    continue
                if _seg_hits_body(ns, b):
                    bad = True
                    break
            if bad:
                break
        if bad:
            final.append(seg)
            continue
        final.extend(new_segs)
        rerouted += 1

    if rerouted == 0:
        return 0

    first = blocks[0][0]
    for s, e, _ in sorted(blocks, key=lambda w: -w[0]):
        text = text[:s] + text[e:]
    ins = "".join(_wire_sexp(s) for s in final)
    text = text[:first] + ins + text[first:]
    open(sch_path, "w", encoding="utf-8").write(text)
    return rerouted


if __name__ == "__main__":
    import sys
    nm = sys.argv[1] if len(sys.argv) > 1 else "circuit.anvil_sch"
    if not nm.endswith(".anvil_sch"):
        nm += ".anvil_sch"
    print("segments rerouted around bodies:", avoid(nm))
