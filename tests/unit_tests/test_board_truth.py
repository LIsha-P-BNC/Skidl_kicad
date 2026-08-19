"""
tests/unit_tests/test_board_truth.py

Saved-KiCad-files-as-truth architecture tests: fingerprint arbitration,
unified READ (get_board_setup extensions + resolver agreement), pending
changes, state_hash vs setup_hash. Self-contained synthetic fixtures.
"""

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.board import board_setup as bs
from skidl.board.rule_discovery import resolve_board_config


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _pcb(tmp, base, generator="skidl_board", copper=("F.Cu", "B.Cu"),
         thickness=1.6, mask=None, edge=None):
    """Minimal .kicad_pcb with a layer table, general block, setup block
    and (optionally) a rectangular Edge.Cuts outline."""
    layer_rows = "".join(f'\n\t\t({i} "{n}" signal)'
                         for i, n in enumerate(copper))
    edge_txt = ""
    if edge:
        x1, y1, x2, y2 = edge
        for (a, b, c, d) in [(x1, y1, x2, y1), (x2, y1, x2, y2),
                             (x2, y2, x1, y2), (x1, y2, x1, y1)]:
            edge_txt += (f'\n\t(gr_line\n\t\t(start {a} {b})\n\t\t(end {c} {d})'
                         f'\n\t\t(layer "Edge.Cuts")\n\t)')
    mask_txt = f'\n\t\t(pad_to_mask_clearance {mask})' if mask is not None else ""
    text = (f'(kicad_pcb\n\t(version 20240108)\n\t(generator "{generator}")\n'
            f'\t(general\n\t\t(thickness {thickness})\n\t)\n'
            f'\t(paper "A4")\n'
            f'\t(layers{layer_rows}\n\t)\n'
            f'\t(setup{mask_txt}\n\t\t(pad_to_paste_clearance 0)\n\t)\n'
            f'{edge_txt}\n)\n')
    (tmp / f"{base}.kicad_pcb").write_text(text, encoding="utf-8")
    return text


def _sidecar(tmp, base, **kv):
    (tmp / f"{base}.board_config.json").write_text(
        json.dumps(kv, indent=2), encoding="utf-8")


def _pcb_extra(tmp, base, extra, **kw):
    """Fixture board with extra content blocks appended before the close."""
    text = _pcb(tmp, base, **kw)
    text = text.rstrip()[:-1] + extra + "\n)\n"
    (tmp / f"{base}.kicad_pcb").write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------------------
# board_edit_status: the ONE arbitration
# ---------------------------------------------------------------------------

def test_edit_status_matrix(tmp_path):
    base = "arb"
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is None

    _pcb(tmp_path, base)                        # our generator, no fingerprint
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is True

    bs.stamp_board_fingerprint(base, tmp_path)  # stamped -> machine
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is True

    # user edit: content changes after the stamp (even with our generator)
    p = tmp_path / f"{base}.kicad_pcb"
    p.write_text(p.read_text(encoding="utf-8").replace("1.6", "1.2"),
                 encoding="utf-8")
    st = bs.board_edit_status(base, tmp_path)
    assert st["machine_generated"] is False

    # no fingerprint + foreign generator -> user-saved
    (tmp_path / f"{base}.board_fingerprint.json").unlink()
    _pcb(tmp_path, base, generator="pcbnew")
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is False


def test_read_saved_state(tmp_path):
    base = "sav"
    _pcb(tmp_path, base, copper=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"),
         thickness=1.2, mask=0.05, edge=(0, 0, 100, 60))
    s = bs.read_saved_state(base, tmp_path)
    assert s["exists"] and s["generator"] == "skidl_board"
    assert s["layers"] == 4
    assert s["thickness"] == 1.2
    assert s["pad_to_mask_clearance"] == 0.05
    assert s["edge_cuts"]["width_mm"] == 100.0
    assert s["edge_cuts"]["height_mm"] == 60.0


# ---------------------------------------------------------------------------
# pending changes + hashes
# ---------------------------------------------------------------------------

def test_pending_changes_on_machine_board(tmp_path):
    base = "pend"
    _pcb(tmp_path, base, copper=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, layers=2, thickness=1.2,
             _sources={"layers": "user", "thickness": "user"})
    pend = bs.compute_pending_changes(base, tmp_path)
    assert pend["layers"] == {"requested": 2, "saved": 4,
                              "requested_via": "user",
                              "applies_when": "next create_pcb"}
    assert pend["thickness"]["requested"] == 1.2


