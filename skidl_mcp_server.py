"""skidl_mcp_server.py -- MCP server that turns THIS repo's SKiDL into circuit tools.

Point Claude Desktop (claude_desktop_config.json -> mcpServers) at this file:

    "anvilcad": {
      "command": "<python 3.11 with the `mcp` package>",
      "args": ["f:\\skidl\\skidl_mcp_server.py"]
    }

It uses ONLY this repo's SKiDL (f:\\skidl\\src\\skidl) + its bundled `skidl.anvil`
engine + plain KiCad symbol libraries.  No proprietary app, no 3D-CAD MCP, no GUI:
every build produces .net + .kicad_sch + .kicad_pro files and returns their paths.

Tools (call build(mode='rules') FIRST, then follow its workflow):
  build(mode='rules')               - the mandatory design workflow + circuit template
  parts(action='search')(queries)            - batch part search: exact match + candidates per query
  parts(action='describe')(parts)             - pins/description/datasheet for a list of parts
  build(name, code, mode)          - generate .net/.kicad_sch/.kicad_pro (body or full script)
  build(mode='status')(name, include_log)  - poll the background build; lists generated files
  build(mode='open')(name, view)     - open the finished project in the Anvil CAD app
  build(name, mode='bom')               - Bill of Materials from the generated netlist
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
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Where generated circuits land (NOT the user's my_circuits folder). Override with
# env SKIDL_MCP_OUT.  Kept inside the repo by default so everything is self-contained.
OUT = Path(os.environ.get("SKIDL_MCP_OUT", str(REPO / "mcp_circuits")))
OUT.mkdir(parents=True, exist_ok=True)

server = FastMCP(
    "anvilcad",
    instructions=(
        "Anvil CAD electronic circuit design tools. Use these tools AUTOMATICALLY "
        "whenever the user asks to design, create, build, or generate ANY electronic "
        "circuit, schematic, netlist, PCB/KiCad project, or BOM -- even if they never "
        "mention 'Anvil CAD', 'MCP', or any tool name. A plain request like 'design a "
        "555 LED blinker' or '5V power supply venum' means: call build(mode='rules') "
        "first and follow that workflow exactly (parts(action='search') -> parts(action='describe') -> "
        "build -> poll build(mode='status') until done -> build(mode='open') once -> ask "
        "about BOM).\n"
        "HARD RULES -- these hold for EVERY circuit, no exceptions:\n"
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
        "3. 'The library doesn't have X', a missing footprint, or an fp-lib-table "
        "warning is NEVER a reason to skip building. Footprints are optional; the "
        "schematic + netlist build regardless.\n"
        "4. A design is DONE only after build(mode='status') returns status:'done' with a "
        "generated .kicad_sch -- cite the produced file paths. NEVER claim a build "
        "succeeded (or describe a schematic as generated) without them.\n"
        "5. ALWAYS show the user the generated file NAME and its FULL PATH (from "
        "build(mode='status') 'generated' -> the .kicad_sch and .kicad_pro) so they can "
        "find/open it, THEN call build(name, mode='open') to open it. If "
        "build(mode='open') reports it did NOT open, STILL give the user the file "
        "path and tell them to open it manually -- NEVER say the schematic 'can't "
        "be rendered/exported here', 'the file-open step isn't available', or that "
        "a tool is unavailable when the .kicad_sch already exists on disk.\n"
        "6. If ANY tool errors, times out, or returns a failure, report the actual "
        "error (and the file path if one was already produced), then STOP -- never "
        "substitute a hand-written schematic, pinout, or BOM as a 'fallback'.\n"
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

# AUTO-NC: every pin the body did NOT connect gets a no-connect cross in the
# schematic (and stays out of ERC unconnected-pin warnings) -- automatic, so
# the circuit code only has to wire the pins it actually uses.
for _part in default_circuit.parts:
    for _pin in _part.pins:
        try:
            if not _pin.is_connected():
                _pin += NC
        except Exception:
            pass

sch, pro = smart_schematic.build()
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
    for ext in (".net", ".kicad_sch", ".kicad_pro"):
        f = OUT / (base + ext)
        if f.is_file():
            files[ext.lstrip(".")] = str(f)
    return files


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
    # stale outputs must not be mistaken for this build's result
    for ext in (".net", ".kicad_sch", ".kicad_pro"):
        try:
            (OUT / (base + ext)).unlink(missing_ok=True)
        except OSError:
            pass
    py = OUT / (base + ".py")
    py.write_text(script_text, encoding="utf-8")
    logfile = OUT / (base + ".build.log")
    fh = open(logfile, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [PYEXE, str(py)],
        cwd=str(OUT),
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
    ok = bool(files.get("net") and files.get("kicad_sch")) and rc == 0
    # Script-mode hierarchical builds may emit schematic+netlist WITHOUT a
    # .kicad_pro (plain generate_schematic path) -- the app then refuses
    # to open the project ("does not appear to be a KiCad project file",
    # measured on the 7-sheet tracker). Seed the standard skeleton.
    if ok and not (OUT / (base + ".kicad_pro")).is_file():
        skeleton = {
            "board": {"design_settings": {"defaults": {}, "rules": {},
                                          "track_widths": [], "via_dimensions": []},
                      "layer_presets": [], "viewports": []},
            "boards": [], "cvpcb": {"equivalence_files": []},
            "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
            "meta": {"filename": base + ".kicad_pro", "version": 1},
            "net_settings": {"classes": [{"name": "Default"}],
                             "meta": {"version": 3}},
            "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
            "schematic": {"legacy_lib_dir": "", "legacy_lib_list": [],
                          "meta": {"version": 1}},
            "sheets": [], "text_variables": {},
        }
        (OUT / (base + ".kicad_pro")).write_text(
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
        "python_file": str(OUT / (base + ".py")),
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
            + " -- the .kicad_sch is WRONG (do not use or show it); the .net "
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
            pyf = OUT / (base + ".py")
            expected_sheets = None
            if pyf.is_file():
                v = mod.analyze_source(str(pyf))
                if v.net_to_blocks:
                    res["connectivity_matrix"] = {
                        nm: sorted(bs) for nm, bs in v.net_to_blocks.items()}
                expected_sheets = 1 + sum(len(c) for c in v.subcircuit_call_lines.values())
            # Phase 10 GOLDEN VERIFICATION: cross-check produced .net <-> .kicad_sch
            # (dropped parts, missing symbols, duplicate UUIDs). Sets res['verified'];
            # a failure must not report as success even though the files exist.
            if files.get("net") and files.get("kicad_sch"):
                sch_paths = [str(p) for p in sorted(OUT.glob(base + "*.kicad_sch"))]
                gv = mod.golden_verify(files["net"], sch_paths,
                                       pro_path=str(OUT / (base + ".kicad_pro")),
                                       expected_sheets=expected_sheets)
                gv_err = [f for f in gv if f.severity == mod.ERROR]
                res["verified"] = not gv_err
                if gv:
                    res["golden_verification"] = [
                        {"rule": f.rule, "severity": f.severity, "message": f.message}
                        for f in gv]
                if gv_err:
                    res["verification_note"] = (
                        "GOLDEN VERIFICATION FAILED -- the .kicad_sch does not match the "
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
              "connectivity_matrix"):
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
set_default_tool(KICAD9)

# ===== circuit body (user) =====
{body}
# ===== end body =====

for _part in default_circuit.parts:
    for _pin in _part.pins:
        try:
            if not _pin.is_connected():
                _pin += NC
        except Exception:
            pass

ERC()
generate_netlist(file_="_precheck_tmp.net")
print("::PRECHECK_OK::")
'''


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
    crashed = proc.returncode != 0 or "::PRECHECK_OK::" not in out
    if crashed or erc_errors or pin_fail:
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
    return {"ok": True,
            "summary": "pre-check passed: body runs, ERC 0 errors"
                       + (f", {n_warn.group(1)} warnings (harmless)" if n_warn else ""),
            "parts": parts,
            "nets": nets,
            "verify": "DEEP-CHECK now: compare every net's pin list and every "
                      "part's value against your intended design. Wrong net -> "
                      "fix the body and build again BEFORE waiting for "
                      "this build."}


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


def _try_open(base: str, view: str = "project") -> str:
    """Open OUT/<base> in Anvil CAD. Launches even when the app is already
    running with OTHER projects (a new window opens); only skips when a window
    for THIS project already exists -- then the user just needs File > Revert."""
    try:
        if _project_window_open(base):
            return (f"'{base}' is already open in Anvil CAD -- do File > Revert "
                    "there to load the freshly built version (never save the "
                    "old buffer over it).")
        mod = _open_anvilcad_mod()
        opener = {"project": mod.open_project, "sch": mod.open_schematic,
                  "pcb": mod.open_pcb}.get(view, mod.open_project)
        ok = opener(str(OUT / base))
        return "opened in Anvil CAD" if ok else \
               "NOT opened (Anvil CAD install not found, or file missing)"
    except Exception as exc:
        return f"open failed: {exc!r}"


def _find_kicad_cli_path() -> str:
    """kicad-cli.exe from the installed CAD app (NOT on PATH here), via the
    same install-bin detector open/verify already use; PATH as fallback."""
    try:
        b = _open_anvilcad_mod()._find_bin()
        if b:
            p = os.path.join(b, "kicad-cli.exe")
            if os.path.isfile(p):
                return p
    except Exception:
        pass
    import shutil
    return shutil.which("kicad-cli") or ""


def _board_drc_gate(pcb_path: Path, refill: bool = False) -> dict:
    """The board gate: prove the .kicad_pcb PARSES in the installed KiCad
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
    # courtyard polygons shipped in the footprint libs -- cosmetic).
    _art = {"malformed_courtyard", "lib_footprint_issues"}
    errors = [v for v in rep.get("violations", [])
              if v.get("severity") == "error" and v.get("type") not in _art]
    return {
        "drc_parsed": True,
        "drc_report": str(report),
        "drc_violations": len(rep.get("violations", [])),
        "drc_errors": len(errors),
        "drc_error_descriptions": [v.get("description") for v in errors][:10],
        "drc_unconnected": len(rep.get("unconnected_items", [])),
    }


