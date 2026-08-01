"""skidl.anvil -- Anvil CAD schematic-generation engine, bundled with SKiDL.

Portable + multi-user: on ANY machine where SKiDL is installed, a circuit script
just does::

    from skidl.anvil import anvil_libs          # sets KICAD*_SYMBOL_DIR (lazy-read by skidl)
    from skidl import *
    from skidl.anvil import smart_schematic, open_anvilcad

No per-machine `sys.path.insert`, no hardcoded username/path -- the helpers travel
with the SKiDL package.

The helper modules import each other by bare name (``import verify_connectivity``);
that keeps them runnable as stand-alone CLIs too (``python find_parts.py ...``),
where the script's own directory is on ``sys.path``. To make the same bare
imports resolve when the helpers are loaded as ``skidl.anvil.*``, we put this
subpackage directory on ``sys.path`` once, here, on first import.
"""
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