def test_no_pending_on_user_edited_board(tmp_path):
    base = "pend2"
    _pcb(tmp_path, base, generator="pcbnew",
         copper=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    _sidecar(tmp_path, base, layers=2)
    # user-edited: the saved file WINS, nothing is 'pending'
    assert bs.compute_pending_changes(base, tmp_path) == {}


def test_state_hash_covers_sidecar_setup_hash_does_not(tmp_path):
    base = "hash"
    _pcb(tmp_path, base)
    _sidecar(tmp_path, base, manufacturer="jlcpcb")
    s1 = bs.get_board_setup(base, tmp_path)
    _sidecar(tmp_path, base, manufacturer="pcbway")     # sidecar-only change
    s2 = bs.get_board_setup(base, tmp_path)
    assert s1["setup_hash"] == s2["setup_hash"]
    assert s1["state_hash"] != s2["state_hash"]


# ---------------------------------------------------------------------------
# unified READ: get_board_setup extensions + resolver agreement
# ---------------------------------------------------------------------------

def test_get_board_setup_exposes_everything(tmp_path):
    base = "gbs"
    _pcb(tmp_path, base, edge=(0, 0, 80, 50))
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, manufacturer="jlcpcb", currents={"VIN": 1.5},
             engine_overrides={"layers": {"was": 2, "now": 4}},
             _sources={"manufacturer": "argument"})
    s = bs.get_board_setup(base, tmp_path)
    assert s["sidecar_meta"]["manufacturer"] == {"value": "jlcpcb",
                                                 "source": "argument"}
    assert s["sidecar_meta"]["currents"]["value"] == {"VIN": 1.5}
    assert s["consumed"]["board_outline"]["value"]["width_mm"] == 80.0
    assert s["engine_overrides"]["layers"]["now"] == 4
    assert s["edit_status"]["machine_generated"] is True
    assert "drc_severities" in s["consumed"]


def test_readers_agree_after_update_regression(tmp_path):
    """F-READ1 regression: sidecar asks 2 while the machine board has 4 --
    BOTH readers must tell the same story (saved=4, next build=2)."""
    base = "agree"
    _pcb(tmp_path, base, copper=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, layers=2, _sources={"layers": "user"})

    s = bs.get_board_setup(base, tmp_path)
    assert s["consumed"]["layers"]["value"] == 4        # saved file truth
    assert s["pending_changes"]["layers"]["requested"] == 2

    rc = resolve_board_config(base, tmp_path)
    assert rc["layers"]["value"] == 2                   # next build applies 2
    assert rc["layers"]["pending"] is True
    assert rc["layers"]["source"] == "user"


def test_resolver_user_edited_board_wins(tmp_path):
    """User saved a 2-layer 100x60 board in KiCad; sidecar still says 4 --
    the SAVED file wins, confirmed, no pending."""
    base = "uwin"
    _pcb(tmp_path, base, generator="pcbnew", copper=("F.Cu", "B.Cu"),
         thickness=1.0, edge=(0, 0, 100, 60))
    _sidecar(tmp_path, base, layers=4, thickness=1.6,
             board_width=80.0, board_height=50.0,
             _sources={"layers": "argument", "board_width": "argument"})
    rc = resolve_board_config(base, tmp_path)
    assert rc["layers"] == {"value": 2, "source": "board-user",
                            "status": "confirmed"}
    assert rc["thickness"]["value"] == 1.0
    assert rc["board_size"]["value"] == {"width_mm": 100.0,
                                         "height_mm": 60.0}
    assert rc["board_size"]["source"] == "board-user"


def test_engine_override_status(tmp_path):
    from skidl.board.rule_discovery import _entry
    e = _entry(4, "engine-escalation")
    assert e["status"] == "engine-override"


# ---------------------------------------------------------------------------
# Phase 3: snapshot -> diff -> auto-adopt + Edge.Cuts verbatim carry
# ---------------------------------------------------------------------------

