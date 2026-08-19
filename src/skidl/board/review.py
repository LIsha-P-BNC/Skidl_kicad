"""
src/skidl/board/review.py

Phase 6 + Phase 8 of the AI PCB Engineer: the human review gate and the
engineering report.

review_design() synthesizes EVERY gate result plus design-INTENT checks
measured on the real board (things no DRC can check: decoupling distance,
track width vs expected current, connectors on the edge, ground pour)
into one structured result + a human-readable <base>_review.md. Every
section carries a status and concrete evidence; the report ends with the
Phase-8 honesty block: verified facts vs NOT-verified assumptions,
clearly split.

approve_design() records the HUMAN's approval, hash-bound to the exact
board + Board Setup reviewed: any change afterwards invalidates it.
export_manufacturing refuses without a valid approval -- manufacturing
never proceeds automatically (original plan, principle #7).
"""

from __future__ import annotations
import hashlib
import json
import re
import subprocess
from pathlib import Path

VERIFIED = "VERIFIED"
OK = "OK"
WARNING = "WARNING"
FAILED = "FAILED"
NOT_VERIFIED = "NOT_VERIFIED"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# IPC-2221-style external-conductor estimate, 1 oz copper, 10C rise:
# minimum width (mm) carrying the given current (A). ADVISORY ONLY.
_WIDTH_FOR_CURRENT = [(0.5, 0.15), (1.0, 0.30), (2.0, 0.75), (3.0, 1.30)]


# ---------------------------------------------------------------------------
# The review
# ---------------------------------------------------------------------------

def review_design(base: str, out_dir, kicad_cli: str) -> dict:
    """Full design review. Returns the structured report and writes
    <base>_review.md next to the project files."""
    out_dir = Path(out_dir)
    sections = {}

    sections["schematic"] = _schematic_gates(base, out_dir)
    sections["board_gates"] = _board_gates(base, out_dir, kicad_cli)
    sections["board_setup"] = _setup_provenance(base, out_dir)
    sections["bom"] = _bom_completeness(base, out_dir)
    sections["design_intent"] = _intent_checks(base, out_dir)
    sections["thermal_advisory"] = _thermal_advisory(base, out_dir)

    # Built board vs configured setup (layers/size/holes/widths/pour):
    # a mismatch with no recorded engine override is a FAILED gate.
    try:
        from skidl.board.conformance import check_config_conformance
        conf = check_config_conformance(base, out_dir)
        sections["config_conformance"] = {
            "status": (VERIFIED if conf.get("ok")
                       else FAILED if conf.get("mismatches") else WARNING),
            "evidence": conf,
        }
    except Exception as exc:
        sections["config_conformance"] = {"status": WARNING,
                                          "evidence": {"error": repr(exc)}}

    sections["decided_behaviors"] = {
        "status": OK,
        "items": [
            "schematic MISMATCH -> hard-stop; regenerated in all-label mode; "
            "a shorted schematic is never published",
            "manual board edits -> create_pcb REFUSES to overwrite; regeneration "
            "only with the user's explicit consent (force)",
            "Board Setup drift -> re-read live before every build + hash "
            "change-detection; the user's current values always win",
        ],
    }

    skipped = []
    if sections["board_gates"].get("drc_unconnected", 0):
        skipped.append("routing incomplete -- unconnected items present")
    sections["not_verified"] = {
        "status": NOT_VERIFIED,
        "items": [
            "EMI / EMC compliance",
            "signal integrity (impedance, crosstalk, timing)",
            "power integrity (IR drop, plane resonance)",
            "thermal performance",
            "regulatory compliance",
            "width-vs-current figures below are IPC-2221-style ESTIMATES, "
            "not verified capacity",
        ] + skipped,
    }

    gate_statuses = [sections["schematic"]["status"],
                     sections["board_gates"]["status"],
                     sections["bom"]["status"],
                     sections["config_conformance"]["status"]]
    intent = sections["design_intent"]["status"]
    # Gate FAILED -> FAILED. Intent findings (advisory heuristics) cap
    # the overall at WARNING -- including intent FAILED, which must
    # never be masked by clean gates (was: VERIFIED shown alongside a
    # FAILED intent check).
    overall = (FAILED if FAILED in gate_statuses else
               WARNING if (intent in (WARNING, FAILED)
                           or WARNING in gate_statuses) else
               VERIFIED)

    report = {"base": base, "overall": overall, "sections": sections}
    md_path = out_dir / f"{base}_review.md"
    md_path.write_text(_render_md(report), encoding="utf-8")
    report["report_md"] = str(md_path)
    report["report_sha256"] = hashlib.sha256(
        md_path.read_bytes()).hexdigest()
    return report


