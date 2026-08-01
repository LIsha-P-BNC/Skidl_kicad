# Wire / Label / Symbol / Bus — the dynamic decision rules

How the Anvil schematic engine decides, for every net, whether to draw a **wire**, a
**net label**, a **power symbol**, or a **bus** — and how it normalizes wire geometry.
The goal is a schematic that reads like a hand-drawn professional design while staying
**electrically identical** to the netlist (a label and a wire are the same node; the
`smart_schematic` connectivity gate re-verifies every seed, so a cosmetic call can never
short). "Dynamic" = every rule is driven by the connectivity graph + generic net/part
inference, never by a part name, so the same engine handles an MCU, a power supply, an
analog front-end, etc.

---

## The rendering hierarchy (highest priority first)

| Rank | Render as | When |
|---|---|---|
| 1 | **Power symbol** | net is a power/ground rail (`+5V`, `+3V3`, `VCC`, `GND`, `AVCC`, …) |
| 2 | **Bus + bus entries** | net is a member of a SKiDL `Bus(...)` with **≥ 3** members |
| 3 | **Wire** | net's *function* is clock / reset / decoupling / USB-diff, OR its pins are all in one functional cluster, OR it's local + uncluttered |
| 4 | **Net label** | net is far / high-fanout / would cross or crowd other wires |

Power is decided *pre-placement* (`auto_stub_nets` → power symbols). Buses are a
presentational overlay (`detect_bus_group`, `Bus` objects only — never inferred from
similar net names). Everything else is decided by the gate flow below.

---

## The wire-vs-label decision — independent gates (NOT a score)

`tools/kicad{6,7,8,9}/gen_schematic._classify_and_stub_complex_nets` runs **after
placement, before routing**. Power/ground nets are already symbols and never reach it.
Each remaining net is decided by short-circuit gates — **first match wins**:

```
1. < 2 pins in this sheet        -> skip (leave as-is)
2. FUNCTION -> WIRE              classify_net_function(net) in {clock, reset,
                                 decoupling, usb_diff}   (net_classify.py)
3. AFFINITY -> WIRE             all pins in ONE detected functional cluster AND
                                 fanout <= auto_stub_cluster_wire_max_pins
4. CROSSINGS -> LABEL           span intersects >= auto_stub_crossing_label_threshold
                                 other nets' spans
5. CONGESTION -> LABEL          >= auto_stub_congestion_label_threshold other-part pins
                                 within auto_stub_congestion_radius of a pin
6. DISTANCE -> LABEL            CLOSEST pin-to-pin distance >= auto_stub_far_label_dist
                                 (closest pins, NOT symbol-center, NOT the span)
7. FANOUT -> LABEL             fanout > auto_stub_max_wire_pins
8. otherwise -> WIRE
```

**Function first, then clutter-avoidance** — the way an experienced designer decides:
keep the critical local circuitry (crystal, reset, decoupling, USB pair) as direct wires
no matter the distance; only fall back to a label when a wire would genuinely tangle or
crowd the drawing. **Distance is one input of several, and it is measured pin-to-pin
(closest pair)** so an IC whose XTAL pins sit beside the crystal still counts as adjacent
even if the two symbol bodies' centers are ~1200 mil apart.

### Net-function classification (`net_classify.classify_net_function`)
`power` (rail/ground) · `clock` (a crystal/oscillator pin, or name `XTAL`/`OSC`) ·
`reset` (name `RESET`/`NRST`/`MCLR`/`RST`) · `usb_diff` (`USB…D±`, `D+`/`D-`, `DP`/`DM`) ·
`decoupling` (a `100n`-class cap on an IC power pin) · else `None`. The first four force
a WIRE.

### Cluster boundary (`cluster.detect_clusters`)
An anchor IC pulls in its low-fanout satellites (crystal + load caps, UCAP, reset R+button,
USB series R, status LED) within 1–2 hops. A **2-pin crystal (ref-prefix Q/Y) is pulled
IN**; a **large connector (header/USB, > 4 pins) is kept OUT** so its wide breakout bus
stays labelled, not a fan of parallel wires.

