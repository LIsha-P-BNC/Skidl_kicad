"""verify_connectivity.py -- PROVE a generated .anvil_sch has NO unwanted (or missing)
connection.

It extracts the netlist FROM THE GENERATED SCHEMATIC (via kicad-cli) and compares its
pin-partition to the intended SKiDL netlist. The comparison is NAME-INDEPENDENT: it looks
only at how pins are grouped into nets, so an unwanted short (two nets merged) or a broken
wire (one net split) is detected regardless of the auto-generated KiCad net names.

Used by smart_schematic.build() as an in-the-loop safety check ("unwanted wire vara
kudathu"): if the wired schematic's connectivity differs from the intended netlist, the
build regenerates in all-label mode (labels never short) so a bad connection can never ship.
"""
import os
import re
import shutil
import subprocess


def _find_kicad_cli():
    """Locate the CAD app's CLI (anvil-cli.exe, with kicad-cli.exe as the legacy name)
    in the installed CAD app -- path/username/branding agnostic."""
    try:
        from open_anvilcad import _find_bin  # shared install-bin detector
        b = _find_bin()
        if b:
            for name in ("anvil-cli.exe", "kicad-cli.exe"):
                p = os.path.join(b, name)
                if os.path.isfile(p):
                    return p
    except Exception:
        pass
    return shutil.which("anvil-cli") or shutil.which("kicad-cli") or ""


KICAD_CLI = _find_kicad_cli()
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _partition(path, symmetric_refs=frozenset()):
    """Return {frozenset('REF.PIN', ...)} -- one frozenset per net with >= 2 pins.

    Single-pin / unconnected nets are ignored (a lone pin cannot be a short). Works on
    both SKiDL's `(net (code 1) (name ..) (node (ref ..)(pin ..)))` and kicad-cli's
    `(net (code "1") (name ..) ...)` (whitespace/quoting differences are tolerated).

    symmetric_refs: refs of NON-polarized 2-pin passives (plain R / C / L) whose two
    pins are electrically interchangeable. The placer may mirror such a part, swapping
    which physical pin lands on which net -- an identical circuit, but a pin-EXACT
    comparison would false-flag it as an "unwanted short + broken wire" pair. For these
    refs we canonicalize the pin to 'x' so pin1<->pin2 swaps compare equal. This ONLY
    collapses a part's OWN two pins (never merges different parts), so a genuine short
    between parts, a split net, or a reversed POLARIZED part (C_Polarized / LED / diode,
    which are NOT in symmetric_refs) is still caught.
    """
    text = open(path, encoding="utf-8", errors="replace").read()
    groups = set()
    for block in re.split(r"\(net\s+\(code", text)[1:]:
        nodes = re.findall(
            r'\(node\s+\(ref\s+"?([^")\s]+)"?\)\s*\(pin\s+"?([^")\s]+)"?', block
        )
        # refs starting with '#' (power symbols, PWR_FLAGs) are VIRTUAL: SKiDL lists
        # them in its netlist but kicad-cli never exports them, so keeping them
        # makes every power net a guaranteed false "short + broken wire" pair.
        pins = frozenset(
            f"{r}.x" if r in symmetric_refs else f"{r}.{p}"
            for r, p in nodes
            if not r.startswith("#")
        )
        if len(pins) >= 2:
            groups.add(pins)
    return groups


def _pins(path, symmetric_refs=frozenset()):
    """Return the set of every real 'REF.PIN' node in a netlist (ANY net size).

    Complements _partition (which ignores <2-pin groups, so it is blind to a
    1-pin intended net or to a symbol the renderer dropped entirely): every pin
    the intended netlist knows about must still EXIST somewhere in the
    schematic-extracted netlist. kicad-cli lists unconnected pads as
    'unconnected-(REF-PadN)' nets, so every rendered symbol pin appears here
    regardless of its connectivity. Same '#'-ref exclusion and symmetric-passive
    canonicalization as _partition.
    """
    text = open(path, encoding="utf-8", errors="replace").read()
    pins = set()
    for block in re.split(r"\(net\s+\(code", text)[1:]:
        nodes = re.findall(
            r'\(node\s+\(ref\s+"?([^")\s]+)"?\)\s*\(pin\s+"?([^")\s]+)"?', block
        )
        for r, p in nodes:
            if not r.startswith("#"):
                pins.add(f"{r}.x" if r in symmetric_refs else f"{r}.{p}")
    return pins


