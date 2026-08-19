"""
src/skidl/board/route/dsn_export.py

[C] first leg: serialize a placed BoardModel to a Specctra .dsn file for
FreeRouting. Emitted directly from BoardModel -- never by re-parsing the
.anvil_pcb -- so the board file and the DSN can't drift apart (one model,
two serializers).

Conventions (matching what KiCad's own exporter feeds FreeRouting):
  * (resolution um 10): all coordinates in 0.1 um integer units.
  * Specctra is Y-UP, KiCad/BoardModel is Y-DOWN: every emitted Y is
    negated here, and ses_import.py negates back.
  * SMD padstacks carry their copper shape on F.Cu only -- FreeRouting
    mirrors shapes itself for components placed on the back side.
    Through-hole padstacks carry shapes on both copper layers.
  * Pad rotation support is 0/90/180/270 via width/height swap in the
    padstack (a rotated-rect padstack per angle bucket). Odd angles fall
    back to a conservative bounding circle.
"""

from __future__ import annotations
from pathlib import Path

from skidl.board.model import BoardModel, Footprint, Pad

U = 10000  # DSN units per mm  (resolution um 10  ->  0.1 um units)


def _u(mm: float) -> int:
    return int(round(mm * U))


def _q(s: str) -> str:
    """Quote a DSN string (freerouting tolerates quotes everywhere)."""
    return '"' + str(s).replace('"', "'") + '"'


