"""Regression: the board<->netlist connectivity proof (board/verify.py) must
credit a power net carried ONLY by a copper pour, not report it as split.

This guards the "power pours excluded from the DSN" work: once a net (e.g. GND)
is poured instead of routed, its connectivity exists solely through the filled
zone. board_partition's ipcd356 export credits a pad's net only where real
copper reaches it, and pcb_writer emits zones outline-only -- so board_partition
must refill before trusting the export. These tests prove:

  1. refill=True  -> a GND-pour-only board with UNFILLED zones verifies OK
                     (board_partition refills a copy first) and does NOT mutate
                     the caller's board.
  2. refill=False -> refuses (raises) rather than emit a false split.

Skipped unless kicad-cli and the lm7805_psu fixture are both present, matching
the other board tests that need the real toolchain.
"""
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FIXTURE_PCB = REPO / "mcp_circuits" / "lm7805_psu.kicad_pcb"
FIXTURE_NET = REPO / "mcp_circuits" / "lm7805_psu.net"


def _kicad_cli():
    try:
        from skidl.board.adapter.pymodel import _find_kicad_cli
        return _find_kicad_cli()
    except Exception:
        return None


CLI = _kicad_cli()

pytestmark = pytest.mark.skipif(
    not CLI or not FIXTURE_PCB.is_file() or not FIXTURE_NET.is_file(),
    reason="needs kicad-cli and the lm7805_psu fixture board",
)


def _strip_blocks(text, head, netname=None):
    """Remove top-level (head ...) blocks, optionally only those whose body
    contains (net "netname"). Paren-matched, string-aware."""
    out, i, n = [], 0, len(text)
    key = "(" + head
    while i < n:
        s = text.find(key, i)
        if s == -1:
            out.append(text[i:])
            break
        out.append(text[i:s])
        depth, j, in_str, esc = 0, s, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        block = text[s:j + 1]
        if netname is None or f'(net "{netname}")' in block:
            i = j + 1
            if i < n and text[i] == "\n":
                i += 1
        else:
            out.append(block)
            i = j + 1
    return "".join(out)


@pytest.fixture
def pour_only_board(tmp_path):
    """lm7805_psu with every GND track removed and zones forced UNFILLED, so
    GND connectivity depends entirely on refilling the pour."""
    txt = FIXTURE_PCB.read_text(encoding="utf-8", errors="replace")
    txt = _strip_blocks(txt, "segment", "GND")     # drop GND tracks
    txt = _strip_blocks(txt, "filled_polygon")     # force unfilled zones
    assert "(zone" in txt and "(filled_polygon" not in txt
    dst = tmp_path / "pour_only.kicad_pcb"
    dst.write_text(txt, encoding="utf-8")
    return dst


def test_pour_only_net_verifies_with_refill(pour_only_board):
    from skidl.board.verify import verify_board
    res = verify_board(pour_only_board, FIXTURE_NET, CLI, refill=True)
    assert res["ok"], (res["mismatch_missing"], res["mismatch_extra"])
    # caller's board must NOT be mutated -- refill happens on a copy
    assert "(filled_polygon" not in pour_only_board.read_text(
        encoding="utf-8", errors="replace")


def test_pour_only_refuses_without_refill(pour_only_board):
    from skidl.board.verify import verify_board
    with pytest.raises(RuntimeError, match="not filled"):
        verify_board(pour_only_board, FIXTURE_NET, CLI, refill=False)
