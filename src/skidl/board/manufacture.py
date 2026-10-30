"""
src/skidl/board/manufacture.py

[E] in the M0/M1 pipeline: manufacturing outputs via kicad-cli. M1 scope:
Gerbers + drill + pick-and-place position file, zipped into a single
<name>_fab.zip a fab house accepts. STEP/PDF/SVG breadth is M5.
"""

from __future__ import annotations
import subprocess
import zipfile
from pathlib import Path

_NO_WINDOW = None  # resolved lazily so non-Windows imports don't break


def _run(cmd, timeout=180):
    import subprocess as sp
    return sp.run(cmd, stdin=sp.DEVNULL, capture_output=True, text=True,
                  timeout=timeout,
                  creationflags=getattr(sp, "CREATE_NO_WINDOW", 0))


def _board_plot_prefs(pcb_path: Path) -> dict:
    """The user's saved Plot/Drill dialog state, read from the board file.
    The app persists both dialogs into the board's (pcbplotparams ...) block
    (the Drill dialog stores its origin there via SetUseAuxOrigin). ANSWER
    PARITY: when the block exists the user configured plotting -- exports
    must honor it, not kicad-cli defaults."""
    prefs = {"has_plot_params": False, "use_aux_origin": False}
    try:
        txt = pcb_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return prefs
    i = txt.find("(pcbplotparams")
    if i < 0:
        return prefs
    prefs["has_plot_params"] = True
    block = txt[i:i + 4000]
    prefs["use_aux_origin"] = bool(
        __import__("re").search(r"\(useauxorigin\s+true\)", block))
    return prefs


def _fab_gerber_layers(pcb_path: Path) -> list:
    """The layers a FAB needs, derived from the board's OWN copper stackup --
    never the board's (layerselection ...) bitmask.

    WHY: a machine-generated board carries a stale template layerselection that
    plots the WRONG set (measured: it dropped Edge.Cuts + silkscreen and added
    non-fab B/F_Adhesive + B/F_Courtyard). A gerber set with NO Edge.Cuts has no
    board outline -- a fab cannot make the board, yet the export still reports
    "ok" (a SILENT defect on EVERY board). So we always plot: every copper layer
    the stackup actually has (F/In*/B) + the standard technical layers + the
    mandatory Edge.Cuts. Dynamic over any layer count; empty silk/paste layers
    export as harmless empty gerbers."""
    import re as _re
    copper = ["F.Cu", "B.Cu"]
    try:
        txt = pcb_path.read_text(encoding="utf-8", errors="replace")
        m = _re.search(r"\(layers\b(.*?)\n\s*\)", txt, _re.S)
        blk = m.group(1) if m else txt
        found = _re.findall(r'"([A-Za-z0-9]+\.Cu)"', blk)
        # F.Cu first, inner layers in order, B.Cu last -- the physical stack.
        inner = [l for l in dict.fromkeys(found) if l not in ("F.Cu", "B.Cu")]
        def _ord(l):
            mm = _re.search(r"In(\d+)", l)
            return int(mm.group(1)) if mm else 0
        copper = (["F.Cu"] if "F.Cu" in found else []) \
            + sorted(inner, key=_ord) \
            + (["B.Cu"] if "B.Cu" in found else [])
        if not copper:
            copper = ["F.Cu", "B.Cu"]
    except OSError:
        pass
    # Standard fab technical layers + the MANDATORY board outline.
    tech = ["F.Mask", "B.Mask", "F.SilkS", "B.SilkS", "F.Paste", "B.Paste",
            "Edge.Cuts"]
    return copper + tech


