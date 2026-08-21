# -*- coding: utf-8 -*-

# The MIT License (MIT) - Copyright (c) Dave Vandenbout.

"""
Constants used when generating schematics.
"""

import os

# Constants for KiCad.
GRID = 50
PIN_LABEL_FONT_SIZE = 50
BOX_LABEL_FONT_SIZE = 50


def _env_pad(name, default_grid_mult):
    """Block padding in mils, env-overridable so spacing stays dynamic/scale-free.

    The value is expressed as a multiple of GRID so it always lands on the routing
    grid. Env var (SKIDL_BLK_INT_PAD / SKIDL_BLK_EXT_PAD) is read in MILS and snapped
    down to a whole GRID multiple; falls back to default_grid_mult * GRID.
    """
    raw = os.environ.get(name)
    if raw:
        try:
            mils = float(raw)
            if mils > 0:
                return max(GRID, int(round(mils / GRID)) * GRID)
        except ValueError:
            pass
    return default_grid_mult * GRID


# Gap INSIDE a functional block (between parts of the same block).
BLK_INT_PAD = _env_pad("SKIDL_BLK_INT_PAD", 2)   # default 100 mil
# Gap BETWEEN functional blocks -- deliberately larger than BLK_INT_PAD so blocks
# read as distinct groups instead of one cramped cluster. Was 2*GRID (== INT_PAD),
# which made blocks touch; widened to 8*GRID (400 mil).
BLK_EXT_PAD = _env_pad("SKIDL_BLK_EXT_PAD", 8)   # default 400 mil
DRAWING_BOX_RESIZE = 100
HIER_TERM_SIZE = 50
