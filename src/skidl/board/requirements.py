"""
src/skidl/board/requirements.py

Design-aware PCB requirements questionnaire -- the "ask the user FIRST"
half of the learn-first contract. Before create_pcb builds anything for
a project the user never initialized, this module analyzes the actual
netlist and the files already on disk and produces ONLY the questions
this design needs and the user hasn't already answered somewhere
(.anvil_pro Board Setup, existing board, system defaults, prior init).

Every question is mechanically actionable: `answer_via` names the exact
initialize_pcb_project parameter(s) that submit the answer, and
`skip_means` defines exactly what a skip does -- no room for the
client model to improvise. Engine limitations are DISCLOSED (statements,
never questions): the engine must not collect requirements it cannot
honor.

Read-only: never writes any file.
"""

from __future__ import annotations
import re
from pathlib import Path

# Hard cap so the client has a bounded conversation. The catalog below
# has exactly 7 entries, ordered by impact; if it ever grows past the
# cap, the overflow is reported in "questions_dropped", never silent.
MAX_QUESTIONS = 7

_CONNECTOR_REF_RE = re.compile(r"^[JP]\d+$", re.I)
_CONNECTOR_FP_RE = re.compile(
    r"(Connector|USB|TerminalBlock|PinHeader|PinSocket|Jack|Molex|JST)", re.I)
_GROUND_RE = re.compile(r"^(GND\w*|AGND|DGND|PGND|VSS\w*|EARTH)$", re.I)


def engine_limitations(usb_nets=()):
    """What the engine CANNOT honor -- disclosed verbatim to the user,
    never asked about. Also returned by initialize_pcb_project's soft
    confirmation so the 'Proceed?' decision is made with eyes open."""
    return [
        "Board outline is an axis-aligned RECTANGLE only -- a round/"
        "chamfered/angled board needs its Edge.Cuts drawn in KiCad after "
        "generation.",
        "All components are placed on the TOP side; there is no "
        "bottom-side placement control.",
        "No controlled impedance, no differential-pair routing, no "
        "length matching -- signal integrity is NOT verified."
        + (f" USB D+/D- detected ({', '.join(sorted(usb_nets))}): they "
           "get a narrow 'USB' net class by NAME PATTERN only, not "
           "controlled impedance." if usb_nets else ""),
        "Mounting holes are limited to M2/M2.5/M3/M4.",
        "Track-width-vs-current figures are IPC-2221 fitted-curve "
        "ESTIMATES (public IPC-2152 stand-in), not verified capacity.",
        "EMC, thermal and creepage/clearance-for-isolation are NOT "
        "verified by the pipeline.",
    ]


def _q(qid, question, why, options, default_if_skipped, skip_means,
       evidence, params):
    return {
        "id": qid,
        "question": question,
        "why_it_matters": why,
        "options": options,
        "default_if_skipped": default_if_skipped,
        "skip_means": skip_means,
        "detected_evidence": evidence,
        "answer_via": {"tool": "initialize_pcb_project", "params": params},
    }


