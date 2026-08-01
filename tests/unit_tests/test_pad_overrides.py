"""pcb_writer applies PadConnectOverride into each pad block. The case that
matters: a footprint where one pad ALREADY carries a (zone_connect ..) from
the library and another does not -- both must be handled in the same
footprint without corrupting either (paren-matched, not regex/string surgery).
"""
from skidl.board.model import BoardModel, Footprint, Pad, PadConnectOverride
from skidl.board.pcb_writer import _write_footprint, write_pcb

# pad "1": THT, already has (zone_connect 2) from the library
# pad "2": SMD, no zone_connect
SRC = (
    '(footprint "Lib:FP" (layer "F.Cu")\n'
    '  (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8) '
    '(layers "*.Cu") (zone_connect 2) (uuid "aaa"))\n'
    '  (pad "2" smd rect (at 3 0) (size 1 1) (layers "F.Cu") (uuid "bbb"))\n'
    ')'
)


def _fp():
    return Footprint(
        ref="U1", lib_id="Lib:FP", side="top", source_text=SRC,
        pads=[Pad("1", "GND", 0, 0, 1.5, 1.5, pad_type="thru_hole", drill=0.8),
              Pad("2", "GND", 3, 0, 1, 1, pad_type="smd")],
    )


def test_embed_replace_and_insert_same_footprint():
    out = _write_footprint(_fp(), {"GND": 1}, {"1": 1, "2": 1})
    # pad 1: existing (zone_connect 2) replaced by 1; pad 2: (zone_connect 1) inserted
    assert "(zone_connect 2)" not in out
    assert out.count("(zone_connect 1)") == 2
    # both pads still intact and net-injected
    assert '(pad "1" thru_hole' in out and '(pad "2" smd' in out
    assert out.count('(net 1 "GND")') == 2
    # parens balanced
    assert out.count("(") == out.count(")")


def test_no_override_leaves_pads_untouched():
    out = _write_footprint(_fp(), {"GND": 1}, {})
    assert "(zone_connect 2)" in out          # library value preserved
    assert "(zone_connect 1)" not in out


def test_template_footprint_gets_zone_connect():
    # no source_text -> template path; THT pad override emitted inline
    fp = Footprint(ref="J1", lib_id="Lib:J", side="top",
                   pads=[Pad("1", "GND", 0, 0, 1.5, 1.5,
                             pad_type="thru_hole", drill=0.8)])
    out = _write_footprint(fp, {"GND": 3}, {"1": 1})
    assert "(zone_connect 1)" in out
    assert out.count("(") == out.count(")")


def test_write_pcb_threads_board_pad_overrides():
    b = BoardModel(name="t", layer_count=2)
    b.nets = {}                                  # keep it minimal
    b.footprints = [_fp()]
    b.outline = [(0, 0), (10, 0), (10, 10), (0, 10)]
    b.pad_overrides = [PadConnectOverride("U1", "1", 1),
                       PadConnectOverride("U1", "2", 1)]
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        pcb = write_pcb(b, Path(d) / "t.kicad_pcb")
        txt = Path(pcb).read_text(encoding="utf-8")
    assert txt.count("(zone_connect 1)") == 2
    assert "(zone_connect 2)" not in txt
