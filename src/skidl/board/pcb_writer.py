"""
src/skidl/board/pcb_writer.py

[A] in the M0/M1 pipeline: <name>.net (SKiDL/KiCad netlist) + footprint
library -> populated BoardModel -> <name>.anvil_pcb.

Net classes: modern KiCad (6+) stores net-class *rules* in the project's
.anvil_pro JSON (net_settings.classes), NOT in the board file -- a
(net_class ...) block inside .anvil_pcb is a KiCad-5-ism the KiCad-10
parser rejects. So write_pcb() emits no class blocks; call
update_project_net_classes() to fold BoardModel.net_classes into the
.anvil_pro the schematic build already produced. Per-net class NAMES come
straight from the netlist's own (class "...") field.

.anvil_pcb writing is hand-templated rather than a generic s-expression
writer -- see sexp.py's docstring for why. The (version ...) datestamp
below is the KiCad-8-era format also used by the repo's example boards;
the M0 gate (kicad-cli pcb drc parses the output) is what validates it
against the installed KiCad-10.99 fork.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from skidl.board import sexp
from skidl.board.model import BoardModel, Footprint

# Default rules: 0.15 mm clearance is comfortably above standard fab
# minimums (JLCPCB 1oz: 0.127 mm) AND compatible with fine-pitch pad
# fields (0.5 mm USB-C, module castellations) where escape traces can
# never keep 0.2 mm apart. The ROUTER still aims higher (guard band in
# dsn_export) so its small slips in tight spots stay DRC-clean.
_DEFAULT_NET_CLASS = {"width": 0.25, "clearance": 0.15, "via_size": 0.8, "via_drill": 0.4}

# KiCad-8-era board format datestamp -- matches the repo's own example
# board (tests/examples/netlist_to_skidl/kicad_project/*.anvil_pcb) and
# parses in the installed 10.99 fork (validated by the M0 gate).
BOARD_FORMAT_VERSION = 20240108


class UnresolvedFootprintsError(Exception):
    """Raised with every missing Lib:Name at once -- pre-flight, per the
    spec ('fail early with the exact missing footprint', mirroring
    generate_bom's missing[] behaviour) -- not just the first miss."""
    def __init__(self, missing: dict):
        self.missing = missing  # {ref: lib_colon_name}
        lines = "\n".join(f"  {ref}: {name}" for ref, name in missing.items())
        super().__init__(f"{len(missing)} footprint(s) could not be resolved:\n{lines}")


# ---------------------------------------------------------------------------
# [A] netlist + footprints -> BoardModel
# ---------------------------------------------------------------------------

def build_board(netlist_path: Path, name: Optional[str] = None,
                 net_classes: Optional[dict] = None,
                 layer_count: int = 2) -> BoardModel:
    """Parse `netlist_path`, resolve every footprint, and return a
    populated BoardModel with a trivial placeholder layout (see
    _default_grid_layout) -- not yet placed by any real rule. Does not
    write the .anvil_pcb file itself; see write_pcb() below."""
    from skidl.board.footprint_libs import (
        load_pads_and_courtyard, load_footprint_source, FootprintNotFoundError,
    )

    netlist_path = Path(netlist_path)
    comps, nets = _read_netlist(netlist_path)
    board = BoardModel(name=name or netlist_path.stem, layer_count=layer_count)
    board.net_classes = dict(net_classes or {"Default": dict(_DEFAULT_NET_CLASS)})

    missing = {}
    for ref, comp in comps.items():
        lib_name = comp["footprint"]
        if not lib_name or lib_name == "No Footprint":
            missing[ref] = lib_name or "(none given)"
            continue
        try:
            pads, courtyard = load_pads_and_courtyard(lib_name)
            source = load_footprint_source(lib_name)
        except FootprintNotFoundError:
            missing[ref] = lib_name
            continue
        board.footprints.append(Footprint(
            ref=ref, lib_id=lib_name, value=comp.get("value", ""),
            pads=pads, courtyard=courtyard, source_text=source,
        ))

    if missing:
        raise UnresolvedFootprintsError(missing)

    for net_name, net_info in nets.items():
        net_class = net_info["class"]
        # Any class the netlist names but the caller didn't parameterize
        # still gets an entry (with default rules) so .anvil_pro emission
        # and DSN export always have rules to point at.
        board.net_classes.setdefault(net_class, dict(_DEFAULT_NET_CLASS))
        net = board.get_or_add_net(net_name, net_class=net_class)
        for ref, pin, ptype in net_info["pins"]:
            net.pad_refs.append((ref, pin))
            net.pin_types[(ref, pin)] = ptype
            try:
                fp = board.footprint(ref)
            except KeyError:
                continue  # ref's footprint was missing; already in `missing` above
            for pad in fp.pads:
                if pad.number == pin:
                    pad.net = net_name

    _default_grid_layout(board)
    return board


def _default_grid_layout(board: BoardModel, spacing: float = 2.0, max_row_width: float = 100.0,
                          origin: tuple = (50.0, 50.0)) -> None:
    """M0-only placeholder layout: lay footprints left-to-right in rows,
    purely so a populate-only board is inspectable (distinct,
    non-overlapping positions) instead of every footprint stacked at the
    origin. `origin` offsets the whole grid away from the page's (0,0)
    top-left corner so the board sits inside the sheet frame, not under
    it. place/simple.py (M1) and place/rules.py (M2) both overwrite
    every x/y/rotation this sets -- nothing downstream should depend on
    this arrangement meaning anything."""
    ox, oy = origin
    x = y = row_height = 0.0
    for fp in board.footprints:
        if fp.courtyard:
            minx, miny, maxx, maxy = fp.courtyard
            w, h = (maxx - minx), (maxy - miny)
        else:
            w = h = 5.0  # no courtyard data (unusual) -- assume a generic 5mm square
        if x + w > max_row_width and x > 0:
            x, y, row_height = 0.0, y + row_height + spacing, 0.0
        fp.x, fp.y = ox + x + w / 2.0, oy + y + h / 2.0
        x += w + spacing
        row_height = max(row_height, h)

    xs, ys = [], []
    for fp in board.footprints:
        bbox = fp.courtyard_world_bbox()
        if bbox:
            xs += [bbox[0], bbox[2]]
            ys += [bbox[1], bbox[3]]
    margin = 3.0
    minx, maxx = (min(xs) - margin, max(xs) + margin) if xs else (0.0, 10.0)
    miny, maxy = (min(ys) - margin, max(ys) + margin) if ys else (0.0, 10.0)
    board.outline = [(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]


# ---------------------------------------------------------------------------
# Netlist reading -- SKiDL/KiCad netlist s-expression format (verified
# against mcp_circuits/*.net):
# (export (components (comp (ref "C1") (value "10uF")
#                            (footprint "Lib:Name") ...) ...)
#         (nets (net (code 1) (name "GND") (class "Default")
#                    (node (ref "C1") (pin "2") (pintype ..)) ...) ...))
# ---------------------------------------------------------------------------

def _read_netlist(path: Path):
    """Returns (comps, nets):
      comps: {ref: {"value": str, "footprint": str|None}}
      nets:  {name: {"class": str, "pins": [(ref, pin), ...]}}
    """
    tree = sexp.read_file(path)
    export = tree[0]  # (export ...)

    comps = {}
    comps_expr = sexp.find_one(export, "components")
    for comp_expr in (sexp.find_all(comps_expr, "comp") if comps_expr else []):
        ref_expr = sexp.find_one(comp_expr, "ref")
        ref = ref_expr[1] if ref_expr else sexp.atom_at(comp_expr, 1)
        value_expr = sexp.find_one(comp_expr, "value")
        footprint_expr = sexp.find_one(comp_expr, "footprint")
        comps[ref] = {
            "value": value_expr[1] if value_expr and len(value_expr) > 1 else "",
            "footprint": footprint_expr[1] if footprint_expr and len(footprint_expr) > 1 else None,
        }

    nets = {}
    nets_expr = sexp.find_one(export, "nets")
    for net_expr in (sexp.find_all(nets_expr, "net") if nets_expr else []):
        name_expr = sexp.find_one(net_expr, "name")
        net_name = name_expr[1] if name_expr else sexp.atom_at(net_expr, 1, "UNNAMED")
        class_expr = sexp.find_one(net_expr, "class")
        net_class = class_expr[1] if class_expr and len(class_expr) > 1 else "Default"
        # SKiDL emits ALL of a net's classes comma-joined, highest
        # priority first ("Power,Default") -- take the first.
        net_class = net_class.split(",")[0].strip() or "Default"
        pins = []
        for node_expr in sexp.find_all(net_expr, "node"):
            ref_expr = sexp.find_one(node_expr, "ref")
            pin_expr = sexp.find_one(node_expr, "pin")
            type_expr = sexp.find_one(node_expr, "pintype")
            if ref_expr and pin_expr:
                ptype = type_expr[1] if type_expr and len(type_expr) > 1 else ""
                pins.append((ref_expr[1], pin_expr[1], ptype))
        nets[net_name] = {"class": net_class, "pins": pins}

    return comps, nets


# ---------------------------------------------------------------------------
# .anvil_pcb writing (hand-templated -- see module docstring)
# ---------------------------------------------------------------------------

def _shift_edge_blocks(blocks, dx, dy):
    """Translate verbatim Edge.Cuts blocks by (dx, dy) -- coordinate pairs
    only, everything else (stroke, uuid, layer) untouched."""
    import re

    def _sh(m):
        return (f"({m.group(1)} {float(m.group(2)) + dx:g} "
                f"{float(m.group(3)) + dy:g})")

    return [re.sub(r"\((start|end|center|mid|xy)\s+([\d.-]+)\s+([\d.-]+)\)",
                   _sh, b) for b in blocks]


def _patch_setup_mask(setup_text: str, mask) -> str:
    """Set pad_to_mask_clearance inside a carried (setup ...) block --
    update in place, or INSERT right after the head when the block never
    had the token."""
    import re
    new, n = re.subn(r"\(pad_to_mask_clearance\s+[\d.-]+\)",
                     f"(pad_to_mask_clearance {mask:g})", setup_text, count=1)
    if n:
        return new
    return re.sub(r"\(\s*setup\b",
                  f"(setup (pad_to_mask_clearance {mask:g})",
                  setup_text, count=1)


def write_pcb(board: BoardModel, out_path: Path, carry_forward: dict = None,
              mask_clearance=None) -> Path:
    """Serialize the board. `carry_forward` (from
    rule_discovery.read_board_setup on the PREVIOUS board file) supplies
    the user's own (general …)/(layers …)/(setup …) blocks -- Board
    Setup edits (stackup, mask/paste, hatch offsets…) survive
    regeneration verbatim, the board-file analog of the .anvil_pro
    setdefault discipline."""
    out_path = Path(out_path)
    cf = carry_forward or {}
    net_code_by_name = {net.name: net.code for net in board.nets.values()}

    parts = [f'(kicad_pcb (version {BOARD_FORMAT_VERSION}) (generator "skidl_board")']
    parts.append("  " + cf["general_text"] if cf.get("general_text")
                 else f'  (general (thickness {board.thickness:g}))')
    # Drawing sheet: everything must live INSIDE the page frame (same
    # concept the schematic generator applies via _pick_paper_size).
    # The size is chosen dynamically from the board's real extents; a
    # user's own paper setting carried forward wins when the board fits.
    parts.append(f'  (paper "{getattr(board, "paper", None) or _pick_paper(board, cf)}")')
    parts.append("  " + cf["layers_text"] if cf.get("layers_text")
                 else _write_layers(board.layer_count))
    mask = mask_clearance if mask_clearance is not None else 0
    if cf.get("setup_text"):
        st = cf["setup_text"]
        if mask_clearance is not None:
            # A pending mask request must survive the verbatim carry --
            # without this the carried block (possibly lacking the token)
            # would silently drop the user's requested clearance.
            st = _patch_setup_mask(st, mask_clearance)
        parts.append("  " + st)
    else:
        parts.append(f'  (setup (pad_to_mask_clearance {mask:g}) (aux_axis_origin 0 0))')
    parts.append('  (net 0 "")')
    for net in board.nets.values():
        parts.append(f'  (net {net.code} "{_esc(net.name)}")')
    zc_by_ref = {}
    for ov in getattr(board, "pad_overrides", []):
        zc_by_ref.setdefault(ov.footprint_ref, {})[ov.pad_number] = ov.zone_connect
    for fp in board.footprints:
        parts.append(_write_footprint(fp, net_code_by_name, zc_by_ref.get(fp.ref) or {}))
    if board.outline:
        uec = cf.get("user_edge_cuts")
        if uec:
            # The user's drawn Edge.Cuts shape, translated so its min
            # corner lands on the regenerated outline's min corner (the
            # sheet-centering shift moved the board; same shape, moved).
            xs = [p[0] for p in board.outline]
            ys = [p[1] for p in board.outline]
            dx = round(min(xs) - uec["extents"][0], 2)
            dy = round(min(ys) - uec["extents"][1], 2)
            for blk in _shift_edge_blocks(uec["blocks"], dx, dy):
                parts.append("  " + blk)
        else:
            parts.append(_write_edge_cuts(board.outline))
    for track in board.tracks:
        parts.append(_write_track(track, net_code_by_name))
    for via in board.vias:
        parts.append(_write_via(via, net_code_by_name))
    for zone in board.zones:
        parts.append(_write_zone(zone, net_code_by_name))
    for ko in getattr(board, "keepouts", []):
        parts.append(_write_rule_area(ko))
    parts.append(")")

    out_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return out_path


def _write_zone(zone, net_code_by_name: dict) -> str:
    """A copper-pour zone. Outline only, (fill yes ...) enabled -- the
    actual fill polygons are computed by kicad-cli's --refill-zones
    (authored from the KiCad zone spec; the repo examples carry no pour
    template to copy)."""
    code = net_code_by_name.get(zone.net, 0)
    pts = " ".join(f"(xy {x:.4f} {y:.4f})" for x, y in zone.outline)
    # connect_pads: "yes" = SOLID (default) -- thermal reliefs on an
    # auto-routed board starve where tracks crowd a pad (measured:
    # starved_thermal on USB-C shield pads); solid can't starve. KiCad
    # writes solid as `yes`, thermal as NO keyword, none as `no`.
    cp = "" if zone.connect_pads == "thermal_reliefs" else zone.connect_pads + " "
    return (
        f'  (zone (net {code}) (net_name "{_esc(zone.net)}") '
        f'(layer "{zone.layer}") (hatch edge 0.5)\n'
        f'    (priority {zone.priority})\n'
        f'    (connect_pads {cp}(clearance {zone.clearance:g}))\n'
        f'    (min_thickness {zone.min_thickness:g}) (filled_areas_thickness no)\n'
        f'    (fill yes (thermal_gap {zone.thermal_gap:g}) '
        f'(thermal_bridge_width {zone.thermal_bridge_width:g}))\n'
        f'    (polygon (pts {pts}))\n'
        f'  )'
    )


def _write_rule_area(ko) -> str:
    """A KiCad keepout (rule area) zone. The app respects it natively on
    zone refill, so pour avoidance needs no extra logic; the router-side
    equivalent is emitted by dsn_export."""
    layers = " ".join(f'"{l}"' for l in ko.layers)
    pts = " ".join(f"(xy {x:.4f} {y:.4f})" for x, y in ko.outline)
    def flag(b):
        return "not_allowed" if b else "allowed"
    return (
        f'  (zone (net 0) (net_name "") (layers {layers}) '
        f'(name "{_esc(ko.name)}") (hatch edge 0.5)\n'
        f'    (connect_pads (clearance 0))\n'
        f'    (min_thickness 0.25)\n'
        f'    (keepout (tracks {flag(ko.no_tracks)}) (vias {flag(ko.no_vias)}) '
        f'(pads allowed) (copperpour {flag(ko.no_pour)}) '
        f'(footprints {flag(ko.no_footprints)}))\n'
        f'    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n'
        f'    (polygon (pts {pts}))\n'
        f'  )'
    )


def read_project_rules(pro_path: Path) -> dict:
    """DYNAMIC rule pickup: read the net-class rules the USER has set in
    KiCad from the project's .anvil_pro (KiCad saves the Board Setup /
    net-class dialogs there). Returns {class_name: {width, clearance,
    via_size, via_drill}} for every class that carries any rule -- empty
    dict when the project has none yet. build_pcb() feeds these into the
    board AND the router, so a clearance the user tunes in the KiCad UI
    is honored end-to-end on the next create_pcb run."""
    pro_path = Path(pro_path)
    if not pro_path.is_file():
        return {}
    try:
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for entry in (pro.get("net_settings", {}).get("classes", []) or []):
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        rules = {}
        if "track_width" in entry:
            rules["width"] = entry["track_width"]
        if "clearance" in entry:
            rules["clearance"] = entry["clearance"]
        if "via_diameter" in entry:
            rules["via_size"] = entry["via_diameter"]
        if "via_drill" in entry:
            rules["via_drill"] = entry["via_drill"]
        if rules:
            out[entry["name"]] = rules
    return out


_INT_MAX = 2147483647


def _net_settings_meta_version(existing_meta: dict) -> int:
    """The net_settings schema version the INSTALLED app expects --
    learned, not assumed: keep whatever an app-written file already has;
    for fresh writes, KiCad >= 10 uses version 5 (measured: 10.99's own
    migration DISCARDS our values from a version-3 file, so writing the
    old version is actively destructive on new apps)."""
    if existing_meta and isinstance(existing_meta.get("version"), int) \
            and existing_meta["version"] >= 5:
        return existing_meta["version"]
    try:
        from skidl.board.rule_discovery import discover_application
        ver = (discover_application().get("version") or "")
        major = int(str(ver).split(".")[0])
        if major >= 10:
            return 5
    except Exception:
        pass
    return existing_meta.get("version", 3) if existing_meta else 3


def canonical_class_entry(name: str, rules: dict, existing: dict = None,
                           priority: int = None) -> dict:
    """A net-class entry in the installed app's canonical shape (fields
    measured from a KiCad-10.99-written file) carrying our rule values.
    Incomplete/foreign-schema entries are what the app's migration
    throws away -- canonical ones survive an open+save round-trip."""
    entry = dict(existing or {})
    entry["name"] = name
    entry.setdefault("pcb_color", "rgba(0, 0, 0, 0.000)")
    entry.setdefault("schematic_color", "rgba(0, 0, 0, 0.000)")
    entry.setdefault("tuning_profile", "")
    entry.setdefault("priority", _INT_MAX if priority is None else priority)
    if "width" in rules or "track_width" in rules:
        entry["track_width"] = rules.get("width", rules.get("track_width"))
    if "clearance" in rules:
        entry["clearance"] = rules["clearance"]
    if "via_size" in rules or "via_diameter" in rules:
        entry["via_diameter"] = rules.get("via_size", rules.get("via_diameter"))
    if "via_drill" in rules:
        entry["via_drill"] = rules["via_drill"]
    return entry


def update_project_net_classes(board: BoardModel, pro_path: Path) -> Path:
    """Fold board.net_classes (+ per-net assignments) into the project's
    .anvil_pro JSON -- where modern KiCad actually keeps net-class rules.
    The schematic build (smart_schematic) already writes a .anvil_pro with
    a bare net_settings.classes [{"name": "Default"}]; this enriches it in
    place, preserving every unrelated key."""
    pro_path = Path(pro_path)
    if pro_path.is_file():
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    else:
        pro = {"meta": {"filename": pro_path.name, "version": 1}}

    # Board design-rule minimums (KiCad defaults are stricter than real
    # fab capability and than common library footprints: e.g. KiCad's
    # 0.3 mm min hole rejects the 0.2 mm thermal vias baked into the
    # ESP32-WROOM paddle). JLCPCB-standard-capable values -- but only
    # FILLED IN when absent: anything the user tuned in KiCad's Board
    # Setup dialog (saved into this same file) is never overwritten.
    rules_out = (pro.setdefault("board", {})
                    .setdefault("design_settings", {})
                    .setdefault("rules", {}))
    for k, v in {
        "min_clearance": 0.1,
        "min_track_width": 0.127,
        "min_through_hole_diameter": 0.15,
        "min_hole_clearance": 0.2,
    }.items():
        rules_out.setdefault(k, v)

    net_settings = pro.setdefault("net_settings", {})
    existing = {c.get("name"): c for c in net_settings.get("classes", []) if isinstance(c, dict)}

    classes = []
    prio = 0
    for name, rules in board.net_classes.items():
        merged = {**_DEFAULT_NET_CLASS, **rules}
        classes.append(canonical_class_entry(
            name, merged, existing.get(name),
            priority=None if name == "Default" else prio))
        if name != "Default":
            prio += 1
    for name, entry in existing.items():
        if name and name not in board.net_classes:
            # A user-created class carrying no rule values never enters
            # board.net_classes (read_project_rules skips it) -- keep
            # their entry verbatim instead of dropping it on rewrite.
            classes.append(entry)
    net_settings["classes"] = classes
    net_settings["meta"] = {
        "version": _net_settings_meta_version(net_settings.get("meta"))}

    # Per-net assignment for anything not in Default (KiCad-8 style
    # pattern list; exact-name patterns). Harmless-if-ignored JSON.
    patterns = [
        {"netclass": net.net_class, "pattern": net.name}
        for net in board.nets.values() if net.net_class != "Default"
    ]
    if patterns:
        net_settings["netclass_patterns"] = patterns

    pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")
    return pro_path


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


# Smallest-first, same ladder the schematic generator uses. KiCad names
# are portrait W<H swapped internally; these are the landscape mm sizes.
PAPER_SIZES = [("A4", 297.0, 210.0), ("A3", 420.0, 297.0),
               ("A2", 594.0, 420.0), ("A1", 841.0, 594.0),
               ("A0", 1189.0, 841.0)]


def _pick_paper(board, cf=None) -> str:
    """Smallest standard paper that fits the board outline (plus margin).
    A carried-forward user paper wins when the board still fits it."""
    if board.outline:
        xs = [p[0] for p in board.outline]
        ys = [p[1] for p in board.outline]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
    else:
        w = h = 0.0
    margin = 20.0   # room for the frame + title block
    user_paper = (cf or {}).get("paper")
    for name, pw, ph in PAPER_SIZES:
        if w + margin <= pw and h + margin <= ph:
            if user_paper and user_paper != name:
                for un, upw, uph in PAPER_SIZES:
                    if un == user_paper and w + margin <= upw and h + margin <= uph:
                        return user_paper
            return name
    return f"User {w + margin:g} {h + margin:g}"


def _write_layers(layer_count: int) -> str:
    entries = ['(0 "F.Cu" signal)']
    if layer_count > 2:
        entries += ['(1 "In1.Cu" signal)', '(2 "In2.Cu" signal)']
    entries += [
        '(31 "B.Cu" signal)',
        '(32 "B.Adhes" user "B.Adhesive")', '(33 "F.Adhes" user "F.Adhesive")',
        '(34 "B.Paste" user)', '(35 "F.Paste" user)',
        '(36 "B.SilkS" user "B.Silkscreen")', '(37 "F.SilkS" user "F.Silkscreen")',
        '(38 "B.Mask" user)', '(39 "F.Mask" user)',
        '(44 "Edge.Cuts" user)',
        '(46 "F.Fab" user)', '(47 "B.Fab" user)',
        '(49 "F.CrtYd" user "F.Courtyard")', '(48 "B.CrtYd" user "B.Courtyard")',
    ]
    body = "\n    ".join(entries)
    return f'  (layers\n    {body}\n  )'


def _write_footprint(fp: Footprint, net_code_by_name: dict, pad_zc: dict = None) -> str:
    """Prefer embedding the library's original .kicad_mod verbatim (real
    silkscreen/fab/courtyard art + correctly-styled text) with position,
    reference, value and pad nets surgically injected. Falls back to the
    minimal pads-only template when no source is available or the part is
    rotated (rotated embedding needs per-pad angle rewriting -- M2).

    `pad_zc`: {pad_number: zone_connect} overrides for this footprint."""
    pad_zc = pad_zc or {}
    if fp.source_text and fp.side == "top":
        try:
            return _embed_footprint(fp, net_code_by_name, pad_zc)
        except Exception:
            pass  # any surgery surprise -> proven minimal template
    return _template_footprint(fp, net_code_by_name, pad_zc)


import re as _re

_FP_HEADER_RE = _re.compile(r'^(\s*\(\s*footprint\s+)("(?:[^"\\]|\\.)*"|[^\s()]+)')
_PROP_REF_RE = _re.compile(r'(\(\s*property\s+"Reference"\s+)"(?:[^"\\]|\\.)*"')
_PROP_VAL_RE = _re.compile(r'(\(\s*property\s+"Value"\s+)"(?:[^"\\]|\\.)*"')
_FPTEXT_REF_RE = _re.compile(r'(\(\s*fp_text\s+reference\s+)("(?:[^"\\]|\\.)*"|[^\s()]+)')
_FPTEXT_VAL_RE = _re.compile(r'(\(\s*fp_text\s+value\s+)("(?:[^"\\]|\\.)*"|[^\s()]+)')
_PAD_NUM_RE = _re.compile(r'\(\s*pad\s+("(?:[^"\\]|\\.)*"|[^\s()]+)')


def _embed_footprint(fp: Footprint, net_code_by_name: dict, pad_zc: dict = None) -> str:
    pad_zc = pad_zc or {}
    s = fp.source_text.strip()

    # Header: library name -> full "Lib:Name" id, then inject placement.
    m = _FP_HEADER_RE.match(s)
    if not m:
        raise ValueError("no (footprint ...) header")
    s = (s[:m.start(2)] + f'"{_esc(fp.lib_id)}"'
         + f'\n    (at {fp.x:.4f} {fp.y:.4f} {fp.rotation:.4f})'
         + s[m.end(2):])

    # Reference / Value text (KiCad 8 property style + legacy fp_text style).
    s = _PROP_REF_RE.sub(lambda mm: mm.group(1) + f'"{_esc(fp.ref)}"', s, count=1)
    s = _PROP_VAL_RE.sub(lambda mm: mm.group(1) + f'"{_esc(fp.value)}"', s, count=1)
    s = _FPTEXT_REF_RE.sub(lambda mm: mm.group(1) + f'"{_esc(fp.ref)}"', s, count=1)
    s = _FPTEXT_VAL_RE.sub(lambda mm: mm.group(1) + f'"{_esc(fp.value)}"', s, count=1)

    rot = fp.rotation % 360.0
    if rot:
        # KiCad stores pad/text angles as TOTAL (footprint + local): bump
        # every text block's angle; pads are bumped in the loop below.
        s = _bump_spans(s, _TEXT_HEAD_RE, rot)

    # Pad nets: append (net CODE "NAME") inside every connected pad block
    # (+ the rotation angle bump when the part is rotated).
    net_by_number = {p.number: p.net for p in fp.pads if p.net}
    out, i = [], 0
    while True:
        m = _PAD_NUM_RE.search(s, i)
        if not m:
            out.append(s[i:])
            break
        end = _match_paren(s, m.start())
        num = m.group(1).strip('"')
        net_name = net_by_number.get(num)
        body = s[m.start():end - 1]
        if rot:
            body = _bump_at_angle(body, rot)
        # zone_connect override: replace an existing (zone_connect ..) in the
        # pad block (a library may already set one), else append it as the
        # last property before the pad's closing paren. Paren-matched, so
        # nested pad sub-exprs (custom primitives, options) are safe.
        zc = pad_zc.get(num)
        append_zc = zc is not None
        if zc is not None:
            zi = body.find("(zone_connect")
            if zi != -1:
                zclose = _match_paren(body, zi)
                body = body[:zi] + f"(zone_connect {zc})" + body[zclose:]
                append_zc = False
        out.append(s[i:m.start()])
        out.append(body)
        if net_name and net_name in net_code_by_name:
            out.append(f' (net {net_code_by_name[net_name]} "{_esc(net_name)}")')
        if append_zc:
            out.append(f' (zone_connect {zc})')
        out.append(")")
        i = end
    return "  " + "".join(out)


_TEXT_HEAD_RE = _re.compile(r'\(\s*(?:property\s+"|fp_text\s+)')
_AT_RE = _re.compile(r'\(at\s+(-?[\d.]+)\s+(-?[\d.]+)(?:\s+(-?[\d.]+))?\s*\)')


def _bump_at_angle(span: str, rot: float) -> str:
    """Add `rot` to the first (at x y [a]) clause in span."""
    def _fix(m):
        a = float(m.group(3)) if m.group(3) else 0.0
        return f"(at {m.group(1)} {m.group(2)} {(a + rot) % 360.0:g})"
    return _AT_RE.sub(_fix, span, count=1)


def _bump_spans(s: str, head_re, rot: float) -> str:
    """Apply _bump_at_angle inside every block whose header matches
    head_re (property/fp_text blocks)."""
    out, i = [], 0
    while True:
        m = head_re.search(s, i)
        if not m:
            out.append(s[i:])
            break
        end = _match_paren(s, m.start())
        out.append(s[i:m.start()])
        out.append(_bump_at_angle(s[m.start():end], rot))
        i = end
    return "".join(out)


def _match_paren(s: str, start: int) -> int:
    """Index just past the ')' matching the '(' at (or right after) start.
    Quote-aware: parens inside double-quoted strings don't count."""
    i = s.index("(", start)
    depth = 0
    in_str = False
    while i < len(s):
        c = s[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced parens in footprint source")


def _template_footprint(fp: Footprint, net_code_by_name: dict, pad_zc: dict = None) -> str:
    pad_zc = pad_zc or {}
    locked = " locked" if fp.locked else ""
    lines = [
        f'  (footprint "{_esc(fp.lib_id)}" (layer "{fp.layer}"){locked}',
        f'    (at {fp.x:.4f} {fp.y:.4f} {fp.rotation:.4f})',
        f'    (property "Reference" "{_esc(fp.ref)}" (layer "F.SilkS") (at 0 0 0))',
        f'    (property "Value" "{_esc(fp.value)}" (layer "F.Fab") (at 0 0 0))',
    ]
    for pad in fp.pads:
        layers_str = " ".join(f'"{l}"' for l in pad.layers)
        if pad.drill and pad.drill_y:
            drill_str = f" (drill oval {pad.drill:.4f} {pad.drill_y:.4f})"
        elif pad.drill:
            drill_str = f" (drill {pad.drill:.4f})"
        else:
            drill_str = ""
        rratio_str = f" (roundrect_rratio {pad.roundrect_rratio:.4f})" if pad.shape == "roundrect" else ""
        net_str = ""
        if pad.net and pad.net in net_code_by_name:
            net_str = f' (net {net_code_by_name[pad.net]} "{_esc(pad.net)}")'
        zc = pad_zc.get(pad.number)
        zc_str = f' (zone_connect {zc})' if zc is not None else ""
        # In a board file a pad's (at ... ANGLE) is absolute: the
        # footprint's own rotation plus the pad's local rotation from the
        # .kicad_mod (KiCad convention).
        pad_angle = (fp.rotation + pad.rotation) % 360.0
        lines.append(
            f'    (pad "{pad.number}" {pad.pad_type} {pad.shape} '
            f'(at {pad.x:.4f} {pad.y:.4f} {pad_angle:.4f}) (size {pad.size_x:.4f} {pad.size_y:.4f})'
            f'{drill_str} (layers {layers_str}){rratio_str}{net_str}{zc_str})'
        )
    lines.append("  )")
    return "\n".join(lines)


def _write_edge_cuts(outline: list) -> str:
    # KiCad 7+ graphic-line syntax: (stroke (width ..) (type ..)), not the
    # legacy KiCad-5/6 bare (width ..).
    lines = []
    n = len(outline)
    for i in range(n):
        (x1, y1), (x2, y2) = outline[i], outline[(i + 1) % n]
        lines.append(
            f'  (gr_line (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) '
            f'(stroke (width 0.1) (type solid)) (layer "Edge.Cuts"))'
        )
    return "\n".join(lines)


def _write_track(track, net_code_by_name: dict) -> str:
    code = net_code_by_name.get(track.net, 0)
    (x1, y1), (x2, y2) = track.start, track.end
    return (
        f'  (segment (start {x1:.4f} {y1:.4f}) (end {x2:.4f} {y2:.4f}) '
        f'(width {track.width:.4f}) (layer "{track.layer}") (net {code}))'
    )


def _write_via(via, net_code_by_name: dict) -> str:
    code = net_code_by_name.get(via.net, 0)
    x, y = via.at
    layers_str = " ".join(f'"{l}"' for l in via.layers)
    return (
        f'  (via (at {x:.4f} {y:.4f}) (size {via.size:.4f}) (drill {via.drill:.4f}) '
        f'(layers {layers_str}) (net {code}))'
    )
