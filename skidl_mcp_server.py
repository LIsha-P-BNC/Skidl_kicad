"""skidl_mcp_server.py -- MCP server that turns THIS repo's SKiDL into circuit tools.

Point Claude Desktop (claude_desktop_config.json -> mcpServers) at this file:

    "anvilcad": {
      "command": "<python 3.11 with the `mcp` package>",
      "args": ["f:\\skidl\\skidl_mcp_server.py"]
    }

It uses ONLY this repo's SKiDL (f:\\skidl\\src\\skidl) + its bundled `skidl.anvil`
engine + plain KiCad symbol libraries.  No proprietary app, no 3D-CAD MCP, no GUI:
every build produces .net + .anvil_sch + .anvil_pro files and returns their paths.

Tools (call build(mode='rules') FIRST, then follow its workflow):
  build(mode='rules')               - the mandatory design workflow + circuit template
  read_pdf(path, pages, dpi)       - render a PDF's pages to PNG images (+ text) so the AI can SEE it
                                     (the built-in Read cannot open PDFs here)
  parts(action='search')(queries)            - batch part search: exact match + candidates per query
  parts(action='describe')(parts)             - pins/description/datasheet for a list of parts
  build(name, code, mode)          - generate .net/.anvil_sch/.anvil_pro (body or full script)
  build(mode='source')(name)        - existing project's source, to CONTINUE/extend it
                                     (add a sheet/block/section) without rebuilding from scratch
  build(mode='status')(name, include_log)  - poll the background build; lists generated files
  build(mode='open')(name, view)     - open the finished project in the Anvil CAD app
  build(name, mode='bom')               - Bill of Materials via the app's own BOM engine (project columns)
  parts(action='add')(name, pins)  - escape hatch: create a genuinely-missing part
                                     from DATASHEET pins (only with the user's OK)
  diagnostics()                    - env sanity: which skidl, symbol dir, out dir
"""
import contextlib
import functools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# --- make THIS repo's skidl win over any pip-installed copy --------------------
REPO = Path(__file__).resolve().parent
SRC = REPO / "src"
if not (SRC / "skidl").is_dir() and (REPO / "skidl").is_dir():
    # Installed layout tolerance: some payloads drop the src CONTENTS beside the
    # server (<here>\skidl\...) instead of under <here>\src\skidl. Accept both --
    # a missing skidl core here kills the whole AI tool server on a shared
    # machine ("circuit tools aren't exposed") with no dev tree to fall back to.
    SRC = REPO
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Root under which generated circuits land. Each project gets its OWN subfolder:
#   <OUT>/<project>/<project>.anvil_pro  (+ .py/.net/.anvil_sch/.anvil_pcb/logs).
# Override the ROOT with env SKIDL_MCP_OUT, or a single project with build(folder=...).
# Default is machine-relative, never a fixed drive letter: the dev box's F:\Anvil
# is used only when that drive actually exists; every other machine (a shared
# install) gets <home>\Anvil. A hardcoded F:\ default crashed the whole server at
# import on machines without an F: drive.
_out_env = os.environ.get("SKIDL_MCP_OUT", "")
if _out_env:
    OUT = Path(_out_env)
elif Path("F:/").exists():
    OUT = Path(r"F:/Anvil")
else:
    OUT = Path.home() / "Anvil"
OUT.mkdir(parents=True, exist_ok=True)

# base -> explicit project directory, set when build() is called with folder=.
# Lets a project live at a user-chosen path instead of the default <OUT>/<base>.
_PROJECT_DIRS: dict = {}


def pdir(base: str) -> Path:
    """Per-project output folder (created on demand). Everything for one
    project -- <base>.py/.net/.anvil_sch (+ sub-sheets)/.anvil_pro/.anvil_pcb
    and all logs -- lives together here. Default <OUT>/<base>; a custom path
    registered via build(folder=...) wins. Generic scratch (pre-check temp,
    diagnostics) stays in the OUT root, not in any project folder."""
    d = _PROJECT_DIRS.get(base) or (OUT / base)
    d.mkdir(parents=True, exist_ok=True)
    return d

server = FastMCP(
    "anvilcad",
    instructions=(
        "Anvil CAD electronic circuit design tools. Use these tools AUTOMATICALLY "
        "whenever the user asks to design, create, build, or generate ANY electronic "
        "circuit, schematic, netlist, PCB/Anvil CAD project, or BOM -- even if they never "
        "mention 'Anvil CAD', 'MCP', or any tool name. A plain request like 'design a "
        "555 LED blinker' or '5V power supply venum' means: call build(mode='rules') "
        "first and follow that workflow exactly (parts(action='search') -> parts(action='describe') -> "
        "build -> poll build(mode='status') until done -> build(mode='open') once -> ask "
        "about BOM).\n"
        "STEP 0 -- EXTEND, DON'T RESTART: BEFORE starting that workflow, check whether "
        "the design already exists. Call get_app_state() (and read_live() if the app is "
        "open); if the request names or continues a project that is already on disk or "
        "open in the window (its next sheet, another block, 'add/change ...', or any "
        "follow-up to earlier work in this conversation), do NOT re-run the full "
        "workflow from scratch: call build(name, mode='source') to get the existing "
        "script and EXTEND it (same name, full script back), or use "
        "edit_schematic_live for small in-place changes. Re-searching and rebuilding "
        "parts that are already placed is wrong even if this conversation has no "
        "memory of placing them -- the project on disk is the memory. Start the full "
        "workflow only for a genuinely NEW, unrelated design.\n"
        "HARD RULES -- these hold for EVERY circuit, no exceptions:\n"
        "0. BRANDING: the application is 'Anvil CAD' -- its schematic editor, "
        "PCB editor, libraries, files and API bridge are ALL Anvil CAD. NEVER "
        "say 'KiCad' (or 'KiCad PCB Editor', 'KiCad API', 'kicad project') to "
        "the user in ANY response, question, button label, or instruction -- "
        "even though internal tool docs, file formats, or error strings may "
        "mention KiCad, always rephrase them as 'Anvil CAD' / 'the PCB "
        "Editor' / 'the schematic editor' when talking to the user. Never "
        "tell the user to enable a 'KiCad API' or any preference -- the Anvil "
        "CAD bridge is built in and always on.\n"
        "1. NEVER design a circuit in prose. Do NOT write pin-by-pin connections, "
        "a wiring/hookup description, or a BOM table as TEXT instead of building. A "
        "circuit you already know by heart (Arduino/ATmega, 555, ESP32, LM317, any "
        "classic reference design) is NOT an exception -- it MUST still go through "
        "parts(action='search') -> parts(action='describe') -> build. If you catch yourself about to type "
        "a component/pin list, STOP and call the tools.\n"
        "2. A part is 'missing' ONLY when parts(action='search') returns total_matches:0 for "
        "it. 0 hits for a full ordering code (ATmega328P-PU, LM317T, 2N2222A) does "
        "NOT mean missing -- parts(action='search') AUTO-BROADENS to the base family and "
        "returns candidates plus a 'note'/'broadened_query'; read them and use the "
        "package-suffix variant (e.g. ATmega328P-P). Genuinely missing (empty) -> "
        "offer parts(action='add'). NEVER bail to a hand-written answer.\n"
        "3. 'The library doesn't have X' or an fp-lib-table warning is NEVER a "
        "reason to skip building. But every part MUST carry footprint=\"Lib:Name\" "
        "-- the precheck blocks bare parts (missing_footprints lists them); pick "
        "the package's real footprint or parts(action='add_footprint') it, never "
        "skip the build over it.\n"
        "4. A design is DONE only after build(mode='status') returns status:'done' with a "
        "generated .anvil_sch -- cite the produced file paths. NEVER claim a build "
        "succeeded (or describe a schematic as generated) without them.\n"
        "5. ALWAYS show the user the generated file NAME and its FULL PATH (from "
        "build(mode='status') 'generated' -> the .anvil_sch and .anvil_pro) so they can "
        "find/open it, THEN call build(name, mode='open') to open it. If "
        "build(mode='open') reports it did NOT open, STILL give the user the file "
        "path and tell them to open it manually -- NEVER say the schematic 'can't "
        "be rendered/exported here', 'the file-open step isn't available', or that "
        "a tool is unavailable when the .anvil_sch already exists on disk.\n"
        "6. If ANY tool errors, times out, or returns a failure, report the actual "
        "error (and the file path if one was already produced), then STOP -- never "
        "substitute a hand-written schematic, pinout, or BOM as a 'fallback'.\n"
        "7. PCB flow: create_pcb on a project the user never initialized returns "
        "status:'requirements_needed' with a questionnaire -- ASK THE USER those "
        "questions (NEVER answer them yourself or proceed with defaults on your "
        "own; show the limitations verbatim), submit the answers via "
        "initialize_pcb_project, show its resolved_configuration and ask "
        "'Proceed?', THEN call create_pcb again and poll it until done -> "
        "run_drc/verify_board -> review_design -> approve_design -> "
        "export_manufacturing. Only an explicit 'defaults are fine' from the "
        "user justifies initialize_pcb_project(name) bare or "
        "create_pcb(name, accept_defaults=True).\n"
        "8. READ -> ACT -> VERIFY: the SAVED Anvil CAD files are the source of "
        "truth. Before board work, READ the current state (get_board_setup); "
        "to change a board setting, WRITE through update_board_setup and "
        "report its per-field `verified` statuses (applied / "
        "pending_regeneration / failed) -- never just say 'done'. If the "
        "user edited and saved in Anvil CAD, their values are ADOPTED "
        "automatically (create_pcb reports `adopted`); accept them, never "
        "re-assert an older AI value. After a build, config_conformance "
        "proves the built board matches the configured setup; report any "
        "overridden_user_values (engine escalations) explicitly.\n"
        "9. READING A PDF: the built-in Read tool CANNOT open .pdf files in this "
        "environment (no PDF renderer installed). For ANY attached .pdf, call the "
        "read_pdf tool -- it renders each page to a PNG image (and pulls embedded "
        "text). Then Read each returned page image to SEE the drawing (image Read "
        "works fine). NEVER tell the user 'the PDF renderer isn't available' or "
        "ask them to resend/convert the PDF, and NEVER give up on a PDF -- use "
        "read_pdf. Trust the rendered image for wiring/connectivity; treat the "
        "extracted text only as a hint.\n"
        "Do not design circuits by hand when these tools are available."
    ),
)


