"""
src/skidl/board/board_setup.py

Board Setup as the LIVE source of truth -- read fresh before every major
operation, because the user can change it in the KiCad application at
any moment.

DYNAMIC discovery, no fixed schema (the core requirement): the readers
capture WHATEVER the files contain -- every (setup ...) sub-entry of the
board file via a generic s-expression->data converter, and the ENTIRE
design_settings/net_settings objects of the .anvil_pro -- including keys
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
    pcb_path = out_dir / f"{base}.anvil_pcb"
    pro_path = out_dir / f"{base}.anvil_pro"
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
    board_text = ""
    if pcb_path.is_file():
        board_text = pcb_path.read_text(encoding="utf-8", errors="replace")
        text = board_text
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

    # Project-file side: the ENTIRE .anvil_pro -- text_variables,
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
        layer_count, layer_src = len(copper), "board file (layer table)"
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
    ext = edge_cuts_extents(board_text) if board_text else None
    consumed["board_outline"] = {
        "value": ({"minx": ext[0], "miny": ext[1], "maxx": ext[2],
                   "maxy": ext[3],
                   "width_mm": round(ext[2] - ext[0], 2),
                   "height_mm": round(ext[3] - ext[1], 2)} if ext else None),
        "used_in": "REAL Edge.Cuts extents -- adopted as board size when "
                   "the user drew/edited the outline"}
    consumed["drc_severities"] = {
        "value": raw["project_file"]["design_settings"].get("rule_severities",
                                                            {}) or {},
        "used_in": "kicad-cli DRC severity mapping"}

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
        if k not in ("rules", "rule_severities"):
            preserved.append(f"design_settings: {k}")
    for k in raw["board_file"]:
        if k not in ("setup", "layers", "general", "layer_table") \
                and not k.startswith("_"):
            preserved.append(f"board file block: {k}")
    for k, v in raw["project_file"].get("everything_else", {}).items():
        if v not in (None, {}, [], ""):
            preserved.append(f"project: {k}")

    # ---- saved-files-as-truth extensions -------------------------------
    srcs = sidecar.get("_sources", {}) or {}
    sidecar_meta = {k: {"value": sidecar[k], "source": srcs.get(k, "ai")}
                    for k in _SIDECAR_META_KEYS if k in sidecar}
    setup_hash = _hash_raw(raw)
    canon = {k: v for k, v in sidecar.items()
             if k not in ("setup_hash", "state_hash", "engine_overrides")}
    state_hash = hashlib.sha256(
        (setup_hash + json.dumps(canon, sort_keys=True, default=str))
        .encode("utf-8")).hexdigest()
    return {
        "raw": raw,
        "consumed": consumed,
        "preserved": preserved,
        "preserved_note": ("these were FOUND on this system but are not "
                           "interpreted by the pipeline -- they are kept "
                           "verbatim (carry-forward / setdefault) and never lost"),
        "sidecar_meta": sidecar_meta,
        "engine_overrides": sidecar.get("engine_overrides", {}),
        "edit_status": board_edit_status(base, out_dir),
        "pending_changes": compute_pending_changes(base, out_dir),
        "manual_changes": diff_saved_vs_snapshot(base, out_dir),
        "setup_hash": setup_hash,
        "state_hash": state_hash,
    }


def _hash_raw(raw: dict) -> str:
    return hashlib.sha256(
        json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def compute_setup_hash(base: str, out_dir) -> str:
    return get_board_setup(base, out_dir)["setup_hash"]


def compute_state_hash(base: str, out_dir) -> str:
    """setup_hash + canonical sidecar: flips when EITHER the saved KiCad
    files OR the AI sidecar change -- pending sidecar requests become
    visible to change-detection without invalidating human approvals
    (approvals stay bound to setup_hash = the physical board only)."""
    return get_board_setup(base, out_dir)["state_hash"]


# ---------------------------------------------------------------------------
# Shared readers + arbitration: SAVED KiCad files are the source of truth
# when the user saved them; the sidecar holds KiCad-less fields and
# PENDING requests for KiCad-owned fields awaiting regeneration.
# ---------------------------------------------------------------------------

_SIDECAR_META_KEYS = (
    "manufacturer", "ipc_class", "mechanical", "board_width", "board_height",
    "mounting_holes", "ground_pour", "currents", "currents_meta", "copper_oz",
    "requirements_confirmed", "requirements_asked", "layers", "thickness",
    "pad_to_mask_clearance",
)


def load_sidecar(base: str, out_dir) -> dict:
    """Tolerant sidecar loader -- {} on missing/corrupt."""
    p = Path(out_dir) / f"{base}.board_config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def stamp_board_fingerprint(base: str, out_dir) -> None:
    """Record the generated board's hash (engine-side twin of the server
    stamp) so later reads can tell 'exactly what the pipeline wrote' from
    'a human changed it'. MUST run after every machine write of the board
    file -- a stale fingerprint makes the arbitration read our own
    intermediate build output as a user edit."""
    out_dir = Path(out_dir)
    pcb = out_dir / f"{base}.anvil_pcb"
    if pcb.is_file():
        digest = hashlib.sha256(pcb.read_bytes()).hexdigest()
        (out_dir / f"{base}.board_fingerprint.json").write_text(
            json.dumps({"sha256": digest}), encoding="utf-8")


def edge_cuts_extents(board_text):
    """(minx, miny, maxx, maxy) of the REAL board outline from Edge.Cuts
    graphics; None when the board has no outline drawn."""
    xs, ys = [], []
    for m in re.finditer(
            r'\(gr_(?:line|rect|arc|circle)\b(.*?)\n\t?\)', board_text, re.S):
        blk = m.group(1)
        if "Edge.Cuts" not in blk:
            continue
        for pm in re.finditer(
                r'\((?:start|end|center|mid)\s+([\d.-]+)\s+([\d.-]+)\)', blk):
            xs.append(float(pm.group(1)))
            ys.append(float(pm.group(2)))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def read_saved_state(base: str, out_dir) -> dict:
    """What the SAVED KiCad files say RIGHT NOW -- the truth side of the
    arbitration. Fast block-scoped reads, no full parse."""
    out_dir = Path(out_dir)
    pcb = out_dir / f"{base}.anvil_pcb"
    pro = out_dir / f"{base}.anvil_pro"
    state = {"exists": pcb.is_file(), "generator": None, "layers": None,
             "thickness": None, "pad_to_mask_clearance": None, "paper": None,
             "edge_cuts": None, "net_classes": {}, "design_rules": {},
             "rule_severities": {},
             "custom_dru": (out_dir / f"{base}.kicad_dru").is_file()}
    if state["exists"]:
        text = pcb.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'\(generator "([^"]+)"\)', text[:300])
        if m:
            state["generator"] = m.group(1)
        lt, _ = _extract_block(text, "layers")
        if lt:
            copper = re.findall(r'"\w+\.Cu"', lt)
            if copper:
                state["layers"] = len(copper)
        gt, _ = _extract_block(text, "general")
        if gt:
            m = re.search(r"\(thickness\s+([\d.]+)\)", gt)
            if m:
                state["thickness"] = float(m.group(1))
        st, _ = _extract_block(text, "setup")
        if st:
            m = re.search(r"\(pad_to_mask_clearance\s+([\d.-]+)\)", st)
            if m:
                state["pad_to_mask_clearance"] = float(m.group(1))
        m = re.search(r'\(paper\s+"([^"]+)"', text)
        if m:
            state["paper"] = m.group(1)
        ext = edge_cuts_extents(text)
        if ext:
            state["edge_cuts"] = {
                "minx": ext[0], "miny": ext[1], "maxx": ext[2], "maxy": ext[3],
                "width_mm": round(ext[2] - ext[0], 2),
                "height_mm": round(ext[3] - ext[1], 2)}
    if pro.is_file():
        try:
            p = json.loads(pro.read_text(encoding="utf-8"))
            ds = p.get("board", {}).get("design_settings", {}) or {}
            state["design_rules"] = ds.get("rules", {}) or {}
            state["rule_severities"] = ds.get("rule_severities", {}) or {}
            for entry in (p.get("net_settings", {}) or {}).get("classes", []) or []:
                if isinstance(entry, dict) and entry.get("name"):
                    state["net_classes"][entry["name"]] = entry
        except Exception:
            pass
    return state


def board_edit_status(base: str, out_dir) -> dict:
    """Fingerprint-first manual-edit arbitration -- the ONE implementation
    (the server's _detect_manual_edits follows the same rule). The
    pipeline's own zone-fill rewrites the file with generator "pcbnew",
    so a matching fingerprint proves machine-generated regardless of the
    generator token; the token only decides when no fingerprint exists.
    machine_generated: True | False | None (no board yet)."""
    out_dir = Path(out_dir)
    pcb = out_dir / f"{base}.anvil_pcb"
    if not pcb.is_file():
        return {"machine_generated": None, "reason": "no board file yet"}
    fp = out_dir / f"{base}.board_fingerprint.json"
    if fp.is_file():
        try:
            recorded = json.loads(fp.read_text(encoding="utf-8")).get("sha256")
            if recorded:
                actual = hashlib.sha256(pcb.read_bytes()).hexdigest()
                if recorded == actual:
                    return {"machine_generated": True,
                            "reason": "fingerprint matches -- exactly what "
                                      "the pipeline wrote"}
                return {"machine_generated": False,
                        "reason": "board changed since the pipeline wrote it "
                                  "-- treat as the user's manual work"}
        except Exception:
            pass
    head = pcb.read_text(encoding="utf-8", errors="replace")[:300]
    if '(generator "skidl_board")' not in head:
        return {"machine_generated": False,
                "reason": "board was SAVED from the PCB editor (generator "
                          "token changed) -- user's manual work"}
    return {"machine_generated": True,
            "reason": "generator skidl_board (no fingerprint recorded)"}


def compute_pending_changes(base: str, out_dir) -> dict:
    """Sidecar requests for KiCad-owned fields that the SAVED board does
    not reflect yet -- applied at the next regeneration. Only meaningful
    on machine-generated boards: on a user-edited board the saved file
    WINS (adopt path) instead of being 'pending'."""
    out_dir = Path(out_dir)
    saved = read_saved_state(base, out_dir)
    if not saved["exists"]:
        return {}
    if board_edit_status(base, out_dir)["machine_generated"] is False:
        return {}
    sc = load_sidecar(base, out_dir)
    srcs = sc.get("_sources", {}) or {}
    pend = {}

    def add(field, saved_v, req_v, via_key=None):
        pend[field] = {"requested": req_v, "saved": saved_v,
                       "requested_via": srcs.get(via_key or field, "unknown"),
                       "applies_when": "next create_pcb"}

    if sc.get("layers") is not None and saved["layers"] is not None \
            and int(sc["layers"]) != int(saved["layers"]):
        add("layers", saved["layers"], int(sc["layers"]))
    if sc.get("thickness") is not None and saved["thickness"] is not None \
            and abs(float(sc["thickness"]) - float(saved["thickness"])) > 1e-6:
        add("thickness", saved["thickness"], float(sc["thickness"]))
    if sc.get("pad_to_mask_clearance") is not None:
        sv = saved["pad_to_mask_clearance"]
        sv_eff = 0.0 if sv is None else float(sv)
        if abs(float(sc["pad_to_mask_clearance"]) - sv_eff) > 1e-6:
            add("pad_to_mask_clearance", sv, float(sc["pad_to_mask_clearance"]))
    mech = (sc.get("mechanical") or {}).get("board") or {}
    req_w = mech.get("width") or sc.get("board_width")
    req_h = mech.get("height") or sc.get("board_height")
    ec = saved["edge_cuts"]
    if req_w and req_h and ec and (
            abs(float(req_w) - ec["width_mm"]) > 0.5
            or abs(float(req_h) - ec["height_mm"]) > 0.5):
        add("board_size",
            {"width_mm": ec["width_mm"], "height_mm": ec["height_mm"]},
            {"width_mm": float(req_w), "height_mm": float(req_h)},
            via_key="mechanical" if mech.get("width") else "board_width")
    return pend


# ---------------------------------------------------------------------------
# Manual-change awareness: snapshot -> diff -> AUTO-ADOPT (KiCad is truth)
# ---------------------------------------------------------------------------

def write_state_snapshot(base: str, out_dir) -> Path:
    """Post-build snapshot of the saved state -- the baseline a later
    session diffs against to EXPLAIN what the user changed (the
    fingerprint only says THAT something changed)."""
    out_dir = Path(out_dir)
    snap = {"saved_state": read_saved_state(base, out_dir),
            "setup_hash": get_board_setup(base, out_dir)["setup_hash"]}
    p = out_dir / f"{base}.state_snapshot.json"
    p.write_text(json.dumps(snap, indent=2, default=str) + "\n",
                 encoding="utf-8")
    return p


def diff_saved_vs_snapshot(base: str, out_dir) -> list:
    """[{field, was, now}] between the last build's snapshot and the saved
    files RIGHT NOW. Empty when no snapshot exists (pre-existing projects)
    or nothing changed."""
    out_dir = Path(out_dir)
    p = out_dir / f"{base}.state_snapshot.json"
    if not p.is_file():
        return []
    try:
        was = json.loads(p.read_text(encoding="utf-8"))["saved_state"]
    except Exception:
        return []
    now = read_saved_state(base, out_dir)
    diffs = []
    for field in ("layers", "thickness", "pad_to_mask_clearance", "paper",
                  "generator", "edge_cuts", "design_rules",
                  "rule_severities", "net_classes"):
        if was.get(field) != now.get(field):
            diffs.append({"field": field, "was": was.get(field),
                          "now": now.get(field)})
    return diffs


def adopt_manual_changes(base: str, out_dir) -> dict:
    """KiCad is truth: on a USER-EDITED board, adopt the fields the user
    ACTUALLY CHANGED (snapshot diff) into the sidecar (source
    "board-user") WITHOUT asking -- the user already decided by saving in
    the application. Fields the user did NOT touch are left alone, so a
    pending update_board_setup request (e.g. layers 4->2 asked in chat
    just before an outline-only edit) survives the adoption instead of
    being clobbered by the board's incidental current value. Wholesale
    adoption only when no snapshot exists (pre-existing projects, no
    baseline to diff against). The destructive-regeneration confirmation
    is a separate concern (layout, not setup).
    Returns {user_edited, manual_changes, adopted}."""
    out_dir = Path(out_dir)
    status = board_edit_status(base, out_dir)
    out = {"user_edited": status["machine_generated"] is False,
           "manual_changes": diff_saved_vs_snapshot(base, out_dir),
           "adopted": {}}
    if not out["user_edited"]:
        return out
    saved = read_saved_state(base, out_dir)
    sc = load_sidecar(base, out_dir)
    srcs = sc.get("_sources") or {}
    adopted = {}
    snap_exists = (out_dir / f"{base}.state_snapshot.json").is_file()
    changed = {d["field"] for d in out["manual_changes"]}

    def _sel(field):
        return (not snap_exists) or field in changed

    def _adopt(field, new):
        old = sc.get(field)
        if new is not None and old != new:
            adopted[field] = {"was": old, "now": new}
            sc[field] = new
            srcs[field] = "board-user"

    if _sel("layers"):
        _adopt("layers", saved["layers"])
    if _sel("thickness"):
        _adopt("thickness", saved["thickness"])
    if _sel("pad_to_mask_clearance") \
            and saved["pad_to_mask_clearance"] is not None:
        _adopt("pad_to_mask_clearance", saved["pad_to_mask_clearance"])
    if saved["edge_cuts"] and _sel("edge_cuts"):
        _adopt("board_width", saved["edge_cuts"]["width_mm"])
        _adopt("board_height", saved["edge_cuts"]["height_mm"])
        mb = (sc.get("mechanical") or {}).get("board")
        if mb:
            mb["width"] = saved["edge_cuts"]["width_mm"]
            mb["height"] = saved["edge_cuts"]["height_mm"]
    if adopted:
        sc["_sources"] = srcs
        (out_dir / f"{base}.board_config.json").write_text(
            json.dumps(sc, indent=2) + "\n", encoding="utf-8")
    out["adopted"] = adopted
    return out


def read_edge_cuts_blocks(board_text: str) -> list:
    """Verbatim gr_* graphics blocks on Edge.Cuts (paren-matched) -- the
    user's own drawn outline, carried through regeneration."""
    blocks = []
    for m in re.finditer(r"\(gr_(?:line|rect|arc|circle|poly|curve)\b",
                         board_text):
        try:
            end = _match_paren(board_text, m.start())
        except Exception:
            continue
        blk = board_text[m.start():end]
        if '"Edge.Cuts"' in blk:
            blocks.append(blk)
    return blocks


def edge_cuts_is_rectangle(blocks) -> bool:
    """True when the outline is a plain axis-aligned rectangle (one
    gr_rect or 4 straight axis-aligned gr_lines) -- then rectangular
    regeneration at the adopted size is lossless. Anything else (arcs,
    polygons, diagonals) must be carried VERBATIM or the user's drawn
    shape is destroyed."""
    if not blocks:
        return True
    if any(b.startswith(("(gr_arc", "(gr_circle", "(gr_poly", "(gr_curve"))
           for b in blocks):
        return False
    rects = [b for b in blocks if b.startswith("(gr_rect")]
    lines = [b for b in blocks if b.startswith("(gr_line")]
    if rects and not lines:
        return len(rects) == 1
    if lines and not rects and len(lines) == 4:
        for b in lines:
            pts = re.findall(r"\((?:start|end)\s+([\d.-]+)\s+([\d.-]+)\)", b)
            if len(pts) == 2:
                (x1, y1), (x2, y2) = pts
                if abs(float(x1) - float(x2)) > 1e-6 \
                        and abs(float(y1) - float(y2)) > 1e-6:
                    return False
        return True
    return False


# ---------------------------------------------------------------------------
# Applying user-requested changes
# ---------------------------------------------------------------------------

_OUR_TO_KICAD = {"width": "track_width", "via_size": "via_diameter"}


def update_board_setup(base: str, out_dir, stackup=None, constraints=None,
                        net_classes=None, via_rules=None, mask=None,
                        drc_severities=None, board=None) -> dict:
    """Apply the user's EXPLICIT Board Setup changes (their instruction is
    authoritative -- direct overwrite, unlike the never-clobber init
    path). Only the sections passed are touched; everything else stays
    exactly as discovered.

    READ -> ACT -> VERIFY: after writing, the files are RE-READ and every
    requested field returns a verdict in `verified`:
      applied              visible in the saved files right now
      pending_regeneration recorded; the next create_pcb applies it
      failed               the write did not land (reported, never hidden)
    `board` = board-level fields KiCad has no dialog for: {board_width,
    board_height, mounting_holes, ground_pour, ipc_class, manufacturer,
    currents} -- ONE write surface; initialize_pcb_project remains the
    questionnaire path."""
    out_dir = Path(out_dir)
    pro_path = out_dir / f"{base}.anvil_pro"
    sidecar_path = out_dir / f"{base}.board_config.json"
    pcb_path = out_dir / f"{base}.anvil_pcb"
    changed = []
    # Arbitration status BEFORE we touch anything: if the board was
    # machine-generated, our own live patch below must re-stamp the
    # fingerprint or the next read would mistake it for a user edit.
    was_machine = board_edit_status(base, out_dir)["machine_generated"]

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
                if was_machine:
                    stamp_board_fingerprint(base, out_dir)
            else:
                changed.append("mask: stored; applies at next regeneration")
        else:
            changed.append("mask: stored; applies at next regeneration")

    # board-level fields KiCad has no dialog for (ONE write surface).
    if board:
        for k in ("board_width", "board_height", "mounting_holes",
                  "ground_pour", "ipc_class", "manufacturer"):
            if k in board:
                sidecar[k] = board[k]
        if board.get("currents"):
            cur = sidecar.get("currents") or {}
            cur.update(board["currents"])
            sidecar["currents"] = cur
            # source "user" = arrived via the tool call, NOT verified human
            # input; never treat "confirmed" as safety-relevant ground truth.
            meta = sidecar.get("currents_meta") or {}
            for net in board["currents"]:
                meta[net] = {"source": "user", "status": "confirmed"}
            sidecar["currents_meta"] = meta
        changed.append(f"board: {sorted(set(board))}")

    # Provenance: these values arrived as the user's explicit request.
    src_fields = []
    if stackup:
        src_fields += [k for k in ("layers", "thickness", "copper_oz")
                       if k in stackup]
    if mask and "pad_to_mask_clearance" in mask:
        src_fields.append("pad_to_mask_clearance")
    if board:
        src_fields += [k for k in ("board_width", "board_height",
                                   "mounting_holes", "ground_pour",
                                   "ipc_class", "manufacturer") if k in board]
    if src_fields:
        srcs = sidecar.get("_sources") or {}
        for f in src_fields:
            srcs[f] = "user"
        sidecar["_sources"] = srcs

    if stackup or mask or board:
        sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n",
                                encoding="utf-8")

    # ---- READ BACK + VERIFY (never just "Done") ------------------------
    snapshot = get_board_setup(base, out_dir)
    saved = read_saved_state(base, out_dir)
    verified = {}

    def _near(a, b):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return a == b

    def _ver(field, req, actual, live, note_pending=""):
        ok = _near(actual, req)
        if ok:
            verified[field] = {"requested": req, "actual_now": actual,
                               "status": "applied", "note": ""}
        elif live:
            verified[field] = {"requested": req, "actual_now": actual,
                               "status": "failed",
                               "note": "write did not land in the saved "
                                       "files -- reported, not hidden"}
        else:
            verified[field] = {"requested": req, "actual_now": actual,
                               "status": "pending_regeneration",
                               "note": note_pending or
                               "recorded; the next create_pcb applies it "
                               "to the board"}

    rules_now = snapshot["consumed"]["design_rules"]["value"]
    for k, v in (constraints or {}).items():
        _ver(f"constraints.{k}", v, rules_now.get(k), live=True)
    sev_now = snapshot["consumed"]["drc_severities"]["value"]
    for k, v in (drc_severities or {}).items():
        _ver(f"drc_severities.{k}", v, sev_now.get(k), live=True)
    classes_now = snapshot["consumed"]["net_classes"]["value"]
    for cls_name, rules_req in (net_classes or {}).items():
        for k, v in rules_req.items():
            kk = _OUR_TO_KICAD.get(k, k)
            _ver(f"net_class.{cls_name}.{kk}", v,
                 (classes_now.get(cls_name) or {}).get(kk), live=True)
    for k, v in (via_rules or {}).items():
        kk = _OUR_TO_KICAD.get(k, k)
        _ver(f"net_class.Default.{kk}", v,
             (classes_now.get("Default") or {}).get(kk), live=True)
    if stackup:
        if "layers" in stackup:
            _ver("layers", int(stackup["layers"]), saved["layers"], live=False)
        if "thickness" in stackup:
            _ver("thickness", stackup["thickness"], saved["thickness"],
                 live=False)
        if "copper_oz" in stackup:
            verified["copper_oz"] = {
                "requested": stackup["copper_oz"],
                "actual_now": sidecar.get("copper_oz"), "status": "applied",
                "note": "sidecar field (KiCad has no home for copper "
                        "weight); drives the IPC-2152 width engine"}
    if mask and "pad_to_mask_clearance" in mask:
        _ver("pad_to_mask_clearance", mask["pad_to_mask_clearance"],
             saved["pad_to_mask_clearance"], live=False)
    if board:
        ec = saved["edge_cuts"] or {}
        if "board_width" in board:
            _ver("board_width", board["board_width"], ec.get("width_mm"),
                 live=False)
        if "board_height" in board:
            _ver("board_height", board["board_height"], ec.get("height_mm"),
                 live=False)
        for k in ("mounting_holes", "ground_pour"):
            if k in board:
                verified[k] = {
                    "requested": board[k], "actual_now": sidecar.get(k),
                    "status": "pending_regeneration",
                    "note": "recorded; manifests on the board (NPTH pads / "
                            "pour zones) at the next create_pcb"}
        for k in ("ipc_class", "manufacturer"):
            if k in board:
                verified[k] = {"requested": board[k],
                               "actual_now": sidecar.get(k),
                               "status": "applied",
                               "note": "sidecar config field (KiCad has no "
                                       "home for it)"}
        if board.get("currents"):
            verified["currents"] = {
                "requested": board["currents"],
                "actual_now": sidecar.get("currents"), "status": "applied",
                "note": "user-declared; width plan uses them at the next "
                        "create_pcb"}

    # Persist the new hashes so the change isn't re-reported as a surprise.
    sidecar["setup_hash"] = snapshot["setup_hash"]
    sidecar["state_hash"] = snapshot["state_hash"]
    sidecar_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

    pending = [f for f, v in verified.items()
               if v["status"] == "pending_regeneration"]
    failed = [f for f, v in verified.items() if v["status"] == "failed"]
    return {"changed": changed, "verified": verified,
            "pending_fields": pending, "failed_fields": failed,
            "board_setup": snapshot}
