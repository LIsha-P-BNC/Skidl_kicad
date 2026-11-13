---
name: skidl-circuit
description: >-
  Turn a circuit request into a correct SKiDL design that builds a netlist + KiCad
  schematic/project INSIDE the Anvil CAD app, through its MCP tools (build / parts). Use
  this WHENEVER someone wants to design/create/generate a circuit, schematic, PCB, netlist,
  or Anvil CAD/KiCad project from a text prompt, a circuit IMAGE/photo/diagram, a datasheet,
  or a spec DOCUMENT. Drives the app's own build() pipeline and part library — never raw
  python / pip / a CLI (those do not exist on a shipped install). Produces PROFESSIONAL,
  standard-compliant schematics (IEEE 315 / IPC-2612 + TI per-function idioms): meaningful
  net names, power/GND symbols, standard values + reference designators, decoupling caps,
  left-to-right flow. Generic and DYNAMIC — works for ANY circuit; never hardcodes one.
---

# SKiDL circuit builder (Anvil CAD app — via the MCP build/parts tools)

Someone describes a circuit as **text, an image, a document, or a datasheet** → you build a
**correct** design that produces `.net` + `.kicad_sch` (+ `.kicad_pro`) and opens in the
**Anvil CAD app**. You drive the app through its **MCP tools** — **never** raw `python`,
`pip install`, or a shell CLI (none of those exist on a shipped install). The lifecycle tools:

- **`get_app_state`** — LOOK before you ACT: the **FIRST call every turn**. It tells you WHICH
  project is open, WHO owns it (`ai` / `user` / `empty`), and the SCENARIO to follow
  (`no_project` → fresh build; `fill_empty` → build into the open blank; `modify_ai` → edit our
  design + rebuild; `adopt_manual` → adopt then edit; `modify_manual` → re-adopt, their file is
  truth). **Never overwrite a `user` file without warning + a backup**; if two projects are open
  and which one the user means is ambiguous, **ASK**.
- **`build`** — the schematic lifecycle, `mode=`:
  - `rules` → the MANDATORY design workflow + canonical circuit **body template**. **CALL FIRST
    for any new circuit** (no name/code needed); follow what it returns exactly.
  - `source` → the existing project's SKiDL source, to **EXTEND** it. **CALL FIRST whenever the
    user continues / adds to / changes a project that already exists** — take `source`, add the
    new block/sheet/section, rebuild the SAME name with the FULL extended script (every existing
    sheet kept).
  - `plan` → register the plan for a NEW project: `code` = a short human-readable plan
    (functional blocks, chosen parts + values, and the user questions you asked with their
    answers). **SHOW the plan and WAIT for explicit approval**; a new build is REFUSED
    (status `workflow_gate`) until a plan exists and you pass `confirm=True`.
  - `body` (**PREFER**) → `code` = ONLY nets/parts/connections (no imports / no build call — the
    template comes from `mode='rules'`). Pre-checked (syntax, real parts/pins, ERC) in seconds;
    on `precheck_failed` apply the returned `fix_suggestions` and call again.
  - `script` → `code` = a COMPLETE SKiDL script (hierarchical `@subcircuit` designs only).
  - `status` → poll the running build until `done` (`include_log` for the log tail).
  - `open` → show the project in the app. `bom` → BOM via the app's own column preset.
- **`parts`** — the part library, `action=`:
  - `search` → `items` = `"Lib:Name"` exact checks or free-text terms. A part is **missing ONLY
    when `total_matches` is 0**. Call **ONCE with the FULL list** before writing any circuit.
  - `describe` → `items` = `["Lib:Name"]`. Returns the exact `Part(...)` line + **EVERY pin
    {num, name, func}** + datasheet. **Never invent a pin** — use only what this returns.
  - `add` / `add_footprint` → create a REAL symbol/footprint from the part's **DATASHEET**
    (show the pin table, user confirms; pass `expected_pins`/`expected_pads`). Opt-in only.
- **`read_pdf`** — render a datasheet / schematic PDF to page images (the plain Read tool cannot
  open PDFs here). **`read_live`** / **`edit_schematic_live`** — read + batch-edit the OPEN
  schematic for small in-place changes (`add_wire`, `edit_value`, `rotate_component`,
  `label_to_wire`, `query_net`, …); one request with several changes = ONE call with several ops.

It must work for **ANY** circuit (DYNAMIC) — never hardcode a specific one; always build a fresh
design from what the user asked for.

## THE ORDER — non-negotiable (follow EVERY time, for any circuit)
1. **Recommend FIRST — before searching the library.** For the request, recommend the
   best part(s) with a **specs table + comparison** (part A vs B, which suits which use
   case). The user often does not know the exact part — advise them; don't lead with a
   library search.
