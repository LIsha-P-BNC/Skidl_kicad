"""open_anvilcad.py -- open a generated design in the Anvil CAD application.

    python open_anvilcad.py <name|path>          # open <name>.anvil_pro (project view)
    python open_anvilcad.py <name|path> --sch     # open <name>.anvil_sch (schematic editor)
    python open_anvilcad.py <name|path> --pcb     # open <name>.anvil_pcb (board editor)

or import it from a circuit script:

    import open_anvilcad
    open_anvilcad.open_project("my_circuit")   # after smart_schematic.build()

<name> may be a bare name OR a full path (with or without extension). The install location
is auto-detected, so this works on ANY machine/user -- nothing is hardcoded.

The project view is best: from there you open the schematic, and Pcbnew -> "Update PCB
from Schematic" (F8) builds the board with the footprints SKiDL already assigned.
"""
import glob
import os
import subprocess
import sys

_EXTS = (".anvil_pro", ".anvil_sch", ".anvil_pcb", ".net", ".py")
# Project-manager exe names, most-preferred first (branding changed over versions).
_PM_NAMES = ("anvilcad.exe", "envilcad.exe", "kicad.exe")


def _find_bin():
    """Return the CAD app's bin dir, or None. Path/username/branding agnostic.

    MCP clients (Claude Desktop) spawn the server with a STRIPPED env where
    LOCALAPPDATA / ProgramFiles are often missing -- which used to make the
    app un-findable and auto-open silently fail. So derive those roots from
    USERPROFILE / SystemDrive when they are absent (nothing hardcoded)."""
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    sysdrive = os.environ.get("SystemDrive") or "C:"
    localapp = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    progfiles = os.environ.get("ProgramFiles") or os.path.join(sysdrive + os.sep, "Program Files")
    progfiles86 = os.environ.get("ProgramFiles(x86)") or os.path.join(sysdrive + os.sep, "Program Files (x86)")
    roots = []
    for base in (localapp, progfiles, progfiles86):
        if base:
            roots += [os.path.join(base, "Programs"), base]
    cands, seen = [], set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if "CAD" not in name.upper():
                continue
            b = os.path.join(root, name, "bin")
            if b in seen or not os.path.isdir(b):
                continue
            # must actually host a KiCad-style toolchain
            if os.path.isfile(os.path.join(b, "eeschema.exe")) or \
               any(os.path.isfile(os.path.join(b, pm)) for pm in _PM_NAMES):
                seen.add(b)
                cands.append((name, b))
    if not cands:
        return None
    cands.sort(key=lambda it: ("ANVIL" in it[0].upper() or "ENVIL" in it[0].upper()),
               reverse=True)
    return cands[0][1]


_BIN = _find_bin()


def _exe(*names):
    """First existing exe among `names` in the detected bin dir."""
    if not _BIN:
        return None
    for n in names:
        p = os.path.join(_BIN, n)
        if os.path.isfile(p):
            return p
    return None


def _base(name):
    """Strip any known extension so 'foo', 'foo.py', 'foo.anvil_pro' all become 'foo'."""
    for e in _EXTS:
        if name.endswith(e):
            return name[: -len(e)]
    return name


def _launch(exe, target):
    if not exe:
        print("!! CAD executable not found (is Anvil CAD installed?)")
        return False
    if not os.path.exists(target):
        print(f"!! file not found: {target}  (generate it first)")
        return False
    subprocess.Popen([exe, os.path.abspath(target)], close_fds=True)
    print(f">>> opening: {os.path.basename(target)}")
    return True


def open_project(name):
    return _launch(_exe(*_PM_NAMES), _base(name) + ".anvil_pro")


def open_schematic(name):
    return _launch(_exe("eeschema.exe", *_PM_NAMES), _base(name) + ".anvil_sch")


def open_pcb(name):
    return _launch(_exe("pcbnew.exe", *_PM_NAMES), _base(name) + ".anvil_pcb")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    name = args[0]
    if "--sch" in args:
        open_schematic(name)
    elif "--pcb" in args:
        open_pcb(name)
    else:
        open_project(name)


if __name__ == "__main__":
    main()
