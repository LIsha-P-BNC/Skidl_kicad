# -*- coding: utf-8 -*-

"""
design_intent.py -- the SENIOR ENGINEER'S CHANGE DISCIPLINE, as code.

A senior never "just edits". Every alteration is first classified into the
ONE design layer it belongs to, then executed as the SMALLEST edit at that
layer, verified at that layer, with everything else protected:

    1. requirement    new spec            -> re-derive, ripples everywhere
    2. topology       add/remove/rewire   -> schematic surgery  -> ERC
    3. value          R/C/L number only   -> recalc + set_value -> no redraw
    4. part_swap      different component -> BOM + footprint + pin-map check
    5. refactor       sheets/readability  -> netlist MUST stay identical
    6. layout         board placement/route -> DRC, never touches netlist
    7. manufacturing  stackup/fab outputs -> gerbers only

Two unbreakable rules enforced by the routing table:
  * a change lives in exactly ONE layer (a layout fix never silently edits
    the netlist; a value change never forces a full redraw);
  * protect what is proven -- the recommended path is always the smallest
    edit, keeping every verified block byte-for-byte intact.

Everything here is table-driven and pattern-based -- NO hardcoded part
names, circuits, refs or counts. Same behaviour on a 5-part or 500-part
design. classify() is pure (text in, dict out); the caller (MCP server /
router) owns tool execution.
"""

from __future__ import annotations
import re

# A reference designator like R5, C12, U3, J1, Q2, L4, D7, SW1, Y1...
_REF = r"\b([RCLDQUJYKFT]|SW|TP|JP|FB|IC)\d+\b"
# A component value token: 10k, 4.7k, 330, 100nF, 22pF, 10uF, 2.2uH, 1M...
_VAL = (r"\b\d+(?:\.\d+)?\s*"
        r"(?:k|m|meg)?(?:ohm|Ω|r)?\b|"
        r"\b\d+(?:\.\d+)?\s*(?:p|n|u|µ|m)?(?:f|h)\b")

