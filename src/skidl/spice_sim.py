"""
src/skidl/spice_sim.py

Run a SPICE netlist through the ngspice engine BUNDLED with the app (the same
ngspice.dll the schematic editor's simulator uses) and return the result vectors
as plain Python -- so the AI can simulate a circuit (operating point / DC sweep /
transient / AC) between schematic and PCB, the standard EDA step.

WHY the bootstrap below exists (all proven necessary on the shipped runtime):
  1. InSpice's package __init__ eagerly imports a matplotlib plot probe; we never
     plot, so a tiny matplotlib stub avoids that heavy dep.
  2. The bundled InSpice ships one Python-3.12-only f-string in NgSpice/Shared.py
     that is a SyntaxError on the shipped Python 3.11. We load a corrected copy of
     that ONE module into sys.modules IN MEMORY (no file on disk is modified).
  3. ngspice is a .dll only (no CLI, no share/ngspice): point InSpice at it via
     NGSPICE_LIBRARY_PATH and set SPICE_LIB_DIR so it skips the missing-tree probe.

Basic analog (R/L/C, independent + controlled sources, diodes/BJT/MOSFET with
inline .model, behavioral B-sources) needs no external code models, so the
missing share/ngspice tree is fine. This is CONNECTIVITY+MODEL simulation, honest
about its inputs -- results are only as good as the models in the netlist.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path


def _find_ngspice_dll():
    """Locate the bundled ngspice.dll relative to this Python, or via env."""
    env = os.environ.get("NGSPICE_LIBRARY_PATH")
    if env and Path(env).is_file():
        return Path(env)
    # sys.executable = <install>\bin\ai\python\python.exe  ->  <install>\bin\ngspice.dll
    exe = Path(sys.executable)
    for up in (exe.parents[2] if len(exe.parents) > 2 else exe.parent,
               exe.parents[3] if len(exe.parents) > 3 else exe.parent):
        cand = up / "ngspice.dll"
        if cand.is_file():
            return cand
    # last resort: search a couple of levels under the install root
    for base in exe.parents[:5]:
        for cand in base.glob("**/ngspice.dll"):
            return cand
    return None


_BOOTSTRAPPED = False


def _bootstrap():
    """Make the bundled InSpice+ngspice importable on the shipped runtime."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    # (1) stub matplotlib (InSpice.__init__ imports a plot probe we never use).
    for n in ("matplotlib", "matplotlib.pyplot"):
        sys.modules.setdefault(n, types.ModuleType(n))
    sys.modules["matplotlib"].pyplot = sys.modules["matplotlib.pyplot"]

    # (3) tell InSpice where the .dll is and skip the missing share/ngspice probe.
    dll = _find_ngspice_dll()
    if dll is not None:
        os.environ.setdefault("NGSPICE_LIBRARY_PATH", str(dll))
        os.environ.setdefault("SPICE_LIB_DIR", str(dll.parent))

    import InSpice  # noqa: F401  (matplotlib stub already in place)

    # (2) load a 3.11-corrected copy of NgSpice/Shared.py IN MEMORY (no disk write).
    shp = Path(InSpice.__file__).parent / "Spice" / "NgSpice" / "Shared.py"
    src = shp.read_text(encoding="utf-8")
    src = src.replace("f' after {kwargs['after']}'", 'f" after {kwargs[\'after\']}"')
    fixed = types.ModuleType("InSpice.Spice.NgSpice.Shared")
    fixed.__file__ = str(shp)
    exec(compile(src, str(shp), "exec"), fixed.__dict__)
    sys.modules["InSpice.Spice.NgSpice.Shared"] = fixed
    _BOOTSTRAPPED = True


def _extract(plot):
    """Turn an ngspice plot into {vector_name: [values]}.

    Real analyses (op/dc/tran) return real value lists. AC returns COMPLEX
    per node, so each AC node yields <name> (magnitude), <name>_db (20log10),
    and <name>_deg (phase); the sweep axis is emitted as 'frequency' (AC) or
    'time' (tran) when available.
    """
    import numpy as np
    analysis = plot.to_analysis()
    out = {}

    def _emit(name, arr):
        arr = np.atleast_1d(np.asarray(arr))
        if np.iscomplexobj(arr):
            mag = np.abs(arr)
            out[name] = [round(float(x), 9) for x in mag]
            out[name + "_db"] = [round(float(20 * np.log10(m)) if m > 0 else -999.0, 4)
                                 for m in mag]
            out[name + "_deg"] = [round(float(np.degrees(np.angle(x))), 4) for x in arr]
        else:
            out[name] = [round(float(x), 9) for x in arr.real]

    # sweep axis (frequency for AC, time for transient)
    for axis in ("frequency", "time"):
        ax = getattr(analysis, axis, None)
        if ax is not None:
            try:
                out[axis] = [round(float(x), 9) for x in np.atleast_1d(np.asarray(ax)).real]
            except Exception:
                pass
    for grp in (getattr(analysis, "nodes", {}), getattr(analysis, "branches", {})):
        for name, wf in dict(grp).items():
            try:
                _emit(str(name), wf)
            except Exception:
                continue
    return out


def run_netlist(netlist_text: str, max_points: int = 2000) -> dict:
    """Simulate a complete SPICE netlist (must contain its own analysis line, e.g.
    .op / .dc / .tran / .ac, and end with .end). Returns
    {ok, analysis_type, vectors:{name:[values]}, summary:{node:last_value}, log}.
    Long vectors are downsampled to max_points for transport."""
    _bootstrap()
    from InSpice.Spice.NgSpice.Shared import NgSpiceShared

    logs = []
    ng = NgSpiceShared.new_instance()
    try:
        ng.load_circuit(netlist_text)
        ng.run()
    except Exception as exc:
        return {"ok": False, "error": "ngspice run failed: %r" % (exc,),
                "log": "\n".join(str(x) for x in logs)[-2000:]}
    try:
        plot = ng.plot(None, ng.last_plot)
        vectors = _extract(plot)
    except Exception as exc:
        return {"ok": False, "error": "could not read results: %r" % (exc,)}

    # analysis type from the plot name (op1/tran1/ac1/dc1/...)
    pname = str(getattr(ng, "last_plot", "") or "")
    atype = ("operating_point" if pname.startswith("op") else
             "transient" if pname.startswith("tran") else
             "ac" if pname.startswith("ac") else
             "dc" if pname.startswith("dc") else pname or "unknown")

    # downsample long sweeps for transport; keep endpoints
    n = max((len(v) for v in vectors.values()), default=0)
    if n > max_points:
        step = n // max_points + 1
        vectors = {k: (v[::step] + [v[-1]] if len(v) > 1 else v)
                   for k, v in vectors.items()}

    summary = {k: v[-1] for k, v in vectors.items()
               if not k.startswith("_") and v}
    return {
        "ok": True,
        "analysis_type": atype,
        "vectors": vectors,
        "summary": summary,
        "n_points": n,
        "note": ("Results are only as good as the netlist's models. This is the "
                 "app's own ngspice; op/dc/tran/ac on R/L/C, sources, and parts "
                 "with inline .model work without external code models."),
    }
