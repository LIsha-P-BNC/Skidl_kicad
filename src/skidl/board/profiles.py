"""
src/skidl/board/profiles.py

Manufacturer capability profiles -- the BOTTOM of the rule-discovery
ladder (rule_discovery.py): these values apply only when nothing above
them (tool argument, project files, the user's own KiCad defaults, a
previous AI choice) supplies a value.

Each profile carries:
  * board design-rule minimums (-> .kicad_pro board.design_settings.rules)
  * per-net-class defaults    (-> net classes / DSN routing rules)
  * physical defaults (thickness, copper weight)

Values are the fab's STANDARD (cheapest) service capabilities, with a
little margin -- never their absolute advanced-process limits.
"""

from __future__ import annotations

PROFILES = {
    # Default: most popular low-cost fab; numbers match its standard
    # 1-2 layer / 1 oz service with margin (abs limits: 0.09 track,
    # 0.09 clearance, 0.15 drill).
    "jlcpcb": {
        "description": "JLCPCB standard service (1-2 layer, 1 oz copper)",
        "rules": {
            "min_clearance": 0.1,
            "min_track_width": 0.127,
            "min_through_hole_diameter": 0.15,
            "min_hole_clearance": 0.2,
        },
        "classes": {
            "Default": {"width": 0.25, "clearance": 0.15, "via_size": 0.8, "via_drill": 0.4},
            "Power":   {"width": 0.4,  "clearance": 0.15, "via_size": 0.8, "via_drill": 0.4},
            "USB":     {"width": 0.2,  "clearance": 0.15, "via_size": 0.6, "via_drill": 0.3},
        },
        "thickness": 1.6,
        "copper_oz": 1,
    },
    "pcbway": {
        "description": "PCBWay standard service",
        "rules": {
            "min_clearance": 0.1,
            "min_track_width": 0.127,
            "min_through_hole_diameter": 0.2,
            "min_hole_clearance": 0.2,
        },
        "classes": {
            "Default": {"width": 0.25, "clearance": 0.15, "via_size": 0.8, "via_drill": 0.4},
            "Power":   {"width": 0.4,  "clearance": 0.15, "via_size": 0.8, "via_drill": 0.4},
            "USB":     {"width": 0.2,  "clearance": 0.15, "via_size": 0.6, "via_drill": 0.3},
        },
        "thickness": 1.6,
        "copper_oz": 1,
    },
    # Conservative numbers ANY fab can build. NOTE: 0.2 clearance is
    # incompatible with 0.5 mm-pitch parts (USB-C) -- measured; prefer
    # jlcpcb/pcbway for boards with fine-pitch footprints.
    "generic": {
        "description": "Conservative rules any fab can build (no fine-pitch parts)",
        "rules": {
            "min_clearance": 0.15,
            "min_track_width": 0.2,
            "min_through_hole_diameter": 0.3,
            "min_hole_clearance": 0.3,
        },
        "classes": {
            "Default": {"width": 0.3, "clearance": 0.2, "via_size": 0.8, "via_drill": 0.4},
            "Power":   {"width": 0.6, "clearance": 0.2, "via_size": 0.8, "via_drill": 0.4},
            "USB":     {"width": 0.25, "clearance": 0.2, "via_size": 0.8, "via_drill": 0.4},
        },
        "thickness": 1.6,
        "copper_oz": 1,
    },
}

# Mounting-hole footprints, verified present in MountingHole.pretty.
MOUNTING_HOLES = {
    "M2":   "MountingHole:MountingHole_2.2mm_M2",
    "M2.5": "MountingHole:MountingHole_2.7mm_M2.5",
    "M3":   "MountingHole:MountingHole_3.2mm_M3",
    "M4":   "MountingHole:MountingHole_4.3mm_M4",
}


def get_profile(name: str) -> dict:
    """Profile by name (case-insensitive); unknown names get 'generic'
    plus a note -- never a crash over a fab we haven't profiled."""
    prof = PROFILES.get((name or "").strip().lower())
    if prof:
        return prof
    out = dict(PROFILES["generic"])
    out["description"] = (f"unknown manufacturer {name!r} -> generic "
                          "conservative rules")
    return out


# IPC product-class DESIGN TARGETS, derived from publicly documented
# IPC-2221B / IPC-6012 figures. One parameter -> every tolerance:
# Class 1 general electronics, Class 2 dedicated-service equipment
# (a vehicle tracker), Class 3 high-reliability. These are design
# floors the fab profile is clamped UP to -- using them is NOT an IPC
# certification claim (that requires fab/assembly qualification).
IPC_CLASSES = {
    1: {"min_annular_ring": 0.05, "min_clearance_floor": 0.0,
        "min_drill_floor": 0.0, "mask_expansion": 0.05,
        "courtyard_gap_mult": 1.0,
        "note": "IPC Class 1 general product; fab minimums govern"},
    2: {"min_annular_ring": 0.10, "min_clearance_floor": 0.15,
        "min_drill_floor": 0.20, "mask_expansion": 0.05,
        "courtyard_gap_mult": 1.25,
        "note": "IPC Class 2 dedicated-service equipment"},
    3: {"min_annular_ring": 0.15, "min_clearance_floor": 0.20,
        "min_drill_floor": 0.30, "mask_expansion": 0.075,
        "courtyard_gap_mult": 1.5,
        "note": "IPC Class 3 high reliability"},
}


def derive_ipc_rules(ipc_class: int, profile: dict) -> dict:
    """Overlay for the profile rung of the discovery ladder: profile
    minimums clamped up to the IPC class floors, plus via sizes raised
    so every net class meets the class's annular ring (via pad >=
    drill + 2*ring). User-sourced values still win at every rung above.
    Returns {'rules': {...}, 'classes': {...}, 'mask_expansion',
    'courtyard_gap_mult', 'ipc_class', 'raised': [...]} -- `raised`
    records exactly which values the class changed (honest trail)."""
    ipc = IPC_CLASSES.get(int(ipc_class) if ipc_class else 0)
    if not ipc:
        return {"ipc_class": None,
                "error": f"unknown IPC class {ipc_class!r} (use 1, 2 or 3)"}
    raised = []
    rules = dict(profile.get("rules", {}))
    for key, floor in (("min_clearance", ipc["min_clearance_floor"]),
                       ("min_through_hole_diameter", ipc["min_drill_floor"])):
        if floor and rules.get(key, 0) < floor:
            raised.append(f"{key}: {rules.get(key)} -> {floor}")
            rules[key] = floor
    classes = {}
    ring = ipc["min_annular_ring"]
    for cname, cvals in (profile.get("classes") or {}).items():
        cv = dict(cvals)
        drill = cv.get("via_drill")
        size = cv.get("via_size")
        if drill and size and size < drill + 2 * ring:
            raised.append(f"classes.{cname}.via_size: {size} -> "
                          f"{round(drill + 2 * ring, 3)}")
            cv["via_size"] = round(drill + 2 * ring, 3)
        classes[cname] = cv
    return {"ipc_class": int(ipc_class), "rules": rules, "classes": classes,
            "mask_expansion": ipc["mask_expansion"],
            "courtyard_gap_mult": ipc["courtyard_gap_mult"],
            "note": ipc["note"], "raised": raised}
