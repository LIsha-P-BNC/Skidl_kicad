"""Item 4: smallest-cap-closest decap ordering in place/rules.py.

_cap_value parses a cap's value to farads so the block placer can slot the
smallest cap into the closest (un-stacked) position by its IC power pin.
"""
from types import SimpleNamespace

import pytest

from skidl.board.place.rules import _cap_value


def _fp(value):
    return SimpleNamespace(value=value)


def test_parses_common_cap_values():
    assert _cap_value(_fp("100nF")) == pytest.approx(100e-9)
    assert _cap_value(_fp("0.1uF")) == pytest.approx(0.1e-6)
    assert _cap_value(_fp("10uF")) == pytest.approx(10e-6)
    assert _cap_value(_fp("4.7uF")) == pytest.approx(4.7e-6)
    assert _cap_value(_fp("22pF")) == pytest.approx(22e-12)
    assert _cap_value(_fp("10u")) == pytest.approx(10e-6)     # unit without F


def test_ordering_smallest_first():
    vals = ["10uF", "1uF", "100nF", "22pF"]
    assert sorted(vals, key=lambda v: _cap_value(_fp(v))) \
        == ["22pF", "100nF", "1uF", "10uF"]


def test_unknown_sorts_last():
    # unit-less or unparseable -> +inf, so a known small cap always wins the
    # closest slot
    assert _cap_value(_fp("100")) == float("inf")      # bare number ambiguous
    assert _cap_value(_fp("")) == float("inf")
    assert _cap_value(_fp("DNP")) == float("inf")
    assert _cap_value(_fp("100nF")) < _cap_value(_fp("100"))
