"""
tests/unit_tests/test_verify_connectivity.py

The connectivity guard must revert a post-processing pass ONLY on positive proof
that the pass broke the netlist -- never merely because it could not verify
(kicad-cli absent, or a flaky back-to-back export). Conflating those two was the
source of the holdout nondeterminism (the export races -> the revert races -> two
output variants) and of the power-rail-draw / junction-heal passes being falsely
thrown away. See verify_connectivity.proved_broken.

These tests exercise the PURE pin-partition comparison (_compare_netlists) and the
proof-vs-could-not-verify boundary (proved_broken) with crafted netlist fixtures,
so they run with NO kicad-cli and no full build.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "skidl" / "anvil"))

import verify_connectivity as VC  # noqa: E402


def _net(*nets):
    """Build a minimal KiCad-style netlist: _net(("GND", [("R1","1"),("C1","2")]), ...)."""
    out = ['(export (version "E")', " (nets"]
    for code, (name, nodes) in enumerate(nets, start=1):
        line = f'  (net (code {code}) (name "{name}")'
        for ref, pin in nodes:
            line += f' (node (ref {ref}) (pin {pin}))'
        line += ")"
        out.append(line)
    out.append(" ))")
    return "\n".join(out) + "\n"


def _cmp(tmp_path, intended_text, drawn_text, sym=frozenset()):
    """Write the two netlists and run the PURE file-based comparison."""
    ip = tmp_path / "intended.net"
    dp = tmp_path / "drawn.net"
    ip.write_text(intended_text, encoding="utf-8")
    dp.write_text(drawn_text, encoding="utf-8")
    return VC._compare_netlists(str(ip), str(dp), sym)


# --- the pure comparison: identical / short / split / dropped pin -------------

def test_identical_netlists_no_diff(tmp_path):
    n = _net(("N1", [("R1", "2"), ("C1", "1")]), ("N2", [("R2", "1"), ("U1", "3")]))
    assert _cmp(tmp_path, n, n) == ([], [], [])


def test_unwanted_short_detected(tmp_path):
    intended = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R4", "1")]))
    # drawn fuses A and B into one net -> an unwanted short
    shorted = _net(("AB", [("R1", "1"), ("R2", "1"), ("R3", "1"), ("R4", "1")]))
    u, m, lost = _cmp(tmp_path, intended, shorted)
    assert u and not lost
    assert ["R1.1", "R2.1", "R3.1", "R4.1"] in u


def test_missing_net_detected(tmp_path):
    intended = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R4", "1")]))
    # drawn splits B apart (R3/R4 no longer share a >=2-pin group) -> B goes missing
    split = _net(("A", [("R1", "1"), ("R2", "1")]),
                 ("B1", [("R3", "1"), ("X9", "1")]))
    u, m, lost = _cmp(tmp_path, intended, split)
    assert ["R3.1", "R4.1"] in m


def test_dropped_symbol_is_lost_pin(tmp_path):
    intended = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R4", "1")]))
    # drawn never renders R4 at all -> R4.1 is lost even if groups look plausible
    dropped = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R9", "1")]))
    u, m, lost = _cmp(tmp_path, intended, dropped)
    assert "R4.1" in lost


def test_symmetric_passive_pin_swap_is_not_a_break(tmp_path):
    # a plain resistor whose two interchangeable pins are mirrored by the placer
    intended = _net(("VIN", [("R1", "1"), ("U1", "2")]), ("VOUT", [("R1", "2"), ("U1", "5")]))
    swapped = _net(("VIN", [("R1", "2"), ("U1", "2")]), ("VOUT", [("R1", "1"), ("U1", "5")]))
    u, m, lost = _cmp(tmp_path, intended, swapped, sym=frozenset({"R1"}))
    assert (u, m, lost) == ([], [], []), "symmetric passive pin swap must compare equal"


# --- the guard predicate: proof vs could-not-verify ---------------------------

def _write_pair(tmp_path, intended_text):
    (tmp_path / "p.net").write_text(intended_text, encoding="utf-8")
    (tmp_path / "p.anvil_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    return str(tmp_path / "p")


def test_proved_broken_false_when_export_unavailable(tmp_path, monkeypatch):
    """A flaky/absent export yields NO proof of breakage -> guard must NOT revert."""
    base = _write_pair(tmp_path, _net(("A", [("R1", "1"), ("R2", "1")])))
    monkeypatch.setattr(VC, "KICAD_CLI", str(tmp_path / "anvil-cli.exe"))
    (tmp_path / "anvil-cli.exe").write_text("", encoding="utf-8")  # exists so the gate passes
    monkeypatch.setattr(VC, "export_from_schematic", lambda name: None)  # export "flaked"
    assert VC.proved_broken(base) is False


def test_proved_broken_false_when_no_cli(tmp_path, monkeypatch):
    base = _write_pair(tmp_path, _net(("A", [("R1", "1"), ("R2", "1")])))
    monkeypatch.setattr(VC, "KICAD_CLI", "")  # no cli at all
    assert VC.proved_broken(base) is False


def test_proved_broken_true_on_real_short(tmp_path, monkeypatch):
    base = _write_pair(tmp_path, _net(("A", [("R1", "1"), ("R2", "1")]),
                                      ("B", [("R3", "1"), ("R4", "1")])))
    monkeypatch.setattr(VC, "KICAD_CLI", str(tmp_path / "anvil-cli.exe"))
    (tmp_path / "anvil-cli.exe").write_text("", encoding="utf-8")
    drawn = tmp_path / "drawn.net"
    drawn.write_text(_net(("AB", [("R1", "1"), ("R2", "1"), ("R3", "1"), ("R4", "1")])),
                     encoding="utf-8")
    monkeypatch.setattr(VC, "export_from_schematic", lambda name: str(drawn))
    assert VC.proved_broken(base) is True


def test_proved_broken_false_on_clean_export(tmp_path, monkeypatch):
    intended = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R4", "1")]))
    base = _write_pair(tmp_path, intended)
    monkeypatch.setattr(VC, "KICAD_CLI", str(tmp_path / "anvil-cli.exe"))
    (tmp_path / "anvil-cli.exe").write_text("", encoding="utf-8")
    drawn = tmp_path / "drawn.net"
    drawn.write_text(intended, encoding="utf-8")
    monkeypatch.setattr(VC, "export_from_schematic", lambda name: str(drawn))
    assert VC.proved_broken(base) is False


# --- defects(): the tri-state the RELATIVE guard is built on ------------------

def _defects_for(tmp_path, monkeypatch, intended, drawn_text):
    base = _write_pair(tmp_path, intended)
    monkeypatch.setattr(VC, "KICAD_CLI", str(tmp_path / "anvil-cli.exe"))
    (tmp_path / "anvil-cli.exe").write_text("", encoding="utf-8")
    drawn = tmp_path / "drawn.net"
    drawn.write_text(drawn_text, encoding="utf-8")
    monkeypatch.setattr(VC, "export_from_schematic", lambda name: str(drawn))
    return VC.defects(base)


def test_defects_none_when_undeterminable(tmp_path, monkeypatch):
    """No kicad-cli -> None (NOT an empty 'clean' set): the guard must be able to
    tell 'cannot check' apart from 'checked, nothing wrong'."""
    base = _write_pair(tmp_path, _net(("A", [("R1", "1"), ("R2", "1")])))
    monkeypatch.setattr(VC, "KICAD_CLI", "")
    assert VC.defects(base) is None
    # cli present but export flakes -> still None
    monkeypatch.setattr(VC, "KICAD_CLI", str(tmp_path / "anvil-cli.exe"))
    (tmp_path / "anvil-cli.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(VC, "export_from_schematic", lambda name: None)
    assert VC.defects(base) is None


def test_defects_empty_when_clean(tmp_path, monkeypatch):
    n = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R4", "1")]))
    assert _defects_for(tmp_path, monkeypatch, n, n) == frozenset()


def test_defects_signatures(tmp_path, monkeypatch):
    intended = _net(("A", [("R1", "1"), ("R2", "1")]), ("B", [("R3", "1"), ("R4", "1")]))
    shorted = _net(("AB", [("R1", "1"), ("R2", "1"), ("R3", "1"), ("R4", "1")]))
    d = _defects_for(tmp_path, monkeypatch, intended, shorted)
    assert ("short", ("R1.1", "R2.1", "R3.1", "R4.1")) in d
    assert ("break", ("R1.1", "R2.1")) in d
    assert ("break", ("R3.1", "R4.1")) in d


def test_relative_guard_exonerates_a_neutral_pass_on_a_prebroken_sheet(tmp_path, monkeypatch):
    """The core P1 property: a pass that does NOT change the netlist introduces
    ZERO new defects even when the sheet it received was ALREADY broken -- so the
    relative guard (after - before) would not revert it. This is the field-text
    false-revert on holdout_can_node reduced to its essence."""
    intended = _net(("X", [("J2", "1"), ("R3", "1"), ("U3", "7")]),
                    ("Y", [("J2", "2"), ("R3", "2"), ("U3", "6")]))
    # the router dropped R3 from both nets (pre-existing break) -- identical
    # "before" and "after" because the neutral pass leaves the netlist untouched
    prebroken = _net(("X", [("J2", "1"), ("U3", "7")]), ("Y", [("J2", "2"), ("U3", "6")]))
    before = _defects_for(tmp_path, monkeypatch, intended, prebroken)
    after = _defects_for(tmp_path, monkeypatch, intended, prebroken)
    assert before and before == after, "pre-existing defects must be present and stable"
    assert (after - before) == frozenset(), "a netlist-neutral pass introduces NO new defect"
