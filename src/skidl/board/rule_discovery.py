"""
src/skidl/board/rule_discovery.py

LEARN FIRST, ACT SECOND -- the user's core requirement. Every user's
system is different, so nothing here is assumed: this module LEARNS the
environment (which application, which capabilities, which rules the user
already configured, where) and only then does project_init.py choose
values -- each with a recorded source AND reason ("why this value on
this system").

The rule ladder (first hit wins, per setting):
  1. explicit tool argument                                  "argument"
  2. project .anvil_pro (Board Setup / net-class dialogs)    "project-user"
     + project .kicad_dru custom rules file (detected, preserved)
  3. existing .anvil_pcb setup values                        "board-user"
  4. user's app-level defaults:
     %APPDATA%/kicad/<version>/pcbnew.json design_settings   "system-user"
  5. AI sidecar <base>.board_config.json (prior AI choices)  "ai"
  6. manufacturer profile (profiles.py)                      "profile"

Application discovery mirrors how the libraries are found (anvil_libs /
_find_bin): probe the actual install, its version, and its REAL
capabilities by asking the installed kicad-cli itself -- so the pipeline
adapts to stock KiCad, the Anvil CAD fork, old or new, on any machine.
"""

from __future__ import annotations
import glob
import json
import os
import re
import subprocess
from pathlib import Path

from skidl.board.profiles import get_profile

_NO_WINDOW = 0
try:
    _NO_WINDOW = subprocess.CREATE_NO_WINDOW
except AttributeError:
    pass


# ---------------------------------------------------------------------------
# Layer 4: the user's own app-level defaults
# ---------------------------------------------------------------------------

