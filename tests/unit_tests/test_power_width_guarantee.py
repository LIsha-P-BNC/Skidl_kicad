"""build_pcb's safety net: power nets must route at their IPC-2152 width even
when initialize_pcb_project never ran (the width plan otherwise only lived in
project_init, so a direct create_pcb routed power at the 0.25mm Default)."""
from skidl.board.model import BoardModel, Net
from skidl.board.pipeline import _guarantee_power_widths


def _board(nets, net_classes=None):
    b = BoardModel(name="t", layer_count=2)
    b.net_classes = net_classes or {"Default": {"width": 0.25}}
    b.nets = {n.name: n for n in nets}
    return b


def test_high_current_power_net_widened():
    # VBUS -> 1.5 A supply heuristic -> ~0.53 mm, over the 0.25 default
    n = Net("VBUS", 1, net_class="Default", pad_refs=[("U1", "1"), ("C1", "1")])
    b = _board([n])
    applied = _guarantee_power_widths(b, None)
    assert applied                                   # a PWR_* class was synthesized
    assert b.nets["VBUS"].net_class != "Default"
    assert b.net_classes[b.nets["VBUS"].net_class]["width"] > 0.25


def test_low_current_signal_untouched():
    n = Net("SCL", 1, net_class="Default", pad_refs=[("U1", "5"), ("J1", "2")])
    b = _board([n])
    _guarantee_power_widths(b, None)
    assert b.nets["SCL"].net_class == "Default"      # ~0.1 mm < default, no change


def test_idempotent_when_class_already_wide():
    # net already in a Power class wide enough -> not re-bucketed
    n = Net("VBUS", 1, net_class="Power", pad_refs=[("U1", "1"), ("C1", "1")])
    b = _board([n], {"Default": {"width": 0.25}, "Power": {"width": 1.0}})
    _guarantee_power_widths(b, None)
    assert b.nets["VBUS"].net_class == "Power"


def test_declared_current_drives_width():
    n = Net("MOTOR", 1, net_class="Default", pad_refs=[("U1", "1"), ("M1", "1")])
    b = _board([n])
    _guarantee_power_widths(b, {"MOTOR": 5.0})
    assert b.nets["MOTOR"].net_class != "Default"
    assert b.net_classes[b.nets["MOTOR"].net_class]["width"] >= 1.0   # 5 A -> wide
