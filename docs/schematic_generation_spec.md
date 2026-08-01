# SKiDL Schematic Generation Specification

**Professional design rules for LLM-based circuit generation**

- **Version:** 1.2
- **Status:** Normative
- **Applies to:** SKiDL → KiCad 6–9 schematic/netlist generation in this repository
- **Companion tooling:** `python -m skidl.scripts.validate_design <project>.py [--netlist <project>.net] [--matrix]`

---

## 1. Purpose and scope

This document defines the electrical, structural, and drafting conventions that every
**generated** SKiDL design MUST follow to produce professional, readable, and
standards-aligned KiCad schematics. It is written to be usable in two ways:

1. as an **engineering style guide** for humans reviewing generated designs, and
2. as an **LLM system-prompt reference** and machine-checkable rule set — every normative
   rule has a stable ID (e.g. `PWR-1`) and a *verifiability class*, so a linter can report
   violations by ID.

It governs **schematic + netlist** generation only. PCB layout (`.kicad_pcb`) is out of
scope.

---

## 2. Terminology (RFC 2119)

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are to be
interpreted as described in RFC 2119.

| Level | Meaning |
| --- | --- |
| **MUST** / **MUST NOT** | Absolute requirement. Violating it produces an incorrect, misleading, or broken schematic. A conforming generator MUST NOT emit such a design. |
| **SHOULD** / **SHOULD NOT** | Strong engineering recommendation. May be overridden only with a documented, deliberate reason recorded in the design report. |
| **MAY** | Optional; a style preference that improves readability. |

## 3. Guiding principle

> The generator describes **electrical connectivity and design intent** — *not* graphical
> drawing.

The schematic engine (`generate_schematic(...)`, or `smart_schematic.build(...)`) decides
whether each connection is rendered as a **wire**, a **net label**, a **global label**, a
**power symbol**, or a **hierarchical sheet pin**, based on circuit topology. The author's
job is to feed it clean, correct connectivity — never to force a visual form.

## 4. Verifiability classes

Each rule is tagged with how it can be checked, which determines what the validator can
enforce automatically:

| Class | Meaning | Checked by |
| --- | --- | --- |
| **Static** | Decidable from the `.py` source (AST / regex). | `validate_design` (no execution) |
| **Netlist** | Requires the generated `.net` (connectivity graph). | `validate_design --netlist` |
| **Artifact** | Requires the produced output files on disk (`.kicad_sch`, `.kicad_pro`). | Build tool (`build_status` / `_finish_build`) |
| **Human** | Requires datasheet/engineering judgment. | Reviewer; validator emits a reminder only |

---

## 5. Rules

### 5.1 Net naming — `NET`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **NET-1** | MUST | Every meaningful signal net has a descriptive, function-based name. `UART_TX`, `I2C_SDA`, `SPI_MOSI`, `MOTOR_EN`, `RESET_N`. | Static |
| **NET-2** | MUST NOT | Do not ship auto/placeholder net names on meaningful signals: `N$1`, `wire3`, `net7`. | Static |
| **NET-3** | SHOULD | Net names are UPPERCASE, underscore-separated, and name the *function*, not the physical location. | Static |
| **NET-4** | MUST | Active-low signals use the ASCII `_N` suffix (`RESET_N`, `CS_N`, `ENABLE_N`). Do not use `/RESET` or a Unicode overbar in the net name. (A KiCad *text-field* overbar `~{NAME}` is a display-only convention and is fine in labels, not net names.) | Static |
| **NET-5** | MUST | **Net identity is by Python object, not by name.** A signal shared between functional blocks MUST be created once and the *same `Net` object* passed to each block. Calling `Net("EN")` in two blocks creates two unconnected nets. | Static (heuristic) / Human |
| **NET-6** | SHOULD | A shared net created at module level SHOULD reach at least one block. A module-level signal net never passed into any `@subcircuit` is likely a missed connection or a dead net. | Static |