2. **Read the datasheet — MANDATORY for every active IC.** For **each active IC you
   place** (regulator, MCU, transceiver, driver, op-amp, sensor, memory, etc.) fetch and
   read its datasheet — `read_pdf` for a PDF the user gave, `WebFetch` a URL, else
   `WebSearch "<mfr part> datasheet"` — to confirm **pin numbers/names, min/typ values, and
   the *Typical Application Circuit***. Not optional for active parts. Only **trivial passives**
   (R / C / L / LED / diode / connector) and truly well-known textbook parts may skip this.
   If the user supplied a datasheet/document/image, read that too (`read_pdf` for a PDF).
   **Never guess a value, a pin, or a connection.**
2b. **Source ↔ datasheet difference analysis**, when the user supplies an image/PDF/doc
   AND a datasheet exists for a key IC:
   - Read the **SOURCE** (image/PDF, via `read_pdf`) = the actual circuit → **this is what you
     draw.**
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
5. **Then check the library — `parts(action='search', items=[...])`** ONCE with the FULL list.
   Report clearly which confirmed parts exist (`total_matches` > 0) and which are missing
   (`total_matches` == 0). Confirm pins with `parts(action='describe')` before wiring.
6. **Never silently substitute a missing part.** If a part isn't in the library, report it and
   stop — adding one is a separate, explicit, user-confirmed step (`parts(action='add')`,
   pins from the datasheet).

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
- Match every **value** and **pin** exactly — never guess; confirm pins with
  `parts(action='describe')` before wiring.

## BOM — always deliver it, never drop a component
Build a **full component list (BOM) with counts** from the input and confirm it with the
user before writing code. Every listed component must end up grounded, library-checked,
and wired. After building, **cross-check the schematic/netlist part count against the
agreed list** — fix before reporting if anything's missing. If a component in an
image/datasheet is unclear, **ask** — never silently skip it.
The BOM (Ref, Value, Qty, `Lib:Name`, Footprint) is a **required deliverable** in the
final report; generate it with **`build(mode='bom', name=<project>)`** (the app's BOM engine,
using the project's own column preset).

## Ground every part — never invent one
Never write a `Part("Lib", "Name")` or a pin number you haven't confirmed exists.
1. **`parts(action='search', items=[...])`** → real `Lib:Name` combinations (one call, full
   list). A part is missing ONLY when `total_matches` is 0.
2. **`parts(action='describe', items=["Lib:Name"])`** → the exact `Part(...)` line + EVERY pin
   {num, name, func} + datasheet. Use ONLY those pins — never invent one.

## Build steps
1. **`get_app_state` FIRST** (LOOK before you ACT; `read_live` too if a project is open), then
   **understand** the circuit from the prompt/image/document/datasheet; list components +
   connections. For a PDF use `read_pdf`. For each active IC read its datasheet and do the
   source↔datasheet difference analysis (THE ORDER 2/2b). Ask a short clarifying question only
   if a value/part is genuinely ambiguous.
2. **New circuit → `build(mode='rules')` FIRST** (no name/code needed) to get the MANDATORY
   workflow + canonical body template; follow it exactly.
   **Continuing an existing design → `build(mode='source', name=<project>)` FIRST**: take the
   returned source, ADD the new block/sheet/section, and rebuild the SAME name with the FULL
   extended script (every existing sheet kept). A small in-place tweak → `edit_schematic_live`.
3. **Ground every part** with `parts(action='search')` + `parts(action='describe')` (above).
   Note real names/pins.
4. **Register + confirm the plan → `build(mode='plan', name=<project>, code=<short plan>)`.**
   The plan = functional blocks + chosen parts/values + the user questions you asked with their
   answers. **SHOW it to the user and WAIT for explicit approval.** (A new-project build is
   REFUSED — status `workflow_gate` — until a plan exists and you pass `confirm=True`.)
5. **Build → `build(mode='body', name=<project>, code=<nets/parts/connections>, confirm=True)`**
   (PREFER `body`: just nets/parts/connections — no imports, no build call; the template comes
   from `mode='rules'`). It is pre-checked (syntax, real parts/pins, ERC) in seconds; on
   `precheck_failed` apply the returned `fix_suggestions` and call again. For a hierarchical
   multi-sheet design use `mode='script'` with a COMPLETE `@subcircuit` script. Apply the
   **SENIOR-DESIGNER DISCIPLINE** + **PROFESSIONAL SCHEMATIC RULES** below when writing the code.
6. **Poll `build(mode='status', name=<project>)` until `status:"done"`** (`include_log` for the
   build-log tail). ERC-clean + netlist is NOT a finished build (GEN-3): only `done` with the
   `.kicad_sch` artifact present is success; the app's golden verification runs in the pipeline.
   If it stops at `.net`, report it as **INCOMPLETE**, not successful.
7. **Self-audit** (below) — re-read the design against the source and fix anything before
   reporting: every part present (incl. `NP`), LED series-R + polarity, pull resistors to a real
   signal, config/boot/reset pins tied, sheet structure sane.
8. **Open + BOM:** `build(mode='open', name=<project>)` once to show it in Anvil CAD;
   `build(mode='bom', name=<project>)` for the BOM.
9. **Report (in Tanglish):** parts (Lib/Name), ERC/precheck result, confirmed `.kicad_sch`
   (+ `.kicad_pro`), the BOM, the "datasheet vs board" note (per key IC: additions/changes + any
   compliance warning), and that it opened in the Anvil CAD app.

## Self-audit — run AFTER building, BEFORE reporting done
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
   call spins off its own page — a repeated unit must be a plain, undecorated helper
   function so it stays on the parent sheet; verified to reach 100+ pages from 50 repeated
   calls otherwise). Page-count model: pages ≈ 1 (top) + number of `@subcircuit` *call sites*.
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
  BEFORE declaring done; clear every ERC/precheck flag OR document why it's waived — no silent violations.
