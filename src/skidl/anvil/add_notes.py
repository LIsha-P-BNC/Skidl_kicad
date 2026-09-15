"""On-sheet design notes -- schematic rule C2 (Sierra "include all required notes").

WHY THIS EXISTS: a professional schematic carries a NOTES block on the drawing
itself -- jumper settings, DNP/NP marks, layout constraints, assembly/test
warnings -- so a reader sees them without opening a separate document. The Anvil
writer (sexp_schematic) emits no free `(text ...)` items, so notes authored via
build(notes=...) had nowhere to land except the chat report / _review.md, never
the sheet the fab and assembler actually read. This pass writes them as KiCad
schematic text in the page's TOP-LEFT corner.

Placement is safe by construction: the auto-fit page centres content with a
>= PAGE_MARGIN (10 mm) empty band on every side, and the title block owns the
bottom-right, so the top-left corner band is free. KiCad's page origin is the
top-left (x right, y down), so small coordinates land there. The block grows
downward and stays left of the (centred) content column.

DYNAMIC: pure text injection over any sheet -- no part names, no counts, no grid
assumption. A note carries no connection point, so it can NEVER change
connectivity or ERC; the caller runs it unguarded, like PWR_FLAG insertion.
"""
import uuid

# Deterministic UUID namespace (matches the codebase convention: uuid5, never
# uuid4, so the multi-seed rebuild + kicad-cli re-exports don't churn the file).
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "anvil-notes")

# Top-left corner block, inside the page border. Content is centred with a
# >= 10 mm margin, so this corner (< margin from both edges) is empty. mm.
_X0 = 8.89     # 350 mil in from the left edge
_Y0 = 8.89     # 350 mil down from the top edge
_LINE = 2.54   # 100 mil between note lines (comfortable for 1.27 mm text)
_FONT = 1.27   # mm -- same text size the writer uses for labels


def _fmt(v):
    """Match the file's 2-decimal coordinate style (11 -> '11', 8.89 -> '8.89')."""
    return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def _norm(notes):
    """Accept a str or an iterable of str; return a clean list of non-empty lines."""
    if notes is None:
        return []
    if isinstance(notes, str):
        notes = [notes]
    out = []
    for n in notes:
        if n is None:
            continue
        s = str(n).strip()
        if s:
            out.append(s)
    return out


def _text_item(s, x, y):
    """One top-level (text ...) record, mirroring sexp_schematic's own format."""
    esc = s.replace("\\", "\\\\").replace('"', '\\"')
    uid = uuid.uuid5(_NS, f"{_fmt(y)}:{s}")
    return (
        f'\t(text "{esc}"\n'
        f"\t\t(at {_fmt(x)} {_fmt(y)} 0)\n"
        f"\t\t(effects\n"
        f"\t\t\t(font\n"
        f"\t\t\t\t(size {_FONT} {_FONT})\n"
        f"\t\t\t)\n"
        f"\t\t\t(justify left top)\n"
        f"\t\t)\n"
        f'\t\t(uuid "{uid}")\n'
        f"\t)"
    )


def add(sch_path, notes, header="Notes:"):
    """Write a notes block to the top-left of *sch_path*.

    notes  -- a string or list of strings; each becomes one numbered line.
    header -- optional un-numbered title above the list ("" to omit).
    Returns the number of text lines written (0 if there were no notes).
    """
    try:
        return _add(sch_path, notes, header)
    except Exception:
        return 0  # never break a build over a decorative pass


def _add(sch_path, notes, header):
    lines = _norm(notes)
    if not lines:
        return 0

    with open(sch_path, encoding="utf-8") as f:
        text = f.read()

    rows = []
    if header:
        rows.append(header)
    for i, ln in enumerate(lines, 1):
        rows.append(f"{i}. {ln}")

    blocks, y = [], _Y0
    for row in rows:
        blocks.append(_text_item(row, _X0, y))
        y += _LINE

    # Insert before the schematic's final top-level ')', each on its own line
    # (the final ')' may be glued to the preceding record, so re-lead with \n).
    ins = text.rstrip().rfind(")")
    if ins < 0:
        return 0
    text = text[:ins].rstrip() + "\n" + "\n".join(blocks) + "\n" + text[ins:]

    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(rows)
