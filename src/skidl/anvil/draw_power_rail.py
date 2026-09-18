"""Power-rail ladder draw -- collapse per-pin power symbols into ONE horizontal
rail wire + short vertical stubs + ONE source symbol per rail.

WHY THIS EXISTS: the generator stubs every power pin to its OWN power symbol
(net_label_to_sexp -> _power_symbol_to_sexp, one [stub,symbol] per pin). That is
electrically correct but reads as "a +12V symbol on every pin". A hand-drawn
professional sheet (and the user's own TRACKER-V2 board) instead draws each rail
as a single horizontal WIRE spine with short vertical stubs dropping to each
pin, and just ONE power symbol naming the rail. This pass rewrites the first form
into the second, PURELY as a text transform on the finished .anvil_sch -- no
change to the generator.

HOW connectivity is preserved (KiCad connects only at coincident endpoints /
junctions):
  * every part pin keeps a wire to the rail (its vertical stub),
  * the rail spine ties all those stub-tops together at a common Y,
  * exactly ONE power:<net> symbol stays, its pin sitting on the rail, so the
    net keeps its name and (via the later add_pwr_flags pass, which finds that
    one symbol) stays ERC-clean.
The caller runs this INSIDE the per-step connectivity revert-guard
(smart_schematic._guarded), so if any rewrite would fuse or split a net the
whole pass is rolled back -- it can only ever improve the drawing.

SCOPE: the caller (smart_schematic) runs this ONLY for flow-hinted single-row
layouts, where a clean horizontal rail actually fits; on an arbitrary multi-block
sheet a rail spine can look worse even when connectivity is fine (which the guard
does not catch). SKIDL_DRAW_RAILS=0/1 forces it off/on for any layout;
SKIDL_DRAW_RAILS_GND=0 keeps ground as per-pin symbols (in flow mode the ground
net becomes a bottom rail by default).

DYNAMIC: pure geometry + net-name pattern over any sheet -- no part names, no
counts, no grid assumption.
"""
import re
import uuid

_NS = uuid.NAMESPACE_URL

# a power net that reads as a positive supply rail (+12V, +5V, +3V3, VCC, VDD,
# VIN, 3V3, 5V ...). GND is handled separately and only when opted in.
#
# CANONICAL source is net_classify._POWER_RAIL_NET_RE so placement top-bias and
# this ladder-draw agree on what a rail is (they used to diverge). Fallback keeps
# the module importable if loaded standalone (it is loaded by bare name). This
# pass is connectivity-guarded by the caller, so any slight narrowing vs the old
# broad local pattern can only reduce collapses, never break a net.
try:
    from skidl.schematics.net_classify import _POWER_RAIL_NET_RE as _RAIL_RE
    from skidl.schematics.net_classify import log_swallowed as _log_swallowed
except Exception:  # standalone / import path not set up
    _RAIL_RE = re.compile(
        r"^(\+[\w.]+|(A|D)?V(CC|DD|IN|BUS|BAT|SYS)\d*|\d+V\d*|[A-Za-z0-9]+_\d+V\d*)$",
        re.I,
    )

    def _log_swallowed(where, exc):
        pass


def draw(sch_path, enable_gnd=False):
    """Redraw per-pin power symbols as horizontal rails. Returns # of rails drawn.

    The CALLER decides when to run this (smart_schematic enables it for
    flow-hinted single-row layouts, where a clean horizontal rail actually
    fits). `enable_gnd` also collapses the ground net into a bottom rail; off by
    default because ground pins fan out in every direction on a non-flow sheet.
    """
    try:
        return _draw(sch_path, enable_gnd)
    except Exception as exc:
        _log_swallowed("draw_power_rail", exc)
        return 0  # never break a build over a drawing pass


def _fmt(v):
    v = round(float(v), 2)
    return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _close_of(text, start):
    """Index of the ')' closing the s-expr that opens at text[start] == '('."""
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced s-expression")


def _wire(x1, y1, x2, y2, tag):
    u = uuid.uuid5(_NS, f"rail-wire:{tag}:{x1}:{y1}:{x2}:{y2}")
    return (
        "  (wire\n"
        "    (pts\n"
        f"      (xy {_fmt(x1)} {_fmt(y1)})\n"
        f"      (xy {_fmt(x2)} {_fmt(y2)}))\n"
        "    (stroke\n"
        "      (width 0)\n"
        "      (type default))\n"
        f"    (uuid {u}))\n"
    )


def _junction(x, y, tag):
    u = uuid.uuid5(_NS, f"rail-junc:{tag}:{x}:{y}")
    return (
        "  (junction\n"
        f"    (at {_fmt(x)} {_fmt(y)})\n"
        "    (diameter 0)\n"
        "    (color 0 0 0 0)\n"
        f"    (uuid {u}))\n"
    )