- **Margin (MET-7):** derate (commercial ~80 % V/I, 70 % P); worst-case the critical values; FMEA mindset.
- **Document the WHY (MET-8):** capture design intent + non-obvious value rationale in the report.
- **Verify, don't infer (MET-9):** report what the build/status/BOM actually confirmed vs assumed (= GEN-3).
- **Rules are defaults (MET-10):** break one only with a documented reason; electrical-safety (`ELE`)
  limits never bend — a reversed diode/LED, a dropped LED series-R, a floating enable, or shorted rails
  is never a "style" override: explain, propose the fix, and confirm.
- **Vetted values (MET-11):** derive every component VALUE from the standard formulas and state
  the formula + inputs in the report (LED R = (Vs−Vf)/I; divider; I²C pull-up window; AN2867
  crystal caps; 555: f = 1.44/((R1+2R2)·C), t = 1.1·R·C; regulator FB divider; buck:
  L = Vout·(Vin−Vout)/(Vin·ΔIL·fsw) with ΔIL ≈ 0.3·Iout, Isat ≥ 1.3·Ipeak; boost:
  L = Vin·D/(ΔIL·fsw), IL = Iout/(1−D); shunt: R = Vsense/Imax with 2× power derate), rounded to
  E-series. Never assert a bare number; a value with no formula is from the datasheet — cite it;
  for a switcher the IC datasheet's own component table wins over the generic sizing.
- **Change discipline (MET-12):** before ALTERING an existing design, call the **`plan_change`**
  MCP tool with the user's request (+ project name). It returns the ONE layer the change lives in
  (requirement / part_swap / topology / value / refactor / layout / manufacturing), the MINIMAL
  edit path, the verification, and the blast radius — follow it. A value change is
  `edit_schematic_live(edit_value)` / `edit_schematic(set_value)` — NEVER a rebuild; a layout
  problem never touches the schematic; a refactor must leave the netlist identical;
  `layer:'unclear'` → ask the user its one question instead of guessing. Smallest edit that
  fixes the root cause; protect everything proven.

## PROFESSIONAL SCHEMATIC RULES — bake these into EVERY build
> **Normative reference:** these rules are formalized with MUST/SHOULD/MAY levels, stable
> rule IDs, and a machine-checkable checklist in
> [docs/schematic_generation_spec.md](../../../docs/schematic_generation_spec.md). The
> numbered guidance below is the working summary; the spec is the source of truth. For how to
> DRAW each recurring block (LDO, buck, MCU, crystal, reset/boot straps, I²C/SPI/USB, op-amp,
> INA current-sense, SAR-ADC input, RF match, ESD, LED, half-bridge) to its TI-grounded idiom,
> see spec **§5.16 `FUNC`** (TI SLOA089 / SPRACU5 / datasheet typical-application circuits).

The app's **build() engine** (`smart_schematic` + `auto_stub`) already does the *drawing* —
placement, routing, wire vs. label classification, power-symbol emission. Your job when writing
the `body`/`script` `code` is to feed it clean inputs so the result is standard-compliant
(IEEE 315 / IEC 60617 graphic-symbol conventions). Apply ALL of these:

1. **Meaningful net names (UPPERCASE, underscores).** Name every signal net you care
   about — never leave it to auto `N$1`. `sda = Net('I2C_SDA'); tx = Net('UART_TX');
   en = Net('MOTOR_EN')`. Function, not location. Active-low → `Net('RESET_N')`
   (ASCII-safe `_N` suffix — this is what SKiDL/KiCad net matching actually keys off of).
   KiCad's own native display convention for an active-low signal is a text overbar,
   written `~{NAME}` in any KiCad text field — `_N` is the portable equivalent most tools
   (including this one) use for the net name itself.
