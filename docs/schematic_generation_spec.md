# SKiDL Schematic Generation Specification

**Professional design rules for LLM-based circuit generation**

- **Version:** 1.4
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

### 5.14 Component orientation & placement — `ORIENT`

How every symbol is turned and positioned. These are **drafting** rules (geometry),
not electrical ones — a design can be electrically correct and still unreadable if a
diode points the wrong way or a series chain is drawn as a loop. The engine owns the
geometry (author sets only net structure, per CONN-2); these rules define what a
*correct* result looks like so a human reviewer and the layout passes agree. Grounded
in IEEE 315 / ANSI Y32.2 and vendor datasheet typical-application drawings.

**Universal orientation (every part):**

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **ORIENT-1** | MUST | Signal flows **left → right**: a stage's input pins on the left, output pins on the right. Draw the whole sheet so the eye reads inputs (connectors/sources) at the left, processing in the middle, outputs at the right. | Human |
| **ORIENT-2** | MUST | Positive power pins point **up**, ground/negative pins point **down**. Power symbols sit above their part, ground symbols below. Never sideways. | Human / Static |
| **ORIENT-3** | MUST | A multi-pin part (IC/MCU/relay) is rotated/mirrored so its pins **face the parts they connect to** — the bundle edge points at its partner, not away (e.g. an MCU's USB pins face the USB connector; a relay's contacts face the load terminal). | Human (`orient_to_neighbors`) |
| **ORIENT-4** | MUST | Everything snaps to the **100 mil (2.54 mm) grid**; pins land on grid so wires are straight. No off-grid parts, no fractional-grid jogs. | Static (`grid_snap`) |
| **ORIENT-5** | SHOULD | Reference and value text reads horizontally (or bottom-up), **never upside-down**, and must **not overlap** the body, pins, pin numbers, or another part's text. | Human (`fix_text_orientation`, `fix_text_placement`) |

**Two-pin series parts (connect pins must face each other → straight wire, no loop):**

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **ORIENT-6** | MUST | A 2-pin part in a series chain is placed **in line** with its neighbours and flipped so the pin that connects to a neighbour is on **that neighbour's side** — a straight wire, never a U-turn ("loop") back around the body. | Human (`orient_2pin_face`) |
| **ORIENT-7** | SHOULD | A simple series chain (source → R → D → R → load) is laid out as **one horizontal row**, left to right, not a rectangle/loop. Detected dynamically from connectivity (one linear chain) — see `_is_linear_chain`. | Human (`flow_place_block` flat-chain) |
| **ORIENT-8** | SHOULD | In a horizontal row, 2-pin parts are drawn **horizontally**; in a vertical stack, **vertically** — the body aligned to the chain axis so pins exit straight into the rail. | Human |

**Per-component orientation:**

| ID | Level | Component | Rule | Verify |
| --- | --- | --- | --- | --- |
| **ORIENT-9**  | MUST | **Diode (`D`)** | Anode (pin 1, triangle flat side) toward the source / current-in; cathode (pin 2, bar) toward the load / current-out. In left-to-right flow the bar is on the **right**. A catch/flyback diode orients so its cathode sits on the switched node (e.g. buck `SW`), anode on GND. The triangle **points in the direction of conventional current**. | Netlist + Human (`ELE-6`, polarity-preserving flip) |
| **ORIENT-10** | MUST | **LED (`D`/`LED`)** | Same as a diode: `rail → R → anode`, `cathode → GND`; arrow/triangle points toward ground (current direction). The series resistor sits between the rail and the anode, in line. | Netlist (`ELE-6`) |
| **ORIENT-11** | MUST | **Polarized cap (`CP`, `C_Polarized`), tantalum, electrolytic** | `+` terminal toward the higher-potential rail (up / toward the positive supply), `−` toward ground. Never reversed. | Netlist / Human |
| **ORIENT-12** | SHOULD | **Resistor (`R`), non-polarized cap (`C`), inductor (`L`)** | Non-directional — orient for readability along the chain axis (ORIENT-8). A pull-up sits above its pin toward the rail; a pull-down below toward GND. | Human |
| **ORIENT-13** | MUST | **Decoupling / bypass cap** | Placed **immediately beside** the power pin it decouples (one 0.1 µF per digital power pin per vendor guidance), the shortest path pin→cap→GND; grouped with its IC, not scattered. | Human (`hug_power_satellites`, decap affinity) |
| **ORIENT-14** | SHOULD | **Crystal/oscillator (`Y`/`X`) + load caps** | Crystal placed **adjacent to** the MCU's OSC_IN/OSC_OUT pins; the two load caps flank it, their ground sides to GND. Kept as a tight cluster. | Human (`cluster`) |
| **ORIENT-15** | SHOULD | **Transistor/MOSFET (`Q`)** | Oriented by function: BJT/FET base/gate toward its driver (input, left), collector-drain/emitter-source along the current path (high side up, source/emitter toward GND for a low-side switch). | Human |
| **ORIENT-16** | MUST | **Connector (`J`/`P`)** | Placed on the **edge** of the sheet on the side its signals enter/leave (input connector left, output right, power top); pin 1 marked. The connector is not buried in the middle of the circuit. | Human |
| **ORIENT-17** | MUST | **IC / MCU / regulator (`U`)** | The functional anchor — placed centrally; inputs left, outputs right, V+ up, GND down; rotated per ORIENT-3 so each pin group faces its partner block. Regulator: VIN left, VOUT right, GND down, feedback divider on the output side. | Human (`orient_to_neighbors`, anchor placement) |