### Tunable parameters (pass to `generate_schematic` / `smart_schematic.build`)
| Option | Default | Gate |
|---|---|---|
| `auto_stub_max_wire_pins` | 3 | fanout (7) + cluster cap base |
| `auto_stub_far_label_dist` | 1500 mil | distance (6), closest-pin |
| `auto_stub_crossing_label_threshold` | 4 | crossings (5) |
| `auto_stub_congestion_label_threshold` | 16 | congestion (5) |
| `auto_stub_congestion_radius` | 300 mil | congestion (5) |
| `auto_stub_cluster_wire_max_pins` | `max_wire_pins*2` = 6 | cluster (3) |

---

## Wire geometry normalization (post-route, connectivity-guarded)

Runs inside `smart_schematic`'s per-step revert-guard (backup → apply → re-verify →
roll back the step alone if connectivity changed → **can never short**). Order:
`strip-dangling → pin-exit normalize → wire beautify → grid-snap`.

| Step | Module | What |
|---|---|---|
| Manhattan 90° (diagonal → L) | `beautify_wires` | ✅ |
| Merge collinear · drop tiny segments | `beautify_wires` | ✅ |
| Pin exits leave AWAY from the body | `beautify_wires._flip_l_exits` | ✅ |
| Min pin-exit length (slide chain + movable label) | `normalize_exits` | ✅ *safe subset* |
| On the 1.27 mm connection grid (IPC-3) | `grid_snap` | ✅ |
| Straight power-symbol stub (no dog-leg) | `sexp_schematic` | ✅ |

**Router-tier (a post-pass cannot do these safely — they need a router-planned jog / new
track, which would risk overlaps or a short):**
- Equal pin-exit length for *pin-to-pin* wires, and a common bend line across a symbol side.
- Parallel-track spacing for a differential pair (USB D+/D-).
- Wire-to-symbol / wire-to-text clearance.
`normalize_exits` handles the tractable case (an exit whose far end is a movable label or
power symbol); a pin-to-pin exit is left to the router. These belong in `route.py` as
constraint-aware routing — the honest boundary of the post-process.

---

## What stays a label / symbol on purpose
- **Power/ground** → power symbols (never labels).
- **Wide port / connector buses** (PB0-7 → header, address bus) → labels; a real `Bus(...)`
  of ≥ 3 members → a bus spine. Wiring 8–16 parallel nets to a connector is *less* readable.
- **Genuinely far or crowded** signal nets → labels (gates 4–7), so the drawing doesn't
  tangle.

## 2026-07-24 robustness additions (smart_schematic post-steps)

- **Junction independence (verified):** the router splits wires at every T-point, so
  connectivity NEVER depends on `(junction ...)` dot elements — deleting all of them
  still gives 0 KiCad ERC errors. Dots are kept for IPC-2612 readability only; a viewer
  that mishandles them cannot break the netlist.
- **`remove_label_taps`:** a 3-way junction whose one branch dead-ends at a single label
  is replaced by the label sitting directly ON the main wire (labels connect by position;
  the tap wire + its dot were noise).
- **`add_pwr_flags`:** every rail with a power-in pin and no power-out driver gets a
  PWR_FLAG pin-coincident with its power symbol — kills the standard 2
  `power_pin_not_driven` ERC errors on every build, never double-drives a regulated rail.
- **`fix_text_orientation`:** property text at 180°/270° normalized to 0°/90° (IEEE 315:
  fields read horizontally or bottom-up, never upside-down).
- **`SKIDL_WIRE_MAX_FANOUT`** (env, default 0=off): optional hard cutoff forcing every
  net with more than N pins to labels regardless of geometry. The default trusts the
  gate flow above — nearby clusters stay wires, only far/crowded nets become labels
  ("label on every part" is explicitly NOT the goal).
- **Verifier:** refs starting with `#` (power symbols, PWR_FLAGs) are ignored in the
  partition compare — kicad-cli never exports them, so counting them made every power
  net a false "short + broken wire" pair (the bug that used to reject every wired seed
  whenever a circuit script manually instantiated `power:*` parts).
- **Atomic publish:** the whole build runs in `.build_stage/` and finished artifacts are
  moved into the project in one pass — an open viewer can never load a half-built sheet.

## Honest state (one line)
The **decision** engine (function-first, cluster-aware, congestion/crossing/distance/fanout
gates) is implemented and dynamic; **power/bus/grid/Manhattan/no-dangling** are enforced;
the remaining gap for a truly hand-drawn look is **placement quality** (even spacing, no
overlap, satellites parked exactly at their pins) + the router-tier geometry above — that
is a placement/routing change (`place.py`/`route.py`), not a label-decision change.
See `RULE_ENGINE.md` for the full 10-phase engine map.
