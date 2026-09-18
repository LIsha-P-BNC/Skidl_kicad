# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Generate a KiCad schematic from a Circuit object (KiCad 7 tool).

De-duplicated (gap M1): this module was byte-for-byte identical to
``kicad6/gen_schematic.py``. Rather than maintain three copies of the same
~1000-line generator, kicad7 and kicad8 re-export kicad6's implementation. Only
``gen_schematic`` is part of the tool contract (the sole ``@export_to_all`` symbol
that ``kicad7/__init__.py``'s ``from .gen_schematic import *`` needs).

If a genuine KiCad-7-specific difference is ever required, replace this shim with
a real implementation (or parameterise the shared one) at that point.
"""

from skidl.tools.kicad6.gen_schematic import gen_schematic  # noqa: F401

__all__ = ["gen_schematic"]
