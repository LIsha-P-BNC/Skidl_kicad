# PLAN → GERBER: the AI design pipeline

AI-facing checklist for taking any circuit request from idea to manufacturing files.
Follow the phases IN ORDER; every gate is mandatory. Tool names are the MCP tools
(`build`, `parts`, `plan_change`, `analyze_pcb_environment`, `initialize_pcb_project`,
`create_pcb`, `run_drc`, `verify_board`, `export_manufacturing`). Sibling docs:
`RULE_ENGINE.md` (schematic engine), `WIRE_LABEL_RULES.md`, `schematic_generation_spec.md`.

```
PLAN -> LIBRARY -> SCHEMATIC -> BOARD SETUP -> PLACE -> ROUTE -> DRC+VERIFY -> GERBER
 (1)      (2)        (3)           (4)          (5)      (6)        (7)          (8)
```

---

## Phase 1 — PLAN (no tool writes anything yet)

1. **Requirements first.** Input voltage/rails, loads + currents, interfaces,
   environment. Missing facts → ask the user; never assume.
2. **Recommend parts BEFORE searching the library** — specs table, A-vs-B
   comparison. Read the DATASHEET of every active IC (regulator, MCU,
   transceiver, op-amp…): pins, min/typ values, Typical Application Circuit.
   Trivial passives may skip.
3. **BOM table is the first approval artifact** (`# | Function | Part | Value |
   Lib:Name | In library? | Action`). User approves the table before any build.
4. **`design.md` in the project folder** — create it when the design starts and
   MAINTAIN it through every phase (template at the bottom of this file).
5. Register the plan: `build(name, mode='plan', code=<plan text>)` → show the
   user → wait for approval. A new-project build is refused until this exists.

## Phase 2 — LIBRARY GROUNDING (first-time-correct, no retry cycles)

1. `parts(action='search', items=[ALL parts])` — ONE call. `exact=False` with
   candidates = package-suffix variant exists → pick by DATASHEET pin count,
   never by name alone. Only `total_matches: 0` means missing.
2. Missing part → show the datasheet pin table, get explicit permission, then
   `parts(action='add')` (+ `add_footprint` if needed) with `expected_pins`.
   NEVER map an IC onto a generic/placeholder symbol.
3. `parts(action='describe', items=[chosen Lib:Name])` — ONE call. Write EVERY
   connection only from this JSON, pin names verbatim.

## Phase 3 — SCHEMATIC (build + electrical gates)

1. **Blocks = FUNCTIONAL MEANING, never a part count.** One
   `smart_schematic.block("<function>")` per subsystem you could bench-test
   alone (power stage, MCU core, comms front-end, sensor channel, driver).
   A 3–4 part support group serving one main IC pin (crystal + load caps,
   reset RC, pull-ups, decaps, indicator LED) goes INSIDE its anchor block,
   wired directly to the main pin — never its own box stitched with labels.
   Labels are for cross-block / cross-sheet hops only.
2. Power nets named canonically (`+5V +3V3 +12V VBAT GND`) → power symbols.
3. **Every pin connected or explicitly `+= NC`** — floating pins fail precheck.
4. `build(name, mode='body', confirm=True)` (or `mode='script'` for
   hierarchical `@subcircuit` pages; ~5+ distinct subsystems → hierarchy).
5. Poll `build(mode='status')` until `done`. Gate: `kicad_erc_errors == 0`,
   `verified: true`, `netlist_verified: true` in the response /
   `<name>.build_result.json`. `check_live('erc')` for the app-engine verdict.
6. **DEEP-CHECK the returned nets dict** pin-for-pin against intent before
   moving on — wrong net = fix body and rebuild NOW, not after layout.
7. **Changing an existing design? `plan_change(request, name)` FIRST** — it
   returns the one design layer (value/part_swap/topology/…) and the minimal
   edit path. A value change is `edit_schematic(op='set_value')`, NEVER a
   rebuild. Layer `unclear` → ask the user its one question.

## Phase 4 — BOARD SETUP (mechanical BEFORE placement — professional order)

1. `analyze_pcb_environment(name)` — step zero, once per session. Show the
   user the usage_map; later steps must honor found user values.
2. **ASK THE USER THE BOARD SIZE** (and anything else the design-aware
   questionnaire raises: layers, connector edges, net currents/voltages,
   mounting holes, IPC class). The questionnaire comes from
   `create_pcb` → `status: requirements_needed`.
