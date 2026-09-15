# Schematic-engine regression circuits

Small, fast circuits that exercise each distinct classifier/placement path of
`smart_schematic.build()`. Run any of them directly (each sets its own
`sys.path` to the repo `src/` and builds into its own folder):

```
set PYTHONHASHSEED=0
python t1_tiny_flat.py
```

PASS criterion per test: `routed with wires ... connectivity OK` (or the noted
expected outcome) + `ready (published atomically)` + ERC 0 errors.

| Test | Path exercised | Expected (2026-09-09 baseline) |
|---|---|---|
| `t1_tiny_flat.py` | FLAT ungrouped (no blocks/@subcircuit) — blk_ids collapse to one id; gates 3/3b/3c must not change behavior | wires, seed=0, verified |
| `t2_autogroup.py` | FLAT medium, auto-grouping path; mixed grouped/ungrouped parts (power-only regulators yield no anchor clusters) | wires, seed=0, verified |
| `t3_subcircuit.py` | `@subcircuit` pages (<50 parts → flattened to one sheet), shared `Net` objects, cross-page net (`LOAD_EN`), plain-helper repetition | **all-label (pre-existing engine weakness — NOT a regression)**: verified A/B 2026-09-09 with `SKIDL_CROSS_BLOCK_LABEL=0` (old classifier) → same all-label result. Tracks the known "cross-sheet routing/labelling of block() groups inside @subcircuit pages is fragile" issue. If this ever wires cleanly, update this row. |

| `t4_motor_driver.py` | 12 V domain, MOSFET low-side driver + flyback; LED value COMPUTED by `anvil.design_calc` at build time (netlist R3 must equal the calculator's E24 answer, "1k") | wires, seed=0, verified, placement=anchor. Also the width/voltage-engine genericity check: MOTOR_A → 2 A, MOTOR_EN → 0.1 A (control-suffix exclusion), +12 V rows. |

The full-size grouped benchmark is `F:/Anvil/stm32_usb_devboard/stm32_usb_devboard.py`
(3 authored blocks, 29 parts): expected `routed with wires (seed=0)` via
`placement = legacy (anchor wouldn't wire)` with cross-block nets as labels.

Related kill switches for A/B debugging:
- `SKIDL_ANCHOR_BLOCKS=0` — disable M11 group-aware anchor placement
- `SKIDL_CROSS_BLOCK_LABEL=0` — restore pre-2026-09-09 classifier (no gate 3 scoping, no 3c)
- `SKIDL_SWEEP_DEBUG=1` — per-seed generation exceptions + verify mismatches + keeps
  each failed sheet as `<name>.debug_fail_seed<N>.anvil_sch`
