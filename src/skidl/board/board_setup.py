"""
src/skidl/board/board_setup.py

Board Setup as the LIVE source of truth -- read fresh before every major
operation, because the user can change it in the KiCad application at
any moment.

DYNAMIC discovery, no fixed schema (the core requirement): the readers
capture WHATEVER the files contain -- every (setup ...) sub-entry of the
board file via a generic s-expression->data converter, and the ENTIRE
design_settings/net_settings objects of the .kicad_pro -- including keys
this code has never heard of. The result is split into:

  raw        everything discovered, verbatim
  consumed   the values the pipeline actively drives behavior with,
             each mapped  found_value -> used_in
  preserved  discovered but not interpreted -- kept verbatim forever
             (carry-forward + setdefault discipline), reported honestly
  setup_hash sha256 over the full raw content: a user change to ANY
             setting, even an uninterpreted one, is detected
"""

from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

from skidl.board import sexp
from skidl.board.pcb_writer import _match_paren


# ---------------------------------------------------------------------------
# Generic s-expression -> data (schema-free)
# ---------------------------------------------------------------------------

def sexp_to_data(expr):
    """Convert a parsed s-expression (nested lists of strings) into plain
    data, GENERICALLY: no knowledge of any particular key.
      (pad_to_mask_clearance 0)        -> {"pad_to_mask_clearance": 0.0}
      (stackup (layer "F.Cu" ...) ...) -> {"stackup": {...}}
    Repeated heads become lists; bare atoms are number-coerced."""
    if not isinstance(expr, list):
        return _coerce(expr)
    if not expr:
        return None
    children = expr[1:]
    atoms = [c for c in children if not isinstance(c, list)]
    lists = [c for c in children if isinstance(c, list)]
    if not lists:
        if not atoms:
            return True                        # bare flag: (legacy_teardrops)
        vals = [_coerce(a) for a in atoms]
        return vals[0] if len(vals) == 1 else vals
    out = {}
    if atoms:
        vals = [_coerce(a) for a in atoms]
        out["_values"] = vals[0] if len(vals) == 1 else vals
    for child in lists:
        if not child or isinstance(child[0], list):
            continue
        key = child[0]
        val = sexp_to_data(child)
        if key in out:
            if not isinstance(out[key], list) or isinstance(val, list):
                out[key] = [out[key]]
            out[key].append(val)
        else:
            out[key] = val
    return out


def _coerce(atom):
    try:
        f = float(atom)
        return int(f) if f == int(f) and "." not in str(atom) else f
    except (ValueError, TypeError):
        return atom


def _top_level_heads(text: str):
    """Head tokens of every DIRECT child block of (kicad_pcb ...) --
    quote-aware scan, so heads inside strings/nested blocks don't count."""
    heads = []
    try:
        start = text.index("(")
        i = text.index("(", start + 1)
    except ValueError:
        return heads
    depth = 1
    in_str = False
    n = len(text)
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
            if depth == 2:
                j = i + 1
                while j < n and text[j] not in " \t\r\n()":
                    j += 1
                head = text[i + 1:j]
                if head and head not in heads:
                    heads.append(head)
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return heads


def _extract_block(text: str, head: str):
    """(raw_text, parsed_data) of the first top-level (head ...) block."""
    m = re.search(r"\(\s*" + head + r"[\s(]", text)
    if not m:
        return None, None
    try:
        end = _match_paren(text, m.start())
    except Exception:
        return None, None
    raw = text[m.start():end]
    try:
        return raw, sexp_to_data(sexp.read(raw)[0])
    except Exception:
        return raw, None


# ---------------------------------------------------------------------------
# Live snapshot
# ---------------------------------------------------------------------------