# KiCad Device-library symbols whose two pins are electrically interchangeable.
# Polarized/directional parts (C_Polarized, CP, LED, D, diodes, ...) are deliberately
# excluded so their pin identity (and thus polarity) is still verified exactly.
_SYMMETRIC_PASSIVE_NAMES = frozenset({
    "R", "R_Small", "R_US", "R_Small_US",
    "C", "C_Small", "C_US",
    "L", "L_Small", "L_Core_Iron", "FB", "Ferrite_Bead", "Ferrite_Bead_Small",
})


def _symmetric_refs():
    """Refs of 2-pin non-polarized passives in the live circuit (see _partition)."""
    refs = set()
    try:
        import builtins
        for p in getattr(builtins, "default_circuit", None).parts:
            name = str(getattr(p, "name", "") or "")
            if name in _SYMMETRIC_PASSIVE_NAMES and len(getattr(p, "pins", [])) == 2:
                ref = str(getattr(p, "ref", "") or "")
                if ref:
                    refs.add(ref)
    except Exception:
        pass
    return frozenset(refs)


def export_from_schematic(name):
    """Export the netlist FROM <name>.anvil_sch, or return ``None`` on failure.

    A failed export must never reuse a previous ``*_fromsch.net`` file: that
    would compare the *old* schematic and falsely approve the current one.
    """
    sch = name + ".anvil_sch"
    out = name + "_fromsch.net"
    if not os.path.exists(sch) or not os.path.exists(KICAD_CLI):
        return None
    try:
        os.remove(out)
    except FileNotFoundError:
        pass
    except OSError:
        return None
    try:
        result = subprocess.run(
            [KICAD_CLI, "sch", "export", "netlist", sch, "-o", out],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=300,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return out if os.path.isfile(out) else None


def verify(name, intended_net=None):
    """Compare schematic-extracted connectivity to the intended SKiDL netlist.

    Returns (ok, message, unwanted, missing):
      unwanted = pin-groups present in the SCHEMATIC but NOT intended (an unwanted short).
      missing  = intended pin-groups absent from the schematic (a broken wire).
    Besides the group comparison, every pin the intended netlist mentions must
    appear SOMEWHERE in the schematic-extracted netlist (pin coverage) -- this
    catches a dropped symbol or a 1-pin net the group check is blind to.
    kicad-cli is REQUIRED: without it the schematic cannot be proven correct, so
    its absence fails the verify (set ANVIL_ALLOW_UNVERIFIED=1 to accept
    unverified output on a machine that genuinely has no kicad-cli). If
    kicad-cli is available but cannot export the schematic, returns ok=False:
    approving an unverified schematic would hide an unwanted short or broken wire.
    """
    intended_net = intended_net or (name + ".net")
    if not os.path.exists(intended_net):
        return True, "verify skipped (no intended netlist)", [], []
    if not os.path.exists(KICAD_CLI):
        if os.environ.get("ANVIL_ALLOW_UNVERIFIED"):
            return True, "verify skipped (kicad-cli unavailable; ANVIL_ALLOW_UNVERIFIED set)", [], []
        return False, ("verify FAILED -- kicad-cli unavailable, cannot prove the "
                       "schematic matches the netlist (install the CAD app, or set "
                       "ANVIL_ALLOW_UNVERIFIED=1 to knowingly accept unverified output)"), [], []
    out = export_from_schematic(name)
    if not out:
        return False, "verify failed (kicad-cli could not export schematic netlist)", [], []
    sym = _symmetric_refs()
    intended = _partition(intended_net, sym)
    drawn = _partition(out, sym)
    lost = sorted(_pins(intended_net, sym) - _pins(out, sym))
    try:
        os.remove(out)
    except OSError:
        pass
    unwanted = sorted(sorted(g) for g in (drawn - intended))
    missing = sorted(sorted(g) for g in (intended - drawn))
    ok = not unwanted and not missing and not lost
    if ok:
        msg = "OK -- schematic connectivity == intended (no unwanted/missing wires)"
    else:
        msg = (
            f"MISMATCH -- {len(unwanted)} unwanted (short) + "
            f"{len(missing)} missing net-group(s)"
        )
        if lost:
            shown = ", ".join(lost[:8]) + ("..." if len(lost) > 8 else "")
            msg += f" + {len(lost)} lost pin(s) not rendered at all: {shown}"
    return ok, msg, unwanted, missing


if __name__ == "__main__":
    import sys

    nm = sys.argv[1] if len(sys.argv) > 1 else "circuit"
    ok, msg, unwanted, missing = verify(nm)
    print(msg)
    for g in unwanted:
        print("  UNWANTED (short):", g)
    for g in missing:
        print("  MISSING (broken wire):", g)
    sys.exit(0 if ok else 1)