*Rationale (NET-5):* Unlike hand-drawn KiCad — where identical label text connects — SKiDL
does not merge same-named nets across function scopes and does not warn. Only power nets
(§5.2) are globally shared by name.

```python
# WRONG — two separate, unconnected nets
def block1(): en = Net("EN"); ...
def block2(): en = Net("EN"); ...

# RIGHT — one net object, shared
en = Net("EN")
block1(en)
block2(en)
```

### 5.2 Power nets — `PWR`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **PWR-1** | MUST | Power/ground rails are expressed as **net names** matching the engine's power pattern; the engine then emits power symbols automatically. | Static |
| **PWR-2** | MUST NOT | Never instantiate power/ground/flag **parts** by hand: `Part("power", "+5V")`, `Part("power", "GND")`, `PWR_FLAG`. This is the #1 duplicate-parts bug. | Static |
| **PWR-3** | MUST | Use one ground net (`GND`) for the whole circuit; all grounds tie to it. | Static (heuristic) |
| **PWR-4** | SHOULD | A rail name that is *meant* to be power but does not match the pattern (e.g. `VCC_3V3`) MUST be renamed to a matching form (`+3.3V`), or it renders as an ordinary wire. | Static (heuristic) |
| **PWR-5** | MUST | Power/ground nets connect **globally by name** and MUST NOT be threaded through `@subcircuit` parameters as ports. (This is the sole exception to NET-5 / HIER-3.) | Human |

**Recognized power-net pattern** (authoritative — from `_POWER_NET_RE` in
`src/skidl/tools/kicad*/gen_schematic.py`), case-insensitive, plus any name starting with
`+`:

```
^(\+\d[\d.]*V[\d]*|GND|AGND|DGND|PGND|VCC|VDD|VSS|VEE|VBUS|VBAT|AVCC|AVDD|DVCC|DVDD)$
```

Examples that match: `+5V`, `+3.3V`, `+12V`, `+1.8V`, `GND`, `AGND`, `DGND`, `PGND`,
`VCC`, `VDD`, `VSS`, `VEE`, `VBUS`, `VBAT`, `AVCC`, `AVDD`, `DVCC`, `DVDD`.
Does **not** match: `VCC_3V3`, `3V3`, `POWER`, `+3.3` (no `V`).

### 5.3 Reference designators — `REF`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **REF-1** | MUST | Use standard IEEE-315 prefix letters (table below). Never `Resistor1`, `IC5`, `Chip1`. | Static |
| **REF-2** | SHOULD | Multi-unit / matched devices share one base number with a suffix letter: `U10A`/`U10B`/`U10C`/`U10D`, `R17A`/`R17B`. | Human |

| Prefix | Component | Prefix | Component |
| --- | --- | --- | --- |
| `R` | Resistor | `Y` | Crystal |
| `C` | Capacitor | `X` | Oscillator |
| `L` | Inductor | `J` | Connector |
| `D` | Diode / LED | `K` | Relay |
| `Q` | Transistor | `SW`/`S` | Switch |
| `U` | Integrated circuit | `F` | Fuse |
| `T` | Transformer | `TP` | Test point |

*Note:* IEEE 315 defines the reference *letter*; the *numbering* convention is covered by
ASME Y14.44 (successor to ANSI Y32.16). Do not over-attribute numbering to IEEE 315.

### 5.4 Component values — `VAL`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **VAL-1** | MUST | Use engineering notation: `10k`, `4.7k`, `1M`, `100nF`, `4.7uF`, `10uF`, `10nF`, `0R`. | Static |
| **VAL-2** | MUST NOT | Do not use raw or malformed values: `10000`, `0.0000001F`, `1000000`. | Static |