**Wiring geometry (so connections read cleanly):**

| ID | Level | Rule | Verify |
| --- | --- | --- | --- |
| **ORIENT-18** | MUST | Wires run only **horizontally or vertically** (Manhattan), route straight out of a pin before turning; no diagonals, no zig-zags, no purposeless offsets. | Static (`beautify_wires`) |
| **ORIENT-19** | MUST | A new wire **does not run over** an existing wire or through a part body / text. Connected crossings form a **Tee with a junction dot**; unrelated crossings have **no dot** and the engine re-routes a cross-net touch rather than fusing it. | Static / Human (cross-net short guard) |
| **ORIENT-20** | SHOULD | Give each part breathing room — do not squash courtyards together; equal, generous pin-exit stub lengths (≥150 mil) so labels/power symbols don't sit on the pin. | Human (`normalize_exits`) |

---

### 5.15 Reference layout patterns — `REFLAY`

Distilled from a set of published professional MCU-board schematics (ATmega32U2 USB
board, STM32F4 KiCad reference, PIC18 energy meter, EFM32 sensor node, ATxmega audio
board, STM32H7 robot controller): how real designers arrange exactly the things this
engine draws. These are the *visual idioms* reviewers expect; each row names the
enforcing pass or the authoring duty.

| ID | Level | Pattern | Who enforces |
| --- | --- | --- | --- |
| **REFLAY-1** | SHOULD | **MCU/main-IC is the page anchor**: one large symbol near the centre. LEFT side carries the *control* cluster — reset button + pull-up, BOOT strap, crystal + load caps, debug header nets; RIGHT side carries IO: GPIO banks, port headers, peripheral connectors. Signal pins exit straight to their partner. | anchor placement + `orient_to_neighbors` |
| **REFLAY-2** | SHOULD | **Decoupling caps draw as ONE BANK ROW**, not scattered: all the 0.1 µF caps for an IC line up in a single horizontal row on the supply rail (top of the section), each dropping to GND, bulk cap at the row's end — even though on the PCB they sit at the pins. One rail wire + evenly spaced caps reads instantly. | rail-draw (`draw_power_rail`) + decap grouping |
| **REFLAY-3** | MUST | **Crystal is a tight closed cluster**: crystal + its two load caps + their shared GND drawn as one small self-contained group beside the OSC pins — never spread out, never label-stubbed apart. Same for a reset RC (button + pull-up + debounce cap). | `cluster` / hug passes |
| **REFLAY-4** | MUST | **Series elements draw inline on the signal path**: USB D+/D− 22 Ω resistors, an LED's series resistor, a filter R — placed ON the wire run between source and destination, body aligned with the wire, no label break. | `orient_2pin_face`, flat-chain row |
| **REFLAY-5** | SHOULD | **Cross-section hops use SHORT LABEL STUBS, in-section connections use WIRES**: a pin that feeds a distant section gets a short stub + net label (XTAL1, LCD_RST, V_BIAS); everything inside one section is drawn copper. A label name states the FUNCTION (PWM_BACKLIGHT, REF_2V048), never N$7. | wire-vs-label gates + NET-naming rules §5.1 |
| **REFLAY-6** | SHOULD | **Medium one-sheet designs use TITLED SECTION BOXES** (dashed/graphic box per function: "MCU Circuit", "LCD Circuit", "Voltage Reference") with the box's parts fully wired inside — hierarchy sheets only when the page genuinely overflows (§ room-aware split). | block boxes + room-aware `big` gate |
| **REFLAY-7** | MUST | **Connectors/headers sit at the page edge** facing out (USB left/bottom, port headers right, debug header right), pin 1 marked; never buried mid-page. | ORIENT-16 |
| **REFLAY-8** | SHOULD | **Unused pins show explicit no-connects (X)**; spare gates/sections are parked in a corner. A reviewer must see every pin deliberately terminated. | floating-pin design gate + NC |
| **REFLAY-9** | MAY | **Port/GPIO banks fan out as ordered ribbons**: PD0..PD7 drawn as parallel, ordered, evenly-spaced stubs to a header or as a labelled bus — never crossing each other. | bus rendering / uncross pass |

