"""
tests/unit_tests/test_net_classify_power.py

Pins the unified power/rail net classification (gap H2). Historically the
"positive supply rail" concept was encoded by two divergent regexes
(net_classify._POWER_RAIL_NET_RE strict vs draw_power_rail._RAIL_RE broad) that
disagreed on real rail names (VIN/VSYS/bare-3V3), and the "render as a power
symbol" regex was copy-pasted into 5 files. This test locks in:

  1. the canonical rail SUPERSET covers every historical strict name PLUS the
     previously-missed rail names (VIN, VSYS, bare NvM: 5V/3V3/1V8, SYS_3V3);
  2. grounds are power nets but NOT rails (rail/ground split preserved);
  3. ordinary signals are neither;
  4. is_power_symbol_net() is byte-equivalent to the historical _POWER_NET_RE
     behaviour -- proving the render decision did NOT silently broaden;
  5. draw_power_rail consumes the SAME canonical rail pattern (no future drift).
"""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "skidl" / "anvil"))

from skidl.schematics.net_classify import (  # noqa: E402
    _GROUND_NET_RE,
    _POWER_RAIL_NET_RE,
    is_power_net,
    is_power_symbol_net,
)

# The exact strict rail pattern that placement used BEFORE unification -- every
# one of these must still be recognised as a rail (no regression).
_HISTORICAL_STRICT_RAILS = [
    "+5V", "+3V3", "+12V", "+1V8", "VCC", "VDD", "VBUS", "VBAT",
    "AVCC", "AVDD", "DVCC", "DVDD", "VCC1", "VDD3",
]
# Rail names the old strict regex MISSED but draw_power_rail drew -- now unified.
_NEWLY_COVERED_RAILS = ["VIN", "VSYS", "5V", "3V3", "1V8", "12V", "SYS_3V3", "MCU_1V8"]

_GROUNDS = ["GND", "AGND", "DGND", "PGND", "VSS", "VEE", "GND1"]
_SIGNALS = ["SDA", "SCL", "MOSI", "TX", "RESET", "LED1", "PWM", "ADC0", "EN", "D0"]

# Byte-copy of the historical _POWER_NET_RE (render-as-power-symbol) to prove
# is_power_symbol_net did not broaden.
_HISTORICAL_POWER_NET_RE = re.compile(
    r"^(\+\d[\d.]*V[\d]*|GND|AGND|DGND|PGND|VCC|VDD|VSS|VEE|VBUS|VBAT"
    r"|AVCC|AVDD|DVCC|DVDD)$",
    re.IGNORECASE,
)


def test_all_rails_recognised():
    for n in _HISTORICAL_STRICT_RAILS + _NEWLY_COVERED_RAILS:
        assert _POWER_RAIL_NET_RE.match(n), f"{n} should classify as a rail"
        assert is_power_net(n), f"{n} should be a power net"


def test_grounds_are_power_but_not_rails():
    for n in _GROUNDS:
        assert _GROUND_NET_RE.match(n), f"{n} should be ground"
        assert not _POWER_RAIL_NET_RE.match(n), f"{n} must NOT be a positive rail"
        assert is_power_net(n), f"{n} should still be a power net"


def test_signals_are_not_power():
    for n in _SIGNALS:
        assert not _POWER_RAIL_NET_RE.match(n), f"{n} must not be a rail"
        assert not is_power_net(n), f"{n} must not be a power net"
        assert not is_power_symbol_net(n), f"{n} must not render as a power symbol"


def test_render_predicate_unchanged_vs_history():
    # is_power_symbol_net MUST equal the historical behaviour for every name --
    # this is the "render did not silently broaden" guarantee.
    names = (_HISTORICAL_STRICT_RAILS + _NEWLY_COVERED_RAILS + _GROUNDS
             + _SIGNALS + ["+3.3V", "+24V"])
    for n in names:
        historical = bool(n.startswith("+") or _HISTORICAL_POWER_NET_RE.match(n))
        assert is_power_symbol_net(n) == historical, (
            f"{n}: render predicate diverged from history "
            f"({is_power_symbol_net(n)} != {historical})"
        )


def test_draw_power_rail_uses_canonical_pattern():
    import draw_power_rail  # noqa: E402  (anvil dir on path)
    assert draw_power_rail._RAIL_RE.pattern == _POWER_RAIL_NET_RE.pattern, (
        "draw_power_rail._RAIL_RE must be the canonical net_classify rail pattern"
    )
