"""sanitize_sch.py -- fix bad full-path symbol lib_ids in generated .anvil_sch files.

SKiDL sometimes writes a symbol's library nickname as the FULL FILE PATH, e.g.
    (symbol "C:\\Users\\Admin\\skidl_symbols\\Regulator_Linear:L7805" ...)
    (lib_id "C:\\Users\\Admin\\skidl_symbols\\Regulator_Linear:L7805")
(this happens for parts loaded via the "backup library" fallback). KiCad/Anvil CAD then
refuses to open the schematic: "Symbol ... contains invalid character 'C'" (the drive
colon / backslashes are illegal in a lib_id). This strips the path back to the proper
nickname:  "Regulator_Linear:L7805".

Only quoted strings that START WITH a drive letter (e.g. C:\\) are touched, so real
lib_ids, footprints and sheet names are never affected.

Use:
    import sanitize_sch
    sanitize_sch.fix_project("atmega_board")   # fixes <name>.anvil_sch + <name>_*.anvil_sch
or:  python sanitize_sch.py atmega_board
"""
import glob
import os
import re
import sys

# a quoted string that begins with a Windows absolute path, e.g. "C:\...\Lib:Part"
_BADPATH = re.compile(r'"([A-Za-z]:\\[^"]+)"')


def _basename(match):
    # keep only the last path component: "...\Regulator_Linear:L7805" -> "Regulator_Linear:L7805"
    return '"' + match.group(1).replace("/", "\\").split("\\")[-1] + '"'


def fix_file(path):
    """Rewrite one .anvil_sch in place. Returns number of refs fixed."""
    with open(path, encoding="utf-8") as f:
        txt = f.read()
    new, n = _BADPATH.subn(_basename, txt)
    if n:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new)
    return n


def fix_project(name):
    """Fix the root sheet <name>.anvil_sch and every child sheet <name>_*.anvil_sch."""
    base = name[:-10] if name.endswith(".anvil_sch") else name
    total = 0
    for path in glob.glob(base + ".anvil_sch") + glob.glob(base + "_*.anvil_sch"):
        n = fix_file(path)
        if n:
            total += n
            print(f"  sanitized {n:2d} lib_id refs in {os.path.basename(path)}")
    return total


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python sanitize_sch.py <project_name>")
    else:
        t = fix_project(sys.argv[1])
        print(f"done: {t} refs fixed" if t else "nothing to fix (already clean)")
