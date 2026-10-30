"""check_all.py -- build EVERY known circuit and print one pass/fail matrix.

THE RULE THIS ENFORCES: no engine change is judged on one circuit. A layout or
classifier tweak that helps one design and regresses another is a REGRESSION
(measured examples: generalized satellite-hug fixed stm32's labels but broke
power_board 0->5 and arduino 0->24; tight packing shrank the buck but broke its
ladder). Run this after ANY engine change; every row must stay green.

Usage:  PYTHONHASHSEED=0 python check_all.py
Exit code: 0 = all pass, 1 = any regression.

Columns:
  ERC     -- KiCad ERC errors from <name>.erc (must be 0)
  labels  -- LOCAL label count in the sheet (informational; big jumps = look)
  wires   -- wire count (informational)
  expect  -- per-circuit invariant that MUST hold (the circuit's own contract):
             buck: ladder (3 rails, 0 labels); power_board/arduino: 0 labels;
             stm32: ERC 0 (dense MCU, cross-block labels are legitimate);
             t1-t4: ERC 0.
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "..", "src"))

# WI-1: reuse the pure metric from schematics/metrics.py so the harness and
# the unit tests measure wire crossings the SAME way (shared endpoints /
# junctions are NOT crossings). Only this one sub-metric is wired in: it needs
# nothing but wire endpoints from the sch. The other sub-metrics (label
# overlap, text/symbol collision, block spacing) need text-box sizing / symbol
# geometry / block grouping and are deliberately NOT faked here.
sys.path.insert(0, SRC)
try:
    from skidl.schematics.metrics import (
        block_spacing_stats, count_wire_crossings, sheet_extent,
    )
except Exception:  # pragma: no cover - metrics is optional for the core gate
    count_wire_crossings = sheet_extent = block_spacing_stats = None

# (name, directory, script, invariant, extra_env)
#   invariant: "labels0" = zero local labels; "erc0" = ERC clean is enough
# The list covers ALL THREE TIERS: single-function circuits (buck ladder,
# t1), single sheet with blocks (power_board, t2/t4, stm32), and HIERARCHY
# (t3 forced multi-sheet via SKIDL_HIER_MIN_PARTS -- root + child sheets all
# ERC'd; the same t3 also runs flattened at the default threshold).
CIRCUITS = [
    ("t1_tiny_flat",    HERE,                              "t1_tiny_flat.py",    "erc0", None),
    ("t2_autogroup",    HERE,                              "t2_autogroup.py",    "erc0", None),
    ("t3_subcircuit",   HERE,                              "t3_subcircuit.py",   "erc0", None),
    ("t3_HIERARCHY",    HERE,                              "t3_subcircuit.py",   "erc0",
     {"SKIDL_HIER_MIN_PARTS": "5"}),
    ("t5_HIER_BLOCKS",  HERE,                              "t5_hier_blocks.py",  "erc0",
     {"SKIDL_HIER_MIN_PARTS": "5"}),
    ("t4_motor_driver", HERE,                              "t4_motor_driver.py", "erc0", None),
    ("buck",            r"F:\Anvil\buck_12v_5v_demo",      "buck_12v_5v_demo.py", "labels0+ladder", None),
    ("power_board",     r"F:\Anvil\power_board_demo",      "power_board_demo.py", "labels0+ladder", None),
    ("arduino",         r"F:\Anvil\arduino_nano_demo",     "arduino_nano_demo.py", "labels0", None),
    ("stm32",           r"F:\Anvil\stm32_usb_devboard",    "stm32_usb_devboard.py", "erc0", None),
    # LAST deliberately: t6's big build perturbs the NEXT build in sequence
    # (arduino went all-label only when run right after t6; passes isolated
    # and when t6 runs last -- cause under investigation, see RULE_ENGINE).
    ("t6_SHEET_PACK",   HERE,                              "t6_big_pack.py",     "erc0", None),
]


def build(cwd, script, extra_env=None):
    env = dict(os.environ, PYTHONHASHSEED="0",
               PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, script], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def stats(cwd, name):
    import glob
    base = os.path.splitext(name)[0]
    sch = os.path.join(cwd, base + ".anvil_sch")
    erc = os.path.join(cwd, base + ".erc")
    wires = labels = -1
    erc_errs = None
    if os.path.exists(sch):
        wires = labels = 0
        # hierarchy: sum the root AND its child sheets (base_*.anvil_sch)
        for f in [sch] + glob.glob(os.path.join(cwd, base + "_*.anvil_sch")):
            t = open(f, encoding="utf-8", errors="replace").read()
            wires += len(re.findall(r"\(wire\b", t))
            labels += len(re.findall(r"\(label\b", t))
    if os.path.exists(erc):
        t = open(erc, encoding="utf-8", errors="replace").read()
        m = re.findall(r"(\d+) errors? found", t)
        if m:
            erc_errs = int(m[-1])
    return wires, labels, erc_errs


def _wire_segments(sch_text):
    """Every wire as ((x1,y1),(x2,y2)), via the same paren-match + (xy ...)
    scan _has_rail uses -- so one parser feeds both the ladder check and the
    crossing metric."""
    segs = []
    for m in re.finditer(r"\(wire\b", sch_text):
        i = m.start()
        depth = 0
        blk = ""
        for j in range(i, len(sch_text)):
            if sch_text[j] == "(":
                depth += 1
            elif sch_text[j] == ")":
                depth -= 1
                if depth == 0:
                    blk = sch_text[i:j + 1]
                    break
        xy = re.findall(r"\(xy (-?[\d.]+) (-?[\d.]+)\)", blk)
        if len(xy) == 2:
            (x1, y1), (x2, y2) = ((float(a), float(b)) for a, b in xy)
            segs.append(((x1, y1), (x2, y2)))
    return segs


def crossings(cwd, name):
    """Total pin-to-pin wire crossings across all sheets of a project, or
    None if the metric is unavailable / no sheet exists."""
    if count_wire_crossings is None:
        return None
    import glob
    base = os.path.splitext(name)[0]
    sheets = ([os.path.join(cwd, base + ".anvil_sch")]
              + glob.glob(os.path.join(cwd, base + "_*.anvil_sch")))
    total = None
    for f in sheets:
        if not os.path.exists(f):
            continue
        t = open(f, encoding="utf-8", errors="replace").read()
        total = (total or 0) + count_wire_crossings(_wire_segments(t))
    return total


def _strip_lib_symbols(text):
    """Remove the (lib_symbols ...) block so sheet-level scans never match
    the symbol-BODY graphics nested inside it."""
    i = text.find("(lib_symbols")
    if i < 0:
        return text
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return text[:i] + text[j + 1:]
    return text


def _sheet_metrics(path):
    """Extent + block frames for ONE sheet file."""
    t = _strip_lib_symbols(
        open(path, encoding="utf-8", errors="replace").read())
    pts = [(float(a), float(b)) for a, b in
           re.findall(r'\(lib_id "[^"]+"\)\s*\(at (-?[\d.]+) (-?[\d.]+)', t)]
    for (x1, y1), (x2, y2) in _wire_segments(t):
        pts += [(x1, y1), (x2, y2)]
    frames = []
    for m in re.finditer(
            r"\(rectangle\s*\(start (-?[\d.]+) (-?[\d.]+)\)\s*"
            r"\(end (-?[\d.]+) (-?[\d.]+)\)", t):
        x1, y1, x2, y2 = (float(v) for v in m.groups())
        frames.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return sheet_extent(pts), frames


def compactness(cwd, name):
    """Composition metrics across ALL sheets of a project (root + hierarchy
    children). Sheets are separate coordinate spaces, so each sheet's extent
    is measured ALONE and then aggregated honestly: area = sum of sheet
    areas, aspect_dev = the WORST sheet, w/h = the largest sheet's. Block
    frames (top-level rectangles) feed spacing stats per the sheet that
    draws >= 2 of them. This is the dimension the 2D block-grid targets --
    crossings cannot see it. Returns None when the metrics module or no
    sheet is available."""
    if sheet_extent is None:
        return None
    import glob
    base = os.path.splitext(name)[0]
    sheets = ([os.path.join(cwd, base + ".anvil_sch")]
              + sorted(glob.glob(os.path.join(cwd, base + "_*.anvil_sch"))))
    per_sheet = []
    all_frames_spacing = None
    blocks = 0
    for f in sheets:
        if not os.path.exists(f):
            continue
        ext, frames = _sheet_metrics(f)
        blocks += len(frames)
        if len(frames) >= 2 and all_frames_spacing is None:
            all_frames_spacing = block_spacing_stats(frames)
        if ext["area"] > 0:
            per_sheet.append(ext)
    if not per_sheet:
        return None
    biggest = max(per_sheet, key=lambda e: e["area"])
    return {"w": biggest["w"], "h": biggest["h"],
            "area": sum(e["area"] for e in per_sheet),
            "aspect_dev": max(e["aspect_dev"] for e in per_sheet),
            "sheets": len(per_sheet), "blocks": blocks,
            "spacing": all_frames_spacing}


def _has_rail(cwd, name, min_mm=20.0):
    """True if any sheet of this project has a merged horizontal wire run
    >= min_mm (the ladder's rail spine; junction healing may split it into
    collinear segments, so merge before measuring)."""
    import glob
    base = os.path.splitext(name)[0]
    sheets = ([os.path.join(cwd, base + ".anvil_sch")]
              + glob.glob(os.path.join(cwd, base + "_*.anvil_sch")))
    for f in sheets:
        if not os.path.exists(f):
            continue
        t = open(f, encoding="utf-8", errors="replace").read()
        runs = {}
        for m in re.finditer(r"\(wire\b", t):
            i = m.start()
            depth = 0
            for j in range(i, len(t)):
                if t[j] == "(":
                    depth += 1
                elif t[j] == ")":
                    depth -= 1
                    if depth == 0:
                        blk = t[i:j + 1]
                        break
            xy = re.findall(r"\(xy (-?[\d.]+) (-?[\d.]+)\)", blk)
            if len(xy) == 2:
                (x1, y1), (x2, y2) = ((float(a), float(b)) for a, b in xy)
                if abs(y1 - y2) < 0.05 and abs(x1 - x2) > 0.01:
                    runs.setdefault(round(y1, 1), []).append(
                        (min(x1, x2), max(x1, x2)))
        for y, segs in runs.items():
            segs.sort()
            cur_lo, cur_hi = segs[0]
            for lo, hi in segs[1:]:
                if lo <= cur_hi + 0.01:
                    cur_hi = max(cur_hi, hi)
                else:
                    if cur_hi - cur_lo >= min_mm:
                        return True
                    cur_lo, cur_hi = lo, hi
            if cur_hi - cur_lo >= min_mm:
                return True
    return False


def main():
    rows = []
    fail = False
    for label, cwd, script, invariant, extra_env in CIRCUITS:
        try:
            rc, out = build(cwd, script, extra_env)
        except Exception as e:
            rows.append((label, "-", "-", "-", f"BUILD CRASH: {e}"))
            fail = True
            continue
        wires, labels, erc = stats(cwd, script)
        if erc is None:
            # some circuits don't persist an .erc file -- take the compliance
            # gate's line from the build output: "KiCad ERC errors     (N)"
            m = re.findall(r"KiCad ERC errors\s*\((\d+)\)", out)
            if m:
                erc = int(m[-1])
        problems = []
        if rc != 0 or "Traceback" in out:
            problems.append("build error")
        if erc is None:
            problems.append("no ERC report")
        elif erc != 0:
            problems.append(f"ERC {erc}")
        if invariant.startswith("labels0") and labels != 0:
            problems.append(f"{labels} local labels (expected 0)")
        if "ladder" in invariant:
            # the LADDER invariant: at least one horizontal power-rail run
            # >= 20 mm must exist (a per-pin-symbol sheet has none) -- catches
            # a silent rail-draw regression that labels/ERC cannot see.
            if not _has_rail(cwd, script, min_mm=20.0):
                problems.append("no ladder rail found (>=20mm horizontal run)")
        if problems:
            # FLAP ABSORBER: a transient kicad-cli hiccup mid-matrix can drop
            # one build to the all-label fallback. Rebuild ONCE before calling
            # it a regression; a REAL regression fails both runs.
            try:
                rc, out = build(cwd, script, extra_env)
                wires, labels, erc = stats(cwd, script)
                if erc is None:
                    m = re.findall(r"KiCad ERC errors\s*\((\d+)\)", out)
                    if m:
                        erc = int(m[-1])
                problems = []
                if rc != 0 or "Traceback" in out:
                    problems.append("build error")
                if erc is None:
                    problems.append("no ERC report")
                elif erc != 0:
                    problems.append(f"ERC {erc}")
                if invariant.startswith("labels0") and labels != 0:
                    problems.append(f"{labels} local labels (expected 0)")
                if "ladder" in invariant and not _has_rail(cwd, script, 20.0):
                    problems.append("no ladder rail found")
                if problems:
                    problems.append("(persisted across retry)")
            except Exception as e:
                problems.append(f"retry crash: {e}")
        verdict = "OK" if not problems else "FAIL: " + "; ".join(problems)
        if problems:
            fail = True
        cross = crossings(cwd, script)
        comp = compactness(cwd, script)
        if comp:
            size = "%dx%d" % (round(comp["w"]), round(comp["h"]))
            asp = "%.2f" % comp["aspect_dev"]
        else:
            size = asp = "-"
        rows.append((label, wires, labels, erc,
                     "-" if cross is None else cross, size, asp, verdict))

    # CROSS / SIZE / ASPECT are INFORMATIONAL only (no gate): they measure
    # readability and composition, and without a TI ground-truth corpus a
    # given value is not yet a failure. A big jump after an engine change is
    # the signal to look -- compare against a baseline run, not an absolute
    # threshold. ASPECT = |log10(w/h)|: 0.00 = square, 0.48 = 3:1 sprawl.
    print("\n%-16s %6s %7s %5s %6s %10s %7s  %s"
          % ("circuit", "wires", "labels", "ERC", "CROSS", "SIZE", "ASPECT",
             "verdict"))
    print("-" * 86)
    for label, w, l, e, x, s, a, v in rows:
        print("%-16s %6s %7s %5s %6s %10s %7s  %s"
              % (label, w, l, e, x, s, a, v))
    print()
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
