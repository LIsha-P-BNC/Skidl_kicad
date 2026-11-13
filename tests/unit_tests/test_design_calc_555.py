"""
tests/unit_tests/test_design_calc_555.py

Covers the NE555 timing helpers added to design_calc: astable (frequency +
duty -> R1,R2,C) and monostable (pulse width -> R,C). Verifies the solved
standard values reproduce the target within E-series rounding tolerance and
that the documented topology limits are enforced.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.anvil import design_calc as dc  # noqa: E402


def test_astable_hits_target_frequency_and_duty():
    r = dc.ne555_astable(1000.0, duty=0.60)
    # E-series rounding of two resistors -> allow a modest tolerance.
    assert abs(r["f_actual_hz"] - 1000.0) / 1000.0 < 0.15
    assert abs(r["duty_actual"] - 0.60) < 0.06
    assert r["r1_ohm"] > 0 and r["r2_ohm"] > 0
    assert 1e-9 <= r["c_farad"] <= 100e-6
    # honesty contract: formula + basis present
    assert "1.44" in r["formula"] and r["basis"]


def test_astable_back_calculation_is_self_consistent():
    r = dc.ne555_astable(500.0, duty=0.65)
    f_check = 1.44 / ((r["r1_ohm"] + 2 * r["r2_ohm"]) * r["c_farad"])
    # f_actual_hz is stored rounded to 3 decimals; allow that rounding step.
    assert abs(f_check - r["f_actual_hz"]) < 1e-3


def test_astable_rejects_sub_50_percent_duty():
    with pytest.raises(ValueError):
        dc.ne555_astable(1000.0, duty=0.40)


def test_astable_honours_explicit_cap():
    r = dc.ne555_astable(1000.0, duty=0.6, c_farad=100e-9)
    assert r["c_farad"] == 100e-9


def test_monostable_hits_target_pulse():
    r = dc.ne555_monostable(1e-3)                 # 1 ms one-shot
    assert abs(r["pulse_actual_s"] - 1e-3) / 1e-3 < 0.10
    t_check = 1.1 * r["r_ohm"] * r["c_farad"]
    assert abs(t_check - r["pulse_actual_s"]) < 1e-9
    assert "1.1" in r["formula"]


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        dc.ne555_astable(0.0)
    with pytest.raises(ValueError):
        dc.ne555_monostable(-1.0)
