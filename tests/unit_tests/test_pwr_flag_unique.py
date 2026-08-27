"""
tests/unit_tests/test_pwr_flag_unique.py

PWR_FLAG auto-annotation must number #FLG references GLOBALLY across every
sheet of a hierarchical project. #FLG refs are annotated across the whole
hierarchy, so if each sheet restarts its counter at #FLG01 two sheets stamp
their first flag #FLG01 and KiCad ERC reports "Duplicate items #FLG01"
(Not annotated). Regression for add_pwr_flags.add() cross-sheet numbering.

Self-contained synthetic fixtures -- no app, no full build, no single-project
reliance: crafted minimal sheet files, one distinct undriven rail per sheet.
"""

import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "skidl" / "anvil"))

import add_pwr_flags as A  # noqa: E402


def _sheet(rail, x=100.0, y=100.0):
    """Minimal sheet holding ONE power symbol for `rail` plus its stub wire,
    so add_pwr_flags recognises an undriven rail needing a flag here."""
    return f'''(kicad_sch (version 20211123) (generator test)
  (lib_symbols
    (symbol "power:{rail}" (power) (pin_names (offset 0)) (in_bom no) (on_board yes)
      (property "Reference" "#PWR" (at 0 0 0))
      (property "Value" "{rail}" (at 0 0 0))
      (symbol "{rail}_0_1" (pin power_in line (at 0 0 90) (length 0) (name "{rail}") (number "1"))))
  )
  (symbol (lib_id "power:{rail}") (at {x} {y} 0) (unit 1)
    (property "Reference" "#PWR01" (at {x} {y} 0))
    (property "Value" "{rail}" (at {x} {y} 0))
    (instances (project "t" (path "/abc" (reference "#PWR01") (unit 1))))
  )
  (wire (pts (xy {x} {y}) (xy {x} {y - 7.62})))
)
'''


def _refs_by_file(base):
    out = {}
    for f in Path(base).parent.glob(Path(base).name + "*.anvil_sch"):
        found = set(re.findall(r'"(#FLG[0-9]+)"', f.read_text(encoding="utf-8")))
        for r in found:
            out.setdefault(r, []).append(f.name)
    return out


def test_flg_unique_across_sheets(tmp_path):
    base = str(tmp_path / "proj")
    # top sheet + 2 sub-sheets, each a DISTINCT undriven rail -> each needs a flag
    (tmp_path / "proj.anvil_sch").write_text(_sheet("+12V", 100, 100), encoding="utf-8")
    (tmp_path / "proj_a.anvil_sch").write_text(_sheet("+5V", 120, 120), encoding="utf-8")
    (tmp_path / "proj_b.anvil_sch").write_text(_sheet("+3V3", 140, 140), encoding="utf-8")
    (tmp_path / "proj.net").write_text(
        '(export (version "E")\n (nets\n'
        '  (net (code 1) (name "+12V") (node (ref R1) (pin 1)))\n'
        '  (net (code 2) (name "+5V")  (node (ref R2) (pin 1)))\n'
        '  (net (code 3) (name "+3V3") (node (ref R3) (pin 1)))\n'
        ' ))\n',
        encoding="utf-8",
    )

    added = A.add(base)
    assert added == 3, f"expected 3 flags, got {added}"

    byfile = _refs_by_file(base)
    # every #FLG ref must live in exactly ONE sheet file (no cross-sheet clash)
    cross = {r: fs for r, fs in byfile.items() if len(fs) > 1}
    assert not cross, f"cross-sheet duplicate #FLG refs: {cross}"
    # and the three flags must be three DISTINCT references
    assert len(byfile) == 3, f"expected 3 distinct #FLG refs, got {sorted(byfile)}"
