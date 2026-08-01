"""
src/skidl/board/route/freerouting.py

[C] middle leg: run the FreeRouting jar headless on a .dsn and produce a
.ses session file. Subprocess-only, mirroring how the rest of the repo
shells out to kicad-cli.

Environment expectations (both auto-detected, both probed by the MCP
diagnostics() tool):
  * a JRE (>= 17; FreeRouting 2.x wants 21) -- PATH, JAVA_HOME, the
    repo's own portable tools/jre/, or an Adoptium install dir
  * tools/freerouting.jar in this repo (or env FREEROUTING_JAR)
"""

from __future__ import annotations
import glob
import os
import shutil
import subprocess
from pathlib import Path

# repo root = .../src/skidl/board/route/freerouting.py -> parents[4]
_REPO = Path(__file__).resolve().parents[4]


def find_java() -> str:
    """Locate a java.exe: env JAVA_EXE, the repo's portable tools/jre,
    PATH, JAVA_HOME, then Adoptium/Temurin install dirs. Empty string if
    none found."""
    env_exe = os.environ.get("JAVA_EXE")
    if env_exe and Path(env_exe).is_file():
        return env_exe

    portable = sorted(_REPO.glob("tools/jre/**/bin/java.exe"))
    if portable:
        return str(portable[0])

    on_path = shutil.which("java")
    if on_path:
        return on_path

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        p = Path(java_home) / "bin" / "java.exe"
        if p.is_file():
            return str(p)

    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
        hits = glob.glob(os.path.join(root, "Eclipse Adoptium", "*", "bin", "java.exe"))
        if hits:
            return sorted(hits)[-1]
    return ""


def find_freerouting_jar() -> str:
    """The preferred jar (first of find_freerouting_jars); empty if none."""
    jars = find_freerouting_jars()
    return jars[0] if jars else ""


def find_freerouting_jars() -> list:
    """FreeRouting jars in preference order.

    HARD RULE: routing must be INVISIBLE -- the user only ever sees the
    result in KiCad, never a FreeRouting window. The 2.x jar runs truly
    headless, so it is the default engine. The classic 1.9 engine routes
    some boards 2.x gives up on, but it MUST open a Swing window to run,
    so it is only ever used when env FREEROUTING_ALLOW_GUI=1 explicitly
    permits a visible window. env FREEROUTING_JAR overrides everything."""
    jars = []
    env_jar = os.environ.get("FREEROUTING_JAR")
    if env_jar and Path(env_jar).is_file():
        jars.append(env_jar)
    tools = _REPO / "tools"
    main = tools / "freerouting.jar"
    if main.is_file():
        jars.append(str(main))
    if os.environ.get("FREEROUTING_ALLOW_GUI") == "1":
        v19 = sorted(tools.glob("freerouting-1.9*.jar"))
        if v19:
            jars.append(str(v19[-1]))
    # De-dup, keep order.
    seen, out = set(), []
    for j in jars:
        if j not in seen:
            seen.add(j)
            out.append(j)
    return out


def route_with_freerouting(dsn_path: Path, ses_path: Path,
                            passes: int = 20, timeout_s: int = 600,
                            progress_file=None) -> dict:
    """Run FreeRouting: dsn in -> ses out, trying each detected jar in
    preference order (1.9 engine first -- see find_freerouting_jars). A
    jar's result is accepted only if it wrote the .ses AND reported no
    unrouted connections; a partial result is kept as a last resort if
    every engine leaves something unrouted. Returns a report dict; 'ok'
    True only when a .ses file exists."""
    dsn_path, ses_path = Path(dsn_path), Path(ses_path)
    java = find_java()
    jars = find_freerouting_jars()
    if not java:
        return {"ok": False, "error": "java not found (install Temurin JRE, or drop a portable one in tools/jre/)"}
    if not jars:
        return {"ok": False, "error": "freerouting jar not found (put it at tools/freerouting.jar)"}

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    last = {"ok": False, "error": "no engine ran"}
    for jar in jars:
        # The 1.9 engine needs a display (HeadlessException otherwise) and
        # is only in the list when FREEROUTING_ALLOW_GUI=1; 2.x runs
        # true-headless -- no window of any kind reaches the user.
        headless = [] if "1.9" in Path(jar).name else ["-Djava.awt.headless=true"]
        ses_path.unlink(missing_ok=True)
        cmd = [
            java, *headless, "-jar", jar,
            "-de", str(dsn_path.resolve()),   # absolute: freerouting resolves
            "-do", str(ses_path.resolve()),   # relative paths against its OWN cwd
            "-mp", str(passes),
            "-dct", "0",          # no dialog confirmation timeout
        ]
        try:
            # STREAM the router's per-pass progress ("pass #N ... (K
            # unrouted)") into progress_file so pollers see LIVE status
            # instead of a 66-minute black box (user-friendliness for
            # genuinely long routing).
            import re as _re3, time as _time
            proc = subprocess.Popen(
                cmd, cwd=str(dsn_path.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                creationflags=no_window)
            out_lines = []
            deadline = _time.time() + timeout_s
            for line in proc.stdout:
                out_lines.append(line)
                if progress_file:
                    m = _re3.search(
                        r"pass #(\d+).*?(?:\((\d+) unrouted\))?\s*$", line)
                    if m and "Auto-router" in line:
                        # Count is absent on most per-pass lines; only
                        # claim a number when the router actually said it.
                        suffix = (f": {m.group(2)} unrouted" if m.group(2)
                                  else " running")
                        try:
                            with open(progress_file, "a", encoding="utf-8") as pf:
                                pf.write(f"routing pass {m.group(1)}{suffix}\n")
                        except OSError:
                            pass
                if _time.time() > deadline:
                    proc.kill()
                    break
            proc.wait(timeout=30)
            if _time.time() > deadline:
                last = {"ok": False,
                        "error": f"FreeRouting exceeded {timeout_s}s budget",
                        "jar": jar}
                continue
        except Exception as exc:
            try:
                proc.kill()
            except Exception:
                pass
            last = {"ok": False, "error": f"FreeRouting failed to launch: {exc!r}",
                    "jar": jar}
            continue

        out = "".join(out_lines)
        if not ses_path.is_file():
            last = {"ok": False,
                    "error": f"FreeRouting exited {proc.returncode} without writing {ses_path.name}",
                    "log_tail": out[-1200:], "jar": jar}
            continue

        # Completeness from the log is advisory only; kicad-cli DRC's
        # unconnected count is the real gate. Only the FINAL session
        # summary counts -- per-pass "(N unrouted)" lines appear during
        # normal routing and a clean finish prints NO final marker, so
        # reading the last match anywhere produced false incompletes
        # (measured: complete=false on a fully-routed, DRC-clean board).
        import re as _re2
        final = _re2.search(r"session completed[^\n]*?\((\d+) unrouted\)", out)
        unrouted = ("could not be routed" in out) or \
                   (final is not None and int(final.group(1)) > 0)
        result = {"ok": True, "ses": str(ses_path), "jar": Path(jar).name,
                  "complete": not unrouted, "log_tail": out[-400:]}
        if not unrouted:
            return result           # log claims fully routed -- done
        last = result               # partial: keep, but try the next engine
    return last
