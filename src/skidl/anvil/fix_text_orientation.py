"""fix_text_orientation.py -- no upside-down text on a generated schematic.

When SKiDL places a rotated/mirrored symbol it can write the symbol's property
text (Reference / Value / ...) with the symbol's own angle, e.g. 180 or 270.
KiCad renders such field text upside-down or reading downward -- IEEE 315 /
IPC-2612 drawings only ever use 0 (horizontal) or 90 (vertical, reading up).

This normalizes every property-text angle in a .anvil_sch:  180 -> 0,
270 -> 90.  Center-justified text (the generator's default) keeps its anchor,
so the text stays in place -- it just becomes readable.  Properties that carry
an explicit left/right (justify ...) are left untouched (flipping those needs
a justification swap too, and the generator never emits them).

Use:
    import fix_text_orientation
    n = fix_text_orientation.fix("myboard.anvil_sch")
"""
import re

# (property "Name" "Value" ... (at X Y 180|270)  -- match only property blocks so a
# symbol's own placement "(at x y 180)" is never touched.
_PROP_AT = re.compile(
    r'(\(property\s+"[^"]*"\s+"[^"]*"\s*\(at\s+[\d.\-]+\s+[\d.\-]+\s+)(180|270)(\))'
)


def fix(sch_path):
    """Rewrite <sch_path> in place. Returns number of angles normalized."""
    with open(sch_path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    changed = 0
    out = []
    last = 0
    for m in _PROP_AT.finditer(text):
        # skip if this property block declares its own left/right justification
        prop_tail = text[m.end() : m.end() + 400]
        if re.search(r"\(justify[^)]*\b(left|right)\b", prop_tail):
            continue
        out.append(text[last : m.start(2)])
        out.append("0" if m.group(2) == "180" else "90")
        last = m.end(2)
        changed += 1
    out.append(text[last:])

    if changed:
        with open(sch_path, "w", encoding="utf-8") as f:
            f.write("".join(out))
    return changed


if __name__ == "__main__":
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else "circuit.anvil_sch"
    if not p.endswith(".anvil_sch"):
        p += ".anvil_sch"
    print(f"text angles normalized: {fix(p)}")