### 5.5 Electrical correctness — `ELE`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **ELE-1** | MUST | Every IC supply pin (VDD/VCC/AVCC/…) has a local `100nF` decoupling cap to `GND`, created in the **same functional block** as the IC. | Netlist / Human |
| **ELE-2** | SHOULD | One bulk cap (`4.7uF`–`10uF`) per supply rail at its entry point. | Netlist / Human |
| **ELE-3** | MUST | Control signals never float: reset/boot/enable/CS pins are pulled to a defined level (e.g. `RESET_N → 10k → +3.3V`, `BOOT → 10k → GND`). | Human |
| **ELE-4** | MUST | Pull/bleed resistors connect to a *real* signal net — not a dead `rail → GND` unless that is deliberately part of the design (e.g. a bleeder). | Netlist / Human |
| **ELE-5** | MUST | I²C `SDA`/`SCL` have pull-ups (`4.7k` to the bus rail). | Human |
| **ELE-6** | MUST | Every LED has a current-limiting resistor and correct polarity: `rail → R → anode`, `cathode → GND`. Never straight across a rail. | Netlist / Human |
| **ELE-7** | MUST | Unused digital inputs are tied to a defined logic level unless the datasheet says otherwise. | Human |

### 5.6 Functional organization — `ORG`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **ORG-1** | SHOULD | Group the design by function (Power, MCU, Sensors, Communications, UI, Outputs). Mandatory above ~12 parts. | Human |
| **ORG-2** | SHOULD | Signal flow reads left→right / input→processing→output. | Human |

### 5.7 Hierarchy — `HIER`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **HIER-1** | SHOULD | Small designs use in-sheet blocks (`with smart_schematic.block("NAME"):`); large designs (>50 parts) use one `@subcircuit` per major subsystem. | Human |
| **HIER-2** | MUST NOT | **Never call an `@subcircuit`-decorated function inside a loop or more than once for repeated identical units.** One call = one sheet; `for i in range(32): channel()` on an `@subcircuit` = 32 pages. Repeated units MUST be plain, undecorated helper functions. | Static |
| **HIER-3** | MUST | Signals crossing block boundaries are passed as the **same `Net` object** into each `@subcircuit`; those parameters become the sheet pins (ports). (Power is the exception — see PWR-5.) | Static (heuristic) / Human |
| **HIER-4** | SHOULD | Page count ≈ `1 + number of @subcircuit call sites`. Reserve `@subcircuit` for genuinely distinct blocks to land a big design at a readable ~4–8 pages. | Human |
| **HIER-5** | MUST NOT | Do not reassign a sheet-pin parameter to a fresh `Net(...)` inside the block. `def controller(uart_tx): uart_tx = Net("UART_TX")` discards the parent's net object and silently breaks the cross-sheet connection. Use the parameter directly. | Static |

### 5.8 Connection methods — `CONN`

The engine chooses the visual form; the author only chooses net structure.

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **CONN-1** | MUST | The generation call enables auto-stubbing (`auto_stub=True`, which `smart_schematic.build` sets for you). Without it the engine will not emit power symbols or fall back to labels on congested nets, and the wire-vs-label policy below does not apply. | Static |
| **CONN-2** | MUST NOT | Do not force a visual form (do not hand-draw long power wires, do not label every connection, do not manually place symbols). Let the engine decide. | Human |

Engine policy (informative — do not fight it):

- **Wire** — parts that are close together (LED + R, crystal + caps, a transistor stage);
  function-critical nets (clock, reset, decoupling, USB-diff).
- **Net label** — distant / crowded / high-fanout signals on the same sheet.
- **Global label** — only true cross-sheet signals in a multi-sheet design.
- **Power symbol** — every power rail (§5.2).
- **Sheet pin** — an `@subcircuit` parameter (port).

### 5.9 Buses — `BUS`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **BUS-1** | SHOULD | Multi-bit signals use `Bus("DATA", 8)` (creates sequential `DATA0..DATA7`) rather than N individual nets. Keep numbering sequential and numeric so the bus renders with `[7..0]` range notation. | Static (heuristic) |

### 5.10 Test points — `TP`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **TP-1** | SHOULD | Add test points on commonly probed nets: every supply rail, `GND`, `RESET`/`NRST`, main clock, and each primary bus (UART TX/RX, I²C SDA/SCL, SPI, CAN). Name them after the net: `TP_GND`, `TP_3V3`, `TP_UART_TX`. | Human |

