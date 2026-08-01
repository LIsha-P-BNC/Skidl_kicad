"""
src/skidl/board/pcb_job.py

The COMPLETE PCB build job -- placement attempts, routing with a REAL
time budget, zone fill, DRC and board-vs-netlist verification -- run as
ONE background process, so no MCP client timeout can ever kill a big
board mid-build again (measured: the 150-part tracker needs 10-30 min of
FreeRouting; Desktop abandons synchronous calls at ~4).

Writes progress to <base>.pcb_build.log and the final consolidated
result (the same shape create_pcb used to return synchronously) to
<base>.pcb_result.json. The MCP server starts this in the background and
polls -- see skidl_mcp_server.create_pcb.

Runnable directly:  python -m skidl.board.pcb_job <base> <out_dir>
                    <kicad_cli> [layers] [route]
"""

from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PASSES = 30


def _route_budget(out_dir, base) -> int:
    """ADAPTIVE routing budget: FreeRouting keeps progress only in
    memory, so a killed attempt yields NOTHING -- one long sitting beats
    fragments (measured: 383-airwire tracker got zero from a 30-min
    fragment). Scale with design size: ~8s per netlist pin, clamped to
    [15 min, 90 min]."""
    try:
        txt = (Path(out_dir) / f"{base}.net").read_text(
            encoding="utf-8", errors="replace")
        pins = txt.count("(node")
    except OSError:
        pins = 100
    return min(5400, max(900, pins * 8))


def _log(out_dir, base, msg):
    with open(Path(out_dir) / f"{base}.pcb_build.log", "a", encoding="utf-8") as f:
        f.write(msg.rstrip() + "\n")