def _stale_outputs(base: str):
    """The .py is the SOURCE OF TRUTH: if it is newer than the netlist,
    the design was edited without rebuilding -- downstream artifacts are
    stale and must not be used. Returns a reason string, or None."""
    py = OUT / (base + ".py")
    net = OUT / (base + ".net")
    if py.is_file() and net.is_file() \
            and py.stat().st_mtime > net.stat().st_mtime + 2:
        return (f"{base}.py was modified AFTER the netlist was generated -- "
                "the outputs are STALE. Run build first so the netlist/"
                "schematic regenerate from the edited source, then retry.")
    return None


def _board_fingerprint_path(base: str) -> Path:
    return OUT / (base + ".board_fingerprint.json")


def _stamp_board_fingerprint(base: str) -> None:
    """Record the generated .kicad_pcb's hash so later runs can tell
    'still exactly what the pipeline wrote' from 'a human changed it'."""
    import hashlib
    pcb = OUT / (base + ".kicad_pcb")
    if pcb.is_file():
        digest = hashlib.sha256(pcb.read_bytes()).hexdigest()
        _board_fingerprint_path(base).write_text(
            json.dumps({"sha256": digest}), encoding="utf-8")


def _detect_manual_edits(base: str):
    """Returns a human-readable reason string if <base>.kicad_pcb shows
    MANUAL edits (saved from the PCB editor, or changed outside the
    pipeline), else None.

    ORDER MATTERS: the fingerprint hash is checked FIRST -- the pipeline's
    own zone-fill step (kicad-cli --save-board) rewrites the file with
    generator "pcbnew", so a matching hash proves machine-generated
    regardless of the generator token. The generator check only decides
    when no fingerprint exists."""
    import hashlib
    pcb = OUT / (base + ".kicad_pcb")
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
out = Path({json.dumps(str(OUT))})
base = {json.dumps(base)}
cli = {json.dumps(_find_kicad_cli_path())}
try:
    v = _vb(out / (base + ".kicad_pcb"), out / (base + ".net"), cli)
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
    entry["matches"] = [
        {{"lib": p.lib_name, "part": p.part_name,
          "desc": (p.description or "").strip()}} for p in res
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
                "the user before substituting; never substitute silently. Then "
                "call parts(action='describe') with the chosen Lib:Name list before "
                "writing any connection.",
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
          expected_pads: int = None) -> dict:
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
        description. Writes into the user library; returns the Part(...) line
        to use in build.

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
                                   keywords=keywords)
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


