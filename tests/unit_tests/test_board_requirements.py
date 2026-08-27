"""
tests/unit_tests/test_board_requirements.py

PCB requirements-intake tests: the layers precedence fix, the canonical
resolver with persisted provenance (_sources), the design-aware
questionnaire, init's confirmation/carry-forward semantics, and
create_pcb's requirements_needed gate (fail-closed + accept_defaults).
Self-contained: synthetic netlists in tmp_path, no library fixtures.
"""

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.board.project_init import initialize_project
from skidl.board.rule_discovery import resolve_board_config
from skidl.board.requirements import generate_questionnaire
from skidl.board import board_setup as bs
from skidl.board.pipeline import build_pcb
from skidl.board.pcb_job import run_pcb_job


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _netlist(path, comps, nets):
    """Write a minimal KiCad netlist the board engine can parse.
    comps: {ref: (value, footprint)}; nets: {name: [(ref, pin), ...]}"""
    lines = ['(export (version "E")', '  (components']
    for ref, (val, fp) in comps.items():
        lines.append(f'    (comp (ref "{ref}") (value "{val}") '
                     f'(footprint "{fp}"))')
    lines.append('  )')
    lines.append('  (nets')
    for i, (name, pins) in enumerate(nets.items(), 1):
        nodes = " ".join(f'(node (ref "{r}") (pin "{p}") (pintype "passive"))'
                         for r, p in pins)
        lines.append(f'    (net (code "{i}") (name "{name}") '
                     f'(class "Default") {nodes})')
    lines.append('  ))')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _plain(tmp, base="brd"):
    _netlist(tmp / f"{base}.net",
             {"R1": ("10k", "Resistor_SMD:R_0603_1608Metric"),
              "C1": ("100nF", "Capacitor_SMD:C_0603_1608Metric")},
             {"N1": [("R1", "1"), ("C1", "1")],
              "GND": [("R1", "2"), ("C1", "2")]})
    return base


def _qids(out):
    return [q["id"] for q in out["questions"]]


# ---------------------------------------------------------------------------
# Phase A: precedence -- no build-time input can shadow the sidecar
# ---------------------------------------------------------------------------

def test_create_pcb_has_no_layers_parameter():
    sys.path.insert(0, str(REPO))
    srv = pytest.importorskip("skidl_mcp_server")
    fn = getattr(srv.create_pcb, "fn", srv.create_pcb)
    params = inspect.signature(fn).parameters
    assert "layers" not in params
    assert "accept_defaults" in params


def test_layers_defaults_are_none_everywhere():
    assert inspect.signature(build_pcb).parameters["layers"].default is None
    assert inspect.signature(run_pcb_job).parameters["layers"].default is None
    assert inspect.signature(initialize_project).parameters["layers"].default \
        is None


def test_explicit_layers_2_records_argument_source(tmp_path):
    base = _plain(tmp_path)
    rep = initialize_project(base, tmp_path, layers=2)
    assert rep["settings"]["layers"]["source"] == "argument"
    rc = resolve_board_config(base, tmp_path)
    assert rc["layers"] == {"value": 2, "source": "argument",
                            "status": "confirmed"}


# ---------------------------------------------------------------------------
# Phase C: design-aware questionnaire
# ---------------------------------------------------------------------------

def test_usb_design_discloses_impedance_limitation(tmp_path):
    base = "usb"
    _netlist(tmp_path / f"{base}.net",
             {"U1": ("MCU", "Package_QFP:LQFP-32")},
             {"USB_DP": [("U1", "1")], "USB_DM": [("U1", "2")],
              "GND": [("U1", "3")]})
    out = generate_questionnaire(base, tmp_path)
    lim = " ".join(out["limitations"])
    assert "USB D+/D- detected" in lim and "USB_DP" in lim
    assert not any("impedance" in qid for qid in _qids(out))


def test_high_current_nets_ask_currents(tmp_path):
    base = "cur"
    _netlist(tmp_path / f"{base}.net",
             {"U1": ("Driver", "Package_SO:SOIC-8")},
             {"MOTOR_A": [("U1", "1")], "VIN": [("U1", "2")],
              "GND": [("U1", "3")], "SIG": [("U1", "4")]})
    out = generate_questionnaire(base, tmp_path)
    assert "power_currents" in _qids(out)
    q = next(q for q in out["questions"] if q["id"] == "power_currents")
    assert "MOTOR_A" in q["detected_evidence"]
    assert "2.0" in q["detected_evidence"]
    assert "heuristic" in q["detected_evidence"]
    assert q["answer_via"]["params"] == ["currents"]


