"""Junction completeness -- the FINAL wire-connectivity safety pass.

WHY THIS EXISTS: the schematic editor's connectivity engine (eeschema
connection_graph.cpp) connects items ONLY where their connection POINTS
coincide -- and a wire's connection points are its two ENDPOINTS. A wire end
touching the INTERIOR of another wire is electrically DEAD unless a
`(junction ...)` record sits at that point (junctions pull in mid-segment
wires explicitly). The editor auto-inserts that junction while a human draws;
a generated file must carry it itself.

The router's add_junctions() (schematics/route.py) computes junctions from its
in-memory segments, but it assumes merge_segments() endpoint ordering
(p1 < p2) and runs BEFORE the post-route geometry passes (pin straps, label
stubs, beautify merges) that can create new end-on-interior touches. Any wire
added or reshaped after that computation can therefore ship a T-touch with no
junction record -- wires visibly touching, net silently split (observed:
led_blinker_555, drone_bms_controller, test_motor_pwm, ...).

So this pass re-derives junctions from the FINAL sheet text, assumption-free:
  * parse every top-level (wire ...) segment (lib_symbols graphics excluded)
  * a point needs a junction when a wire ENDPOINT lands strictly inside
    another wire's span, or when >= 3 wire endpoints coincide
  * insert a (junction ...) record for every such point not already present
  * SPLIT every wire whose interior passes through a junction point, so the
    dot always sits on wire ENDPOINTS -- the exact structure the editor
    itself draws (BreakSegment + junction). This matters: a dot on an
    UNBROKEN wire's midpoint is honored by the CLI netlister but the GUI
    editor's live connectivity still reports the tapping pin unconnected
    (observed live on led_blinker_555 DISCH), so endpoint form is the only
    representation that works everywhere.

It only ADDS junctions at same-net wire touches the file already draws (the
routing gates forbid cross-net end-on-interior touches), and the caller runs
it inside the per-step connectivity revert-guard, so it can only ever heal.
DYNAMIC: pure geometry over any sheet -- no part names, no counts, no grid
assumption (works at any coordinate the wires actually use).
"""
import re
import uuid

# match the file's own 2-decimal coordinate style for point identity
_Q = 0.01


def _key(x, y):
    return (round(float(x) / _Q) * _Q, round(float(y) / _Q) * _Q)


def _fmt(v):
    return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _spans(text, tag):
    """(start, end) spans of every top-level (tag ...) s-expr."""
    spans = []
    for m in re.finditer(r"\(" + tag + r"\b", text):
        i = m.start()
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    spans.append((i, j + 1))
                    break
    return spans


def _interior(pt, a, b):
    """pt lies STRICTLY inside axis-aligned segment a-b (order-agnostic)."""
    (x, y), (x1, y1), (x2, y2) = pt, a, b
    if x1 == x2 == x:
        lo, hi = min(y1, y2), max(y1, y2)
        return lo < y < hi
    if y1 == y2 == y:
        lo, hi = min(x1, x2), max(x1, x2)
        return lo < x < hi
    return False


def ensure(sch_path):
    """Add every missing wire-junction record. Returns # of junctions added."""
    try:
        return _ensure(sch_path)
    except Exception:
        return 0  # never break a build over a healing pass


def _ensure(sch_path):
    with open(sch_path, encoding="utf-8") as f:
        text = f.read()

    # work on a copy with lib_symbols blanked so symbol-internal graphics
    # (polyline xy, pin at) never look like sheet wires; offsets stay valid
    scan = text
    for s, e in _spans(text, "lib_symbols"):
        scan = scan[:s] + " " * (e - s) + scan[e:]

    segs = []
    for s, e in _spans(scan, "wire"):
        pts = re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", scan[s:e])
        p = [_key(x, y) for x, y in pts]
        segs.extend(zip(p, p[1:]))

    ends = {}
    for a, b in segs:
        ends[a] = ends.get(a, 0) + 1
        ends[b] = ends.get(b, 0) + 1

    have = set()
    for s, e in _spans(scan, "junction"):
        m = re.search(r"\(at ([-\d.]+) ([-\d.]+)\)", scan[s:e])
        if m:
            have.add(_key(m.group(1), m.group(2)))

    need = set()
    for pt, n in ends.items():
        if pt in have:
            continue
        if n >= 3 or any(_interior(pt, a, b) for a, b in segs):
            need.add(pt)

    # every junction (existing or new) must sit on wire ENDPOINTS, never on
    # an unbroken wire's interior -- split crossed wires exactly like the
    # editor's BreakSegment does
    all_juncs = have | need
    wire_spans = _spans(scan, "wire")
    splits = 0
    new_text_parts = []
    cursor = 0
    line_start = text.rfind("\n", 0, wire_spans[0][0]) + 1 if wire_spans else 0
    indent = text[line_start:wire_spans[0][0]] if wire_spans else "\t"
    tab = indent + "\t" if indent else "\t"

    for s, e in wire_spans:
        pts = [_key(x, y) for x, y in
               re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", scan[s:e])]
        # cut the polyline at every junction point interior to one of its segments
        pieces, cur = [], [pts[0]] if pts else []
        for a, b in zip(pts, pts[1:]):
            cuts = sorted(
                (j for j in all_juncs if _interior(j, a, b)),
                key=lambda j: (abs(j[0] - a[0]), abs(j[1] - a[1])),
            )
            for j in cuts:
                cur.append(j)
                pieces.append(cur)
                cur = [j]
            cur.append(b)
        if cur and len(cur) > 1:
            pieces.append(cur)
        if len(pieces) <= 1:
            continue  # nothing to split in this wire
        splits += len(pieces) - 1
        blocks = []
        for piece in pieces:
            pid = uuid.uuid5(uuid.NAMESPACE_URL,
                             "anvil-wire:" + ";".join(f"{x}:{y}" for x, y in piece))
            xy = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in piece)
            blocks.append(
                f"{indent}(wire\n"
                f"{tab}(pts\n"
                f"{tab}\t{xy}\n"
                f"{tab})\n"
                f"{tab}(stroke\n"
                f"{tab}\t(width 0)\n"
                f"{tab}\t(type default)\n"
                f"{tab})\n"
                f"{tab}(uuid \"{pid}\")\n"
                f"{indent})"
            )
        new_text_parts.append(text[cursor:s])
        new_text_parts.append("\n".join(blocks))
        cursor = e
    new_text_parts.append(text[cursor:])
    text = "".join(new_text_parts)

    if not need and not splits:
        return 0

    if need:
        # insert junction records after the last top-level wire
        wire_spans = _spans(text, "wire")
        ins_at = wire_spans[-1][1] if wire_spans else text.rfind(")")
        blocks = []
        for x, y in sorted(need):
            jid = uuid.uuid5(uuid.NAMESPACE_URL, f"anvil-junction:{x}:{y}")
            blocks.append(
                f"\n{indent}(junction\n"
                f"{tab}(at {_fmt(x)} {_fmt(y)})\n"
                f"{tab}(diameter 0)\n"
                f"{tab}(color 0 0 0 0)\n"
                f"{tab}(uuid \"{jid}\")\n"
                f"{indent})"
            )
        text = text[:ins_at] + "".join(blocks) + text[ins_at:]

    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(need) + splits