# ---------------------------------------------------------------------------
# THE LAYER TABLE -- one row per design layer, ordered by RIPPLE (widest
# blast radius first). When several layers match, the WIDEST one wins and
# the narrower matches are reported as 'also_matched' so the caller can ask.
# Each row: cues that mean "this layer", the minimal edit path (real MCP
# tools in this system), the verification that proves the edit, the blast
# radius to warn about, and what must be protected (left untouched).
# ---------------------------------------------------------------------------
LAYERS = [
    {
        "layer": "requirement",
        "cues": [
            r"\b(now|instead|must|should)\b.*\b\d+(?:\.\d+)?\s*v\b",
            r"\bsupport\b.*\b(usb|can|rs-?485|ethernet|wifi|ble|bluetooth)\b",
            r"\b(spec|requirement|input voltage|output voltage|operating "
            r"temp|temperature range|battery|power budget)\b.*\b(change|"
            r"new|different|increase|decrease|to)\b",
            r"\bredesign for\b|\bre-?spec\b",
        ],
        "edit_path": ["re-derive affected blocks: build(mode='source') -> "
                      "EXTEND the script with recalculated values/parts -> "
                      "build (full verify gates)"],
        "verify": ["ERC (netlist + drawn)", "re-run affected design_calc "
                   "formulas", "re-check PCB constraints if built"],
        "blast_radius": "WIDEST -- may invalidate values, parts, layout and "
                        "manufacturing outputs downstream",
        "protect": "unaffected functional blocks: extend the script, never "
                   "restart the project",
    },
    {
        "layer": "part_swap",
        "cues": [
            r"\breplace\b.*\bwith\b|\bswap\b|\bsubstitute\b",
            r"\b(different|another|alternative|cheaper|available)\b.*\b"
            r"(part|chip|ic|regulator|mcu|sensor|transistor|mosfet|diode)\b",
            r"\b(smd|smt|tht|through.?hole|0603|0805|0402|sot-?23|soic|"
            r"qfn|tqfp)\b.*\b(instead|version|variant|change|use)\b",
            r"\bfootprint\b.*\b(change|swap|assign|wrong)\b",
        ],
        "edit_path": ["parts(action='search'/'describe') for the new part -> "
                      "build(mode='source') EXTEND with the swap (pin-map may "
                      "differ!) -> assign_footprints if footprint changed"],
        "verify": ["pin-map check via parts(action='describe')", "ERC",
                   "BOM re-generate", "DRC if the footprint changed on a "
                   "built board"],
        "blast_radius": "BOM + footprint + possibly pin wiring; layout "
                        "courtyard may change",
        "protect": "every other part and all routed copper not under the "
                   "swapped footprint",
    },
    {
        "layer": "topology",
        "cues": [
            r"\b(add|remove|delete|insert)\b.*\b(part|component|resistor|"
            r"capacitor|cap|led|diode|transistor|connector|pull-?up|"
            r"pull-?down|filter|protection|tvs|fuse)\b",
            r"\b(connect|disconnect|rewire|re-?route the net|wire)\b.*"
            r"\b(pin|net|to|from)\b",
            r"\bmissing\b.*\b(decoupl|bypass|cap|resistor|connection)\b",
        ],
        "edit_path": ["small: edit_schematic_live (app open, undoable) / "
                      "edit_schematic(delete_part) -- larger: "
                      "build(mode='source') EXTEND the script"],
        "verify": ["ERC immediately after (post-edit ERC is automatic for "
                   "delete_part/rename_net)", "netlist re-sync before any "
                   "PCB re-route"],
        "blast_radius": "netlist changes -> board is stale until "
                        "create_pcb re-run; placement near the edit moves",
        "protect": "all unrelated nets/blocks; never full-rebuild to add "
                   "one part",
    },
    {
        "layer": "value",
        "cues": [
            r"(?:change|set|make|update|increase|decrease)\b[^.]{0,40}" + _REF,
            _REF + r"[^.]{0,30}\b(?:to|=|->)\s*(?:" + _VAL + r")",
            r"\bvalue\b.*\b(change|wrong|should be|to)\b",
            # either word order: "make the LED brighter" / "brighter LED"
            r"\b(brighter|dimmer|faster|slower|higher|lower)\b[^.]{0,30}"
            r"\b(led|blink|cutoff|frequency|current|timing)\b",
            r"\b(led|blink|cutoff|frequency|current|timing)\b[^.]{0,30}"
            r"\b(brighter|dimmer|faster|slower|higher|lower)\b",
        ],
        "edit_path": ["recalculate with design_calc (led_series_resistor / "
                      "ne555_astable / rc_cutoff / ...) -> "
                      "edit_schematic(op='set_value') -- NEVER a rebuild"],
        "verify": ["the design_calc formula's back-computed actuals",
                   "no ERC needed (value is a display field; connectivity "
                   "unchanged)"],
        "blast_radius": "ONE part's value + the BOM line; netlist topology "
                        "and layout untouched",
        "protect": "the entire drawn schematic byte-for-byte except one "
                   "property string",
    },
    {
        "layer": "refactor",
        "cues": [
            # either word order: "split into hierarchical sheets" /
            # "hierarchy sheets reduce pannanum". NOTE: no trailing \b on
            # the noun group -- it must match prefixes (hierarchICAL,
            # sheetS, pageS).
            r"\b(hierarch|flat|sheet|page)[^.]{0,40}\b(split|merge|"
            r"convert|reduce|single|fewer|reorganiz|restructure)",
            r"\b(split|merge|convert|combine|reduce|restructure|"
            r"reorganiz)\w*\b[^.]{0,40}\b(hierarch|flat|sheet|page)",
            r"\brename\b.*\b(net|label|signal)\b",
            r"\b(clean ?up|tidy|readab|reorganiz)\b.*\b(schematic|sheet)\b",
        ],
        "edit_path": ["rename: edit_schematic(op='rename_net') -- "
                      "sheet restructure: build(mode='source') EXTEND with "
                      "the same circuit re-grouped"],
        "verify": ["NETLIST MUST STAY IDENTICAL -- diff the .net before/"
                   "after (golden verify); ERC after rename"],
        "blast_radius": "readability only; any netlist delta means the "
                        "refactor FAILED",
        "protect": "electrical behaviour -- a refactor that changes the "
                   ".net is a bug, revert it",
    },
    {
        "layer": "layout",
        "cues": [
            r"\b(move|place|position|rotate|nudge)\b.*\b(on the board|pcb|"
            r"footprint|connector|component)\b",
            r"\b(route|track|trace|via|pour|zone|copper|ground plane|"
            r"ratsnest|unrouted)\b",
            r"\b(board size|outline|edge|mounting hole|keepout)\b",
            r"\b(silk|silkscreen|courtyard)\b",
        ],
        "edit_path": ["edit_board_live (app open) / update_board_setup -> "
                      "create_pcb re-route -- NEVER touch the schematic for "
                      "a layout problem"],
        "verify": ["run_drc (live-editor parity)", "verify_board (copper vs "
                   "netlist) -- the netlist must be bit-identical"],
        "blast_radius": "copper/placement only; manufacturing outputs stale "
                        "until re-export",
        "protect": "the netlist and schematic -- a layout edit that changes "
                   "connectivity is a SHORT/OPEN, not an edit",
    },
    {
        "layer": "manufacturing",
        "cues": [
            r"\b(gerber|drill|pick.?and.?place|pos file|fab|jlc|pcbway|"
            r"oshpark|panel)\b",
            r"\b(stackup|layer count|2.?layer|4.?layer|copper weight|"
            r"1oz|2oz)\b.*\b(change|to|make)\b",
            r"\b(solder ?mask|surface finish|enig|hasl)\b",
        ],
        "edit_path": ["update_board_setup (stackup/constraints) -> "
                      "create_pcb re-route if copper rules changed -> "
                      "export_manufacturing"],
        "verify": ["DRC gate + approval gate before export (enforced)",
                   "stale-output purge is automatic on re-export"],
        "blast_radius": "fab outputs; a stackup change also invalidates "
                        "routing widths/pours",
        "protect": "schematic + netlist entirely; usually also placement",
    },
]

