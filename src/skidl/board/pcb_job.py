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
    # Split unconnected into REAL (a pad/pin the router failed to connect --
    # the board is electrically broken) vs a copper-POUR artifact (two filled
    # GROUND regions of the same net not tied together -- every pad is still
    # connected; it is a floating-copper/EMI nit, not a missing connection).
    # A ratsnest item is a pour island only when EVERY endpoint is a Zone.
    unconn = rep.get("unconnected_items", [])
    pad_unconn, zone_islands = 0, 0
    for u in unconn:
        items = u.get("items", [])
        if items and all("Zone" in (it.get("description") or "") for it in items):
            zone_islands += 1
        else:
            pad_unconn += 1
    return {"drc_parsed": True, "drc_report": str(report),
            "drc_violations": len(rep.get("violations", [])),
            "drc_errors": len(errors),
            "drc_error_descriptions": [v.get("description") for v in errors][:10],
            "drc_unconnected": pad_unconn,          # REAL missing pin connections
            "drc_zone_islands": zone_islands,        # floating ground-pour regions
            "drc_unconnected_total": len(unconn)}


def _gate_and_heal(out_dir, base, kicad_cli, route, info):
    """DRC-gate the freshly written board and heal router sliver artifacts
    (micro-fragments that DRC flags as clearance/shorts on dead copper).
    heal only ever REMOVES fragments. Mutates + returns `info`. Shared by the
    placement-attempt loop and the routing-completeness escalation so both
    gate identically."""
    gate = _drc(out_dir / f"{base}.anvil_pcb", kicad_cli, refill=bool(route))
    info.update(gate)
    if info.get("routing", {}).get("ok") and (gate.get("drc_errors") or 0) > 0:
        try:
            from skidl.board.route.heal import heal_slivers
            for _ in range(4):     # keep healing while it makes progress
                hr = heal_slivers(out_dir / f"{base}.anvil_pcb", kicad_cli,
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
    # kicad-cli refill/heal rewrote the board -- re-stamp so the truth
    # arbitration keeps reading this as machine-generated, not user work.
    try:
        from skidl.board.board_setup import stamp_board_fingerprint
        stamp_board_fingerprint(base, out_dir)
    except Exception:
        pass
    return info


def run_pcb_job(base: str, out_dir, kicad_cli: str, layers: int = None,
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
    # A build writes the board, project and sidecar as a SET; keep them
    # together so a restored `best` board is never paired with an
    # escalated (different layers/size) project or sidecar.
    _snap_exts = (".anvil_pcb", ".anvil_pro", ".board_config.json")

    def _consider(info):
        """Keep the fewest-flaw result AND snapshot its full output set, so
        what is left on disk always matches `best` (later escalation runs
        overwrite these files)."""
        nonlocal best
        if best is None or flaws(info) < flaws(best):
            best = info
            import shutil
            for ext in _snap_exts:
                f = out_dir / f"{base}{ext}"
                if f.is_file():
                    shutil.copy(f, out_dir / f"{base}.best{ext}")

    # The user's own class rules BEFORE any attempt -- the uniform-width
    # fallback narrows Power/USB; if that attempt wins, the change is
    # recorded as an engine override, never passed off as the user's.
    try:
        from skidl.board.pcb_writer import read_project_rules as _rpr
        _pre_classes = _rpr(out_dir / f"{base}.anvil_pro")
    except Exception:
        _pre_classes = {}

    for i, (placement, passes, extra_classes) in enumerate(attempts, 1):
        label = placement + (" uniform-width" if extra_classes else "")
        _log(out_dir, base, f"attempt {i}/{len(attempts)}: {label} "
                            f"placement, route budget {budget}s")
        try:
            info = build_pcb(out_dir / f"{base}.net", out_dir=out_dir,
                             name=base, layers=layers, route=route,
                             passes=passes, route_timeout_s=budget,
                             placement=placement, net_classes=extra_classes,
                             progress_file=out_dir / f"{base}.pcb_build.log",
                             kicad_cli=kicad_cli)
        except Exception as exc:
            import traceback
            _log(out_dir, base, f"attempt {i} crashed: {exc!r}")
            info = {"ok": False, "error": repr(exc),
                    "trace": traceback.format_exc()[-600:]}
        if not info.get("ok"):
            best = best or info
            continue
        info = _gate_and_heal(out_dir, base, kicad_cli, route, info)
        _log(out_dir, base,
             f"attempt {i}: routed={info.get('routing', {}).get('ok')} "
             f"drc_errors={info.get('drc_errors')} "
             f"unconnected={info.get('drc_unconnected')}")
        # Archive each attempt's artifacts -- forensics on attempt 1
        # after attempt 2 overwrote it used to cost a full re-run.
        import shutil
        for ext in (".anvil_pcb", ".drc.json", ".ses", ".dsn"):
            src = out_dir / f"{base}{ext}"
            if src.is_file():
                shutil.copy(src, out_dir / f"{base}.attempt{i}{ext}")
        if extra_classes and flaws(info) == 0:
            info["placement_retry"] = ("board too dense for widened "
                                       "Power-class tracks -- routed with "
                                       "uniform width")
        _consider(info)
        if info.get("drc_parsed") and flaws(info) == 0:
            break                    # clean -- stop trying

    # ---- DYNAMIC ROUTING-COMPLETENESS ESCALATION ----------------------
    # When the best placement still leaves DRC errors or unconnected nets,
    # spend the CHEAP levers first and the expensive one last (policy):
    #   1) more router budget at the same layers/size,
    #   2) grow the AUTO-SIZED board for more routing room (skipped when the
    #      size is user/enclosure-fixed -- growing it would break that),
    #   3) only then escalate to 4 layers (a real fab-cost step).
    # Each stage runs only while the board is not yet clean; the fewest-flaw
    # result wins. Fully generic -- density, not any one circuit, drives it.
    if route and best and best.get("ok") and flaws(best) > 0:
        import json as _json
        sc_path = out_dir / f"{base}.board_config.json"

        def _sidecar():
            try:
                return _json.loads(sc_path.read_text(encoding="utf-8"))
            except Exception:
                return {}

        def _write_sidecar(d):
            sc_path.write_text(_json.dumps(d, indent=2), encoding="utf-8")

        def _esc_run(layers_n, budget_n):
            info = build_pcb(out_dir / f"{base}.net", out_dir=out_dir,
                             name=base, layers=layers_n, route=route,
                             passes=PASSES * 3, route_timeout_s=budget_n,
                             progress_file=out_dir / f"{base}.pcb_build.log",
                             kicad_cli=kicad_cli)
            if info.get("ok"):
                info = _gate_and_heal(out_dir, base, kicad_cli, route, info)
            _consider(info)
            return info

        base_sc = _sidecar()                 # baseline config; each stage
        user_sized = bool(base_sc.get("board_width") or base_sc.get("board_height")
                          or base_sc.get("mechanical"))
        cur_layers = int(base_sc.get("layers") or layers or 2)
        big_budget = min(5400, int(budget * 1.8))
        escalation = []

        # Each stage is INDEPENDENT (applied to the BASELINE, not stacked) so
        # the single best change wins -- stacking grow+layers polluted the most
        # reliable lever. _consider() keeps the fewest-flaw output set.
        # 1) more router budget, same layers/size
        _log(out_dir, base, f"escalation 1/3: +router budget ({big_budget}s), "
                            f"{cur_layers} layers")
        escalation.append("budget")
        _write_sidecar(base_sc)
        _esc_run(cur_layers, big_budget)

        # 2) grow the auto-sized board (more routing room), same layers
        if flaws(best) > 0 and not user_sized:
            bd = best.get("board_dimensions") or {}
            w, h = bd.get("width_mm"), bd.get("height_mm")
            if w and h:
                sc2 = dict(base_sc)
                sc2["board_width"] = round(float(w) * 1.3, 1)
                sc2["board_height"] = round(float(h) * 1.3, 1)
                # Provenance: the ENGINE grew the board, not the user.
                sc2["_sources"] = dict(base_sc.get("_sources") or {})
                sc2["_sources"].update({"board_width": "engine-escalation",
                                        "board_height": "engine-escalation"})
                sc2["engine_overrides"] = dict(
                    base_sc.get("engine_overrides") or {})
                sc2["engine_overrides"]["board_size"] = {
                    "was": [w, h],
                    "now": [sc2["board_width"], sc2["board_height"]],
                    "was_source": (base_sc.get("_sources") or {})
                    .get("board_width", "ai"),
                    "reason": "auto-sized board grown 1.3x for routing room"}
                _write_sidecar(sc2)
                _log(out_dir, base, f"escalation 2/3: grow board -> "
                                    f"{sc2['board_width']}x{sc2['board_height']}mm")
                escalation.append(f"grow_{sc2['board_width']}x{sc2['board_height']}")
                _esc_run(cur_layers, big_budget)

        # 3) LAST RESORT: 4 layers (fab-cost step), from the BASELINE size
        if flaws(best) > 0 and cur_layers < 4:
            sc3 = dict(base_sc)
            sc3["layers"] = 4
            # Provenance: the ENGINE bumped the layer count (a real
            # fab-cost step) -- recorded durably so next session's reads
            # never mislabel it as the user's own choice.
            sc3["_sources"] = dict(base_sc.get("_sources") or {})
            sc3["_sources"]["layers"] = "engine-escalation"
            sc3["engine_overrides"] = dict(base_sc.get("engine_overrides")
                                           or {})
            sc3["engine_overrides"]["layers"] = {
                "was": cur_layers, "now": 4,
                "was_source": (base_sc.get("_sources") or {})
                .get("layers", "ai"),
                "reason": "2 layers could not route clean"}
            _write_sidecar(sc3)
            _log(out_dir, base, "escalation 3/3: 4 layers (2 could not route clean)")
            escalation.append("layers_4")
            _esc_run(4, big_budget)

        best["escalation"] = escalation
        best["escalation_result"] = (
            f"flaws {flaws(best)} after: {', '.join(escalation)}")
        _log(out_dir, base,
             f"escalation done: {escalation} -> flaws={flaws(best)}")

    # The winning result may have been an earlier run; the last escalation
    # write is not necessarily it. Restore the best output SET so the board,
    # project and sidecar on disk all match `best` (verify_board reads them).
    if best:
        import shutil
        for ext in _snap_exts:
            snap = out_dir / f"{base}.best{ext}"
            if snap.is_file():
                shutil.copy(snap, out_dir / f"{base}{ext}")
                snap.unlink(missing_ok=True)
        # The restore rewrote the board file -- re-stamp the fingerprint
        # and snapshot the delivered state (the baseline the next session
        # diffs against to explain the user's manual changes).
        try:
            from skidl.board.board_setup import (stamp_board_fingerprint,
                                                 write_state_snapshot)
            stamp_board_fingerprint(base, out_dir)
            write_state_snapshot(base, out_dir)
        except Exception:
            pass

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
                v = verify_board(out_dir / f"{base}.anvil_pcb",
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

    # A routed, pin-complete board may still carry a few FLOATING GROUND
    # regions the fill+heal couldn't tie in (island surrounded by signals /
    # the edge). Every PIN is connected -- this is an EMI/DFM nit, not a
    # broken net -- so it does NOT fail the board; surface it as a note so the
    # user can drop a manual ground via if they care.
    islands = res.get("drc_zone_islands", 0)
    if res.get("ok") and res.get("status") == "routed" and islands:
        res["ground_islands"] = islands
        res["ground_islands_note"] = (
            f"{islands} floating ground-pour region(s) remain (every PIN is "
            "connected; this is an EMI/DFM nit). Heal added ground vias where a "
            "clear spot existed; the rest are edge/signal-locked -- drop a "
            "manual GND via there if desired.")

    # Uniform-width fallback won: the routed copper is NARROWER than the
    # project's Power/USB class widths -- record the engine override
    # durably (restoring the wide values against thin tracks would
    # manufacture DRC violations; the honest fix is the record).
    if res.get("placement_retry") and _pre_classes:
        changed_cls = {}
        for cname in ("Power", "USB"):
            was = (_pre_classes.get(cname) or {}).get("width")
            if was and abs(float(was) - 0.25) > 1e-6:
                changed_cls[cname] = {"width": {"was": was, "now": 0.25}}
        if changed_cls:
            try:
                scp = out_dir / f"{base}.board_config.json"
                sc = json.loads(scp.read_text(encoding="utf-8")) \
                    if scp.is_file() else {}
                ov = sc.get("engine_overrides") or {}
                ov["net_classes"] = {"classes": changed_cls,
                                     "was_source": "project-file",
                                     "reason": res["placement_retry"]}
                sc["engine_overrides"] = ov
                scp.write_text(json.dumps(sc, indent=2) + "\n",
                               encoding="utf-8")
            except Exception:
                pass

    # CONFIG-CONFORMANCE VERIFY: the built board vs the configured setup
    # (layers / size / holes / widths / pour). DRC-clean says nothing
    # about conformance; a mismatch with NO recorded engine override is a
    # pipeline bug and fails the build.
    try:
        from skidl.board.conformance import check_config_conformance
        conf = check_config_conformance(base, out_dir)
        res["config_conformance"] = conf
        if res.get("ok") and res.get("status") == "routed" \
                and conf.get("ok") is False:
            res.update(ok=False, status="config_mismatch",
                       error=("built board does not match the configured "
                              f"setup ({', '.join(conf.get('mismatches', []))})"
                              " -- pipeline bug, do not use"))
    except Exception as exc:
        res["config_conformance"] = {"ok": None, "error": repr(exc)}

    (out_dir / f"{base}.pcb_result.json").write_text(
        json.dumps(res, indent=2, default=str) + "\n", encoding="utf-8")
    _log(out_dir, base, f"DONE: {res.get('status')}")
    return res


if __name__ == "__main__":
    base, out_dir, cli = sys.argv[1], sys.argv[2], sys.argv[3]
    layers = int(sys.argv[4]) if len(sys.argv) > 4 else None
    route = (sys.argv[5].lower() != "false") if len(sys.argv) > 5 else True
    run_pcb_job(base, out_dir, cli, layers=layers, route=route)