---

### 5.16 Per-function schematic idioms — `FUNC`

How TI's own reference designs and datasheet *Typical Application* circuits draw each
recurring block. `REFLAY` (§5.15) gives the generic MCU-board arrangement; this table is
the **per-function drawing catalog** — for each block the author builds the net structure
so the engine renders it this way. Every row is grounded in a named TI document (see
*Sources* below). These are **specializations** of `ORIENT`/`REFLAY`/`ELE`; where a
per-function idiom and electrical correctness (§5.5 `ELE`) disagree, `ELE` wins.

**Power & supply blocks:**

| ID | Level | Block | Idiom (build the net structure so it draws this way) | TI source |
| --- | --- | --- | --- | --- |
| **FUNC-1** | MUST | **LDO regulator** | `IN → OUT` left-to-right; ≥1 µF ceramic `IN→GND`, 1–10 µF X5R/X7R `OUT→GND`; `EN` tied to `IN` (or driven), never floating; `GND` at bottom. Add a noise-reduction cap only if the datasheet requires one. | LP5907 DS |
| **FUNC-2** | MUST | **Buck / switching regulator** | `VIN` bypassed by a 10 µF X5R close to the pin; 0.01 µF `BOOT→PH/SW` bootstrap cap (rate ≥10 V); `PH/SW → inductor → output caps`; catch diode **cathode on the SW node, anode to GND**; set `VOUT` with an `R1/R2` divider from `VOUT` to `FB` equal to the internal reference (e.g. 1.221 V on TPS5430), tapped **after** the output bulk cap; keep the switch-node net electrically tight. | TPS5430 DS; SPRACU5 PMIC checklist |
| **FUNC-3** | SHOULD | **Board power input** | Draw bulk (tantalum/aluminium, LF ripple) **and** a ceramic (HF) as a pair at the entry point. | SLOA089 §17.5.4 |
| **FUNC-4** | SHOULD | **Ferrite-filtered analog / PLL / USB / OSC supply** | π-filter: ferrite bead on **+V only** (never in the ground return), a 0.1 µF/0.01 µF cap inside the bead + a 1000 pF cap outside; the chip-side cap sits closest to the supply pin. **Do not** ferrite-filter digital/core-logic rails (blocks the logic's dI/dt). | SZZA009 §2.2.4; SPRACU5 §6.1; SPRABV2 §5.3 |
| **FUNC-5** | MUST | **Decoupling** | One HF ceramic (0.1 µF X7R) **per power pin** + one bulk cap per rail group; smallest value nearest the pin; drawn next to the pin it serves; rate every cap > 2× worst-case voltage. | SPRACU5 §6.1.2.2; SLOA089 §17.5.3 |

**MCU & digital blocks:**

| ID | Level | Block | Idiom | TI source |
| --- | --- | --- | --- | --- |
| **FUNC-6** | SHOULD | **MCU core** | Decouple every VDD/VDDIO pin, bulk cap per rail group; MCU is the central anchor (control cluster left, IO right); VDDA/analog supply filtered separately (FUNC-4). | SPRACU5 §5–6; SLOA089 |
| **FUNC-7** | MUST | **Unused / high-Z IO** | Pull every floating or undriven IO to a defined level with 10 k–47 k — a floating CMOS input is an antenna / EOS risk. Never add a pull that **contends** with an attached device's internal pull. | SPRACU5 §5.2.1.2 |
| **FUNC-8** | MUST | **Boot-mode / config straps** | Pull **every** boot/config pin (10 k/47 k), populating a pull-up **or** pull-down on each — including reserved strap pins. **Never tie a boot pin directly to GND or VIO** (the pins have alternate output functions); a DIP-switch config uses a 1 k pull-up / 47 k pull-down divider. | SPRACU5 §6.1.5 |
| **FUNC-9** | SHOULD | **Reset circuit** | Hold reset/boot inputs in a known state until latched; pull-down on any reset-status output, drawn near the pin; if a cap sits on the reset line use > 22 pF. | SPRACU5 §6.1.4 |
| **FUNC-10** | MUST | **Crystal / oscillator** | Crystal + its two load caps as one **tight closed cluster** at XI/XO (OSC_IN/OSC_OUT); load-cap value from the datasheet *Crystal Requirements* table (it includes PCB capacitance); cap ground sides to GND. If driven by an external LVCMOS clock, ground XO per the datasheet. (= REFLAY-3) | SPRACU5 §6.1.3 |

**Comms / interface blocks:**

| ID | Level | Block | Idiom | TI source |
| --- | --- | --- | --- | --- |
| **FUNC-11** | MUST | **I²C bus** | SDA/SCL are open-drain → **external pull-ups are mandatory** (4.7 k–10 k, value per SLVA704) to the bus rail; address-select jumper + pull. | SPRACU5 §5.2.1.4; SLVA704 |
| **FUNC-12** | SHOULD | **IO series-R provisioning** | A 0 Ω default series R on bus IO (for trace tuning); 22 Ω inline on USB D+/D− and on retimed/fast clock outputs, placed near the driver pin; optional 8–10 pF DNP cap at a clock receiver. | SPRACU5 §5.2.1 |
| **FUNC-13** | SHOULD | **USB front-end** | D+/D− drawn as a differential pair annotated **90 Ω**, ESD array at the connector, optional common-mode choke bypassable with 0 Ω (DNP). | SPRACU5 §5.4.8/§9 |

**Analog blocks:**

| ID | Level | Block | Idiom | TI source |
| --- | --- | --- | --- | --- |
| **FUNC-14** | MUST | **Op-amp stage** | Decouple every op-amp **package** (one per package, 1/2/4 devices); keep the inverting-input node minimal; **terminate every unused section** (noninverting → mid-supply/ref, output → inverting = unity follower). | SLOA089 §17.5.3/§17.7.3 |
| **FUNC-15** | MUST | **Current-sense amp (INA)** | **Kelvin (4-wire)** connection to the shunt — sense pins as a tight pair as close to the shunt as possible (required below ~0.5 mΩ); pick a fixed-gain variant so full-scale shunt drop maps into range; ~1 nF decoupling at the sense/supply pins; output = a labelled net (`CURRENT`). | INA180/INA240 DS; SBOA197; SLVA959B §7.8 |
| **FUNC-16** | SHOULD | **Differential / SAR-ADC input** | Drive with a wide-GBW buffer + an RC **charge-bucket** filter at the ADC pins (C0G/NP0 filter cap); the reference decoupling cap sits **directly** at REF/GND with no via between REF and its cap. | SBAA286A |
| **FUNC-17** | SHOULD | **Precision reference** | Shunt/series reference (e.g. TL431) with its set resistors + output cap, placed at the AREF/analog-reference pin. | general (TL431 / TI REF series) |

**Protection / connectors / RF:**

| ID | Level | Block | Idiom | TI source |
| --- | --- | --- | --- | --- |
| **FUNC-18** | SHOULD | **ESD on external connectors** | A TVS/ESD array on **every** IO wired to an external connector (internal ESD cells are handling-rated only); diode between the line and GND, right at the connector. | SPRACU5 §5.2.1.5 |
| **FUNC-19** | MUST | **RSVD / reserved pins** | Leave unconnected — **no trace and no test point**. | SPRACU5 |
| **FUNC-20** | SHOULD | **RF matching network** | A π-network immediately before the antenna, two of the three positions populated (unused = 0 Ω), each shunt cap to GND; balun/LC short; the LC→antenna run is a controlled 50 Ω; decouple each RF supply pin at its pin. | SWRA640 |

**Indicators / discretes / review:**

| ID | Level | Block | Idiom | TI source |
| --- | --- | --- | --- | --- |
| **FUNC-21** | MUST | **LED indicator** | `rail/GPIO → series R → anode`, `cathode → GND`; size R for If, tuned per colour (blue/white ~60 Ω, red/green/yellow ~270 Ω); never `rail → LED → GND` without R. For continuous/high-current LEDs, switch with an external FET driven by the GPIO — **not** the GPIO directly. (= ELE-6) | SPRACU5 §5.4.9 |
| **FUNC-22** | MUST | **Polarized / electrolytic cap** | `+` terminal to the more-positive DC node; reversed polarity destroys the part. | SLOA089 §17.4.2 |
| **FUNC-23** | SHOULD | **MOSFET half-bridge / power stage** | `+V` (drain) top, phase node (`MA/MB/MC`) middle, source/`GND` bottom; gate driven via a series R (~20 Ω) + a labelled net (`HIA/LOA`); high-side above, low-side below; repeated bridges column-aligned. | ref boards; SLVA959B power-stage |
| **FUNC-24** | SHOULD | **Test points** | On each supply rail, `RESET`/`NRST`, the main clock, and each primary bus (UART/I²C/SPI/CAN); a Kelvin-sense TP across a shunt/0 Ω element; name the TP after its net (`TP_UART_TX`). | SPRACU5 §6.1.1/§6.1.4 |
| **FUNC-25** | SHOULD | **DNP / DNI marking** | Mark do-not-populate parts explicitly (value `NP`/`DNI`): 0 Ω CM-choke bypass, provisioned caps, reset-path isolation 0 Ω. | SPRACU5 §6 |

**Sources (TI literature numbers):** SLOA089 *Circuit Board Layout Techniques* (Op Amps
for Everyone Ch.17); SPRACU5 *Schematic Design Guidelines & Review Checklist*; SZZA009
*PCB Design Guidelines for Reduced EMI*; SPRABV2 *BGA PCB Design*; SWRA640 *CC13xx/CC26xx
HW Config & PCB Design*; SBAA286A *Driving a High-Voltage SAR ADC*; SBOA197 (INA240 layout);
SLVA704 *I²C pull-up resistor calc*; and datasheets LP5907, TPS5430, INA180/INA240,
SLVA959B. These are the same sources the PCB-placement methodology cites, so schematic
intent and board layout stay consistent.

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
- [ ] **ORIENT/REFLAY** Every part turned per §5.14; blocks arranged per §5.15.
- [ ] **FUNC-1…25** Each recurring block drawn to its TI per-function idiom (§5.16).
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

## 11. Senior-designer methodology & review gates

The rule tables above are the *common case*. A **senior** designer's value is the **judgment
and process** layered on top — front-loading the thinking so the drawing is almost mechanical,
and never trusting an unreviewed, unverified result. Apply this whole-design discipline to
**every** generated design, scaled to its size. Grounded in Rick Hartley (stackup/ground/return
first), Henry Ott (EMC by design), TI SPRACU5 (per-block self-review), IPC-2221 / IPC-A-610 /
IPC-6012 (class drives decisions), Charles Pfeil (risk-first placement), and NASA
EEE-INST-002 / WCCA (derating, worst-case).

**MET-1 — Requirements & constraints FIRST (MUST).** Before placing a part, state the design's
operating box: input-voltage range incl. transients, max load/rail currents, worst-case
dissipation + ambient/airflow, mechanical envelope + fixed connectors, **compliance/reliability
class (IPC Class 1/2/3)**, cost/volume, and the test strategy. Class is a design *input*, not a
label — Class 3 changes what is acceptable for the *same* artifact. Everything downstream
derives from these numbers; a senior writes them down, a junior assumes them.

**MET-2 — Architecture & risk-first (MUST).** Draw the **block diagram, power tree, clock tree,
and ground/return strategy** before capture. Decide the **stackup and reference-plane strategy
before layout** (Hartley: the stackup contains the fields, so it precedes routing; keep every
signal/power layer one dielectric from a reference plane; default to a single continuous ground
and partition by *placement*, not by cutting the plane). **Identify the hardest/riskiest element
first** (largest BGA, fastest edges, most sensitive analog, tightest thermal) and let it drive
placement, stackup, and layer count.

**MET-3 — Part selection with lifecycle + errata (MUST / SHOULD).** Check lifecycle (avoid
NRND/EOL silicon); qualify a second source for every critical active part *while drawing*;
reuse proven building blocks over novel ones for non-differentiating circuitry. Read
**datasheet + app-note + errata together**, honor Recommended Operating Conditions, and treat
the typical-application circuit as a **starting point to re-derive for YOUR worst case** — never
as gospel.

**MET-4 — Concurrent DFx (SHOULD).** Think DFM / DFA / DFT / design-for-debug from day one, not
bolted on: DRC set to the chosen IPC class and the *fab's real* limits; pin-1/polarity/rotation
unambiguous across footprint + silk + assembly drawing + centroid; fiducials & panelization
planned early; **test points on every testable net + every power rail, clock, reset, and
programming line**; break the design into isolatable sub-circuits so it can be brought up. (Full
layout-side rules: the `pcb-place-route-fix` skill.)

**MET-5 — Order of work (MUST).** Set length/impedance/matched-pair constraints **before**
routing; place mechanically-fixed and highest-criticality/RF parts first and nearest their
connectors; route and **lock** the critical/high-speed nets first — the rest of the board serves
them. Pour/fill is the *last* stage, never where the design is decided.

**MET-6 — Review gates — never self-approve blind (MUST).** Gate the work:

| Gate | When | What it checks |
| --- | --- | --- |
| **Requirements/architecture** | before capture | block diagram ↔ requirements, power/thermal budget documented, class fixed |
| **Schematic review** | before any layout | per-block self-review checklist (TI SPRACU5 model); decoupling-per-rail, straps/resets tied, net-name consistency, interface compatibility, power sequencing |
| **Placement review** | before routing | zones, critical-part placement, courtyards, edge/thermal/DFM (acceptance checklist in the PCB skill) |
| **Pre-release** | before fab | ERC/DRC clean **or waived with a documented reason**, DFM vs class, BOM lifecycle |

A **formal schematic review before layout** catches the majority of hardware bugs and roughly
halves respins. Always put a reviewer who did **not** draw it in the loop. **No silent
violations** — clear every ERC/DRC flag or record why it is waived.

**MET-7 — Margin, worst-case, derating (MUST hi-rel / SHOULD commercial).** Apply a written
derating policy (NASA EEE-INST-002 reference: caps ≤60 % rated V, resistors ≤60 % P,
semiconductors ≤50 % P / ≤75 % V with Tj margin; commercial default ~80 % V/I, 70 % P, 25–50 °C
Tj margin). Do a **worst-case analysis** on critical circuits (every part at its worst drift
over tolerance + temperature + aging) and keep an **FMEA mindset** — "what breaks, and what
happens when it does."

**MET-8 — Documentation & design intent (MUST).** Capture the **WHY** next to the block, not
just the what — power-budget assumptions, interface agreements, the rationale behind every
non-obvious value — so a reviewer (and future-you) can check intent. Keep schematic/layout/BOM
under revision control; the fab/assembly drawing (stackup, impedance, class, finish, acceptance
basis) is a first-class deliverable. (Engine-level: this maps to `META-*` — title block, rev,
stable tags, one stable project name.)

**MET-9 — Iterate with measurement; be honest about what is verified (MUST).** Write a bring-up
plan before the board exists (expected first-power current, each rail's value, enable order);
power up current-limited, one rail at a time, from the input inward. **Record what was actually
measured on hardware vs what is still theoretical** — verify, don't infer. (This is the same
discipline as `GEN-3` / the golden gate: nothing is "done" until the artifact exists and is
checked.)

**MET-10 — Rules are defaults; know when to break one — and document why (SHOULD / MUST).**
Every guideline here (a derating %, an IPC spacing, a stackup default, a wire-vs-label call) is
the common case, overridable by engineering judgment. When you deviate, **record the deviation,
the rationale, and the review sign-off** — a waived DRC flag, a part run above standard
derating, a deliberately split plane each needs a documented reason, never a silent override.
A deviation that changes class/compliance margin escalates to the review gate, not the
individual. (This is exactly the spec's "SHOULD may be overridden only with a documented,
deliberate reason" — §2, applied as a habit.)

> **Junior → senior tell:** a junior jumps to drawing/routing; a senior front-loads
> requirements, stackup, ground, risk, and placement constraints so the rest is mechanical,
> thinks in **return-current paths and fields** (not just copper), and **closes loops**
> (measurement → next rev, errata → BOM, acceptance data → design).

## 12. Summary

A conforming SKiDL design satisfies six principles:

1. **Electrical correctness** — connectivity, decoupling, pull-ups, defined inputs, real parts.
2. **Clear naming** — meaningful net names, standard designators, engineering values.
3. **Structured hierarchy** — functional `@subcircuit` blocks; shared signals passed as one `Net` object.
4. **Automatic rendering** — the author states intent; the engine chooses wires/labels/symbols/pins.
5. **Maintainability** — stable tags, complete metadata, test points, one stable project name.
6. **Senior discipline (§11)** — requirements & stackup/ground first, risk-first order, review gates
   before each stage, worst-case/derating margin, documented design intent, verify-don't-infer, and
   every rule-break recorded with its reason.