def export_manufacturing(pcb_path: Path, kicad_cli: str,
                          formats: list = None) -> dict:
    """Export fab files next to the board into <name>_fab/ and zip them
    as <name>_fab.zip. Returns per-format status + the zip path. A format
    that fails is reported, never silently dropped. Honors the user's saved
    Plot settings from the board (pcbplotparams): gerbers use
    --board-plot-params, drill/pos use the configured aux origin."""
    pcb_path = Path(pcb_path)
    base = pcb_path.stem
    fab_dir = pcb_path.parent / f"{base}_fab"
    # CLEAR stale outputs first: kicad-cli writes new files but never deletes
    # old ones, so a re-export after a layer/stackup change would SHIP the stale
    # gerbers alongside the new (measured: buck kept B_Adhesive/B_Courtyard from
    # an earlier wrong export). The zip is the whole dir -> it must hold ONLY
    # this run's files.
    if fab_dir.exists():
        for _f in fab_dir.iterdir():
            if _f.is_file():
                try:
                    _f.unlink()
                except OSError:
                    pass
    fab_dir.mkdir(exist_ok=True)
    formats = formats or ["gerbers", "drill", "pos"]
    prefs = _board_plot_prefs(pcb_path)

    results = {}
    notes = {}                 # informational, kept OUT of per-format status
    gerber_src = pcb_path      # may be swapped for a sanitized temp copy
    tmp_holder = []            # keeps TemporaryDirectory alive until zipped
    for fmt in formats:
        if fmt == "gerbers":
            if prefs["has_plot_params"]:
                # FAB SANITY: (drillshape 1/2) plots drill MARKS into the
                # gerbers -- a KiCad print-dialog default, never wanted by a
                # fab (the drill file carries the holes; a 0.35mm mark
                # flashed on the Profile layer reads as a tiny cutout).
                # Export from a same-stem temp copy with marks off so the
                # user's board file and their other settings stay untouched.
                import re as _re, tempfile
                txt = pcb_path.read_text(encoding="utf-8", errors="replace")
                if _re.search(r"\(drillshape\s+[12]\)", txt):
                    td = tempfile.TemporaryDirectory()
                    tmp_holder.append(td)
                    gerber_src = Path(td.name) / pcb_path.name
                    gerber_src.write_text(
                        _re.sub(r"\(drillshape\s+[12]\)", "(drillshape 0)",
                                txt), encoding="utf-8")
                    notes["drill_marks"] = ("disabled in fab gerbers "
                                            "(board file untouched)")
            # ALWAYS plot an explicit fab layer set derived from the board's own
            # copper stackup -- NOT the board's (layerselection) bitmask, and
            # NOT --board-plot-params (which RE-APPLIES that bitmask and so
            # overrides --layers -- measured: buck's template bitmask dropped
            # Edge.Cuts even WITH --layers present). A machine board's stale
            # bitmask omits the outline (unmanufacturable) yet still reports
            # "ok" -- a SILENT defect. --layers guarantees outline + standard
            # set on every board. Origin stays consistent with the drill/pos
            # files because our boards carry aux_axis_origin (0 0).
            cmd = [kicad_cli, "pcb", "export", "gerbers",
                   "--output", str(fab_dir) + "/",
                   "--layers", ",".join(_fab_gerber_layers(pcb_path)),
                   str(gerber_src)]
        elif fmt == "drill":
            cmd = [kicad_cli, "pcb", "export", "drill",
                   "--output", str(fab_dir) + "/",
                   # fab houses need the map; PDF is the app's default form
                   "--generate-map", "--map-format", "pdf"]
            if prefs["use_aux_origin"]:
                cmd += ["--drill-origin", "plot"]
            cmd.append(str(pcb_path))
        elif fmt == "pos":
            cmd = [kicad_cli, "pcb", "export", "pos", "--units", "mm"]
            if prefs["use_aux_origin"]:
                # keep placement coordinates in the same frame as the drill
                # file -- an absolute-origin .pos against aux-origin drills
                # shifts every part at assembly
                cmd.append("--use-drill-file-origin")
            cmd += ["--output", str(fab_dir / f"{base}.pos"), str(pcb_path)]
        elif fmt == "pdf":
            cmd = [kicad_cli, "pcb", "export", "pdf",
                   "--layers", "F.Cu,B.Cu,Edge.Cuts,F.SilkS",
                   "--output", str(fab_dir / f"{base}.pdf"), str(pcb_path)]
        elif fmt == "step":
            cmd = [kicad_cli, "pcb", "export", "step",
                   "--output", str(fab_dir / f"{base}.step"), str(pcb_path)]
        else:
            results[fmt] = "SKIPPED: unknown format"
            continue
        proc = _run(cmd, timeout=300 if fmt == "step" else 180)
        results[fmt] = ("ok" if proc.returncode == 0 else
                        f"FAILED exit {proc.returncode}: "
                        f"{(proc.stderr or proc.stdout or '')[-300:]}")

    files = sorted(p for p in fab_dir.iterdir() if p.is_file())
    zip_path = pcb_path.parent / f"{base}_fab.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)

    out = {
        "ok": all(v == "ok" for v in results.values()),
        "formats": results,
        "files": [f.name for f in files],
        "zip": str(zip_path),
    }
    out.update(notes)
    return out