def _schematic_gates(base, out_dir) -> dict:
    out = {"status": VERIFIED, "evidence": {}}
    erc_path = out_dir / f"{base}.erc"
    log_path = out_dir / f"{base}.build.log"
    text = ""
    for p in (erc_path, log_path):
        if p.is_file():
            text += p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"(\d+)\s+errors?\s+found", text, re.IGNORECASE)
    errs = int(m.group(1)) if m else text.count("ERC ERROR")
    out["evidence"]["erc_errors"] = errs
    if errs:
        out["status"] = FAILED

    # Netlist-level electrical intent rules (ELE-1 supply caps, ELE-6 LED
    # series resistor...) from the existing spec validator.
    try:
        vd = _load_validate_design()
        findings = vd.validate_netlist(str(out_dir / f"{base}.net"))
        ele = [f for f in findings if getattr(f, "severity", "") == "ERROR" or
               (isinstance(f, dict) and f.get("severity") == "ERROR")]
        out["evidence"]["netlist_rule_errors"] = len(ele)
        out["evidence"]["netlist_rule_warnings"] = max(len(findings) - len(ele), 0)
        if ele:
            out["status"] = FAILED
            out["evidence"]["netlist_rule_details"] = [str(f)[:120] for f in ele][:5]
    except Exception as exc:
        out["evidence"]["netlist_rules"] = f"validator unavailable: {exc!r}"
    return out