def _drc(pcb_path: Path, kicad_cli: str, refill: bool) -> dict:
    report = pcb_path.with_suffix(".drc.json")
    extra = ["--refill-zones", "--save-board"] if refill else []
    proc = subprocess.run(
        [kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
         *extra, "--output", str(report), str(pcb_path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=300, creationflags=_NO_WINDOW)
    if extra and proc.returncode != 0 and "nrecogni" in (proc.stderr or ""):
        proc = subprocess.run(
            [kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
             "--output", str(report), str(pcb_path)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=300, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not report.is_file():
        return {"drc_parsed": False,
                "drc_error": ((proc.stderr or "") + (proc.stdout or ""))[-400:]}
    rep = json.loads(report.read_text(encoding="utf-8", errors="replace"))
    art = {"malformed_courtyard", "lib_footprint_issues"}
    errors = [v for v in rep.get("violations", [])
              if v.get("severity") == "error" and v.get("type") not in art]
    return {"drc_parsed": True, "drc_report": str(report),
            "drc_violations": len(rep.get("violations", [])),
            "drc_errors": len(errors),
            "drc_error_descriptions": [v.get("description") for v in errors][:10],
            "drc_unconnected": len(rep.get("unconnected_items", []))}


def run_pcb_job(base: str, out_dir, kicad_cli: str, layers: int = 2,
                route: bool = True) -> dict:
    out_dir = Path(out_dir)
    # Library env (KICAD9_FOOTPRINT_DIR etc.) is set as an import side
    # effect; without it a direct `python -m skidl.board.pcb_job` run
    # can't resolve ANY footprint (measured: 157 unresolved on tracker).
    try:
        import skidl.anvil.anvil_libs  # noqa: F401
    except Exception as exc:
        _log(out_dir, base, f"anvil_libs env setup failed: {exc!r}")
    from skidl.board.pipeline import build_pcb
    from skidl.board.project_init import initialize_project

    # LEARN-FIRST: auto-init when the project was never initialized.
    if not (out_dir / f"{base}.board_config.json").is_file():
        _log(out_dir, base, "initializing project (defaults)")
        try:
            initialize_project(base, out_dir, kicad_cli=kicad_cli)
        except Exception as exc:
            _log(out_dir, base, f"init failed: {exc!r}")

    def flaws(g):
        return (g.get("drc_unconnected", 0) or 0) + (g.get("drc_errors", 0) or 0)

    budget = _route_budget(out_dir, base)
    attempts = [("rules", PASSES, None),
                ("simple", PASSES * 2, None),
                ("simple", PASSES * 2, {"Power": {"width": 0.25},
                                        "USB": {"width": 0.25}})]
    if budget > 2700:
        # Big board: two long sittings beat three; width-uniformity
        # matters less than density at this scale.
        attempts = attempts[:2]
    best = None
    for i, (placement, passes, extra_classes) in enumerate(attempts, 1):
        label = placement + (" uniform-width" if extra_classes else "")
        _log(out_dir, base, f"attempt {i}/{len(attempts)}: {label} "
                            f"placement, route budget {budget}s")
        try:
            info = build_pcb(out_dir / f"{base}.net", out_dir=out_dir,
                             name=base, layers=layers, route=route,
                             passes=passes, route_timeout_s=budget,
                             placement=placement, net_classes=extra_classes,
                             progress_file=out_dir / f"{base}.pcb_build.log")
        except Exception as exc:
            import traceback
            _log(out_dir, base, f"attempt {i} crashed: {exc!r}")
            info = {"ok": False, "error": repr(exc),
                    "trace": traceback.format_exc()[-600:]}
        if not info.get("ok"):
            best = best or info
            continue
        gate = _drc(out_dir / f"{base}.kicad_pcb", kicad_cli,
                    refill=bool(route))
        info.update(gate)
        if (info.get("routing", {}).get("ok")
                and (gate.get("drc_errors") or 0) > 0):
            # Router sliver artifacts (micro-fragments at dense
            # junctions) show up as clearance/short errors on copper
            # that carries nothing. heal only ever REMOVES fragments and
            # restores the file when the result is not strictly better.
            try:
                from skidl.board.route.heal import heal_slivers
                for _ in range(4):     # keep healing while it makes progress
                    hr = heal_slivers(out_dir / f"{base}.kicad_pcb", kicad_cli,
                                      max_rounds=24)
                    info["heal"] = hr
                    if not hr.get("healed"):
                        break
                    info.update({"drc_errors": hr["drc_errors"],
                                 "drc_unconnected": hr["drc_unconnected"]})
                    _log(out_dir, base,
                         f"healed {hr['removed_segments']} sliver segment(s): "
                         f"errors {hr['drc_errors_before']} -> {hr['drc_errors']}")
                    if not hr["drc_errors"]:
                        break
            except Exception as exc:
                import traceback
                _log(out_dir, base, f"heal failed: {exc!r}\n"
                     + traceback.format_exc()[-500:])
        _log(out_dir, base,
             f"attempt {i}: routed={info.get('routing', {}).get('ok')} "
             f"drc_errors={info.get('drc_errors')} "
             f"unconnected={info.get('drc_unconnected')}")
        # Archive each attempt's artifacts -- forensics on attempt 1
        # after attempt 2 overwrote it used to cost a full re-run.
        import shutil
        for ext in (".kicad_pcb", ".drc.json", ".ses", ".dsn"):
            src = out_dir / f"{base}{ext}"
            if src.is_file():
                shutil.copy(src, out_dir / f"{base}.attempt{i}{ext}")
        if extra_classes and flaws(info) == 0:
            info["placement_retry"] = ("board too dense for widened "
                                       "Power-class tracks -- routed with "
                                       "uniform width")
        if best is None or flaws(info) < flaws(best):
            best = info
        if gate.get("drc_parsed") and flaws(info) == 0:
            break                    # clean -- stop trying

    res = best or {"ok": False, "error": "all attempts failed"}
    if res.get("ok"):
        routed = bool(res.get("routing", {}).get("ok"))
        if res.get("drc_parsed") is False:
            res.update(ok=False, status="failed",
                       error="generated board REJECTED by KiCad -- writer bug")
        elif routed and res.get("drc_errors", 0) > 0:
            res.update(ok=False, status="failed",
                       error=f"{res['drc_errors']} electrical DRC error(s) -- do not use")
        elif routed and res.get("drc_unconnected", 0) > 0:
            res.update(ok=False, status="routing_incomplete",
                       error=(f"{res['drc_unconnected']} connection(s) could not "
                              "be auto-routed after all attempts -- finish "
                              "manually in the editor; NEVER export this"))
        elif routed:
            try:
                from skidl.board.verify import verify_board
                v = verify_board(out_dir / f"{base}.kicad_pcb",
                                 out_dir / f"{base}.net", kicad_cli)
                res["board_matches_netlist"] = bool(v.get("ok"))
                if not v.get("ok"):
                    res.update(ok=False, status="failed",
                               error="MISMATCH: board does not implement the netlist")
                else:
                    res["status"] = "routed"
            except Exception as exc:
                res["board_matches_netlist"] = None
                res["verify_error"] = repr(exc)
                res["status"] = "routed"
        else:
            res["status"] = "placed_unrouted"

    (out_dir / f"{base}.pcb_result.json").write_text(
        json.dumps(res, indent=2, default=str) + "\n", encoding="utf-8")
    _log(out_dir, base, f"DONE: {res.get('status')}")
    return res


if __name__ == "__main__":
    base, out_dir, cli = sys.argv[1], sys.argv[2], sys.argv[3]
    layers = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    route = (sys.argv[5].lower() != "false") if len(sys.argv) > 5 else True
    run_pcb_job(base, out_dir, cli, layers=layers, route=route)
