"""End-to-end acceptance for the dynamic power pours -- the LIVE route gate.

The other unit tests cover the pour decisions, geometry, exclusion set, and
file format. Only this one runs a real route, which is the sole way to prove
(a) power pads reach their dedicated-plane pour and (b) the fully routed
board is DRC-clean with the pours in place.

The repo bundles the toolchain (tools/jre/ + tools/freerouting.jar), and
find_java() locates the bundled JRE, so this RUNS by default (~7-17 s). It
skips only where the toolchain has been stripped (e.g. a slimmed CI image or
a machine without kicad-cli). It builds lm7805_psu with routing on and
asserts the gate the whole pipeline is designed around: DRC unconnected == 0
AND errors == 0 on the routed, poured board.
"""
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NET = REPO / "mcp_circuits" / "lm7805_psu.net"


def _toolchain_ready() -> bool:
    try:
        from skidl.board.route.freerouting import find_java, find_freerouting_jars
        from skidl.board.adapter.pymodel import _find_kicad_cli
        return bool(find_java()) and bool(find_freerouting_jars()) and bool(_find_kicad_cli())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _toolchain_ready() or not NET.is_file(),
    reason="live router: needs java + FreeRouting jar + kicad-cli "
           "(manual verification step -- see module docstring)",
)


def test_routed_poured_board_is_drc_clean(tmp_path):
    import skidl.anvil.anvil_libs  # noqa: F401  -- sets KICAD9_FOOTPRINT_DIR
    from skidl.board.pipeline import build_pcb
    from skidl.board.pcb_job import _drc
    from skidl.board.adapter.pymodel import _find_kicad_cli

    net = tmp_path / "e2e.net"
    shutil.copy2(NET, net)

    rep = build_pcb(net, out_dir=tmp_path, name="e2e", layers=2, route=True)
    assert rep.get("ok"), rep
    assert rep.get("routing", {}).get("ok"), rep.get("routing")
    # ground was poured + excluded from the DSN; the router handled the rest
    assert "GND" in rep.get("power_pours", {}).get("excluded_from_routing", [])

    gate = _drc(tmp_path / "e2e.kicad_pcb", _find_kicad_cli(), refill=True)
    assert gate.get("drc_unconnected") == 0, gate
    assert gate.get("drc_errors") == 0, gate