def test_snapshot_then_diff_detects_user_changes(tmp_path):
    base = "diff"
    _pcb(tmp_path, base, thickness=1.6, edge=(0, 0, 80, 50))
    bs.stamp_board_fingerprint(base, tmp_path)
    bs.write_state_snapshot(base, tmp_path)
    assert bs.diff_saved_vs_snapshot(base, tmp_path) == []

    _pcb(tmp_path, base, thickness=1.0, edge=(0, 0, 100, 60))  # user edit
    d = {x["field"]: x for x in bs.diff_saved_vs_snapshot(base, tmp_path)}
    assert d["thickness"] == {"field": "thickness", "was": 1.6, "now": 1.0}
    assert d["edge_cuts"]["now"]["width_mm"] == 100.0


def test_adopt_manual_changes_user_board(tmp_path):
    base = "adopt"
    _pcb(tmp_path, base, generator="pcbnew", copper=("F.Cu", "B.Cu"),
         thickness=1.0, edge=(0, 0, 100, 60))
    _sidecar(tmp_path, base, layers=4, board_width=80.0, board_height=50.0,
             _sources={"layers": "argument"})
    r = bs.adopt_manual_changes(base, tmp_path)
    assert r["user_edited"] is True
    assert r["adopted"]["layers"] == {"was": 4, "now": 2}
    assert r["adopted"]["board_width"]["now"] == 100.0
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["layers"] == 2 and sc["board_width"] == 100.0
    assert sc["_sources"]["layers"] == "board-user"


def test_adopt_noop_on_machine_board(tmp_path):
    base = "adopt2"
    _pcb(tmp_path, base)
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, layers=4)
    r = bs.adopt_manual_changes(base, tmp_path)
    assert r["user_edited"] is False and r["adopted"] == {}
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["layers"] == 4                      # pending request untouched


def test_adopt_only_user_changed_fields(tmp_path):
    """Outline-only user edit must NOT clobber a pending layers request:
    adopt what the user CHANGED (snapshot diff), not the board's
    incidental current values."""
    base = "adopt3"
    four = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    _pcb(tmp_path, base, copper=four, edge=(0, 0, 80, 50))
    bs.stamp_board_fingerprint(base, tmp_path)
    bs.write_state_snapshot(base, tmp_path)
    _sidecar(tmp_path, base, layers=2, _sources={"layers": "user"})
    # user edits ONLY the outline (layers stay 4 on the board)
    _pcb(tmp_path, base, copper=four, edge=(0, 0, 100, 60))
    r = bs.adopt_manual_changes(base, tmp_path)
    assert r["user_edited"] is True
    assert r["adopted"]["board_width"]["now"] == 100.0
    assert "layers" not in r["adopted"]
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["layers"] == 2                 # pending request SURVIVES
    assert sc["board_width"] == 100.0


def test_edge_cuts_blocks_and_rectangle_detection(tmp_path):
    base = "shape"
    text = _pcb(tmp_path, base, edge=(0, 0, 80, 50))
    blocks = bs.read_edge_cuts_blocks(text)
    assert len(blocks) == 4
    assert bs.edge_cuts_is_rectangle(blocks) is True

    rounded = text.replace(
        ")\n", ")\n\t(gr_arc\n\t\t(start 0 5)\n\t\t(mid 1.5 1.5)\n\t\t"
        '(end 5 0)\n\t\t(layer "Edge.Cuts")\n\t)\n', 1)
    blocks2 = bs.read_edge_cuts_blocks(rounded)
    assert bs.edge_cuts_is_rectangle(blocks2) is False


def test_shift_edge_blocks():
    from skidl.board.pcb_writer import _shift_edge_blocks
    blk = '(gr_line\n\t\t(start 0 0)\n\t\t(end 100 0)\n\t\t(layer "Edge.Cuts")\n\t)'
    out = _shift_edge_blocks([blk], 10.5, -2.0)[0]
    assert "(start 10.5 -2)" in out and "(end 110.5 -2)" in out


# ---------------------------------------------------------------------------
# mech-path regressions (reg5v live-test bugs, 2026-08-11)
# ---------------------------------------------------------------------------

def test_mech_plan_falls_back_to_toplevel_board_size():
    """Questionnaire/update store the size TOP-LEVEL; connector edges live
    under mechanical -- the plan must see the size or edge pins silently
    skip and placement collides at a corner (measured: reg5v)."""
    from skidl.board.place.mech import build_mech_plan
    from skidl.board.model import BoardModel
    plan = build_mech_plan(
        {"board_width": 70, "board_height": 50,
         "mechanical": {"connectors": [{"ref": "J1", "edge": "right"}]}},
        BoardModel(name="x"))
    assert plan.width == 70 and plan.height == 50