def export_dsn(board: BoardModel, out_path: Path, exclude=None) -> Path:
    """Serialize the placed board to a Specctra .dsn for FreeRouting.

    `exclude`: net names to leave OUT of the routing problem because they
    are carried by a copper pour instead (see board/pour.py). Default
    None = exclude nothing (every net is routed, the original behavior).
    """
    out_path = Path(out_path)
    exclude = set(exclude or ())
    default_rules = board.net_classes.get("Default", {})
    width_u = _u(default_rules.get("width", 0.25))
    # GUARD BAND: route at +0.05 mm over the DRC clearance so the
    # router's small slips in dense pad fields (measured: uniform ~0.15
    # actuals against a 0.2 rule around 0.5 mm-pitch USB-C) still land
    # above what KiCad checks.
    drc_clearance = default_rules.get("clearance", 0.15)
    clear_u = _u(drc_clearance + 0.05)
    via_size = default_rules.get("via_size", 0.8)
    via_drill = default_rules.get("via_drill", 0.4)

    # Copper stack from the board's ACTUAL layer count (Board Setup is
    # the source of truth): 2 -> F/B, 4 -> F/In1/In2/B, etc.
    n = max(2, int(board.layer_count or 2))
    copper_layers = (["F.Cu"]
                     + [f"In{i}.Cu" for i in range(1, n - 1)]
                     + ["B.Cu"])
    via_name = f"Via[0-{n-1}]_{int(via_size*1000)}:{int(via_drill*1000)}_um"

    images, padstacks, pin_variants = _collect_images_and_padstacks(
        board, copper_layers)
    ref_image = {fp.ref: _image_name(fp) for fp in board.footprints}

    lines = []
    a = lines.append
    a(f"(pcb {_q(board.name)}")
    a("  (parser")
    a('    (string_quote ")')
    a("    (space_in_quoted_tokens on)")
    # host_cad/version matter: FreeRouting parses them and switches into a
    # degraded "old KiCad" compatibility mode for anything it can't read
    # as KiCad >= 6 (observed: vias never used, routes left incomplete).
    a('    (host_cad "KiCad")')
    a('    (host_version "8.0.0")')
    a("  )")
    a("  (resolution um 10)")
    a("  (unit um)")
    a("  (structure")
    for i, layer in enumerate(copper_layers):
        a(f"    (layer {layer}")
        a("      (type signal)")
        a(f"      (property (index {i}))")
        a("    )")
    a("    (boundary")
    a("      " + _boundary_path(board))
    a("    )")
    # NPTH pads (mounting holes) carry no net and no pin in the images --
    # emit hard keepouts so the router can't run copper across the hole.
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.pad_type != "np_thru_hole":
                continue
            wx, wy = fp.pad_world_xy(pad)
            dia = max(pad.size_x, pad.drill) + 0.4  # hole + margin
            for layer in copper_layers:
                a(f"    (keepout \"\" (circle {layer} {_u(dia)} {_u(wx)} {_u(-wy)}))")
    # User/RF rule areas: polygon keepouts per copper layer so the
    # router obeys the same regions KiCad's rule-area zones enforce.
    for ko in getattr(board, "keepouts", []):
        if not ko.outline:
            continue
        layers = (copper_layers if any(l in ("*.Cu", "*") for l in ko.layers)
                  else [l for l in ko.layers if l in copper_layers])
        pts = " ".join(f"{_u(x)} {_u(-y)}" for x, y in ko.outline)
        for layer in layers:
            if ko.no_tracks:
                a(f'    (keepout "" (polygon {layer} 0 {pts}))')
            elif ko.no_vias:
                a(f'    (via_keepout "" (polygon {layer} 0 {pts}))')
    a(f"    (via {_q(via_name)})")
    a("    (rule")
    a(f"      (width {width_u})")
    a(f"      (clearance {clear_u})")
    a(f"      (clearance {clear_u} (type default_smd))")
    # smd_smd gets the FULL clearance too: relaxing it (as KiCad's own
    # exporter does) lets the router pack different-net stubs between
    # fine-pitch array pads closer than KiCad's DRC allows -> clearance
    # errors on import (measured on an R_Cat16 dual-array).
    a(f"      (clearance {clear_u} (type smd_smd))")
    a("    )")
    a("  )")

    # -- placement --------------------------------------------------------
    a("  (placement")
    by_image = {}
    for fp in board.footprints:
        by_image.setdefault(_image_name(fp), []).append(fp)
    for image_name, fps in by_image.items():
        a(f"    (component {_q(image_name)}")
        for fp in fps:
            side = "front" if fp.side == "top" else "back"
            # Rotation sign: KiCad rotates with [c,s;-s,c] (Y-down); the
            # DSN mirror M=diag(1,-1) conjugates it to the standard Y-up
            # CCW matrix at the SAME angle (M*R_k(t)*M = R_std(t)), so the
            # angle passes through unchanged.
            a(f"      (place {_q(fp.ref)} {_u(fp.x)} {_u(-fp.y)} {side} {fp.rotation:g})")
        a("    )")
    a("  )")

    # -- library ----------------------------------------------------------
    a("  (library")
    for image_name, pin_lines in images.items():
        a(f"    (image {_q(image_name)}")
        for pl in pin_lines:
            a("      " + pl)
        a("    )")
    for ps_name, shape_lines in padstacks.items():
        a(f"    (padstack {_q(ps_name)}")
        for sl in shape_lines:
            a("      " + sl)
        a("      (attach off)")
        a("    )")
    # The via padstack.
    a(f"    (padstack {_q(via_name)}")
    for layer in copper_layers:
        a(f"      (shape (circle {layer} {_u(via_size)}))")
    a("      (attach off)")
    a("    )")
    a("  )")

    # -- network ----------------------------------------------------------
    a("  (network")
    for net in board.nets.values():
        if len(net.pad_refs) < 2:
            continue  # a 1-pin net has nothing to route
        if net.name in exclude:
            continue  # carried by a copper pour, not routed
        # A pad number shared by several physical pads (SIM shield "SH")
        # expands to every uniquified pin id -- the router must tie them
        # all, exactly as KiCad's connectivity sees them.
        expanded = []
        for ref, pin in net.pad_refs:
            var = pin_variants.get(ref_image.get(ref), {}).get(pin)
            for p in (var or [pin]):
                expanded.append(f"{ref}-{p}")
        pins = " ".join(dict.fromkeys(expanded))
        a(f"    (net {_q(net.name)}")
        a(f"      (pins {pins})")
        a("    )")
    # One routing class per net class, carrying its rules.
    for nc_name, rules in board.net_classes.items():
        members = [n for n in board.nets.values()
                   if n.net_class == nc_name and len(n.pad_refs) >= 2
                   and n.name not in exclude]
        if not members and nc_name != "Default":
            continue
        names = " ".join(_q(n.name) for n in members)
        a(f"    (class {_q('kicad_' + nc_name)} {names}")
        a(f"      (circuit (use_via {_q(via_name)}))")
        a("      (rule")
        a(f"        (width {_u(rules.get('width', 0.25))})")
        # Same guard band as the global rule.
        a(f"        (clearance {_u(rules.get('clearance', 0.15) + 0.05)})")
        a("      )")
        a("    )")
    a("  )")

    a("  (wiring)")
    a(")")

    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def _boundary_path(board: BoardModel, edge_clearance: float = 0.5) -> str:
    outline = board.outline or [(0, 0), (100, 0), (100, 100), (0, 100)]
    # Inset the routable area by the copper-to-edge clearance: the DSN has
    # no way to express KiCad's edge-clearance constraint, so without this
    # the router parks vias 0.39mm from the outline and DRC rejects the
    # board (measured twice at the identical spot on the tracker).
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    lo_x, hi_x = min(xs) + edge_clearance, max(xs) - edge_clearance
    lo_y, hi_y = min(ys) + edge_clearance, max(ys) - edge_clearance
    inset = [(min(max(x, lo_x), hi_x), min(max(y, lo_y), hi_y))
             for x, y in outline]
    pts = list(inset) + [inset[0]]  # closed
    coords = " ".join(f"{_u(x)} {_u(-y)}" for x, y in pts)
    return f"(path pcb 0 {coords})"