### 5.11 Real components — `LIB`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **LIB-1** | MUST NOT | Never substitute a generic connector (`Conn_01x40`, etc.) for a real IC/MCU — it has no pin names or ERC pin types, so miswiring passes silently. | Static (heuristic) / Human |
| **LIB-2** | MUST | Use a genuine library symbol. If the exact part is missing, use a pin-compatible symbol from the same family and set `value=` to the intended part; only add a library entry as a last resort. | Human |
| **LIB-3** | MUST | Never write a `Part(...)` or a pin number that has not been confirmed to exist (via `skidl-part-search` or by instantiating and inspecting the part). | Human |

### 5.12 Metadata & stability — `META`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **META-1** | MUST | Every `Part(...)` has a stable, author-chosen `tag=` (e.g. `tag="r1"`). Omitting it auto-generates a random tag per run, breaking UUID/identity stability across regenerations. | Static |
| **META-2** | MUST | Every `@subcircuit` **call** also passes a stable `tag=` (e.g. `power_regulation(..., tag="power_blk")`) — the block gets its own missing-tag warning and its tag becomes the sheet filename suffix. | Static |
| **META-3** | SHOULD | Provide project metadata — name, revision, company, engineer — via the build call (`smart_schematic.build(rev="A", company=..., engineer=...)`) so the KiCad title block carries version info. | Static (heuristic) |
| **META-4** | MUST | One circuit = one project name, forever. Iterate/rebuild under the **same** name; never mint `_v2`/`New` variants. | Human |

### 5.13 Generator invocation — `GEN`

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **GEN-1** | SHOULD | Keep `flatness=0.0` (the default). `flatness` is **not** a page-count control — only `flatness=1.0` merges repeated pages, and that erases *all* hierarchy. Control page count at the source (HIER-2), not via this parameter. | Static |
| **GEN-2** | MAY | Tune `auto_stub_fanout` / `auto_stub_max_wire_pins` / `auto_stub_max_wire_dist` only when a dense block keeps failing to route — do not lower them pre-emptively. | Human |
| **GEN-3** | MUST NOT | Do not report the build as successful ("schematic generated", "Build Successful", "verified") until the `.kicad_sch` file actually exists on disk **and** passes connectivity verification (no MISMATCH). A run that stops at `.net` is INCOMPLETE — report it as such (see §10). | Artifact |

---

## 6. Decision algorithm

For every connection, in order:

1. **Is it a power rail?** → name it to match the power pattern (`+3.3V`, `+5V`, `GND`).
   The engine emits power symbols. Do **not** pass it through `@subcircuit` ports. (PWR-1, PWR-5)
2. **Is it inside one functional block?** → use the same `Net` object; let the engine pick
   wire vs. label. (NET-5, CONN-2)
3. **Does it connect multiple blocks?** → create the `Net` in the parent and pass the *same*
   `Net` object into every `@subcircuit`; these become sheet pins. (HIER-3)
4. **Is the design multi-sheet?** → use hierarchy; only genuine cross-sheet signals become
   global labels. (HIER-1)
5. **Never recreate a net by name.** Always pass the original `Net` object. (NET-5)

---

## 7. Concept mapping

| Schematic concept | SKiDL representation |
| --- | --- |
| Electrical connection | `Net` |
| Local connection | Same `Net` object |
| Wire | Generated automatically |
| Net label | Generated automatically |
| Global label | Generated automatically for cross-sheet signals |
| Power symbol | Net name matching the power pattern (§5.2) |
| Functional block | `@subcircuit` |
| Hierarchy sheet | One `@subcircuit` **call site** |
| Sheet pin | `@subcircuit` function parameter |
| Bus | `Bus(...)` |
| Test point | `Part("Connector", "TestPoint")` |

---

## 8. Validation checklist

