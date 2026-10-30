---
name: skidl-circuit
description: >-
  Turn a circuit request into a correct SKiDL Python script that generates a netlist +
  KiCad schematic project, using ONLY this repo's own SKiDL library and plain KiCad — no
  proprietary app, no per-machine setup. Use this WHENEVER someone working in this repo
  (or any project with SKiDL installed) wants to design/create/generate a circuit,
  schematic, netlist, or KiCad project from a text prompt, a circuit IMAGE/photo/diagram,
  a datasheet, or a spec DOCUMENT. Produces PROFESSIONAL, standard-compliant schematics
  (IEEE 315): meaningful net names, power/GND symbols, standard values + reference
  designators, decoupling caps, left-to-right flow. Generic and DYNAMIC — works for ANY
  circuit; never hardcodes one. Fully portable: no absolute paths, no external app.
---

# SKiDL circuit builder (portable — repo-local, no external app)

Someone wants: describe a circuit as **text, an image, a document, or a datasheet** → you
write a **correct** SKiDL `.py` script → running it produces `.net` + one `.kicad_sch` per
hierarchy sheet, which open directly in **plain KiCad** (any recent version this repo
supports — kicad6 through kicad9). No proprietary app, no per-user folder convention —
this skill only depends on what's in this repo (`pip install -e .` from repo root gets
`skidl` and the `skidl-part-search` CLI on the path) plus a KiCad install for viewing
output and resolving footprints/symbols. **Note (verified by running it):** plain SKiDL
does NOT generate a `.kicad_pro` project file — only `.net` + `.kicad_sch`. Opening the
top-level `.kicad_sch` directly in KiCad, or opening/saving the folder as a new KiCad
project, is what creates the `.kicad_pro` — don't claim this skill produces one.

## THE ORDER — non-negotiable (follow EVERY time, for any circuit)
1. **Recommend FIRST — before searching the library.** For the request, recommend the
   best part(s) with a **specs table + comparison** (part A vs B, which suits which use
   case). The user often does not know the exact part — advise them; don't lead with a
   library search.
2. **Read the datasheet — MANDATORY for every active IC.** For **each active IC you
   place** (regulator, MCU, transceiver, driver, op-amp, sensor, memory, etc.) fetch and
   read its datasheet — `WebFetch` a URL the user gave, else `WebSearch "<mfr part>
   datasheet"` — to confirm **pin numbers/names, min/typ values, and the *Typical
   Application Circuit***. Not optional for active parts. Only **trivial passives** (R /
   C / L / LED / diode / connector) and truly well-known textbook parts may skip this.
   If the user supplied a datasheet/document/image, read that too. **Never guess a value,
   a pin, or a connection.**
