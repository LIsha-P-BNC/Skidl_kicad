"""
src/skidl/board/adapter/base.py

BoardBackend is the one seam between "AI reasoning about a board" (rules,
placement heuristics, routing strategy -- everything upstream) and "how
a board actually gets read/written/checked" (pure-python today; the
pcbnew SWIG bindings or the KiCad IPC API later, per Part 2 of the
M0/M1 spec).

Nothing outside adapter/*.py should ever import pcbnew, kipy, or shell
out to kicad-cli directly -- go through a BoardBackend method instead,
so swapping PyModelBackend for a future IpcApiBackend / PcbnewSwigBackend
is a one-line change at the call site, not a rewrite of place/rules.py
or route/*.py.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from pathlib import Path

from skidl.board.model import BoardModel


class BoardBackend(ABC):
    """One implementation per KiCad access strategy. See pymodel.py (v1,
    ships in M0/M1); swig.py / ipc.py are deferred stubs -- Part 2 of the
    spec explicitly puts those "not before M5"."""

    @abstractmethod
    def populate(self, board: BoardModel, netlist_path: Path) -> BoardModel:
        """Parse a .net file plus the footprint library set into `board`:
        one Footprint (with local-coordinate Pads) per component, one Net
        per net, no real placement yet. Must raise a clear, actionable
        error listing every unresolved "Lib:Name" footprint reference
        rather than failing on the first one (UnresolvedFootprintsError,
        pcb_writer.py)."""

    @abstractmethod
    def apply_placement(self, board: BoardModel) -> None:
        """Push board.footprints[i].x/y/rotation (already decided by
        place/rules.py or place/simple.py) into whatever the backend
        needs internally before writing/routing. For PyModelBackend this
        is a no-op today -- the dataclasses already ARE the source of
        truth -- but the seam exists because a pcbnew-backed
        implementation would need to mutate real pcbnew.FOOTPRINT
        objects here instead."""

    @abstractmethod
    def apply_net_classes(self, board: BoardModel) -> None:
        """Push board.net_classes (trace width / clearance / via size per
        class, sourced from skidl.design_class.NetClass) onto the model
        so write_pcb / export_dsn can consume them."""

    @abstractmethod
    def write_pcb(self, board: BoardModel, out_path: Path) -> Path:
        """Serialize `board` to a KiCad .kicad_pcb file at out_path."""

    @abstractmethod
    def export_dsn(self, board: BoardModel, out_path: Path) -> Path:
        """Serialize `board` (post-placement) to a Specctra .dsn file for
        FreeRouting. M1 (route/dsn_export.py)."""

    @abstractmethod
    def import_ses(self, board: BoardModel, ses_path: Path) -> None:
        """Read a FreeRouting .ses session file and fold (wire ...) /
        (via ...) entries back into board.tracks / board.vias. M1
        (route/ses_import.py)."""

    @abstractmethod
    def run_drc(self, pcb_path: Path) -> dict:
        """Shell `kicad-cli pcb drc --format json` against pcb_path and
        return the parsed report. This is the one place a BoardBackend
        is expected to shell out to kicad-cli rather than staying pure
        python -- DRC itself is KiCad's job, not ours to reimplement."""

    @abstractmethod
    def export_board_netlist(self, pcb_path: Path) -> dict:
        """Return a net -> {(ref, pad), ...} partition for the *board*,
        in the same shape anvil/verify_connectivity.py._partition
        expects for the schematic, so verify_board() (M1) can diff the
        two and reject a board whose routed connectivity drifted from
        the intended netlist."""
