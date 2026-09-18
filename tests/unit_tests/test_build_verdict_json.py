"""
tests/unit_tests/test_build_verdict_json.py

Locks in gap H3: _finish_build must derive the success/mode verdict from the
authoritative <base>.build_result.json that smart_schematic emits, and grep the
free-form log ONLY as a fallback when that file is absent.

The bug this prevents: a build that hit an early connectivity MISMATCH but then
recovered via a fallback still carries the stale "all-label mode -> ...MISMATCH..."
line in its log; the old log-grep read that stale line and WRONGLY reported the
recovered build as failed.
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]          # ...\skidl
sys.path.insert(0, str(REPO))                        # skidl_mcp_server.py
sys.path.insert(0, str(REPO / "src"))                # skidl package

sys.argv = ["test"]
import skidl_mcp_server as S  # noqa: E402


class _FakeProc:
    returncode = 0


def _setup(tmp_path, base, log_text, result_json):
    """Register a fake finished build in tmp_path with the given log + result."""
    S._PROJECT_DIRS[base] = tmp_path
    d = S.pdir(base)
    (d / (base + ".net")).write_text("(export)\n", encoding="utf-8")
    (d / (base + ".anvil_sch")).write_text("(kicad_sch)\n", encoding="utf-8")
    (d / (base + ".anvil_pro")).write_text("{}\n", encoding="utf-8")
    logfile = d / (base + ".build.log")
    logfile.write_text(log_text, encoding="utf-8")
    if result_json is not None:
        (d / (base + ".build_result.json")).write_text(
            json.dumps(result_json), encoding="utf-8")
    S._BUILDS[base] = {"proc": _FakeProc(), "logfile": logfile,
                       "fh": open(os.devnull, "w"), "started": 0.0}


def _isolate(monkeypatch):
    # Skip golden verification (needs the validate module) and the auto-open.
    monkeypatch.setattr(S, "_validate_mod", lambda: None)
    monkeypatch.setattr(S, "_try_open", lambda *a, **k: "skipped")


_STALE_MISMATCH_LOG = (
    ">>> smart_schematic: all-label mode -> FAIL -- MISMATCH: net GND differs\n"
    ">>> smart_schematic: routed with wires (seed=3); connectivity OK [legacy fallback]\n"
    ">>> smart_schematic: placement = legacy (anchor fell back)\n"
)


def test_recovered_mismatch_is_success_with_json(tmp_path, monkeypatch):
    _isolate(monkeypatch)
    base = "recov"
    _setup(tmp_path, base, _STALE_MISMATCH_LOG,
           {"schema": 1, "name": base, "verified": True,
            "verify_available": True, "routed": True, "routed_seed": 3,
            "placer": "legacy (anchor fell back)", "schematic_mode": "wires"})
    res = S._finish_build(base)
    assert res["ok"] is True, "recovered build must be a success (stale MISMATCH ignored)"
    assert res["schematic_mode"] == "wires"


def test_legacy_grep_still_fails_without_json(tmp_path, monkeypatch):
    _isolate(monkeypatch)
    base = "legacy"
    _setup(tmp_path, base, _STALE_MISMATCH_LOG, result_json=None)
    res = S._finish_build(base)
    assert res["ok"] is False, "no JSON -> legacy grep must still fail on MISMATCH"
    assert "MISMATCH" in res.get("error", "")


def test_json_unverified_fails(tmp_path, monkeypatch):
    _isolate(monkeypatch)
    base = "unver"
    _setup(tmp_path, base, ">>> smart_schematic: placement = legacy\n",
           {"schema": 1, "name": base, "verified": False,
            "verify_available": True, "routed": False, "routed_seed": None,
            "placer": "legacy", "schematic_mode": "labels"})
    res = S._finish_build(base)
    assert res["ok"] is False, "json verified=False must fail the build"
    assert "not verified" in res.get("error", "").lower()


def test_json_labels_mode(tmp_path, monkeypatch):
    _isolate(monkeypatch)
    base = "lab"
    _setup(tmp_path, base, ">>> ok\n",
           {"schema": 1, "name": base, "verified": True,
            "verify_available": True, "routed": False, "routed_seed": None,
            "placer": "anchor", "schematic_mode": "labels"})
    res = S._finish_build(base)
    assert res["ok"] is True
    assert res["schematic_mode"].startswith("labels")