def test_connectors_ask_edges(tmp_path):
    base = "con"
    _netlist(tmp_path / f"{base}.net",
             {"J1": ("Conn_01x04", "Connector_PinHeader_2.54mm:PinHeader_1x04"),
              "R1": ("10k", "Resistor_SMD:R_0603_1608Metric")},
             {"N1": [("J1", "1"), ("R1", "1")], "GND": [("J1", "2")]})
    out = generate_questionnaire(base, tmp_path)
    q = next(q for q in out["questions"] if q["id"] == "connector_edges")
    assert "J1" in q["detected_evidence"]
    assert "mechanical" in q["answer_via"]["params"]


def test_plain_design_asks_core_only_ipc_last(tmp_path):
    base = _plain(tmp_path)
    out = generate_questionnaire(base, tmp_path)
    ids = _qids(out)
    assert ids == ["board_size", "layers", "manufacturer",
                   "mounting_holes", "ipc_class"]
    q = out["questions"][-1]
    assert "I don't know / skip" in q["options"]
    assert q["skip_means"] == "omit the ipc_class parameter entirely"


def test_user_values_suppress_questions(tmp_path):
    base = _plain(tmp_path)
    (tmp_path / f"{base}.board_config.json").write_text(json.dumps({
        "manufacturer": "pcbway", "layers": 4,
        "board_width": 80.0, "board_height": 50.0,
        "_sources": {"manufacturer": "argument", "layers": "argument",
                     "board_width": "argument", "board_height": "argument"},
    }), encoding="utf-8")
    out = generate_questionnaire(base, tmp_path)
    ids = _qids(out)
    for suppressed in ("board_size", "layers", "manufacturer"):
        assert suppressed not in ids
    assert out["already_answered"]["board_size"]["source"] == "argument"
    assert out["already_answered"]["layers"]["value"] == 4


def test_user_saved_board_suppresses_questions(tmp_path):
    """KiCad is truth: a user-SAVED board (4 copper layers, 100x60 drawn
    outline) answers the layers/board_size questions -- never re-asked."""
    base = _plain(tmp_path)
    rows = "".join(f'\n\t\t({i} "{n}" signal)' for i, n in
                   enumerate(("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")))
    edge = ""
    for (a, b, c, d) in [(0, 0, 100, 0), (100, 0, 100, 60),
                         (100, 60, 0, 60), (0, 60, 0, 0)]:
        edge += (f'\n\t(gr_line\n\t\t(start {a} {b})\n\t\t(end {c} {d})'
                 f'\n\t\t(layer "Edge.Cuts")\n\t)')
    (tmp_path / f"{base}.anvil_pcb").write_text(
        '(kicad_pcb\n\t(version 20240108)\n\t(generator "pcbnew")\n'
        f'\t(layers{rows}\n\t)\n{edge}\n)\n', encoding="utf-8")
    out = generate_questionnaire(base, tmp_path)
    ids = [q["id"] for q in out["questions"]]
    assert "board_size" not in ids and "layers" not in ids
    assert out["already_answered"]["board_size"]["source"] == "board-user"
    assert out["already_answered"]["layers"]["value"] == 4


def test_fine_pitch_triggers_ipc_evidence(tmp_path, monkeypatch):
    import skidl.board.project_init as pi
    monkeypatch.setattr(pi, "_has_fine_pitch_parts", lambda p: True)
    base = _plain(tmp_path)
    out = generate_questionnaire(base, tmp_path)
    q = next(q for q in out["questions"] if q["id"] == "ipc_class")
    assert "fine-pitch" in q["detected_evidence"]


