"""
src/skidl/board/adapter/pymodel.py

PyModelBackend -- the v1 BoardBackend implementation (Part 2 of the
M0/M1 spec: "Board-mutation backend = pure-python"). Pure Python plus
subprocess calls out to kicad-cli for the two things that stay KiCad's
job (DRC, board netlist export) -- never pcbnew, never kipy.

export_dsn / import_ses are M1 (route/dsn_export.py, route/ses_import.py
land the real implementations). export_board_netlist is stubbed too,
deliberately -- see its docstring for why, rather than guessing at a
fixed-column IPC-D-356 layout with no real sample to check it against.
"""

from __future__ import annotations
import json
import os
import shutil
import subprocess
from pathlib import Path

from skidl.board.adapter.base import BoardBackend
from skidl.board.model import BoardModel
from skidl.board import pcb_writer


def _find_kicad_cli() -> str:
    """Locate kicad-cli via the anvil install-bin detector (the installed
    CAD app is NOT on PATH; open_anvilcad._find_bin is path/username/
    branding agnostic and survives the stripped env MCP clients spawn
    with), falling back to PATH for stock-KiCad machines."""
    try:
        from skidl.anvil.open_anvilcad import _find_bin
        b = _find_bin()
        if b:
            p = os.path.join(b, "kicad-cli.exe")
            if os.path.isfile(p):
                return p
    except Exception:
        pass
    exe = shutil.which("kicad-cli")
    if not exe:
        raise EnvironmentError(
            "kicad-cli not found (no CAD app install detected, not on PATH)"
        )
    return exe


class PyModelBackend(BoardBackend):
    def populate(self, board: BoardModel, netlist_path: Path) -> BoardModel:
        populated = pcb_writer.build_board(
            netlist_path, name=board.name,
            net_classes=board.net_classes or None,
            layer_count=board.layer_count,
        )
        board.footprints = populated.footprints
        board.nets = populated.nets
        board.net_classes = populated.net_classes
        board.outline = populated.outline
        return board

    def apply_placement(self, board: BoardModel) -> None:
        # No-op for PyModelBackend: board.footprints[i].x/y/rotation
        # already IS the placement state (place/simple.py and
        # place/rules.py mutate it directly). The seam stays here so a
        # pcbnew-backed implementation has somewhere to push those
        # values into real FOOTPRINT objects instead.
        return

    def apply_net_classes(self, board: BoardModel) -> None:
        # Same story: board.net_classes is already the source of truth
        # here; write_pcb() reads it directly.
        return

    def write_pcb(self, board: BoardModel, out_path: Path) -> Path:
        return pcb_writer.write_pcb(board, out_path)

    def export_dsn(self, board: BoardModel, out_path: Path) -> Path:
        raise NotImplementedError(
            "export_dsn is M1 (route/dsn_export.py) -- not part of the M0 "
            "vertical slice."
        )

    def import_ses(self, board: BoardModel, ses_path: Path) -> None:
        raise NotImplementedError(
            "import_ses is M1 (route/ses_import.py) -- not part of the M0 "
            "vertical slice."
        )

    def run_drc(self, pcb_path: Path) -> dict:
        cli = _find_kicad_cli()
        report_path = pcb_path.with_suffix(".drc.json")
        result = subprocess.run(
            [cli, "pcb", "drc", "--format", "json", "--severity-all",
             "--output", str(report_path), str(pcb_path)],
            capture_output=True, text=True,
        )
        if result.returncode not in (0, 2):
            raise RuntimeError(
                f"kicad-cli pcb drc failed (exit {result.returncode}):\n{result.stderr}"
            )
        return json.loads(report_path.read_text(encoding="utf-8"))

    def export_board_netlist(self, pcb_path: Path) -> dict:
        # Deliberately NOT implemented here: IPC-D-356 is a fixed-column
        # legacy format, and I don't have a real sample from your
        # kicad-cli to derive the exact column offsets from -- guessing
        # them would risk a *silently wrong* board<->schematic
        # consistency check, which is worse than an explicit stub for a
        # gate whose whole job is catching subtle errors. Two ways to
        # unblock M1's verify_board():
        #   1. Run `kicad-cli pcb export ipcd356` on any board from this
        #      machine and use the real output to fill in the parsing
        #      below, or
        #   2. Parse `kicad-cli pcb export ipc2581` (XML) instead --
        #      more verbose, but self-describing, so far less likely to
        #      be silently mis-parsed than a fixed-column format guessed
        #      at second-hand.
        raise NotImplementedError(
            "export_board_netlist needs a real kicad-cli ipcd356 (or "
            "ipc2581) sample from this machine before it can be "
            "implemented safely -- see comment above. M1."
        )