def find_app_config_dir():
    """The user's KiCad-family config dir for the NEWEST version present:
    %APPDATA%/kicad/<highest numeric version>/. Covers stock KiCad and
    the Anvil CAD fork (which also writes under kicad/<ver>). None when
    absent -- a system where the user never ran the app's UI."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidates = []
    for d in glob.glob(os.path.join(appdata, "kicad", "*")):
        base = os.path.basename(d)
        try:
            candidates.append((tuple(int(x) for x in base.split(".")), d))
        except ValueError:
            continue
    if not candidates:
        return None
    return sorted(candidates)[-1][1]


def read_system_user_defaults():
    """What this user ALWAYS uses for new boards: design_settings from
    their pcbnew.json (saved there by the app when they customize Board
    Setup defaults). Empty dict when never customized -- then the ladder
    correctly falls through to the profile."""
    cfg_dir = find_app_config_dir()
    if not cfg_dir:
        return {}, None
    p = Path(cfg_dir) / "pcbnew.json"
    if not p.is_file():
        return {}, None
    try:
        ds = json.loads(p.read_text(encoding="utf-8")).get("design_settings", {}) or {}
    except Exception:
        return {}, str(p)
    out = {}
    if ds.get("rules"):
        out["rules"] = ds["rules"]
    if ds.get("track_widths"):
        out["track_widths"] = ds["track_widths"]
    if ds.get("via_dimensions"):
        out["via_dimensions"] = ds["via_dimensions"]
    return out, str(p)


# ---------------------------------------------------------------------------
# Layer 3: existing board file setup values
# ---------------------------------------------------------------------------

def read_board_setup(pcb_path) -> dict:
    """User-relevant blocks from an existing .anvil_pcb: the raw (setup ...)
    and (layers ...) text (for verbatim carry-forward) plus parsed
    thickness/mask values. Empty dict when no board exists yet."""
    pcb_path = Path(pcb_path)
    if not pcb_path.is_file():
        return {}
    text = pcb_path.read_text(encoding="utf-8", errors="replace")
    out = {}
    for head in ("setup", "layers", "general"):
        m = re.search(r"\(\s*" + head + r"[\s(]", text)
        if m:
            from skidl.board.pcb_writer import _match_paren
            try:
                end = _match_paren(text, m.start())
                out[head + "_text"] = text[m.start():end]
            except Exception:
                continue
    setup = out.get("setup_text", "")
    m = re.search(r"\(pad_to_mask_clearance\s+([\d.-]+)\)", setup)
    if m:
        out["pad_to_mask_clearance"] = float(m.group(1))
    m = re.search(r"\(thickness\s+([\d.]+)\)", out.get("general_text", ""))
    if m:
        out["thickness"] = float(m.group(1))
    m = re.search(r'\(paper\s+"([^"]+)"', text)
    if m:
        out["paper"] = m.group(1)
    out["has_stackup"] = "(stackup" in setup
    return out


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def discover_rules(base: str, out_dir) -> dict:
    """LEARN everything rule-related this system/project already has.
    Returns a discovery report -- no decisions are made here, only facts
    with locations, so project_init can choose with reasons."""
    out_dir = Path(out_dir)
    pro_path = out_dir / f"{base}.anvil_pro"
    pcb_path = out_dir / f"{base}.anvil_pcb"
    dru_path = out_dir / f"{base}.kicad_dru"
    sidecar_path = out_dir / f"{base}.board_config.json"

    from skidl.board.pcb_writer import read_project_rules

    project_classes = read_project_rules(pro_path)
    project_design_rules = {}
    project_severities = {}
    if pro_path.is_file():
        try:
            pro = json.loads(pro_path.read_text(encoding="utf-8"))
            ds = pro.get("board", {}).get("design_settings", {})
            project_design_rules = ds.get("rules", {}) or {}
            project_severities = ds.get("rule_severities", {}) or {}
        except Exception:
            pass

    sidecar = {}
    if sidecar_path.is_file():
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    system_defaults, system_path = read_system_user_defaults()

    return {
        "project": {
            "pro_exists": pro_path.is_file(),
            "net_classes": project_classes,          # user/AI-set, in .anvil_pro
            "design_rules": project_design_rules,
            "rule_severities_present": bool(project_severities),
            "custom_drc_rules": dru_path.is_file(),  # .kicad_dru: preserved,
                                                     # kicad-cli honors it as-is
        },
        "board": read_board_setup(pcb_path),         # existing board setup
        "system_user": {
            "config_file": system_path,
            "defaults": system_defaults,             # {} = user never customized
        },
        "sidecar": sidecar,                          # prior AI choices
        "paths": {"pro": str(pro_path), "pcb": str(pcb_path),
                  "dru": str(dru_path), "sidecar": str(sidecar_path)},
    }


# ---------------------------------------------------------------------------
# Canonical post-hoc resolution: ONE interpretation of the board config
# for the requirements questionnaire, initialize's resolved summary and
# the build (which consumes the same sidecar/ladder).
# ---------------------------------------------------------------------------

_USER_SOURCES = ("argument", "user", "project-user", "board-user", "system-user")


def _entry(value, source):
    if source in _USER_SOURCES:
        status = "confirmed"
    elif source == "engine-escalation":
        # The engine changed a setting to route the board (recorded in
        # engine_overrides); reported, not re-asked.
        status = "engine-override"
    else:
        status = "unconfirmed"
    return {"value": value, "source": source, "status": status}


def resolve_board_config(base: str, out_dir) -> dict:
    """Resolve the board-level configuration from the files on disk --
    the same ladder the build consumes, no in-memory state. Per key:
    {value, source, status}. `source` comes from the sidecar's persisted
    `_sources` block when the sidecar supplies the value: init records
    how each value ARRIVED (argument vs ai vs profile), which a bare
    file re-read cannot know. status "confirmed" = produced by a user
    action; "unconfirmed" = AI/profile fallback or heuristic estimate.
    Keys with no answer anywhere resolve to value None, source "unset"
    (the questionnaire asks exactly those)."""
    out_dir = Path(out_dir)
    from skidl.board.board_setup import read_saved_state, board_edit_status
    disc = discover_rules(base, out_dir)
    sc = disc["sidecar"] or {}
    srcs = sc.get("_sources", {}) or {}
    saved = read_saved_state(base, out_dir)
    # SAVED KiCad files win when the USER saved them (fingerprint
    # arbitration); on machine-generated boards the sidecar is what the
    # next build applies -- a mismatch is flagged pending, not hidden.
    manual = bool(saved["exists"]) and \
        board_edit_status(base, out_dir)["machine_generated"] is False
    out = {}

    if manual and saved["layers"]:
        out["layers"] = _entry(int(saved["layers"]), "board-user")
    elif sc.get("layers"):
        e = _entry(int(sc["layers"]), srcs.get("layers", "ai"))
        if saved["exists"] and saved["layers"] \
                and int(saved["layers"]) != int(sc["layers"]):
            e["pending"] = True
        out["layers"] = e
    elif saved["exists"] and saved["layers"]:
        out["layers"] = _entry(int(saved["layers"]), "ai")
    else:
        out["layers"] = _entry(None, "unset")

    for key in ("manufacturer", "ipc_class"):
        out[key] = (_entry(sc[key], srcs.get(key, "ai"))
                    if sc.get(key) is not None else _entry(None, "unset"))
    out["ground_pour"] = (_entry(bool(sc["ground_pour"]),
                                 srcs.get("ground_pour", "ai"))
                          if "ground_pour" in sc else _entry(None, "unset"))

    if manual and saved["thickness"]:
        out["thickness"] = _entry(saved["thickness"], "board-user")
    elif sc.get("thickness"):
        e = _entry(sc["thickness"], srcs.get("thickness", "ai"))
        if saved["exists"] and saved["thickness"] \
                and abs(float(saved["thickness"]) - float(sc["thickness"])) > 1e-6:
            e["pending"] = True
        out["thickness"] = e
    elif saved["exists"] and saved["thickness"]:
        out["thickness"] = _entry(saved["thickness"], "ai")
    else:
        out["thickness"] = _entry(None, "unset")

    mech = sc.get("mechanical") or {}
    if manual and saved["edge_cuts"]:
        # The user drew/edited the outline in KiCad -- that IS the size.
        out["board_size"] = _entry(
            {"width_mm": saved["edge_cuts"]["width_mm"],
             "height_mm": saved["edge_cuts"]["height_mm"]}, "board-user")
    else:
        w = (mech.get("board") or {}).get("width") or sc.get("board_width")
        h = (mech.get("board") or {}).get("height") or sc.get("board_height")
        if w and h:
            src_key = "mechanical" if (mech.get("board") or {}).get("width") \
                else "board_width"
            e = _entry({"width_mm": w, "height_mm": h},
                       srcs.get(src_key, "ai"))
            ec = saved["edge_cuts"] if saved["exists"] else None
            if ec and (abs(float(w) - ec["width_mm"]) > 0.5
                       or abs(float(h) - ec["height_mm"]) > 0.5):
                e["pending"] = True
            out["board_size"] = e
        else:
            out["board_size"] = _entry(None, "unset")

    holes = (mech.get("mounting_holes") or {}).get("size") \
        or sc.get("mounting_holes")
    out["mounting_holes"] = (_entry(holes, srcs.get("mounting_holes", "ai"))
                             if holes else _entry(None, "unset"))
    out["mechanical"] = (_entry(mech, srcs.get("mechanical", "ai"))
                         if mech else _entry(None, "unset"))

    # currents: values in sidecar `currents`, provenance in the sibling
    # `currents_meta` (source:"user" means "arrived via the tool call",
    # NOT verified human input -- a misbehaving client could self-answer;
    # never treat "confirmed" as ground truth for safety-relevant logic).
    meta = sc.get("currents_meta") or {}
    out["currents"] = {
        net: {"amps": amps,
              "source": (meta.get(net) or {}).get("source", "ai"),
              "status": (meta.get(net) or {}).get("status", "unconfirmed")}
        for net, amps in (sc.get("currents") or {}).items()}

    # design-rule minimums, per-key: project > system > sidecar > profile
    # (the same order init's choose() walks).
    prof = get_profile(sc.get("manufacturer") or "jlcpcb")
    sys_rules = disc["system_user"]["defaults"].get("rules", {})
    rules = {}
    for key, prof_val in prof["rules"].items():
        if disc["project"]["design_rules"].get(key) is not None:
            rules[key] = _entry(disc["project"]["design_rules"][key],
                                "project-user")
        elif sys_rules.get(key) is not None:
            rules[key] = _entry(sys_rules[key], "system-user")
        elif (sc.get("rules") or {}).get(key) is not None:
            rules[key] = _entry(sc["rules"][key], srcs.get(f"rules.{key}", "ai"))
        else:
            rules[key] = _entry(prof_val, "profile")
    out["rules"] = rules

    # net classes live in .anvil_pro (user dialogs AND our writer both
    # edit it) -- provenance is genuinely mixed, so never claim confirmed.
    out["net_classes"] = {"value": disc["project"]["net_classes"] or None,
                          "source": "project-file", "status": "unconfirmed"}
    return out


def resolve(setting: str, argument, discovery: dict, ai_value, profile_value):
    """One ladder resolution -> (value, source, reason). `discovery` keys
    checked per setting are the caller's job to pre-extract; this helper
    just formalizes argument > ai > profile tail so every decision has a
    recorded WHY."""
    if argument is not None:
        return argument, "argument", f"{setting}: explicitly requested in the tool call"
    if ai_value is not None:
        return ai_value, "ai", f"{setting}: kept from this project's previous AI choice"
    return profile_value, "profile", f"{setting}: no user/system value found -> manufacturer profile default"


# ---------------------------------------------------------------------------
# Application discovery ("check the desktop application like the libraries")
# ---------------------------------------------------------------------------

_APP_CACHE = None


def discover_application(kicad_cli: str = "") -> dict:
    """LEARN the installed application itself: which app, where, which
    version, and which capabilities its OWN kicad-cli reports -- so the
    pipeline adapts to whatever is installed instead of assuming this
    machine's setup. Cached per process (probing spawns subprocesses)."""
    global _APP_CACHE
    if _APP_CACHE is not None:
        return _APP_CACHE

    info = {"kicad_cli": kicad_cli or "", "install_bin": None, "version": None,
            "export_formats": [], "drc_flags": [], "config_dir": find_app_config_dir()}

    if not kicad_cli:
        try:
            from skidl.anvil.open_anvilcad import _find_bin
            b = _find_bin()
            if b:
                info["install_bin"] = b
                p = os.path.join(b, "kicad-cli.exe")
                if os.path.isfile(p):
                    info["kicad_cli"] = p
        except Exception:
            pass
        if not info["kicad_cli"]:
            import shutil
            info["kicad_cli"] = shutil.which("kicad-cli") or ""

    cli = info["kicad_cli"]
    if cli:
        info["version"] = _probe(cli, ["version"]).strip().splitlines()[-1:] or None
        if info["version"]:
            info["version"] = info["version"][0]
        # Capabilities from the installed binary's own help output.
        export_help = _probe(cli, ["pcb", "export", "--help"])
        m = re.search(r"\{([a-z0-9,_]+)\}", export_help)
        if m:
            info["export_formats"] = m.group(1).split(",")
        drc_help = _probe(cli, ["pcb", "drc", "--help"])
        info["drc_flags"] = re.findall(r"(--[a-z-]+)", drc_help)

    caps = {
        "zone_refill": "--refill-zones" in info["drc_flags"],
        "schematic_parity": "--schematic-parity" in info["drc_flags"],
        "ipc2581": "ipc2581" in info["export_formats"],
        "odb": "odb" in info["export_formats"],
        "step": "step" in info["export_formats"],
        "ipcd356": "ipcd356" in info["export_formats"],
    }
    info["capabilities"] = caps
    _APP_CACHE = info
    return info


def _probe(cli, args, timeout=20) -> str:
    try:
        proc = subprocess.run([cli, *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
        return (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        return ""