2b. **Source ↔ datasheet difference analysis**, when the user supplies an image/PDF/doc
   AND a datasheet exists for a key IC:
   - Read the **SOURCE** (image/PDF) = the actual circuit → **this is what you draw.**
   - Read the datasheet's *Typical Application Circuit* = the reference only.
   - **List the deltas**: parts the board adds (status LEDs, extra filtering, protection,
     test points), parts it omits, value differences, **NP/not-populated** parts,
     different pins.
   - **Draw the source-faithful version — the board wins.** Use the datasheet only to (a)
     validate pins, (b) fill a spot the image is unclear/cut off, (c) **flag a WARNING**
     if the board seems to violate a datasheet requirement (e.g. an output cap below the
     regulator's stability minimum) — surface it, do **not** silently "correct" the board.
3. **The user decides — their choice is final.** Present options (`AskUserQuestion` is
   ideal). Only recommend; never choose for them.
4. **Wait for confirmation** before touching the part library.
5. **Then search the library** — `skidl-part-search "<keyword>"` (installed via this
   repo's `setup.py` console_scripts; broken on Windows before this repo's readline fix —
   confirm it runs, or fall back to `python -m skidl.scripts.part_search_cli`). Report
   clearly which confirmed parts exist in the library and which don't.
6. **Never silently substitute a missing part.** If a part isn't in the library, report it
   and stop — adding new library entries is a separate, explicit step.

## Input fidelity — output must match the input 1:1
Text prompt, circuit **image**/photo, spec **document**, or **datasheet** — however the
input arrives, the output must be **faithful to it**: same components, same values, same
wiring. **Goal = exact transcription of the source, not a "simplified/functional"
re-interpretation.**
- **Never skip a part because it looks electrically inert** — test points (`TP…`),
  `NP`/`N.M.` (not-populated), fiducials, logos: still create them (value `NP` where
  marked).
- **Loops are fine for repetition, but must reproduce EVERY per-instance part and its
  exact reference designator** — a repeated block with N parts per instance in the source
  must emit N × count parts with the source's real refs, not a shrunk loop with
  auto-generated refs.
- **Read dense sheets part-by-part, not as a pattern** — a heavily populated page hides
  parts inside clusters; check each one against the BOM.
- Match every **value** and **pin** exactly — never guess; confirm pins before wiring
  (`skidl-part-search --fields ...` or read the library entry directly).

## BOM TABLE FIRST — before ANY build (MANDATORY gate, not an afterthought)
The BOM is the **FIRST approval artifact**, presented BEFORE building — never dumped after
the build. Present it as a **TABLE** with these columns:

| # | Function | Part | Value | `Lib:Name` | In library? | Action |
|---|----------|------|-------|-----------|-------------|--------|

- **In library?** = `✅ yes` or `❌ missing` — from the `parts(action='search')` result
  (`total_matches>0` → yes; `0` → missing).
- **Action** = `use` for a library part; `ADD (needs approval)` for a missing one.
- **Get the user's approval of this TABLE before building.** It is the plan's core.

**Library-missing parts → permission BEFORE adding, THEN design.** For every `❌ missing`
row: show the part's **datasheet pin table** and ask the user's **explicit permission to ADD**
it (symbol AND footprint, pins typed from the datasheet). Add ONLY after they approve; start
the design ONLY after the library is complete. **Never add a part silently, never add-then-ask.**

**`design.md` FIRST.** When the design starts, create a **`design.md`** in the project folder
capturing: the BOM table, chosen parts + values, the functional-block breakdown, and key
decisions (why this regulator, why this MCU, fan current, etc.). It is the design's source of
truth — **drive every later change THROUGH the md first** (update the doc, then rebuild).

Every listed component must end up grounded, library-checked, and wired. After generating,
**cross-check the schematic/netlist part count against the agreed BOM table** — fix before
reporting if anything's missing. If a component is unclear, **ask** — never silently skip it.
The final-report BOM (via `build(mode='bom')`, columns Ref/Value/Qty/`Lib:Name`/Footprint)
must match the approved table.

## Ground every part — never invent one
Never write a `Part("Lib", "Name")` or a pin number you haven't confirmed exists.
1. `skidl-part-search "<keyword>"` → real `Lib:Name` combinations. Pick the right one.
2. Confirm pins before wiring — either via the search tool's `--fields` output or by
   instantiating the part (`p = Part("Lib", "Name"); print(p.pins)`) and checking numbers
   and names before connecting.

## Build steps
1. **Understand** the circuit from the prompt/image/document/datasheet; list components +
   connections. For each active IC, read its datasheet and do the source ↔ datasheet
   difference analysis (THE ORDER, step 2/2b). Ask a short clarifying question only if a
   value/part is genuinely ambiguous.
1a. **Free-text on every question (global).** Any time you offer the user options,
   suggestions, or choice buttons — part/MCU selection, values, footprints, sheet
   organization, layout, routing yes/no, BOM columns, manufacturer, board size, output
   format, **any** decision — there is **always an "other" free-text path**. When the user
   types their own answer, **read it, analyse what it means** (map it to the concrete
   action/parameter, even if loosely phrased or in their own language), and **act on it** —
   never ignore it, never force them back to the preset list. Ambiguous → one short
   clarifying question; clear → just do it. The listed options are only shortcuts.
1b. **Design-organization intake (before the first build).** The plan you present for
   approval MUST include the **design organization**, not just the BOM, and you ASK the
   user to confirm or change it — their answers become this project's **custom design
   rules** for the whole conversation. In ONE consolidated question, state and invite
   edits to: **(a) sheet breakdown** — for a multi-sheet/hierarchical design, which
   function goes on which sheet (e.g. sheet 1 = power/input, 2 = MCU + decoupling +
   crystal, 3 = comms/USB, 4 = sensors/IO) and how many sheets; a small design is one
   sheet; **(b) key part choices** the user cares about (MCU/regulator/connector family);
   **(c) per-sheet layout** intent (signal L→R, power top / ground bottom, connectors on
   the edge, which blocks cluster). Always offer a **recommended default + a few concrete
   suggestions** (proposed sheet split, likely MCU/part choices) so the user can approve
   in one tap — but make clear they are only suggestions and the user can **type their own**
   answer ("or tell me your own split / parts / rule"); accept and follow whatever
   free-text organization they type, never restrict them to the listed options.
   **Precedence on conflict:** the user's typed requirement WINS over style/layout defaults
   (sheet split, L→R flow, power-top/ground-bottom) — apply it as given. It does **not**
   override **electrical correctness** (spec §5.5 `ELE`): if the request would reverse a
   diode/LED/polarized cap, drop an LED's series resistor, float a power/enable pin, or
   short two rails, do **not** silently obey or silently ignore — explain in plain words
   why it is unsafe, propose the correct alternative, and confirm before building.
   Unconventional-but-safe requests are always honoured; only circuit-breaking ones are
   questioned. Follow any custom rule the user gives ("MCU on its own
   sheet", "inputs on the left sheet", "one sheet per subsystem") for **every** sheet,
   including sheets added later. Skip the questionnaire for a trivial single-component
   circuit — just state the one-sheet plan. Build each sheet to the agreed organization
   and the ORIENT rules (spec §5.14). **Wait for the user's OK — do not build anything
   until they explicitly approve the plan + organization** ("ok"/"start"/"go", or an
   edited version they then approve). If they change it, show the updated plan and wait
   again. Only on their approval do you begin building to exactly the agreed plan.
2. **Ground every part** (see above). Note real names/pins.
3. **Write a fresh script** — pick a project name, write `<project>.py` (new unique name;
   never overwrite an unrelated existing circuit's file). **Write it in the fixed authoring
   order** (spec [§9.1](../../../docs/schematic_generation_spec.md)) so connections cannot
   silently split: imports → tool → **all shared nets created ONCE at module level** (every
   cross-block signal + every power rail) → `@subcircuit` blocks (local nets stay inside)
   → top-level instantiation passing the shared `Net` objects → ERC → netlist → schematic.
   For a multi-sheet design, sketch the **net → sheet connectivity matrix** first (spec
   §9.3) and build to it.
   ```python
   from skidl import *
   set_default_tool(KICAD9)   # or KICAD6..KICAD8 to match the target KiCad version

   # parts, nets, buses, @subcircuit blocks — see "PROFESSIONAL SCHEMATIC RULES" below

   ERC()
   generate_netlist(file_="<project>.net")
   generate_schematic(filepath=".", top_name="<project>", title="<Project Title>",
                       flatness=0.0, auto_stub=True)
   ```
   - Set `tag=` on every `Part()` (a short stable string you choose, e.g. `tag="r1"`) —
     verified directly: omitting it logs a "Missing tag" warning and SKiDL auto-generates
     a random one per run, which defeats the whole point (the tag is what keeps a part's
     identity/UUID stable across regenerations, so re-running the script doesn't create a
     fresh, disconnected footprint in the PCB each time). **Also pass `tag=` on the
     `@subcircuit` call itself** (e.g. `power_regulation(vin, vout, gnd,
     tag="power_regulation_blk")`) — verified: the block/Node instance gets its own
     "Missing tag" warning too, and the tag also becomes that block's generated sheet
     filename suffix instead of an auto-numbered one.
   - Apply the **PROFESSIONAL SCHEMATIC RULES** below (meaningful net names, power
     rails that match `_POWER_NET_RE`, standard values, standard ref letters, a
     decoupling cap per IC power pin) — this is what makes the output look professional,
     not just electrically correct.
   - **Wire vs label vs port — author the net structure that produces the right form**
     (spec §5.8 CONN + engine WIRE_LABEL_RULES): **wire** = same-sheet local cluster +
     always for crystal/reset/decoupling/USB-diff/RF; **net label** = same-sheet
     distant/crowded/high-fanout, short stub with a FUNCTION name (never `N$7`);
     **global label** = true cross-sheet signal only, sparingly; **port/sheet pin** =
     an `@subcircuit`'s interface — every cross-block signal is a shared `Net` created
     ONCE at module level and passed as a parameter (it renders as the sheet's port);
     **power symbol** = every rail, always. Never force a visual form by hand (CONN-2)
     — the authoring choices above are what make the engine pick the right one.
   - **Draw like the published reference boards** (spec §5.15 REFLAY, distilled from
     ATmega32U2/STM32/PIC18/EFM32 reference schematics): MCU central — control cluster
     (reset+pull-up, BOOT, crystal+load-caps, debug) on its LEFT, IO/ports/connectors on
     its RIGHT; decoupling caps as ONE bank row on the rail (not scattered); crystal =
     tight closed cluster at the OSC pins; series parts (USB 22Ω, LED resistor) inline ON
     the signal wire; cross-section hops = short FUNCTION-named label stubs, in-section =
     real wires; connectors at the page edge; every unused pin explicitly no-connected;
     GPIO banks as ordered parallel ribbons to their header.
   - **Draw each recurring block to its TI per-function idiom** (spec §5.16 `FUNC`, grounded
     in TI SLOA089 / SPRACU5 / datasheet *Typical Application* circuits). FUNC-1…25 give the
     values, decoupling, pull/strap handling, and polarity for each block type — LDO,
     buck/switcher, MCU core, crystal, reset/boot straps, I²C/SPI/UART, op-amp, current-sense
     (INA), differential/SAR-ADC input, RF match, ESD on connectors, USB front-end, LED
     indicator, MOSFET half-bridge. Build the net structure so the engine renders that
     arrangement (power at top, GND at bottom, decap at the pin, series parts inline on the
     signal wire, cross-block hops as function-named labels). The PCB layout then mirrors
     these same functional blocks 1:1 (pcb-place-route-fix skill, "Schematic-block → PCB-region
     mapping").
   - **Pin-side support parts are INLINE, never a block or a label.** A handful of parts
     hanging off one IC/MCU pin — a filter (pin → R → C → GND), a series diode+LED (pin →
     D → LED → GND), a pull-up/pull-down, a decoupling cap — is NOT a subsystem: write it
     INLINE at module/parent level, connected directly to that pin, so the engine wires it
     straight to the pin (short, local, left-to-right) with NO net label and NO titled box.
     Decide the connection yourself (R→IC pin, R→C, C→GND) — it is unambiguous; don't ask
     and don't wrap 2–4 parts in their own `@subcircuit`. Only a GENUINELY DISTINCT
     multi-part function (a power stage, a comms/USB front-end, a sensor channel group —
     ~5+ parts with its own role) earns an `@subcircuit`/block. Over-boxing tiny pin groups
     is the main cause of a schematic that reads as scattered labels instead of clean local
     wiring. (Verified: an 8-pin MCU with a pin→R→C filter and a pin→diode→LED built inline
     renders as 27 direct wires, 0 labels, 0 blocks.)
   - **What EARNS a block — the TI test (learn block granularity from TI reference designs).**
     A block = a distinct FUNCTIONAL SUBSYSTEM you could bench-test in isolation: power
     input+regulation, each comms front-end (USB front-end, UART bridge, CAN, I²C sensor
     group), the MCU core, each driver/output stage (e.g. a MOSFET fan driver), each analog
     front-end (INA current-sense + shunt + filter). Test: ONE anchor IC/connector + its
     serving cluster = a block. These are **inline, NEVER their own block**: decoupling caps,
     pull-ups/downs, series terminators, reset RC, boot/config straps, **indicator LEDs,
     push buttons, test points**. (Caught live: giving two status LEDs their own "INDICATORS"
     box is OVER-blocking — LEDs sit inline at the GPIO/rail pin they're on.) Each FUNC-1…25
     block type (see the TI per-function catalog above) is a candidate block; its support
     parts stay inline within it. Don't box everything — meaningful function only.
   - **HOW TO SPLIT — the decision procedure** (industry-grounded: TI reference designs +
     Altium / Flux.ai / PCBWay schematic-quality guides + Graham Sutherland's "high-quality
     schematics"). Follow it every design:
     1. **List the functional SUBSYSTEMS**: power input, each regulated rail, MCU/processor
        core, each comms interface (USB front-end, UART bridge, CAN, I²C sensor group), each
        sensor, each driver/output stage, connector groups.
     2. **Each subsystem = ONE block.** Its support parts (decoupling, pull-ups/downs, series
        R, indicator LED, button, boot/config strap, test point) stay INLINE inside it, drawn
        next to the part they serve (e.g. the ESD IC sits next to its connector).
     3. **Connect blocks with net LABELS / ports — never long cross-sheet wires.** Inside a
        block = real wires. Signal flows left→right; power up, ground down.
     4. **SHEET CHOICE.** A design that fits ONE readable sheet → single sheet with titled
        boxes (`with smart_schematic.block("…"):` per subsystem). A design with **~5+ distinct
        subsystems, or dominated by a big module / wide connector** (e.g. an MCU module + a
        38-pin GPIO header + USB + UART bridge) → **HIERARCHY: one `@subcircuit` sheet per
        block, even when the part count is under the ~50 threshold** — a complex board crammed
        onto one flat sheet spreads unreadably (the lesson from a flat 27-part ESP32+USB+UART+
        sensor board). Part-count is only a floor; subsystem-count and big modules also force
        hierarchy.
     5. **Hierarchy connectivity = sheet PINS / ports, NOT a flood of global labels.** A
        cross-sheet net is a shared `Net` created once at module level and passed into each
        `@subcircuit` (renders as that sheet's port); keep global labels minimal so each block
        stays a clean abstraction ("each sheet = an object with a defined interface"). A
        repeated identical subsystem → ONE hierarchical sheet reused, not copy-pasted. The
        top/root sheet then reads as the design's BLOCK DIAGRAM. (Industry standard — KiCad,
        Altium, pcdandf: hierarchy for complex boards reduces complexity, eases ECOs, enables
        reuse.)
     6. **Give each block ROOM** — don't cram; prefer a larger sheet / more sheets over a
        squashed page. **Plan the block/sheet breakdown BEFORE building** — it goes in the
        `design.md` and the plan you get approved.
   - Group into `@subcircuit` functions **by function** (power, MCU, comms, sensing, …),
     5–15 parts each for best auto-routing. **`@subcircuit` = one schematic page per call,
     every call** — never call an `@subcircuit`-decorated function inside a loop/repeated
     block. Verified directly: 50 identical `@subcircuit` calls (a "sensor channel," each
     nesting one more `@subcircuit`, 100 hierarchy nodes total) produced **101 separate
     `.kicad_sch` pages** — this is exactly how a design explodes to "100 pages."
     **Repeated identical sub-units (LEDs, resistor arrays, connector cells, per-channel
     blocks, …) must be a plain, undecorated helper function** — it adds its parts
     straight onto the *current* (parent) sheet instead of spinning off a new one, no
     matter how many times it's called in a loop:
     ```python
     # WRONG — 50 calls = 50 extra pages
     @subcircuit
     def sensor_channel(vcc, gnd, sig): ...
     for i in range(50):
         sensor_channel(vcc, gnd, Net(f"CH{i}"))

     # RIGHT — stays on the parent sheet, no page explosion
     def sensor_channel(vcc, gnd, sig): ...   # no decorator
     for i in range(50):
         sensor_channel(vcc, gnd, Net(f"CH{i}"))
     ```
     **Page-count model: pages ≈ 1 (top) + number of `@subcircuit` *call sites*
     (instances), never the number of function *definitions*.** A single
     `@subcircuit`-decorated function called 100 times yields ~101 pages, not 2 — the
     decorator applies per-call, not per-def.
     Reserve `@subcircuit` for the handful of **genuinely distinct** blocks in the design
     (power, MCU, USB, RF, sensor front-end, …) — that naturally lands a big design at a
     readable ~4–8 pages instead of one page per repeated instance.
   - **Don't reach for `flatness` to fix a page-count problem** — verified directly:
     `flatness` folds child node-*types* back onto the parent sheet smallest-total-size
     first, so a type repeated N times (its total size grows with N) is usually the
     *last* type eligible to flatten. In testing, `flatness=0.2/0.5/0.8` left all 50
     repeated-instance pages unflattened; only `flatness=1.0` merged them — and that also
     erases *all* hierarchy (the genuinely distinct blocks too), defeating the purpose.
     Keep `flatness=0.0` (the template default) and control page count at the source —
     the plain-function rule above — not via this parameter.
   - **Pre-flight check, before running the script**: re-read what you just wrote and ask,
     for every `@subcircuit`-decorated function — *is it called more than once, or from
     inside a loop?* If yes, that's `N` extra pages for `N` calls; stop and convert it to
     a plain undecorated function before running, rather than discovering it after
     generation.
4. **Run it**: `python <project>.py`. First run may build a symbol/footprint cache — allow
   time or run in the background.
5. **Check ERC**: aim for 0 errors; investigate real warnings (usually a missing
   connection).
6. **Validate against the spec — MANDATORY gate before reporting done.** Run
   `python -m skidl.scripts.validate_design <project>.py --netlist <project>.net --matrix`
   (falls back to running the file directly:
   `python src/skidl/scripts/validate_design.py ...`). It statically checks the `.py` (and
   the `.net`) against the normative rules in
   [docs/schematic_generation_spec.md](../../../docs/schematic_generation_spec.md) and
   reports by rule ID. **Every `ERROR` (a MUST/MUST NOT violation) is blocking — fix the
   `.py` and re-run until zero errors.** Review each `WARN`; fix or justify it in the
   report. The validator never executes the target, so it is safe and fast. For
   multi-sheet designs, `--matrix` prints the **net → block connectivity matrix** — diff
   it against your intended net-to-sheet plan (and the source image/PDF) to confirm no net
   was missed (`(not passed to any block)` = a missed connection, also flagged NET-6).
7. **Self-audit** (see below) — re-read the `.py` against the source and fix anything
   before reporting.
8. **Confirm the schematic ARTIFACT exists — the success gate (GEN-3).** ERC-clean +
   netlist is NOT a finished build. **Never say "schematic generated" / "Build Successful"
   / "verified" until the `.kicad_sch` file actually exists on disk** and passes
   connectivity verification. On the MCP path, that means polling `build(name, mode='status')`
   until `status:"done"` with `generated` listing `.kicad_sch` (a MISMATCH there = failed;
   the `.net` is still valid for PCB). On the plain-SKiDL path, confirm
   `generate_schematic(...)` ran and the `.kicad_sch` file(s) are on disk. If only `.py` +
   `.net` exist, the build **stopped at the netlist — report it as INCOMPLETE**, not
   successful. Then run **golden verification** —
   `python -m skidl.scripts.validate_design <project>.py --golden` (auto-runs in the MCP
   `_finish_build`, sets `verified`): it cross-checks `.net` ↔ `.kicad_sch` for dropped
   parts (GV-1), duplicate UUIDs (GV-2), and count parity (GV-3). Only report **"BUILD
   SUCCESSFUL (Verified)"** when golden passes (no GV ERROR) AND the human-assisted diffs
   vs. the source (component count, pin connectivity, `--matrix`, hierarchy) match. See
   spec [§10](../../../docs/schematic_generation_spec.md).
8b. **RUN THE REAL ERC on the DRAWN sheet — the build's self-report is NOT enough (verified
   2026-10-22).** `build`'s `verified:true` / `kicad_erc_errors:0` / `--golden` are skidl's
   **netlist-level** checks: they confirm the `.net` is right but **do NOT catch a pin left
   physically disconnected in the drawn `.kicad_sch`**. Always run the **real** schematic ERC —
   `check_live('erc')` (MCP, open editor) or `anvil-cli sch erc <file>.kicad_sch` (headless, the
   same engine) — and treat THAT as the verdict. **KNOWN non-deterministic draw bug:** the
   ladder-rail + `ensure_junctions` passes are *marginal* — the connectivity revert-guard can roll
   back the junctions that tie a pin's stub to the rail spine (`junction heal reverted …
   introduced N net defects` in the log), leaving a power pin — most often a **far / page-edge
   connector power pin** (e.g. a `+5V` output-connector pin) — **unconnected in the drawing though
   the netlist is correct**. It VARIES run-to-run (same `.py`: one build clean, another a 1-pin
   gap). **If the real ERC reports an unconnected/`Pin not connected` power pin: REBUILD the same
   name** (the draw varies per run — a re-run usually draws it clean) and re-ERC; only if it keeps
   recurring, connect the pin by hand in the app (`edit_schematic_live` currently **ignores x/y**,
   so surgical placement fails — rebuild or draw it manually). Never ship on `verified:true` alone.
   **Footprints:** the pre-check does NOT confirm a footprint exists — use a real library name
   (terminal block = `TerminalBlock:TerminalBlock_MaiXu_MX126-5.0-02P_1x02_P5.00mm`, never a guess).
9. **Report**: parts (Lib/Name), ERC result, **validator result (0 errors)**,
   **confirmed `.kicad_sch` (+ `.kicad_pro` on the MCP path)**, the BOM, the "datasheet vs
   board" note (per key IC: additions/changes + any compliance warning), and that the
   top-level `.kicad_sch` opens directly in KiCad (plain SKiDL doesn't emit `.kicad_pro`;
   KiCad creates it on first open/save).

## Self-audit — run AFTER writing the .py, BEFORE reporting done
A netlist with "0 errors" can still be electrically wrong (a dropped or miswired part).
Re-check:
1. **Every source part present** — match by ref designator; nothing dropped, including
   `NP` parts (value `NP`, still in the schematic).
2. **LED = series resistor + correct polarity.** Never wire an LED straight across a
   rail. Path: `rail → R → anode`, `cathode → GND`. Confirm pin direction (KiCad
   `Device:LED` pin 1 = K/cathode, pin 2 = A/anode) before wiring.
3. **Pull/bleed resistors connect to a real signal** — a resistor sitting straight
   `rail → GND` does nothing; it must pull an actual net (a reset/enable/mode pin, a
   divider tap).
4. **Config pins tied to a defined level** — reset, boot-mode, unused enable/CS pins
   pulled to their correct rail/GND; no floating control pins.
5. **Sheet structure is sane** — no single `@subcircuit` wildly oversized; no near-empty
   sheet either; and **no `@subcircuit`-decorated function called inside a loop** (every
   call spins off its own page — verified to reach 100+ pages from 50 repeated calls;
   convert it to a plain undecorated function instead, see PROFESSIONAL SCHEMATIC RULES
   step 3 above). If routing keeps failing, check whether a block is too densely
   interconnected for `auto_stub`'s selective-stub thresholds and either split it or
   raise `auto_stub_fanout`/`auto_stub_max_wire_pins`.
6. Report every auto-fix or intentional deviation in the "datasheet vs board" note.

## SENIOR-DESIGNER DISCIPLINE — the process/judgment layer (apply to EVERY design)
The rules below are the common case; a senior's value is the **process + judgment** on top:
front-load the thinking so the drawing is almost mechanical, and never report an unreviewed /
unverified result. Full set: spec [§11 `MET-1…10`](../../../docs/schematic_generation_spec.md).
Grounded in Rick Hartley, Henry Ott, TI SPRACU5, IPC class 1/2/3, NASA EEE-INST-002. The habits:
- **Requirements first (MET-1):** before parts, state the operating box — V-range + transients,
  rail/load currents, worst-case dissipation, mechanical/connector constraints, reliability class.
- **Architecture + risk-first (MET-2):** block diagram + power tree + ground/return strategy first;
  find the hardest part (fine-pitch QFN/BGA, fast edges, sensitive analog, thermal) and let it drive
  the sheet/layout plan.
- **Datasheet + errata (MET-3):** datasheet + app-note + errata together; the typical-app circuit is a
  STARTING POINT to re-derive for THIS design's worst case, not gospel; prefer proven, in-lifecycle,
  second-sourced parts.
- **DFx + debug from day 1 (MET-4):** test points on every rail/clock/reset/programming line;
  pin-1/polarity unambiguous; design in isolatable blocks so the board can be brought up.
- **Review gates — never self-approve blind (MET-6):** run the self-audit as a real per-block review
  BEFORE declaring done; clear every ERC/validator flag OR document why it's waived — no silent violations.
- **Margin (MET-7):** derate (commercial ~80 % V/I, 70 % P); worst-case the critical values; FMEA mindset.
- **Document the WHY (MET-8):** capture design intent + non-obvious value rationale in the report.
- **Verify, don't infer (MET-9):** report what the build/ERC/validator actually confirmed vs assumed (= GEN-3).
- **Rules are defaults (MET-10):** break one only with a documented reason; electrical-safety (`ELE`)
  limits never bend — a reversed diode/LED, a dropped LED series-R, a floating enable, or shorted rails
  is never a "style" override: explain, propose the fix, and confirm.

## PROFESSIONAL SCHEMATIC RULES — bake these into EVERY generated .py
> **Normative reference:** these rules are formalized with MUST/SHOULD/MAY levels, stable
> rule IDs, and a machine-checkable checklist in
> [docs/schematic_generation_spec.md](../../../docs/schematic_generation_spec.md). The
> numbered guidance below is the working summary; the spec is the source of truth, and the
> validator (build step 6) enforces it by rule ID.

`generate_schematic(..., auto_stub=True)` already does the *drawing* — placement,
routing, wire vs. label classification, power-symbol emission. Your job when **writing**
the `.py` is to feed it clean inputs so the result is standard-compliant (IEEE 315 /
IEC 60617 graphic-symbol conventions). Apply ALL of these:

1. **Meaningful net names (UPPERCASE, underscores).** Name every signal net you care
   about — never leave it to auto `N$1`. `sda = Net('I2C_SDA'); tx = Net('UART_TX');
   en = Net('MOTOR_EN')`. Function, not location. Active-low → `Net('RESET_N')`
   (ASCII-safe `_N` suffix — this is what SKiDL/KiCad net matching actually keys off of).
   KiCad's own native display convention for an active-low signal is a text overbar,
   written `~{NAME}` in any KiCad text field — `_N` is the portable equivalent most tools
   (including this one) use for the net name itself.
2. **Power rails must match the engine's power-net pattern so they render as power
   SYMBOLS, not wires.** This repo's own detector (`_POWER_NET_RE` in
   `src/skidl/tools/kicad*/gen_schematic.py`) recognizes: `+<num>V` (e.g. `+5V`, `+3.3V`,
   `+12V`, `+1.8V`) and `GND AGND DGND PGND VCC VDD VSS VEE VBUS VBAT AVCC AVDD DVCC
   DVDD`. So write `vcc = Net('+3.3V'); gnd = Net('GND')`. A name like `Net('VCC_3V3')`
   will NOT be recognized as power — use `+3.3V` for a 3.3 V rail. **One ground net for
   the whole circuit** (`GND`), everything ties to it.
3. **Standard value format**: `10k  4.7k  100nF  4.7uF  1M  0R` — never `10000`,
   `.0000001F`, or inconsistent case. Always pass `value=` in this k/M/u/n/p form.
4. **Standard reference-designator letters** (IEEE 315 defines the *letter*; a companion
   standard, ASME Y14.44/successor to ANSI Y32.16, covers numbering — don't over-claim
   IEEE-315 coverage for the numbering itself): `R`=resistor, `C`=cap, `L`=inductor,
   `U`=IC, `Q`=transistor, `D`=diode/LED, `Y`=crystal, `X`=oscillator, `J`=connector,
   `K`=relay, `SW`/`S`=switch, `F`=fuse, `T`=transformer, `TP`=test point. Never
   `Resistor1`/`IC_5`.
   - **Suffix letters for matched/multi-element parts**: when several instances are
     really one logical unit — a quad op-amp's 4 gates, a matched resistor pair — share
     one base number with a suffix letter: `R17A`/`R17B`, `U10A`/`U10B`/`U10C`/`U10D`.
5. **Decoupling cap on every IC power pin**: for each IC's VCC/VDD pin, add a `100nF` cap
   from that pin to `GND`, created in the **same `@subcircuit`** as the IC so placement
   keeps it adjacent. Bulk `10uF`/`4.7uF` once per supply rail at its entry point.
6. **Pull-up/pull-down resistors explicit**, tied to their rail (e.g. I2C `4.7k` to
   `+3.3V`, reset `10k` to `+3.3V`). Never leave a floating enable/reset — connect or
   pull it.
7. **Left→right signal flow, functional grouping — MANDATORY above ~12 parts.**
   A flat script (all parts in one namespace) gives the placer nothing to organize
   around, so the sheet comes out as a scattered smear that "doesn't tell the
   circuit's story". Group EVERY functional stage:
   - single-sheet designs (≤50 parts): wrap each stage's `Part(...)` creations in
     `with smart_schematic.block("STAGE NAME"):` — the placer keeps the group
     together and draws a labeled box around it;
   - multi-sheet designs (>50 parts): one `@subcircuit` function per page/stage.
   Higher-level/input stages conceptually first (left), outputs last (right). Use
   `&` for clean series chains (`vin & r1 & vout & r2 & gnd`).
8. **Buses for multi-bit signals**: `data = Bus('DATA', 8)` instead of 8 separate nets —
   this creates sequential per-bit nets `DATA0..DATA7`. Keep bus-member names sequential
   and numeric (never rename bits ad hoc) — that's what lets a bus render with `[7..0]`
   bracket-range notation, the standard EDA convention (Altium, KiCad) for labeling the
   bus wire itself, distinct from each bit's own net label.
9. **Account for every pin**: tie unused/NC IC inputs to a defined level (pull or GND) —
   don't leave inputs floating, it shows up as ERC warnings.
10. **Net identity is by Python object, NOT by name string — a real SKiDL gotcha.**
    Unlike hand-editing a KiCad schematic (where typing the same label text on two sheets
    connects them), calling `Net('EN')` twice in two different `@subcircuit`/helper
    functions creates TWO separate, unconnected nets that just happen to share a display
    name — SKiDL will not merge them, and won't warn either. Only actual power-rail names
    (rule 2, matched by `_POWER_NET_RE`) are treated as globally shared. So: (a) never
    reuse a generic local name (`en`, `sel`, `clk`, `rst`) across unrelated
    `@subcircuit`s assuming they'll connect — they won't; (b) for a non-power signal that
    genuinely must cross subcircuits, pass the *same* `Net` object in as a function
    argument, don't just repeat the string name.
11. **`auto_stub=True` is required for anything beyond a trivial circuit** — without it,
    the schematic generator won't auto-emit power symbols or fall back to labels for
    congested nets. Tune `auto_stub_fanout` (default 3) / `auto_stub_max_wire_pins`
    (default 3) / `auto_stub_max_wire_dist` (default 2000 mil) if routing keeps failing
    on a dense block, rather than fighting the placer with a giant flat circuit.
12. **NEVER instantiate `power:*` symbol parts manually — this is the #1 duplicate-parts
    bug.** Writing `Part("power", "+5V")` (worse: a list of 8 of them, one per hand-drawn
    rail arrow) creates N extra floating one-pin PARTS. The generator already emits one
    power symbol at every pin of a rail net automatically from the net NAME (rule 2) —
    `vcc = Net('+5V')` is ALL you write. The duplicates scatter placement, bloat the
    sheet, and (before the 2026-07-24 verifier fix) made connectivity verification fail
    on every seed, collapsing the build to a scattered labels-only sheet. Same logic:
    never create PWR_FLAG parts — the pipeline adds them itself.
13. **NEVER use a placeholder symbol for a real IC** (`Conn_01x40` "standing in" for an
    MCU is BANNED). A generic connector has no pin names and no ERC pin types, so a
    miswired pin passes every check silently. `parts` (action='search') first; if the exact part
    name is missing, use a pin-compatible REAL symbol from the same family and set
    `value=` to the actual part (8051-family DIP-40 → `Part("MCU_Intel", "P8031AH",
    value="AT89C51")` — verified same pinout). Only if no compatible real symbol exists,
    add one to the user library (`parts` (action='add')) — never fake it with a connector.
14. **Where WIRE, where LABEL, where GLOBAL LABEL** (leave the choice to the engine —
    it implements exactly this policy; never force labels on everything):
    - **Power symbol**: every rail/ground net (rule 2). Never a long +5V wire snaking
      the sheet, never a plain `GND` text label.
    - **WIRE**: nearby connections — point-to-point pairs and small LOCAL clusters
      (crystal + load caps at the MCU, reset R-C-button, LED + series R, a transistor
      stage). Function-critical nets (clock/reset/decoupling/USB-diff) stay wires.
    - **NET LABEL**: genuinely far, crowded, or high-fanout signals — MCU port pins
      feeding a distant stage, wide breakouts to connectors. A label on BOTH ends
      replaces one long wire across the sheet. Never a label on every part: if two
      pins sit next to each other, that connection must be a short wire.
    - **GLOBAL LABEL**: only for nets crossing sheet boundaries in a multi-sheet
      (>50-part) design. Single-sheet designs use local labels only.
15. **One circuit = ONE project name, forever.** When fixing or iterating a circuit,
    REBUILD under the SAME name — never mint a new name for the same design
    ("pulse_driver" → "Pulse_Driver_v2" → "PulseDriverNew" is BANNED). Renaming
    leaves stale duplicate projects on disk and stale open windows in the app, and
    the user can no longer tell which one is current. If a build fails, fix the
    .py and rebuild the same name; the atomic publish replaces the files in place.
16. **Test points on the signals a technician actually probes.** Add a `TP` part
    (`Part("Connector", "TestPoint", value="TP_3V3")`, or a 1-pin header) on: every
    supply rail, `RESET`/`NRST`, the main clock/oscillator, and each primary bus line
    (UART_TX/RX, I2C_SDA/SCL, SPI, CAN_H/L). Name the test point after the net it taps
    (`TP_GND`, `TP_UART_TX`). This is what makes board bring-up and debugging possible —
    a professional board without probe points is hard to service.
17. **Fill in the title block** — pass project metadata to the build call so the sheet
    carries revision/company/engineer for version tracking:
    `smart_schematic.build(rev="A", company="<org>", engineer="<name>")`. These populate
    the KiCad title block (title defaults to the project name, date is automatic).

**Robustness note (engine, since 2026-07-24):** a big (>50-part) FLAT script no longer
*hangs* the router — `build()` auto-derives functional blocks from the connectivity graph
and lays them out as one grouped, connectivity-clean sheet. Do NOT rely on this as an
excuse to skip grouping (rule 7): auto-grouping keeps a forgotten build from thrashing,
but hand-written `@subcircuit`/`block(...)` structure still reads far better. Multi-sheet
(`@subcircuit`-per-page, >50 parts) connects correctly across pages via global labels;
it currently opens with a benign KiCad "annotation" notice, so for a single deliverable
sheet the grouped single-sheet path is the cleanest.

After generating, sanity-check the produced schematic against these (power symbols
present, every IC has a decoupling cap, test points on rails/reset/buses, no `N$` net
names on important signals, ZERO manual `power:*`/PWR_FLAG parts in the .py, zero
placeholder connector-ICs, nearby clusters wired not labelled) BEFORE reporting done.

## TI-GROUNDED PER-FUNCTION DESIGN RULES — inline catalog (apply when the block appears)
> The full normative table with per-rule TI citations is spec §5.16 `FUNC` / §5.14 `ORIENT`
> / §5.15 `REFLAY`. This is the **actionable inline summary** so the rules apply even without
> opening the spec. Build the net structure so the engine renders each arrangement; values are
> starting points — the part datasheet wins (MET-3).

**Layout frame — every sheet (ORIENT / REFLAY):**
- Signal flows **left→right** (connectors/sources left, processing middle, outputs right);
  **+V pins point up, GND pins point down** (power symbol above the part, ground below, never
  sideways). Everything on the 100 mil grid.
- Rotate each part so its pin groups **face the block they connect to** (MCU's USB pins face
  the USB connector; regulator VIN left / VOUT right / GND down / FB divider on the output side).
- **MCU/main-IC = the page anchor**, centred: control cluster (reset+pull-up, BOOT strap,
  crystal+load caps, debug) on its LEFT; IO/ports/headers on its RIGHT.
- **Connectors at the page edge** facing out (input left, output right, power top), pin 1 marked.
- **Decoupling caps = one bank row** on the rail; **crystal = tight closed cluster** at the OSC
  pins; **series parts inline on the signal wire** (USB 22Ω, LED R, filter R) — no label break.
- In-section nets = **wires**; cross-section hop = **short FUNCTION-named label stub**; every
  unused pin = explicit no-connect.

**Per-function idioms (FUNC-1…25) — draw the block to its TI idiom:**
- **LDO (1):** IN→OUT L→R; ≥1 µF IN→GND, 1–10 µF X5R/X7R OUT→GND; `EN` tied to IN or driven,
  never floating; GND at bottom.
- **Buck / switcher (2):** 10 µF X5R close to VIN; 0.01 µF BOOT→SW bootstrap; SW→inductor→output
  caps; catch diode **cathode on SW, anode to GND**; set VOUT with R1/R2 from VOUT→FB = the
  internal ref, tapped *after* the output bulk cap; keep the SW node tight.
- **Board power input (3):** bulk (tantalum/aluminium, LF) **and** a ceramic (HF) as a pair at entry.
- **Ferrite π-filter supply (4):** ferrite on **+V only** (never the ground return); 0.1/0.01 µF
  inside the bead + 1000 pF outside; **do not** ferrite-filter digital/core rails.
- **Decoupling (5):** one 0.1 µF X7R **per power pin** + one bulk per rail group; smallest nearest
  the pin; rate every cap > 2× worst-case V.
- **MCU core (6):** decouple every VDD/VDDIO pin; VDDA/analog supply filtered separately (4);
  MCU is the central anchor.
- **Unused / high-Z IO (7):** pull every floating IO to a level (10 k–47 k); don't contend with a
  device's internal pull.
- **Boot / config straps (8):** pull **every** boot/config pin (10 k/47 k); **never tie a boot pin
  straight to GND or VIO**.
- **Reset (9):** hold reset in a known state until latched; pull-down any reset-status output; a
  reset cap > 22 pF.
- **Crystal (10):** crystal + its 2 load caps as one tight cluster at XI/XO; load-cap value from the
  datasheet *Crystal Requirements* table; cap grounds to GND.
- **I²C (11):** SDA/SCL are open-drain → **external pull-ups mandatory** (4.7 k–10 k) to the bus rail.
- **IO series-R (12):** 0 Ω default on bus IO; 22 Ω inline on USB D± / fast clocks, near the driver pin.
- **USB front-end (13):** D± as a 90 Ω differential pair; ESD array at the connector; optional CM
  choke bypassable with a 0 Ω DNP.
- **Op-amp (14):** decouple every package; keep the inverting-input node minimal; **terminate every
  unused section** (noninverting→mid-supply/ref, output→inverting follower).
- **Current-sense INA (15):** **Kelvin 4-wire** to the shunt, sense pair tight at the shunt;
  fixed-gain variant; ~1 nF at the sense/supply pins; output a labelled net.
- **SAR / diff ADC input (16):** buffer + RC charge-bucket (C0G/NP0) at the ADC pins; the REF
  decoupling cap sits directly at REF/GND, no via between.
- **Precision reference (17):** shunt/series ref (TL431) + set R's + output cap at the AREF pin.
- **ESD on connectors (18):** TVS/ESD array on **every** IO wired to an external connector, right at
  the connector.
- **RSVD pins (19):** leave unconnected — no trace, no test point.
- **RF match (20):** π-network before the antenna, 2 of 3 positions populated (unused = 0 Ω); the
  LC→antenna run is a controlled 50 Ω; decouple each RF supply pin at its pin.
- **LED indicator (21):** `rail/GPIO → series R → anode`, `cathode → GND`; R per colour
  (blue/white ~60 Ω, red/green/yellow ~270 Ω); high-current LED → external FET, not the GPIO.
- **Polarized / electrolytic cap (22):** `+` to the more-positive DC node; never reversed.
- **MOSFET half-bridge / power stage (23):** +V (drain) top, phase node middle, source/GND bottom;
  gate via ~20 Ω series R + a labelled net; high-side above, low-side below; repeated bridges
  column-aligned.
- **Test points (24):** on each supply rail, RESET/NRST, the main clock, and each primary bus
  (UART/I²C/SPI/CAN); name the TP after its net (`TP_UART_TX`).
- **DNP / DNI (25):** mark do-not-populate parts explicitly (value `NP`/`DNI`).

## Constraints / gotchas
- **Stays DYNAMIC for any circuit, no per-circuit code**: grounding via
  `skidl-part-search` + `auto_stub` + `@subcircuit` blocks all generalize. The only
  per-request artifact is a freshly-written `<project>.py`.
- This repo's `skidl-part-search` CLI needed a fix to run on Windows (an unconditional
  `import readline`, now made optional) — if working from an unpatched copy of this
  library, that CLI will crash on import on Windows; fall back to
  `python -m skidl.scripts.part_search_cli` after confirming the fix is present, or
  search the library directly via `SchLib`/`PartSearchDB` in Python.
- Component/wire visual spacing has **no official standard** (checked directly against
  IEEE 315, KiCad's own Library Conventions, Altium's docs, and OrCAD's DRC — all are
  silent on it) — `auto_stub`'s job is legibility, not conformance to a spec that doesn't
  exist. Don't invent a "clearance rule" to justify a design choice; it isn't standards
  business.
- Routing can fail on very dense flat circuits — keep `@subcircuit`s to roughly 5-15
  parts and let `auto_stub` fall back to labels rather than fighting a giant flat block.
- `.kicad_pcb` PCB layout is a separate step from schematic/netlist generation — this
  skill covers schematic + netlist; PCB placement happens afterward in KiCad itself
  (Pcbnew → Update PCB from Schematic), using the footprints already assigned via
  `footprint=` on each `Part()`.
- Don't clobber an existing circuit script — pick a new, clearly-named `<project>.py`.

## Quick reference
```
pip install -e .                                  # from repo root, once
skidl-part-search "<keyword>"                      # search parts (or: python -m skidl.scripts.part_search_cli)
skidl-part-search "<keyword>" --fields part_name,lib_name,description
python <project>.py                                # generate .net + one .kicad_sch per sheet (no .kicad_pro)
```
