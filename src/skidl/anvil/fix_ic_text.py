"""fix_ic_text.py -- move a multi-pin symbol's (IC / connector) Reference and
Value text OFF the body so it stops overlapping the pin names drawn inside it.

Measured defect (bms_1s): U1's Value "MCP73831-2-OT" was stamped on the body
center, right over the VDD/VBAT pin-name text -> rendered as garbage
("MVCBA7T3831-2-OT"). Same for connector J1 ("USB_5V_IN" over "Pin_1").

Rule (sibling of fix_text_placement.py, which handles VERTICAL 2-PIN
passives): for every placed symbol with 3+ pins, or any J-ref connector,
  * Reference goes ABOVE the body's top edge,
  * Value goes BELOW the body's bottom edge (below the pin-name overhang for
    connectors),
both horizontally centered on the body -- never inside it.

Body extents come from the instance's lib_symbols definition: union of its
body rectangle(s) and every pin's endpoints, transformed to world coordinates
by the instance (at x y angle) / (mirror ..). Only PROPERTY TEXT moves --
never a pin, wire, label or junction -- so connectivity is untouched.
Fail-safe: any error -> file untouched, returns 0.
"""
import math
import re

REF_GAP_MM = 1.27          # Reference text row above the body top
VAL_GAP_MM = 1.60          # Value text row below the body bottom
VAL_GAP_CONN_MM = 7.62     # connectors draw pin-name text past the pin tip


def _blocks(text, head_re):
    """(start, end) spans of balanced-paren blocks starting at head_re."""
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


def _lib_bodies(text):
    """lib_id -> (bbox (x0,y0,x1,y1) in lib coords, pin_count)."""
    out = {}
    m = re.search(r"\(lib_symbols", text)
    if not m:
        return out
    # lib_symbols block span
    s, e = _blocks(text, r"\(lib_symbols")[0]
    lib = text[s:e]
    for (bs, be) in _blocks(lib, r'\n    \(symbol "'):
        blk = lib[bs:be]
        name = re.search(r'\(symbol "([^"]+)"', blk).group(1)
        xs, ys = [], []
        best_rect = None
        for rm in re.finditer(
                r"\(rectangle\s*\(start ([\d.\-]+) ([\d.\-]+)\)\s*"
                r"\(end ([\d.\-]+) ([\d.\-]+)\)", blk):
            x0, y0, x1, y1 = map(float, rm.groups())
            area = abs(x1 - x0) * abs(y1 - y0)
            if best_rect is None or area > best_rect[0]:
                best_rect = (area, x0, y0, x1, y1)
        pins = re.findall(
            r"\(pin \w+ \w+\s*\(at ([\d.\-]+) ([\d.\-]+) ([\d.\-]+)\)\s*"
            r"\(length ([\d.\-]+)\)", blk)
        for px, py, pa, pl in pins:
            px, py, pa, pl = float(px), float(py), float(pa), float(pl)
            dx, dy = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}.get(
                int(pa) % 360, (0, 0))
            xs += [px, px + dx * pl]
            ys += [py, py + dy * pl]
        if best_rect and best_rect[0] >= 8.0:
            # a real body rectangle: body bbox = that rectangle; still include
            # pin endpoints so the text clears the pin stubs too.
            _, x0, y0, x1, y1 = best_rect
            xs += [x0, x1]
            ys += [y0, y1]
        elif best_rect:
            _, x0, y0, x1, y1 = best_rect
            xs += [x0, x1]
            ys += [y0, y1]
        if not xs:
            continue
        out[name] = ((min(xs), min(ys), max(xs), max(ys)), len(pins))
    return out


def fix(sch_path):
    """Rewrite <sch_path> in place. Returns number of text fields moved."""
    try:
        with open(sch_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0
    try:
        bodies = _lib_bodies(text)
    except Exception:
        return 0
    if not bodies:
        return 0

    out, last, moved = [], 0, 0
    for (s, e) in _blocks(text, r"\(symbol\s*\n\s*\(lib_id"):
        span = text[s:e]
        lib = re.search(r'\(lib_id "([^"]*)"', span)
        at = re.search(r'\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\)', span)
        ref = re.search(r'\(property "Reference" "([^"]*)"', span)
        if not (lib and at and ref):
            continue
        lib_id = lib.group(1)
        if lib_id.startswith("power:") or lib_id not in bodies:
            continue
        (bx0, by0, bx1, by1), npins = bodies[lib_id]
        refname = ref.group(1)
        is_conn = refname.startswith("J")
        if npins < 3 and not is_conn:
            continue                     # 2-pin passives: sibling pass
        x0, y0 = float(at.group(1)), float(at.group(2))
        ang_deg = float(at.group(3)) % 360
        ang = math.radians(ang_deg)
        # Property text renders HORIZONTAL when its stored angle equals the
        # SYMBOL's angle mod 180 (the renderer subtracts the symbol rotation;
        # verified on a 90-deg connector: stored 0 drew vertical, stored 90
        # drew horizontal).
        txt_ang = int(ang_deg % 180)
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
            wy.append(y0 - ry)           # sheet Y grows downward
        top, bot = min(wy), max(wy)
        cx = round((min(wx) + max(wx)) / 2.0, 2)
        ref_y = round(top - REF_GAP_MM, 2)
        val_y = round(bot + (VAL_GAP_CONN_MM if is_conn else VAL_GAP_MM), 2)

        def _move(m):
            nonlocal moved
            name, val = m.group(1), m.group(2)
            ty = ref_y if name == "Reference" else val_y
            moved += 1
            return f'(property "{name}" "{val}" (at {cx} {ty} {txt_ang})'

        new_span = re.sub(
            r'\(property "(Reference|Value)" "([^"]*)"\s*'
            r'\(at [\d.\-]+ [\d.\-]+ [\d.\-]+\)',
            _move, span)
        if new_span != span:
            out.append(text[last:s])
            out.append(new_span)
            last = e
    out.append(text[last:])

    if moved:
        try:
            with open(sch_path, "w", encoding="utf-8") as f:
                f.write("".join(out))
        except OSError:
            return 0
    return moved
