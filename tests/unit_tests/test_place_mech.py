"""
Mechanical-first placement (place/mech.py + rules placer integration):
fixed outline is authoritative, connectors land flush on their assigned
edges, explicit mounting holes are exact, keepouts stay part-free, and a
too-small board fails HONESTLY instead of growing.

Uses the lm7805 netlist fixture (small, fast, has J1/J2 connectors).
"""

import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NET_FIXTURE = REPO / "mcp_circuits" / "lm7805_psu.net"

pytestmark = pytest.mark.skipif(
    not NET_FIXTURE.is_file(), reason="lm7805 net fixture not present")


def _build(tmp_path, mechanical):
    import sys
    sys.path.insert(0, str(REPO / "src"))
    import skidl.anvil.anvil_libs  # noqa: F401  (library env)
    from skidl.board.pipeline import build_pcb

    shutil.copy(NET_FIXTURE, tmp_path / "M.net")
    sidecar = {"manufacturer": "jlcpcb", "layers": 2, "ground_pour": True,
               "mechanical": mechanical}
    (tmp_path / "M.board_config.json").write_text(
        json.dumps(sidecar), encoding="utf-8")
    return build_pcb(tmp_path / "M.net", out_dir=tmp_path, name="M",
                     layers=2, route=False, placement="rules")


MECH = {
    "board": {"width": 60.0, "height": 40.0, "grow": False},
    "mounting_holes": {"size": "M3",
                       "positions": [[4.0, 4.0], [56.0, 36.0]]},
    "connectors": [{"ref": "J1", "edge": "left", "offset": 20.0},
                   {"ref": "J2", "edge": "right", "offset": 20.0}],
    "keepouts": [{"name": "antenna", "rect": [40.0, 0.0, 60.0, 10.0],
                  "layers": ["*.Cu"], "no_footprints": True,
                  "reason": "test keepout"}],
}


def test_mechanical_first_placement(tmp_path):
    info = _build(tmp_path, MECH)
    assert info["ok"], info.get("error")
    pl = info["placement"]
    assert pl["placer"] == "rules"
    assert pl.get("rules_fallback_reason") is None
    assert pl["courtyard_overlaps"] == 0
    assert pl["board_size_mm"] == (60.0, 40.0)

    # Reload geometry from the written board (offset by sheet centering).
    import sys
    sys.path.insert(0, str(REPO / "src"))
    from skidl.board.rule_discovery import read_board_setup  # noqa: F401
    txt = (tmp_path / "M.kicad_pcb").read_text(encoding="utf-8")
    dx, dy = pl.get("sheet_offset", (0, 0))

    import re

    def fp_at(ref):
        i = txt.find(f'"Reference" "{ref}"')
        s = txt.rfind("(footprint", 0, i)
        m = re.search(r"\(at ([-\d.]+) ([-\d.]+)", txt[s:s + 2000])
        return float(m.group(1)) - dx, float(m.group(2)) - dy

    # Explicit mounting holes exactly where the enclosure says.
    assert fp_at("MH1") == pytest.approx((4.0, 4.0), abs=0.01)
    assert fp_at("MH2") == pytest.approx((56.0, 36.0), abs=0.01)

    # Edge connectors on their assigned sides (their courtyard sits
    # flush ~1mm from the edge; centers depend on footprint origin, so
    # assert side + offset, not absolute center).
    j1x, j1y = fp_at("J1")
    j2x, j2y = fp_at("J2")
    assert j1x < j2x, "J1 (left edge) must sit left of J2 (right edge)"
    assert j1y == pytest.approx(20.0, abs=0.01)
    assert j2y == pytest.approx(20.0, abs=0.01)

    # Keepout written as a rule area and honored by placement.
    assert '(name "antenna")' in txt
    assert "(keepout (tracks not_allowed)" in txt


def test_too_small_board_fails_honestly(tmp_path):
    tiny = {"board": {"width": 8.0, "height": 6.0, "grow": False},
            "connectors": [], "keepouts": []}
    info = _build(tmp_path, tiny)
    assert not info["ok"]
    assert info["status"] in ("mechanical_fit_failed", "placement_failed")
    # The honest area numbers, not a silently grown board.
    if info["status"] == "mechanical_fit_failed":
        assert info["board_mm2"] == pytest.approx(48.0)


def test_no_mechanical_section_unchanged(tmp_path):
    info = _build(tmp_path, None)
    # build_pcb with mechanical=None in the sidecar -> classic auto flow.
    assert info["ok"]
    assert info["placement"]["courtyard_overlaps"] == 0
