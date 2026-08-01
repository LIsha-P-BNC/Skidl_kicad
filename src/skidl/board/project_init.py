"""
src/skidl/board/project_init.py

initialize_pcb_project's engine: LEARN (rule_discovery) -> CHOOSE (with a
source + reason per setting) -> WRITE (.kicad_pro + .board_config.json
sidecar) -> REPORT.

Nothing user-configured is ever overwritten: the ladder in
rule_discovery.py decides which value wins, and the report tells the
user exactly which values are theirs and which the AI chose and WHY.
"""

from __future__ import annotations
import json
import re
from pathlib import Path

from skidl.board.profiles import get_profile, MOUNTING_HOLES
from skidl.board.rule_discovery import discover_rules, discover_application


class _NetName:
    """Duck-typed net for the schematic classifiers (name-only)."""
    def __init__(self, name):
        self.name = name
        self.pins = []
        self.drive = None


def initialize_project(base: str, out_dir, layers=2, board_width=None,
                        board_height=None, mounting_holes=None,
                        manufacturer="jlcpcb", ground_pour=True,
                        kicad_cli: str = "", mechanical: dict = None,
                        ipc_class: int = None) -> dict:
    """Run the full init: discovery -> choices -> writes -> tagged report.
    Raises FileNotFoundError if the netlist doesn't exist yet."""
    out_dir = Path(out_dir)
    net_path = out_dir / f"{base}.net"
    if not net_path.is_file():
        raise FileNotFoundError(f"netlist not found: {net_path} -- run build first")

    profile = get_profile(manufacturer)
    disc = discover_rules(base, out_dir)
    app = discover_application(kicad_cli)

    # IPC product class: ONE parameter derives annular ring, spacing and
    # drill floors, mask expansion and courtyard spacing. It replaces
    # the PROFILE rung of the ladder only -- every user source above
    # still wins. Sticky via the sidecar across re-inits.
    ipc_report = None
    ipc_class = ipc_class if ipc_class is not None else \
        (disc.get("sidecar", {}) or {}).get("ipc_class")
    if ipc_class:
        from skidl.board.profiles import derive_ipc_rules
        ipc_report = derive_ipc_rules(ipc_class, profile)
        if ipc_report.get("ipc_class"):
            profile = dict(profile)
            profile["rules"] = ipc_report["rules"]
            profile["classes"] = ipc_report["classes"]
        else:
            ipc_class = None    # unknown class -- reported, not applied

    report = {"application": {
        "kicad_cli": app.get("kicad_cli"),
        "version": app.get("version"),
        "capabilities": app.get("capabilities"),
        "config_dir": app.get("config_dir"),
    }}
    settings = {}

    def choose(name, argument, project_val, board_val, system_val, ai_val,
               profile_val, unit=""):
        """The ladder, with a recorded WHY for every decision."""
        if argument is not None:
            v, src, why = argument, "argument", "explicitly requested in this call"
        elif project_val is not None:
            v, src, why = project_val, "project-user", \
                "found in the project's .kicad_pro (user's Board Setup / net-class dialogs)"
        elif board_val is not None:
            v, src, why = board_val, "board-user", \
                "found in the existing .kicad_pcb (user edited the board)"
        elif system_val is not None:
            v, src, why = system_val, "system-user", \
                f"this user's own app defaults ({disc['system_user']['config_file']})"
        elif ai_val is not None:
            v, src, why = ai_val, "ai", "kept from this project's previous AI choice"
        else:
            v, src, why = profile_val, "profile", \
                f"no user/system value found -> {manufacturer} profile default"
            if ipc_class:
                src = f"profile+ipc{ipc_class}"
                why += f" with IPC Class {ipc_class} floor applied"
        settings[name] = {"value": v, "source": src, "reason": why}
        return v

    sidecar_prev = disc.get("sidecar", {})
    sys_rules = disc["system_user"]["defaults"].get("rules", {})
    proj_rules = disc["project"]["design_rules"]

    # ---- design-rule minimums (per-key ladder) -------------------------
    design_rules = {}
    for key, prof_val in profile["rules"].items():
        design_rules[key] = choose(
            f"rules.{key}", None,
            proj_rules.get(key), None, sys_rules.get(key),
            sidecar_prev.get("rules", {}).get(key), prof_val)

    # ---- physical ------------------------------------------------------
    thickness = choose("thickness", None, None,
                       disc["board"].get("thickness"),
                       None, sidecar_prev.get("thickness"),
                       profile["thickness"])
    layers = choose("layers", layers if layers != 2 else None, None, None,
                    None, sidecar_prev.get("layers"), 2)

    # ---- outline / holes / pour / origin -------------------------------
    width = choose("board_width", board_width, None, None, None,
                   sidecar_prev.get("board_width"), None)
    height = choose("board_height", board_height, None, None, None,
                    sidecar_prev.get("board_height"), None)
    if settings["board_width"]["source"] == "profile":
        settings["board_width"].update(
            value=None, source="ai",
            reason="no dimensions given -> outline auto-sized from placed parts")
        settings["board_height"].update(
            value=None, source="ai",
            reason="no dimensions given -> outline auto-sized from placed parts")
        width = height = None
    holes = choose("mounting_holes", mounting_holes, None, None, None,
                   sidecar_prev.get("mounting_holes"), None)
    if holes and str(holes) not in MOUNTING_HOLES:
        settings["mounting_holes"]["reason"] += \
            f" (unknown size {holes!r} -> M3 used)"
        holes = "M3"
        settings["mounting_holes"]["value"] = holes
    pour = choose("ground_pour", None if ground_pour else False, None, None,
                  None, sidecar_prev.get("ground_pour"), True)
    if pour is True and settings["ground_pour"]["source"] == "profile":
        settings["ground_pour"]["reason"] = \
            "standard practice: B.Cu GND pour improves EMI/grounding (disable with ground_pour=False)"

    # ---- mechanical constraints (enclosure-driven, professional-first) --
    mech = choose("mechanical", mechanical, None, None, None,
                  sidecar_prev.get("mechanical"), None)
    if mech:
        problems = _validate_mechanical(mech, net_path)
        if problems:
            settings["mechanical"]["reason"] += \
                f" -- REJECTED: {'; '.join(problems)}"
            settings["mechanical"]["value"] = None
            mech = None

    # ---- net classes ---------------------------------------------------
    net_classes, assignments = _auto_net_classes(
        net_path, disc, profile, design_rules, settings)

    # ---- write .kicad_pro ---------------------------------------------
    _write_project(disc["paths"]["pro"], net_classes, assignments, design_rules)

    # ---- write sidecar (AI-owned board-level params) -------------------
    sidecar = {
        "manufacturer": manufacturer,
        "rules": design_rules,
        "thickness": thickness,
        "layers": layers,
        "board_width": width,
        "board_height": height,
        "mounting_holes": holes,
        "ground_pour": bool(pour),
        "net_class_assignments": assignments,
    }
    if mech:
        sidecar["mechanical"] = mech
    if ipc_class and ipc_report:
        sidecar["ipc_class"] = int(ipc_class)
        sidecar["mask_expansion"] = ipc_report["mask_expansion"]
        report["ipc"] = {
            "class": int(ipc_class), "note": ipc_report["note"],
            "raised": ipc_report["raised"],
            "disclaimer": ("design targets from public IPC-2221B/6012 "
                           "figures -- not an IPC certification"),
        }
        if int(ipc_class) == 3 and _has_fine_pitch_parts(net_path):
            report["ipc"]["warning"] = (
                "Class 3 clearance floors conflict with fine-pitch parts "
                "(<0.65 mm) on this design -- routing may be impossible; "
                "a user rule override is required, nothing is silently "
                "downgraded")
    Path(disc["paths"]["sidecar"]).write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")

    report["settings"] = settings
    report["net_classes"] = net_classes
    report["net_class_assignments"] = assignments
    report["custom_drc_rules_file"] = disc["project"]["custom_drc_rules"]
    report["board_setup_carry_forward"] = {
        k: bool(disc["board"].get(k + "_text"))
        for k in ("setup", "layers")
    } if disc["board"] else {}
    report["sidecar"] = disc["paths"]["sidecar"]
    return report