def _shift_ats(block, dx, dy):
    """Translate every (at X Y [ang]) inside a symbol block by (dx,dy)."""
    def repl(m):
        x = float(m.group(1)) + dx
        y = float(m.group(2)) + dy
        tail = m.group(3) or ""
        return f"(at {_fmt(x)} {_fmt(y)}{tail})"
    return re.sub(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(\s+-?[\d.]+)?\)", repl, block)


# ---- power-symbol block + its stub wire ---------------------------------

_SYM_OPEN = re.compile(r'\(symbol\s*\(lib_id\s+"power:([^"]+)"\)')
_AT_RE = re.compile(r"\(at\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\)")


def _find_symbols(text):
    """List of dicts for every power:<net> symbol: net, span (s,e), sx, sy, block."""
    out = []
    for m in _SYM_OPEN.finditer(text):
        net = m.group(1)
        if net == "PWR_FLAG":
            continue
        s = m.start()
        e = _close_of(text, s) + 1
        block = text[s:e]
        am = _AT_RE.search(block)  # first (at) after lib_id is the symbol origin
        if not am:
            continue
        out.append({
            "net": net, "s": s, "e": e, "block": block,
            "sx": float(am.group(1)), "sy": float(am.group(2)),
        })
    return out


def _find_stub(text, sx, sy):
    """Stub wire (span + far endpoint) whose one endpoint == (sx,sy). None if the
    symbol was clamped onto the pin (no separate stub)."""
    for wm in re.finditer(
        r"\(wire\s*\(pts\s*\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s*"
        r"\(xy\s+(-?[\d.]+)\s+(-?[\d.]+)\)\)",
        text,
    ):
        x1, y1, x2, y2 = (float(wm.group(i)) for i in (1, 2, 3, 4))
        e1 = abs(x1 - sx) < 0.01 and abs(y1 - sy) < 0.01
        e2 = abs(x2 - sx) < 0.01 and abs(y2 - sy) < 0.01
        if not (e1 or e2):
            continue
        s = wm.start()
        e = _close_of(text, s) + 1
        far = (x2, y2) if e1 else (x1, y1)
        return {"s": s, "e": e, "far": far}
    return None


# part-body half-sizes (mm) by symbol class, for the "no wire over a component"
# rule -- a rail stub that would run THROUGH a part body is forbidden (the router
# already treats a part bbox as un-routable; the rail-draw must honour the same).
# Passives are kept SMALL so a pin sitting on the body edge is never mistaken for
# an interior crossing; ICs get a tall/wide box because their power pins often sit
# on the far side from the rail (e.g. a regulator VIN at the bottom, +rail at top).
_PASSIVE_RE = re.compile(r"^(C|CP|R|L|D|LED|FB|Fuse|Crystal|Y|SW|TP)(_|\d|$)", re.I)


def _part_bodies(text):
    """(cx, cy, hw, hh) for every non-power part symbol -- an approximate body box."""
    bodies = []
    for m in re.finditer(r'\(symbol\s*\(lib_id\s+"([^"]+)"\)\s*\(at\s+(-?[\d.]+)\s+(-?[\d.]+)', text):
        lib = m.group(1)
        if lib.startswith("power:"):
            continue
        name = lib.split(":")[-1]
        cx, cy = float(m.group(2)), float(m.group(3))
        if _PASSIVE_RE.match(name):
            hw, hh = 1.6, 1.2          # 2-pin passive: tiny, never blocks its own stub
        elif "Conn" in name:
            hw, hh = 2.2, 2.6          # connector
        else:
            hw, hh = 6.5, 8.0          # IC / multi-pin regulator / driver
        bodies.append((cx, cy, hw, hh))
    return bodies


def _stub_crosses_body(px, py, rail_y, bodies):
    """True if the vertical stub x=px, y in [py..rail_y] runs through a part body
    interior (more than a boundary touch) -- i.e. a wire over a component."""
    ylo, yhi = (py, rail_y) if py <= rail_y else (rail_y, py)
    for cx, cy, hw, hh in bodies:
        if cx - hw + 0.01 < px < cx + hw - 0.01:
            ov = min(yhi, cy + hh) - max(ylo, cy - hh)
            if ov > 0.6:
                return True
    return False


def _block_boxes(text):
    """(x0, y0, x1, y1) of every DASHED rectangle -- the drawn function-block
    boxes. Rails are drawn PER BLOCK so a rail never crosses a block boundary
    (the user's ladder is a per-function shape, not a sheet-wide bus)."""
    boxes = []
    for m in re.finditer(
            r"\(rectangle\s*\(start\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s*"
            r"\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s*\(stroke[^)]*\)\s*\(type dash",
            text):
        x0, y0, x1, y1 = (float(m.group(i)) for i in (1, 2, 3, 4))
        boxes.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
    return boxes


