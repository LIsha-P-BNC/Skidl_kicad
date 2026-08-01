"""
src/skidl/board/route/heal.py

Post-route sliver healing. FreeRouting's SES output occasionally leaves
degenerate micro-segments (0.0001-0.1 mm) and short cross-net fragments
at dense junctions; KiCad's DRC then reports clearance/short errors on
copper that carries no real connectivity (measured on the 150-part
tracker: 18 errors, all from fragments < 0.8 mm at two junctions).

Strategy -- DRC is the oracle, segments are only ever REMOVED (never
drawn; hand-drawn bridges cross other nets, measured twice):
  1. Drop degenerate segments (< 0.05 mm) outright.
  2. DRC. For every clearance/short error whose items include a track
     fragment shorter than `max_frag`, remove that fragment.
  3. DRC again. If errors are gone and no NEW unconnected pairs
     appeared, keep the result; otherwise restore the last removal and
     report honestly.

All file edits are span-based (parse the exact segment blocks, splice
them out) -- no regex substitution on the raw text.
"""

from __future__ import annotations
import json
import math
import re
from pathlib import Path

_SEG = re.compile(
    r"\(segment\s*\(start ([-\d.]+) ([-\d.]+)\)\s*\(end ([-\d.]+) ([-\d.]+)\)"
    r"\s*\(width ([-\d.]+)\)\s*\(layer \"([^\"]+)\"\)\s*\(net (\"[^\"]*\"|\d+)\)",
    re.S)


def _segments(txt):
    """All segment blocks: (span_start, span_end, x1, y1, x2, y2, length,
    layer, net_token). Span covers the whole '(segment ...)' block
    including the leading tab and trailing newline when present."""
    out = []
    for m in _SEG.finditer(txt):
        # walk to block end from the '(segment'
        start = m.start()
        depth, j = 0, start
        while j < len(txt):
            c = txt[j]
            if c == '"':
                j = txt.index('"', j + 1)
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        end = j + 1
        # absorb leading whitespace and trailing newline
        s = start
        while s > 0 and txt[s - 1] in " \t":
            s -= 1
        if end < len(txt) and txt[end] == "\n":
            end += 1
        x1, y1, x2, y2 = (float(m.group(i)) for i in range(1, 5))
        out.append({"s": s, "e": end, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "len": math.hypot(x2 - x1, y2 - y1),
                    "layer": m.group(6), "net": m.group(7)})
    return out


def _remove(txt, spans):
    """Splice the given (s, e) spans out of txt (non-overlapping)."""
    for s, e in sorted(spans, reverse=True):
        txt = txt[:s] + txt[e:]
    return txt


def _drc_report(pcb_path: Path, kicad_cli: str) -> dict:
    from skidl.board.pcb_job import _drc
    g = _drc(pcb_path, kicad_cli, refill=True)
    rep_path = pcb_path.with_suffix(".drc.json")
    rep = {}
    if rep_path.is_file():
        rep = json.loads(rep_path.read_text(encoding="utf-8", errors="replace"))
    return {"gate": g, "raw": rep}


_ITEM = re.compile(r"Track \[([^\]]+)\] on ([\w.]+), length ([\d.]+) mm")


def _shortest_fragments(rep, art=("malformed_courtyard", "lib_footprint_issues")):
    """For each DRC error, the SINGLE shortest involved track fragment:
    (net, layer, length, x, y). Removing only the shortest item of each
    pair preserves the longer copper that actually carries the net
    (measured: removing every nearby fragment fixed 18 errors but broke
    6 connections -- the long fragments were real)."""
    out = []
    for v in rep.get("violations", []):
        if v.get("severity") != "error" or v.get("type") in art:
            continue
        frags = []
        for it in v.get("items", []):
            m = _ITEM.match(it.get("description", ""))
            pos = it.get("pos") or {}
            if m and "x" in pos:
                frags.append((float(m.group(3)), m.group(1), m.group(2),
                              pos["x"], pos["y"]))
        if frags:
            frags.sort()
            out.append(frags[0])
    return out


def heal_slivers(pcb_path, kicad_cli, max_frag=1.0, radius=0.6,
                 max_rounds=12) -> dict:
    """Remove sliver fragments implicated in DRC errors. Returns a
    report; the board file is modified in place only when the result is
    strictly better (fewer errors, no new unconnected)."""
    pcb_path = Path(pcb_path)
    original = pcb_path.read_text(encoding="utf-8", errors="replace")

    base = _drc_report(pcb_path, kicad_cli)
    err0 = base["gate"].get("drc_errors")
    unc0 = base["gate"].get("drc_unconnected")
    if not err0:
        return {"healed": False, "reason": "no errors to heal",
                "drc_errors": err0, "drc_unconnected": unc0}

    # NOTE: no blanket removal pass -- even a 0.0001 mm segment can be
    # junction glue between rounding-split nodes (measured: bulk removal
    # fixed 18 errors but broke 6 connections). Fragments are removed
    # ONE at a time with a full DRC after each; a removal is kept only
    # when errors strictly drop and no new unconnected appears.
    txt = original
    removed_total = 0
    err_cur, unc_cur, report = err0, unc0, base["raw"]
    tried = set()

    for _ in range(max_rounds):
        if not err_cur:
            break
        cands = [(ln, net, layer, px, py)
                 for (ln, net, layer, px, py) in _shortest_fragments(report)
                 if ln < max_frag and (round(px, 3), round(py, 3), net) not in tried]
        if not cands:
            break
        length, net, layer, px, py = cands[0]
        tried.add((round(px, 3), round(py, 3), net))
        segs = _segments(txt)
        best = None
        for s in segs:
            if s["layer"] != layer or s["net"].strip('"') != net:
                continue
            if abs(s["len"] - length) > 0.02:
                continue
            mx, my = (s["x1"] + s["x2"]) / 2, (s["y1"] + s["y2"]) / 2
            d = math.hypot(mx - px, my - py)
            if d <= radius + length and (best is None or d < best[0]):
                best = (d, s["s"], s["e"])
        if not best:
            continue
        trial = _remove(txt, [(best[1], best[2])])
        pcb_path.write_text(trial, encoding="utf-8")
        after = _drc_report(pcb_path, kicad_cli)
        err1 = after["gate"].get("drc_errors")
        unc1 = after["gate"].get("drc_unconnected")
        if err1 is not None and err1 < err_cur and (unc1 or 0) <= (unc0 or 0):
            txt, err_cur, unc_cur, report = trial, err1, unc1, after["raw"]
            removed_total += 1
        # else: txt unchanged; next candidate (file rewritten below anyway)

    if removed_total and err_cur < err0:
        pcb_path.write_text(txt, encoding="utf-8")
        _drc_report(pcb_path, kicad_cli)   # report matches final board
        return {"healed": True, "removed_segments": removed_total,
                "drc_errors_before": err0, "drc_errors": err_cur,
                "drc_unconnected": unc_cur}

    pcb_path.write_text(original, encoding="utf-8")
    _drc_report(pcb_path, kicad_cli)
    return {"healed": False,
            "reason": "no single-fragment removal improved DRC -- restored original",
            "drc_errors": err0, "drc_unconnected": unc0}