def _validate_mechanical(mech: dict, net_path) -> list:
    """Sanity-check a `mechanical` spec BEFORE it is stored: connector
    refs must exist in the netlist, keepout rects and hole positions
    must lie inside the fixed board (when one is given). Returns a list
    of problem strings -- empty means valid."""
    problems = []
    if not isinstance(mech, dict):
        return ["mechanical must be an object"]
    board = mech.get("board") or {}
    W, H = board.get("width"), board.get("height")
    try:
        net_txt = Path(net_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        net_txt = ""

    def inside(x, y):
        return (W is None or 0 <= x <= W) and (H is None or 0 <= y <= H)

    for c in mech.get("connectors", []) or []:
        ref = c.get("ref", "")
        if f'(ref "{ref}")' not in net_txt and f"(ref {ref})" not in net_txt:
            problems.append(f"connector ref {ref!r} not in netlist")
        if c.get("edge") not in ("left", "right", "top", "bottom"):
            problems.append(f"connector {ref}: edge must be left/right/top/bottom")
    holes = (mech.get("mounting_holes") or {}).get("positions") or []
    for (x, y) in holes:
        if not inside(x, y):
            problems.append(f"mounting hole ({x},{y}) outside board {W}x{H}")
    for k in mech.get("keepouts", []) or []:
        rect = k.get("rect")
        poly = k.get("polygon")
        if not rect and not poly:
            problems.append(f"keepout {k.get('name')!r}: needs rect or polygon")
            continue
        pts = ([(rect[0], rect[1]), (rect[2], rect[3])] if rect else poly)
        for (x, y) in pts:
            if not inside(x, y):
                problems.append(f"keepout {k.get('name')!r} point ({x},{y}) "
                                f"outside board {W}x{H}")
    return problems


def _auto_net_classes(net_path, disc, profile, design_rules, settings):
    """Assign nets to classes. Author/user-set classes (from the netlist's
    (class ...) or the user's .kicad_pro) pass through untouched; only
    Default-class nets are auto-classified by the schematic engine's
    net_classify regexes. Returns (classes, {net_name: class_name})."""
    from skidl.board.pcb_writer import _read_netlist
    from skidl.schematics.net_classify import classify_net_role, classify_net_function

    _comps, nets = _read_netlist(Path(net_path))
    user_classes = disc["project"]["net_classes"]  # rules user already has

    assignments = {}
    used = {"Default"}
    for net_name, info in nets.items():
        netlist_class = info["class"]
        if netlist_class != "Default":
            # Author set a NetClass in SKiDL -- respected as-is.
            assignments[net_name] = netlist_class
            used.add(netlist_class)
            settings[f"net.{net_name}"] = {
                "value": netlist_class, "source": "netlist",
                "reason": "NetClass set by the circuit author in SKiDL"}
            continue
        n = _NetName(net_name)
        func = classify_net_function(n)
        if func == "power" or classify_net_role(n) is not None:
            assignments[net_name] = "Power"
            used.add("Power")
            settings[f"net.{net_name}"] = {
                "value": "Power", "source": "ai",
                "reason": "name matches power/ground rail pattern -> wider tracks"}
        elif func == "usb_diff":
            assignments[net_name] = "USB"
            used.add("USB")
            settings[f"net.{net_name}"] = {
                "value": "USB", "source": "ai",
                "reason": "USB differential-pair name pattern -> controlled narrow tracks"}

    # LEARN the board's own parts: dense boards with fine-pitch footprints
    # cannot thread wide power tracks through the fan-out (measured: 0.5mm
    # Power tracks left 20 connections unrouted on a 71-part LQFP board).
    fine_pitch = _has_fine_pitch_parts(net_path)

    classes = {}
    min_w = design_rules.get("min_track_width", 0.127)
    min_c = design_rules.get("min_clearance", 0.1)
    for cls in sorted(used):
        base_rules = dict(profile["classes"].get(cls, profile["classes"]["Default"]))
        # user's .kicad_pro class rules WIN over profile
        user_set = cls in user_classes
        base_rules.update(user_classes.get(cls, {}))
        if cls == "Power" and fine_pitch and not user_set:
            base_rules["width"] = min(base_rules.get("width", 0.4), 0.3)
            settings["class.Power.width_clamp"] = {
                "value": base_rules["width"], "source": "ai",
                "reason": "fine-pitch footprints on this board -> power width "
                          "reduced for routability (user can override in KiCad)"}
        # profile-clamp: never below the fab's minimums
        base_rules["width"] = max(base_rules.get("width", 0.25), min_w)
        base_rules["clearance"] = max(base_rules.get("clearance", 0.15), min_c)
        classes[cls] = base_rules
        src = "project-user" if cls in user_classes else "profile"
        settings[f"class.{cls}"] = {
            "value": base_rules, "source": src,
            "reason": ("rules found in .kicad_pro (user-set), fab-minimum clamped"
                       if src == "project-user" else
                       "profile defaults for this class, fab-minimum clamped")}

    # IPC-2152-informed width plan (ADVISORY estimates): nets whose
    # required width exceeds their class get synthesized PWR_* classes
    # so the router honors them with zero router changes. The fine-pitch
    # routability clamp still wins -- a wide track that cannot be routed
    # helps nobody; the tension is recorded, not hidden.
    try:
        from skidl.board.width_engine import net_width_plan, bucket_classes
        currents = (disc.get("sidecar", {}) or {}).get("currents") or {}
        nets_by_class = {}
        for net_name, cls in assignments.items():
            nets_by_class.setdefault(cls, []).append(net_name)
        plan = net_width_plan(nets_by_class, {"classes": classes,
                                              "copper_oz": profile.get("copper_oz", 1),
                                              "rules": design_rules},
                              currents=currents)
        if plan and not fine_pitch:
            for cname, spec in bucket_classes(plan).items():
                classes[cname] = dict(classes.get("Power",
                                                  classes["Default"]))
                classes[cname]["width"] = spec["width"]
                for net in spec["nets"]:
                    assignments[net] = cname
                    settings[f"net.{net}"] = {
                        "value": cname, "source": "ai",
                        "reason": (f"IPC-2152-informed estimate: "
                                   f"{plan[net]['amps']}A ({plan[net]['basis']})"
                                   f" -> {spec['width']}mm -- ADVISORY, "
                                   "verify actual currents")}
        elif plan and fine_pitch:
            settings["width_plan_deferred"] = {
                "value": sorted(plan), "source": "ai",
                "reason": ("width plan suggests wider tracks but fine-pitch "
                           "parts make them unroutable -- kept at class "
                           "widths; declare sidecar 'currents' and review "
                           "manually")}
        settings["width_plan"] = {
            "value": plan, "source": "ai",
            "reason": "IPC-2152-informed ADVISORY width estimates"}
    except Exception as exc:
        settings["width_plan"] = {"value": None, "source": "ai",
                                  "reason": f"width engine failed: {exc!r}"}
    return classes, assignments


def _has_fine_pitch_parts(net_path, threshold=0.65) -> bool:
    """True when any footprint on the board has adjacent pads closer than
    `threshold` mm center-to-center (QFPs, 0.5mm USB-C, module edge
    castellations) -- learned from the actual footprint geometry."""
    from skidl.board.pcb_writer import _read_netlist
    from skidl.board.footprint_libs import load_pads_and_courtyard, FootprintNotFoundError

    comps, _nets = _read_netlist(Path(net_path))
    seen = set()
    for comp in comps.values():
        fp = comp.get("footprint")
        if not fp or fp in seen:
            continue
        seen.add(fp)
        try:
            pads, _ = load_pads_and_courtyard(fp)
        except (FootprintNotFoundError, ValueError):
            continue
        pts = [(p.x, p.y) for p in pads if p.pad_type != "np_thru_hole"]
        pts.sort()
        for a, b in zip(pts, pts[1:]):
            if abs(a[0] - b[0]) < threshold and abs(a[1] - b[1]) < threshold \
                    and (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 < threshold ** 2:
                return True
    return False


def _write_project(pro_path, net_classes, assignments, design_rules):
    """Fold classes + patterns + rules into .kicad_pro, preserving every
    user key (same discipline as update_project_net_classes)."""
    pro_path = Path(pro_path)
    if pro_path.is_file():
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    else:
        pro = {"meta": {"filename": pro_path.name, "version": 1}}

    rules_out = (pro.setdefault("board", {})
                    .setdefault("design_settings", {})
                    .setdefault("rules", {}))
    for k, v in design_rules.items():
        rules_out.setdefault(k, v)

    from skidl.board.pcb_writer import (
        canonical_class_entry, _net_settings_meta_version,
    )
    net_settings = pro.setdefault("net_settings", {})
    existing = {c.get("name"): c for c in net_settings.get("classes", [])
                if isinstance(c, dict)}
    out_classes = []
    prio = 0
    for name, rules in net_classes.items():
        # setdefault semantics preserved: user-set values in the existing
        # entry win; the canonical shape is applied around them.
        base = dict(rules)
        entry = canonical_class_entry(
            name, base, None,
            priority=None if name == "Default" else prio)
        entry.update(existing.get(name, {}))   # user values overwrite ours
        entry["name"] = name
        out_classes.append(entry)
        if name != "Default":
            prio += 1
    for name, entry in existing.items():
        if name not in net_classes:
            out_classes.append(canonical_class_entry(name, {}, entry))
    net_settings["classes"] = out_classes
    net_settings["meta"] = {
        "version": _net_settings_meta_version(net_settings.get("meta"))}
    patterns = [{"netclass": cls, "pattern": net}
                for net, cls in sorted(assignments.items()) if cls != "Default"]
    if patterns:
        net_settings["netclass_patterns"] = patterns

    pro_path.write_text(json.dumps(pro, indent=2) + "\n", encoding="utf-8")
