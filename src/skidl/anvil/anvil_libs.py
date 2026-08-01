"""anvil_libs.py -- dynamically point SKiDL at the Anvil CAD symbol + footprint libs.

Import this FIRST, before `skidl`, in every circuit / helper script:

    import anvil_libs        # noqa: F401  (must precede skidl)
    from skidl import *

WHY / WHAT IT DOES (works on ANY machine, user or install path -- nothing hardcoded):
  1. Auto-finds the installed CAD app's symbol dir (Anvil CAD, ... under
     %LOCALAPPDATA%\\Programs or Program Files) -- no fixed username/path.
  2. The app stores each library as a <Lib>.kicad_symdir\\<Part>.kicad_sym FOLDER (one file
     per symbol). SKiDL/KiCad instead want ONE <Lib>.kicad_sym file per library, so we
     FLATTEN each symdir into a single library file, cached under  ~/skidl_symbols .
  3. A lib is rebuilt only when its symdir changed (mtime) -- so newly added symbols
     (e.g. L99BM114, HMC611) appear automatically after an app update, with no manual step.
  4. Sets KICAD6/7/8/9_SYMBOL_DIR (-> the cache) and _FOOTPRINT_DIR (-> the install).

SKiDL reads the KICAD*_SYMBOL_DIR env vars at import time, which is why this must run first.
"""
import glob
import os
import shutil
import sys

# Make stdout/stderr UTF-8 so printing part descriptions that contain Ω, µ, °, etc. never
# crashes with UnicodeEncodeError when captured through a pipe (MCP server / cp1252 console).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Per-user cache of flattened libraries (no hardcoded username).
CACHE = os.path.join(os.path.expanduser("~"), "skidl_symbols")
_SYMDIR_EXT = ".kicad_symdir"


def _find_install_symbols():
    """Return the CAD app's ...\\share\\kicad\\symbols dir, or None. Path/username agnostic."""
    roots = []
    for base in (os.environ.get("LOCALAPPDATA"), os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)")):
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
            sym = os.path.join(root, name, "share", "kicad", "symbols")
            if os.path.isdir(sym) and sym not in seen:
                seen.add(sym)
                cands.append((name, sym))
    if not cands:
        return None
    # Prefer Anvil/Envil branding, then any dir that actually ships .kicad_symdir libs.
    def _score(item):
        nm = item[0].upper()
        has = bool(glob.glob(os.path.join(item[1], "*" + _SYMDIR_EXT)))
        return (("ANVIL" in nm or "ENVIL" in nm), has)
    cands.sort(key=_score, reverse=True)
    return cands[0][1]


def _newest_mtime(d):
    """Newest mtime of the dir itself or any file directly inside it."""
    m = os.path.getmtime(d)
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    mt = e.stat().st_mtime
                except OSError:
                    continue
                if mt > m:
                    m = mt
    except OSError:
        pass
    return m


def _extract_symbols(txt):
    """Yield every TOP-LEVEL (symbol ...) block from a .kicad_sym text via paren matching.

    Robust to files with or without the (kicad_symbol_lib ...) wrapper and to nested
    sub-unit symbols (e.g. "X_0_1"), and ignores parens inside quoted strings.
    """
    out, n, i, key = [], len(txt), 0, "(symbol "
    while True:
        s = txt.find(key, i)
        if s == -1:
            break
        depth, j, in_str, esc = 0, s, False, False
        while j < n:
            c = txt[j]
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
                    out.append(txt[s:j + 1])
                    break
            j += 1
        i = j + 1  # continue AFTER this whole block -> skips its nested sub-symbols
    return out


def _flatten_symdir(symdir, out_file):
    """Merge <Lib>.kicad_symdir/*.kicad_sym (one symbol each) into one library file."""
    blocks = []
    for f in sorted(glob.glob(os.path.join(symdir, "*.kicad_sym"))):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                blocks.extend(_extract_symbols(fh.read()))
        except OSError:
            continue
    body = "\n".join("\t" + b.replace("\n", "\n\t") for b in blocks)
    content = ('(kicad_symbol_lib\n\t(version 20251024)\n'
               '\t(generator "anvil_libs")\n\t(generator_version "9.0")\n'
               + body + "\n)\n")
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, out_file)  # atomic: never leaves a half-written lib


def _sync(src):
    """Rebuild the cache from the install's symdirs (only the stale ones)."""
    os.makedirs(CACHE, exist_ok=True)
    for symdir in glob.glob(os.path.join(src, "*" + _SYMDIR_EXT)):
        lib = os.path.basename(symdir)[:-len(_SYMDIR_EXT)]
        out = os.path.join(CACHE, lib + ".kicad_sym")
        if (not os.path.exists(out)) or _newest_mtime(symdir) > os.path.getmtime(out):
            try:
                _flatten_symdir(symdir, out)
            except OSError:
                pass
    # Copy any plain <Lib>.kicad_sym that sit directly in the install symbols dir.
    for f in glob.glob(os.path.join(src, "*.kicad_sym")):
        out = os.path.join(CACHE, os.path.basename(f))
        if (not os.path.exists(out)) or os.path.getmtime(f) > os.path.getmtime(out):
            try:
                shutil.copy2(f, out)
            except OSError:
                pass


_install_sym = _find_install_symbols()
if _install_sym:
    try:
        _sync(_install_sym)
    except OSError:
        pass

# Exported paths (other helper scripts import these).
SYMBOLS = CACHE if os.path.isdir(CACHE) else None
FOOTPRINTS = None
if _install_sym:
    _fp = os.path.join(os.path.dirname(_install_sym), "footprints")
    if os.path.isdir(_fp):
        FOOTPRINTS = _fp

if SYMBOLS:
    for _v in ("KICAD6_SYMBOL_DIR", "KICAD7_SYMBOL_DIR",
               "KICAD8_SYMBOL_DIR", "KICAD9_SYMBOL_DIR"):
        os.environ[_v] = SYMBOLS

if FOOTPRINTS:
    for _v in ("KICAD6_FOOTPRINT_DIR", "KICAD7_FOOTPRINT_DIR",
               "KICAD8_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR"):
        os.environ[_v] = FOOTPRINTS
