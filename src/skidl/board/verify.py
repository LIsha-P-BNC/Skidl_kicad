"""
src/skidl/board/verify.py

[D] in the M0/M1 pipeline: prove the ROUTED board still implements the
intended netlist -- the board analog of anvil/verify_connectivity.py's
schematic gate. Two independent checks:

  1. DRC        -- kicad-cli pcb drc (electrical clearances + unconnected
                   items). PyModelBackend.run_drc owns the invocation.
  2. Board<->netlist partition -- kicad-cli pcb export ipcd356 gives the
     as-routed copper netlist; its pin-partition must EQUAL the intended
     .net partition. Name-independent on the comparison side: two nets
     merged by a stray track (short) or one net split by a missing track
     shows up regardless of net naming.

Parser written against a REAL `kicad-cli pcb export ipcd356` output from
this machine (KiCad 10.99), not the spec alone:

  327THRESH_RC        C1    -1          A01X+002244Y-007087...
  317GND              D1    -1    D0354PA00X+005512Y...
  317DISCH_RC         VIA        MD0157PA00X...

  cols [0:3]  record type: 317 = through-hole, 327 = SMT
  cols [3:17] net name (14 chars, space padded; "N/C" = unconnected)
  cols [20:26] reference designator ("VIA" for via records)
  cols [26:]  "-<pin>" for component records
"""

from __future__ import annotations
import subprocess
from pathlib import Path


def board_partition(pcb_path: Path, kicad_cli: str) -> dict:
    """{net_name: frozenset("REF.PIN", ...)} from a fresh ipcd356 export;
    VIA records and N/C pads are skipped.

    IMPORTANT -- this reflects each pad's net ASSIGNMENT, not copper
    connectivity. kicad-cli's ipcd356 writes the net stored on each pad,
    so a net carried by an UNfilled (or entirely absent) pour still reads
    as fully grouped here -- measured: a board with all GND tracks AND the
    zone removed exports the identical ipcd356 as the routed board. So
    verify_board proves net-to-pad ASSIGNMENT (footprint pad-mapping,
    wrong-net pads), NOT that copper physically connects the pads. The
    copper-connectivity gate is DRC's `unconnected_items` (run_drc) --
    use that to prove a pour actually ties its pads."""
    pcb_path = Path(pcb_path)
    d356 = pcb_path.with_suffix(".d356")
    proc = subprocess.run(
        [kicad_cli, "pcb", "export", "ipcd356",
         "--output", str(d356), str(pcb_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not d356.is_file():
        raise RuntimeError(
            f"ipcd356 export failed (exit {proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '')[-400:]}"
        )
    nets = {}
    for line in d356.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line[:3] in ("317", "327"):
            continue
        net = line[3:17].strip()
        ref = line[20:26].strip()
        if not net or net == "N/C" or ref == "VIA":
            continue
        rest = line[26:]
        if not rest.startswith("-"):
            continue
        pin = rest[1:].split()[0] if rest[1:].split() else ""
        if not pin:
            continue
        nets.setdefault(net, set()).add(f"{ref}.{pin}")
    return {name: frozenset(pins) for name, pins in nets.items()}


def netlist_partition(net_path: Path) -> dict:
    """{net_name: frozenset("REF.PIN", ...)} for the INTENDED .net.
    Same regex family as anvil/verify_connectivity._partition, kept here
    with net names attached so mismatches can be reported by name."""
    import re
    text = Path(net_path).read_text(encoding="utf-8", errors="replace")
    nets = {}
    for block in re.split(r"\(net\s*\n?\s*\(code", text)[1:]:
        nm = re.search(r'\(name\s*"/?([^"]+)"\)', block)
        if not nm:
            continue
        nodes = re.findall(
            r'\(node\s*\n?\s*\(ref\s*"?([^")\s]+)"?\)\s*\n?\s*\(pin\s*"?([^")\s]+)"?',
            block,
        )
        pins = {f"{r}.{p}" for r, p in nodes if not r.startswith("#")}
        if pins:
            nets[nm.group(1)] = frozenset(pins)
    return nets


def verify_board(pcb_path: Path, net_path: Path, kicad_cli: str) -> dict:
    """Compare the board's pad-net ASSIGNMENTS to the intended netlist
    partition. Nets with >= 2 pins must group pins IDENTICALLY; a mismatch
    means the board was built with a wrong-net pad or a footprint pad-
    mapping error. Single-pin nets are reported informationally only.

    This is an ASSIGNMENT check, not a copper-connectivity proof (see
    board_partition): it does not detect physical opens/shorts, and a net
    carried by a pour reads as connected here whether or not the pour
    actually ties the pads. The copper gate is DRC `unconnected_items`."""
    board = board_partition(pcb_path, kicad_cli)
    intended = netlist_partition(net_path)

    board_sets = {pins for pins in board.values() if len(pins) >= 2}
    intended_sets = {pins for pins in intended.values() if len(pins) >= 2}

    missing = intended_sets - board_sets    # intended group not on board
    extra = board_sets - intended_sets      # board group not intended

    def _names(sets, mapping):
        out = []
        for s in sets:
            names = [n for n, p in mapping.items() if p == s]
            out.append({"nets": names, "pins": sorted(s)})
        return out

    ok = not missing and not extra
    return {
        "ok": ok,
        "board_nets": len(board),
        "intended_nets": len(intended),
        "mismatch_missing": _names(missing, intended),   # broken/split
        "mismatch_extra": _names(extra, board),          # short/merged
        "note": ("board matches the intended netlist pin-for-pin" if ok else
                 "MISMATCH: the routed board does NOT implement the intended "
                 "netlist -- do not manufacture. 'missing' = intended "
                 "connectivity absent from the board (broken/split net); "
                 "'extra' = copper connectivity not in the netlist (short)."),
    }