@server.tool()
@_quiet
def build(name: str, code: str = "", mode: str = "body",
          include_log: bool = False, view: str = "project") -> dict:
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
    mode='status' -> poll the running build until 'done' (include_log for
        the build log tail).
    mode='bom'    -> Bill of Materials from the finished netlist
                     (missing[] must be empty).
    mode='open'   -> open the finished project in the CAD app
                     (view: 'project'|'sch'|'pcb') -- ONCE, at the end.

    Build flow: mode='rules' -> parts(action='search'/'describe') ->
    mode='body' -> mode='status' until done -> mode='open'."""
    if mode == "rules":
        return {"ok": True, "design_rules": get_design_rules()}
    if mode == "status":
        return build_status(name, include_log=include_log)
    if mode == "bom":
        return generate_bom(name)
    if mode == "open":
        return open_in_anvilcad(name, view=view)
    if mode not in ("body", "script"):
        return {"ok": False, "status": "precheck_failed",
                "error": "mode must be one of: rules, body, script, status, bom, open"}
    if not code:
        return {"ok": False, "status": "precheck_failed",
                "error": "code is required for mode='body'/'script'"}
    base = _safe_name(name)
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
    script = _HARNESS.format(src=str(SRC), body=code)
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
    for k in ("parts", "nets", "verify"):
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
        logfile = OUT / (base + ".build.log")
        if files.get("net") and files.get("kicad_sch"):
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
    return {"target": str(OUT / base), "status": _try_open(base, view)}


# ============================================================================
#  Custom parts -> BOM -> design rules
# ============================================================================
CACHE = os.path.join(os.path.expanduser("~"), "skidl_symbols")  # anvil_libs cache

_PIN_TYPES = {"input", "output", "bidirectional", "tri_state", "passive", "free",
              "unspecified", "power_in", "power_out", "open_collector",
              "open_emitter", "no_connect"}


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _make_symbol(name, pins, ref_prefix="U", value=None, footprint="",
                 datasheet="", description="", keywords=""):
    """Build a valid KiCad (symbol ...) block: a rectangle body + pins on left/right."""
    value = value or name
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
                        keywords: str = "") -> dict:
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
        "pins_added": len(pins),
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


@_quiet
def generate_bom(name: str) -> dict:
    """Build a complete Bill of Materials from a circuit's generated
    netlist so NO part is missed. Lists every component (Ref, Value, Footprint),
    groups quantities, and FLAGS any part missing a value or footprint. Writes
    <name>_bom.csv next to the outputs. Returns rows + missing[] (must be empty)."""
    base = _safe_name(name)
    net = OUT / (base + ".net")
    if not net.is_file():
        return {"ok": False, "error": f"netlist not found: {net} -- run build first"}
    comps = _parse_comps(net.read_text(encoding="utf-8", errors="replace"))
    if not comps:
        return {"ok": False, "error": "no components parsed from netlist"}

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

    # group qty by (value, footprint)
    groups = {}
    for c in comps:
        key = (c["value"], c["footprint"])
        groups.setdefault(key, []).append(c["ref"])

    csv_path = OUT / (base + "_bom.csv")
    rows = ["Ref,Value,Footprint,Status"]
    for c in sorted(comps, key=lambda x: x["ref"]):
        rows.append(f'{c["ref"]},{c["value"]},{c["footprint"]},{c["status"]}')
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    return {
        "ok": len(missing) == 0,
        "bom_csv": str(csv_path),
        "total_parts": len(comps),
        "line_items": [
            {"value": k[0], "footprint": k[1], "qty": len(v), "refs": sorted(v)}
            for k, v in sorted(groups.items())
        ],
        "components": sorted(comps, key=lambda x: x["ref"]),
        "missing": missing,
        "note": "missing must be empty. If not, fill value/footprint on those parts and rebuild.",
    }


@server.tool()
@_quiet
def create_pcb(name: str, layers: int = 2, route: bool = True,
               force: bool = False) -> dict:
    """Create a placed AND routed .kicad_pcb from a finished build's netlist.
    STEP ZERO: if you haven't analyzed this machine yet this session, call
    analyze_pcb_environment first -- every user's system and Board Setup
    differs, and user-set values are always detected and kept.
    Requires that build/build(mode='status') already produced <name>.net. Pipeline:
    resolve every part's REAL footprint (fails early listing any missing
    "Lib:Name") -> rule placement (anchor IC centered, decaps beside their IC,
    connectors on the edge, zero courtyard overlaps) -> INVISIBLE FreeRouting
    autorouting (headless -- the user NEVER sees a routing window; they only
    see the result in KiCad) -> <name>.kicad_pcb + net classes in .kicad_pro
    -> kicad-cli DRC gate -> board-vs-netlist connectivity verification.
    Report status honestly: 'routed' only when routing completed AND the board
    matches the netlist pin-for-pin; otherwise say exactly what is missing.
    After success, open with build(mode='open')(name, view='pcb')."""
    base = _safe_name(name)
    net = OUT / (base + ".net")
    if not net.is_file():
        return {"ok": False, "error": f"netlist not found: {net} -- run build first"}

    # ---- ASYNC job engine: big boards need 10-30 min of routing, which
    # no MCP client will wait for (measured: the 150-part tracker timed
    # out Desktop at 4 min). The whole attempt-chain + gates run in ONE
    # background process (board/pcb_job.py); this tool STARTS it and
    # subsequent calls POLL it -- same UX as build/build(mode='status').
    job = _PCB_JOBS.get(base)
    result_file = OUT / (base + ".pcb_result.json")

    if job and job["proc"].poll() is None:
        elapsed = int(time.time() - job["started"])
        if elapsed > 14400:   # adaptive budgets: big boards run hours
            job["proc"].kill()
            _PCB_JOBS.pop(base, None)
            return {"ok": False, "status": "failed",
                    "error": "PCB job exceeded 60 min -- killed; check "
                             f"{base}.pcb_build.log"}
        tail = ""
        logf = OUT / (base + ".pcb_build.log")
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
        result_file.replace(OUT / (base + ".pcb_result.delivered.json"))
        _stamp_board_fingerprint(base)
        if finished_job and finished_job.get("setup_changed"):
            res["board_setup_changed"] = True
            res["board_setup_note"] = ("user edited Board Setup since the last "
                                       "build -- this build USED the new values")
        if res.get("status") == "routed":
            res["note"] = ("Board placed + routed + verified (DRC clean, 0 "
                           "unconnected, board-vs-netlist matched). Open with "
                           "build(name, mode='open', view='pcb'); then "
                           "review_design -> approval -> export_manufacturing.")
        return res

    # Detached job still running (server restarted, registry gone)?
    # A recent log with no result yet = in progress -- do NOT double-start.
    logf = OUT / (base + ".pcb_build.log")
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
    manual = _detect_manual_edits(base)
    if manual and not force:
        return {
            "ok": False,
            "status": "manual_edits_detected",
            "error": f"REFUSED to overwrite {base}.kicad_pcb: {manual}",
            "instruction": ("ASK THE USER before proceeding -- their manual "
                            "layout would be LOST. Options: (a) keep their "
                            "board and continue with run_drc/verify_board/"
                            "export_manufacturing on it as-is; (b) re-run "
                            "create_pcb with force=True to regenerate from "
                            "scratch, ONLY after they explicitly agree."),
        }

    result_file.unlink(missing_ok=True)
    (OUT / (base + ".pcb_build.log")).unlink(missing_ok=True)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {json.dumps(str(SRC))})\n"
        "from skidl.anvil import anvil_libs\n"
        "from skidl.board.pcb_job import run_pcb_job\n"
        f"run_pcb_job({json.dumps(base)}, {json.dumps(str(OUT))}, "
        f"{json.dumps(_find_kicad_cli_path())}, layers={int(layers)}, "
        f"route={bool(route)})\n"
    )
    proc = subprocess.Popen(
        [PYEXE, "-X", "utf8", "-c", code],
        cwd=str(OUT), env=_subprocess_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _PCB_JOBS[base] = {"proc": proc, "started": time.time(),
                       "setup_changed": setup_changed}
    return {"ok": True, "status": "pcb_building", "elapsed_s": 0,
            "note": ("PCB build STARTED in the background: placement -> "
                     "invisible routing (generous budget) -> zone fill -> "
                     "DRC -> verification, with automatic placement "
                     "fallbacks. Poll with create_pcb(name) every 1-3 "
                     "minutes until status is no longer 'pcb_building'. "
                     "Small boards finish in ~1 min; a 150-part board can "
                     "take 10-30 min -- this call will NEVER time out again.")}


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
      rules the USER already set in .kicad_pro, existing board setup
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
    d = discover_rules(base, Path({json.dumps(str(OUT))}))
    info["project"] = {{
        "user_net_classes": d["project"]["net_classes"],
        "user_design_rules": d["project"]["design_rules"],
        "custom_drc_rules_file": d["project"]["custom_drc_rules"],
        "board_setup": {{k: v for k, v in d["board"].items()
                         if not k.endswith("_text")}},
        "prior_ai_choices": bool(d["sidecar"]),
    }}
info["system_user_defaults"] = discover_rules(base or "_probe_",
    Path({json.dumps(str(OUT))}))["system_user"]
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
    """(changed, current_hash): whether Board Setup differs from what the
    last build consumed (stored in the sidecar). Read-only tools report
    the flag but do NOT update the stored hash -- only a rebuild or an
    explicit update_board_setup 'consumes' the new setup."""
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.board_setup import compute_setup_hash
h = compute_setup_hash({json.dumps(base)}, Path({json.dumps(str(OUT))}))
print("\\n::RESULT::" + json.dumps({{"ok": True, "hash": h}}))
'''
    res = _py_json(code, timeout=60)
    current = res.get("hash")
    stored = None
    sc = OUT / (base + ".board_config.json")
    if sc.is_file():
        try:
            stored = json.loads(sc.read_text(encoding="utf-8")).get("setup_hash")
        except Exception:
            pass
    changed = bool(current and stored and current != stored)
    return changed, current


@server.tool()
@_quiet
def get_board_setup(name: str) -> dict:
    """READ the user's CURRENT Board Setup -- call this before autoroute/
    create_pcb/major operations and whenever the user may have edited
    Board Setup in the KiCad application. DYNAMIC discovery, no fixed
    schema: returns `raw` (EVERYTHING found in the board file's setup/
    layers blocks + the project's design_settings/net_settings, even keys
    this pipeline has never seen), `consumed` (the values that actively
    drive routing/DRC, each mapped found_value -> used_in), `preserved`
    (found but not interpreted -- kept verbatim, never lost), and
    `setup_hash` (changes when the user edits ANY setting). The values
    here are the source of truth -- later steps MUST use them, never
    defaults."""
    base = _safe_name(name)
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.board_setup import get_board_setup as _gbs
try:
    info = {{"ok": True, **_gbs({json.dumps(base)}, Path({json.dumps(str(OUT))}))}}
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
                        mask: dict = None, drc_severities: dict = None) -> dict:
    """APPLY the user's requested Board Setup changes (their explicit
    instruction is authoritative). Only the sections passed are touched;
    everything else -- including settings this pipeline doesn't interpret
    -- stays exactly as-is. Sections: stackup {layers,thickness,copper_oz},
    constraints {min_track_width,min_clearance,...}, net_classes
    {"Power": {"width": 1.0, "clearance": ...}}, via_rules {via_size,
    via_drill}, mask {pad_to_mask_clearance}, drc_severities {rule:
    "error"|"warning"|"ignore"}. Re-run create_pcb afterwards so routing
    uses the new values (layers/width changes need a re-route)."""
    base = _safe_name(name)
    args = {"stackup": stackup, "constraints": constraints,
            "net_classes": net_classes, "via_rules": via_rules,
            "mask": mask, "drc_severities": drc_severities}
    if not any(v for v in args.values()):
        return {"ok": False, "error": "nothing to change -- pass at least one section"}
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.board.board_setup import update_board_setup as _ubs
kw = json.loads({json.dumps(json.dumps(args))})
try:
    info = {{"ok": True, **_ubs({json.dumps(base)}, Path({json.dumps(str(OUT))}),
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
        # The board file may have been patched (mask) -- keep the
        # manual-edit fingerprint truthful.
        _stamp_board_fingerprint(base)
        res["note"] = ("Board Setup updated. Re-run create_pcb (with the "
                       "user's consent if the board has manual work) so "
                       "placement/routing use the new values.")
    return res


@server.tool()
@_quiet
def initialize_pcb_project(name: str, layers: int = 2,
                            board_width: float = None, board_height: float = None,
                            mounting_holes: str = None,
                            manufacturer: str = "jlcpcb",
                            ground_pour: bool = True,
                            ipc_class: int = None,
                            mechanical: dict = None,
                            currents: dict = None) -> dict:
    """LEARN-FIRST PCB project setup. Call analyze_pcb_environment FIRST
    (step zero) and show the user what their system already has -- then
    this tool discovers what THIS system and THIS
    user already configured (project .kicad_pro Board Setup + net classes,
    .kicad_dru custom rules, existing board edits, the user's own KiCad
    app defaults under APPDATA, the installed application and its real
    capabilities) and only fills in what's missing from the manufacturer
    profile. Auto-assigns net classes (power rails -> Power/wider tracks,
    USB D+/D- -> USB), writes design rules + classes into .kicad_pro and
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
    Run before create_pcb for explicit control; create_pcb auto-runs it
    with defaults otherwise."""
    base = _safe_name(name)
    if not (OUT / (base + ".net")).is_file():
        return {"ok": False, "error": f"netlist not found -- run build first"}
    code = f'''
import sys, json
sys.path.insert(0, {json.dumps(str(SRC))})
from pathlib import Path
from skidl.anvil import anvil_libs   # sets KICAD*_FOOTPRINT_DIR -- fine-pitch
                                     # probing loads real footprints
from skidl.board.project_init import initialize_project
try:
    rep = initialize_project({json.dumps(base)}, Path({json.dumps(str(OUT))}),
                             layers={int(layers)},
                             board_width={board_width if board_width else "None"},
                             board_height={board_height if board_height else "None"},
                             mounting_holes={json.dumps(mounting_holes) if mounting_holes else "None"},
                             manufacturer={json.dumps(manufacturer)},
                             ground_pour={bool(ground_pour)},
                             ipc_class={int(ipc_class) if ipc_class else "None"},
                             mechanical={json.dumps(mechanical) if mechanical else "None"},
                             kicad_cli={json.dumps(_find_kicad_cli_path())})
    if {json.dumps(currents) if currents else "None"}:
        scp = Path({json.dumps(str(OUT))}) / ({json.dumps(base)} + ".board_config.json")
        sc = json.loads(scp.read_text(encoding="utf-8"))
        sc["currents"] = {json.dumps(currents) if currents else "None"}
        scp.write_text(json.dumps(sc, indent=2), encoding="utf-8")
        rep["currents"] = "stored in sidecar -- rerun initialize to apply width plan"
    info = {{"ok": True, **rep}}
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
    net = OUT / (base + ".net")
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
    """Run KiCad's Design Rules Check on <name>.kicad_pcb (created by
    create_pcb). Returns violation counts + the JSON report path. On a routed
    board expect 0 errors and 0 unconnected; silk/lib warnings are cosmetic.
    Any ERROR-severity violation or unconnected item must be reported to the
    user verbatim -- never described as passing."""
    base = _safe_name(name)
    pcb = OUT / (base + ".kicad_pcb")
    if not pcb.is_file():
        return {"ok": False, "error": f"board not found: {pcb} -- run create_pcb first"}
    res = _board_drc_gate(pcb)
    res["ok"] = bool(res.get("drc_parsed"))
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
    netlist from <name>.kicad_pcb (IPC-D-356) and compare its pin-partition to
    the intended <name>.net. ok:true = every net groups the same pins on the
    board as in the netlist (no short, no broken connection). ok:false =
    MISMATCH -- the board must NOT be manufactured; report 'missing' (broken
    nets) and 'extra' (shorts) to the user verbatim."""
    base = _safe_name(name)
    if not (OUT / (base + ".kicad_pcb")).is_file():
        return {"ok": False, "error": "board not found -- run create_pcb first"}
    if not (OUT / (base + ".net")).is_file():
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
    if not (OUT / (base + ".kicad_pcb")).is_file():
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
    info = {{"ok": True, **_rd({json.dumps(base)}, Path({json.dumps(str(OUT))}),
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
    info = _ad({json.dumps(base)}, Path({json.dumps(str(OUT))}),
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
    {{"ok": True, **check_approval({json.dumps(base)}, Path({json.dumps(str(OUT))}))}}))
'''
    return _py_json(code, timeout=60)


@server.tool()
@_quiet
def export_manufacturing(name: str, formats: list[str] = None) -> dict:
    """Export manufacturing files for <name>.kicad_pcb: Gerbers + drill +
    pick-and-place by default (optionally 'pdf', 'step'), zipped into
    <name>_fab.zip a fab house accepts. REFUSES to export a board that fails
    DRC (error-severity) or the board<->netlist check -- manufacturing a
    known-bad board is never acceptable; report what failed instead."""
    base = _safe_name(name)
    pcb = OUT / (base + ".kicad_pcb")
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
out = Path({json.dumps(str(OUT))})
info = _em(out / ({json.dumps(base)} + ".kicad_pcb"),
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
    exts = [".kicad_pro", ".kicad_sch", ".kicad_pcb", ".net",
            "_bom.csv", ".drc.json", "_fab.zip",
            "_review.md", ".approval.json"]
    import zipfile
    files = []
    for ext in exts:
        p = OUT / (base + ext)
        if p.is_file():
            files.append(p)
    # child schematic sheets (<base>_*.kicad_sch)
    files += [p for p in OUT.glob(base + "_*.kicad_sch") if p.is_file()]
    if not any(f.suffix == ".kicad_pcb" for f in files):
        return {"ok": False, "error": "no board found -- run create_pcb first"}
    zip_path = OUT / (base + "_project.zip")
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
    '# 1) NETS (wires / signals)\n'
    'vcc = Net("VCC")\n'
    'gnd = Net("GND")\n\n'
    '# 2) PARTS -- Part("Lib","Name", ref=, tag=, value=, footprint=)\n'
    '#    Lib/Name come from parts search; pins from parts describe. Never invent them.\n'
    'r = Part("Device", "R", ref="R1", tag="R1", value="330",\n'
    '         footprint="Resistor_SMD:R_0805_2012Metric")\n'
    'led = Part("Device", "LED", ref="D1", tag="D1",\n'
    '           footprint="LED_SMD:LED_0805_2012Metric")\n\n'
    '# 3) CONNECT -- series:  vcc & r & led & gnd\n'
    '#    or pin-by-pin:  r[1] += vcc ; r[2] += led[1] ; led[2] += gnd\n'
    'vcc & r & led & gnd\n\n'
    '# 4) UNUSED PINS -- mark with NC so the schematic draws no-connect crosses:\n'
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
    "* this MCP's builder DOES produce a .kicad_pro project (smart_schematic),\n"
    "  unlike plain SKiDL described in the skill text.\n"
    "* UNUSED pins are AUTO-marked NC by the harness (no-connect crosses drawn\n"
    "  automatically) -- you do not need to add NC yourself.\n"
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
    "  .kicad_sch is WRONG (shorts/missing nets) -- tell the user NOT to open\n"
    "  it; the .net netlist is still valid for PCB layout. NEVER present that\n"
    "  build as a success. schematic_mode='labels' with status 'done' is FINE\n"
    "  (labels connect by name -- normal practice for dense circuits).\n"
    "* SUCCESS GATE (GEN-3, spec section 10): a build is SUCCESSFUL only when\n"
    "  build(mode='status') returns status:'done' AND 'generated' lists a .kicad_sch\n"
    "  (plus .kicad_pro). Until then it is 'building' or INCOMPLETE. NEVER say\n"
    "  'schematic generated' / 'verified' / 'Build Successful' from ERC + netlist\n"
    "  alone -- ERC-clean + .net is only the pre-check. If you cannot confirm the\n"
    "  .kicad_sch file exists, the build is NOT done; keep polling build(mode='status').\n"
    "* DENSE FLAT SHEETS FAIL TO RENDER (spec section 10.6): a flat mode='body'\n"
    "  design above ~12 parts can pass ERC + netlist yet FAIL to publish a\n"
    "  schematic -- on one dense sheet the renderer geometrically shorts nets\n"
    "  (observed: GND+3V3+SCL fused) and refuses to publish. For any design\n"
    "  above ~12 parts, use mode='script' with functional @subcircuit blocks\n"
    "  (power / MCU / clock / reset / comms / I/O / test-points), passing shared\n"
    "  nets as arguments. Decomposition de-densifies each region and lets it\n"
    "  route with wires. A clean pre-check does NOT guarantee the schematic\n"
    "  renders -- only status:'done' with a .kicad_sch + verified:true does.\n"
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
    "* ERC WARNINGS are harmless; rebuild ONLY for a real ERC ERROR. ONE build.\n"
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
        "              electrical precheck). Unused pins are AUTO-marked NC by the\n"
        "              harness (no-connect crosses drawn automatically -- do not add\n"
        "              NC yourself). It returns IMMEDIATELY with status 'building'\n"
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


if __name__ == "__main__":
    server.run()