def _quiet(fn):
    """CRITICAL for MCP stdio: skidl / anvil_libs / part_query print progress
    ("Adding <lib> ...", ERC INFO, etc.) to STDOUT -- but stdout is the JSON-RPC
    channel the client parses. Any stray byte there corrupts the protocol and the
    client hangs forever. So every tool runs with stdout redirected to stderr."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with contextlib.redirect_stdout(sys.stderr):
            return fn(*args, **kwargs)
    return wrapper


# Interpreter used for every build subprocess (same one running this server).
PYEXE = sys.executable

# Boilerplate wrapped around a user-supplied circuit BODY so imports / determinism /
# output-naming / build are always correct and no GUI ever pops up.
_HARNESS = '''\
import os, sys
os.environ["PYTHONHASHSEED"] = "0"
sys.path.insert(0, r"{src}")
from skidl.anvil import anvil_libs          # noqa: F401  (sets KICAD*_SYMBOL_DIR)
from skidl import *
from skidl.anvil import smart_schematic
set_default_tool(KICAD9)

# ===== circuit body (user) =====
{body}
# ===== end body =====

# FLOATING-PIN GATE: every pin must be either connected or EXPLICITLY marked
# no-connect by the body (part[pin] += NC). The old blanket auto-NC hid
# genuinely forgotten connections (floating MCU/STAT/collector pins shipped
# with zero warnings), so a floating pin is now a build-stopping design error.
# Library-declared no-connect pins (func == NOCONNECT) are exempt: the symbol
# itself says they connect to nothing.
_floating = []
for _part in default_circuit.parts:
    for _pin in _part.pins:
        try:
            if not _pin.nets and _pin.func != Pin.types.NOCONNECT:
                _floating.append("%s[%s] %s" % (_part.ref, _pin.num,
                                                str(_pin.name or "").strip()))
        except Exception:
            pass
if _floating:
    print("FLOATING PINS -- connect each one, or mark it no-connect in the "
          "body (e.g. part[pin_num] += NC):")
    for _f in _floating:
        print("  " + _f)
    sys.exit(2)

sch, pro = smart_schematic.build({build_opts})
print("::SCH::", sch)
print("::PRO::", pro)
'''


def _env_get_ci(env: dict, key: str):
    """Case-insensitive env lookup (Windows env blocks are case-insensitive,
    but a copied dict is not)."""
    ku = key.upper()
    for k, v in env.items():
        if k.upper() == ku:
            return v
    return None


def _subprocess_env() -> dict:
    """Build a COMPLETE env for skidl subprocesses.

    MCP clients (Claude Desktop included) spawn this server with a MINIMAL
    environment -- vars like ProgramFiles may be missing, so anvil_libs cannot
    find the Anvil CAD install and skidl cannot find any symbol library
    ("Can't open file: Device"). Rebuild the essentials DYNAMICALLY (per-user,
    nothing hardcoded to a username)."""
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONPATH"] = str(SRC) + os.pathsep + (_env_get_ci(env, "PYTHONPATH") or "")
    env["PYTHONIOENCODING"] = "utf-8"
    # MCP-built circuits are destined for real boards: every part must carry a
    # footprint (smart_schematic._design_gates enforces it under this flag;
    # direct library/test use without the flag stays permissive).
    env["ANVIL_REQUIRE_FOOTPRINTS"] = "1"

    home = _env_get_ci(env, "USERPROFILE") or str(Path.home())
    defaults = {
        "USERPROFILE": home,
        "HOME": home,
        "LOCALAPPDATA": os.path.join(home, "AppData", "Local"),
        "APPDATA": os.path.join(home, "AppData", "Roaming"),
        "ProgramFiles": os.path.join(_env_get_ci(env, "SystemDrive") or "C:", os.sep, "Program Files"),
        "ProgramFiles(x86)": os.path.join(_env_get_ci(env, "SystemDrive") or "C:", os.sep, "Program Files (x86)"),
        "SystemRoot": os.path.join(_env_get_ci(env, "SystemDrive") or "C:", os.sep, "Windows"),
    }
    for k, v in defaults.items():
        if _env_get_ci(env, k) is None:
            env[k] = v

    # Last-resort symbol dirs: point skidl straight at the per-user flattened
    # cache (anvil_libs will overwrite these when it can see the install).
    cache = os.path.join(home, "skidl_symbols")
    if os.path.isdir(cache):
        for v in ("KICAD6_SYMBOL_DIR", "KICAD7_SYMBOL_DIR",
                  "KICAD8_SYMBOL_DIR", "KICAD9_SYMBOL_DIR"):
            if _env_get_ci(env, v) is None:
                env[v] = cache

    # Last-resort FOOTPRINT dirs: the symbol fallback above has no footprint twin,
    # so a routing subprocess spawned from a stripped environment (where anvil_libs
    # can't find the install) had KICAD*_FOOTPRINT_DIR unset -> create_pcb failed with
    # UnresolvedFootprintsError. Detect the install's footprint folder and set every
    # KICAD*_FOOTPRINT_DIR when the current value is missing or not a real directory.
    fpd = _env_get_ci(env, "KICAD9_FOOTPRINT_DIR")
    if not fpd or not os.path.isdir(fpd):
        try:
            b = _open_anvilcad_mod()._find_bin()
            if b:
                fp = os.path.join(os.path.dirname(b), "share", "anvil", "footprints")
                if os.path.isdir(fp):
                    for v in ("KICAD6_FOOTPRINT_DIR", "KICAD7_FOOTPRINT_DIR",
                              "KICAD8_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR"):
                        env[v] = fp
        except Exception:
            pass

    return env


def _safe_name(name: str) -> str:
    """A filesystem-safe base name (no extension, no path)."""
    base = os.path.splitext(os.path.basename(name.strip()))[0]
    base = re.sub(r"[^A-Za-z0-9_\-]", "_", base) or "circuit"
    return base


# --- asynchronous builds ---------------------------------------------------
# Schematic routing for big circuits can take minutes, which is longer than
# most MCP clients' per-call timeout. So build() only STARTS the build
# (returns instantly) and build(mode='status')() is polled until it is done.
_BUILDS = {}          # base -> {"proc","logfile","fh","started"}
_MAX_BUILD_S = 900    # kill runaway builds after 15 min


def _gather_files(base: str) -> dict:
    files = {}
    for ext in (".net", ".anvil_sch", ".anvil_pro"):
        f = pdir(base) / (base + ext)
        if f.is_file():
            files[ext.lstrip(".")] = str(f)
    return files


def _prebuild_guard(base: str):
    """HARD overwrite gate -- the enforcement behind the LOOK-BEFORE-ACT rule.
    A build deletes and regenerates the schematic, which destroys everything
    a netlist cannot carry (positions, wiring, notes, DNP/BOM flags). So:
      1) ALWAYS back up the current outputs to *.prebuild.bak first;
      2) REFUSE to build over a USER-owned sheet (hand-made, or AI-made then
         saved/edited in the editor) unless adopt_project consented first
         (one-shot <base>.rebuild_ok marker).
    Returns an error dict to block the build, or None to proceed."""
    d = pdir(base)
    sch = d / (base + ".anvil_sch")
    if not sch.is_file():
        return None
    import shutil
    for ext in (".anvil_sch", ".anvil_pro", ".net"):
        f = d / (base + ext)
        if f.is_file():
            try:
                shutil.copy2(f, d / (base + ext + ".prebuild.bak"))
            except OSError:
                pass
    parts, _nets = _design_counts(base, d)
    if _owner_of(base, d, parts) != "user":
        return None
    consent = d / (base + ".rebuild_ok")
    if consent.is_file():
        try:
            consent.unlink()
        except OSError:
            pass
        return None
    return {
        "ok": False,
        "status": "build_blocked",
        "error": (f"'{base}.anvil_sch' is USER-owned (hand-made or manually "
                  "edited+saved in the editor). Building would DELETE the "
                  "user's layout, wiring, notes and DNP/BOM flags. Blocked."),
        "options": [
            "small change -> edit_schematic (surgical file edit, keeps layout) "
            "or edit_schematic_live (undoable, in the open editor)",
            "regenerate from the user's design -> adopt_project first (tell "
            "the user layout/wiring will be redrawn), then build",
        ],
        "backup": str(d / (base + ".anvil_sch.prebuild.bak")),
    }


def _start_build(base: str, script_text: str) -> dict:
    """Write OUT/<base>.py and launch it in the background."""
    old = _BUILDS.get(base)
    if old and old["proc"].poll() is None:
        return {"ok": False, "status": "building",
                "error": f"a build of '{base}' is already running -- "
                         f"call build(mode='status')('{base}')"}
    if old:  # finished but never collected -- release its log handle first
        try:
            old["fh"].close()
        except Exception:
            pass
    blocked = _prebuild_guard(base)
    if blocked:
        return blocked
    # stale outputs must not be mistaken for this build's result
    # (backed up above before deletion -- *.prebuild.bak)
    for ext in (".net", ".anvil_sch", ".anvil_pro"):
        try:
            (pdir(base) / (base + ext)).unlink(missing_ok=True)
        except OSError:
            pass
    py = pdir(base) / (base + ".py")
    py.write_text(script_text, encoding="utf-8")
    logfile = pdir(base) / (base + ".build.log")
    fh = open(logfile, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PYEXE, str(py)],
        cwd=str(pdir(base)),
        env=_subprocess_env(),
        stdin=subprocess.DEVNULL,   # never inherit the MCP protocol pipe
        stdout=fh,
        stderr=subprocess.STDOUT,
    )
    _BUILDS[base] = {"proc": proc, "logfile": logfile, "fh": fh,
                     "started": time.time()}
    return {
        "ok": True,
        "status": "building",
        "python_file": str(py),
        "note": f"Build started in the background. Call build(mode='status')('{base}') "
                "repeatedly until status is 'done' -- each call waits up to "
                "~20 s. Big circuits can take 2-4 minutes; 'building' is "
                "normal, not an error.",
    }


def _finish_build(base: str) -> dict:
    """Collect results after the build process has exited."""
    info = _BUILDS.pop(base, None)
    log = ""
    rc = None
    if info:
        rc = info["proc"].returncode
        try:
            info["fh"].close()
        except Exception:
            pass
        try:
            log = Path(info["logfile"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    files = _gather_files(base)
    ok = bool(files.get("net") and files.get("anvil_sch")) and rc == 0
    # Script-mode hierarchical builds may emit schematic+netlist WITHOUT a
    # .anvil_pro (plain generate_schematic path) -- the app then refuses
    # to open the project ("does not appear to be a KiCad project file",
    # measured on the 7-sheet tracker). Seed the standard skeleton.
    if ok and not (pdir(base) / (base + ".anvil_pro")).is_file():
        skeleton = {
            "board": {"design_settings": {"defaults": {}, "rules": {},
                                          "track_widths": [], "via_dimensions": []},
                      "layer_presets": [], "viewports": []},
            "boards": [], "cvpcb": {"equivalence_files": []},
            "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
            "meta": {"filename": base + ".anvil_pro", "version": 1},
            "net_settings": {"classes": [{"name": "Default"}],
                             "meta": {"version": 3}},
            "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
            "schematic": {"legacy_lib_dir": "", "legacy_lib_list": [],
                          "meta": {"version": 1}},
            "sheets": [], "text_variables": {},
        }
        (pdir(base) / (base + ".anvil_pro")).write_text(
            json.dumps(skeleton, indent=2) + "\n", encoding="utf-8")
        files = _gather_files(base)
    # How was the schematic actually drawn? (printed by smart_schematic)
    if "all-label mode ->" in log:
        mode = "labels (nets connect by label name -- normal for dense circuits)"
    elif "partial wire route (seed=" in log:
        mode = "partial (wires where routable, labels on dense nets)"
    elif "routed with wires (seed=" in log:
        mode = "wires"
    else:
        mode = "unknown"
    res = {
        "ok": ok,
        "status": "done" if ok else "failed",
        "returncode": rc,
        "schematic_mode": mode,
        "python_file": str(pdir(base) / (base + ".py")),
        "generated": files,
        "log": log[-6000:],  # tail is where errors/summary live
    }
    # NEVER report a schematic the verifier said is WRONG as a success. The
    # all-label fallback prints its verify result; MISMATCH there means the
    # drawn sheet has shorts / missing connections.
    mism = re.search(r"all-label mode -> [^\n]*MISMATCH[^\n]*", log)
    if ok and mism:
        res["ok"] = False
        res["status"] = "failed"
        res["error"] = (
            "schematic connectivity MISMATCH: " + mism.group(0).split("-> ", 1)[-1]
            + " -- the .anvil_sch is WRONG (do not use or show it); the .net "
              "netlist is still valid for PCB layout. Tell the user exactly this."
        )
    # Non-blocking design review of the finished output: netlist-level checks
    # (LED series resistor, decoupling caps, floating nets) + the net->block
    # connectivity matrix from the .py, so the model can confirm no net was
    # missed. Advisory only -- never flips a good build to failed, never raises.
    try:
        mod = _validate_mod()
        if mod is not None:
            if files.get("net"):
                nf = mod.validate_netlist(files["net"])
                notes = [{"rule": f.rule, "severity": f.severity, "message": f.message}
                         for f in nf]
                if notes:
                    res["design_review"] = notes
            pyf = pdir(base) / (base + ".py")
            expected_sheets = None
            if pyf.is_file():
                v = mod.analyze_source(str(pyf))
                if v.net_to_blocks:
                    res["connectivity_matrix"] = {
                        nm: sorted(bs) for nm, bs in v.net_to_blocks.items()}
                expected_sheets = 1 + sum(len(c) for c in v.subcircuit_call_lines.values())
            # Phase 10 GOLDEN VERIFICATION: cross-check produced .net <-> .anvil_sch
            # (dropped parts, missing symbols, duplicate UUIDs). Sets res['verified'];
            # a failure must not report as success even though the files exist.
            if files.get("net") and files.get("anvil_sch"):
                sch_paths = [str(p) for p in sorted(pdir(base).glob(base + "*.anvil_sch"))]
                gv = mod.golden_verify(files["net"], sch_paths,
                                       pro_path=str(pdir(base) / (base + ".anvil_pro")),
                                       expected_sheets=expected_sheets)
                gv_err = [f for f in gv if f.severity == mod.ERROR]
                res["verified"] = not gv_err
                if gv:
                    res["golden_verification"] = [
                        {"rule": f.rule, "severity": f.severity, "message": f.message}
                        for f in gv]
                if gv_err:
                    res["verification_note"] = (
                        "GOLDEN VERIFICATION FAILED -- the .anvil_sch does not match the "
                        "netlist (dropped part / missing symbol / duplicate UUID). Do NOT "
                        "report this build as successful; see golden_verification[].")
    except Exception:
        pass
    # AUTO-OPEN: a finished good build opens in Anvil CAD immediately -- the
    # user should never have to ask (fires exactly once per build, here).
    if res["ok"]:
        res["opened_in_anvilcad"] = _try_open(base)
    return res


def _await_short(base: str, res: dict, wait_s: int = 18) -> dict:
    """Let a FAST build finish inside the same build() call so the client gets
    status:'done' + auto-open in ONE shot -- it never has to poll (and so cannot
    skip polling and leave the schematic unopened). A long build still times out
    here, stays 'building', and is polled via build(mode='status') as before."""
    info = _BUILDS.get(base)
    if not info or res.get("status") != "building":
        return res
    try:
        info["proc"].wait(timeout=wait_s)
    except Exception:
        return res                      # still routing -> keep 'building', poll
    try:
        done = _finish_build(base)      # this also fires the auto-open
    except Exception:
        return res
    for k in ("precheck", "parts", "nets", "verify", "validation_warnings",
              "connectivity_matrix", "semantic_warnings", "semantic_rule"):
        if k in res and k not in done:
            done[k] = res[k]
    return done


# Failure signatures skidl logs on bad lookups (mixins.py / schlib.py). A
# "No pins found" line can appear even with exit code 0 (the connection is
# silently dropped), so the pre-check must treat it as a failure by itself.
_PIN_FAIL_RE = re.compile(r"No pins found using (.+?):(\S+?)\[(.+?)\]")
_PART_FAIL_RE = re.compile(r"Unable to find part (\S+) in library")


def _suggest_fixes(out_text: str):
    """Turn skidl's lookup failures into EXACT corrections (pin-level error
    localization): for a bad pin name -> that part's real pin list + closest
    matches; for a bad part name -> library candidates. This is what makes a
    failed pre-check one-shot fixable instead of a guess-and-retry loop."""
    pin_fails, seen = [], set()
    for m in _PIN_FAIL_RE.finditer(out_text):
        name, ref, ids = m.group(1), m.group(2), m.group(3)
        req = re.findall(r"'([^']+)'", ids) or re.findall(r"[\w+./#-]+", ids)
        key = (name, tuple(req))
        if key in seen:
            continue
        seen.add(key)
        pin_fails.append({"part": name, "ref": ref, "requested": req})
    part_fails = sorted(set(_PART_FAIL_RE.findall(out_text)))
    if not pin_fails and not part_fails:
        return None
    code = f'''
import sys, json, difflib
sys.path.insert(0, {json.dumps(str(SRC))})
from skidl.anvil import anvil_libs
from skidl import KICAD9, set_default_tool
from skidl.part_query import PartSearchDB, show_part as _show
set_default_tool(KICAD9)
pin_fails = json.loads({json.dumps(json.dumps(pin_fails))})
part_fails = json.loads({json.dumps(json.dumps(part_fails))})
db = PartSearchDB(tool=KICAD9)
db.load_from_lib_search_paths()
res = {{"bad_pins": [], "bad_parts": []}}
for f in pin_fails:
    entry = {{"part": f["part"], "ref": f["ref"], "requested_pins": f["requested"],
              "real_pins": [], "closest": {{}}}}
    hit = None
    for p in db.search(f["part"]):
        if p.part_name.lower() == f["part"].lower():
            hit = p
            break
    if hit is not None:
        try:
            part = _show(hit.lib_name, hit.part_name)
        except Exception:
            part = None
        if part is not None:
            names = sorted({{(pin.name or "").strip() for pin in part.pins}} - {{""}})
            entry["real_pins"] = ["%s:%s" % (str(pin.num), (pin.name or "").strip())
                                  for pin in part.pins]
            for want in f["requested"]:
                entry["closest"][want] = difflib.get_close_matches(
                    want, names, n=3, cutoff=0.4)
    res["bad_pins"].append(entry)
for nm in part_fails:
    cands = ["%s:%s" % (p.lib_name, p.part_name) for p in db.search(nm)[:8]]
    res["bad_parts"].append({{"requested": nm, "candidates": cands}})
print("\\n::RESULT::" + json.dumps({{"ok": True, "fixes": res}}))
'''
    out = _py_json(code, timeout=240)
    return out.get("fixes") if out.get("ok") else None


# Fast electrical dry-run: body + ERC + netlist ONLY (no schematic routing).
# Catches bad lib/part names, bad pin numbers, ERC errors in seconds, so the
# slow schematic build never starts from a broken .py.
_DRY_HARNESS = '''\
import os, sys
os.environ["PYTHONHASHSEED"] = "0"
sys.path.insert(0, r"{src}")
from skidl.anvil import anvil_libs          # noqa: F401
from skidl import *
from skidl.anvil import smart_schematic     # the canonical body template uses
                                            # smart_schematic.block(...) -- without
                                            # this import every rules-conformant
                                            # body dies in precheck with NameError
set_default_tool(KICAD9)

# ===== circuit body (user) =====
{body}
# ===== end body =====

# FLOATING-PIN + BARE-FOOTPRINT GATES (same rules as the build chokepoint in
# smart_schematic._design_gates): report offenders, never hide them behind an
# auto-NC. The dry-run turns a non-empty list into a blocking precheck failure
# with a paste-ready fix per item.
import json as _json
_floating, _nofp = [], []
for _part in default_circuit.parts:
    _ref = str(_part.ref or "")
    if _ref.startswith("#") or not list(_part.pins):
        continue
    for _pin in _part.pins:
        try:
            if not _pin.nets and _pin.func != Pin.types.NOCONNECT:
                _floating.append({{"ref": _ref, "pin": str(_pin.num),
                                   "name": str(_pin.name or "").strip()}})
        except Exception:
            pass
    if not str(getattr(_part, "footprint", "") or "").strip():
        _nofp.append({{"ref": _ref, "part": str(getattr(_part, "name", "") or "")}})
print("::FLOATING::" + _json.dumps(_floating))
print("::NOFOOTPRINT::" + _json.dumps(_nofp))

# NET/PART MAPS for the server-side SEMANTIC lint (pin-function-name vs
# net-name): every net with its [ref, pin_num, pin_name] list + ref->part-name.
_netmap = dict()
_partmap = dict()
for _part in default_circuit.parts:
    _partmap[str(_part.ref or "")] = str(getattr(_part, "name", "") or "")
for _net in default_circuit.nets:
    if _net.__class__.__name__ == "NCNet":
        continue
    try:
        _pl = [[str(_p.part.ref), str(_p.num), str(_p.name or "").strip()]
               for _p in _net.pins]
        if _pl:
            _netmap[str(_net.name)] = _pl
    except Exception:
        pass
print("::NETMAP::" + _json.dumps(_netmap))
print("::PARTMAP::" + _json.dumps(_partmap))

ERC()
generate_netlist(file_="_precheck_tmp.net")
print("::PRECHECK_OK::")
'''


# --- SEMANTIC LINT: pin-function-name vs net-name -------------------------
# A netlist-level swap (RS485 A<->B, USB D+<->D-, CANH<->CANL, SCL<->SDA, a
# feedback pin on a switch node) is electrically legal, so no ERC or
# connectivity verify can see it -- only the NAMES betray it. These checks are
# deliberately tight (exact tokens, RS485-gated A/B) to stay near-zero-noise;
# they surface as non-blocking warnings the model must reconcile per datasheet.
_DIFF_PAIRS = (("DP", "DM"), ("CANH", "CANL"), ("SCL", "SDA"))
_RS485_PART_RE = re.compile(r"485|SN65|SN75|MAX3[01]|ADM3?4", re.I)
_FB_TOKENS = frozenset({"FB", "VFB", "FDBK"})
_SW_TOKENS = frozenset({"SW", "LX"})


def _name_tokens(s: str):
    s = (s or "").upper().replace("D+", "DP").replace("D-", "DM")
    return frozenset(t for t in re.split(r"[^A-Z0-9]+", s) if t)


def _semantic_lint(netmap: dict, partmap: dict) -> list:
    """Return [{net, ref, pin, pin_name, issue}] for name-level miswires."""
    warns = []
    for net_name, pins in (netmap or {}).items():
        nt = _name_tokens(net_name)
        for ref, num, pname in pins:
            pt = _name_tokens(pname)
            if not pt:
                continue
            pairs = list(_DIFF_PAIRS)
            if _RS485_PART_RE.search(partmap.get(ref, "")):
                pairs.append(("A", "B"))
            for x, y in pairs:
                for a, b in ((x, y), (y, x)):
                    if a in pt and b in nt and a not in nt:
                        warns.append({
                            "net": net_name, "ref": ref, "pin": num,
                            "pin_name": pname,
                            "issue": f"pin '{pname}' looks like the {a} side but "
                                     f"sits on a net named for {b} -- swapped?"})
            if (pt & _FB_TOKENS and nt & _SW_TOKENS) or \
               (pt & _SW_TOKENS and nt & _FB_TOKENS):
                warns.append({
                    "net": net_name, "ref": ref, "pin": num, "pin_name": pname,
                    "issue": f"feedback/switch-node mixup: pin '{pname}' on net "
                             f"'{net_name}' (FB must see the divider midpoint, "
                             "never the SW/LX node)"})
    return warns


def _dry_run(base: str, body: str) -> dict:
    """Phase-1 check: run the body electrically (no routing). Returns ok/errors."""
    script = _DRY_HARNESS.format(src=str(SRC), body=body)
    env = _subprocess_env()
    try:
        proc = subprocess.run(
            [PYEXE, "-X", "utf8", "-c", script],
            cwd=str(OUT), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=180, check=False,
        )
    except subprocess.TimeoutExpired:
        try:
            (OUT / "_precheck_tmp.net").unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "status": "precheck_failed",
                "error": "electrical pre-check timed out (180s)"}
    out = proc.stdout or ""
    erc_errors = re.findall(r"^ERC ERROR:.*$", out, flags=re.MULTILINE)
    pin_fail = bool(_PIN_FAIL_RE.search(out))
    m_float = re.search(r"^::FLOATING::(\[.*\])\s*$", out, flags=re.MULTILINE)
    try:
        floating = json.loads(m_float.group(1)) if m_float else []
    except (ValueError, AttributeError):
        floating = []
    m_nofp = re.search(r"^::NOFOOTPRINT::(\[.*\])\s*$", out, flags=re.MULTILINE)
    try:
        nofp = json.loads(m_nofp.group(1)) if m_nofp else []
    except (ValueError, AttributeError):
        nofp = []
    crashed = proc.returncode != 0 or "::PRECHECK_OK::" not in out
    if crashed or erc_errors or pin_fail or floating or nofp:
        try:
            (OUT / "_precheck_tmp.net").unlink(missing_ok=True)
        except OSError:
            pass
        tail = out[-3000:]
        if crashed:
            err = "body crashed before ERC (bad part/pin/name? see log)"
        elif pin_fail:
            err = ("a pin lookup FAILED -- that connection was silently dropped, "
                   "the circuit is wrong even though the body ran")
        elif floating:
            err = (f"{len(floating)} FLOATING pin(s) -- every pin must be either "
                   "connected or explicitly marked no-connect. For each pin below, "
                   "either wire it per the design intent or add "
                   "`<part_var>[<pin>] += NC` to the body. Do NOT blanket-NC pins "
                   "that the datasheet says must be connected.")
        elif nofp:
            err = (f"{len(nofp)} part(s) with NO footprint -- every part must "
                   "carry footprint=\"Lib:Name\". Pick a real footprint matching "
                   "the package in the part's datasheet (or add one via "
                   "parts(action='add_footprint')), then build again.")
        else:
            err = (f"{len(erc_errors)} ERC ERROR(s) -- fix the .py, then call "
                   "build again (pre-check is cheap, seconds)")
        resp = {
            "ok": False,
            "status": "precheck_failed",
            "erc_errors": erc_errors,
            "error": err,
            "log": tail,
        }
        if floating:
            resp["floating_pins"] = floating
        if nofp:
            resp["missing_footprints"] = nofp
        fixes = _suggest_fixes(out)
        if fixes:
            resp["fix_suggestions"] = fixes
            resp["fix_rule"] = ("bad_pins lists each part's REAL pins (num:name) and "
                                "the closest matches to what you wrote; bad_parts "
                                "lists library candidates. Apply these EXACT strings "
                                "to the body and call build again -- one cycle.")
        return resp
    # DEEP-VERIFY payload: echo back what the server actually understood --
    # every part (ref=value) and every net with the exact pins on it. The
    # model MUST compare this against its intended design before trusting
    # the build (wrong connection here = wrong circuit, however clean ERC is).
    parts, nets = [], {}
    try:
        ntxt = (OUT / "_precheck_tmp.net").read_text(encoding="utf-8", errors="replace")
        parts = [{"ref": c["ref"], "value": c["value"], "footprint": c["footprint"]}
                 for c in _parse_comps(ntxt)]
        netsec = ntxt[ntxt.find("(nets"):]
        for chunk in re.split(r"\(net\s*\(code", netsec)[1:]:
            nm = re.search(r'\(name\s*"/?([^"]+)"\)', chunk)
            if not nm:
                continue
            pins = re.findall(r'\(ref\s*"(\w+)"\)[\s\S]*?\(pin\s*"([^"]+)"\)', chunk)
            nets[nm.group(1)] = [f"{r}.{p}" for r, p in pins]
    except OSError:
        pass
    finally:
        try:
            (OUT / "_precheck_tmp.net").unlink(missing_ok=True)
        except OSError:
            pass
    n_warn = re.search(r"(\d+) warnings found while running ERC", out)
    resp = {"ok": True,
            "summary": "pre-check passed: body runs, skidl pre-ERC 0 errors"
                       + (f", {n_warn.group(1)} warnings (harmless)" if n_warn else "")
                       + " -- NOT the app's ERC verdict; for that use "
                         "check_live('erc')",
            "parts": parts,
            "nets": nets,
            "verify": "DEEP-CHECK now: compare every net's pin list and every "
                      "part's value against your intended design. Wrong net -> "
                      "fix the body and build again BEFORE waiting for "
                      "this build."}
    try:
        m_nm = re.search(r"^::NETMAP::(\{.*\})\s*$", out, flags=re.MULTILINE)
        m_pm = re.search(r"^::PARTMAP::(\{.*\})\s*$", out, flags=re.MULTILINE)
        sem = _semantic_lint(json.loads(m_nm.group(1)) if m_nm else {},
                             json.loads(m_pm.group(1)) if m_pm else {})
    except Exception:
        sem = []
    if sem:
        resp["semantic_warnings"] = sem
        resp["semantic_rule"] = (
            "These pin-name-vs-net-name mismatches look like classic swaps "
            "(RS485 A/B, USB D+/D-, CANH/CANL, SCL/SDA, FB-on-SW-node). They "
            "do NOT block the build -- but boards HAVE shipped with exactly "
            "these swaps, so verify each against the DATASHEET now and fix "
            "the body BEFORE the build finishes if any is real.")
    return resp


_PCB_JOBS = {}   # base -> {proc, started, setup_changed} (async PCB builds)

_MARK = "::RESULT::"


def _py_json(code: str, timeout: int = 90) -> dict:
    """Run a python snippet in a SUBPROCESS and return its ::RESULT:: JSON.

    WHY: importing skidl inside this server process hangs/breaks the MCP stdio
    session (first-import side effects in a worker thread). build() never
    had that problem because it already runs skidl in a subprocess -- so every
    skidl-touching tool goes through here. The subprocess may print noise before
    the marker; we parse only what follows the LAST marker."""
    env = _subprocess_env()
    try:
        proc = subprocess.run(
            [PYEXE, "-X", "utf8", "-c", code],
            cwd=str(OUT),               # writable: skidl's .erc/.log land here
            env=env,
            stdin=subprocess.DEVNULL,   # never inherit the MCP protocol pipe
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # include partial output so the hang point is diagnosable
        so = exc.stdout or ""
        se = exc.stderr or ""
        if isinstance(so, bytes):
            so = so.decode("utf-8", "replace")
        if isinstance(se, bytes):
            se = se.decode("utf-8", "replace")
        return {"ok": False, "error": f"subprocess timed out after {timeout}s",
                "stdout_tail": so[-1000:], "stderr_tail": se[-1000:]}
    out = proc.stdout or ""
    idx = out.rfind(_MARK)
    if idx < 0:
        return {"ok": False,
                "error": "no result marker from subprocess",
                "stdout_tail": out[-1500:], "stderr_tail": (proc.stderr or "")[-1500:]}
    payload = out[idx + len(_MARK):].strip()
    try:
        return json.loads(payload.splitlines()[0] if payload else "{}")
    except Exception as exc:
        return {"ok": False, "error": f"bad JSON from subprocess: {exc!r}",
                "payload_head": payload[:500]}


_VALIDATE_MOD = None


def _validate_mod():
    """Load src/skidl/scripts/validate_design.py BY FILE PATH as a standalone
    module. It imports ONLY stdlib (ast/re/json) -- never the skidl package --
    so this is safe in-process and cannot trigger the skidl-import stdio hang.
    Returns the module, or None if it can't be loaded (validation is optional)."""
    global _VALIDATE_MOD
    if _VALIDATE_MOD is None:
        try:
            import importlib.util
            p = SRC / "skidl" / "scripts" / "validate_design.py"
            spec = importlib.util.spec_from_file_location("_skidl_validate_design", str(p))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _VALIDATE_MOD = mod
        except Exception:
            _VALIDATE_MOD = False   # remember the failure; don't retry every call
    return _VALIDATE_MOD or None


# Rules the HARNESS owns (imports / set_default_tool / smart_schematic.build) --
# suppressed when validating a mode='body' snippet, which has none of them.
_BODY_SUPPRESS = {"GEN-0", "CONN-1", "GEN-1", "META-3"}


def _static_review(src: str, body_mode: bool):
    """Run the pure-AST spec validator on design source. Returns
    {'errors','warnings','matrix'} or None if the validator is unavailable.
    Never raises -- a validator problem must not break a build."""
    mod = _validate_mod()
    if mod is None:
        return None
    try:
        v = mod.analyze_text(src)
    except Exception:
        return None
    suppress = _BODY_SUPPRESS if body_mode else set()
    errors, warnings = [], []
    for f in v.findings:
        if f.rule in suppress:
            continue
        item = {"rule": f.rule, "line": f.line, "message": f.message}
        if f.severity == mod.ERROR:
            errors.append(item)
        elif f.severity == mod.WARN:
            warnings.append(item)
    matrix = {nm: sorted(bs) for nm, bs in v.net_to_blocks.items()}
    return {"errors": errors, "warnings": warnings, "matrix": matrix}


def _validation_failed(review: dict) -> dict:
    """precheck_failed response for MUST-level spec violations."""
    return {
        "ok": False,
        "status": "precheck_failed",
        "error": (f"{len(review['errors'])} schematic-spec rule violation(s) (MUST) -- "
                  "fix the code and call build again (this check is instant)"),
        "validation_errors": review["errors"],
        "validation_warnings": review["warnings"],
        "fix_rule": ("each item has a rule ID (e.g. PWR-2, NET-5, HIER-5 -- see "
                     "docs/schematic_generation_spec.md / build(mode='rules')) and the exact "
                     "fix. Apply ALL of them, then build again. Common ones: never "
                     'Part("power",...) (name the net instead), every Part() needs tag=, '
                     "never recreate a shared Net by name (pass the same object)."),
    }


_OPEN_MOD = None


def _open_anvilcad_mod():
    """Load src/skidl/anvil/open_anvilcad.py by FILE PATH (not via the skidl
    package, which would import skidl core in-process and hang the session).
    It is pure os/glob/subprocess and AUTO-DETECTS the Anvil CAD install under
    %LOCALAPPDATA%\\Programs / Program Files -- so it works for ANY user and
    ANY install path, nothing hardcoded."""
    global _OPEN_MOD
    if _OPEN_MOD is None:
        import importlib.util
        p = SRC / "skidl" / "anvil" / "open_anvilcad.py"
        spec = importlib.util.spec_from_file_location("_open_anvilcad", str(p))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _OPEN_MOD = mod
    return _OPEN_MOD


def _project_window_open(base: str) -> bool:
    """True if some app window ALREADY shows THIS project (title contains its
    name). Other projects being open must NOT block opening this one."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object {$_.MainWindowTitle} | "
             "ForEach-Object {$_.MainWindowTitle}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        ).stdout.lower()
        return base.lower() in out
    except Exception:
        return False


def _list_window_titles() -> list:
    """All visible top-level window titles (lowercased). Used to tell WHICH
    project is open in Anvil CAD right now -- the app puts the project name in
    its title bar. Empty list if the probe fails (never raises)."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object {$_.MainWindowTitle} | "
             "ForEach-Object {$_.MainWindowTitle}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
        ).stdout
        return [ln.strip().lower() for ln in out.splitlines() if ln.strip()]
    except Exception:
        return []


def _known_projects() -> list:
    """Every project folder we can see: each <OUT>/<name> (or a custom dir
    registered via build(folder=)) that holds a <name>.anvil_pro OR
    <name>.anvil_sch. Returns [(base, dir_path)], newest first by mtime."""
    seen, found = set(), []
    dirs = list(_PROJECT_DIRS.values())
    if OUT.is_dir():
        dirs += [d for d in OUT.iterdir() if d.is_dir()]
    for d in dirs:
        try:
            base = d.name
            if base in seen:
                continue
            if (d / (base + ".anvil_pro")).is_file() or \
               (d / (base + ".anvil_sch")).is_file():
                seen.add(base)
                mt = 0.0
                for ext in (".anvil_sch", ".anvil_pcb", ".anvil_pro"):
                    f = d / (base + ext)
                    if f.is_file():
                        mt = max(mt, f.stat().st_mtime)
                found.append((base, d, mt))
        except OSError:
            continue
    found.sort(key=lambda t: t[2], reverse=True)
    return [(b, d) for b, d, _ in found]


def _design_counts(base: str, pdir_path: Path) -> tuple:
    """(parts, nets) for a project, robust to hierarchy. Prefers the netlist;
    otherwise counts instance symbols across the ROOT schematic AND every
    sub-sheet file (a hierarchical root holds sheets, not symbols, so counting
    only the root would wrongly read as 'empty'). nets is None when no netlist
    exists (needs a kicad-cli export -- run adopt_project)."""
    net = pdir_path / (base + ".net")
    try:
        if net.is_file():
            t = net.read_text(encoding="utf-8", errors="replace")
            return (len(re.findall(r"\(comp\s+\(ref", t)),
                    len(re.findall(r"\(net\s+\(code", t)))
    except OSError:
        pass
    parts = 0
    try:
        for sch in pdir_path.glob(base + "*.anvil_sch"):
            parts += len(re.findall(r"\(lib_id ", sch.read_text(
                encoding="utf-8", errors="replace")))
    except OSError:
        pass
    return (parts, None)


def _live_design_counts() -> dict:
    """(parts, nets, base) from the OPEN schematic editor's in-memory model, or
    None when the app isn't reachable / no schematic is open. This is the TRUTH
    for a project that is open right now: it sees unsaved edits and every
    sub-sheet, so a populated sheet can never read as 0/'empty' just because the
    .net wasn't exported or the .anvil_sch is hierarchical. Never raises."""
    try:
        r = _live_call("get_schematic", {}, timeout=8.0)
    except Exception:                              # noqa: BLE001 -- best effort
        return None
    if not isinstance(r, dict) or not r.get("ok"):
        return None                                # not_reachable / no sheet open
    sch_file = r.get("sch_file") or ""
    nets = r.get("nets")
    return {"parts": int(r.get("count", 0)),
            "nets": (len(nets) if isinstance(nets, dict) else None),
            "base": (Path(sch_file).stem if sch_file else "")}


def _owner_of(base: str, pdir_path: Path, parts: int) -> str:
    """WHO owns this project's design.
      'ai'      -> we generated the schematic ((generator "skidl")) and it has
                   not been re-saved by the user's editor
      'user'    -> a .anvil_sch made or SAVED by the KiCad/Anvil editor
                   ((generator "eeschema") -- hand-made, or ours then hand-edited)
      'empty'   -> .anvil_sch exists but holds no real part yet
      'unknown' -> nothing readable
    We key off the schematic's own (generator ...) tag, NOT mtime: our build
    writes .py first and the .anvil_sch later, so mtime always looks 'edited'.
    When the user opens our sheet and SAVES, the editor rewrites the tag to
    'eeschema' -- that flip, not the clock, is the ownership signal."""
    sch = pdir_path / (base + ".anvil_sch")
    py = pdir_path / (base + ".py")
    if sch.is_file():
        if parts == 0:
            return "empty"
        gen = ""
        try:
            head = sch.read_text(encoding="utf-8", errors="replace")[:4000]
            mo = re.search(r'\(generator\s+"?([\w-]+)"?', head)
            gen = (mo.group(1).lower() if mo else "")
        except OSError:
            pass
        if gen == "skidl":
            return "ai"                 # our generated sheet, not re-saved
        return "user"                   # eeschema-saved or foreign generator
    if py.is_file():
        return "ai"
    return "unknown"


def _summarize_project(base: str, pdir_path: Path) -> dict:
    """WHAT is inside: cheap counts from the netlist (preferred) or schematic."""
    sch = pdir_path / (base + ".anvil_sch")
    pcb = pdir_path / (base + ".anvil_pcb")
    parts, nets = _design_counts(base, pdir_path)
    return {"has_schematic": sch.is_file(), "has_pcb": pcb.is_file(),
            "has_python": (pdir_path / (base + ".py")).is_file(),
            "parts": parts, "nets": nets}


def _try_open(base: str, view: str = "project") -> str:
    """Open OUT/<base> in Anvil CAD. Prefers loading into the ALREADY-OPEN window via
    the app's tool server (no second window pops up); only falls back to launching a
    fresh window when nothing is listening (e.g. the app isn't running)."""
    pro = pdir(base) / (base + ".anvil_pro")

    # 1) Load into the running window (in-app chat, or any client while the app is open).
    if pro.is_file():
        try:
            r = _live_call("open_project", {"path": str(pro)}, timeout=8.0)
            if r.get("ok"):
                return "opened in the current Anvil CAD window"
        except Exception:
            pass   # nothing listening -> fall through to launching

    # 2) Fallback: launch a window (app not running / external with no open app).
    try:
        if _project_window_open(base):
            return (f"'{base}' is already open in Anvil CAD -- do File > Revert "
                    "there to load the freshly built version (never save the "
                    "old buffer over it).")
        mod = _open_anvilcad_mod()
        opener = {"project": mod.open_project, "sch": mod.open_schematic,
                  "pcb": mod.open_pcb}.get(view, mod.open_project)
        ok = opener(str(pdir(base) / base))
        return "opened in Anvil CAD" if ok else \
               "NOT opened (Anvil CAD install not found, or file missing)"
    except Exception as exc:
        return f"open failed: {exc!r}"


_CLI_CACHE: dict = {}


