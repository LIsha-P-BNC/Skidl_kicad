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

## BOM — always deliver it, never drop a component
Build a **full component list (BOM) with counts** from the input and confirm it with the
user before writing code. Every listed component must end up grounded, library-checked,
and wired. After generating, **cross-check the schematic/netlist part count against the
agreed list** — fix before reporting if anything's missing. If a component in an
image/datasheet is unclear, **ask** — never silently skip it.
The BOM (Ref, Value, Qty, `Lib:Name`, Footprint) is a **required deliverable** in the
final report.

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
   connectivity verification. On the MCP/Anvil path, that means polling `build(name, mode='status')`
   until `status:"done"` with `generated` listing `.kicad_sch` **and** `.kicad_pro` (a
   MISMATCH there = failed; the `.net` is still valid for PCB). "running in the background"
   with no confirmed file is NOT success. If only `.py` + `.net` exist, the build
   **stopped at the netlist — report it as INCOMPLETE**, not successful. Golden
   verification auto-runs in `_finish_build` and sets `verified` (cross-checks
   `.net` ↔ `.kicad_sch`: dropped parts GV-1, duplicate UUIDs GV-2, count parity GV-3).
   Only report **"BUILD SUCCESSFUL (Verified)"** when `verified` is true AND the
   human-assisted diffs vs. the source (component count, pin connectivity, `--matrix`,
   hierarchy) match. See spec [§10](../../../docs/schematic_generation_spec.md).
9. **Report**: parts (Lib/Name), ERC result, **validator result (0 errors)**,
   **confirmed `.kicad_sch` + `.kicad_pro`**, the BOM, the "datasheet vs board" note (per
   key IC: additions/changes + any compliance warning), and that the project opens in the
   Anvil CAD app.

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

After generating, sanity-check the produced schematic against these (power symbols
present, every IC has a decoupling cap, no `N$` net names on important signals) BEFORE
reporting done.

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