_LAYER_RANK = {row["layer"]: i for i, row in enumerate(LAYERS)}


def _matches(text, row):
    """All cue hits for one layer row (list of matched cue snippets)."""
    hits = []
    for cue in row["cues"]:
        m = re.search(cue, text, re.IGNORECASE)
        if m:
            hits.append(m.group(0)[:60])
    return hits


def classify(text: str) -> dict:
    """Classify ONE change request into its design layer.

    Returns {layer, confidence, signals, edit_path, verify, blast_radius,
    protect, also_matched, refs, values, note}. Pure function: no I/O, no
    tool calls -- the caller routes to the actual MCP tools.

    Resolution rule (the senior instinct): when several layers match, the
    WIDEST-ripple layer wins -- treating a requirement change as a value
    tweak ships a wrong board, while the reverse merely over-verifies.
    EXCEPTION: an explicit ref+value pattern (e.g. "R5 to 10k") is such a
    strong value signal that it beats a coincidental topology cue.
    """
    t = (text or "").strip()
    scored = []
    for row in LAYERS:
        hits = _matches(t, row)
        if hits:
            scored.append((row, hits))

    refs = re.findall(_REF, t, re.IGNORECASE)
    explicit_val = re.search(
        _REF + r"[^.]{0,30}(?:to|=|->)\s*(?:" + _VAL + r")", t, re.IGNORECASE)

    if not scored:
        return {
            "layer": "unclear", "confidence": 0.0, "signals": [],
            "note": ("No layer cue matched. Ask ONE question: is this a new "
                     "requirement, a wiring change, a value change, a part "
                     "swap, a sheet cleanup, a board edit, or a fab-output "
                     "change? Do NOT guess and do NOT rebuild."),
            "refs": refs, "values": [], "also_matched": [],
        }

    # widest ripple wins...
    scored.sort(key=lambda rh: _LAYER_RANK[rh[0]["layer"]])
    winner, hits = scored[0]
    # ...except an explicit "R5 to 10k" which is unambiguously a value edit
    # unless a requirement cue ALSO fired (spec change re-derives values).
    if explicit_val and winner["layer"] in ("topology", "part_swap"):
        for row, h in scored:
            if row["layer"] == "value":
                winner, hits = row, h
                break
        else:
            winner = next(r for r in LAYERS if r["layer"] == "value")
            hits = [explicit_val.group(0)[:60]]

    # confidence: single clean match is high; multi-layer match lowers it.
    conf = 0.9 if len(scored) == 1 else max(0.4, 0.9 - 0.15 * (len(scored) - 1))

    return {
        "layer": winner["layer"],
        "confidence": round(conf, 2),
        "signals": hits,
        "edit_path": winner["edit_path"],
        "verify": winner["verify"],
        "blast_radius": winner["blast_radius"],
        "protect": winner["protect"],
        "also_matched": [r["layer"] for r, _ in scored
                         if r["layer"] != winner["layer"]],
        "refs": refs,
        "values": re.findall(_VAL, t, re.IGNORECASE)[:8],
        "note": ("Multiple layers matched -- confirm with the user before "
                 "editing; the widest layer was chosen because mis-classing "
                 "DOWN ships a wrong board, mis-classing UP only "
                 "over-verifies." if len(scored) > 1 else
                 "Single-layer match: apply the minimal edit path, verify "
                 "at this layer only, leave every other layer untouched."),
    }
