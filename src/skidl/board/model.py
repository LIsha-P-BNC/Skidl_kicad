"""
src/skidl/board/model.py

In-memory board representation shared by every stage of the pure-python
board pipeline: pcb_writer (populate) -> place (mutate x/y/rotation) ->
route (dsn_export / ses_import) -> verify -> manufacture.

No KiCad types (pcbnew.*, kipy.*) appear here, or in anything that only
depends on this module -- that split is the whole point of BoardBackend
(see adapter/base.py). Units are millimetres and degrees throughout.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Pad:
    number: str                 # "1", "2", "A1", ... as printed on the footprint
    net: Optional[str]          # net name, or None if unconnected (mounting holes etc.)
    x: float                    # LOCAL offset from the footprint's own origin, mm
    y: float
    size_x: float
    size_y: float
    shape: str = "roundrect"    # "circle" | "rect" | "roundrect" | "oval" | "custom"
    pad_type: str = "smd"       # "smd" | "thru_hole" | "np_thru_hole"
    layers: tuple = ("F.Cu", "F.Paste", "F.Mask")
    drill: float = 0.0          # mm, 0 for SMD
    drill_y: float = 0.0        # mm, second axis of an oval drill; 0 = circular
    rotation: float = 0.0       # degrees, the pad's own LOCAL rotation from the
                                # .kicad_mod (at x y ROT); board files emit
                                # footprint rotation + this (KiCad convention)
    roundrect_rratio: float = 0.25


@dataclass
class Footprint:
    ref: str                          # "U1", "R4", ...
    lib_id: str                       # "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    value: str = ""
    x: float = 0.0                    # BOARD-space, mm -- meaningless until place/* runs
    y: float = 0.0
    rotation: float = 0.0             # degrees, KiCad convention (CCW)
    side: str = "top"                 # "top" -> F.Cu, "bottom" -> B.Cu
    pads: list = field(default_factory=list)          # list[Pad], LOCAL coords
    courtyard: Optional[tuple] = None                  # (minx, miny, maxx, maxy) LOCAL, mm
    locked: bool = False
    dnp: bool = False
    source_text: Optional[str] = None                  # raw .kicad_mod text; when set,
                                                       # pcb_writer embeds it verbatim
                                                       # (real silk/fab/courtyard art)

    @property
    def layer(self) -> str:
        return "F.Cu" if self.side == "top" else "B.Cu"

    def pad_world_xy(self, pad: Pad) -> tuple:
        """Pad position in board space, honoring this footprint's
        placement and rotation (and mirroring if on the bottom side).
        Uses KiCad's rotation matrix [c, s; -s, c] (Y-down, positive =
        CCW on screen) so model geometry matches the .kicad_pcb exactly."""
        lx, ly = pad.x, pad.y
        if self.side == "bottom":
            lx = -lx
        rad = math.radians(self.rotation)
        rx = lx * math.cos(rad) + ly * math.sin(rad)
        ry = -lx * math.sin(rad) + ly * math.cos(rad)
        return (self.x + rx, self.y + ry)

    def courtyard_world_bbox(self) -> Optional[tuple]:
        """Axis-aligned world-space bbox, accounting for rotation (so a
        45deg-rotated part still gets a conservative non-overlap check
        in place/simple.py)."""
        if self.courtyard is None:
            return None
        minx, miny, maxx, maxy = self.courtyard
        corners = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]
        rad = math.radians(self.rotation)
        cos_r, sin_r = math.cos(rad), math.sin(rad)
        world = []
        for cx, cy in corners:
            if self.side == "bottom":
                cx = -cx
            # KiCad rotation matrix (see pad_world_xy).
            wx = self.x + cx * cos_r + cy * sin_r
            wy = self.y - cx * sin_r + cy * cos_r
            world.append((wx, wy))
        xs = [p[0] for p in world]
        ys = [p[1] for p in world]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class Net:
    name: str
    code: int
    net_class: str = "Default"
    pad_refs: list = field(default_factory=list)   # list[(ref, pad_number)]
    pin_types: dict = field(default_factory=dict)  # (ref, pad_number) -> netlist
                                                   # pintype ("INPUT"/"OUTPUT"/
                                                   # "PASSIVE"/...), used by the
                                                   # rule placer for flow direction


@dataclass
class Track:
    net: str
    layer: str
    start: tuple           # (x, y) mm, board space
    end: tuple
    width: float


@dataclass
class Via:
    net: str
    at: tuple
    size: float
    drill: float
    layers: tuple = ("F.Cu", "B.Cu")


@dataclass
class Zone:
    """A copper pour (e.g. a GND plane). Outline only -- the fill polygons
    are computed by kicad-cli (drc --refill-zones --save-board). Field names
    map to KiCad's zone s-expression; `net` is the NAME (pcb_writer resolves
    the net index at write time)."""
    net: str                    # net NAME, e.g. "GND"
    layer: str = "B.Cu"
    outline: list = field(default_factory=list)   # [(x, y), ...] mm
    priority: int = 0           # KiCad: HIGHER priority wins fill overlaps
    connect_pads: str = "yes"   # "yes" = solid (default; thermal reliefs starve
                                # on auto-routed boards), "thermal_reliefs", "no"
    clearance: float = 0.3
    min_thickness: float = 0.25
    thermal_gap: float = 0.5
    thermal_bridge_width: float = 0.5


@dataclass
class PadConnectOverride:
    """Per-pad zone-connection override, applied on the PAD inside its
    footprint block (a separate serialization path from the zone). e.g.
    through-hole pads -> thermal relief for hand-solderability."""
    footprint_ref: str          # "U1"
    pad_number: str             # matches Pad.number
    zone_connect: int           # 0 = none, 1 = thermal relief, 2 = solid


@dataclass
class KeepoutArea:
    """A rule area: region where tracks/vias/pour/parts are forbidden.
    Written to KiCad as a keepout zone (the app then respects it on
    refill) and to the DSN as polygon keepouts so the router obeys it
    too. `source` records WHY it exists (mechanical spec, RF rule...)."""
    name: str                                  # "antenna", "enclosure_boss_1", ...
    outline: list = field(default_factory=list)   # [(x, y), ...] mm, board space
    layers: tuple = ("F.Cu", "B.Cu")           # copper layers it applies to
    no_tracks: bool = True
    no_vias: bool = True
    no_pour: bool = True
    no_footprints: bool = False                # placement obstacle when True
    source: str = "mechanical"                 # "mechanical" | "rf" | "auto"

    def bbox(self):
        xs = [p[0] for p in self.outline]
        ys = [p[1] for p in self.outline]
        return min(xs), min(ys), max(xs), max(ys)


@dataclass
class BoardModel:
    name: str
    footprints: list = field(default_factory=list)     # list[Footprint]
    nets: dict = field(default_factory=dict)             # name -> Net
    net_classes: dict = field(default_factory=dict)      # name -> dict(width/clearance/via_*, mm)
    tracks: list = field(default_factory=list)           # list[Track], post-route (M1)
    vias: list = field(default_factory=list)             # list[Via], post-route (M1)
    zones: list = field(default_factory=list)            # list[Zone] copper pours
    keepouts: list = field(default_factory=list)         # list[KeepoutArea] rule areas
    outline: Optional[list] = None                        # [(x,y), ...] mm; None = not sized yet
    layer_count: int = 2
    thickness: float = 1.6                                # board thickness, mm
    warnings: list = field(default_factory=list)          # human-readable notes a stage
                                                          # couldn't act on silently (e.g. a
                                                          # pad with no legal stitching-via
                                                          # spot); logged before the DRC gate
    pad_overrides: list = field(default_factory=list)     # list[PadConnectOverride];
                                                          # applied in each pad's block at write

    def footprint(self, ref: str) -> Footprint:
        for fp in self.footprints:
            if fp.ref == ref:
                return fp
        raise KeyError(f"{self.name}: no footprint with ref {ref!r}")

    def get_or_add_net(self, name: str, net_class: str = "Default") -> Net:
        if name not in self.nets:
            self.nets[name] = Net(name=name, code=len(self.nets) + 1, net_class=net_class)
        return self.nets[name]