def test_apply_fixed_centers_connector_without_offset():
    """offset omitted -> centered along the edge, NOT origin-at-corner
    (which hung half the courtyard outside the outline -- a
    size-independent overlap the placer could never resolve)."""
    from skidl.board.place.mech import build_mech_plan, apply_fixed
    from skidl.board.model import BoardModel, Footprint
    b = BoardModel(name="x")
    b.footprints.append(Footprint(ref="J1", lib_id="t", value="",
                                  x=0.0, y=0.0, pads=[],
                                  courtyard=(-2.0, -2.0, 2.0, 2.0)))
    plan = build_mech_plan(
        {"board_width": 70.0, "board_height": 50.0,
         "mechanical": {"connectors": [{"ref": "J1", "edge": "right"}]}}, b)
    apply_fixed(b, plan)
    fp = b.footprints[0]
    assert fp.locked and "J1" in plan.fixed_refs
    assert fp.y == 25.0                     # centered on the edge
    assert fp.x == 70.0 - 1.0 - 2.0         # W - EDGE_FLUSH - courtyard x2
    # explicit offset still wins
    b2 = BoardModel(name="y")
    b2.footprints.append(Footprint(ref="J1", lib_id="t", value="",
                                   x=0.0, y=0.0, pads=[],
                                   courtyard=(-2.0, -2.0, 2.0, 2.0)))
    plan2 = build_mech_plan(
        {"board_width": 70.0, "board_height": 50.0,
         "mechanical": {"connectors": [{"ref": "J1", "edge": "left",
                                        "offset": 10.0}]}}, b2)
    apply_fixed(b2, plan2)
    assert b2.footprints[0].y == 10.0


# ---------------------------------------------------------------------------
# Phase 4: config-conformance VERIFY gate
# ---------------------------------------------------------------------------

def test_conformance_pass(tmp_path):
    from skidl.board.conformance import check_config_conformance
    base = "conf1"
    _pcb(tmp_path, base, copper=("F.Cu", "B.Cu"), edge=(0, 0, 80, 50))
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, layers=2, board_width=80.0, board_height=50.0,
             _sources={"layers": "user", "board_width": "user"})
    conf = check_config_conformance(base, tmp_path)
    assert conf["ok"] is True
    assert conf["checks"]["layers"]["ok"] is True
    assert conf["checks"]["board_size"]["ok"] is True
    assert conf["mismatches"] == []


def test_conformance_mismatch_without_override_fails(tmp_path):
    from skidl.board.conformance import check_config_conformance
    base = "conf2"
    _pcb(tmp_path, base, copper=("F.Cu", "B.Cu"))        # built 2-layer
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, layers=4, _sources={"layers": "user"})
    conf = check_config_conformance(base, tmp_path)
    assert conf["ok"] is False
    assert "layers" in conf["mismatches"]


def test_conformance_mismatch_with_override_reported_not_failed(tmp_path):
    from skidl.board.conformance import check_config_conformance
    base = "conf3"
    _pcb(tmp_path, base, copper=("F.Cu", "B.Cu"))
    bs.stamp_board_fingerprint(base, tmp_path)
    _sidecar(tmp_path, base, layers=4,
             _sources={"layers": "engine-escalation"},
             engine_overrides={"layers": {"was": 2, "now": 4,
                                          "was_source": "argument",
                                          "reason": "test"}})
    conf = check_config_conformance(base, tmp_path)
    assert conf["ok"] is True                    # override recorded -> expected
    assert conf["mismatches_with_override"] == ["layers"]
    assert conf["overridden_user_values"][0]["field"] == "layers"


def test_count_npth_scoped_to_mounting_holes(tmp_path):
    from skidl.board.conformance import count_npth_holes
    mh = ('\n\t(footprint "MountingHole:MountingHole_3.2mm_M3"\n'
          '\t\t(pad "" np_thru_hole circle)\n\t)')
    conn = ('\n\t(footprint "Connector_USB:USB_C"\n'
            '\t\t(pad "" np_thru_hole circle)\n'
            '\t\t(pad "" np_thru_hole circle)\n\t)')
    base = "npth"
    text = _pcb_extra(tmp_path, base, mh + mh + conn)
    assert count_npth_holes(text) == 2           # connector NPTH excluded


