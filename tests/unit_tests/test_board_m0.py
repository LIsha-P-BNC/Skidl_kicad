# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""M0 tests for the pure-python board pipeline (src/skidl/board/).

Two tiers:
  1. Synthetic fixture (test_data/board_m0/) -- runs anywhere, no KiCad
     libraries or kicad-cli needed.
  2. Real fixture (mcp_circuits/ne555_1hz_led_blinker.net + the installed
     CAD app's .pretty libraries) -- skipped when either is absent, so CI
     without the app still passes.

The separate M0 *gate* -- `kicad-cli pcb drc` parsing the generated
board -- is exercised by the MCP create_pcb tool, not here.
"""

from pathlib import Path

import pytest

from skidl.board import sexp
from skidl.board.model import BoardModel
from skidl.board.adapter.pymodel import PyModelBackend
from skidl.board.pcb_writer import (
    UnresolvedFootprintsError,
    update_project_net_classes,
    write_pcb,
)

THIS_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = THIS_DIR / "test_data" / "board_m0"
FIXTURE_NETLIST = FIXTURES_DIR / "blinker.net"
REPO_ROOT = THIS_DIR.parents[1]
REAL_NETLIST = REPO_ROOT / "mcp_circuits" / "ne555_1hz_led_blinker.net"


@pytest.fixture
def synthetic_fp_dir(monkeypatch):
    """Point footprint resolution at the synthetic Test.pretty fixture."""
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", str(FIXTURES_DIR))


def _populate(netlist_path):
    board = BoardModel(name=netlist_path.stem)
    PyModelBackend().populate(board, netlist_path)
    return board


def test_populate_and_write(synthetic_fp_dir, tmp_path):
    board = _populate(FIXTURE_NETLIST)

    assert len(board.footprints) == 5
    assert set(board.nets) == {"VCC", "GND", "NODE_A", "NODE_B", "OUT", "CTRL"}

    # Every footprint got a distinct position (the M0 placeholder grid,
    # not overlapping at the origin) and a courtyard from its .kicad_mod.
    positions = {(fp.x, fp.y) for fp in board.footprints}
    assert len(positions) == 5, "two footprints ended up at the same x,y"
    assert all(fp.courtyard is not None for fp in board.footprints)

    # Net -> pad wiring: U1 pin 8 (VCC) actually landed on the VCC net,
    # not just present in the netlist's node list.
    u1 = board.footprint("U1")
    pad8 = next(p for p in u1.pads if p.number == "8")
    assert pad8.net == "VCC"

    # CTRL is a single-node net -- the one-pin-net edge case -- and
    # should still resolve cleanly.
    assert board.nets["CTRL"].pad_refs == [("U1", "5")]

    out_path = tmp_path / "blinker_m0_test.kicad_pcb"
    write_pcb(board, out_path)
    text = out_path.read_text(encoding="utf-8")

    assert text.count("(") == text.count(")"), "unbalanced parens in generated .kicad_pcb"
    # Modern KiCad keeps net-class rules in .kicad_pro, never the board file.
    assert "net_class" not in text
    # KiCad 7+ graphic-line syntax on the board outline.
    assert "(stroke (width" in text

    # Re-parse our own output with the same reader used for .kicad_mod --
    # a cheap self-consistency smoke test that doesn't require kicad-cli.
    tree = sexp.read(text)
    assert tree and tree[0][0] == "kicad_pcb"
    footprint_blocks = sexp.find_all(tree[0], "footprint")
    assert len(footprint_blocks) == 5

    net_blocks = [e for e in tree[0] if isinstance(e, list) and e[:1] == ["net"] and len(e) == 3]
    # 6 real nets + the reserved (net 0 "") no-connect declaration.
    assert len(net_blocks) == 7


def test_project_net_classes(synthetic_fp_dir, tmp_path):
    board = _populate(FIXTURE_NETLIST)
    board.net_classes["Power"] = {"width": 0.5, "clearance": 0.2}
    board.nets["VCC"].net_class = "Power"
    board.nets["GND"].net_class = "Power"

    pro_path = tmp_path / "blinker.kicad_pro"
    update_project_net_classes(board, pro_path)

    import json
    pro = json.loads(pro_path.read_text(encoding="utf-8"))
    classes = {c["name"]: c for c in pro["net_settings"]["classes"]}
    assert classes["Power"]["track_width"] == 0.5
    assert classes["Default"]["track_width"] == 0.25
    patterns = {p["pattern"]: p["netclass"] for p in pro["net_settings"]["netclass_patterns"]}
    assert patterns == {"VCC": "Power", "GND": "Power"}

    # Enrichment must preserve unrelated keys of an existing project file.
    pro["schematic"] = {"meta": {"version": 1}}
    pro_path.write_text(json.dumps(pro), encoding="utf-8")
    update_project_net_classes(board, pro_path)
    pro2 = json.loads(pro_path.read_text(encoding="utf-8"))
    assert pro2["schematic"] == {"meta": {"version": 1}}


def test_unresolved_footprint_reports_all_missing(synthetic_fp_dir, tmp_path):
    bad_netlist = tmp_path / "bad_blinker.net"
    bad_netlist.write_text(
        FIXTURE_NETLIST.read_text(encoding="utf-8")
        .replace('"Test:R_0805"', '"Nonexistent:DoesNotExist"', 1)  # break R1 only
    , encoding="utf-8")

    with pytest.raises(UnresolvedFootprintsError) as excinfo:
        _populate(bad_netlist)
    assert "R1" in excinfo.value.missing
    assert len(excinfo.value.missing) == 1  # only R1 broke, not R2/C1 (same real footprint)


def _real_footprint_dir():
    try:
        from skidl.anvil import anvil_libs
        return anvil_libs.FOOTPRINTS
    except Exception:
        return None


@pytest.mark.skipif(
    not REAL_NETLIST.is_file() or not _real_footprint_dir(),
    reason="real NE555 netlist or installed CAD-app footprint libraries unavailable",
)
def test_real_ne555_board(monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD9_FOOTPRINT_DIR", _real_footprint_dir())
    board = _populate(REAL_NETLIST)

    assert len(board.footprints) >= 5
    # Every netlist pin landed on a real pad of a real footprint.
    for net in board.nets.values():
        for ref, pin in net.pad_refs:
            fp = board.footprint(ref)
            pad = next((p for p in fp.pads if p.number == pin), None)
            assert pad is not None, f"{ref}.{pin} has no matching pad in {fp.lib_id}"
            assert pad.net == net.name

    out_path = tmp_path / "ne555_m0.kicad_pcb"
    write_pcb(board, out_path)
    text = out_path.read_text(encoding="utf-8")
    assert text.count("(") == text.count(")")
    tree = sexp.read(text)
    assert len(sexp.find_all(tree[0], "footprint")) == len(board.footprints)