Automated (run `python -m skidl.scripts.validate_design <project>.py --netlist <project>.net`):

- [ ] **PWR-2** No `Part("power", …)` / `PWR_FLAG` instantiated.
- [ ] **PWR-4** No rail-looking net name fails the power pattern.
- [ ] **NET-2** No `N$…` / `wire\d` / `net\d` names on meaningful nets.
- [ ] **NET-4** No `/RESET` or Unicode-overbar active-low net names.
- [ ] **NET-5** No same-named non-power `Net(...)` created in two different function scopes.
- [ ] **NET-6** No module-level shared net left unpassed to any block (run `--matrix` to see).
- [ ] **HIER-2** No `@subcircuit`-decorated function called in a loop / more than once.
- [ ] **HIER-5** No sheet-pin parameter reassigned to a fresh `Net(...)` inside a block.
- [ ] **META-1** Every `Part(...)` has `tag=`.
- [ ] **META-2** Every `@subcircuit` call passes `tag=`.
- [ ] **CONN-1** Generation call has `auto_stub=True` (or uses `smart_schematic.build`).
- [ ] **GEN-1** `flatness` is `0.0` or `1.0`, not an intermediate value.
- [ ] **VAL-1/2** Every `value=` string is engineering notation.
- [ ] **REF-1** No non-standard `ref=` designators.
- [ ] **LIB-1** No generic connector used where an IC is expected.
- [ ] **ELE-6** (netlist) No LED with both pins on power nets / no series R.
- [ ] **ELE-1** (netlist) Each supply rail has decoupling caps present.

Manual (Human class — reviewer confirms against datasheet/intent):

- [ ] **ELE-1/3/5/7** Decoupling per IC pin; config pins at defined levels; I²C pull-ups; no floating inputs.
- [ ] **LIB-2/3** Every part and pin confirmed real; compatible substitutions valued correctly.
- [ ] **ORG-1/2, HIER-1/4** Sensible functional grouping and page count; left→right flow.
- [ ] **TP-1** Test points on rails/reset/clock/buses.
- [ ] **META-4** Same project name as prior revisions.
- [ ] **§9.3** Diff the generated `--matrix` against the hand-authored Net Connectivity
      Matrix (and the source Altium/PDF) — confirm every intended net × block cell is present.

---

## 9. Authoring workflow & connectivity matrix

Correctness is easier to achieve by **writing the design in a fixed order** than by
retro-fitting connections. Follow this order; it makes HIER-3 / NET-5 / HIER-5 violations
almost impossible to introduce.

### 9.1 Coding order (normative for hierarchical designs)

```text
 1. Imports                    from skidl import *
 2. Tool / libraries           set_default_tool(KICAD9)
 3. Shared nets                every cross-block signal + every power rail, created ONCE
 4. Global constants           values, part refs, config
 5. @subcircuit blocks         one per subsystem: power, controller, SIM, memory, GSM, interfaces
 6. Top-level instantiation    call each block, passing the shared Net objects
 7. ERC()
 8. generate_netlist(...)
 9. generate_schematic(..., auto_stub=True)
```

**Why this order works:**

- Creating **all shared nets first, once, at module level** (step 3) guarantees NET-5 /
  HIER-3 — every block receives the *same* `Net` object, so cross-sheet connections
  cannot silently split.
- Declaring **local nets only inside their block** (e.g. `XTAL1`/`XTAL2` inside the MCU
  block) keeps ports minimal and honours HIER-3.
- Instantiating **after** the blocks are defined (step 6) is where the shared objects
  become sheet pins.

### 9.2 Per-net decision procedure

For every net, in order:

1. **Does this signal leave its block (go to another sheet)?**
   → **Yes:** it is a sheet pin — create it at module level and pass it as a function
   argument. **No:** create it as a local `Net(...)` inside the block.
2. **Was it already created in the parent?** → **Yes:** pass the *same object*; never
   call `Net("SAME_NAME")` again (NET-5). **No:** it is local.