def test_question_shape_is_mechanically_complete(tmp_path):
    base = "shape"
    _netlist(tmp_path / f"{base}.net",
             {"J1": ("USB_C", "Connector_USB:USB_C_Receptacle"),
              "U1": ("MCU", "Package_QFP:LQFP-32")},
             {"VIN": [("U1", "1"), ("J1", "1")], "GND": [("U1", "2")],
              "USB_DP": [("J1", "2"), ("U1", "3")]})
    out = generate_questionnaire(base, tmp_path)
    # answer_via targets the MCP tool initialize_pcb_project; `currents`
    # is server-level (written to the sidecar there), not an engine param.
    init_params = set(inspect.signature(initialize_project).parameters) \
        | {"currents"}
    required = ("id", "question", "why_it_matters", "options",
                "default_if_skipped", "skip_means", "detected_evidence",
                "answer_via")
    assert out["questions"], "expected at least one question"
    for q in out["questions"]:
        for field in required:
            assert field in q, f"{q['id']} missing {field}"
        assert q["answer_via"]["tool"] == "initialize_pcb_project"
        for p in q["answer_via"]["params"]:
            assert p in init_params, f"{q['id']} maps to unknown param {p}"


# ---------------------------------------------------------------------------
# Phase D: init confirmation, provenance, carry-forward
# ---------------------------------------------------------------------------

def test_initialize_marks_confirmed_and_persists_sources(tmp_path):
    base = _plain(tmp_path)
    initialize_project(base, tmp_path)
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["requirements_confirmed"] is True
    assert sc["_sources"]["layers"] == "profile"       # nothing given
    assert sc["_sources"]["manufacturer"] == "profile"


def test_reinit_preserves_currents_meta_and_manufacturer(tmp_path):
    base = _plain(tmp_path)
    initialize_project(base, tmp_path, manufacturer="pcbway")
    scp = tmp_path / f"{base}.board_config.json"
    sc = json.loads(scp.read_text(encoding="utf-8"))
    assert sc["_sources"]["manufacturer"] == "argument"
    # server-style post-init writes that a re-init used to drop
    sc["currents"] = {"MOTOR_A": 2.5}
    sc["currents_meta"] = {"MOTOR_A": {"source": "user",
                                       "status": "confirmed"}}
    sc["requirements_asked"] = ["board_size", "power_currents"]
    scp.write_text(json.dumps(sc, indent=2), encoding="utf-8")

    initialize_project(base, tmp_path)      # bare re-init
    sc2 = json.loads(scp.read_text(encoding="utf-8"))
    assert sc2["currents"] == {"MOTOR_A": 2.5}
    assert sc2["currents_meta"]["MOTOR_A"]["status"] == "confirmed"
    assert sc2["requirements_asked"] == ["board_size", "power_currents"]
    assert sc2["manufacturer"] == "pcbway"          # sticky, not reset
    assert sc2["requirements_confirmed"] is True

    rc = resolve_board_config(base, tmp_path)
    assert rc["currents"]["MOTOR_A"] == {"amps": 2.5, "source": "user",
                                         "status": "confirmed"}


def test_heuristic_currents_never_confirmed(tmp_path):
    base = _plain(tmp_path)
    initialize_project(base, tmp_path)
    scp = tmp_path / f"{base}.board_config.json"
    sc = json.loads(scp.read_text(encoding="utf-8"))
    sc["currents"] = {"VIN": 1.5}       # no meta -> AI/heuristic origin
    scp.write_text(json.dumps(sc, indent=2), encoding="utf-8")
    rc = resolve_board_config(base, tmp_path)
    assert rc["currents"]["VIN"]["status"] == "unconfirmed"
    initialize_project(base, tmp_path)  # carry-forward keeps it unconfirmed
    rc2 = resolve_board_config(base, tmp_path)
    assert rc2["currents"]["VIN"]["status"] == "unconfirmed"


def test_reinit_preserves_update_written_fields(tmp_path):
    base = _plain(tmp_path)
    initialize_project(base, tmp_path)
    bs.update_board_setup(base, tmp_path, stackup={"copper_oz": 2},
                          mask={"pad_to_mask_clearance": 0.05},
                          board={"board_width": 44.0, "board_height": 33.0})
    initialize_project(base, tmp_path)      # bare re-init
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["copper_oz"] == 2
    assert sc["pad_to_mask_clearance"] == 0.05
    assert sc["board_width"] == 44.0
    assert sc["_sources"]["copper_oz"] == "user"
    # value carried through the sidecar rung keeps WHO chose it
    assert sc["_sources"]["board_width"] == "user"


