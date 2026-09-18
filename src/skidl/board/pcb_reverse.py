"""
src/skidl/board/pcb_reverse.py

REVERSE flow for a PCB-only input (gap: "user gives a .anvil_pcb/.kicad_pcb, no
schematic"). A board carries, per footprint: reference, footprint lib_id, value,
and each pad -> net -- enough to reconstruct CONNECTIVITY + values + footprints.
It does NOT carry the schematic symbol / pin functions (libsource), so the symbol
is INFERRED: passives/connectors exactly (by ref-prefix + footprint), ICs
best-effort (caller may library-match), generic N-pin box otherwise.

Output is a synthesized KiCad netlist (the exact shape netlist_to_skidl parses),
so the existing netlist -> SKiDL -> build pipeline rebuilds a schematic. The
schematic is CONNECTIVITY-faithful, not pin-semantics-faithful, until a human
confirms the IC symbols.

Pure + import-light (reuses board/sexp.py); no file writing here -- the caller
(skidl_mcp_server.import_pcb) creates any generic symbols and runs the build.
"""

from __future__ import annotations

import os
import re

from skidl.board import sexp

# ref-prefix -> (lib, part) for parts whose symbol IS determined by the prefix
# (+ footprint refinements below). "exact" confidence.
_PASSIVE_MAP = {
    "R": ("Device", "R"),
    "C": ("Device", "C"),
    "L": ("Device", "L"),
    "D": ("Device", "D"),
    "LED": ("Device", "LED"),
    "Y": ("Device", "Crystal"),
    "F": ("Device", "Fuse"),
}
_ANCHOR_PREFIXES = {"U", "IC", "A", "Q"}
_CONNECTOR_PREFIXES = {"J", "P", "CN"}


def _ref_prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return (m.group(1).upper() if m else "")


def _prop(fp, name):
    """Value of a footprint (property "<name>" "<value>" ...) block, or ''."""
    for p in sexp.find_all(fp, "property"):
        if sexp.atom_at(p, 1) == name:
            return sexp.atom_at(p, 2, "") or ""
    return ""


def parse_pcb(path) -> dict:
    """Parse a .anvil_pcb / .kicad_pcb into components + nets.

    Returns {"components": [{ref, footprint, value, side, pads:[{num, net}]}],
             "nets": [{code, name, nodes:[[ref, pin], ...]}]}.
    Footprints with no Reference (mounting holes, graphics) are skipped.
    """
    tree = sexp.read_file(path)
    if not tree or not isinstance(tree[0], list):
        raise ValueError("not a KiCad PCB s-expression")
    root = tree[0]
    components = []
    nets_by_name = {}          # name -> {"code", "nodes": [[ref, pin]]}
    order = []                 # preserve first-seen net order

    for fp in sexp.find_all(root, "footprint"):
        ref = _prop(fp, "Reference")
        if not ref or ref.startswith("#"):     # power flags / no-ref graphics
            continue
        lib_id = sexp.atom_at(fp, 1, "") or ""
        value = _prop(fp, "Value")
        layer = sexp.find_one(fp, "layer")
        side = "bottom" if (layer and sexp.atom_at(layer, 1, "").startswith("B.")) else "top"
        pads = []
        seen_pad = set()
        for pad in sexp.find_all(fp, "pad"):
            num = sexp.atom_at(pad, 1, "")
            if num in ("", '""') or num in seen_pad:
                continue        # unnumbered mech pad, or a repeated (thermal) pad
            seen_pad.add(num)
            net = sexp.find_one(pad, "net")
            net_name = sexp.atom_at(net, 2, "") if net else ""
            pads.append({"num": num, "net": net_name})
            if net_name:
                if net_name not in nets_by_name:
                    code = sexp.atom_at(net, 1, str(len(order) + 1)) if net else str(len(order) + 1)
                    nets_by_name[net_name] = {"code": code, "nodes": []}
                    order.append(net_name)
                nets_by_name[net_name]["nodes"].append([ref, num])
        components.append({"ref": ref, "footprint": lib_id, "value": value,
                           "side": side, "pads": pads})

    nets = [{"code": nets_by_name[n]["code"], "name": n,
             "nodes": nets_by_name[n]["nodes"]} for n in order]
    return {"components": components, "nets": nets}


def infer_symbol(comp) -> dict:
    """Infer a schematic symbol for one parsed component.

    Returns {lib, part, confidence, generic_pins}. confidence is
    'exact' (ref/footprint-determined), 'matched' (caller-resolved from a
    library search -- not done here), or 'generic' (N-pin box). For 'generic'
    the caller must create the symbol; generic_pins is the pin list to create.
    """
    ref = comp.get("ref", "")
    prefix = _ref_prefix(ref)
    fp = (comp.get("footprint") or "").lower()
    npads = len([p for p in comp.get("pads", []) if p.get("num")])

    # LEDs / polarized caps are footprint-refined passives.
    if prefix == "D" and "led" in fp:
        return {"lib": "Device", "part": "LED", "confidence": "exact", "generic_pins": None}
    if prefix == "C" and any(k in fp for k in ("cp_", "polar", "electrolytic", "tantalum", "_ep")):
        return {"lib": "Device", "part": "CP", "confidence": "exact", "generic_pins": None}
    if prefix in _PASSIVE_MAP:
        lib, part = _PASSIVE_MAP[prefix]
        return {"lib": lib, "part": part, "confidence": "exact", "generic_pins": None}
    if prefix in _CONNECTOR_PREFIXES:
        return {"lib": "Connector_Generic", "part": "Conn_01x%02d" % max(npads, 1),
                "confidence": "exact", "generic_pins": None}
    if prefix == "SW":
        return {"lib": "Switch", "part": "SW_Push", "confidence": "exact", "generic_pins": None}

    # IC / anything else -> generic N-pin box (caller creates the symbol). The
    # server may upgrade this to 'matched' by a library search on the value.
    part_name = "GEN_%s_%dP" % (re.sub(r"[^A-Za-z0-9]", "", ref) or "U", npads)
    pins = [{"num": p["num"], "name": p["num"], "type": "passive"}
            for p in comp.get("pads", []) if p.get("num")]
    return {"lib": "ReversePCB", "part": part_name, "confidence": "generic",
            "generic_pins": pins}


