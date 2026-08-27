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
    fab_dir.mkdir(exist_ok=True)
    formats = formats or ["gerbers", "drill", "pos"]
    prefs = _board_plot_prefs(pcb_path)

    results = {}
    for fmt in formats:
        if fmt == "gerbers":
            cmd = [kicad_cli, "pcb", "export", "gerbers",
                   "--output", str(fab_dir) + "/"]
            if prefs["has_plot_params"]:
                # the user's own Plot dialog settings (layers, format
                # options, origin) exactly as saved in the board
                cmd.append("--board-plot-params")
            cmd.append(str(pcb_path))
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

    return {
        "ok": all(v == "ok" for v in results.values()),
        "formats": results,
        "files": [f.name for f in files],
        "zip": str(zip_path),
    }