def generate_questionnaire(base: str, out_dir) -> dict:
    """Build the questionnaire for THIS design. Suppresses any question
    the resolved config already answers from a USER source (the rule
    ladder: argument / .anvil_pro / .anvil_pcb / system defaults) --
    only what would otherwise silently fall to AI/profile defaults is
    asked. Returns {questions, limitations, already_answered,
    instruction}."""
    out_dir = Path(out_dir)
    from skidl.board.rule_discovery import resolve_board_config
    from skidl.board.pcb_writer import _read_netlist
    from skidl.board.width_engine import estimate_net_current
    from skidl.schematics.net_classify import (classify_net_function,
                                               classify_net_role)
    from skidl.board.project_init import _NetName, _has_fine_pitch_parts

    resolved = resolve_board_config(base, out_dir)
    comps, nets = _read_netlist(out_dir / f"{base}.net")

    def answered(key):
        return resolved.get(key, {}).get("status") == "confirmed"

    # ---- netlist analysis (evidence, not decisions) --------------------
    usb_nets, current_candidates = [], []
    declared = resolved.get("currents", {})
    for net_name, info in nets.items():
        n = _NetName(net_name)
        if classify_net_function(n) == "usb_diff":
            usb_nets.append(net_name)
        if _GROUND_RE.match(net_name or ""):
            continue    # plane-served; a current figure is meaningless
        if declared.get(net_name, {}).get("status") == "confirmed":
            continue    # the user already declared this one
        cls = ("Power" if classify_net_function(n) == "power"
               or classify_net_role(n) is not None else info["class"])
        amps, basis = estimate_net_current(net_name, cls)
        if amps >= 1.0:
            current_candidates.append((net_name, amps, basis))

    connectors = sorted(
        ref for ref, c in comps.items()
        if _CONNECTOR_REF_RE.match(ref)
        or _CONNECTOR_FP_RE.search(c.get("footprint") or ""))

    try:
        fine_pitch = _has_fine_pitch_parts(out_dir / f"{base}.net")
    except Exception:
        fine_pitch = False

    # ---- questions (impact order; each only when NOT already answered) --
    questions = []

    if not answered("board_size"):
        questions.append(_q(
            "board_size",
            "Must the board fit an enclosure or product? If so, what are "
            "the exact width x height in mm (and does it need mounting-"
            "hole positions / connector edges from the enclosure)?",
            "Otherwise the outline is auto-sized to the tightest rectangle "
            "around the placed parts, which rarely matches a real product.",
            ["auto-size from parts", "exact W x H in mm",
             "full enclosure spec (size + hole positions + connector edges)"],
            "auto-sized tightest rectangle",
            "omit board_width/board_height; the outline auto-sizes from "
            "the placed parts (it can only GROW during routing escalation)",
            "no board dimensions found in any user source",
            ["board_width", "board_height", "mechanical"]))

    if current_candidates:
        ev = "; ".join(f"{n} ~{a} A ({b})" for n, a, b in
                       sorted(current_candidates, key=lambda t: -t[1]))
        questions.append(_q(
            "power_currents",
            "These nets look like they carry real current -- what are the "
            "actual maximum amps for each?",
            "Track widths follow the IPC-2152-informed plan; a wrong "
            "current means an under- or over-sized track. Without your "
            "numbers only name-based heuristic ESTIMATES are used.",
            ['declare per net: currents={"NET": amps}',
             "skip -- use the advisory heuristic estimates"],
            "advisory heuristic estimates (unconfirmed)",
            "omit the currents parameter; widths use the name-based "
            "heuristic estimates shown in the evidence, labeled unconfirmed",
            ev, ["currents"]))

    mech_val = resolved.get("mechanical", {}).get("value") or {}
    if connectors and not (answered("mechanical")
                           and mech_val.get("connectors")):
        questions.append(_q(
            "connector_edges",
            f"Connectors detected: {', '.join(connectors)}. Should any of "
            "them exit a specific board edge (left/right/top/bottom), e.g. "
            "to match a case cutout?",
            "Connector positions define how the product is plugged in; "
            "the placer otherwise guesses input-side left, output-side "
            "right.",
            ["specify edge per connector "
             '(mechanical={"connectors": [{"ref": "J1", "edge": "left"}]})',
             "let the placer choose"],
            "placer picks edges (input left, output right)",
            "omit mechanical.connectors; the placement engine chooses "
            "edges from signal direction",
            f"connectors on this design: {', '.join(connectors)}",
            ["mechanical"]))

    if not answered("layers"):
        questions.append(_q(
            "layers",
            "How many copper layers -- 2 or 4?",
            "Layer count is a real fabrication-cost step and fixes the "
            "routing strategy (4+ layers get dedicated ground/power "
            "planes).",
            ["2", "4"],
            "2 layers",
            "omit the layers parameter; 2 layers are used and the engine "
            "only escalates to 4 as a last resort when 2 cannot route "
            "clean",
            "no layer count found in any user source",
            ["layers"]))

    if not answered("manufacturer"):
        questions.append(_q(
            "manufacturer",
            "Which board manufacturer (their minimum track/clearance/via "
            "rules apply)?",
            "Design rules below the fab's real minimums fail DRC at order "
            "time; each profile carries that fab's published minimums.",
            ["jlcpcb", "pcbway", "generic (conservative)"],
            "jlcpcb profile rules",
            "omit the manufacturer parameter; the jlcpcb profile is used",
            "no manufacturer recorded for this project",
            ["manufacturer"]))

    if not answered("mounting_holes"):
        questions.append(_q(
            "mounting_holes",
            "Does the board need mounting holes, and which screw size?",
            "Holes must be placed before routing -- adding them later "
            "forces a re-route.",
            ["M2", "M2.5", "M3", "M4", "none"],
            "no mounting holes",
            "omit the mounting_holes parameter; no holes are added "
            "(only M2/M2.5/M3/M4 are supported)",
            "no mounting-hole choice found in any user source",
            ["mounting_holes"]))

    # ipc_class: ALWAYS asked (ranked last) unless previously answered --
    # a vehicle/industrial product is typically Class 2 and the user is
    # the only one who knows the product's reliability requirement.
    if resolved.get("ipc_class", {}).get("value") is None:
        questions.append(_q(
            "ipc_class",
            "Is there a reliability requirement -- IPC product class 1, 2 "
            "or 3? (Class 2 is typical for vehicle/industrial products; "
            "class 3 for life-critical.)",
            "The class derives clearance, annular-ring and drill floors "
            "plus mask expansion. Skipping applies the plain manufacturer "
            "profile with no reliability floors.",
            ["IPC Class 1", "IPC Class 2", "IPC Class 3",
             "I don't know / skip"],
            "plain manufacturer profile",
            "omit the ipc_class parameter entirely",
            ("fine-pitch (<0.65mm) footprints detected -- note: Class 3 "
             "floors can conflict with fine-pitch routing" if fine_pitch
             else "no fine-pitch parts detected"),
            ["ipc_class"]))

    dropped = [q["id"] for q in questions[MAX_QUESTIONS:]]
    questions = questions[:MAX_QUESTIONS]

    # ---- limitations: DISCLOSED, never asked about ---------------------
    limitations = engine_limitations(usb_nets)

    # ---- what the user's own files already answered --------------------
    already = {}
    for key, entry in resolved.items():
        if key == "rules":
            for rk, re_ in entry.items():
                if re_["status"] == "confirmed":
                    already[f"rules.{rk}"] = {"value": re_["value"],
                                              "source": re_["source"]}
        elif key == "currents":
            for net, ce in entry.items():
                if ce["status"] == "confirmed":
                    already[f"currents.{net}"] = {"value": ce["amps"],
                                                  "source": ce["source"]}
        elif isinstance(entry, dict) and entry.get("status") == "confirmed":
            already[key] = {"value": entry["value"],
                            "source": entry["source"]}

    out = {
        "ok": True,
        "questions": questions,
        "limitations": limitations,
        "already_answered": already,
        "instruction": (
            "Ask the user ONLY these questions -- everything else was "
            "already answered by their own files (see already_answered). "
            "Show the limitations list verbatim. Submit the answers via "
            "initialize_pcb_project: each question's answer_via names the "
            "exact parameter; a skipped question follows its skip_means. "
            "Pass requirements_asked=[the ids you actually asked]."),
    }
    if dropped:
        out["questions_dropped"] = dropped
    return out