def _looks_like_mpn(value: str) -> bool:
    """A part value worth a library search: has a letter AND a digit, length>=3,
    and is not a plain passive value (10k, 10uF, 4.7nH...)."""
    v = (value or "").strip()
    if len(v) < 3 or not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
        return False
    if re.match(r"^\d+(\.\d+)?\s*[a-zA-Zµμ]{1,3}$", v):   # 10uF, 4k7, 100nH-ish
        return False
    return True


def match_ic_symbol(value: str, npads: int):
    """Best-effort: find a REAL library symbol for an IC by its value/MPN.

    Returns (lib, part) only when a candidate's pin count EXACTLY equals the
    board's pad count (so a wrong-package sibling never wires wrong silently);
    otherwise None -> caller falls back to a generic box. Import-heavy (loads the
    symbol DB), so it is opt-in via analyze(match_ics=True)."""
    if not _looks_like_mpn(value):
        return None
    try:
        from skidl import KICAD9, set_default_tool, TEMPLATE, Part
        from skidl.part_query import PartSearchDB
        set_default_tool(KICAD9)
        db = PartSearchDB(tool=KICAD9)
        db.load_from_lib_search_paths()
        cands = db.search(value)
    except Exception:
        return None
    for p in sorted(cands, key=lambda x: (x.lib_name, x.part_name)):
        try:
            part = Part(p.lib_name, p.part_name, dest=TEMPLATE)
            if len(list(part.pins)) == npads:
                return (p.lib_name, p.part_name)
        except Exception:
            continue
    return None


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def synthesize_netlist(components, nets, symbol_map, source="import_pcb") -> str:
    """Emit a KiCad netlist (the exact shape netlist_to_skidl parses) from the
    parsed board + a {ref: {lib, part}} symbol_map. One flat sheet ("/")."""
    out = ['(export']
    out.append('  (version "D")')
    out.append('  (design')
    out.append('    (source "%s")' % _esc(source))
    out.append('    (sheet (number 1) (name "/") (tstamps "/")')
    out.append('      (title_block (title) (company) (rev) (date))))')
    out.append('  (components')
    for c in components:
        ref = c["ref"]
        sym = symbol_map.get(ref, {"lib": "ReversePCB", "part": "GEN"})
        out.append('    (comp')
        out.append('      (ref "%s")' % _esc(ref))
        out.append('      (value "%s")' % _esc(c.get("value") or "~"))
        out.append('      (footprint "%s")' % _esc(c.get("footprint") or ""))
        out.append('      (libsource (lib "%s") (part "%s"))'
                   % (_esc(sym["lib"]), _esc(sym["part"])))
        out.append('      (sheetpath (names "/") (tstamps "/")))')
    out.append('  )')
    out.append('  (nets')
    for i, n in enumerate(nets, start=1):
        code = n.get("code") or str(i)
        out.append('    (net (code "%s") (name "%s")' % (_esc(code), _esc(n["name"])))
        for ref, pin in n["nodes"]:
            out.append('      (node (ref "%s") (pin "%s"))' % (_esc(ref), _esc(pin)))
        out.append('    )')
    out.append('  )')
    out.append(')')
    return "\n".join(out) + "\n"


def analyze(path, match_ics=False) -> dict:
    """Full read-only analysis of a PCB: parse + infer every symbol + synthesize
    the netlist. Returns everything the caller needs to create generic symbols,
    write the .net, and report to the user. No files are written here.

    match_ics=True upgrades generic ICs to a REAL library symbol when the value
    is an MPN whose symbol has the exact pad count (best-effort; import-heavy).
    """
    parsed = parse_pcb(path)
    symbol_map = {}
    generics = []          # [{ref, name, lib, pins, value, footprint}]
    report = []
    for c in parsed["components"]:
        inf = infer_symbol(c)
        # best-effort: turn a generic IC into a real matched symbol (real pins).
        if match_ics and inf["confidence"] == "generic":
            npads = len([p for p in c.get("pads", []) if p.get("num")])
            m = match_ic_symbol(c.get("value", ""), npads)
            if m:
                inf = {"lib": m[0], "part": m[1], "confidence": "matched",
                       "generic_pins": None}
        symbol_map[c["ref"]] = {"lib": inf["lib"], "part": inf["part"]}
        if inf["confidence"] == "generic":
            generics.append({"ref": c["ref"], "lib": inf["lib"], "name": inf["part"],
                             "pins": inf["generic_pins"], "value": c.get("value", ""),
                             "footprint": c.get("footprint", "")})
        report.append({"ref": c["ref"], "value": c.get("value", ""),
                       "footprint": c.get("footprint", ""),
                       "symbol": "%s:%s" % (inf["lib"], inf["part"]),
                       "confidence": inf["confidence"]})
    netlist_text = synthesize_netlist(parsed["components"], parsed["nets"],
                                      symbol_map, source=os.path.basename(str(path)))
    return {
        "components": parsed["components"],
        "nets": parsed["nets"],
        "symbol_map": symbol_map,
        "generic_symbols": generics,
        "report": report,
        "netlist_text": netlist_text,
        "counts": {"parts": len(parsed["components"]), "nets": len(parsed["nets"]),
                   "generic": len(generics)},
    }