def get_board_setup(base: str, out_dir) -> dict:
    """The user's CURRENT Board Setup, discovered dynamically from the
    project's real files. See module docstring for the raw/consumed/
    preserved split. Call before every major operation."""
    out_dir = Path(out_dir)
    pcb_path = out_dir / f"{base}.kicad_pcb"
    pro_path = out_dir / f"{base}.kicad_pro"
    dru_path = out_dir / f"{base}.kicad_dru"
    sidecar_path = out_dir / f"{base}.board_config.json"

    raw = {"board_file": {}, "project_file": {}}

    # Board-file side: capture EVERY top-level block that is SETUP, not
    # design content -- schema-free, so Board Setup sections that write
    # their own blocks (embedded_files, properties, title_block, future
    # sections) are all caught without naming them here.
    _CONTENT_HEADS = {
        "footprint", "module", "segment", "arc", "via", "zone", "net",
        "gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly", "gr_text",
        "gr_curve", "group", "dimension", "target", "image", "generated",
        "text", "net_class", "version", "generator", "generator_version",
        "host",
    }
    layers_table = []
    if pcb_path.is_file():
        text = pcb_path.read_text(encoding="utf-8", errors="replace")
        for head in _top_level_heads(text):
            if head in _CONTENT_HEADS:
                continue
            raw_txt, data = _extract_block(text, head)
            if raw_txt is not None:
                raw["board_file"][head] = data
                raw["board_file"][f"_{head}_text"] = raw_txt
        # Layer table entries: (N "Name" type ["alias"]) -- parsed generically.
        lt = raw["board_file"].get("_layers_text")
        if lt:
            try:
                for entry in sexp.read(lt)[0][1:]:
                    if isinstance(entry, list) and len(entry) >= 3:
                        layers_table.append({"number": _coerce(entry[0]),
                                             "name": entry[1],
                                             "type": entry[2]})
            except Exception:
                pass
    raw["board_file"]["layer_table"] = layers_table

    # Project-file side: the ENTIRE .kicad_pro -- text_variables,
    # component classes, cvpcb, whatever KiCad keeps there. Schema-free;
    # the hash covers every key.
    pro = {}
    if pro_path.is_file():
        try:
            pro = json.loads(pro_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    raw["project_file"] = {
        "design_settings": pro.get("board", {}).get("design_settings", {}),
        "net_settings": pro.get("net_settings", {}),
        "everything_else": {k: v for k, v in pro.items()
                            if k not in ("board", "net_settings")},
    }
    raw["custom_dru"] = dru_path.is_file()

    sidecar = {}
    if sidecar_path.is_file():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # ---- consumed: what the pipeline DRIVES behavior with ---------------
    copper = [l for l in layers_table if str(l.get("name", "")).endswith(".Cu")]
    if copper:
        layer_count, layer_src = len(copper), "board-user (layer table)"
    elif sidecar.get("layers"):
        layer_count, layer_src = int(sidecar["layers"]), "sidecar (init choice)"
    else:
        layer_count, layer_src = 2, "default"

    classes = {}
    for entry in raw["project_file"]["net_settings"].get("classes", []) or []:
        if isinstance(entry, dict) and entry.get("name"):
            classes[entry["name"]] = entry     # WHOLE entry, every key kept

    setup_data = raw["board_file"].get("setup") or {}
    general = raw["board_file"].get("general") or {}
    rules = raw["project_file"]["design_settings"].get("rules", {}) or {}

    consumed = {
        "layers": {"value": layer_count, "source": layer_src,
                   "used_in": "routing copper layers (DSN) + board layer table"},
        "net_classes": {"value": {n: {k: v for k, v in e.items()
                                      if k in ("track_width", "clearance",
                                               "via_diameter", "via_drill")}
                                  for n, e in classes.items()},
                        "used_in": "router track widths + DRC clearances per net"},
        "design_rules": {"value": rules,
                         "used_in": "DRC minimums + manufacturing refusal gate"},
        "thickness": {"value": general.get("thickness", sidecar.get("thickness", 1.6)),
                      "used_in": "board file (general) + STEP export"},
        "pad_to_mask_clearance": {"value": setup_data.get("pad_to_mask_clearance", 0),
                                  "used_in": "board (setup) -> mask Gerbers"},
        "custom_dru": {"value": raw["custom_dru"],
                       "used_in": "kicad-cli DRC (honored automatically)"},
    }

    # ---- preserved: found but not interpreted --------------------------
    consumed_setup_keys = {"pad_to_mask_clearance"}
    consumed_class_keys = {"name", "track_width", "clearance", "via_diameter", "via_drill"}
    preserved = []
    for k in setup_data:
        if k not in consumed_setup_keys and not k.startswith("_"):
            preserved.append(f"board setup: {k}")
    for n, e in classes.items():
        extra = [k for k in e if k not in consumed_class_keys]
        if extra:
            preserved.append(f"net class {n}: {', '.join(sorted(extra))}")
    for k in raw["project_file"]["design_settings"]:
        if k != "rules":
            preserved.append(f"design_settings: {k}")
    for k in raw["board_file"]:
        if k not in ("setup", "layers", "general", "layer_table") \
                and not k.startswith("_"):
            preserved.append(f"board file block: {k}")
    for k, v in raw["project_file"].get("everything_else", {}).items():
        if v not in (None, {}, [], ""):
            preserved.append(f"project: {k}")

    return {
        "raw": raw,
        "consumed": consumed,
        "preserved": preserved,
        "preserved_note": ("these were FOUND on this system but are not "
                           "interpreted by the pipeline -- they are kept "
                           "verbatim (carry-forward / setdefault) and never lost"),
        "setup_hash": _hash_raw(raw),
    }


def _hash_raw(raw: dict) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def compute_setup_hash(base: str, out_dir) -> str:
    return get_board_setup(base, out_dir)["setup_hash"]


# ---------------------------------------------------------------------------
# Applying user-requested changes
# ---------------------------------------------------------------------------

_OUR_TO_KICAD = {"width": "track_width", "via_size": "via_diameter"}


def update_board_setup(base: str, out_dir, stackup=None, constraints=None,
                        net_classes=None, via_rules=None, mask=None,
                        drc_severities=None) -> dict:
    """Apply the user's EXPLICIT Board Setup changes (their instruction is
    authoritative -- direct overwrite, unlike the never-clobber init
    path). Only the sections passed are touched; everything else stays
    exactly as discovered."""
    out_dir = Path(out_dir)
    pro_path = out_dir / f"{base}.kicad_pro"
    sidecar_path = out_dir / f"{base}.board_config.json"
    pcb_path = out_dir / f"{base}.kicad_pcb"
    changed = []

    pro = {}
    if pro_path.is_file():
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    ds = pro.setdefault("board", {}).setdefault("design_settings", {})

    if constraints:
        ds.setdefault("rules", {}).update(constraints)
        changed.append(f"constraints: {sorted(constraints)}")

    if drc_severities:
        ds.setdefault("rule_severities", {}).update(drc_severities)
        changed.append(f"drc_severities: {sorted(drc_severities)}")

    if net_classes or via_rules:
        from skidl.board.pcb_writer import (
            canonical_class_entry, _net_settings_meta_version,
        )
        ns = pro.setdefault("net_settings", {})
        entries = {c.get("name"): c for c in ns.get("classes", [])
                   if isinstance(c, dict)}
        for cls_name, rules in (net_classes or {}).items():
            entry = canonical_class_entry(cls_name, {}, entries.get(cls_name))
            for k, v in rules.items():
                entry[_OUR_TO_KICAD.get(k, k)] = v
            entries[cls_name] = entry
            changed.append(f"net class {cls_name}: {sorted(rules)}")
        if via_rules:
            entry = canonical_class_entry("Default", {}, entries.get("Default"))
            for k, v in via_rules.items():
                entry[_OUR_TO_KICAD.get(k, k)] = v
            entries["Default"] = entry
            changed.append(f"via rules (Default): {sorted(via_rules)}")
        ns["classes"] = list(entries.values())
        ns["meta"] = {"version": _net_settings_meta_version(ns.get("meta"))}

    if pro:
        pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")

    sidecar = {}
    if sidecar_path.is_file():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if stackup:
        for k in ("layers", "thickness", "copper_oz"):
            if k in stackup:
                sidecar[k] = stackup[k]
        changed.append(f"stackup: {sorted(set(stackup) & {'layers','thickness','copper_oz'})}")
    if mask and "pad_to_mask_clearance" in mask:
        sidecar["pad_to_mask_clearance"] = mask["pad_to_mask_clearance"]
        # Patch the EXISTING board too so exports pick it up immediately.
        if pcb_path.is_file():
            text = pcb_path.read_text(encoding="utf-8", errors="replace")
            new, n = re.subn(r"\(pad_to_mask_clearance\s+[\d.-]+\)",
                             f"(pad_to_mask_clearance {mask['pad_to_mask_clearance']})",
                             text, count=1)
            if n:
                pcb_path.write_text(new, encoding="utf-8")
                changed.append("mask: patched into existing board file")
            else:
                changed.append("mask: stored; applies at next regeneration")
        else:
            changed.append("mask: stored; applies at next regeneration")
    if stackup or mask:
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n",
                                encoding="utf-8")

    snapshot = get_board_setup(base, out_dir)
    # Persist the new hash so the change isn't re-reported as a surprise.
    sidecar["setup_hash"] = snapshot["setup_hash"]
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

    return {"changed": changed, "board_setup": snapshot}
