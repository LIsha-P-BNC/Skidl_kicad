"""
tests/unit_tests/test_design_calc_power.py

Covers the power-stage sizing helpers: buck_converter, boost_converter,
current_sense_shunt. Verifies the solved standard values are self-consistent
with the stated formulas and that topology limits are enforced.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from skidl.anvil import design_calc as dc  # noqa: E402


def test_buck_12v_to_5v_2a_is_sane():
    r = dc.buck_converter(12.0, 5.0, 2.0, 500e3)
    assert abs(r["duty"] - 5.0 / 12.0) < 1e-3
    # standard inductor in a plausible buck range for 500 kHz / 2 A
    assert 1e-6 <= r["l_standard_h"] <= 100e-6
    # ripple current self-consistent with the chosen standard L
    d_il = 5.0 * (12.0 - 5.0) / (12.0 * r["l_standard_h"] * 500e3)
    assert abs(d_il - r["i_ripple_a"]) < 0.01
    assert r["i_l_peak_a"] > 2.0                      # Iout + ripple/2
    assert r["i_l_sat_min_a"] > r["i_l_peak_a"]       # 1.3x margin
    assert r["c_out_min_f"] > 0
    assert "L=Vout*(Vin-Vout)" in r["formula"]


def test_buck_rejects_impossible_ratio():
    with pytest.raises(ValueError):
        dc.buck_converter(5.0, 12.0, 1.0, 500e3)      # step-UP is not a buck
    with pytest.raises(ValueError):
        dc.buck_converter(12.0, 5.0, 0.0, 500e3)


def test_boost_5v_to_12v_1a_is_sane():
    r = dc.boost_converter(5.0, 12.0, 1.0, 500e3)
    assert abs(r["duty"] - (1.0 - 5.0 / 12.0)) < 1e-3
    # input current exceeds output current in a boost
    assert r["i_l_avg_a"] > 1.0
    assert r["i_l_peak_a"] > r["i_l_avg_a"]
    assert r["l_standard_h"] > 0 and r["c_out_min_f"] > 0
    # boost output cap is much larger than a comparable buck's
    b = dc.buck_converter(12.0, 5.0, 1.0, 500e3)
    assert r["c_out_min_f"] > b["c_out_min_f"]


def test_boost_rejects_impossible_ratio():
    with pytest.raises(ValueError):
        dc.boost_converter(12.0, 5.0, 1.0, 500e3)     # step-DOWN is not a boost


def test_shunt_sizing():
    r = dc.current_sense_shunt(10.0, v_sense_mv=50.0)
    assert abs(r["r_exact_ohm"] - 0.005) < 1e-9
    p_check = 10.0 ** 2 * r["r_standard_ohm"]
    assert abs(p_check - r["p_dissipated_w"]) < 1e-6
    assert r["p_rating_min_w"] >= 2 * r["p_dissipated_w"] * 0.999  # 2x derate
    with pytest.raises(ValueError):
        dc.current_sense_shunt(0.0)


def test_honesty_contract_on_all_power_helpers():
    for res in (dc.buck_converter(12, 5, 2, 500e3),
                dc.boost_converter(5, 12, 1, 500e3),
                dc.current_sense_shunt(5)):
        assert res["formula"] and res["basis"]