3. **Is it a power rail?** → **Yes:** name it to match the power pattern (`+3.3V`, `GND`);
   it connects globally by name — do **not** thread it through ports (PWR-5).

### 9.3 Net Connectivity Matrix — recommended pre-design artifact

For a multi-sheet design, list **which signal goes to which sheet** *before* writing
Python — a table of net × block. It is the ground truth you check the generated schematic
against (e.g. versus an Altium schematic or a PDF) to confirm **no net is missed**.

| Net | power/GND | controller | GSM | interfaces | … |
| --- | :---: | :---: | :---: | :---: | :---: |
| `+3.3V` | ● | ● | | | |
| `GND` | ● | ● | ● | ● | |
| `UART_TX` | | ● | ● | | |
| `CAN_TX` | | ● | | ● | |

The validator **derives this matrix from the finished code** so you can diff intent vs.
result:

```
python -m skidl.scripts.validate_design tracker.py --matrix
```

Any signal shown as `(not passed to any block)` is a candidate missed connection
(also reported as **NET-6**); any signal reaching only one block is a candidate *local*
net that need not be a sheet pin.

## 10. Build completion & success criteria

Generating correct Python and a clean ERC/netlist is **not** a finished build. The
deliverable is a schematic the user can open. Do not conflate "the code ran" with "the
schematic exists."

### 10.1 Output artifacts

A complete build produces, on disk:

```
<project>.py            # the SKiDL source
<project>.net           # netlist (valid for PCB even if the schematic step fails)
<project>.kicad_sch     # THE schematic — the actual deliverable
<project>.kicad_pro     # project file (MCP/smart_schematic path; plain SKiDL does not emit this)
```

If only `.py` + `.net` exist, the build **stopped at the netlist** — that is
**INCOMPLETE**, not successful.

### 10.2 Success gate (GEN-3) — normative

**MUST NOT** claim "schematic generated" / "Build Successful" / "verified" until **all**
of the following hold. State the build as *incomplete* (and why) if any fails:

1. ✅ Python generated
2. ✅ Static validation passed (0 MUST errors — `validate_design`)
3. ✅ ERC passed (0 errors)
4. ✅ Netlist `.net` generated
5. ✅ **`.kicad_sch` file actually exists on disk**
6. ✅ Connectivity verification passed (no `MISMATCH`; via `build_status` → `status:"done"`)
7. ✅ Opens in KiCad / the schematic tool
8. ✅ No missing symbols, no duplicate UUIDs, no floating/undriven important nets
9. ✅ Connectivity matrix (`--matrix`) matches the intended net→sheet plan

### 10.3 How the tooling enforces it

- **MCP path:** the real `skidl_mcp_server.py` gates on this — `_finish_build` sets
  `status:"done"` only when `files.get("net") and files.get("kicad_sch") and rc == 0`, and
  flips a MISMATCH to `failed`. Poll `build_status(name)` until `status:"done"`; a response
  with `generated` listing `.kicad_sch` is the proof. "running in the background" / "no
  polling tool" is **not** proof — if you cannot confirm the `.kicad_sch` file, the build
  is not done.
- **Plain-SKiDL path:** confirm `generate_schematic(...)` ran and the `.kicad_sch`
  file(s) are on disk before reporting done. (Plain SKiDL does not emit `.kicad_pro`;
  KiCad creates it on first open.)

### 10.4 Golden verification (Phase 10) — the schematic matches the design

A `.kicad_sch` that *exists and opens* can still be wrong: a resistor may be dropped, a
symbol missing, or `UART_TX` wired to the wrong pin. **Golden verification** cross-checks
the produced artifacts before declaring **BUILD SUCCESSFUL (Verified)**. It splits into:

