"""The `exclude` mechanism in route/dsn_export.py: named nets are left out
of the DSN (they are carried by a copper pour instead). The DECISION of
which nets to exclude lives in board/pour.py (see test_pour.py); this file
only checks that export_dsn honors the exclusion.

Pure-python (no kicad-cli/Java), so it always runs.
"""
from pathlib import Path

from skidl.board.model import BoardModel, Net
from skidl.board.route.dsn_export import export_dsn


def _board():
    b = BoardModel(name="t", layer_count=2)
    b.nets = {
        "GND":  Net("GND",  1, pad_refs=[("U1", "1"), ("C1", "2"), ("J1", "3")]),
        "+3V3": Net("+3V3", 2, net_class="power", pad_refs=[("U1", "2"), ("C1", "1")]),
        "DATA": Net("DATA", 3, pad_refs=[("U1", "4"), ("J1", "1")]),
    }
    b.net_classes = {"Default": {"width": 0.25, "clearance": 0.15},
                     "power": {"width": 0.4, "clearance": 0.15}}
    return b


def test_excluded_net_absent_from_dsn(tmp_path):
    text = Path(export_dsn(_board(), tmp_path / "t.dsn",
                           exclude={"GND"})).read_text(encoding="utf-8")
    assert '(net "GND"' not in text     # no routing entry
    assert '"GND"' not in text          # not a class member either
    assert '(net "DATA"' in text        # signals still routed
    assert '(net "+3V3"' in text


def test_default_excludes_nothing(tmp_path):
    text = Path(export_dsn(_board(), tmp_path / "t.dsn")).read_text(encoding="utf-8")
    for name in ("GND", "DATA", "+3V3"):
        assert f'(net "{name}"' in text