def test_update_board_setup_keeps_confirmation(tmp_path):
    base = _plain(tmp_path)
    initialize_project(base, tmp_path, manufacturer="pcbway")
    bs.update_board_setup(base, tmp_path, stackup={"thickness": 1.2})
    sc = json.loads((tmp_path / f"{base}.board_config.json")
                    .read_text(encoding="utf-8"))
    assert sc["requirements_confirmed"] is True     # merge, not rebuild
    assert sc["manufacturer"] == "pcbway"
    assert sc["thickness"] == 1.2


# ---------------------------------------------------------------------------
# Phase E: the requirements gate in create_pcb
# ---------------------------------------------------------------------------

@pytest.fixture
def srv():
    os.environ.setdefault("SKIDL_MCP_OUT", tempfile.mkdtemp())
    sys.path.insert(0, str(REPO))
    return pytest.importorskip("skidl_mcp_server")


def _route_project(srv_mod, base, tmp):
    srv_mod._PROJECT_DIRS[base] = tmp
    return tmp


def test_gate_predicate_matrix(srv, tmp_path):
    base = "gpred"
    _route_project(srv, base, tmp_path)
    scp = tmp_path / f"{base}.board_config.json"
    assert srv._requirements_confirmed(base) is False        # no sidecar
    scp.write_text(json.dumps({"setup_hash": "x"}), encoding="utf-8")
    assert srv._requirements_confirmed(base) is False        # partial setup
    scp.write_text(json.dumps({"manufacturer": "jlcpcb"}), encoding="utf-8")
    assert srv._requirements_confirmed(base) is True         # legacy init
    scp.write_text(json.dumps({"requirements_confirmed": True}),
                   encoding="utf-8")
    assert srv._requirements_confirmed(base) is True         # new init


def test_gate_fires_and_fails_closed(srv, tmp_path, monkeypatch):
    base = "gclosed"
    tmp = _route_project(srv, base, tmp_path)
    _plain(tmp, base)
    # analyzer subprocess "crashes" -> generic questions, NEVER a build
    monkeypatch.setattr(srv, "_py_json",
                        lambda code, timeout=90: {"ok": False,
                                                  "error": "boom"})
    fn = getattr(srv.create_pcb, "fn", srv.create_pcb)
    res = fn(base)
    assert res["status"] == "requirements_needed"
    assert res["ok"] is False
    q = res["questionnaire"]
    assert q.get("generator_error")
    assert [x["id"] for x in q["questions"]] == ["board_size", "layers",
                                                 "manufacturer"]
    assert "initialize_pcb_project" in res["instruction"]
    assert "do NOT answer them yourself" in res["instruction"]


def test_accept_defaults_bypasses_gate(srv, tmp_path, monkeypatch):
    base = "gdefaults"
    tmp = _route_project(srv, base, tmp_path)
    _plain(tmp, base)
    monkeypatch.setattr(srv, "_py_json",
                        lambda code, timeout=90: {"ok": True, "hash": "h"})

    class FakeProc:
        def poll(self):
            return None

        def kill(self):
            pass

    monkeypatch.setattr(srv.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    fn = getattr(srv.create_pcb, "fn", srv.create_pcb)
    try:
        res = fn(base, accept_defaults=True)
        assert res["status"] == "pcb_building"
    finally:
        srv._PCB_JOBS.pop(base, None)


def test_confirmed_project_never_gates(srv, tmp_path, monkeypatch):
    base = "gconfirmed"
    tmp = _route_project(srv, base, tmp_path)
    _plain(tmp, base)
    (tmp / f"{base}.board_config.json").write_text(
        json.dumps({"manufacturer": "jlcpcb"}), encoding="utf-8")
    monkeypatch.setattr(srv, "_py_json",
                        lambda code, timeout=90: {"ok": True, "hash": "h"})

    class FakeProc:
        def poll(self):
            return None

        def kill(self):
            pass

    monkeypatch.setattr(srv.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    fn = getattr(srv.create_pcb, "fn", srv.create_pcb)
    try:
        res = fn(base)
        assert res["status"] == "pcb_building"      # legacy: no re-gate
    finally:
        srv._PCB_JOBS.pop(base, None)