3. `initialize_pcb_project(name, board_width, board_height, layers,
   ipc_class, mechanical={board, connectors: [{ref, edge, offset}], keepouts},
   currents={NET: A}, voltages={NET: V}, requirements_asked=[…])`.
   Show `resolved_configuration` (every value + source + confirmed status) and
   the `limitations` list; ask "Proceed?".
4. Lessons (verified 2026-11-13): a board that cannot fit the fixed edge items
   FAILS HONESTLY instead of growing — offer the user "enlarge or move the
   connector", and don't over-pin connectors (start with the one that is
   mechanically forced; each extra fixed edge shrinks the placer's freedom).

## Phase 5 — PLACEMENT (inside the user's outline)

`create_pcb(name)` with `route` omitted → places and STOPS
(`placed_unrouted`). The rules placer mirrors the schematic blocks 1:1 as
board regions: anchor IC centered per block, decaps at their IC's pins,
crystal beside the MCU, fixed connectors on their edges. Check the placement
report: `courtyard_overlaps` must be 0; review `quality` (HPWL, congestion).
Show the route_prompt and ASK the user before routing — never route silently.

## Phase 6 — ROUTING

- Board open in the app → `autoroute_live()` (user watches; same FreeRouting
  engine). Headless → `create_pcb(name, route='yes')`.
- Power/GND prefer pours: the pipeline pours GND (+ stitch vias) and excludes
  poured nets from track routing. Net classes from Phase 4 set widths
  (Power class wider). Routing gate: `routing.complete: true`,
  `unrouted_remaining == 0`.
- Won't fully route → `pcb-place-route-fix` skill (place → fanout → route →
  pour order, island healing), not random retries.

## Phase 7 — DRC + TRUTH GATE (both must pass; neither substitutes the other)

1. `run_drc(name)` — the app/CLI engine with the user's severities. Report
   EVERY error-severity violation verbatim. Gate: 0 errors.
2. `verify_board(name)` — as-routed copper netlist (IPC-D-356) vs intended
   `.net`, pin-partition compare. `ok: false` = short or broken net →
   **DO NOT MANUFACTURE**; report `missing` (breaks) and `extra` (shorts)
   verbatim and fix (usually re-place/re-route, never a silent netlist edit).
3. `config_conformance` in the create_pcb result proves the built board kept
   the user's size/layers/pour/track-width choices.

## Phase 8 — GERBER / MANUFACTURING OUTPUT

1. **Ask first** ("Generate BOM / Gerbers now?") — never auto-export.
2. `export_manufacturing(name)` → fab zip: Gerbers per layer, Excellon drill,
   IPC-D-356, pick-and-place, BOM (via `build(mode='bom')` — the app's own
   column preset). Extra formats per `analyze_pcb_environment` capabilities:
   STEP, IPC-2581, ODB++.
3. Record in `design.md`: fab profile (default jlcpcb), layer count, finished
   size, the DRC/verify verdicts, and the zip path. The fab zip + the
   `.build_result.json` verdicts are the deliverable proof.

---

## The gates, one table

| # | gate | proof |
|---|------|-------|
| 1 | user approved plan + BOM table | `build(mode='plan')` registered |
| 2 | every part real, pins from `describe` | search/describe JSON |
| 3 | ERC 0, netlist verified | `build_result.json`, `check_live('erc')` |
| 4 | board config user-confirmed | `resolved_configuration` all confirmed |
| 5 | placement fits, 0 overlaps | placement report |
| 6 | routing complete | `routing.complete`, 0 unrouted |
| 7 | DRC 0 errors AND board==netlist | `run_drc`, `verify_board ok:true` |
| 8 | user asked for outputs | `export_manufacturing` result |

## Per-project `design.md` template (maintain it, don't just create it)

```markdown
# <project> — design record
## Requirements        (what the user asked; Q&A log)
## BOM                 (the approved table, updated on every part change)
## Architecture        (functional blocks, power tree, cross-block nets)
## Key decisions       (why this regulator/topology/value; datasheet refs)
## Board               (size WxH mm, layers, IPC class, net classes,
                        connector edges, currents/voltages declared)
## Status              (phase reached; ERC/DRC/verify verdicts with dates)
## Manufacturing       (fab profile, zip path, exported formats)
## Open items          (warnings accepted, deferred ideas)
```

Keep this file and the per-project `design.md` as the AI's memory between
sessions: the project on disk is the truth, the markdown is why it looks
that way.