def _image_name(fp: Footprint) -> str:
    return fp.lib_id.replace('"', "'")


def _collect_images_and_padstacks(board: BoardModel, copper_layers=("F.Cu", "B.Cu")):
    """One image per distinct lib_id, one padstack per distinct pad
    geometry. Returns ({image_name: [pin lines]}, {ps_name: [shape lines]})."""
    images = {}
    padstacks = {}

    pin_variants = {}   # image_name -> {pad_number: [unique ids]}
    for fp in board.footprints:
        image_name = _image_name(fp)
        if image_name in images:
            continue
        pin_lines = []
        nc_idx = 0
        seen_nums = {}
        variants = {}
        numbered = [p for p in fp.pads if p.number]
        for pad in fp.pads:
            if pad.pad_type == "np_thru_hole":
                continue  # mounting hole: no copper connection to route
            num = pad.number
            if not num:
                # Unnumbered pads exist in real libraries (e.g. the 4
                # anchor pads forming a TO-263 tab). An EMPTY pin id
                # shifts freerouting's token stream -> "DSN structure
                # parsing failed" (measured). If the pad OVERLAPS a
                # numbered pad it is the same physical copper -- drop it
                # (emitting it as a foreign obstacle sits ON the router's
                # target and makes the pin unroutable, measured on the
                # LM2596 tab). Isolated ones become unique obstacles.
                if any(abs(pad.x - p.x) < (pad.size_x + p.size_x) / 2 and
                       abs(pad.y - p.y) < (pad.size_y + p.size_y) / 2
                       for p in numbered):
                    continue
                nc_idx += 1
                num = f"SKIDLNC{nc_idx}"
            else:
                # DSN pin ids must be UNIQUE within an image. Real
                # libraries repeat a number for multi-pad pins (the JAE
                # SIM socket has NINE "SH" shield pads) -- duplicates
                # confused freerouting into overlapping micro-jog wires
                # (measured: the tracker's one stubborn sliver junction).
                # Uniquify as SH, SH@2, ... and record the variants so
                # the net pin list can name every physical pad.
                k = seen_nums.get(num, 0) + 1
                seen_nums[num] = k
                base_num = num
                if k > 1:
                    num = f"{num}@{k}"
                variants.setdefault(base_num, []).append(num)
            ps_name = _padstack_for(pad, padstacks, copper_layers)
            pin_lines.append(
                f"(pin {_q(ps_name)} {num} {_u(pad.x)} {_u(-pad.y)})"
            )
        images[image_name] = pin_lines
        pin_variants[image_name] = {n: v for n, v in variants.items()
                                    if len(v) > 1}

    return images, padstacks, pin_variants


def _padstack_for(pad: Pad, padstacks: dict, copper_layers=("F.Cu", "B.Cu")) -> str:
    """Get-or-create the padstack for this pad's geometry; returns its name."""
    w, h = pad.size_x, pad.size_y
    rot = pad.rotation % 360.0
    if rot in (90.0, 270.0):
        w, h = h, w
        rot = 0.0

    through = pad.pad_type == "thru_hole"
    # Through-hole pads exist on EVERY copper layer of the stack.
    layers = list(copper_layers) if through else ["F.Cu"]

    if pad.shape == "circle" or (rot not in (0.0, 180.0)):
        # Odd rotations: conservative bounding circle.
        d = max(w, h) if pad.shape != "circle" else w
        name = f"Round[{'A' if through else 'T'}]Pad_{int(d*1000)}_um"
        shapes = [f"(shape (circle {l} {_u(d)}))" for l in layers]
    elif pad.shape == "oval":
        # Stadium: a path of width min(w,h) along the long axis.
        if w >= h:
            half = (w - h) / 2.0
            path = f"{_u(-half)} 0 {_u(half)} 0"
            width = h
        else:
            half = (h - w) / 2.0
            path = f"0 {_u(-half)} 0 {_u(half)}"
            width = w
        name = f"Oval[{'A' if through else 'T'}]Pad_{int(w*1000)}x{int(h*1000)}_um"
        shapes = [f"(shape (path {l} {_u(width)} {path}))" for l in layers]
    else:
        # rect / roundrect / custom -> rectangle.
        name = f"Rect[{'A' if through else 'T'}]Pad_{int(w*1000)}x{int(h*1000)}_um"
        shapes = [
            f"(shape (rect {l} {_u(-w/2)} {_u(-h/2)} {_u(w/2)} {_u(h/2)}))"
            for l in layers
        ]

    if name not in padstacks:
        padstacks[name] = shapes
    return name