def test_ruleless_user_class_survives_rewrite(tmp_path):
    from skidl.board.pcb_writer import update_project_net_classes
    from skidl.board.model import BoardModel
    pro = tmp_path / "x.kicad_pro"
    pro.write_text(json.dumps({
        "net_settings": {"classes": [
            {"name": "Default", "track_width": 0.25},
            {"name": "UserEmpty"}]}}), encoding="utf-8")
    b = BoardModel(name="x")
    b.net_classes = {"Default": {"width": 0.3}}
    update_project_net_classes(b, pro)
    out = json.loads(pro.read_text(encoding="utf-8"))
    names = [c["name"] for c in out["net_settings"]["classes"]]
    assert "UserEmpty" in names


# ---------------------------------------------------------------------------
# server twin agreement (contract: one arbitration rule)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 2: ACT + read-back verify
# ---------------------------------------------------------------------------

def test_update_verified_statuses_no_board(tmp_path):
    base = "act1"
    res = bs.update_board_setup(
        base, tmp_path,
        stackup={"layers": 4, "copper_oz": 2},
        constraints={"min_clearance": 0.2},
        board={"board_width": 30.0, "manufacturer": "pcbway",
               "currents": {"VIN": 2.0}})
    v = res["verified"]
    assert v["layers"]["status"] == "pending_regeneration"
    assert v["copper_oz"]["status"] == "applied"
    assert v["constraints.min_clearance"] == {
        "requested": 0.2, "actual_now": 0.2, "status": "applied", "note": ""}
    assert v["board_width"]["status"] == "pending_regeneration"
    assert v["manufacturer"]["status"] == "applied"
    assert v["currents"]["status"] == "applied"
    assert "layers" in res["pending_fields"]
    assert res["failed_fields"] == []
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["_sources"]["layers"] == "user"
    assert sc["_sources"]["manufacturer"] == "user"
    assert sc["currents_meta"]["VIN"] == {"source": "user",
                                          "status": "confirmed"}


def test_update_applied_when_saved_matches(tmp_path):
    base = "act2"
    _pcb(tmp_path, base, copper=("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"))
    bs.stamp_board_fingerprint(base, tmp_path)
    res = bs.update_board_setup(base, tmp_path, stackup={"layers": 4})
    assert res["verified"]["layers"]["status"] == "applied"


def test_mask_patch_keeps_machine_status(tmp_path):
    base = "act3"
    _pcb(tmp_path, base, mask=0.0)
    bs.stamp_board_fingerprint(base, tmp_path)
    res = bs.update_board_setup(base, tmp_path,
                                mask={"pad_to_mask_clearance": 0.05})
    assert res["verified"]["pad_to_mask_clearance"]["status"] == "applied"
    # the live patch rewrote OUR board -- it must still read as machine
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is True


def test_mask_pending_when_token_absent(tmp_path):
    base = "act4"
    _pcb(tmp_path, base)                      # setup block WITHOUT the token
    bs.stamp_board_fingerprint(base, tmp_path)
    res = bs.update_board_setup(base, tmp_path,
                                mask={"pad_to_mask_clearance": 0.05})
    assert res["verified"]["pad_to_mask_clearance"]["status"] == \
        "pending_regeneration"
    # ...and the write-time patch inserts the token into a carried block
    from skidl.board.pcb_writer import _patch_setup_mask
    patched = _patch_setup_mask("(setup\n\t\t(pad_to_paste_clearance 0)\n\t)",
                                0.05)
    assert "(pad_to_mask_clearance 0.05)" in patched


def test_update_does_not_confirm_requirements_gate(tmp_path):
    base = "act5"
    bs.update_board_setup(base, tmp_path, stackup={"layers": 2})
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert "requirements_confirmed" not in sc
    assert "manufacturer" not in sc


def test_server_detect_manual_edits_agrees(tmp_path):
    import os
    import tempfile
    os.environ.setdefault("SKIDL_MCP_OUT", tempfile.mkdtemp())
    sys.path.insert(0, str(REPO))
    srv = pytest.importorskip("skidl_mcp_server")
    base = "twin"
    srv._PROJECT_DIRS[base] = tmp_path

    _pcb(tmp_path, base)
    bs.stamp_board_fingerprint(base, tmp_path)
    assert srv._detect_manual_edits(base) is None
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is True

    p = tmp_path / f"{base}.kicad_pcb"
    p.write_text(p.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert srv._detect_manual_edits(base) is not None
    assert bs.board_edit_status(base, tmp_path)["machine_generated"] is False
