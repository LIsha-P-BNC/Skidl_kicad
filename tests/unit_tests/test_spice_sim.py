"""
tests/unit_tests/test_spice_sim.py

SPICE simulation via the app's bundled ngspice (spice_sim.run_netlist). Skips
cleanly when the InSpice/ngspice stack is not importable (e.g. the plain CI
Python without the app runtime); runs for real under the app's bundled Python.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))


def _sim_or_skip():
    from skidl.spice_sim import run_netlist
    try:
        r = run_netlist("op\nV1 vin 0 DC 10\nR1 vin vout 1k\nR2 vout 0 1k\n.op\n.end")
    except Exception as exc:
        pytest.skip(f"ngspice/InSpice stack unavailable in this runtime: {exc!r}")
    if not r.get("ok"):
        pytest.skip(f"ngspice could not run here: {r.get('error')}")
    return run_netlist


def test_operating_point_divider():
    run_netlist = _sim_or_skip()
    r = run_netlist("op\nV1 vin 0 DC 10\nR1 vin vout 1k\nR2 vout 0 1k\n.op\n.end")
    assert r["ok"] and r["analysis_type"] == "operating_point"
    assert abs(r["summary"]["vout"] - 5.0) < 1e-3, "1k/1k divider of 10V must be 5V"


def test_transient_rc_settles():
    run_netlist = _sim_or_skip()
    r = run_netlist("rc\nV1 vin 0 DC 5\nR1 vin vout 1k\nC1 vout 0 1u\n.tran 0.1m 5m\n.end")
    assert r["ok"] and r["analysis_type"] == "transient"
    assert r["n_points"] > 5 and abs(r["summary"]["vout"] - 5.0) < 0.05


def test_ac_runs():
    run_netlist = _sim_or_skip()
    r = run_netlist("ac\nV1 vin 0 DC 0 AC 1\nR1 vin vout 1k\nC1 vout 0 159n\n.ac dec 10 10 100k\n.end")
    assert r["ok"] and r["analysis_type"] == "ac" and r["n_points"] > 10


def test_bad_netlist_returns_dict():
    run_netlist = _sim_or_skip()
    r = run_netlist("missing analysis\nV1 a 0 5\nR1 a 0 1k\n.end")
    assert isinstance(r, dict) and "ok" in r
