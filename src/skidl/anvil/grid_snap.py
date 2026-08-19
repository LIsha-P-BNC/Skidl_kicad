"""IPC-3 grid alignment -- snap the CONNECTION coordinates of a .anvil_sch to the
KiCad 1.27 mm (50 mil) grid.

WHY a post-pass and not just placement: SKiDL snaps each part's *anchor* pin to
the 50 mil grid, but a symbol whose other pins sit at non-50 mil offsets (some
connectors / crystals / USB) then lands its remaining pins -- and the wires that
meet them -- off grid. KiCad flags those as `endpoint_off_grid` (IPC-2612 / IPC-3
"all connections on the working grid").

SURGICAL SCOPE (critical): a KiCad 9 sheet inlines each symbol's *graphic* geometry
(polyline `(xy ...)`, pin `(at ...)`) into the body instance, in LOCAL symbol
coordinates (e.g. `(xy -1.905 0)`). Those must NEVER be snapped -- doing so mangles
the symbol's drawn shape. So this snaps ONLY:
  * `(xy ...)` inside `(wire ...)` / `(bus ...)` blocks   -- the routed segments
  * the instance-position `(at ...)` right after `(lib_id "...")`  -- moves the whole
    symbol (its local graphics + pins ride along, staying correctly shaped)
  * `(at ...)` of `(junction ...)`, labels, `(no_connect ...)`
It never touches symbol-internal `(xy)`/`(at)`, `(lib_symbols ...)` definitions, or
property text positions.

CONNECTIVITY SAFETY: snapping is a pure function `snap(v)=round(v/G)*G`; a wire end
and the pin it meets snap to the SAME grid point whenever the pin-to-anchor offset
is a whole grid step (the common case). Where it isn't, the wire could shift off the
pin -- which is why the caller runs this INSIDE the connectivity revert-guard: any
net change throws the whole snap away. So it can only ever help. DYNAMIC: any sheet.
"""
import re

GRID_MM = 1.27


def _snap(v):
    return round(round(float(v) / GRID_MM) * GRID_MM, 2)


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


def snap(sch_path):
    """Snap connection coordinates to the 1.27 mm grid. Returns # of coords moved."""
    try:
        return _snap_file(sch_path)
    except Exception:
        return 0  # never break a build over grid cosmetics


def _snap_xy(m):
    """Snap a '(xy X Y)' match; return (text, moved?)."""
    x, y = float(m.group(1)), float(m.group(2))
    sx, sy = _snap(x), _snap(y)
    moved = sx != round(x, 2) or sy != round(y, 2)
    return f"(xy {_fmt(sx)} {_fmt(sy)})", moved


def _snap_file(sch_path):
    text = open(sch_path, encoding="utf-8").read()
    moved = 0

    # Collect the byte spans we ARE allowed to snap (wire/bus routed segments),
    # then rewrite from the end so earlier offsets stay valid.
    seg_spans = _spans(text, "wire") + _spans(text, "bus")
    edits = []  # (start, end, replacement)

    for s, e in seg_spans:
        blk = text[s:e]
        new_blk, cnt = _sub_xy(blk)
        if cnt:
            edits.append((s, e, new_blk))
            moved += cnt

    # Instance / junction / label / no_connect PRIMARY (at ...) positions.
    #  - symbol instance:  (symbol (lib_id "..") (at X Y [rot])
    #  - junction / no_connect:  (junction (at X Y) ...)
    #  - labels:  (global_label|label|hierarchical_label ".." (at X Y rot) ..)
    at_patterns = [
        r'(\(symbol\s*\(lib_id "[^"]+"\)\s*\(at )([\d.\-]+) ([\d.\-]+)',
        r'(\((?:junction|no_connect)\s*\(at )([\d.\-]+) ([\d.\-]+)',
        r'(\((?:global_label|hierarchical_label|label)\s+"[^"]*"\s*(?:\([^)]*\)\s*)*?\(at )([\d.\-]+) ([\d.\-]+)',
    ]
    for pat in at_patterns:
        for m in re.finditer(pat, text):
            x, y = float(m.group(2)), float(m.group(3))
            sx, sy = _snap(x), _snap(y)
            if sx != round(x, 2) or sy != round(y, 2):
                s = m.start(2)
                e = m.end(3)
                edits.append((s, e, f"{_fmt(sx)} {_fmt(sy)}"))
                moved += 1

    if not moved:
        return 0
    for s, e, rep in sorted(edits, key=lambda t: -t[0]):
        text = text[:s] + rep + text[e:]
    open(sch_path, "w", encoding="utf-8").write(text)
    return moved


def _sub_xy(blk):
    cnt = [0]

    def repl(m):
        new, mv = _snap_xy(m)
        if mv:
            cnt[0] += 1
        return new

    out = re.sub(r"\(xy ([\d.\-]+) ([\d.\-]+)\)", repl, blk)
    return out, cnt[0]


if __name__ == "__main__":
    import sys

    name = sys.argv[1] if len(sys.argv) > 1 else "circuit.anvil_sch"
    if not name.endswith(".anvil_sch"):
        name += ".anvil_sch"
    print("coordinates snapped to grid:", snap(name))