def _box_ix(boxes, x, y):
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        if x0 <= x <= x1 and y0 <= y <= y1:
            return i
    return -1


def _draw(sch_path, include_gnd=False):
    text = open(sch_path, encoding="utf-8", errors="replace").read()

    bodies = _part_bodies(text)
    boxes = _block_boxes(text)
    syms = _find_symbols(text)
    # group symbols by net
    by_net = {}
    for sy in syms:
        by_net.setdefault(sy["net"], []).append(sy)

    del_spans = []      # (s,e) ranges to delete (extra symbols + all their stubs)
    new_blocks = []     # rail wires + stubs + relocated source symbols
    drawn = 0

    for net, group in sorted(by_net.items()):
        is_gnd = net.upper() in ("GND", "GNDA", "GNDD", "GNDPWR", "AGND", "DGND")
        if is_gnd and not include_gnd:
            continue
        if not is_gnd and not _RAIL_RE.match(net):
            continue
        if len(group) < 2:
            continue  # a single symbol is already the clean form

        # resolve each symbol's part-pin point via its stub
        for g in group:
            st = _find_stub(text, g["sx"], g["sy"])
            if st:
                g["stub"] = st
                g["px"], g["py"] = st["far"]
            else:
                g["stub"] = None
                g["px"], g["py"] = g["sx"], g["sy"]   # clamped: pin == symbol origin

        # PARTITION BY BLOCK BOX: the ladder is a PER-FUNCTION shape -- each
        # block gets its OWN rail + ONE symbol, and a rail never crosses a
        # dashed block boundary (a multi-block sheet must not grow one
        # sheet-wide bus bar). Pins outside any box form one extra partition.
        parts_map = {}
        for g in group:
            parts_map.setdefault(_box_ix(boxes, g["px"], g["py"]), []).append(g)

        for bix, members in sorted(parts_map.items()):
            if len(members) < 2:
                continue  # a lone pin in this block keeps its own symbol

            # provisional rail Y (for the crossing test), then split the pins:
            # RAILABLE -- a clean straight stub to the rail; EXCLUDED -- the
            # stub would run through a component body (e.g. a regulator VIN on
            # the far side from the rail). Excluded pins KEEP their own power
            # symbol -- so no wire is ever drawn over a component.
            if is_gnd:
                prov = max(g["py"] for g in members) + 2.54
            else:
                prov = min(g["sy"] for g in members)
            railable = [g for g in members
                        if not _stub_crosses_body(g["px"], g["py"], prov, bodies)]
            if len(railable) < 2:
                continue  # not enough clean pins to justify a rail

            pins = [(g["px"], g["py"]) for g in railable]
            if len({round(px, 2) for px, _ in pins}) < 2:
                continue  # all railable pins share one X: nothing horizontal

            # final rail geometry from this block's railable pins only
            if is_gnd:
                rail_y = max(py for _, py in pins) + 2.54
            else:
                rail_y = min(g["sy"] for g in railable)
            xs = [px for px, _ in pins]
            rail_x1, rail_x2 = min(xs), max(xs)

            # keep the LEFTMOST railable symbol as this block's single source;
            # relocate it onto the rail's left end; delete the others + stubs.
            railable.sort(key=lambda g: g["px"])
            keep = railable[0]
            relocated = _shift_ats(keep["block"],
                                   rail_x1 - keep["sx"], rail_y - keep["sy"])
            for g in railable:
                del_spans.append((g["s"], g["e"]))
                if g["stub"]:
                    del_spans.append((g["stub"]["s"], g["stub"]["e"]))

            # rail spine + one vertical stub per railable pin + a junction dot
            # at every INTERIOR tap (a stub top strictly inside the spine is
            # dead without an explicit junction record, and the revert-guard
            # verifies BEFORE ensure_junctions runs).
            tag = f"{net}:{bix}"
            seg = [relocated, _wire(rail_x1, rail_y, rail_x2, rail_y, tag)]
            for px, py in sorted(pins):
                if abs(py - rail_y) >= 0.01:
                    seg.append(_wire(px, py, px, rail_y, tag))
                if rail_x1 + 0.01 < px < rail_x2 - 0.01:
                    seg.append(_junction(px, rail_y, tag))
            new_blocks.append("".join(seg))
            drawn += 1

    if not drawn:
        return 0

    # apply deletions high->low so earlier offsets stay valid
    for s, e in sorted(set(del_spans), reverse=True):
        text = text[:s] + text[e:]

    # insert the rail blocks just before the sheet's final closing paren
    end = _close_of(text, text.index("(kicad_sch"))
    text = text[:end] + "\n" + "".join(new_blocks) + text[end:]

    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(text)
    return drawn