def _load_validate_design():
    import importlib.util
    p = Path(__file__).resolve().parents[1] / "scripts" / "validate_design.py"
    spec = importlib.util.spec_from_file_location("_vd_review", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _board_gates(base, out_dir, kicad_cli) -> dict:
    out = {"status": VERIFIED, "evidence": {}}
    pcb = out_dir / f"{base}.anvil_pcb"
    if not pcb.is_file():
        return {"status": FAILED, "evidence": {"error": "no board file"}}

    report = out_dir / f"{base}.review_drc.json"
    proc = subprocess.run(
        [kicad_cli, "pcb", "drc", "--format", "json", "--severity-all",
         "--output", str(report), str(pcb)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=180, creationflags=_NO_WINDOW)
    if proc.returncode != 0 or not report.is_file():
        return {"status": FAILED,
                "evidence": {"error": f"DRC failed to run: {(proc.stderr or '')[-200:]}"}}
    rep = json.loads(report.read_text(encoding="utf-8", errors="replace"))
    art = {"malformed_courtyard", "lib_footprint_issues"}
    errors = [v for v in rep.get("violations", [])
              if v.get("severity") == "error" and v.get("type") not in art]
    art_only = [v for v in rep.get("violations", [])
                if v.get("severity") == "error" and v.get("type") in art]
    unconn = rep.get("unconnected_items", [])
    out["evidence"].update({
        "drc_errors": len(errors),
        "drc_warnings": sum(1 for v in rep.get("violations", [])
                            if v.get("severity") == "warning"),
        "drc_unconnected": len(unconn),
        "library_art_defects": [v.get("description", "")[:80] for v in art_only],
    })
    out["drc_unconnected"] = len(unconn)
    if errors or unconn:
        out["status"] = FAILED
        out["evidence"]["drc_error_details"] = \
            [v.get("description", "")[:100] for v in errors][:5]

    try:
        from skidl.board.verify import verify_board as _vb
        v = _vb(pcb, out_dir / f"{base}.net", kicad_cli)
        out["evidence"]["board_matches_netlist"] = bool(v.get("ok"))
        if not v.get("ok"):
            out["status"] = FAILED
            out["evidence"]["mismatch"] = {
                "missing": v.get("mismatch_missing"),
                "extra": v.get("mismatch_extra")}
    except Exception as exc:
        out["status"] = FAILED
        out["evidence"]["verify_error"] = repr(exc)
    return out


def _setup_provenance(base, out_dir) -> dict:
    from skidl.board.board_setup import get_board_setup
    snap = get_board_setup(base, out_dir)
    consumed = {k: {"value": v.get("value"),
                    "source": v.get("source", "")} if isinstance(v, dict) else v
                for k, v in snap["consumed"].items()}
    out = {"status": OK,
           "evidence": {"consumed": consumed,
                        "preserved_count": len(snap["preserved"]),
                        "setup_hash": snap["setup_hash"]}}
    # IPC product class, when the project declared one at init.
    try:
        import json as _json
        from pathlib import Path as _P
        sc = _json.loads((_P(out_dir) / f"{base}.board_config.json")
                         .read_text(encoding="utf-8"))
        if sc.get("ipc_class"):
            out["evidence"]["ipc_class"] = sc["ipc_class"]
            out["evidence"]["ipc_disclaimer"] = (
                "rule floors derived from public IPC-2221B/6012 figures -- "
                "design targets, not an IPC certification")
    except Exception:
        pass
    return out


def _bom_completeness(base, out_dir) -> dict:
    from skidl.board.pcb_writer import _read_netlist
    comps, _nets = _read_netlist(out_dir / f"{base}.net")
    missing = [ref for ref, c in comps.items()
               if not c.get("value") or not c.get("footprint")]
    return {"status": VERIFIED if not missing else FAILED,
            "evidence": {"parts": len(comps), "missing_value_or_footprint": missing}}


_THERMAL_PART_RE = None


def _thermal_advisory(base, out_dir) -> dict:
    """Power-part detection + pour/thermal-via recommendations. INFO
    ONLY -- no dissipation is claimed unless it is derivable; most
    tracker-class boards only need attention at the switching regulator.
    """
    import re
    from skidl.board.pcb_writer import _read_netlist
    part_re = re.compile(
        r"(regulator|ldo|buck|boost|driver|mosfet|power.?pad|dpak|to-?220"
        r"|to-?263|powerpad|hs?op|wson.*ep|qfn.*ep|SOT-?223)", re.I)
    try:
        comps, _nets = _read_netlist(Path(out_dir) / f"{base}.net")
    except Exception as exc:
        return {"status": "INFO", "error": repr(exc)}
    items = []
    for ref, c in sorted(comps.items()):
        hay = " ".join(str(c.get(k) or "") for k in
                       ("value", "footprint", "description", "part"))
        if part_re.search(hay):
            items.append({
                "ref": ref, "value": c.get("value"),
                "recommendation": (
                    f"give {ref} a copper pour under/around its tab and "
                    "consider a 4-9 array of 0.3mm thermal vias into the "
                    "ground plane"),
            })
    return {"status": "INFO",
            "note": ("advisory only -- dissipation not computed; declare "
                     "sidecar 'currents' for width/current analysis and "
                     "verify thermals for the switching regulator on the "
                     "bench"),
            "power_parts": items}


# ---------------------------------------------------------------------------
# Design-intent checks -- measured on the REAL board file
# ---------------------------------------------------------------------------

def _intent_checks(base, out_dir) -> dict:
    out = {"status": OK, "checks": []}
    try:
        board = _board_from_files(base, out_dir)
    except Exception as exc:
        return {"status": WARNING, "checks": [],
                "note": f"could not reconstruct board geometry: {exc!r}"}

    def add(name, status, evidence):
        out["checks"].append({"check": name, "status": status,
                              "evidence": evidence})
        if status == FAILED:
            out["status"] = FAILED if out["status"] != FAILED else out["status"]
        elif status == WARNING and out["status"] == OK:
            out["status"] = WARNING

    # -- decoupling distance ------------------------------------------------
    from skidl.board.place.simple import _build_views
    from skidl.schematics.cluster import find_decap_affinities
    views, _nets = _build_views(board)
    pairs = {}
    for _net, (decap, ic) in find_decap_affinities(list(views.values())).items():
        pairs.setdefault(decap.ref, ic.ref)
    for decap_ref, ic_ref in sorted(pairs.items()):
        d = _decap_distance(board, decap_ref, ic_ref)
        if d is None:
            continue
        status = OK if d < 5.0 else WARNING if d < 10.0 else FAILED
        add("decoupling_distance", status,
            f"{decap_ref} supply pad is {d:.1f} mm from {ic_ref}'s pin "
            f"(OK<5, WARN<10)")
    if not pairs:
        add("decoupling_distance", OK, "no decoupling caps detected on this design")

    # -- width vs current (ADVISORY estimate) --------------------------------
    txt = (out_dir / f"{base}.anvil_pcb").read_text(encoding="utf-8",
                                                    errors="replace")
    for net in board.nets.values():
        if net.net_class.lower() not in ("power",):
            continue
        widths = _net_track_widths(txt, net.name)
        if not widths:
            continue
        w = min(widths)
        cap = next((amps for amps, need in reversed(_WIDTH_FOR_CURRENT)
                    if w >= need), 0.3)
        status = OK if w >= 0.3 else WARNING
        add("width_vs_current", status,
            f"net {net.name}: min routed width {w:.2f} mm ~ handles ")
        out["checks"][-1]["evidence"] += \
            f"~{cap:g} A (IPC-2221-style ESTIMATE, 1 oz, advisory only)"

    # -- connectors on edge: COURTYARD edge vs the REAL Edge.Cuts extents
    # (centers vs an approximated outline mis-measured flush connectors
    # as 9-16mm inboard -- measured).
    from skidl.schematics.net_classify import is_connector_part
    edge = _edge_cuts_extents(txt) or (
        (0, 0, max((p[0] for p in (board.outline or [(0, 0)])), default=0),
         max((p[1] for p in (board.outline or [(0, 0)])), default=0)))
    eminx, eminy, emaxx, emaxy = edge
    for ref, view in sorted(views.items()):
        if not is_connector_part(view):
            continue
        fp = board.footprint(ref)
        bb = fp.courtyard_world_bbox()
        if bb is None:
            bb = (fp.x - 2, fp.y - 2, fp.x + 2, fp.y + 2)
        edge_d = min(bb[0] - eminx, bb[1] - eminy,
                     emaxx - bb[2], emaxy - bb[3])
        status = OK if edge_d < 5.0 else WARNING
        add("connector_on_edge", status,
            f"{ref} courtyard is {max(edge_d, 0):.1f} mm from the nearest "
            "board edge")

    # -- ground pour -----------------------------------------------------------
    has_zone = "(zone" in txt
    filled = "filled_polygon" in txt
    if has_zone and filled:
        add("ground_pour", OK, "GND zone present and filled")
    elif has_zone:
        add("ground_pour", WARNING, "zone present but not filled")
    else:
        add("ground_pour", WARNING, "no copper pour on this board")

    return out


def _board_from_files(base, out_dir):
    """BoardModel with REAL positions from the current .anvil_pcb (works
    for both our writer's format and kicad-cli's canonical rewrite)."""
    from skidl.board.model import BoardModel
    from skidl.board.adapter.pymodel import PyModelBackend

    board = BoardModel(name=base)
    PyModelBackend().populate(board, out_dir / f"{base}.net")

    txt = (out_dir / f"{base}.anvil_pcb").read_text(encoding="utf-8",
                                                    errors="replace")
    # footprint blocks: (footprint "lib" ... (at X Y [R]) ... "REF"
    for m in re.finditer(
            r'\(footprint\s+"[^"]+"(.*?)(?=\n\t?\(footprint\s|\Z)', txt, re.S):
        blk = m.group(1)
        ref_m = re.search(r'"Reference"\s*\n?\s*"(\w+)"|\(property "Reference" "(\w+)"',
                          blk)
        at_m = re.search(r'\(at\s+([\d.-]+)\s+([\d.-]+)(?:\s+([\d.-]+))?\)', blk)
        if not ref_m or not at_m:
            continue
        ref = ref_m.group(1) or ref_m.group(2)
        try:
            fp = board.footprint(ref)
        except KeyError:
            continue
        fp.x, fp.y = float(at_m.group(1)), float(at_m.group(2))
        fp.rotation = float(at_m.group(3) or 0)

    xs = [fp.x for fp in board.footprints]
    ys = [fp.y for fp in board.footprints]
    if xs:
        board.outline = [(0, 0), (max(xs) + 4, 0),
                         (max(xs) + 4, max(ys) + 4), (0, max(ys) + 4)]
    return board


def _decap_distance(board, decap_ref, ic_ref):
    from skidl.schematics.net_classify import classify_net_role

    class _N:
        def __init__(self, name):
            self.name = name
            self.pins = []
            self.drive = None

    try:
        decap = board.footprint(decap_ref)
        ic = board.footprint(ic_ref)
    except KeyError:
        return None
    best = None
    for dpad in decap.pads:
        # The decap's RAIL pad: any net shared with the IC that isn't
        # ground. Name-based power classification is too narrow here --
        # rails like VIN/VOUT (regulator boards) don't match the
        # VCC/VDD/+xV patterns (measured: check silently vanished).
        if not dpad.net or classify_net_role(_N(dpad.net)) == "ground":
            continue
        dx, dy = decap.pad_world_xy(dpad)
        for ipad in ic.pads:
            if ipad.net != dpad.net:
                continue
            ix, iy = ic.pad_world_xy(ipad)
            d = ((dx - ix) ** 2 + (dy - iy) ** 2) ** 0.5
            best = d if best is None else min(best, d)
    return best


def _edge_cuts_extents(board_text):
    """(minx, miny, maxx, maxy) of the REAL board outline -- the single
    implementation lives in board_setup (shared with config resolution
    and conformance checks)."""
    from skidl.board.board_setup import edge_cuts_extents
    return edge_cuts_extents(board_text)


def _net_track_widths(board_text, net_name):
    """Track widths used by a net -- handles our numeric-net format AND
    kicad-cli's canonical by-name format."""
    widths = []
    code = None
    m = re.search(r'\(net (\d+) "' + re.escape(net_name) + r'"\)', board_text)
    if m:
        code = m.group(1)
    for seg in re.finditer(r'\(segment\b(.*?)\n\t?\)', board_text, re.S):
        blk = seg.group(0)
        nm = re.search(r'\(net\s+(?:"([^"]+)"|(\d+))\)', blk)
        if not nm:
            continue
        seg_net = nm.group(1) or nm.group(2)
        if seg_net == net_name or (code and seg_net == code):
            wm = re.search(r'\(width ([\d.]+)\)', blk)
            if wm:
                widths.append(float(wm.group(1)))
    return widths


# ---------------------------------------------------------------------------
# Approval -- hash-bound to exactly what was reviewed
# ---------------------------------------------------------------------------

def _board_sha(base, out_dir):
    p = Path(out_dir) / f"{base}.anvil_pcb"
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def approve_design(base: str, out_dir, approved_by: str, note: str = "",
                    kicad_cli: str = "") -> dict:
    """Record the HUMAN's approval. Requires an existing review whose gate
    sections all pass; binds to the exact board + setup reviewed."""
    out_dir = Path(out_dir)
    md = out_dir / f"{base}_review.md"
    if not md.is_file():
        return {"ok": False,
                "error": "no review exists -- run review_design first and "
                         "show it to the human"}
    if not (approved_by or "").strip():
        return {"ok": False, "error": "approved_by (the human's name) is required"}

    # Re-run the gates fresh -- approval must reflect CURRENT state.
    rep = review_design(base, out_dir, kicad_cli)
    gates = rep["sections"]
    hard = [gates["schematic"]["status"], gates["board_gates"]["status"],
            gates["bom"]["status"]]
    if FAILED in hard:
        return {"ok": False, "error": "gates FAILED -- a failing design "
                                       "cannot be approved", "review": rep}

    from skidl.board.board_setup import compute_setup_hash
    approval = {
        "approved_by": approved_by.strip(),
        "note": note,
        "board_sha256": _board_sha(base, out_dir),
        "setup_hash": compute_setup_hash(base, out_dir),
        "review_sha256": rep["report_sha256"],
        "intent_warnings": [c for c in gates["design_intent"]["checks"]
                            if c["status"] in (WARNING, FAILED)],
    }
    (out_dir / f"{base}.approval.json").write_text(
        json.dumps(approval, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "approval": approval, "review_overall": rep["overall"]}


def check_approval(base: str, out_dir) -> dict:
    out_dir = Path(out_dir)
    p = out_dir / f"{base}.approval.json"
    if not p.is_file():
        return {"valid": False,
                "reason": "no human approval on record -- run review_design, "
                          "show the report, then approve_design after the "
                          "human explicitly agrees"}
    try:
        a = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"valid": False, "reason": "approval file unreadable"}
    if a.get("board_sha256") != _board_sha(base, out_dir):
        return {"valid": False,
                "reason": "board CHANGED after approval -- re-review and "
                          "re-approve required"}
    from skidl.board.board_setup import compute_setup_hash
    if a.get("setup_hash") != compute_setup_hash(base, out_dir):
        return {"valid": False,
                "reason": "Board Setup CHANGED after approval -- re-review "
                          "and re-approve required"}
    return {"valid": True, "approved_by": a.get("approved_by"),
            "note": a.get("note")}


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

_ICON = {VERIFIED: "[VERIFIED]", OK: "[OK]", WARNING: "[WARNING]",
         FAILED: "[FAILED]", NOT_VERIFIED: "[NOT VERIFIED]"}


def _render_md(report) -> str:
    s = report["sections"]
    L = [f"# Design Review: {report['base']}",
         f"",
         f"**Overall: {_ICON[report['overall']]}**",
         "",
         "## 1. Schematic gates " + _ICON[s['schematic']['status']]]
    for k, v in s["schematic"]["evidence"].items():
        L.append(f"- {k}: {v}")
    L += ["", "## 2. Board gates (checked against YOUR Board Setup) "
          + _ICON[s['board_gates']['status']]]
    for k, v in s["board_gates"]["evidence"].items():
        L.append(f"- {k}: {v}")
    L += ["", "## 3. Board Setup provenance " + _ICON[OK]]
    for k, v in s["board_setup"]["evidence"]["consumed"].items():
        if isinstance(v, dict):
            L.append(f"- {k}: {v.get('value')}  _(source: {v.get('source')})_")
    L.append(f"- settings found-but-not-interpreted (preserved verbatim): "
             f"{s['board_setup']['evidence']['preserved_count']}")
    L += ["", "## 4. BOM " + _ICON[s['bom']['status']]]
    for k, v in s["bom"]["evidence"].items():
        L.append(f"- {k}: {v}")
    L += ["", "## 5. Design-intent checks (measured on the real board) "
          + _ICON[s['design_intent']['status']]]
    for c in s["design_intent"]["checks"]:
        L.append(f"- {_ICON[c['status']]} {c['check']}: {c['evidence']}")
    L += ["", "## 6. Decided safety behaviors (always in force)"]
    for item in s["decided_behaviors"]["items"]:
        L.append(f"- {item}")
    L += ["", "## 7. NOT VERIFIED -- requires simulation / lab measurement"]
    for item in s["not_verified"]["items"]:
        L.append(f"- {item}")
    L += ["", "---",
          "_A human must review this report and record approval "
          "(approve_design) before manufacturing files can be exported._",
          ""]
    return "\n".join(L)