- **Automatable — internal consistency of `.net` ↔ `.kicad_sch`** (run
  `validate_design <py> --golden`; auto-runs in the MCP `_finish_build`, sets `verified`):

  | ID | Level | Check |
  | --- | --- | --- |
  | **GV-1** | MUST | Every component in the `.net` has a placed symbol in some `.kicad_sch` (no dropped parts). |
  | **GV-2** | MUST | No duplicate UUIDs across sheets (symbols and sub-sheets). |
  | **GV-3** | SHOULD | Component count parity: netlist components == placed symbols (extras = power symbols). |
  | **GV-4** | MUST | At least one `.kicad_sch` parsed; project structure sane (`.kicad_pro` note only — KiCad creates it on open). |
  | **GV-5** | INFO | Sheet count vs the hierarchy plan (`1 + @subcircuit` call sites); flattening a small design to one sheet is legitimate. |

- **Human-assisted — generated vs. the ORIGINAL source** (image / Altium / PDF). No tool
  can do this without a machine-readable source, so confirm these against the BOM +
  connectivity matrix:
  1. **Component-count diff** — source vs generated.
  2. **Pin-connectivity diff** — is each pin on the correct net?
  3. **Net-matrix diff** — do shared nets reach the right sheets? (use `--matrix`).
  4. **Hierarchy diff** — expected sheets == generated sheets.

Report **BUILD SUCCESSFUL (Verified)** only when GV-1/GV-2/GV-4 pass (no ERROR) **and**
the four human-assisted diffs pass. Otherwise report the specific mismatch.

### 10.5 KiCad project folder structure

All output files live in **one project folder**; `.kicad_pro` is only a config file — the
schematic data lives in the `.kicad_sch` files (one per sheet), not inside `.kicad_pro`.

```
Tracker/
├── Tracker.kicad_pro       # project config (references the sheets; no schematic data)
├── Tracker.kicad_sch       # ROOT / top sheet
├── Power.kicad_sch         # child sheets, one per @subcircuit call site
├── Controller.kicad_sch
├── GSM.kicad_sch
├── SIM.kicad_sch
├── Memory.kicad_sch
├── Interfaces.kicad_sch
└── Tracker.py              # SKiDL source
```

A single-sheet design is just `<name>.kicad_pro` + `<name>.kicad_sch` (+ `<name>.py`).

### 10.6 Dense flat-sheet render failure → decompose (ORG-1 is load-bearing)

**Observed, then fixed (STM32F103 board, 31 parts):** a flat design — correct netlist,
0 ERC errors — can still **fail to publish a schematic**. On one dense sheet the renderer
laid all 31 parts in a single auto-derived block; the all-label emitter then *geometrically
fused* `GND + +3.3V + SCL` into one net (`verify_connectivity` → `MISMATCH -- 1 unwanted
(short) + 3 missing net-group(s)`), and `smart_schematic` correctly **refused to publish**
(`RuntimeError`). Result: no `.kicad_sch` — an INCOMPLETE build (which is exactly what
GEN-3 / golden verification catches).

- **Root cause:** flat layout, no functional grouping → one dense blob → power-symbol /
  label geometric collision. This is why **ORG-1 (group every design >~12 parts) is a MUST,
  not cosmetic.**
- **Fix (verified):** decompose into functional `@subcircuit` blocks (power, MCU, clock,
  reset, EEPROM, I/O, test-points). The same circuit then routed **with wires** on the
  first seed and verified clean. No `.py` net/pin change — only structure.
- **MCP note:** `build(mode='body')` is flat; for a design above ~12 parts use
  `mode='script'` with `@subcircuit` blocks. A clean pre-check (ERC + netlist) does **not**
  guarantee the schematic will render — always confirm via the artifact + golden gates.

## 11. Summary

A conforming SKiDL design satisfies five principles:

1. **Electrical correctness** — connectivity, decoupling, pull-ups, defined inputs, real parts.
2. **Clear naming** — meaningful net names, standard designators, engineering values.
3. **Structured hierarchy** — functional `@subcircuit` blocks; shared signals passed as one `Net` object.
4. **Automatic rendering** — the author states intent; the engine chooses wires/labels/symbols/pins.
5. **Maintainability** — stable tags, complete metadata, test points, one stable project name.