def _cli_works(path: str) -> bool:
    """PROBE the exe actually runs -- a stale/broken install can exist on disk
    yet fail to load its DLLs, which would silently kill every ERC/DRC."""
    try:
        proc = subprocess.run([path, "version"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30)
        return proc.returncode == 0
    except Exception:
        return False


def _find_kicad_cli_path() -> str:
    """The CAD app's CLI (anvil-cli.exe, with kicad-cli.exe as the legacy
    name). Candidates in order: the installed app's bin, a dev-tree install
    next to this repo, PATH. Every candidate is PROBED (must actually run --
    exist-on-disk is not enough; a broken install dies on missing DLLs) and
    the first working one wins; result cached for the process lifetime."""
    if "cli" in _CLI_CACHE:
        return _CLI_CACHE["cli"]
    cands: list = []
    try:
        b = _open_anvilcad_mod()._find_bin()
        if b:
            for name in ("anvil-cli.exe", "kicad-cli.exe"):
                p = os.path.join(b, name)
                if os.path.isfile(p):
                    cands.append(p)
    except Exception:
        pass
    try:
        root = Path(__file__).resolve().parent.parent
        for bindir in sorted(root.glob("kicad-source-mirror/build/install/*/bin")):
            for name in ("anvil-cli.exe", "kicad-cli.exe"):
                q = bindir / name
                if q.is_file():
                    cands.append(str(q))
    except Exception:
        pass
    import shutil
    for name in ("anvil-cli", "kicad-cli"):
        w = shutil.which(name)
        if w:
            cands.append(w)
    for c in cands:
        if _cli_works(c):
            _CLI_CACHE["cli"] = c
            return c
    _CLI_CACHE["cli"] = cands[0] if cands else ""
    return _CLI_CACHE["cli"]


def _board_drc_gate(pcb_path: Path, refill: bool = False) -> dict:
    """The board gate: prove the .anvil_pcb PARSES in the installed KiCad
    and DRC it. refill=True additionally fills copper zones and saves the
    board (kicad-cli --refill-zones --save-board) in the same call; if the
    installed CLI doesn't know those flags it gracefully retries without
    them (capability adaptation -- every system differs)."""
    cli = _find_kicad_cli_path()
    if not cli:
        return {"drc_parsed": None,
                "drc_note": "kicad-cli not found -- board written but not validated"}
    report = pcb_path.with_suffix(".drc.json")
    extra = ["--refill-zones", "--save-board"] if refill else []
    try:
        proc = subprocess.run(
            [cli, "pcb", "drc", "--format", "json", "--severity-all",
             *extra, "--output", str(report), str(pcb_path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
        )
        if extra and proc.returncode != 0 and "nrecogni" in (proc.stderr or ""):
            # Older kicad-cli without zone-refill support: degrade gracefully.
            proc = subprocess.run(
                [cli, "pcb", "drc", "--format", "json", "--severity-all",
                 "--output", str(report), str(pcb_path)],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
            )
    except Exception as exc:
        return {"drc_parsed": False, "drc_error": f"kicad-cli drc failed to run: {exc!r}"}
    if proc.returncode != 0 or not report.is_file():
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-800:]
        return {"drc_parsed": False,
                "drc_error": f"kicad-cli pcb drc exit {proc.returncode}: {tail}"}
    try:
        rep = json.loads(report.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {"drc_parsed": False, "drc_error": f"unreadable DRC report: {exc!r}"}
    # Electrical error count EXCLUDES library-art defects (unclosed
    # courtyard polygons shipped in the footprint libs -- cosmetic). They
    # still gate nothing, but the app's manual DRC LISTS them, so they are
    # reported (drc_cosmetic_*) instead of silently dropped -- the answer
    # shown to the user must contain everything the app would show.
    _art = {"malformed_courtyard", "lib_footprint_issues"}
    all_errors = [v for v in rep.get("violations", [])
                  if v.get("severity") == "error"]
    errors = [v for v in all_errors if v.get("type") not in _art]
    cosmetic = [v for v in all_errors if v.get("type") in _art]
    return {
        "drc_parsed": True,
        "drc_report": str(report),
        "drc_violations": len(rep.get("violations", [])),
        "drc_errors": len(errors),
        "drc_error_descriptions": [v.get("description") for v in errors][:10],
        "drc_cosmetic_errors": len(cosmetic),
        "drc_cosmetic_descriptions": [v.get("description") for v in cosmetic][:10],
        "drc_cosmetic_note": ("library-art defects (courtyard/footprint-lib) -- "
                              "not electrical, but the app's manual DRC lists "
                              "them; mention them to the user") if cosmetic else None,
        "drc_unconnected": len(rep.get("unconnected_items", [])),
    }


def _file_erc(sch_path: Path) -> dict:
    """Real-engine ERC on the SAVED schematic file via the installed CLI.
    kicad-cli sch erc loads the project's .kicad_pro (rule_severities /
    pin_map / exclusions), so the verdict matches the app's manual ERC as of
    the last save. Used as the fallback when the live editor is not open --
    NEVER replaced by the skidl build pre-check, which knows only a handful
    of rules and none of the user's settings."""
    cli = _find_kicad_cli_path()
    if not cli:
        return {"ok": False,
                "error": "kicad-cli not found -- cannot run real ERC on the file"}
    report = sch_path.with_suffix(".erc.json")
    try:
        # error+warning ONLY (never --severity-all: that adds the EXCLUSION
        # bit, resurfacing violations the user explicitly excluded in the
        # app -- the manual ERC view hides those, and we must match it).
        proc = subprocess.run(
            [cli, "sch", "erc", "--format", "json",
             "--severity-error", "--severity-warning",
             "--output", str(report), str(sch_path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180,
        )
    except Exception as exc:
        return {"ok": False, "error": f"kicad-cli sch erc failed to run: {exc!r}"}
    if not report.is_file():
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-800:]
        return {"ok": False,
                "error": f"kicad-cli sch erc exit {proc.returncode}: {tail}"}
    try:
        rep = json.loads(report.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        return {"ok": False, "error": f"unreadable ERC report: {exc!r}"}
    violations = []
    errors = warnings = 0
    for sheet in rep.get("sheets", []):
        for v in sheet.get("violations", []):
            sev = v.get("severity")
            if sev == "error":
                errors += 1
            elif sev == "warning":
                warnings += 1
            violations.append({"severity": sev, "type": v.get("type"),
                               "message": v.get("description")})
    return {"ok": True, "clean": errors == 0, "error_count": errors,
            "warning_count": warnings, "violations": violations[:50],
            # Checks the user set to 'ignore' in Violation Severity -- the
            # app skips them silently; we say WHICH were skipped.
            "ignored_checks": [c.get("key") for c in rep.get("ignored_checks", [])],
            "erc_report": str(report),
            "message": (f"{errors} error(s), {warnings} warning(s)."
                        if errors or warnings else
                        "ERC clean -- no errors or warnings.")}


def _stale_outputs(base: str):
    """The .py is the SOURCE OF TRUTH: if it is newer than the netlist,
    the design was edited without rebuilding -- downstream artifacts are
    stale and must not be used. Returns a reason string, or None."""
    py = pdir(base) / (base + ".py")
    net = pdir(base) / (base + ".net")
    if py.is_file() and net.is_file() \
            and py.stat().st_mtime > net.stat().st_mtime + 2:
        return (f"{base}.py was modified AFTER the netlist was generated -- "
                "the outputs are STALE. Run build first so the netlist/"
                "schematic regenerate from the edited source, then retry.")
    return None


def _board_fingerprint_path(base: str) -> Path:
    return pdir(base) / (base + ".board_fingerprint.json")


def _stamp_board_fingerprint(base: str) -> None:
    """Record the generated .anvil_pcb's hash so later runs can tell
    'still exactly what the pipeline wrote' from 'a human changed it'."""
    import hashlib
    pcb = pdir(base) / (base + ".anvil_pcb")
    if pcb.is_file():
        digest = hashlib.sha256(pcb.read_bytes()).hexdigest()
        _board_fingerprint_path(base).write_text(
            json.dumps({"sha256": digest}), encoding="utf-8")


def _detect_manual_edits(base: str):
    """Returns a human-readable reason string if <base>.anvil_pcb shows
    MANUAL edits (saved from the PCB editor, or changed outside the
    pipeline), else None.

    CONTRACT: this must agree with the canonical arbitration in
    skidl.board.board_setup.board_edit_status (same fingerprint-first
    rule) -- it is duplicated here only because this server process must
    never import skidl in-process (MCP stdio). A test locks the two to
    the same verdicts.

    ORDER MATTERS: the fingerprint hash is checked FIRST -- the pipeline's
    own zone-fill step (kicad-cli --save-board) rewrites the file with
    generator "pcbnew", so a matching hash proves machine-generated
    regardless of the generator token. The generator check only decides
    when no fingerprint exists."""
    import hashlib
    pcb = pdir(base) / (base + ".anvil_pcb")
    if not pcb.is_file():
        return None
    fp = _board_fingerprint_path(base)
    if fp.is_file():
        try:
            recorded = json.loads(fp.read_text(encoding="utf-8")).get("sha256")
            actual = hashlib.sha256(pcb.read_bytes()).hexdigest()
            if recorded:
                if recorded == actual:
                    return None       # exactly what the pipeline wrote
                return ("board file changed since the pipeline generated it "
                        "-- treat as the user's manual layout work")
        except Exception:
            pass
    head = pcb.read_text(encoding="utf-8", errors="replace")[:300]
    if '(generator "skidl_board")' not in head:
        return ("board was SAVED from the PCB editor (generator is no longer "
                "skidl_board) -- it contains the user's manual layout work")
    return None


def _board_verify_gate(base: str) -> dict:
    """Board<->netlist connectivity check (ipcd356 partition vs .net
    partition) -- the board analog of the schematic MISMATCH gate. Runs
    in a subprocess like every skidl-adjacent module."""
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.verify import verify_board as _vb
out = Path({json.dumps(str(pdir(base)))})
base = {json.dumps(base)}
cli = {json.dumps(_find_kicad_cli_path())}
try:
    v = _vb(out / (base + ".anvil_pcb"), out / (base + ".net"), cli)
    info = {{"ok": True, "verify": v}}
except Exception as exc:
    info = {{"ok": False, "verify_error": repr(exc)}}
print("\\n::RESULT::" + json.dumps(info))
'''
    res = _py_json(code, timeout=120)
    if not res.get("ok"):
        return {"board_matches_netlist": None,
                "verify_note": res.get("verify_error", "verify failed to run")}
    v = res.get("verify", {})
    out = {"board_matches_netlist": bool(v.get("ok"))}
    if not v.get("ok"):
        out["verify_mismatch"] = {
            "missing": v.get("mismatch_missing"),
            "extra": v.get("mismatch_extra"),
        }
    return out


# ---------------------------------------------------------------- tools --------
@server.tool()
@_quiet
def diagnostics() -> dict:
    """Environment health check: confirm the server uses THIS repo's skidl,
    which symbol dir is active, and where output goes."""
    code = f'''
import sys, os, json
sys.path.insert(0, {json.dumps(str(SRC))})
info = {{}}
try:
    from skidl.anvil import anvil_libs   # sets KICAD*_SYMBOL_DIR
    import skidl
    info["ok"] = True
    info["skidl_module"] = skidl.__file__
    info["symbol_dir"] = os.environ.get("KICAD9_SYMBOL_DIR")
except Exception as exc:
    info["ok"] = False
    info["skidl_import_error"] = repr(exc)
print("\\n::RESULT::" + json.dumps(info))
'''
    res = _py_json(code, timeout=60)
    res.update({"repo": str(REPO), "src_on_path": SRC.is_dir(),
                "out_dir": str(OUT), "python": PYEXE})

    # Board-pipeline environment (create_pcb + future routing milestones).
    import shutil
    cli = _find_kicad_cli_path()
    board = {"kicad_cli": cli or "NOT FOUND"}
    if cli:
        try:
            v = subprocess.run([cli, "version"], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=20)
            board["kicad_cli_version"] = (v.stdout or "").strip().splitlines()[-1]
        except Exception as exc:
            board["kicad_cli_version"] = f"probe failed: {exc!r}"
    # Routing engine probes via the board package's own detectors (they
    # know about the repo's portable tools/jre and tools/*.jar).
    try:
        import importlib.util as _ilu
        _fr_path = SRC / "skidl" / "board" / "route" / "freerouting.py"
        _spec = _ilu.spec_from_file_location("_fr_probe", str(_fr_path))
        _fr = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_fr)
        board["java"] = _fr.find_java() or \
            "NOT FOUND (drop a portable JRE in tools/jre/ or install Temurin)"
        board["freerouting_jars"] = _fr.find_freerouting_jars() or \
            ["NOT FOUND (put freerouting.jar in tools/)"]
    except Exception as exc:
        board["routing_probe_error"] = repr(exc)
    res["board"] = board
    return res


@server.tool()
@_quiet
def get_app_state() -> dict:
    """LOOK before you ACT -- the agent's first call every turn.

    Tells you WHAT is happening in Anvil CAD right now so you route correctly
    instead of blindly overwriting the user's work:
      WHICH  -> which project window is currently open (title-bar match)
      WHAT   -> how many parts/nets it holds, whether it has a PCB
      WHO    -> owner: 'ai' (we built it), 'user' (hand-made / hand-edited),
                'empty' (blank sheet), 'unknown'
      SCENARIO -> the routing hint you should follow next:
        'no_project'      : nothing open        -> fresh design (build)
        'fill_empty'      : blank project open   -> design into it (build folder=)
        'modify_ai'       : we own it            -> edit its .py + rebuild
        'adopt_manual'    : user made it, no .py -> adopt_project then edit
        'modify_manual'   : user hand-edited ours-> re-adopt (their file is truth)

    NEVER assume which project 'this' means when two are open -- ASK. NEVER
    overwrite a 'user'/'modify_manual' file without warning + a backup."""
    titles = _list_window_titles()
    projects = _known_projects()

    # LOOK LIVE FIRST: the open editor's in-memory model outranks any file on
    # disk. Counting a stale/missing .net or a hierarchical .anvil_sch made a
    # populated schematic wrongly read as 0 parts / owner 'empty'. Ask the
    # running app once; use it for whichever project is actually open.
    live = _live_design_counts()

    listed = []
    open_bases = []
    for base, d in projects:
        summ = _summarize_project(base, d)
        live_match = bool(live and live["base"]
                          and live["base"].lower() == base.lower())
        is_open = live_match or any(base.lower() in t for t in titles)
        # For the open project, trust the live counts over the disk parse.
        if is_open and live and (live_match or not live["base"]):
            summ = {**summ, "parts": live["parts"], "nets": live["nets"],
                    "source": "live_editor"}
        owner = _owner_of(base, d, summ["parts"])
        if is_open:
            open_bases.append(base)
        listed.append({
            "project": base,
            "dir": str(d),
            "open_in_app": is_open,
            "owner": owner,
            **summ,
        })

    # current = the open one; if several, the most-recently-touched (already
    # sorted newest-first) -- but flag ambiguity so the agent ASKS.
    current = next((p for p in listed if p["open_in_app"]), None)
    ambiguous = len(open_bases) > 1

    if current is None:
        scenario = "no_project"
    else:
        owner = current["owner"]
        scenario = {
            "empty": "fill_empty",
            "ai": "modify_ai",
            "user": "adopt_manual",
            "unknown": "no_project",
        }.get(owner, "adopt_manual")
        # an 'ai' project whose schematic was touched later is really manual now
        if owner == "user" and current.get("has_python"):
            scenario = "modify_manual"

    return {
        "current_project": current,
        "scenario": scenario,
        "multiple_open": ambiguous,
        "open_projects": open_bases,
        "all_projects": listed,
        "app_running": bool(titles),
        "note": ("ASK the user which project they mean before acting."
                 if ambiguous else
                 "No project window detected -- treat a design request as fresh."
                 if current is None else
                 f"Route by scenario='{scenario}'. If owner is 'user'/"
                 "'modify_manual', WARN before any write and keep a backup."),
    }


def _resolve_project(name: str) -> tuple:
    """Map a user-given name/path to (base, project_dir). Accepts a known
    project base, a folder, or a path to any of its files."""
    for base, d in _known_projects():
        if base == name:
            return base, d
    p = Path(name)
    if p.suffix.lower() in (".anvil_sch", ".anvil_pro", ".anvil_pcb", ".net"):
        return p.stem, p.parent
    if p.is_dir():
        return p.name, p
    return name, pdir(name)


@server.tool()
@_quiet
def adopt_project(name: str) -> dict:
    """REVERSE flow: turn a hand-made KiCad project (schematic/PCB, NO python)
    into an editable SKiDL <base>.py so the agent can then modify it.

    Use this for scenario 'adopt_manual'/'modify_manual' from get_app_state:
    the user drew the board in Anvil CAD, there is no .py, and they now ask to
    change it. Steps performed:
      1. find/export a netlist  (uses <base>.net, else kicad-cli exports it
         from <base>.anvil_sch)
      2. netlist_to_skidl        -> writes <base>.py (its OWN backup is kept if
         one already exists)
      3. reports parts/nets + whether a PCB is present

    After this, edit the returned python and rebuild with build(), OR make a
    surgical change. NEVER overwrite the user's .anvil_sch from the fresh build
    without warning -- their drawn layout is the truth."""
    base, d = _resolve_project(name)
    sch = d / (base + ".anvil_sch")
    net = d / (base + ".net")
    pcb = d / (base + ".anvil_pcb")
    py = d / (base + ".py")

    if not sch.is_file() and not net.is_file():
        return {"ok": False, "error": f"no .anvil_sch or .net found for '{base}' "
                f"in {d} -- nothing to adopt."}

    steps = []
    # 1. ensure a netlist exists
    if not net.is_file():
        cli = _find_kicad_cli_path()
        if not cli:
            return {"ok": False, "error": "kicad-cli not found -- cannot export a "
                    "netlist from the schematic. Install the CAD app or provide a "
                    f".net next to {sch.name}."}
        try:
            r = subprocess.run(
                [cli, "sch", "export", "netlist", "--output", str(net), str(sch)],
                cwd=str(d), env=_subprocess_env(), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=180,
            )
        except Exception as exc:
            return {"ok": False, "error": f"kicad-cli netlist export failed: {exc!r}"}
        if not net.is_file():
            return {"ok": False, "error": "kicad-cli did not produce a netlist",
                    "stdout_tail": (r.stdout or "")[-800:],
                    "stderr_tail": (r.stderr or "")[-800:]}
        steps.append(f"exported netlist -> {net.name}")
    else:
        steps.append(f"used existing netlist {net.name}")

    # 2. back up an existing entry point, then convert netlist -> SKiDL python.
    #    netlist_to_skidl(output_dir=...) writes main.py PLUS one module per
    #    hierarchical sheet -- the whole runnable project, not just a string
    #    (output_dir=None would import sub-blocks that were never written).
    entry = d / "main.py"
    for existing in (entry, py):
        if existing.is_file():
            i = 1
            while (d / f"{existing.name}.{i}.bak").exists():
                i += 1
            bak = d / f"{existing.name}.{i}.bak"
            try:
                bak.write_text(existing.read_text(encoding="utf-8",
                               errors="replace"), encoding="utf-8")
                steps.append(f"backed up {existing.name} -> {bak.name}")
            except OSError:
                pass

    code = f'''
import sys, os, json, re, glob
sys.path.insert(0, {json.dumps(str(SRC))})
res = {{}}
try:
    from skidl.anvil import anvil_libs          # sets symbol dirs
    from skidl.netlist_to_skidl import netlist_to_skidl
    outdir = {json.dumps(str(d))}
    before = set(glob.glob(os.path.join(outdir, "*.py")))
    netlist_to_skidl({json.dumps(str(net))}, output_dir=outdir)
    after = set(glob.glob(os.path.join(outdir, "*.py")))
    made = sorted(os.path.basename(p) for p in (after - before))
    entry_ok = os.path.isfile(os.path.join(outdir, "main.py"))
    txt = open({json.dumps(str(net))}, encoding="utf-8", errors="replace").read()
    res = {{"ok": True, "entry_ok": entry_ok, "modules": made,
           "parts": len(re.findall(r"\\(comp\\s+\\(ref", txt)),
           "nets": len(re.findall(r"\\(net\\s+\\(code", txt))}}
except Exception as exc:
    import traceback
    res = {{"ok": False, "error": repr(exc), "tb": traceback.format_exc()[-1200:]}}
print("\\n" + {json.dumps(_MARK)} + json.dumps(res))
'''
    conv = _py_json(code, timeout=240)
    if not conv.get("ok"):
        return {"ok": False, "error": "netlist_to_skidl failed",
                "detail": conv, "steps": steps}
    if not conv.get("entry_ok"):
        return {"ok": False, "error": "conversion ran but no main.py entry was "
                "written", "detail": conv, "steps": steps}
    mods = conv.get("modules") or []
    steps.append(f"generated {len(mods)} python file(s), entry main.py "
                 f"({conv.get('parts')} parts)")

    # ONE-SHOT rebuild consent: adopting IS the user's decision to continue
    # in code-land, so the next build may regenerate the user-owned sheet.
    # _prebuild_guard consumes this marker; a second build re-blocks.
    try:
        (d / (base + ".rebuild_ok")).write_text(
            "written by adopt_project -- consumed by the next build\n",
            encoding="utf-8")
    except OSError:
        pass

    return {
        "ok": True,
        "project": base,
        "dir": str(d),
        "python_entry": str(entry),
        "modules": mods,
        "netlist": str(net),
        "parts": conv.get("parts"),
        "nets": conv.get("nets"),
        "has_pcb": pcb.is_file(),
        "steps": steps,
        "note": ("Adopted. main.py + one module per sheet were written -- run/edit "
                 "main.py to rebuild. WARNING to relay to the user BEFORE any "
                 "rebuild: regeneration redraws the sheet -- the user's symbol "
                 "positions, wire routing, notes, Datasheet edits and DNP/"
                 "exclude-from-BOM flags will NOT survive (a netlist cannot "
                 "carry them). For small changes prefer edit_schematic or "
                 "edit_schematic_live instead of rebuilding. The next build is "
                 "pre-consented by this adoption (one shot); the previous files "
                 "are backed up as *.prebuild.bak." + (
                 " A PCB exists; read its stackup/pours via the board tools before "
                 "changing layers." if pcb.is_file() else "")),
    }


_LABEL_TAGS = ("label", "global_label", "hierarchical_label")


def _sch_files(base: str, d: Path) -> list:
    """All schematic sheets for a project (root + sub-sheets), newest first."""
    return sorted(d.glob(base + "*.anvil_sch"))


def _sym_prop(sym: list, key: str):
    """The (property "<key>" "<val>" ...) sub-list of a symbol, or None."""
    for sub in sym:
        if isinstance(sub, list) and len(sub) >= 3 and sub[0] == "property" \
                and sub[1] == key:
            return sub
    return None


def _close_paren(text: str, open_idx: int) -> int:
    """Index of the ')' that closes the '(' at open_idx, honouring quoted
    strings (so parens inside "..." don't miscount). -1 if unbalanced."""
    depth, i, n, instr = 0, open_idx, len(text), False
    while i < n:
        c = text[i]
        if instr:
            if c == '"' and text[i - 1] != "\\":
                instr = False
        elif c == '"':
            instr = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _instance_symbol_spans(text: str) -> list:
    """(start, end) spans of each INSTANCE symbol block -- i.e. (symbol (lib_id
    ...)) placed on a sheet. Library definitions inside (lib_symbols ...) start
    with (symbol "Lib:Name" and are deliberately skipped, so we never corrupt
    the symbol library."""
    spans = []
    for mo in re.finditer(r"\(symbol\s*\n\s*\(lib_id", text):
        start = mo.start()
        end = _close_paren(text, start)
        if end > start:
            spans.append((start, end))
    return spans


def _backup_file(f: Path) -> str:
    """Copy f to f.<n>.bak (never clobbering) before we mutate it."""
    i = 1
    while (f.parent / f"{f.name}.{i}.bak").exists():
        i += 1
    bak = f.parent / f"{f.name}.{i}.bak"
    bak.write_text(f.read_text(encoding="utf-8", errors="replace"),
                   encoding="utf-8")
    return bak.name


@server.tool()
@_quiet
def edit_schematic(name: str, op: str, ref: str = "", value: str = "",
                   net: str = "", new_name: str = "") -> dict:
    """SURGICAL edit of a project's .anvil_sch -- touch ONE thing, leave the
    user's layout otherwise byte-for-byte intact (never a full regenerate).

    ops:
      'list'         -> enumerate every symbol (ref, value, lib_id) + wire/label
                        counts. Call this FIRST to see what is editable.
      'set_value'    -> ref + value : change one part's Value (e.g. R1 -> 10k)
      'delete_part'  -> ref          : remove one symbol block
      'rename_net'   -> net + new_name : rename matching labels

    ALWAYS 'list' first to get exact refs. A backup (.bak) is written before any
    change and the result is re-parsed to prove it is still valid. delete_part
    can leave a wire dangling / a pin floating -- the response flags that; run a
    connectivity/ERC check and confirm with the user. For 'set_value' on a
    user-owned ('user'/'modify_manual') schematic, WARN before writing."""
    try:
        from simp_sexp import Sexp
    except Exception as exc:
        return {"ok": False, "error": f"simp_sexp unavailable: {exc!r}"}

    base, d = _resolve_project(name)
    files = _sch_files(base, d)
    if not files:
        return {"ok": False, "error": f"no .anvil_sch found for '{base}' in {d}"}

    # ---- list: read-only inventory across all sheets ----------------------
    if op == "list":
        symbols, wires, labels = [], 0, 0
        for f in files:
            try:
                s = Sexp(f.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            for ch in s:
                if not (isinstance(ch, list) and ch):
                    continue
                if ch[0] == "symbol":
                    r = _sym_prop(ch, "Reference")
                    v = _sym_prop(ch, "Value")
                    lib = next((x[1] for x in ch if isinstance(x, list)
                                and x and x[0] == "lib_id"), None)
                    symbols.append({"ref": r[2] if r else None,
                                    "value": v[2] if v else None,
                                    "lib_id": lib, "sheet": f.name})
                elif ch[0] == "wire":
                    wires += 1
                elif ch[0] in _LABEL_TAGS:
                    labels += 1
        return {"ok": True, "op": "list", "project": base,
                "symbols": symbols, "symbol_count": len(symbols),
                "wires": wires, "labels": labels}

    if op not in ("set_value", "delete_part", "rename_net"):
        return {"ok": False, "error": f"unknown op '{op}'. Use list / set_value / "
                "delete_part / rename_net."}

    # ---- mutating ops: TEXT surgery (never a full reserialize -- simp_sexp
    #      drops quotes on output, which would corrupt the sheet for KiCad).
    #      We locate the exact byte span and change only that, leaving the rest
    #      of the user's file identical. ----------------------------------
    changed_file = None
    detail = {}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        out = None

        if op in ("set_value", "delete_part"):
            for start, end in _instance_symbol_spans(text):
                block = text[start:end + 1]
                rmo = re.search(r'\(property "Reference" "([^"]*)"', block)
                if not rmo or rmo.group(1) != ref:
                    continue
                if op == "delete_part":
                    line_start = text.rfind("\n", 0, start) + 1
                    cut_end = end + 1
                    if text[cut_end:cut_end + 1] == "\n":
                        cut_end += 1
                    out = text[:line_start] + text[cut_end:]
                else:  # set_value
                    vmo = re.search(r'(\(property "Value" ")([^"]*)(")', block)
                    if not vmo:
                        return {"ok": False, "error": f"'{ref}' has no Value "
                                "property to set."}
                    detail = {"ref": ref, "old_value": vmo.group(2),
                              "new_value": value}
                    newblock = block[:vmo.start(2)] + value + block[vmo.end(2):]
                    out = text[:start] + newblock + text[end + 1:]
                break

        elif op == "rename_net":
            pat = re.compile(r'(\((?:%s) ")%s(")' %
                             ("|".join(_LABEL_TAGS), re.escape(net)))
            new_text, cnt = pat.subn(r"\g<1>" + new_name + r"\g<2>", text)
            if cnt:
                detail = {"net": net, "new_name": new_name, "labels_renamed": cnt}
                out = new_text

        if out is not None:
            try:
                Sexp(out)               # re-parse: prove the edit is still valid
            except Exception as exc:
                return {"ok": False, "error": "edit produced invalid s-expr -- "
                        f"aborted, file untouched: {exc!r}"}
            bak = _backup_file(f)
            f.write_text(out, encoding="utf-8")
            changed_file = f
            detail["backup"] = bak
            break

    if changed_file is None:
        if op in ("set_value", "delete_part"):
            return {"ok": False, "error": f"reference '{ref}' not found. "
                    "Call edit_schematic(name, op='list') for the exact refs."}
        return {"ok": False, "error": f"net label '{net}' not found. "
                "Call edit_schematic(name, op='list') to see labels."}

    warn = None
    if op == "delete_part":
        warn = (f"'{ref}' removed. Any wire that only touched it is now dangling "
                "and its net may have lost a node -- run a connectivity/ERC check "
                "and confirm the intent with the user.")

    return {"ok": True, "op": op, "project": base,
            "file": str(changed_file), **detail,
            "note": warn or "Surgical edit applied; a .bak backup was kept and "
            "the file re-parsed clean. If this sheet is user-owned, tell the user "
            "what changed."}


@_quiet
def search_parts(queries: list[str]) -> dict:
    """Batch part search/availability check against the Anvil CAD libraries.
    Call this FIRST, ONCE, with the FULL list of parts you need, BEFORE writing
    any circuit. Each item is "Lib:Name" (exact check) or a free-text search
    term (e.g. "resistor", "lm2596", "stm32 lqfp64"). Returns per query:
    exact (bool) + exact_part ("Lib:Name") + up to 40 candidate matches
    ({lib, part, desc}) + total_matches. Never invent Lib/Name -- use only
    names returned here."""
    code = f'''
import sys, json, re
sys.path.insert(0, {json.dumps(str(SRC))})
from skidl.anvil import anvil_libs
from skidl import KICAD9, set_default_tool
from skidl.part_query import PartSearchDB, show_part as _show
set_default_tool(KICAD9)
queries = json.loads({json.dumps(json.dumps([str(q) for q in queries]))})
db = PartSearchDB(tool=KICAD9)
db.load_from_lib_search_paths()

def _broaden(q):
    """Progressively broader forms of a part query so a full ordering code
    (ATmega328P-PU, LM317T, MPU-6050, 2N2222A) still resolves to the library
    family. Fully GENERIC -- no part name is hard-coded."""
    q = (q or "").strip()
    forms, seen = [], set()
    def add(f):
        f = (f or "").strip()
        if len(f) >= 2 and f.lower() not in seen:
            seen.add(f.lower()); forms.append(f)
    add(q)
    alnum = re.sub(r"[^A-Za-z0-9]", "", q)            # MPU-6050 -> MPU6050
    if alnum != q:
        add(alnum)
    segs = q.split("-")                               # drop trailing -suffix groups
    while len(segs) > 1:
        segs = segs[:-1]; add("-".join(segs))
    if len(q) > 3 and q[-1].isalpha() and any(c.isdigit() for c in q[:-1]):
        add(q[:-1])                                   # LM317T -> LM317; 2N2222A -> 2N2222
    words = q.split()                                 # drop trailing words
    while len(words) > 1:
        words = words[:-1]; add(" ".join(words))
    return forms

def _do_search(term):
    """db.search that AUTO-BROADENS until the library yields candidates, so an
    empty result never mis-reads as 'part missing'. Returns (matches, used)."""
    res = db.search(term)
    if res:
        return res, term
    for alt in _broaden(term):
        if alt.lower() == term.lower():
            continue
        r2 = db.search(alt)
        if r2:
            return r2, alt
    return [], term

results = []
for item in queries:
    item = item.strip()
    entry = {{"query": item, "exact": False, "exact_part": None,
              "matches": [], "total_matches": 0}}
    bare = item.split(":", 1)[-1].strip() if ":" in item else item
    if ":" in item:
        lib, nm = [s.strip() for s in item.split(":", 1)]
        try:
            p = _show(lib, nm)
        except Exception:
            p = None
        if p is not None:
            entry["exact"] = True
            entry["exact_part"] = lib + ":" + nm
        res, used = _do_search(nm)
    else:
        res, used = _do_search(item)
    if not entry["exact"]:
        for p in res:
            if p.part_name.lower() == item.lower():
                entry["exact"] = True
                entry["exact_part"] = p.lib_name + ":" + p.part_name
                break
    if used.lower() != bare.lower():
        entry["broadened_query"] = used
        entry["note"] = ("no library part matched '%s' exactly; showing the "
                         "'%s' family -- a package-suffix variant (e.g. -P PDIP, "
                         "-A TQFP) IS the same part, use it. Only a truly EMPTY "
                         "result (total_matches 0) means the part is missing "
                         "-> parts(action='add')." % (bare, used))
    entry["total_matches"] = len(res)
    res = sorted(res, key=lambda p: (p.lib_name, p.part_name))[:40]
    def _pin_count(p, _cache={{}}):
        # pin count per candidate so same-family package variants (SOIC-8 vs
        # SOIC-16) are distinguishable BEFORE one is picked; capped lazily --
        # symbol libs are parse-cached, so repeats are cheap.
        key = (p.lib_name, p.part_name)
        if key not in _cache:
            try:
                pt = _show(p.lib_name, p.part_name)
                _cache[key] = len(pt.pins) if pt is not None else None
            except Exception:
                _cache[key] = None
        return _cache[key]
    entry["matches"] = [
        {{"lib": p.lib_name, "part": p.part_name,
          "pins": _pin_count(p) if i < 20 else None,
          "desc": (p.description or "").strip()}} for i, p in enumerate(res)
    ]
    results.append(entry)
print("\\n::RESULT::" + json.dumps({{"ok": True, "results": results}}))
'''
    res = _py_json(code, timeout=240)
    if not res.get("ok"):
        return res
    return {
        "ok": True,
        "rule": "Never invent Lib/Name. exact=False -> show candidates and ASK "
                "the user before substituting; never substitute silently. When "
                "several same-family candidates differ, match 'pins' to the "
                "DATASHEET's pin count -- never pick a package variant by name "
                "alone (an 8-pin part built from a 16-pin sibling wires wrong "
                "with zero errors). Then call parts(action='describe') with the "
                "chosen Lib:Name list before writing any connection.",
        "results": res["results"],
    }


@_quiet
def describe_part(parts: list[str]) -> dict:
    """Machine-readable description of a LIST of parts in ONE call -- items are
    "Lib:Name" (or a bare name, resolved by exact match). Returns per part: the
    exact Part(...) line, ref_prefix, default value, description, keywords,
    DATASHEET link (use it for value calculations), and EVERY pin as
    {num, name, func}. Call this ONCE before writing a circuit body and write
    ALL connections ONLY from these exact names/numbers -- copy them verbatim
    ('VDD_1' is not 'VDD')."""
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from skidl.anvil import anvil_libs
from skidl import KICAD9, set_default_tool
from skidl.part_query import PartSearchDB, show_part as _show
set_default_tool(KICAD9)
items = json.loads({json.dumps(json.dumps([str(p) for p in parts]))})
db = None
out = {{}}
for item in items:
    item = item.strip()
    entry = {{"found": False}}
    lib = nm = None
    if ":" in item:
        lib, nm = [s.strip() for s in item.split(":", 1)]
    else:
        if db is None:
            db = PartSearchDB(tool=KICAD9)
            db.load_from_lib_search_paths()
        for p in db.search(item):
            if p.part_name.lower() == item.lower():
                lib, nm = p.lib_name, p.part_name
                break
    part = None
    if lib and nm:
        try:
            part = _show(lib, nm)
        except Exception:
            part = None
    if part is None:
        entry["error"] = ("not found -- run parts(action='search') with the user's ORIGINAL "
                          "part name (do not guess a new name)")
    else:
        entry["found"] = True
        entry["part"] = 'Part("%s", "%s")' % (lib, nm)
        entry["ref_prefix"] = getattr(part, "ref_prefix", "U") or "U"
        val = getattr(part, "value", None)
        if val and str(val) != nm:
            entry["value"] = str(val)
        desc = str(getattr(part, "description", "") or "").strip()
        if desc:
            entry["description"] = desc
        kw = str(getattr(part, "keywords", "") or "").strip()
        if kw:
            entry["keywords"] = kw
        ds = str(getattr(part, "datasheet", "") or "").strip()
        if ds:
            entry["datasheet"] = ds
        pins = []
        for pin in part.pins:
            fn = getattr(pin, "func", None)
            pins.append({{"num": str(pin.num),
                          "name": (pin.name or "").strip(),
                          "func": (getattr(fn, "name", "") or "").lower()}})
        pins.sort(key=lambda d: (0, int(d["num"])) if d["num"].isdigit() else (1, 0))
        entry["pins"] = pins
        entry["pin_count"] = len(pins)
    out[item] = entry
print("\\n::RESULT::" + json.dumps({{"ok": True, "parts": out}}))
'''
    res = _py_json(code, timeout=240)
    if not res.get("ok"):
        return res
    return {
        "ok": True,
        "parts": res["parts"],
        "rule": "write EVERY connection from these exact pin names/numbers -- "
                "verbatim, no abbreviating, no memory. func=power_in pins each "
                "need a decoupling cap to GND. Use the datasheet link for "
                "value calculations.",
    }


@server.tool()
@_quiet
def parts(action: str, items: list[str] = None, name: str = "",
          pins: list = None, lib: str = "UserParts", ref_prefix: str = "U",
          value: str = "", footprint: str = "", datasheet: str = "",
          description: str = "", keywords: str = "",
          mod_content: str = "", mod_path: str = "",
          pads_geometry: list = None, body: dict = None,
          expected_pads: int = None, expected_pins: int = None) -> dict:
    """The part-library tool. `action` selects:

    action='search'  -> items = list of "Lib:Name" exact checks or free-text
        terms. Auto-broadens ordering codes (ATmega328P-PU -> family). A part
        is 'missing' ONLY when total_matches is 0. Call ONCE with the FULL
        list BEFORE writing any circuit.
    action='describe' -> items = list of "Lib:Name". Returns the exact
        Part(...) line + EVERY pin {num, name, func} + datasheet. Never invent
        pins -- use only what this returns.
    action='add'      -> create a REAL symbol for a part the library lacks:
        `name`, `pins`=[{"num","name","func"}...] taken from the part's
        DATASHEET (show the user the pin table for confirmation FIRST; never
        invent pins), plus value/footprint (an existing "Lib:Name")/datasheet/
        description. ALWAYS pass expected_pins=<the datasheet's total pin
        count> -- the add is REFUSED if the supplied list's length differs or
        any pin number repeats (wrong pin data poisons every future design).
        Writes into the user library; returns the Part(...) line to use in
        build.

    action='add_footprint' -> create a footprint the library lacks. Either
        INSTALL a real .kicad_mod (mod_content=raw text or mod_path=file;
        validated + pad-count-checked vs expected_pads) or GENERATE from an
        explicit pad table (pads_geometry=[{"num","x","y","w","h","shape",
        "type","drill"}...] + body={"w","h"} -- every number FROM THE
        DATASHEET's land pattern, confirmed by the user; never guessed).
        Lands in UserFootprints.pretty, immediately usable as
        "UserFootprints:<name>" in symbols/assign_footprints/create_pcb.

    MISSING-PART RESOLUTION LADDER (works for ANY vendor's part): 1) library
    search (auto-broadens); 2) web datasheet search -- include vendor and
    distributor sources (LCSC, the manufacturer's own site; many real parts
    are Chinese/regional vendors absent from western sources -- 'not found in
    my search' NEVER means 'not a real part'); 3) still unresolved -> ASK THE
    USER for the datasheet PDF/link or pin table and add from that. Never
    invent, never dead-end -- the user always has their own BOM's datasheets.
    Same ladder for footprints: vendor CAD file > community .kicad_mod
    (verified vs the land-pattern drawing) > generate from the drawing's
    dimension table."""
    if action == "search":
        if not items:
            return {"ok": False, "error": "items (list of queries) required"}
        return search_parts(items)
    if action == "describe":
        if not items:
            return {"ok": False, "error": "items (list of 'Lib:Name') required"}
        return describe_part(items)
    if action == "add":
        if not name or not pins:
            return {"ok": False, "error": "name and pins required for action='add'"}
        return add_part_to_library(name, pins, lib=lib, ref_prefix=ref_prefix,
                                   value=value, footprint=footprint,
                                   datasheet=datasheet, description=description,
                                   keywords=keywords, expected_pins=expected_pins)
    if action == "add_footprint":
        if not name:
            return {"ok": False, "error": "name required for action='add_footprint'"}
        fp_lib = lib if lib != "UserParts" else "UserFootprints"
        code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from skidl.anvil import anvil_libs   # sets KICAD*_FOOTPRINT_DIR
from skidl.board.footprint_create import install_footprint, generate_footprint
kw = json.loads({json.dumps(json.dumps({
    "name": name, "lib": lib if lib != "UserParts" else "UserFootprints",
    "mod_content": mod_content, "mod_path": mod_path,
    "pads_geometry": pads_geometry, "body": body,
    "expected_pads": expected_pads}))})
try:
    if kw["mod_content"] or kw["mod_path"]:
        info = install_footprint(kw["name"], mod_content=kw["mod_content"],
                                 mod_path=kw["mod_path"], lib=kw["lib"],
                                 expected_pads=kw["expected_pads"])
    elif kw["pads_geometry"]:
        info = generate_footprint(kw["name"], kw["pads_geometry"],
                                  body=kw["body"], lib=kw["lib"])
    else:
        info = {{"ok": False, "error": "provide mod_content/mod_path (install) "
                                       "or pads_geometry (generate)"}}
except Exception as exc:
    import traceback
    info = {{"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-400:]}}
print("\\n::RESULT::" + json.dumps(info))
'''
        return _py_json(code, timeout=90)
    return {"ok": False,
            "error": "action must be 'search', 'describe', 'add' or 'add_footprint'"}


def get_build_source(name: str) -> dict:
    """Return the current SKiDL source of an existing project so a follow-up
    request can EXTEND it (add another sheet / block / section, or the next part
    of a document) instead of rebuilding it from scratch.

    The .py IS the project's single source of truth: re-running it regenerates
    every sheet. So the safe way to continue a design is to take the source
    returned here, add the new section to it, and call build with the SAME name
    and the FULL extended script -- every existing sheet is kept because it is
    still in the code, and only the addition is new. This is layout-agnostic: it
    works whether the design is one sheet with blocks or a multi-sheet hierarchy;
    the same style is reported back so the extension matches it."""
    base = _safe_name(name)
    py = pdir(base) / (base + ".py")
    if not py.is_file():
        return {"ok": False, "status": "not_found",
                "error": (f"no source on disk for '{base}' -- there is nothing to "
                          "continue, so this is a NEW project: build it normally.")}
    text = py.read_text(encoding="utf-8", errors="replace")
    # Which layout the existing project used, so the extension keeps the same style.
    if "flatness=1.0" in text:
        layout = "single"      # one sheet with boxed blocks
    elif "flatness=0.0" in text:
        layout = "hierarchy"   # one child sheet per @subcircuit page
    else:
        layout = "auto"
    return {
        "ok": True,
        "status": "ok",
        "name": base,
        "python_file": str(py),
        "layout": layout,
        "existing_files": _gather_files(base),
        "source": text,
        "how_to_continue": (
            "To ADD to this project (a new sheet / block / section, an edit, or the "
            "next part of the same document): keep ALL of 'source' above, add the new "
            "parts/nets/subcircuit into it, then call build(mode='script', name='"
            + base + "', code=<the full extended script>, layout='" + layout + "'). "
            "Building with the SAME name regenerates from the full script, so every "
            "existing sheet is preserved and only your addition is new. Never drop or "
            "shorten the existing sections. For a small in-place change to what is "
            "already drawn, prefer edit_schematic_live instead of a rebuild."),
    }


def _parse_pages(spec: str, n: int):
    """Turn a 1-based page spec ('1-3', '2,5', '1-3,7') into ordered 0-based
    indices within [0, n). Empty/blank -> every page."""
    if not spec or not spec.strip():
        return list(range(n))
    out = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                for x in range(int(a), int(b) + 1):
                    if 1 <= x <= n:
                        out.append(x - 1)
            else:
                x = int(part)
                if 1 <= x <= n:
                    out.append(x - 1)
        except ValueError:
            continue
    seen, res = set(), []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


@server.tool()
@_quiet
def read_pdf(path: str, pages: str = "", dpi: int = 150, max_pages: int = 20) -> dict:
    """SEE inside a PDF. The built-in Read tool cannot open .pdf files in this
    environment (no PDF renderer), so ALWAYS use read_pdf for a .pdf instead of
    Read. It renders each page to a PNG image and extracts any embedded text;
    then Read each returned page image to view the drawing (image Read works
    fine). `pages`='1-3' or '2,5' (1-based) selects pages; default = all, capped
    at `max_pages`. `dpi` sets sharpness (150 default; 200-300 for dense sheets).
    Trust the rendered image for wiring/connectivity; treat text only as a hint."""
    src = Path(path).expanduser()
    if not src.is_file():
        return {"ok": False, "error": f"no such file: {src}"}
    if src.suffix.lower() != ".pdf":
        return {"ok": False,
                "error": f"not a PDF -- use the built-in Read for {src.suffix} files: {src}"}
    try:
        import pypdfium2 as pdfium
    except Exception as e:
        return {"ok": False,
                "error": f"pypdfium2 is not available in this Python ({e}); cannot render PDF."}
    dpi = max(72, min(int(dpi or 150), 400))
    scale = dpi / 72.0
    try:
        pdf = pdfium.PdfDocument(str(src))
    except Exception as e:
        return {"ok": False, "error": f"could not open PDF: {e}"}
    try:
        n = len(pdf)
        requested = _parse_pages(pages, n)
        capped = len(requested) > max_pages
        want = requested[:max_pages]
        outdir = src.parent / (src.stem + "_pages")
        outdir.mkdir(exist_ok=True)
        rendered = []
        for i in want:
            page = pdf[i]
            try:
                pil = page.render(scale=scale).to_pil()
                img_path = outdir / f"{src.stem}_p{i + 1:03d}.png"
                pil.save(str(img_path))
                try:
                    tp = page.get_textpage()
                    text = tp.get_text_range()
                    tp.close()
                except Exception:
                    text = ""
            finally:
                page.close()
            rendered.append({"page": i + 1, "image": str(img_path),
                             "width": pil.size[0], "height": pil.size[1],
                             "text": text})
        res = {
            "ok": True,
            "source": str(src),
            "total_pages": n,
            "rendered": len(rendered),
            "dpi": dpi,
            "pages": rendered,
            "how_to_use": ("Read each 'image' path to SEE that page -- image Read needs no "
                           "PDF renderer. To reproduce a schematic, read every page image net "
                           "by net before drawing; use 'text' only as a hint."),
        }
        if capped:
            res["note"] = (f"rendered the first {max_pages} of {len(requested)} requested "
                           f"pages; call read_pdf again with pages= for the rest.")
        return res
    finally:
        pdf.close()


@server.tool()
@_quiet
def build(name: str, code: str = "", mode: str = "body",
          include_log: bool = False, view: str = "project",
          folder: str = "", layout: str = "auto",
          variant: str = "") -> dict:
    """The schematic-lifecycle tool. `mode` selects the action:

    mode='rules'  -> returns the MANDATORY design workflow + canonical
                     circuit BODY template. CALL THIS FIRST for any new
                     circuit (no name/code needed).
    mode='body' (PREFER for building): `code` is ONLY nets/parts/
        connections (no imports/build call -- template from mode='rules').
        Pre-checked (syntax, real parts/pins, ERC) in seconds; on
        'precheck_failed' apply fix_suggestions and call again. A clean
        body starts the async build.
    mode='script': `code` is a COMPLETE SKiDL script (hierarchical
        @subcircuit designs only). Skips the electrical pre-check.
    mode='source' -> return the existing project's SKiDL source so a
        follow-up request can EXTEND it (add another sheet / block /
        section, an edit, or the next part of a document) instead of
        rebuilding from scratch. CALL THIS FIRST whenever the user asks to
        continue / add to / change a project that already exists: take the
        returned 'source', add the new section, and rebuild with the SAME
        name and the FULL extended script so every existing sheet is kept.
    mode='status' -> poll the running build until 'done' (include_log for
        the build log tail).
    mode='bom'    -> Bill of Materials via the app's BOM engine, using the
                     project's own column preset (same columns as the user's
                     manual Symbol Fields Table export)
                     (missing[] must be empty). Optional `variant`: export a
                     specific assembly variant's BOM (names come back in the
                     result's 'variants' list); default = the default
                     variant, same as a freshly-opened app.
    mode='open'   -> open the finished project in the CAD app
                     (view: 'project'|'sch'|'pcb') -- ONCE, at the end.

    `folder` (optional): absolute path to put THIS project's files in,
        instead of the default <OUT>/<project>/. Use it when the user asks
        for a specific location; otherwise every project lands in its own
        <OUT>/<project>/ folder automatically.

    `layout` (optional) -- how the schematic is drawn:
        'single'    = ONE sheet with boxed functional blocks.
        'hierarchy' = one child SHEET per @subcircuit page (each page itself
                      laid out with boxed blocks). For 'hierarchy' the body
                      MUST wrap each section in an @subcircuit page function
                      (mode='script'); a flat body has no pages to split.
        'auto' (default) = pick by design size. If the user asks for one
        explicitly, pass it; otherwise leave 'auto'.

    Build flow: mode='rules' -> parts(action='search'/'describe') ->
    mode='body' -> mode='status' until done -> mode='open'."""
    if mode == "rules":
        return {"ok": True, "design_rules": get_design_rules()}
    if mode == "status":
        return build_status(name, include_log=include_log)
    if mode == "source":
        return get_build_source(name)
    if mode == "bom":
        return generate_bom(name, variant=variant)
    if mode == "open":
        return open_in_anvilcad(name, view=view)
    if mode not in ("body", "script"):
        return {"ok": False, "status": "precheck_failed",
                "error": "mode must be one of: rules, source, body, script, status, bom, open"}
    if not code:
        return {"ok": False, "status": "precheck_failed",
                "error": "code is required for mode='body'/'script'"}
    base = _safe_name(name)
    if folder:                       # user-chosen location for this project
        _PROJECT_DIRS[base] = Path(folder).expanduser()
    if mode == "script":
        # Strip GUI-launch lines to stay SKiDL-only (no external app window).
        # Pass 1: the whole call, incl. multi-line arg lists (one nesting
        # level), replaced by the same number of newlines so every later
        # SyntaxError lineno still matches the caller's original script.
        cleaned = re.sub(
            r"^[^\n]*\bopen_anvilcad\.[A-Za-z_]+\((?:[^()]|\([^()]*\))*\)[^\n]*$",
            lambda m: "\n" * m.group(0).count("\n"),
            code, flags=re.MULTILINE)
        # Pass 2 (safety net): blank any line still mentioning it -- bare
        # calls AND assignment/condition forms (x = open_anvilcad.f(...)).
        cleaned = re.sub(r"^.*\bopen_anvilcad\.[A-Za-z_]+\(.*$", "",
                         cleaned, flags=re.MULTILINE)
        try:
            compile(cleaned, base + ".py", "exec")
        except SyntaxError as e:
            return {"ok": False, "status": "precheck_failed",
                    "error": f"Python syntax error in script line {e.lineno or 1}: {e.msg}",
                    "bad_line": (e.text or "").rstrip()}
        # VERIFIED-PATH GATE: a script that calls generate_schematic() directly
        # bypasses smart_schematic.build() -- and with it the design gates
        # (floating pins / footprints), the schematic-vs-netlist connectivity
        # verify, atomic staging, and dangling-label cleanup. The TRACKER_V2
        # field build shipped a floating R1 exactly this way. Scripts MUST go
        # through smart_schematic.build().
        if re.search(r"^[^#\n]*\bgenerate_schematic\s*\(", cleaned, flags=re.MULTILINE):
            return {"ok": False, "status": "precheck_failed",
                    "error": "script calls generate_schematic() directly -- that "
                             "bypasses the design gates and the connectivity "
                             "verifier, so nothing proves the drawn schematic "
                             "matches the netlist. Replace it with "
                             "smart_schematic.build() (imports: from skidl.anvil "
                             "import smart_schematic). ERC()/generate_netlist() "
                             "may stay; smart_schematic.build() re-runs them "
                             "safely."}
        if "smart_schematic" not in cleaned:
            return {"ok": False, "status": "precheck_failed",
                    "error": "script never calls smart_schematic.build() -- the "
                             "schematic must be produced through it (design "
                             "gates + connectivity verify). Add: from skidl.anvil "
                             "import smart_schematic; ...; smart_schematic.build()"}
        review = _static_review(cleaned, body_mode=False)
        if review and review["errors"]:
            return _validation_failed(review)
        res = _start_build(base, cleaned)
        if review:
            if review["warnings"]:
                res["validation_warnings"] = review["warnings"]
            if review["matrix"]:
                res["connectivity_matrix"] = review["matrix"]
        return _await_short(base, res)
    # LAYOUT choice -> flatness passed to smart_schematic.build():
    #   'single'    -> ONE sheet with boxed functional blocks (flatness=1.0)
    #   'hierarchy' -> one child SHEET per @subcircuit page      (flatness=0.0)
    #   'auto'/else -> let smart_schematic pick by design size (default).
    _layout = (layout or "auto").strip().lower()
    if _layout in ("single", "single_sheet", "one_sheet", "blocks", "flat"):
        _build_opts = "flatness=1.0"
    elif _layout in ("hierarchy", "hierarchical", "sheets", "pages", "multi_sheet"):
        _build_opts = "flatness=0.0"
    else:
        _build_opts = ""
    script = _HARNESS.format(src=str(SRC), body=code, build_opts=_build_opts)
    # phase 0: instant syntax check (body line numbers reported relative to body)
    try:
        compile(script, base + ".py", "exec")
    except SyntaxError as e:
        offset = _HARNESS.split("{body}")[0].count("\n")
        return {"ok": False, "status": "precheck_failed",
                "error": f"Python syntax error in body line {max(1, (e.lineno or 1) - offset)}: {e.msg}",
                "bad_line": (e.text or "").rstrip()}
    # phase 0b: instant spec validation (pure-AST, body line numbers). Blocks the
    # expensive build on any MUST violation so the .py is right the first time.
    review = _static_review(code, body_mode=True)
    if review and review["errors"]:
        return _validation_failed(review)
    # phase 1: fast electrical dry-run (no schematic routing)
    pre = _dry_run(base, code)
    if not pre["ok"]:
        return pre
    # phase 2: the real (async) schematic build
    res = _start_build(base, script)
    res["precheck"] = pre["summary"]
    for k in ("parts", "nets", "verify", "semantic_warnings", "semantic_rule"):
        if pre.get(k):
            res[k] = pre[k]
    if review:
        if review["warnings"]:
            res["validation_warnings"] = review["warnings"]
        if review["matrix"]:
            res["connectivity_matrix"] = review["matrix"]
    return _await_short(base, res)


@_quiet
def build_status(name: str, include_log: bool = False) -> dict:
    """Check/wait on a background build started by build. Waits up to ~20 s:
    returns {'status':'done', generated} when finished, or {'status':'building',
    elapsed_s} if still routing -- in that case just call build(mode='status') again
    (big circuits take 2-4 minutes; 'building' is normal, NOT an error).
    `generated` always lists the produced file paths. include_log=True adds
    the build-log tail; FAILED builds always include the log."""
    base = _safe_name(name)
    info = _BUILDS.get(base)
    if info is None:
        files = _gather_files(base)
        logfile = pdir(base) / (base + ".build.log")
        if files.get("net") and files.get("anvil_sch"):
            res = {"ok": True, "status": "done", "generated": files,
                   "note": "build finished earlier; results read from disk"}
            if include_log and logfile.is_file():
                res["log"] = logfile.read_text(
                    encoding="utf-8", errors="replace")[-6000:]
            return res
        if logfile.is_file():
            # the build ran and died -- that is a failure, not "unknown"
            return {"ok": False, "status": "failed", "generated": files,
                    "error": "build ran earlier but produced no netlist/"
                             "schematic -- see log",
                    "log": logfile.read_text(
                        encoding="utf-8", errors="replace")[-6000:]}
        return {"ok": False, "status": "unknown",
                "error": f"no build known for '{base}' -- call build first"}
    proc = info["proc"]
    elapsed = time.time() - info["started"]
    if elapsed > _MAX_BUILD_S and proc.poll() is None:
        proc.kill()
        proc.wait()
        res = _finish_build(base)
        res["error"] = f"build killed after {_MAX_BUILD_S}s (runaway)"
        return res
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        return {"ok": True, "status": "building",
                "elapsed_s": round(time.time() - info["started"]),
                "note": "still routing the schematic -- call build(mode='status') again"}
    res = _finish_build(base)
    if res.get("status") == "done" and not include_log:
        res.pop("log", None)
    return res


@_quiet
def open_in_anvilcad(name: str, view: str = "project") -> dict:
    """Open a generated circuit in the Anvil CAD app. NOTE: a successful build
    already AUTO-OPENS itself (build(mode='status') reports 'opened_in_anvilcad'), so
    this is only needed to re-open something later. The install location is
    AUTO-DETECTED (any user / any machine). It launches even if the app is
    busy with other projects; it only skips when a window for THIS project
    already exists (then: File > Revert reloads it).
    view = 'project' (recommended) | 'sch' (schematic) | 'pcb' (board)."""
    base = _safe_name(name)
    return {"target": str(pdir(base) / base), "status": _try_open(base, view)}


# ============================================================================
#  Custom parts -> BOM -> design rules
# ============================================================================
CACHE = os.path.join(os.path.expanduser("~"), "skidl_symbols")  # anvil_libs cache

_PIN_TYPES = {"input", "output", "bidirectional", "tri_state", "passive", "free",
              "unspecified", "power_in", "power_out", "open_collector",
              "open_emitter", "no_connect"}


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _norm_pins(pins):
    """Normalize a user pin list to [(num, name, type)] -- shared by the symbol
    writer and the add-time validation gates so both see identical data."""
    norm = []
    for i, p in enumerate(pins):
        if not isinstance(p, dict):   # tolerate "3" or "3:OUT" style items
            s = str(p)
            num, _, pn = s.partition(":")
            p = {"num": num.strip() or i + 1, "name": pn.strip()}
        num = str(p.get("num", i + 1)).strip()
        pn = (str(p.get("name") or "~").strip()) or "~"
        typ = str(p.get("type") or "passive").strip().lower()
        if typ not in _PIN_TYPES:
            typ = "passive"
        norm.append((num, pn, typ))
    return norm


def _make_symbol(name, pins, ref_prefix="U", value=None, footprint="",
                 datasheet="", description="", keywords=""):
    """Build a valid KiCad (symbol ...) block: a rectangle body + pins on left/right."""
    value = value or name
    norm = _norm_pins(pins)
    n = len(norm)
    left, right = norm[: (n + 1) // 2], norm[(n + 1) // 2:]
    length, spacing, half_w = 2.54, 2.54, 10.16
    rows = max(len(left), len(right), 1)
    half_h = round(((rows - 1) * spacing) / 2 + 2.54, 3)

    def pin_line(num, pn, typ, x, y, ang):
        return (f'(pin {typ} line (at {x} {y} {ang}) (length {length})\n'
                f'  (name "{_esc(pn)}" (effects (font (size 1.27 1.27))))\n'
                f'  (number "{_esc(num)}" (effects (font (size 1.27 1.27)))))')

    lines = []
    ytop_l = ((len(left) - 1) * spacing) / 2
    for i, (num, pn, typ) in enumerate(left):
        lines.append(pin_line(num, pn, typ, -(half_w + length), round(ytop_l - i * spacing, 3), 0))
    ytop_r = ((len(right) - 1) * spacing) / 2
    for i, (num, pn, typ) in enumerate(right):
        lines.append(pin_line(num, pn, typ, (half_w + length), round(ytop_r - i * spacing, 3), 180))
    pins_body = "\n".join(lines)

    top = round(half_h + 2.54, 3)
    props = [
        ("Reference", ref_prefix, f"(at 0 {top} 0)", ""),
        ("Value", value, f"(at 0 {round(top - 2.54, 3)} 0)", ""),
        ("Footprint", footprint, "(at 0 0 0)", "(hide yes)"),
        ("Datasheet", datasheet, "(at 0 0 0)", "(hide yes)"),
        ("Description", description, "(at 0 0 0)", "(hide yes)"),
        ("ki_keywords", keywords, "(at 0 0 0)", "(hide yes)"),
    ]
    prop_txt = "\n".join(
        f'(property "{k}" "{_esc(v)}" {at} {hide} (effects (font (size 1.27 1.27))))'
        for k, v, at, hide in props
    )
    return (
        f'(symbol "{_esc(name)}"\n'
        f'(pin_numbers (hide no))\n(pin_names (offset 0.254))\n'
        f'(exclude_from_sim no)\n(in_bom yes)\n(on_board yes)\n'
        f'{prop_txt}\n'
        f'(symbol "{_esc(name)}_0_1"\n'
        f'(rectangle (start {-half_w} {half_h}) (end {half_w} {-half_h}) '
        f'(stroke (width 0.254) (type default)) (fill (type background))))\n'
        f'(symbol "{_esc(name)}_1_1"\n{pins_body}\n)\n)'
    )


def _extract_symbols_local(txt):
    """Top-level (symbol ...) blocks via paren matching (local copy of
    anvil_libs._extract_symbols so this server never imports skidl in-process)."""
    out, n, i, key = [], len(txt), 0, "(symbol "
    while True:
        s = txt.find(key, i)
        if s == -1:
            break
        depth, j, in_str, esc = 0, s, False, False
        while j < n:
            c = txt[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    out.append(txt[s:j + 1])
                    break
            j += 1
        i = j + 1
    return out


def _write_lib(path, blocks):
    body = "\n".join("\t" + b.replace("\n", "\n\t") for b in blocks)
    content = ('(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "anvil_mcp")\n'
               '\t(generator_version "9.0")\n' + body + "\n)\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


@_quiet
def add_part_to_library(name: str, pins: list, lib: str = "UserParts",
                        ref_prefix: str = "U", value: str = "", footprint: str = "",
                        datasheet: str = "", description: str = "",
                        keywords: str = "", expected_pins: int = None) -> dict:
    """EXACT-match escape hatch: create a missing part in a custom library so
    nothing is substituted. ONLY call this AFTER the user has explicitly agreed
    to create the new part -- never on your own, and NEVER with pins from
    memory: type the pin list from the part's DATASHEET. `pins` = list of
    {"num","name","type"} where type is a KiCad electrical type (input/output/
    bidirectional/passive/power_in/power_out/...; default passive). Writes to
    <cache>/<lib>.kicad_sym -- keep `lib` a name that does NOT collide with a
    vendor library ('UserParts' is safe; 'Custom' is app-managed and gets
    regenerated, wiping anything added there). After this,
    Part("<lib>","<name>") works -- call parts(action='describe') and wire from its pin
    JSON. Returns the path + the Part(...) line to use."""
    if not pins:
        return {"ok": False, "error": "pins required: list of {'num','name','type'}"}
    # SYMBOL PIN GATES (the symbol-side twin of the footprint installer's
    # expected_pads check): wrong pin data entered here poisons every future
    # design that uses this part, so refuse it at the door.
    norm = _norm_pins(pins)
    nums = [n for n, _, _ in norm]
    dups = sorted({n for n in nums if nums.count(n) > 1})
    if dups:
        return {"ok": False,
                "error": f"duplicate pin number(s) {dups} -- every symbol pin "
                         "number must be unique. A datasheet pin table never "
                         "repeats a number; re-read it and send the exact list."}
    if expected_pins is not None and len(norm) != int(expected_pins):
        return {"ok": False,
                "error": f"pin-count mismatch: {len(norm)} pins supplied but "
                         f"expected_pins={expected_pins}. Re-read the datasheet "
                         "pin table -- do NOT add the part until the count "
                         "matches (a wrong-pin-count symbol wires wrong with "
                         "zero errors downstream)."}
    os.makedirs(CACHE, exist_ok=True)
    block = _make_symbol(name, pins, ref_prefix=ref_prefix, value=value or name,
                         footprint=footprint, datasheet=datasheet,
                         description=description, keywords=keywords)
    path = os.path.join(CACHE, lib + ".kicad_sym")
    keep = []
    if os.path.isfile(path):
        old = open(path, encoding="utf-8", errors="replace").read()
        for b in _extract_symbols_local(old):
            if not b.lstrip().startswith(f'(symbol "{_esc(name)}"'):
                keep.append(b)
    _write_lib(path, keep + [block])
    return {
        "ok": True,
        "library_file": path,
        "use": f'Part("{lib}", "{name}", ref="{ref_prefix}1", tag="{ref_prefix}1"'
               + (f', footprint="{footprint}"' if footprint else "") + ")",
        "pins_added": len(norm),
        "note": "New part is now on the symbol search path; build (subprocess) "
                "will find it immediately.",
    }


def _parse_comps(net_txt):
    comps = []
    for m in re.finditer(r"\(comp\b", net_txt):
        chunk = net_txt[m.start():m.start() + 900]
        ref = re.search(r'\(ref "([^"]*)"\)', chunk)
        val = re.search(r'\(value "([^"]*)"\)', chunk)
        fp = re.search(r'\(footprint "([^"]*)"\)', chunk)
        if ref:
            comps.append({"ref": ref.group(1),
                          "value": val.group(1) if val else "",
                          "footprint": fp.group(1) if fp else ""})
    return comps


def _project_bom_settings(base: str) -> dict:
    """The ACTIVE BOM view from the project file -- the exact columns, labels,
    grouping, sort and delimiters the user sees in the app's Symbol Fields
    Table (schematic.bom_settings / bom_fmt_settings in .anvil_pro). Empty
    fields list = user never customized -> the CLI's own defaults apply."""
    out = {"fields": [], "labels": [], "group_by": [], "sort_field": "",
           "sort_asc": True, "filter": "", "exclude_dnp": False,
           "group_symbols": True, "fmt": {}, "export_filename": ""}
    pro = pdir(base) / (base + ".anvil_pro")
    try:
        # utf-8-sig: a BOM byte at the head of a hand-touched project file
        # must not kill the parse.
        j = json.loads(pro.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        return out
    sch = j.get("schematic") or {}
    bs = sch.get("bom_settings") or {}
    for f in bs.get("fields_ordered") or []:
        if f.get("show"):
            out["fields"].append(f.get("name", ""))
            out["labels"].append(f.get("label") or f.get("name", ""))
            if f.get("group_by"):
                out["group_by"].append(f.get("name", ""))
    out["sort_field"] = bs.get("sort_field") or ""
    out["sort_asc"] = bool(bs.get("sort_asc", True))
    out["filter"] = bs.get("filter_string") or ""
    out["exclude_dnp"] = bool(bs.get("exclude_dnp", False))
    # The dialog's 'Group symbols' checkbox is the MASTER switch: unchecked
    # means the per-field Group By ticks are inert (one row per component) --
    # mirror it by dropping --group-by entirely.
    out["group_symbols"] = bool(bs.get("group_symbols", True))
    # 'Include Exclude-from-BOM symbols' needs NO mapping: the app forces it
    # OFF for every export (view-only checkbox) -- also the CLI default.
    out["fmt"] = sch.get("bom_fmt_settings") or {}
    out["export_filename"] = sch.get("bom_export_filename") or ""
    # Assembly variants registered in the project (per-instance DNP/field
    # overrides live inside the schematic; only names are listed here).
    out["variants"] = [v.get("name") for v in sch.get("variants") or []
                       if isinstance(v, dict) and v.get("name")]
    return out


def _netlist_bom_audit(base: str) -> dict:
    """Ref/value/footprint audit from the netlist: qty grouping + the
    missing-value/footprint flags. JSON-only supplement -- never the CSV."""
    net = pdir(base) / (base + ".net")
    if not net.is_file():
        return {"components": [], "line_items": [], "missing": []}
    comps = _parse_comps(net.read_text(encoding="utf-8", errors="replace"))
    missing = []
    for c in comps:
        probs = []
        if not c["value"]:
            probs.append("value")
        if not c["footprint"]:
            probs.append("footprint")
        c["status"] = "OK" if not probs else ("MISSING " + " & ".join(probs))
        if probs:
            missing.append(c["ref"])
    groups = {}
    for c in comps:
        groups.setdefault((c["value"], c["footprint"]), []).append(c["ref"])
    return {
        "components": sorted(comps, key=lambda x: x["ref"]),
        "line_items": [
            {"value": k[0], "footprint": k[1], "qty": len(v), "refs": sorted(v)}
            for k, v in sorted(groups.items())
        ],
        "missing": missing,
    }


def _bom_csv_path(base: str, s: dict | None = None) -> Path:
    """The ONE user-facing BOM CSV path -- the app's own export target
    (bom_export_filename, default ${PROJECTNAME}.csv i.e. <base>.csv). This is
    the single ready-to-order file; we do NOT also leave a parallel
    <base>_bom.csv cluttering the project folder. When the project's export
    target is a spreadsheet (.xlsx/.xls) we still emit CSV under <base>.csv,
    because the CLI BOM engine here produces CSV."""
    if s is None:
        s = _project_bom_settings(base)
    exp = (s.get("export_filename") or "").replace("${PROJECTNAME}", base)
    if exp and not exp.lower().endswith((".xlsx", ".xls")):
        p = Path(exp)
        return p if p.is_absolute() else pdir(base) / p
    return pdir(base) / (base + ".csv")


@_quiet
def generate_bom(name: str, variant: str = "") -> dict:
    """Bill of Materials with ANSWER PARITY: the CSV is produced by the app's
    own BOM engine (anvil-cli sch export bom) on <name>.anvil_sch, using the
    project's OWN column preset (bom_settings in .anvil_pro) -- so the AI's
    BOM has the SAME columns, labels, grouping and delimiters as the user's
    manual Symbol Fields Table export. Writes the ONE ready-to-order file
    <name>.csv (the app's own export target). The JSON return
    adds a netlist audit (qty line items + missing value/footprint flags,
    must be empty); those flags are NEVER columns in the CSV. Falls back to a
    plain netlist CSV only when the schematic or CLI is unavailable."""
    base = _safe_name(name)
    s = _project_bom_settings(base)
    # ONE ready-to-order file only (the app's own export target). We used to
    # also drop a parallel <base>_bom.csv -- that just left two identical CSVs
    # confusing the user, so the packaging pipeline now reads this same file.
    csv_path = _bom_csv_path(base, s)
    audit = _netlist_bom_audit(base)
    sch = pdir(base) / (base + ".anvil_sch")
    cli = _find_kicad_cli_path()

    if sch.is_file() and cli:
        # Variant must be VALIDATED here: the CLI silently falls back to the
        # default variant on an unknown name -- a wrong-variant BOM shipped
        # as the right one is worse than an error.
        if variant and variant not in s.get("variants", []):
            return {"ok": False,
                    "error": f"unknown variant '{variant}'",
                    "variants": s.get("variants", []),
                    "note": "pick one of 'variants' (or omit for the "
                            "default variant)"}
        try:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        cmd = [cli, "sch", "export", "bom", "--output", str(csv_path)]
        if variant:
            cmd += ["--variant", variant]
        if s["fields"]:
            cmd += ["--fields", ",".join(s["fields"]),
                    "--labels", ",".join(s["labels"])]
        # 'Group symbols' checkbox is the master switch over the per-field
        # Group By ticks -- when the user unchecked it, export ungrouped.
        if s["group_by"] and s["group_symbols"]:
            cmd += ["--group-by", ",".join(s["group_by"])]
        if s["sort_field"]:
            cmd += ["--sort-field", s["sort_field"]]
        if s["filter"]:
            cmd += ["--filter", s["filter"]]
        if s["exclude_dnp"]:
            cmd += ["--exclude-dnp"]
        fmt = s["fmt"]
        # EMPTY delimiter is a real setting (ref_range_delimiter "" = never
        # collapse C4,C5 into C4-C5) -- pass on key presence, not truthiness.
        for key, flag in (("field_delimiter", "--field-delimiter"),
                          ("string_delimiter", "--string-delimiter"),
                          ("ref_delimiter", "--ref-delimiter"),
                          ("ref_range_delimiter", "--ref-range-delimiter")):
            if key in fmt:
                cmd += [flag, fmt[key]]
        if fmt.get("keep_tabs"):
            cmd += ["--keep-tabs"]
        if fmt.get("keep_line_breaks"):
            cmd += ["--keep-line-breaks"]
        cmd.append(str(sch))
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=180)
        except Exception as exc:
            proc = None
            cli_err = f"anvil-cli export bom failed to run: {exc!r}"
        if proc is not None and proc.returncode == 0 and csv_path.is_file():
            try:
                lines = csv_path.read_text(
                    encoding="utf-8-sig", errors="replace").splitlines()
            except Exception:
                lines = []
            header = lines[0] if lines else ""
            # Descending sort: the CLI cannot express sort_asc=false (its
            # --sort-asc arg only holds the default), so ask ascending and
            # reverse the data rows -- same order the app dialog shows.
            if not s["sort_asc"] and len(lines) > 2:
                lines = [lines[0]] + list(reversed(lines[1:]))
                csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return {
                "ok": len(audit["missing"]) == 0,
                "source": "app_engine",
                "bom_csv": str(csv_path),
                "columns": header,
                "columns_from": ("project bom_settings (matches the user's "
                                 "Symbol Fields Table view)" if s["fields"]
                                 else "app default preset"),
                "variant": variant or "(default)",
                "variants": s.get("variants", []),
                "grouped": bool(s["group_by"] and s["group_symbols"]),
                "total_parts": len(audit["components"]),
                "line_items": audit["line_items"],
                "missing": audit["missing"],
                "note": "missing must be empty. If not, fill value/footprint "
                        "on those parts and rebuild.",
            }
        cli_err = cli_err if proc is None else (
            f"anvil-cli export bom exit {proc.returncode}: "
            + ((proc.stderr or "") + (proc.stdout or ""))[-400:])
    else:
        cli_err = ("schematic not found -- run build first" if not sch.is_file()
                   else "anvil-cli not found")

    # FALLBACK ONLY: plain netlist CSV (not the app's columns) -- say so.
    if not audit["components"]:
        return {"ok": False, "error": f"BOM unavailable: {cli_err}; "
                                      "and no netlist to fall back on"}
    rows = ["Ref,Value,Footprint"]
    for c in audit["components"]:
        rows.append(f'{c["ref"]},{c["value"]},{c["footprint"]}')
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return {
        "ok": len(audit["missing"]) == 0,
        "source": "netlist_fallback",
        "fallback_reason": cli_err,
        "bom_csv": str(csv_path),
        "columns": rows[0],
        "total_parts": len(audit["components"]),
        "line_items": audit["line_items"],
        "missing": audit["missing"],
        "note": "FALLBACK CSV (netlist scrape) -- columns will NOT match the "
                "app's BOM export; missing must be empty.",
    }


def _requirements_confirmed(base: str) -> bool:
    """True when the requirements conversation happened for this project.
    Keyed on INIT-WRITTEN sidecar markers: `requirements_confirmed`
    (new) or `manufacturer` (every legacy init wrote it -- old projects
    never re-gate). NOT bare file existence: update_board_setup also
    creates the sidecar (setup_hash only) without any requirements ever
    being answered -- that must still gate."""
    scp = pdir(base) / (base + ".board_config.json")
    if not scp.is_file():
        return False
    try:
        sc = json.loads(scp.read_text(encoding="utf-8"))
    except Exception:
        return False
    return bool(sc.get("requirements_confirmed") or "manufacturer" in sc)


def _generic_requirements_questionnaire(error: str) -> dict:
    """FAIL-CLOSED fallback when the design-aware generator crashes: the
    gate's purpose is the conversation, not the analysis -- never proceed
    silently just because the analyzer broke."""
    def q(qid, question, params, skip_means, default):
        return {"id": qid, "question": question,
                "why_it_matters": "core board decision -- silently "
                                  "defaulting it produced wrong boards",
                "options": [], "default_if_skipped": default,
                "skip_means": skip_means, "detected_evidence": "",
                "answer_via": {"tool": "initialize_pcb_project",
                               "params": params}}
    return {
        "ok": True, "generator_error": error,
        "questions": [
            q("board_size", "Must the board fit an enclosure? Exact "
              "width x height in mm?", ["board_width", "board_height"],
              "omit board_width/board_height; outline auto-sizes",
              "auto-sized tightest rectangle"),
            q("layers", "How many copper layers -- 2 or 4?", ["layers"],
              "omit the layers parameter; 2 is used", "2 layers"),
            q("manufacturer", "Which board manufacturer (jlcpcb/pcbway/"
              "generic)?", ["manufacturer"],
              "omit the manufacturer parameter; jlcpcb rules are used",
              "jlcpcb profile rules"),
        ],
        "limitations": [], "already_answered": {},
        "instruction": "design analyzer unavailable -- ask these generic "
                       "questions instead",
    }


@server.tool()
@_quiet
def create_pcb(name: str, route=None,
               force: bool = False, accept_defaults: bool = False) -> dict:
    """Create a placed (and, once the user OKs it, routed) .anvil_pcb.

    ROUTE GATE (professional order: place -> ASK -> route): `route` is a
    3-state control and DEFAULTS TO ASK -- the engine places the board and
    STOPS, so the user decides whether to route (never auto-routes silently):
      * route omitted / 'ask'  -> placement only, then returns
        status:'placed_unrouted' + route_prompt. SHOW that to the user and
        ASK 'Route the board now?'. If the board is OPEN in the app the user
        can WATCH it -- prefer autoroute_live (visible progress); otherwise
        call create_pcb(name, route='yes') for a headless route.
      * route True / 'yes'     -> place AND route in one headless job.
      * route False / 'no'     -> placement only, no route prompt (the user
        explicitly wanted placement only).
    After a successful route the result carries output_prompt: ASK the user
    'Generate BOM / Gerbers now?' before export_manufacturing -- never
    auto-generate outputs.

    Create a placed AND routed .anvil_pcb from a finished build's netlist.
    STEP ZERO: if you haven't analyzed this machine yet this session, call
    analyze_pcb_environment first -- every user's system and Board Setup
    differs, and user-set values are always detected and kept.
    FIRST RUN of a project the user never initialized returns
    status:'requirements_needed' with a design-aware questionnaire: ASK
    THE USER those questions, submit the answers via
    initialize_pcb_project, show its resolved_configuration and ask
    'Proceed?', then call create_pcb again. accept_defaults=True skips
    the questionnaire ONLY when the user already explicitly agreed to
    defaults. The gate fires on every call until requirements are
    confirmed; any successful initialize_pcb_project confirms them.
    Requires that build/build(mode='status') already produced <name>.net. Pipeline:
    resolve every part's REAL footprint (fails early listing any missing
    "Lib:Name") -> rule placement (anchor IC centered, decaps beside their IC,
    connectors on the edge, zero courtyard overlaps) -> INVISIBLE FreeRouting
    autorouting (headless -- the user NEVER sees a routing window; they only
    see the result in KiCad) -> <name>.anvil_pcb + net classes in .anvil_pro
    -> kicad-cli DRC gate -> board-vs-netlist connectivity verification.
    Report status honestly: 'routed' only when routing completed AND the board
    matches the netlist pin-for-pin; otherwise say exactly what is missing.
    After success, open with build(mode='open')(name, view='pcb')."""
    base = _safe_name(name)
    # ROUTE MODE: place -> ASK -> route. Default asks before routing so the
    # user (not the engine) decides. Accepts bool for back-compat and the
    # natural words a user/AI would pass. Fully dynamic -- no design assumed.
    def _route_mode(r):
        if r is None:
            return "ask"
        if isinstance(r, bool):
            return "yes" if r else "no"
        s = str(r).strip().lower()
        if s in ("yes", "true", "1", "route", "route_now", "autoroute"):
            return "yes"
        if s in ("no", "false", "0", "place", "placement", "place_only",
                 "placeonly", "placed", "dont_route", "no_route"):
            return "no"
        return "ask"                       # any unknown word -> safe default
    route_mode = _route_mode(route)
    do_route = route_mode == "yes"         # what the headless job receives
    net = pdir(base) / (base + ".net")
    if not net.is_file():
        return {"ok": False, "error": f"netlist not found: {net} -- run build first"}

    # ---- ASYNC job engine: big boards need 10-30 min of routing, which
    # no MCP client will wait for (measured: the 150-part tracker timed
    # out Desktop at 4 min). The whole attempt-chain + gates run in ONE
    # background process (board/pcb_job.py); this tool STARTS it and
    # subsequent calls POLL it -- same UX as build/build(mode='status').
    job = _PCB_JOBS.get(base)
    result_file = pdir(base) / (base + ".pcb_result.json")

    if job and job["proc"].poll() is None:
        elapsed = int(time.time() - job["started"])
        if elapsed > 14400:   # adaptive budgets: big boards run hours
            job["proc"].kill()
            _PCB_JOBS.pop(base, None)
            return {"ok": False, "status": "failed",
                    "error": "PCB job exceeded 60 min -- killed; check "
                             f"{base}.pcb_build.log"}
        tail = ""
        logf = pdir(base) / (base + ".pcb_build.log")
        if logf.is_file():
            tail = logf.read_text(encoding="utf-8", errors="replace")[-500:]
        return {"ok": True, "status": "pcb_building", "elapsed_s": elapsed,
                "log_tail": tail,
                "note": ("PCB build running in the background (placement + "
                         "routing attempts + gates -- big boards can take "
                         "10-30 min). Call create_pcb(name) again in 1-3 "
                         "minutes to poll. Do NOT start other builds of the "
                         "same project meanwhile.")}

    if job and job["proc"].poll() is not None and not result_file.is_file():
        _PCB_JOBS.pop(base, None)
        return {"ok": False, "status": "failed",
                "error": f"PCB job exited (rc={job['proc'].returncode}) "
                         f"without a result -- check {base}.pcb_build.log"}

    if result_file.is_file():
        # Finished (tracked job, or a detached job surviving a server
        # restart -- Desktop restarts kill the registry, not the job).
        finished_job = _PCB_JOBS.pop(base, None)
        res = json.loads(result_file.read_text(encoding="utf-8"))
        result_file.replace(pdir(base) / (base + ".pcb_result.delivered.json"))
        _stamp_board_fingerprint(base)
        if finished_job and finished_job.get("setup_changed"):
            res["board_setup_changed"] = True
            res["board_setup_note"] = ("user edited Board Setup since the last "
                                       "build -- this build USED the new values")
        job_route_mode = (finished_job or {}).get("route_mode", route_mode)
        if res.get("status") == "routed":
            res["note"] = ("Board placed + routed + verified (DRC clean, 0 "
                           "unconnected, board-vs-netlist matched). Open with "
                           "build(name, mode='open', view='pcb').")
            # OUTPUT GATE: ask before generating manufacturing files.
            res["next_action"] = "ask_outputs"
            res["output_prompt"] = (
                "Routing + DRC done. ASK THE USER before generating any "
                "manufacturing output: 'Generate BOM and/or Gerbers now?' "
                "Only on their explicit yes: review_design -> (human "
                "approval) -> export_manufacturing. Never auto-generate "
                "BOM/Gerbers.")
        elif res.get("status") == "placed_unrouted" and job_route_mode == "ask":
            # ROUTE GATE: placement is done; the board is intentionally left
            # unrouted. Ask the user before routing (the professional order).
            res["next_action"] = "ask_route"
            res["route_prompt"] = (
                "Placement is done -- parts arranged overlap-free and DRC-"
                "checked. The board is NOT routed yet (unrouted ratsnest is "
                "EXPECTED here, not an error). ASK THE USER: 'Route the board "
                "now?' If the board is OPEN in the app, prefer autoroute_live "
                "so they can WATCH it; otherwise call create_pcb(name, "
                "route='yes') for a headless route. Do not route without "
                "their OK.")
            res["note"] = ("Board PLACED (overlap-free, DRC-checked) and left "
                           "UNROUTED on purpose. Open with build(name, "
                           "mode='open', view='pcb') to view placement; then "
                           "ask the user whether to route.")
        # Surface the final board size to the user. When it was auto-sized (no
        # board_width/board_height given), TELL them the dimensions and that
        # they can pin an exact size -- most real boards must fit an enclosure.
        bd = res.get("board_dimensions")
        if bd:
            res["board_size_summary"] = (
                f"Board is {bd['width_mm']} x {bd['height_mm']} mm "
                f"({bd['area_cm2']} cm2, rectangular) -- {bd['source']}.")
            if bd.get("source", "").startswith("auto"):
                res["board_size_prompt"] = (
                    "This is the TIGHTEST rectangle that fits the parts, not a "
                    "product outline. If it must fit an enclosure, give me the "
                    "exact width x height (mm) -- and mounting-hole positions / "
                    "which edge each connector exits, if any -- then re-run "
                    "initialize_pcb_project(board_width=..., board_height=..., "
                    "mechanical={...}) + create_pcb. NOTE: the engine only "
                    "produces axis-aligned RECTANGULAR outlines (W x H). A "
                    "round/chamfered/angled board is NOT specified by a "
                    "'diagonal' or a rotation angle -- it needs a real Edge.Cuts "
                    "shape, which today you draw in KiCad after generation.")
        ov = (res.get("config_conformance") or {}).get("overridden_user_values")
        if ov:
            res["overridden_user_values_note"] = (
                "The engine OVERRODE user-chosen value(s) to complete "
                "routing -- TELL THE USER explicitly what changed and why: "
                + json.dumps(ov, default=str))
        return res

    # Detached job still running (server restarted, registry gone)?
    # A recent log with no result yet = in progress -- do NOT double-start.
    logf = pdir(base) / (base + ".pcb_build.log")
    if not job and logf.is_file() and (time.time() - logf.stat().st_mtime) < 3600 \
            and "DONE:" not in logf.read_text(encoding="utf-8",
                                              errors="replace")[-200:]:
        return {"ok": True, "status": "pcb_building",
                "elapsed_s": int(time.time() - logf.stat().st_mtime),
                "log_tail": logf.read_text(encoding="utf-8",
                                           errors="replace")[-500:],
                "note": "background PCB job in progress (survived a server "
                        "restart) -- poll again with create_pcb(name)"}

    # No job -> starting one fresh. GATES apply at START time only --
    # polls of a running job must never trip them (the job's own writes
    # change the fingerprint mid-build; measured false-positive).
    stale = _stale_outputs(base)
    if stale:
        return {"ok": False, "status": "stale_source",
                "error": stale}
    setup_changed, _cur = _setup_hash_state(base)

    # KiCad IS TRUTH: read the saved files, diff against the last build's
    # snapshot, and AUTO-ADOPT the user's board-setup-level edits (layers/
    # thickness/size/mask/outline) into the sidecar -- no permission
    # needed, the user already decided by saving in the application. Only
    # destroying their hand-placed LAYOUT still asks (below).
    adopt_code = (
        "import sys, json\n"
        f"sys.path.insert(0, {json.dumps(str(SRC))})\n"
        "from skidl.board.board_setup import adopt_manual_changes\n"
        f"r = adopt_manual_changes({json.dumps(base)}, "
        f"{json.dumps(str(pdir(base)))})\n"
        "print('\\n::RESULT::' + json.dumps({'ok': True, **r}, default=str))\n"
    )
    adopt = _py_json(adopt_code, timeout=60)
    manual_changes = adopt.get("manual_changes") or []
    adopted = adopt.get("adopted") or {}

    manual = _detect_manual_edits(base)
    if manual and not force:
        return {
            "ok": False,
            "status": "manual_edits_detected",
            "error": f"REFUSED to overwrite {base}.anvil_pcb: {manual}",
            "manual_changes": manual_changes,
            "adopted": adopted,
            "instruction": ("The user's board-setup-level edits (layers/"
                            "thickness/size/outline/mask) were ADOPTED "
                            "automatically -- see `adopted`; any "
                            "regeneration will USE them, and a user-drawn "
                            "outline shape is carried verbatim. The one "
                            "remaining question is DESTRUCTIVE: "
                            "regenerating discards their hand-placed "
                            "LAYOUT (positions/tracks/zones). ASK THE "
                            "USER: (a) keep their board as-is and continue "
                            "with run_drc/verify_board/export_manufacturing "
                            "on it; (b) create_pcb with force=True to "
                            "regenerate WITH the adopted values, ONLY "
                            "after they explicitly agree."),
        }

    # REQUIREMENTS GATE: never silently build a board with defaults. The
    # schematic path blocks on precheck_failed; this is the PCB analog.
    if not _requirements_confirmed(base) and not accept_defaults:
        qcode = (
            "import sys, json\n"
            f"sys.path.insert(0, {json.dumps(str(SRC))})\n"
            "from skidl.anvil import anvil_libs\n"
            "from skidl.board.requirements import generate_questionnaire\n"
            f"q = generate_questionnaire({json.dumps(base)}, "
            f"{json.dumps(str(pdir(base)))})\n"
            "print('\\n::RESULT::' + json.dumps(q))\n"
        )
        q = _py_json(qcode, timeout=120)
        if not q.get("ok"):
            # FAIL CLOSED: analyzer broke -> generic questions, never a
            # silent default build.
            q = _generic_requirements_questionnaire(str(q.get("error")))
        q.pop("ok", None)
        return {
            "ok": False,
            "status": "requirements_needed",
            "questionnaire": q,
            "instruction": (
                "ASK THE USER these questions -- do NOT answer them "
                "yourself and do NOT proceed with defaults on your own. "
                "Show the limitations list verbatim. Then submit the "
                "answers via initialize_pcb_project(name, ...): each "
                "question's answer_via names the exact parameter, skipped "
                "questions follow their skip_means, and pass "
                "requirements_asked=[the ids you asked]. Show its "
                "resolved_configuration and ask 'Proceed?', then call "
                "create_pcb again. ONLY if the user explicitly says "
                "defaults are fine: call initialize_pcb_project(name) "
                "bare, or create_pcb(name, accept_defaults=True). This "
                "gate fires on every create_pcb until requirements are "
                "confirmed."),
        }

    result_file.unlink(missing_ok=True)
    (pdir(base) / (base + ".pcb_build.log")).unlink(missing_ok=True)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {json.dumps(str(SRC))})\n"
        "from skidl.anvil import anvil_libs\n"
        "from skidl.board.pcb_job import run_pcb_job\n"
        f"run_pcb_job({json.dumps(base)}, {json.dumps(str(pdir(base)))}, "
        f"{json.dumps(_find_kicad_cli_path())}, "
        f"route={do_route})\n"
    )
    proc = subprocess.Popen(
        [PYEXE, "-X", "utf8", "-c", code],
        cwd=str(pdir(base)), env=_subprocess_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _PCB_JOBS[base] = {"proc": proc, "started": time.time(),
                       "setup_changed": setup_changed,
                       "route_mode": route_mode}
    _stage = ("placement -> invisible routing (generous budget) -> zone "
              "fill -> DRC -> verification" if do_route
              else "placement (overlap-free, size-fit) -> DRC check "
                   "(the board is left UNROUTED on purpose -- you will ASK "
                   "the user before routing)")
    return {"ok": True, "status": "pcb_building", "elapsed_s": 0,
            "route_mode": route_mode,
            "note": (f"PCB build STARTED in the background: {_stage}, with "
                     "automatic placement fallbacks. Poll with "
                     "create_pcb(name) every 1-3 minutes until status is no "
                     "longer 'pcb_building'. Small boards finish in ~1 min; "
                     "a 150-part board can take 10-30 min -- this call will "
                     "NEVER time out again.")}


@server.tool()
@_quiet
def analyze_pcb_environment(name: str = "") -> dict:
    """STEP ZERO for ALL PCB work -- call this FIRST, before
    initialize_pcb_project or create_pcb, and SHOW the user what was
    found. Every user's system and Board Setup is different, so nothing
    is ever assumed: this analyzes THIS machine and reports:
    - the installed application (which app, where, version) and the REAL
      capabilities its own kicad-cli exposes (export formats, DRC flags)
    - libraries (symbol/footprint roots, how many .pretty libs)
    - routing engines (java, freerouting jars)
    - the user's own app-level Board Setup defaults (APPDATA pcbnew.json)
    - when `name` is given, that project's state: net classes and design
      rules the USER already set in .anvil_pro, existing board setup
      (stackup/mask edits), custom .kicad_dru rules, manual-edit status.
    READ-ONLY: changes nothing. Values found here are the user's own --
    later steps keep them and only fill what's missing."""
    base = _safe_name(name) if name else ""
    code = f'''
import sys, json, glob, os
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.anvil import anvil_libs
from skidl.board.rule_discovery import discover_application, discover_rules
from skidl.board.route.freerouting import find_java, find_freerouting_jars

info = {{"ok": True}}
app = discover_application({json.dumps(_find_kicad_cli_path())})
info["application"] = {{k: app.get(k) for k in
    ("kicad_cli", "install_bin", "version", "capabilities", "config_dir")}}
fp_root = os.environ.get("KICAD9_FOOTPRINT_DIR", "")
info["libraries"] = {{
    "symbol_dir": os.environ.get("KICAD9_SYMBOL_DIR"),
    "footprint_dir": fp_root,
    "footprint_libs": len(glob.glob(os.path.join(fp_root, "*.pretty"))) if fp_root else 0,
}}
info["routing"] = {{"java": find_java() or "NOT FOUND",
                    "engines": [Path(j).name for j in find_freerouting_jars()]}}
base = {json.dumps(base)}
if base:
    d = discover_rules(base, Path({json.dumps(str(pdir(base)))}))
    info["project"] = {{
        "user_net_classes": d["project"]["net_classes"],
        "user_design_rules": d["project"]["design_rules"],
        "custom_drc_rules_file": d["project"]["custom_drc_rules"],
        "board_setup": {{k: v for k, v in d["board"].items()
                         if not k.endswith("_text")}},
        "prior_ai_choices": bool(d["sidecar"]),
    }}
info["system_user_defaults"] = discover_rules(base or "_probe_",
    Path({json.dumps(str(pdir(base)))}))["system_user"]
print("\\n::RESULT::" + json.dumps(info))
'''
    res = _py_json(code, timeout=90)
    if res.get("ok") and base:
        manual = _detect_manual_edits(base)
        res.setdefault("project", {})["manual_edits"] = manual or "none"
    if res.get("ok"):
        res["usage_map"] = _usage_map(res)
        res["instruction"] = (
            "LEARN -> USE -> ACT: show the user this usage_map. Every entry "
            "says WHERE the found value is used and HOW it changes the next "
            "steps. Later steps MUST follow these findings -- never a fixed "
            "recipe that ignores what this system has.")
    return res


def _usage_map(res: dict) -> list:
    """finding -> where it is used -> what the next steps will do because
    of it. Built ONLY from what was actually found on this system, so the
    plan that follows is demonstrably derived from the analysis."""
    out = []

    def add(finding, value, used_in, next_step):
        out.append({"finding": finding, "value": value,
                    "used_in": used_in, "next_step": next_step})

    caps = (res.get("application") or {}).get("capabilities") or {}
    if caps:
        add("application.zone_refill", caps.get("zone_refill"),
            "ground-pour fill (kicad-cli --refill-zones inside the DRC gate)",
            "pour WILL be filled automatically" if caps.get("zone_refill")
            else "pour outline written but fill must be done in the editor")
        fmts = [f for f in ("step", "ipc2581", "odb", "ipcd356") if caps.get(f)]
        add("application.export_formats", fmts,
            "export_manufacturing format list",
            f"only these extra formats will be offered: {fmts}")
    routing = res.get("routing") or {}
    if routing:
        have = routing.get("java", "NOT FOUND") != "NOT FOUND" and routing.get("engines")
        add("routing.engines", routing.get("engines"),
            "create_pcb autorouting (invisible FreeRouting subprocess)",
            "boards will be auto-routed" if have else
            "boards will be PLACED ONLY -- user routes manually in the editor")
    libs = res.get("libraries") or {}
    if libs.get("footprint_libs"):
        add("libraries.footprint_libs", libs["footprint_libs"],
            "footprint embedding in the board + assign_footprints validation",
            "footprints resolve against THIS install's libraries only")
    sysd = (res.get("system_user_defaults") or {}).get("defaults") or {}
    add("system_user_defaults", sysd or "none set",
        "rule ladder step 4 (this user's own new-board defaults)",
        "these override the manufacturer profile" if sysd else
        "user never customized app defaults -> manufacturer profile applies")

    proj = res.get("project") or {}
    if proj:
        ncs = proj.get("user_net_classes") or {}
        if ncs:
            add("project.net_classes", {k: v.get("width") for k, v in ncs.items()},
                "router track widths per net class + DRC clearance checks",
                "routing will use exactly these widths -- NOT profile defaults")
        if proj.get("user_design_rules"):
            add("project.design_rules", proj["user_design_rules"],
                "DRC minimums + the manufacturing-export refusal gate",
                "DRC and fab gates check against THESE values")
        add("project.custom_drc_rules_file", proj.get("custom_drc_rules_file"),
            "every kicad-cli DRC run (the .kicad_dru sits beside the board)",
            "custom rules WILL be enforced in DRC" if proj.get("custom_drc_rules_file")
            else "no custom rules file -- class/minimum rules only")
        bs = proj.get("board_setup") or {}
        if bs:
            add("project.board_setup", bs,
                "carried forward VERBATIM into any regenerated board "
                "(stackup/mask/thickness survive)",
                "regeneration preserves the user's Board Setup exactly")
        me = proj.get("manual_edits")
        add("project.manual_edits", me,
            "the create_pcb overwrite-protection gate",
            "board may be regenerated freely" if me == "none" else
            "create_pcb will REFUSE to overwrite -- ask the user first")
    return out


def _setup_hash_state(base: str):
    """(changed, current_hash): whether the configuration differs from what
    the last build consumed (stored in the sidecar). Covers BOTH hashes:
    setup_hash (saved KiCad files -- a user edit in the application) and
    state_hash (files + sidecar -- a pending update_board_setup request).
    Read-only tools report the flag but do NOT update the stored hashes --
    only a rebuild or an explicit update_board_setup 'consumes' them."""
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.board_setup import get_board_setup as _gbs
s = _gbs({json.dumps(base)}, Path({json.dumps(str(pdir(base)))}))
print("\\n::RESULT::" + json.dumps({{"ok": True, "hash": s["setup_hash"],
                                     "state_hash": s["state_hash"]}}))
'''
    res = _py_json(code, timeout=60)
    current = res.get("hash")
    cur_state = res.get("state_hash")
    stored = stored_state = None
    sc = pdir(base) / (base + ".board_config.json")
    if sc.is_file():
        try:
            d = json.loads(sc.read_text(encoding="utf-8"))
            stored = d.get("setup_hash")
            stored_state = d.get("state_hash")
        except Exception:
            pass
    changed = bool(current and stored and current != stored) \
        or bool(cur_state and stored_state and cur_state != stored_state)
    return changed, current


@server.tool()
@_quiet
def get_board_setup(name: str) -> dict:
    """READ the user's CURRENT saved KiCad state -- call this before
    create_pcb/major operations and whenever the user may have edited
    anything in the KiCad application. The SAVED files are the source of
    truth; later steps MUST use these values, never defaults. Returns:
    `raw` (everything found, schema-free), `consumed` (values that drive
    routing/DRC incl. the REAL Edge.Cuts board_outline + drc_severities),
    `sidecar_meta` (board-level fields KiCad has no dialog for --
    manufacturer/ipc_class/currents/holes/pour -- with per-field
    provenance), `edit_status` (fingerprint arbitration: machine-written
    vs USER-edited), `pending_changes` (update_board_setup requests the
    saved board doesn't reflect yet -- applied at the next create_pcb),
    `manual_changes` (field-level diff of what the user changed since the
    last build -- REPORT it; create_pcb auto-adopts it), engine_overrides
    (values the ENGINE changed, e.g. 2->4 layer escalation -- never
    misattribute these to the user), and setup_hash/state_hash. This tool
    only REPORTS -- it never writes."""
    base = _safe_name(name)
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.board_setup import get_board_setup as _gbs
try:
    info = {{"ok": True, **_gbs({json.dumps(base)}, Path({json.dumps(str(pdir(base)))}))}}
    # raw board-file text blocks are bulky -- summarize for the response
    bf = info.get("raw", {{}}).get("board_file", {{}})
    for k in list(bf):
        if k.startswith("_") and k.endswith("_text"):
            bf[k] = f"<{{len(bf[k])}} chars, preserved verbatim>"
except Exception as exc:
    import traceback
    info = {{"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-500:]}}
print("\\n::RESULT::" + json.dumps(info))
'''
    res = _py_json(code, timeout=90)
    if res.get("ok"):
        changed, _cur = _setup_hash_state(base)
        res["board_setup_changed_since_last_build"] = changed
    return res


@server.tool()
@_quiet
def update_board_setup(name: str, stackup: dict = None, constraints: dict = None,
                        net_classes: dict = None, via_rules: dict = None,
                        mask: dict = None, drc_severities: dict = None,
                        board: dict = None) -> dict:
    """APPLY the user's requested Board Setup changes (their explicit
    instruction is authoritative). Only the sections passed are touched;
    everything else -- including settings this pipeline doesn't interpret
    -- stays exactly as-is. Sections: stackup {layers,thickness,copper_oz},
    constraints {min_track_width,min_clearance,...}, net_classes
    {"Power": {"width": 1.0, "clearance": ...}}, via_rules {via_size,
    via_drill}, mask {pad_to_mask_clearance}, drc_severities {rule:
    "error"|"warning"|"ignore"}, board {board_width, board_height,
    mounting_holes, ground_pour, ipc_class, manufacturer,
    currents={"NET": amps}} -- the ONE write surface for board-level
    fields KiCad has no dialog for.
    READ->ACT->VERIFY: the return's `verified` maps EVERY requested field
    to applied | pending_regeneration | failed after re-reading the saved
    files -- REPORT those statuses to the user (never just say 'done');
    pending fields take effect at the next create_pcb re-route."""
    base = _safe_name(name)
    args = {"stackup": stackup, "constraints": constraints,
            "net_classes": net_classes, "via_rules": via_rules,
            "mask": mask, "drc_severities": drc_severities, "board": board}
    if not any(v for v in args.values()):
        return {"ok": False, "error": "nothing to change -- pass at least one section"}
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.board_setup import update_board_setup as _ubs
kw = json.loads({json.dumps(json.dumps(args))})
try:
    info = {{"ok": True, **_ubs({json.dumps(base)}, Path({json.dumps(str(pdir(base)))}),
             **{{k: v for k, v in kw.items() if v}})}}
    bf = info.get("board_setup", {{}}).get("raw", {{}}).get("board_file", {{}})
    for k in list(bf):
        if k.startswith("_") and k.endswith("_text"):
            bf[k] = f"<{{len(bf[k])}} chars>"
except Exception as exc:
    import traceback
    info = {{"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-500:]}}
print("\\n::RESULT::" + json.dumps(info))
'''
    res = _py_json(code, timeout=90)
    if res.get("ok"):
        # NOTE: no fingerprint stamp here -- the engine re-stamps only when
        # the board was machine-generated. Stamping unconditionally would
        # erase the user's manual-edit evidence and let create_pcb
        # regenerate over their layout without asking.
        res["note"] = ("Report the `verified` statuses to the user. Fields "
                       "marked pending_regeneration apply at the next "
                       "create_pcb (run it with the user's consent if the "
                       "board has manual work).")
    return res


@server.tool()
@_quiet
def initialize_pcb_project(name: str, layers: "int | None" = None,
                            board_width: float = None, board_height: float = None,
                            mounting_holes: str = None,
                            manufacturer: str = None,
                            ground_pour: bool = True,
                            ipc_class: int = None,
                            mechanical: dict = None,
                            currents: dict = None,
                            requirements_asked: list = None) -> dict:
    """LEARN-FIRST PCB project setup. Call analyze_pcb_environment FIRST
    (step zero) and show the user what their system already has -- then
    this tool discovers what THIS system and THIS
    user already configured (project .anvil_pro Board Setup + net classes,
    .kicad_dru custom rules, existing board edits, the user's own KiCad
    app defaults under APPDATA, the installed application and its real
    capabilities) and only fills in what's missing from the manufacturer
    profile. Auto-assigns net classes (power rails -> Power/wider tracks,
    USB D+/D- -> USB), writes design rules + classes into .anvil_pro and
    board-level choices into <name>.board_config.json. EVERY setting in
    the returned report carries its source (argument/project-user/
    board-user/system-user/ai/profile) AND the reason it was chosen --
    show the user which values are THEIRS (kept) vs AI-chosen.
    mounting_holes: "M2"|"M2.5"|"M3"|"M4" (only added when requested).
    ipc_class: 1/2/3 -- one parameter derives clearance/drill/annular-ring
    floors + mask expansion (design targets from public IPC-2221B/6012
    figures, NOT a certification). A vehicle/industrial product is
    typically Class 2.
    mechanical: enclosure spec applied BEFORE placement (professional
    order): {"board": {"width": mm, "height": mm, "grow": false},
    "mounting_holes": {"size": "M3", "positions": [[x,y],...]},
    "connectors": [{"ref": "J1", "edge": "left|right|top|bottom",
    "offset": mm}], "keepouts": [{"name": "...", "rect": [x1,y1,x2,y2],
    "no_footprints": true, "reason": "..."}]}. Refs/rects are validated;
    a board too small FAILS honestly instead of growing.
    currents: {"NET": amps} user-declared currents for the
    IPC-2152-informed width plan (advisory estimates otherwise).
    This is also how you SUBMIT the user's answers to create_pcb's
    requirements questionnaire: each question's answer_via names the
    parameter, skipped questions follow their skip_means, and
    requirements_asked=[ids you actually asked] records the audit trail.
    Any successful call marks requirements confirmed and unlocks
    create_pcb. The return includes resolved_configuration (every value
    with source + confirmed/unconfirmed status) and limitations -- SHOW
    both to the user and ask 'Proceed?' BEFORE calling create_pcb;
    unconfirmed values are estimates, say so."""
    base = _safe_name(name)
    if not (pdir(base) / (base + ".net")).is_file():
        return {"ok": False, "error": f"netlist not found -- run build first"}
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.anvil import anvil_libs   # sets KICAD*_FOOTPRINT_DIR -- fine-pitch
                                     # probing loads real footprints
from skidl.board.project_init import initialize_project
try:
    out_dir = Path({json.dumps(str(pdir(base)))})
    scp = out_dir / ({json.dumps(base)} + ".board_config.json")
    currents = {json.dumps(currents) if currents else "None"}
    if currents:
        # User-declared currents go in BEFORE init so THIS run's width
        # plan uses them (init carries them forward from the sidecar).
        # currents_meta source "user" means "arrived via the tool call",
        # NOT verified human input -- never treat "confirmed" as ground
        # truth for safety-relevant logic.
        sc = {{}}
        if scp.is_file():
            try:
                sc = json.loads(scp.read_text(encoding="utf-8"))
            except Exception:
                pass
        sc["currents"] = currents
        meta = sc.get("currents_meta") or {{}}
        for net in currents:
            meta[net] = {{"source": "user", "status": "confirmed"}}
        sc["currents_meta"] = meta
        scp.write_text(json.dumps(sc, indent=2) + "\\n", encoding="utf-8")
    rep = initialize_project({json.dumps(base)}, out_dir,
                             layers={int(layers) if layers is not None else "None"},
                             board_width={board_width if board_width else "None"},
                             board_height={board_height if board_height else "None"},
                             mounting_holes={json.dumps(mounting_holes) if mounting_holes else "None"},
                             manufacturer={json.dumps(manufacturer) if manufacturer else "None"},
                             ground_pour={bool(ground_pour)},
                             ipc_class={int(ipc_class) if ipc_class else "None"},
                             mechanical={json.dumps(mechanical) if mechanical else "None"},
                             requirements_asked={json.dumps(requirements_asked) if requirements_asked else "None"},
                             kicad_cli={json.dumps(_find_kicad_cli_path())})
    if currents:
        rep["currents"] = "user-declared -- applied to the width plan"
    # SOFT CONFIRMATION: one canonical file-based resolution of what the
    # build WILL do, every value with its source + confirmed status.
    from skidl.board.rule_discovery import resolve_board_config
    from skidl.board.requirements import engine_limitations
    info = {{"ok": True, **rep}}
    info["requirements_confirmed"] = True
    info["resolved_configuration"] = resolve_board_config({json.dumps(base)}, out_dir)
    info["limitations"] = engine_limitations()
    info["instruction"] = (
        "Show this resolved_configuration + limitations to the user and "
        "ask 'Proceed?' BEFORE calling create_pcb. Unconfirmed values "
        "are estimates -- say so.")
except Exception as exc:
    import traceback
    info = {{"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-600:]}}
print("\\n::RESULT::" + json.dumps(info))
'''
    return _py_json(code, timeout=120)


@server.tool()
@_quiet
def assign_footprints(name: str, assignments: dict[str, str]) -> dict:
    """Assign real footprints to parts whose netlist entry has none (the
    reason create_pcb reports missing_footprints). assignments maps
    reference -> "Lib:Name" (e.g. {"J1": "Connector_PinHeader_2.54mm:
    PinHeader_1x04_P2.54mm_Vertical"}). Each footprint is VERIFIED to
    exist in the installed libraries before the netlist is patched --
    never invent names; find them with parts(action='search')/parts(action='describe').
    NOTE: also fix the footprint= in the circuit source for future
    rebuilds; this patches the current <name>.net only."""
    base = _safe_name(name)
    net = pdir(base) / (base + ".net")
    if not net.is_file():
        return {"ok": False, "error": f"netlist not found: {net} -- run build first"}
    if not assignments:
        return {"ok": False, "error": "assignments is empty"}
    code = f'''
import sys, json, re
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.anvil import anvil_libs
from skidl.board.footprint_libs import resolve_path, FootprintNotFoundError

net_path = Path({json.dumps(str(net))})
assignments = json.loads({json.dumps(json.dumps(assignments))})
text = net_path.read_text(encoding="utf-8")
applied, errors = {{}}, {{}}
for ref, fp in assignments.items():
    try:
        resolve_path(fp)   # must exist in the installed libraries
    except (FootprintNotFoundError, ValueError) as exc:
        errors[ref] = str(exc)
        continue
    # Patch this comp's (footprint ...) inside its (comp (ref "REF") ...) block.
    pat = re.compile(
        r'(\\(comp\\s*\\n\\s*\\(ref "' + re.escape(ref) + r'"\\)[\\s\\S]*?\\(footprint) "[^"]*"',
    )
    new_text, n = pat.subn(r'\\1 "' + fp + r'"', text, count=1)
    if n == 0:
        errors[ref] = "no (comp (ref ...)) block with a (footprint ...) field found"
    else:
        text = new_text
        applied[ref] = fp
py_patched = []
if applied:
    # THE PYTHON FILE IS THE SOURCE OF TRUTH: patch the Part(...) lines
    # in <base>.py FIRST (then the netlist), so the freshness gate never
    # sees the source newer than the outputs after our own edit.
    py_path = net_path.with_suffix(".py")
    if py_path.is_file():
        from skidl.board.pcb_writer import _match_paren
        src_txt = py_path.read_text(encoding="utf-8")
        for ref, fp in applied.items():
            m = re.search('ref\\\\s*=\\\\s*["\\\']' + re.escape(ref) + '["\\\']', src_txt)
            if not m:
                continue
            start = src_txt.rfind("Part(", 0, m.start())
            if start < 0:
                continue
            try:
                end = _match_paren(src_txt, start + 4)
            except Exception:
                continue
            call = src_txt[start:end]
            new_call, n = re.subn('footprint\\\\s*=\\\\s*["\\\'][^"\\\']*["\\\']',
                                  'footprint="' + fp + '"', call, count=1)
            if n == 0:
                new_call = call[:-1] + ', footprint="' + fp + '")'
            if new_call != call:
                src_txt = src_txt[:start] + new_call + src_txt[end:]
                py_patched.append(ref)
        if py_patched:
            py_path.write_text(src_txt, encoding="utf-8")
    net_path.write_text(text, encoding="utf-8")
info = {{"ok": bool(applied) and not errors, "applied": applied, "errors": errors,
         "source_py_patched": py_patched,
         "note": "netlist AND the .py source patched -- the Python file is the "
                 "single source of truth; rebuilds keep these assignments."}}
print("\\n::RESULT::" + json.dumps(info))
'''
    return _py_json(code, timeout=120)


@server.tool()
@_quiet
def run_drc(name: str) -> dict:
    """Run KiCad's Design Rules Check for <name>. ANSWER-PARITY RULE: if the
    board is OPEN in the running app, the check runs INSIDE the live editor
    (same engine, same user-configured severities, sees unsaved manual edits)
    so the answer is IDENTICAL to the user's manual DRC; otherwise it falls
    back to the installed CLI on the saved .anvil_pcb. The result carries
    'source' ('live_editor' or 'saved_file'). Report EVERY error-severity
    violation to the user verbatim (cosmetic library-art ones included,
    labeled as such) -- never described as passing, never silently hidden."""
    base = _safe_name(name)
    pcb = pdir(base) / (base + ".anvil_pcb")
    if not pcb.is_file():
        return {"ok": False, "error": f"board not found: {pcb} -- run create_pcb first"}
    # LIVE FIRST: the user's manual edits + settings changes live in the open
    # editor; the file only catches up on autosave. Only trust the live answer
    # when the open board IS this project (board_file added by the app).
    live = _live_call("run_drc", {})
    if live.get("ok"):
        lf = live.get("board_file") or ""
        if lf and Path(lf).name != pcb.name:
            live_note = (f"a DIFFERENT board is open in the editor ({Path(lf).name}); "
                         f"checked the saved {pcb.name} file instead")
        else:
            live["source"] = "live_editor"
            live["parity_note"] = ("run inside the open editor with the user's own "
                                   "ERC/DRC settings -- identical to manual DRC")
            if not lf:
                live["project_match_unverified"] = (
                    "app build predates board_file reporting -- could not confirm "
                    "the open board is this project")
            changed, _cur = _setup_hash_state(base)
            if changed:
                live["board_setup_changed"] = True
            return live
    else:
        live_note = None
    res = _board_drc_gate(pcb)
    res["ok"] = bool(res.get("drc_parsed"))
    res["source"] = "saved_file"
    if live_note:
        res["live_note"] = live_note
    changed, _cur = _setup_hash_state(base)
    if changed:
        res["board_setup_changed"] = True
        res["board_setup_note"] = ("Board Setup was edited AFTER this board was "
                                   "built -- re-run create_pcb so the board "
                                   "reflects the new settings")
    if res["ok"]:
        try:
            rep = json.loads(Path(res["drc_report"]).read_text(encoding="utf-8"))
            errs = [v for v in rep.get("violations", []) if v.get("severity") == "error"]
            res["errors"] = [v.get("description") for v in errs][:20]
            res["error_count"] = len(errs)
            res["clean"] = not errs and not rep.get("unconnected_items")
        except Exception:
            pass
    return res


@server.tool()
@_quiet
def verify_board(name: str) -> dict:
    """Board<->schematic consistency proof: extract the as-routed copper
    netlist from <name>.anvil_pcb (IPC-D-356) and compare its pin-partition to
    the intended <name>.net. ok:true = every net groups the same pins on the
    board as in the netlist (no short, no broken connection). ok:false =
    MISMATCH -- the board must NOT be manufactured; report 'missing' (broken
    nets) and 'extra' (shorts) to the user verbatim."""
    base = _safe_name(name)
    if not (pdir(base) / (base + ".anvil_pcb")).is_file():
        return {"ok": False, "error": "board not found -- run create_pcb first"}
    if not (pdir(base) / (base + ".net")).is_file():
        return {"ok": False, "error": "netlist not found -- run build first"}
    gate = _board_verify_gate(base)
    ok = gate.get("board_matches_netlist")
    out = {"ok": bool(ok), "board_matches_netlist": ok}
    if ok is None:
        out["error"] = gate.get("verify_note")
    elif not ok:
        out["mismatch"] = gate.get("verify_mismatch")
        out["error"] = ("MISMATCH: board connectivity differs from the "
                        "netlist -- do not manufacture")
    # CONFIG CONFORMANCE: connectivity alone says nothing about layers/
    # size/holes/widths matching what the user configured -- check it.
    conf_code = (
        "import sys, json\n"
        f"sys.path.insert(0, {json.dumps(str(SRC))})\n"
        "from skidl.board.conformance import check_config_conformance\n"
        f"c = check_config_conformance({json.dumps(base)}, "
        f"{json.dumps(str(pdir(base)))})\n"
        "print('\\n::RESULT::' + json.dumps({'ok': True, 'conformance': c}, "
        "default=str))\n"
    )
    conf_res = _py_json(conf_code, timeout=60)
    conf = conf_res.get("conformance")
    if conf:
        out["config_conformance"] = conf
        if conf.get("ok") is False:
            out["ok"] = False
            out["error"] = ((out.get("error") or "") + " CONFIG MISMATCH: "
                            "built board does not match the configured "
                            f"setup ({', '.join(conf.get('mismatches', []))})"
                            ).strip()
    return out


@server.tool()
@_quiet
def review_design(name: str) -> dict:
    """PHASE-6 DESIGN REVIEW -- run after verification, BEFORE any talk of
    manufacturing. Synthesizes every gate (ERC, DRC vs the user's Board
    Setup, board-vs-netlist, BOM) PLUS design-INTENT checks measured on
    the real board (decoupling-cap distance in mm, track width vs
    IPC-2221-style current ESTIMATE, connectors on edge, ground pour)
    into one report, written to <name>_review.md. SHOW the user the
    report summary and path VERBATIM -- never paraphrase failures away.
    The report ends with the honesty block: EMI/SI/PI/thermal are NOT
    verified. Manufacturing export requires the human to read this and
    then approve_design."""
    base = _safe_name(name)
    if not (pdir(base) / (base + ".anvil_pcb")).is_file():
        return {"ok": False, "error": "no board -- run create_pcb first"}
    stale = _stale_outputs(base)
    if stale:
        return {"ok": False, "status": "stale_source", "error": stale}
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.anvil import anvil_libs
from skidl.board.review import review_design as _rd
try:
    info = {{"ok": True, **_rd({json.dumps(base)}, Path({json.dumps(str(pdir(base)))}),
                               {json.dumps(_find_kicad_cli_path())})}}
except Exception as exc:
    import traceback
    info = {{"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-600:]}}
print("\\n::RESULT::" + json.dumps(info, default=str))
'''
    return _py_json(code, timeout=300)


@server.tool()
@_quiet
def approve_design(name: str, approved_by: str, note: str = "") -> dict:
    """Record the HUMAN's manufacturing approval -- hash-bound to the
    exact board + Board Setup reviewed. HARD RULES: (1) call this ONLY
    after the human has SEEN the review_design report and EXPLICITLY
    said they approve, in their own words; (2) the AI must NEVER invent
    approved_by or approve on its own initiative -- approved_by is the
    name the human states; (3) gates are re-run fresh at approval time;
    a failing design cannot be approved. Any board or Board Setup change
    afterwards INVALIDATES the approval automatically."""
    base = _safe_name(name)
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.anvil import anvil_libs
from skidl.board.review import approve_design as _ad
try:
    info = _ad({json.dumps(base)}, Path({json.dumps(str(pdir(base)))}),
               {json.dumps(approved_by)}, note={json.dumps(note)},
               kicad_cli={json.dumps(_find_kicad_cli_path())})
except Exception as exc:
    import traceback
    info = {{"ok": False, "error": repr(exc), "trace": traceback.format_exc()[-600:]}}
print("\\n::RESULT::" + json.dumps(info, default=str))
'''
    return _py_json(code, timeout=300)


def _check_approval_gate(base: str) -> dict:
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.review import check_approval
print("\\n::RESULT::" + json.dumps(
    {{"ok": True, **check_approval({json.dumps(base)}, Path({json.dumps(str(pdir(base)))}))}}))
'''
    return _py_json(code, timeout=60)


@server.tool()
@_quiet
def export_manufacturing(name: str, formats: list[str] = None) -> dict:
    """Export manufacturing files for <name>.anvil_pcb: Gerbers + drill +
    pick-and-place by default (optionally 'pdf', 'step'), zipped into
    <name>_fab.zip a fab house accepts. REFUSES to export a board that fails
    DRC (error-severity) or the board<->netlist check -- manufacturing a
    known-bad board is never acceptable; report what failed instead."""
    base = _safe_name(name)
    pcb = pdir(base) / (base + ".anvil_pcb")
    if not pcb.is_file():
        return {"ok": False, "error": f"board not found: {pcb} -- run create_pcb first"}

    # PHASE-6 HARD GATE: manufacturing NEVER proceeds without a valid,
    # hash-bound HUMAN approval (original plan, principle #7).
    appr = _check_approval_gate(base)
    if not appr.get("valid"):
        return {"ok": False,
                "status": "approval_required",
                "error": f"REFUSED: {appr.get('reason', 'no valid approval')}",
                "instruction": ("Flow: review_design(name) -> SHOW the human "
                                "the report -> the human explicitly approves "
                                "-> approve_design(name, approved_by=<their "
                                "name>) -> export_manufacturing. Never "
                                "approve on the user's behalf.")}
    approved_by = appr.get("approved_by")

    # Hard gates before anything leaves for a fab. Library-ART defects
    # (e.g. malformed_courtyard: an unclosed courtyard polygon shipped in
    # the footprint library itself) are exempt -- courtyard layers never
    # appear in fab Gerbers, so they cannot affect the manufactured
    # board. They are still REPORTED, never hidden.
    _LIBRARY_ART_ERRORS = {"malformed_courtyard", "lib_footprint_issues"}
    drc = _board_drc_gate(pcb)
    if not drc.get("drc_parsed"):
        return {"ok": False, "error": "DRC could not run", **drc}
    try:
        rep = json.loads(Path(drc["drc_report"]).read_text(encoding="utf-8"))
        errs = [v for v in rep.get("violations", []) if v.get("severity") == "error"]
        blocking = [v for v in errs if v.get("type") not in _LIBRARY_ART_ERRORS]
        art_only = [v for v in errs if v.get("type") in _LIBRARY_ART_ERRORS]
        if blocking or rep.get("unconnected_items"):
            return {"ok": False,
                    "error": (f"REFUSED: board has {len(blocking)} DRC error(s) and "
                              f"{len(rep.get('unconnected_items', []))} unconnected "
                              "item(s) -- fix and re-run create_pcb first"),
                    "drc_errors": [v.get("description") for v in blocking][:10]}
    except Exception as exc:
        return {"ok": False, "error": f"DRC report unreadable: {exc!r}"}
    gate = _board_verify_gate(base)
    if gate.get("board_matches_netlist") is False:
        return {"ok": False,
                "error": "REFUSED: board does not match the netlist (short or "
                         "broken connection) -- fix and re-run create_pcb",
                "mismatch": gate.get("verify_mismatch")}

    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.manufacture import export_manufacturing as _em
out = Path({json.dumps(str(pdir(base)))})
info = _em(out / ({json.dumps(base)} + ".anvil_pcb"),
           {json.dumps(_find_kicad_cli_path())},
           formats={json.dumps(formats) if formats else "None"})
print("\\n::RESULT::" + json.dumps(info))
'''
    res = _py_json(code, timeout=420)
    if res.get("ok"):
        res["approved_by"] = approved_by
        res["note"] = ("Exported under the human approval of "
                       f"{approved_by!r}. Gerber/drill/pos files zipped; DRC + "
                       "board-vs-netlist gates passed. NOT verified: EMI, "
                       "signal/power integrity, thermal -- state this "
                       "honestly if the user asks about production readiness.")
        if art_only:
            res["library_art_defects"] = [v.get("description") for v in art_only]
            res["note"] += (" NOTE: footprint-library art defects present "
                            "(courtyard drawing errors in the library itself) -- "
                            "cosmetic only, cannot affect the manufactured board.")
    return res


@server.tool()
@_quiet
def package_project(name: str) -> dict:
    """Bundle the complete project into <name>_project.zip: schematic,
    netlist, board, BOM, DRC report and the manufacturing zip -- everything a
    reviewer or fab needs in one file. Run export_manufacturing first."""
    base = _safe_name(name)
    exts = [".anvil_pro", ".anvil_sch", ".anvil_pcb", ".net",
            ".drc.json", "_fab.zip",
            "_review.md", ".approval.json"]
    import zipfile
    files = []
    for ext in exts:
        p = pdir(base) / (base + ext)
        if p.is_file():
            files.append(p)
    # BOM: the single ready-to-order CSV (app export target, was <base>_bom.csv)
    bom = _bom_csv_path(base)
    if bom.is_file():
        files.append(bom)
    # child schematic sheets (<base>_*.anvil_sch)
    files += [p for p in pdir(base).glob(base + "_*.anvil_sch") if p.is_file()]
    if not any(f.suffix == ".anvil_pcb" for f in files):
        return {"ok": False, "error": "no board found -- run create_pcb first"}
    zip_path = pdir(base) / (base + "_project.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(set(files)):
            zf.write(f, f.name)
    return {"ok": True, "zip": str(zip_path),
            "files": sorted({f.name for f in files}),
            "note": "hand this zip to the human reviewer; manufacturing must "
                    "never proceed without their approval"}


_SKILL_MD = REPO / ".claude" / "skills" / "skidl-circuit" / "SKILL.md"

# The canonical circuit BODY pattern for build(mode='body') -- formerly the
# get_template() tool; now embedded in the design-rules text.
_TEMPLATE = (
    '# ============ HOW THE SHEET IS LAID OUT (you just write the circuit; the\n'
    '# builder picks the layout automatically from its SIZE) ============\n'
    '#   small / one function       -> ONE sheet, mostly WIRES.\n'
    '#   medium, several functions  -> ONE sheet with boxed BLOCKS.\n'
    '#   BIG (>~50 parts) with       -> HIERARCHY: one child SHEET per @subcircuit\n'
    '#   @subcircuit pages             page; each page laid out with boxed blocks.\n'
    '# You only decide the STRUCTURE (blocks + pages); the builder decides sheets.\n\n'
    '# 1) NETS -- name POWER/GROUND canonically so they render as POWER SYMBOLS,\n'
    '#    not repeated text labels:  +5V  +3V3  +12V  VBAT  GND  (NOT "5V_IN", "NET_GND").\n'
    'p5v = Net("+5V")\n'
    'gnd = Net("GND")\n\n'
    '# 2) GROUP parts into FUNCTIONAL BLOCKS -- THIS is what makes a NEAT schematic.\n'
    '#    One `with smart_schematic.block("<function>"):` per functional group; put\n'
    '#    every part of that function inside it. Derive the names DYNAMICALLY from the\n'
    '#    circuit function (Power In / Buck / LDO / Charger / MCU / Comms / LED ...) --\n'
    '#    never a hardcoded list. The builder draws a labelled box around each block.\n'
    'with smart_schematic.block("LED INDICATOR"):\n'
    '    r = Part("Device", "R", ref="R1", tag="R1", value="330",\n'
    '             footprint="Resistor_SMD:R_0805_2012Metric")\n'
    '    led = Part("Device", "LED", ref="D1", tag="D1",\n'
    '               footprint="LED_SMD:LED_0805_2012Metric")\n'
    '    # 3) CONNECT -- series:  p5v & r & led & gnd   (or pin-by-pin:\n'
    '    #    r[1] += p5v ; r[2] += led[1] ; led[2] += gnd)\n'
    '    p5v & r & led & gnd\n\n'
    '# WIRE vs LABEL -- do NOT try to wire everything. The builder WIRES only the\n'
    '# short local links (a main IC to its 2-3 immediately-adjacent parts: a\n'
    '# decoupling cap, a pull-up, a crystal) and LABELS everything else (rails, and\n'
    '# any net crossing to another block/page). Fewer wires = fewer labels, nothing\n'
    '# fails to route, and the sheet stays neat. You get this automatically just by\n'
    '# grouping into blocks and naming power nets -- you never set wire-vs-label.\n\n'
    '# 4) BIG DESIGN ONLY -- wrap each MAJOR section in an @subcircuit PAGE so it\n'
    '#    becomes its OWN sheet; keep block()s INSIDE each page for sub-sections:\n'
    '#        @subcircuit\n'
    '#        def power_stage():\n'
    '#            with smart_schematic.block("INPUT FILTER"):\n'
    '#                ...\n'
    '#            with smart_schematic.block("BULK CAPS"):\n'
    '#                ...\n'
    '#        power_stage(); mcu_stage(); comms_stage()\n'
    '#    Small/medium circuits need NO @subcircuit -- just blocks on one sheet.\n'
    '# 5) UNUSED PINS -- mark with NC so the schematic draws no-connect crosses:\n'
    '#    u1[4,6,8] += NC\n'
)

_MCP_ADDENDUM = (
    "\n\n"
    "==================== MCP TOOL MAPPING (overrides where they differ) ====\n"
    "You are using this skill THROUGH the anvilcad MCP tools -- map steps so:\n"
    "* part search / availability -> parts(action='search') (ONE call with the FULL part\n"
    "  list; each item 'Lib:Name' or a search term). NOT any CLI. Returns per\n"
    "  query: exact / exact_part / matches / total_matches. A bare name is\n"
    "  exact ONLY if a part with that EXACT name exists -- e.g. 'NE555' gives\n"
    "  exact=False with candidates Timer:NE555D / Timer:NE555P: show the\n"
    "  candidates and ASK the user which to use, never pick one silently.\n"
    "  AUTO-BROADEN: parts(action='search') already retries broader forms of an ordering\n"
    "  code (ATmega328P-PU -> ATmega328P family, LM317T -> LM317); when it does\n"
    "  it sets 'broadened_query' + 'note'. So 'exact=False' with a non-empty\n"
    "  'matches' list = the part IS available (pick the suffix variant), NOT\n"
    "  missing. Only total_matches:0 means truly absent.\n"
    "* PACKAGE-SUFFIX RULE: KiCad symbols carry package suffixes (-A = TQFP,\n"
    "  -M = QFN), NOT ordering codes (-AU, -MU). If exact=False but a\n"
    "  candidate is the SAME part with a different suffix (ATmega32U2-AU ->\n"
    "  ATmega32U2-A), that IS the part -- use it. Never declare a part\n"
    "  missing while such a candidate exists.\n"
    "* PLACEHOLDER BAN: NEVER map an IC onto a generic connector or any\n"
    "  placeholder symbol with a pin table from memory or read off an image\n"
    "  -- memorized pinouts miswire boards, and no precheck can catch it\n"
    "  (a generic symbol makes every pin electrically legal). If a part is\n"
    "  GENUINELY missing after the suffix check: report it; with the user's\n"
    "  OK, parts(action='add') with pins typed from the DATASHEET, then\n"
    "  parts(action='describe') and wire from its JSON.\n"
    "* pins / datasheet / description -> parts(action='describe') (ONE call with the\n"
    "  chosen Lib:Name list).\n"
    "* build -> build(name, code), mode='body' PREFERRED: code is ONLY the\n"
    "  circuit body (nets, parts, connections -- imports/build call are added\n"
    "  automatically). mode='script' ONLY for hierarchical @subcircuit\n"
    "  designs: a COMPLETE script (your own imports + smart_schematic.build());\n"
    "  script mode SKIPS the electrical precheck + fix_suggestions cycle.\n"
    "  build returns 'building' IMMEDIATELY; poll build(name, mode='status') until\n"
    "  'done' (each call waits ~20 s; minutes-long routing is NORMAL, not an\n"
    "  error). build(mode='status')(name, include_log=True) adds the build-log tail\n"
    "  (failed builds always include it).\n"
    "* env sanity (wrong skidl / no symbol dir) -> diagnostics().\n"
    "* this MCP's builder DOES produce a .anvil_pro project (smart_schematic),\n"
    "  unlike plain SKiDL described in the skill text.\n"
    "* EVERY pin of every part must be either CONNECTED or explicitly marked\n"
    "  no-connect in the body (u1[4,6,8] += NC). There is NO auto-NC: a pin\n"
    "  you leave floating FAILS the precheck (floating_pins lists each one).\n"
    "  Decide per pin from the DATASHEET: wire it if the design needs it,\n"
    "  += NC only if it is genuinely unused. Never blanket-NC to silence the\n"
    "  gate -- that recreates the exact class of shipped-floating-pin bugs\n"
    "  this gate exists to stop.\n"
    "* build(name, mode='open') opens the finished project in the Anvil CAD app\n"
    "  (install auto-detected; it will NOT relaunch if the app is already\n"
    "  open). There is NO tool to read generated file contents.\n"
    "* permission: ask the user ONLY about PARTS, as ONE consolidated report\n"
    "  (available / missing + alternatives + add-as-new-part option). NEVER\n"
    "  call parts(action='add') without the user's explicit OK. If every\n"
    "  part exists exactly -> build directly, no questions.\n"
    "* FIRST-TIME-CORRECT .py (write it right the FIRST time -- no retry\n"
    "  cycles): NEVER write a Part() or a pin connection from memory.\n"
    "  BEFORE writing the body, in this order:\n"
    "    (1) parts(action='search') ONCE with the full parts list;\n"
    "    (2) parts(action='describe') ONCE with the chosen Lib:Name list -> the machine-\n"
    "        readable pin table (num/name/func) for EVERY part. Write ALL\n"
    "        connections ONLY from that JSON, copying pin names VERBATIM\n"
    "        ('VDD_1' is not 'VDD').\n"
    "    (3) values from the datasheet (internally).\n"
    "* CANONICAL CIRCUIT BODY (mode='body') -- follow this pattern:\n"
    + _TEMPLATE +
    "* IF precheck fails: the response carries fix_suggestions -- per wrong\n"
    "  pin the part's REAL pin list + closest matches, per missing part the\n"
    "  library candidates. Apply those EXACT strings and rebuild ONCE. Never\n"
    "  guess a second time.\n"
    "* IF build(mode='status') ends 'failed' with 'connectivity MISMATCH': the\n"
    "  .anvil_sch is WRONG (shorts/missing nets) -- tell the user NOT to open\n"
    "  it; the .net netlist is still valid for PCB layout. NEVER present that\n"
    "  build as a success. schematic_mode='labels' with status 'done' is FINE\n"
    "  (labels connect by name -- normal practice for dense circuits).\n"
    "* SUCCESS GATE (GEN-3, spec section 10): a build is SUCCESSFUL only when\n"
    "  build(mode='status') returns status:'done' AND 'generated' lists a .anvil_sch\n"
    "  (plus .anvil_pro). Until then it is 'building' or INCOMPLETE. NEVER say\n"
    "  'schematic generated' / 'verified' / 'Build Successful' from ERC + netlist\n"
    "  alone -- ERC-clean + .net is only the pre-check. If you cannot confirm the\n"
    "  .anvil_sch file exists, the build is NOT done; keep polling build(mode='status').\n"
    "* DENSE FLAT SHEETS FAIL TO RENDER (spec section 10.6): a flat mode='body'\n"
    "  design above ~12 parts can pass ERC + netlist yet FAIL to publish a\n"
    "  schematic -- on one dense sheet the renderer geometrically shorts nets\n"
    "  (observed: GND+3V3+SCL fused) and refuses to publish. For any design\n"
    "  above ~12 parts, use mode='script' with functional @subcircuit blocks\n"
    "  (power / MCU / clock / reset / comms / I/O / test-points), passing shared\n"
    "  nets as arguments. Decomposition de-densifies each region and lets it\n"
    "  route with wires. A clean pre-check does NOT guarantee the schematic\n"
    "  renders -- only status:'done' with a .anvil_sch + verified:true does.\n"
    "  WHILE writing: nets first; every Part() gets ref= tag= value=\n"
    "  footprint=; connections pin-by-pin from the verified pinouts.\n"
    "  BEFORE calling build, SELF-AUDIT the body line by line:\n"
    "    - every pin you reference exists in the parts(action='describe') output;\n"
    "    - every net touches >= 2 pins; refs are unique;\n"
    "    - no POWER-OUT pin tied to another POWER-OUT (use a decoupling cap\n"
    "      node instead); no @subcircuit called in a loop.\n"
    "  Fix on paper first; only then build ONCE.\n"
    "* DEEP-VERIFY the returned map: build pre-checks the body\n"
    "  (syntax, real parts/pins, ERC) in seconds and returns 'parts' (ref=\n"
    "  value) + 'nets' (net -> exact pins). COMPARE EVERY net and value\n"
    "  against your intended design -- this is the ground truth of what the\n"
    "  .py actually says. If ANY net/value is wrong, fix the body and call\n"
    "  build again immediately. If status is 'precheck_failed', the\n"
    "  exact errors are in the response -- fixing + retrying costs seconds.\n"
    "* HIERARCHY = real engineering decomposition (think like a circuit\n"
    "  designer, not a transcriber). BEFORE writing code, sketch the block\n"
    "  diagram mentally: which SUBSYSTEMS exist (power entry/regulation, MCU\n"
    "  core, analog front-end, sensing, comms/interface, drivers/actuation,\n"
    "  protection, connectors) and which nets flow between them. Then:\n"
    "    - one GENUINELY DISTINCT subsystem = one @subcircuit (tag= it); when\n"
    "      the design is big each becomes its own sheet; interface nets are\n"
    "      passed as arguments (they become the sheet's ports).\n"
    "    - repeated identical channels/units (cell taps, LED arrays, per-\n"
    "      channel filters) = plain UNdecorated helper functions inside their\n"
    "      parent block -- pages must never explode with copies.\n"
    "    - the top sheet must read like the block diagram; depth <= 2 levels.\n"
    "    - small designs (<=50 parts) stay on ONE sheet automatically -- still\n"
    "      group with @subcircuit so each subsystem gets a labeled box.\n"
    "  Split by FUNCTION, never by part-type or arbitrary count.\n"
    "* NO SCOPE QUESTIONS: never ask 'how much should I build', which sheet,\n"
    "  which variant, or any option list. If the user gave a reference\n"
    "  image/document/multi-sheet design -> build the FULL design exactly as\n"
    "  drawn. Reduce scope only if the user themselves said so in the prompt.\n"
    "  The parts report above is the ONLY question you may ever ask.\n"
    "* PRE-CHECK vs ERC: the build pre-check's ERC warnings are harmless;\n"
    "  rebuild ONLY for a real pre-check ERROR. ONE build. But the pre-check\n"
    "  is NOT 'the ERC result' and must never be reported as one.\n"
    "* ANSWER PARITY (MANDATORY): when the user asks for ERC, DRC, or 'any\n"
    "  errors?', the answer MUST come from the app's real engine --\n"
    "  check_live('erc'|'drc', name) (live editor: user's own severities,\n"
    "  pin matrix, exclusions, unsaved edits) or run_drc(name) (live-first,\n"
    "  saved-file fallback). NEVER answer from the skidl pre-check, from\n"
    "  memory, or from a previous result. Report every violation the app\n"
    "  would show, verbatim -- cosmetic ones labeled cosmetic, never hidden.\n"
    "  The AI's answer and the user's manual Run ERC/DRC must MATCH.\n"
    "* FIX ERRORS = DEEP ANALYSIS, NOT A BLIND REBUILD. When the user asks to\n"
    "  fix an ERC/DRC error, work like a circuit designer, one error at a time:\n"
    "    1) READ the real design first: read_live('schematic') (symbols + pins +\n"
    "       the 'nets' map = true connectivity) or read_live('board'); and\n"
    "       check_live to get the EXACT violations with their positions.\n"
    "    2) DIAGNOSE the ROOT CAUSE per violation -- trace the offending net/pin\n"
    "       through the 'nets' map, compare against the part's real datasheet\n"
    "       pinout (parts(action='describe')) and the design intent. State WHY\n"
    "       it is wrong (e.g. power pin left floating, two outputs shorted, a\n"
    "       missing decoupling return, a mislabeled net) before touching it.\n"
    "    3) FIX SURGICALLY on the OPEN design: edit_schematic_live /\n"
    "       edit_board_live (undoable, preserve layout/wiring/flags). NEVER\n"
    "       rebuild the whole schematic to fix one error -- a rebuild REDRAWS\n"
    "       everything and destroys the user's work (build blocks this anyway).\n"
    "    4) RE-CHECK with check_live and confirm THAT violation is gone and no\n"
    "       new one appeared; loop until clean. Report each fix + why.\n"
    "  A warning the user configured to 'ignore' is already hidden -- do not\n"
    "  'fix' it. Fix real errors; ask before changing intended design choices.\n"
    "* LOOK BEFORE ACT (MANDATORY step 0): on EVERY turn about an existing\n"
    "  design, FIRST call get_app_state(); if the app is open, ALSO\n"
    "  read_live() before reasoning -- the user may have manually edited the\n"
    "  schematic/board and the live editor is the truth, not your last build\n"
    "  and not the disk file. If get_app_state says owner is 'user'/manual\n"
    "  (modify_manual/adopt_manual), NEVER build over it -- adopt_project\n"
    "  first, or edit via edit_schematic_live/edit_board_live.\n"
    "  ENFORCED IN CODE: build() REFUSES (status 'build_blocked') over a\n"
    "  user-owned sheet; adopt_project grants a ONE-SHOT consent for the\n"
    "  next build, and every build backs up outputs to *.prebuild.bak.\n"
    "  PREFER surgical edits (edit_schematic / edit_schematic_live) over\n"
    "  adopt+rebuild: a rebuild REDRAWS the sheet -- the user's positions,\n"
    "  wiring, notes and DNP/BOM flags cannot survive it. Rebuild only when\n"
    "  the change is structural and the user was told what will be lost.\n"
    "* POST-BUILD ORDER (fixed): (1) when build(mode='status') is 'done', call\n"
    "  build(name, mode='open') ONCE -- it will not relaunch if the app is\n"
    "  already open; (2) give the brief build report; (3) then ask the user\n"
    "  ONE question: 'Do you want the BOM?' -- yes -> build(name, mode='bom').\n"
    "  Never generate it unasked; if the user says no, stop.\n"
    "* keep the report brief: what it does + final values. No math dumps.\n"
)


@_quiet
def get_design_rules() -> str:
    """The MANDATORY Anvil CAD design workflow (loaded from the repo's
    skidl-circuit skill + MCP tool mapping). Read and follow this before building
    any circuit through this MCP."""
    if _SKILL_MD.is_file():
        txt = _SKILL_MD.read_text(encoding="utf-8", errors="replace")
        # strip YAML frontmatter (skill metadata, not for the model)
        if txt.startswith("---"):
            end = txt.find("\n---", 3)
            if end != -1:
                txt = txt[end + 4:]
        return txt.strip() + _MCP_ADDENDUM
    # fallback: compact built-in rules if the skill file is missing
    return (
        "ANVIL CAD DESIGN WORKFLOW -- be FAST and user-friendly: aim for ONE\n"
        "build, ONE app-open. Scale the same steps for simple OR complex circuits.\n"
        "\n"
        "1. ANALYZE  - list the parts + the values needed.\n"
        "2. LIBRARY  - call parts(action='search') ONCE with the full list of parts, then\n"
        "              parts(action='describe') ONCE with the chosen Lib:Name list -- write ALL\n"
        "              connections verbatim from its pins JSON ('VDD_1' is not 'VDD').\n"
        "              KiCad symbols carry package suffixes (-A TQFP, -M QFN), not\n"
        "              ordering codes (-AU/-MU) -- a same-name candidate with a\n"
        "              different suffix IS the part. NEVER wire an IC through a\n"
        "              generic placeholder symbol with pins from memory.\n"
        "3. VALUES   - compute each value from the part DATASHEET (parts(action='describe') gives\n"
        "              the link). Verify internally. Do NOT show the math -- put only\n"
        "              the final value in the part.\n"
        "4. PERMISSION - the ONLY thing you may ask the user about is PARTS, and it\n"
        "              must be ONE single consolidated report (never one question per\n"
        "              part, never spread over multiple messages):\n"
        "                  AVAILABLE: <all found parts, one line each>\n"
        "                  MISSING:   <each missing part> -> closest alternatives from\n"
        "                             its candidates + the option 'add it as a new\n"
        "                             library part (pins typed from the datasheet)'\n"
        "              Then ask ONCE: 'use these alternatives, or add the new part(s)?'\n"
        "              and WAIT. NEVER call parts(action='add') without the user's\n"
        "              explicit OK, and NEVER substitute a different part silently.\n"
        "              If EVERY part exists exactly, DO NOT ask anything -- go straight\n"
        "              to build. There is NO separate 'may I build?' permission.\n"
        "              NO SCOPE QUESTIONS either: never ask 'how much should I\n"
        "              build' / which sheet / which variant. A reference image or\n"
        "              document means: build the FULL design exactly as drawn,\n"
        "              unless the user's own prompt limited it.\n"
        "5. BUILD    - write the circuit with correct values and call build(name,\n"
        "              code) exactly ONCE (mode='body' preferred; mode='script' only\n"
        "              for hierarchical @subcircuit scripts -- it skips the\n"
        "              electrical precheck). EVERY pin must be connected or\n"
        "              explicitly NC'd in the body (u1[4,6,8] += NC) -- floating\n"
        "              pins FAIL the precheck (floating_pins lists each; decide\n"
        "              per pin from the datasheet, never blanket-NC to pass).\n"
        "              It returns IMMEDIATELY with status 'building'\n"
        "              (the build runs in the background). Then call\n"
        "              build(name, mode='status') repeatedly until status is 'done' -- each\n"
        "              call waits up to ~20 s. Big circuits can take 2-4 minutes;\n"
        "              'building' is NORMAL, never a timeout or an error. Tell the\n"
        "              user it is routing and keep polling.\n"
        "6. WARNINGS - the build succeeds when ERC has 0 ERRORS. ERC WARNINGS are\n"
        "              normal and HARMLESS -- especially 'no dedicated power-source\n"
        "              symbol' (power comes via a passive connector) and 'unconnected'\n"
        "              optional pins. DO NOT add PWR_FLAG. DO NOT rebuild to clear\n"
        "              warnings. Rebuild ONLY for a real ERC ERROR (e.g. a pin\n"
        "              conflict) -- fix that one thing and build once more.\n"
        "              This pre-check is NOT the app's ERC: when the user asks\n"
        "              for ERC/DRC results, answer ONLY from check_live/run_drc\n"
        "              (real engine, user's own settings) so the AI answer and\n"
        "              the user's manual check are IDENTICAL.\n"
        "7. OPEN     - call build(name, mode='open') exactly ONCE, at the very end.\n"
        "              If the app is already open it will NOT relaunch (one window\n"
        "              is enough) -- just tell the user the file path to open.\n"
        "8. REPORT   - brief summary: what it does + final values. Do not dump the\n"
        "              calculations. Then ASK ONE question: 'Do you want the BOM?'\n"
        "              and WAIT.\n"
        "9. FOLLOW-UP - BOM -> build(name, mode='bom') ('missing' MUST be empty -- no\n"
        "              part left out). Nothing unasked; user says no -> stop.\n"
        "\n"
        "CANONICAL CIRCUIT BODY for build(mode='body') -- write ONLY nets, parts\n"
        "and connections (imports / set_default_tool / build are added\n"
        "automatically):\n\n"
        + _TEMPLATE
    )


# ======================================================================================
# LIVE EDITOR TOOLS — drive the OPEN Anvil CAD editors through the app's
# ANVIL_AI_TOOL_SERVER (line-delimited JSON on 127.0.0.1:5571; always started by the
# Anvil app for its built-in chat; the "AnvilCAD MCP" menu is only for an external client).
# 4 consolidated tools, not one-per-op: a user request holding
# several changes travels as ONE call with an ops ARRAY, executed in order with per-op
# results. Everything below is a thin relay — the actual editing runs inside the app.
# ======================================================================================

_LIVE_HOST = "127.0.0.1"
_LIVE_PORT = int(os.environ.get("ANVIL_MCP_PORT", "5571"))

# op name -> which editor executes it (must match the app's AnvilIsBoardTool split)
_LIVE_SCH_OPS = {
    "add_component", "add_wire", "add_label", "add_junction", "add_no_connect",
    "edit_value", "move_component", "delete_component", "delete_at", "snap_to_grid",
    "annotate",
}
_LIVE_PCB_OPS = {
    "add_footprint", "move_footprint", "add_track", "add_via", "delete_track_at",
    "set_text_variable", "capture_footprints",
}


def _live_call(tool: str, input_: dict, timeout: float = 60.0) -> dict:
    """One request line to the app's tool server; one reply line back."""
    import socket

    try:
        with socket.create_connection( ( _LIVE_HOST, _LIVE_PORT ), timeout=timeout ) as s:
            s.sendall( ( json.dumps( { "tool": tool, "input": input_ or {} } ) + "\n" )
                       .encode( "utf-8" ) )
            s.settimeout( timeout )
            buf = b""
            while not buf.endswith( b"\n" ):
                chunk = s.recv( 65536 )
                if not chunk:
                    break
                buf += chunk
        return json.loads( buf.decode( "utf-8", "replace" ).strip() or "{}" )
    except ( ConnectionRefusedError, TimeoutError, OSError ) as exc:
        if os.environ.get( "ANVIL_IN_APP_CHAT" ) == "1":
            guidance = ( "Open the required Schematic or PCB Editor and retry. "
                         "The in-app chat starts its local tool channel automatically." )
        else:
            guidance = ( "Open Anvil CAD and click AnvilCAD MCP -> Start in the app "
                         "menu, then retry." )

        return { "ok": False, "not_reachable": True,
                 "message": f"Anvil CAD tool server not reachable on "
                            f"{_LIVE_HOST}:{_LIVE_PORT} ({exc.__class__.__name__}). "
                            + guidance }
    except Exception as exc:                      # noqa: BLE001 — surface, never raise
        return { "ok": False, "message": f"live tool error: {exc!r}" }


def _live_run_ops(ops, allowed: set, side: str) -> dict:
    """Run an ordered ops array against the app; aggregate per-op results honestly."""
    if not isinstance( ops, list ) or not ops:
        return { "ok": False, "results": [],
                 "message": "ops must be a non-empty list of {\"op\": name, ...params}" }

    results, all_ok = [], True

    for i, op in enumerate( ops ):
        if not isinstance( op, dict ) or "op" not in op:
            results.append( { "index": i, "op": None, "ok": False,
                              "message": "each item needs an 'op' name" } )
            all_ok = False
            continue

        name = str( op["op"] )

        if name not in allowed:
            results.append( { "index": i, "op": name, "ok": False,
                              "message": f"unknown {side} op; allowed: {sorted(allowed)}" } )
            all_ok = False
            continue

        params = { k: v for k, v in op.items() if k != "op" }
        r = _live_call( name, params )
        results.append( { "index": i, "op": name, "ok": bool( r.get( "ok" ) ),
                          "message": r.get( "message", "" ) } )

        if not r.get( "ok" ):
            all_ok = False

            if r.get( "not_reachable" ):
                # The app/server is down: every further op would fail identically.
                results[-1]["aborted_remaining_ops"] = True
                break

    return { "ok": all_ok, "results": results }


@server.tool()
def read_live(target: str = "schematic") -> dict:
    """LIVE state of the OPEN editor in the running Anvil CAD app (NOT files on disk).
    target='schematic' (components/nets of the open schematic) or 'board' (footprints/
    tracks of the open board). The in-app chat connects automatically; an external MCP
    client must start its own AnvilCAD MCP session."""
    tool = "get_board" if str( target ).lower().startswith( "b" ) else "get_schematic"
    return _live_call( tool, {} )


@server.tool()
def edit_schematic_live(ops: list) -> dict:
    """Batch-edit the OPEN schematic in the running Anvil CAD app. ops = ORDERED list,
    each {"op": <name>, ...params} with op one of: add_component, add_wire, add_label,
    add_junction, add_no_connect, edit_value, move_component, delete_component,
    delete_at, snap_to_grid, annotate. One user request with several changes = ONE call
    with several ops (never one call per op). Params pass straight to the editor (e.g.
    add_component: lib_id/ref/value/x/y in mils; edit_value: ref+value; add_wire:
    x1/y1/x2/y2). Returns per-op ok/message so partial success is visible."""
    return _live_run_ops( ops, _LIVE_SCH_OPS, "schematic" )


@server.tool()
def edit_board_live(ops: list) -> dict:
    """Batch-edit the OPEN board in the running Anvil CAD app. ops = ORDERED list, each
    {"op": <name>, ...params} with op one of: add_footprint, move_footprint, add_track,
    add_via, delete_track_at, set_text_variable, capture_footprints. One call per user
    request; per-op results returned."""
    return _live_run_ops( ops, _LIVE_PCB_OPS, "board" )


@server.tool()
def check_live(target: str = "erc", name: str = "") -> dict:
    """THE canonical ERC/DRC answer -- target='erc' (schematic) or 'drc' (board).
    Runs the REAL engine inside the OPEN editor (the user's own severity settings,
    pin matrix, exclusions -- and unsaved manual edits), so the result is IDENTICAL
    to the user pressing Run ERC/DRC in the app. If the app is not reachable and a
    project 'name' is given, falls back to the installed CLI on the SAVED file
    (honors settings as of last save). 'source' says which path answered. When the
    user asks for ERC or DRC results, answer from THIS tool -- never from the build
    pre-check, which knows only a few rules and none of the user's settings."""
    tool = "run_drc" if str( target ).lower().startswith( "d" ) else "run_erc"
    res = _live_call( tool, {} )
    if res.get( "ok" ):
        res["source"] = "live_editor"
        return res
    if res.get( "not_reachable" ) and name:
        base = _safe_name( name )
        if tool == "run_erc":
            sch = pdir( base ) / ( base + ".anvil_sch" )
            if not sch.is_file():
                return { "ok": False,
                         "error": f"app not reachable and no saved schematic: {sch}" }
            out = _file_erc( sch )
        else:
            pcb = pdir( base ) / ( base + ".anvil_pcb" )
            if not pcb.is_file():
                return { "ok": False,
                         "error": f"app not reachable and no saved board: {pcb}" }
            out = _board_drc_gate( pcb )
            out["ok"] = bool( out.get( "drc_parsed" ) )
        out["source"] = "saved_file"
        out["source_note"] = ( "app not open -- real engine run on the saved file; "
                               "settings honored as of last project save" )
        return out
    return res


@server.tool()
def autoroute_live() -> dict:
    """Autoroute the board OPEN in the running Anvil CAD app, using the SAME bundled
    FreeRouting engine as the app's Route -> 'Autoroute Board (FreeRouting)' menu -- so
    an AI-triggered autoroute is IDENTICAL to the user doing it by hand. The app must
    have the PCB editor open (a progress dialog shows there while it routes; big boards
    take minutes). Returns unrouted_remaining. If the app is not open, use the headless
    build pipeline instead (create_pcb auto-routes the FILE with the same engine)."""
    return _live_call( "autoroute", {}, timeout=1800.0 )


if __name__ == "__main__":
    server.run()