2. **Power rails must use engine-recognized names so they render as power SYMBOLS, not
   wires.** The engine's power-net detector recognizes: `+<num>V` (e.g. `+5V`, `+3.3V`,
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
7. **Left→right signal flow, functional grouping**: each functional area in its own
   `@subcircuit`, higher-level/input signals conceptually first. Use `&` for clean series
   chains (`vin & r1 & vout & r2 & gnd`).
8. **Buses for multi-bit signals**: `data = Bus('DATA', 8)` instead of 8 separate nets —
   this creates sequential per-bit nets `DATA0..DATA7`. Keep bus-member names sequential
   and numeric (never rename bits ad hoc) — that's what lets a bus render with `[7..0]`
   bracket-range notation, the standard EDA convention (Altium, KiCad) for labeling the
   bus wire itself, distinct from each bit's own net label.
9. **Account for every pin**: tie unused/NC IC inputs to a defined level (pull or GND) —
   don't leave inputs floating, it shows up as ERC warnings.
10. **Net identity is by Python object, NOT by name string — a real SKiDL gotcha.**
    Calling `Net('EN')` twice in two different `@subcircuit`/helper functions creates TWO
    separate, unconnected nets that just happen to share a display name — SKiDL will not
    merge them, and won't warn either. Only actual power-rail names (rule 2) are treated as
    globally shared. So: (a) never reuse a generic local name (`en`, `sel`, `clk`, `rst`)
    across unrelated `@subcircuit`s assuming they'll connect — they won't; (b) for a
    non-power signal that genuinely must cross subcircuits, pass the *same* `Net` object in
    as a function argument, don't just repeat the string name.
11. **Let the engine classify wire vs. label — don't force it.** The build engine runs
    `auto_stub` (power symbols for rails, wires for local clusters, labels only for far /
    congested / cross-sheet nets). Write clean nets/parts/connections and keep a block's
    parts tightly related (5–15 parts) so local connections stay wires; don't fight the
    placer with a giant flat block.

After building, sanity-check the produced schematic against these (power symbols present,
every IC has a decoupling cap, no `N$` net names on important signals, LEDs wired not across a
rail, nearby clusters wired not labelled) BEFORE reporting done.

## Constraints / gotchas
- **Stays DYNAMIC for any circuit, no per-circuit code**: grounding via `parts` + the
  `auto_stub` wire/label engine + `@subcircuit` blocks all generalize. The only per-request
  artifact is the freshly-written `body`/`script` `code`.
- **Missing part** → `parts(action='add')` (symbol) / `parts(action='add_footprint')`
  (footprint), pins/pads **from the datasheet**, user-confirmed, with `expected_pins` /
  `expected_pads`. Never fake an IC with a generic connector.
- **PDFs** (datasheets, source schematics) → always `read_pdf` (the plain Read tool cannot open
  a PDF here); then Read the returned page images.
- **One project = ONE name, forever.** Iterating/fixing a design REBUILDS under the SAME name
  (via `mode='source'` → extend → rebuild); never mint `_v2`/`New` names — that strands stale
  duplicate projects and stale windows.
- Routing can fail on very dense flat blocks — keep `@subcircuit`s to roughly 5–15 parts and
  let `auto_stub` fall back to labels rather than fighting a giant flat block.
- `.kicad_pcb` PCB layout is a separate step — this skill covers schematic + netlist; PCB
  placement/routing is the `pcb-place-route-fix` skill + the app's PCB tools, using the
  footprints already assigned on each `Part()`.
- Don't clobber an existing circuit — `get_app_state` first; a `user`-owned file needs a warning
  + backup before any change.

## Quick reference (MCP tools — NO shell / pip / CLI on a shipped install)
```
get_app_state()                                              # LOOK first, every turn
build(mode='rules')                                          # MANDATORY workflow + body template (new circuit)
parts(action='search',  items=['Lib:Name', 'free text', ...])# check the library (ONE call, full list)
parts(action='describe', items=['Lib:Name'])                 # exact Part() line + every pin
build(mode='plan',   name='<project>', code='<short plan>')  # show plan, WAIT for OK
build(mode='body',   name='<project>', code='<nets/parts/connections>', confirm=True)
build(mode='status', name='<project>')                       # poll until done
build(mode='open',   name='<project>')                       # open in Anvil CAD
build(mode='bom',    name='<project>')                       # BOM
# existing design: build(mode='source', name='<project>') -> extend -> rebuild SAME name (full script)
# small in-place edit on the OPEN schematic: edit_schematic_live(ops=[...])
```
