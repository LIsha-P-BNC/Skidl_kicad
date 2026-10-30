"""fix_text_placement.py -- move a VERTICAL 2-pin passive's Reference/Value
text OFF the body so it stops overlapping the pins, pin numbers and each other.

A vertical passive (Device:R/C/L/CP/D/LED drawn at angle 0 or 180, body along
Y, pins top & bottom) gets its Reference and Value property text centered on
the SAME X as the symbol -- i.e. sitting right on the vertical body and its pin
numbers (measured: "R1" over pin 2, "1k" over pin 1, reads as "1.6k"). The
professional placement is to the SIDE: shift the text in X and left-justify it
so it reads outward from the body into clear space.

Only PROPERTY TEXT positions move -- never a pin, wire, label or junction -- so
drawn connectivity is byte-for-byte unaffected (the caller's connectivity guard
confirms it). Pure geometry, works on any .anvil_sch. Fail-safe: any error ->
file untouched, returns 0.

Use:
    import fix_text_placement
    n = fix_text_placement.fix("board.anvil_sch")
"""
import re

OFFSET_MM = 3.81          # 150 mil: centered text at this X clears the body

# A vertical 2-pin passive: ref-prefix R/C/L/D (incl. CP/LED) whose symbol angle
# is 0 or 180 (body vertical). Horizontal ones (90/270) put text above/below,
# which does not overlap pin numbers, so they are left alone.
_PASSIVE_LIB = re.compile(r':(R|C|L|CP|D|LED|F|Fuse|Polyfuse)(_|$|["\d])',
                          re.IGNORECASE)


def _instance_spans(text):
    """(start, end) of each placed-symbol block: (symbol (lib_id ...))."""
    spans = []
    for mo in re.finditer(r"\(symbol\s*\n\s*\(lib_id", text):
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


def fix(sch_path):
    """Rewrite <sch_path> in place. Returns number of text fields moved."""
    try:
        with open(sch_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0

    out, last, moved = [], 0, 0
    for (s, e) in _instance_spans(text):
        span = text[s:e]
        lib = re.search(r'\(lib_id "([^"]*)"', span)
        at = re.search(r'\(lib_id "[^"]*"\s*\)\s*\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\)',
                       span)
        if not (lib and at) or not _PASSIVE_LIB.search(lib.group(1)):
            continue
        sym_x = float(at.group(1))
        angle = float(at.group(3)) % 360
        if angle not in (0.0, 180.0):
            continue                          # only vertical passives
        tgt_x = round(sym_x + OFFSET_MM, 2)
        new_span = span

        def _move_prop(m):
            nonlocal moved
            name, val, px, py, pa = m.groups()
            if abs(float(px) - sym_x) > 0.51:    # already off the body axis
                return m.group(0)
            moved += 1
            # Shift X only; centered text at +150 mil clears the body. No
            # justify surgery (nesting it into (font ...) is fragile and
            # unnecessary for readability).
            return f'(property "{name}" "{val}" (at {tgt_x} {py} {pa})'

        new_span = re.sub(
            r'\(property "(Reference|Value)" "([^"]*)"\s*'
            r'\(at ([\d.\-]+) ([\d.\-]+) ([\d.\-]+)\)',
            _move_prop, new_span, flags=re.DOTALL)
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
