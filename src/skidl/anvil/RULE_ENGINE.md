# Dynamic Schematic Rule Engine — design + which-rule-for-what + status

**Core principle: drive layout from the CONNECTIVITY GRAPH, never from a part
name.** No rule below says "if ATmega32U2 / if USB". Every classification is a
*generic* inference (ref-prefix class, pin function, net-name role keyword, graph
degree/fanout), so the SAME engine draws an MCU board, a power supply, an analog
front-end, an FPGA, or a motor driver.

Status legend:  ✅ implemented · 🟡 partial · ⬜ planned. Code column names the
module (`skidl.anvil.*` = engine helpers; `skidl.schematics.*` / `skidl.tools.*`
= SKiDL core).

---

## The pipeline (10 phases)

### Phase 1 — Detect functional blocks  🟡
**Dynamic method:** classify each part by *role*, not name:
`ref_prefix` class (U/Q→active, R/C/L→passive, J/P→connector, Y/X→clock, D→diode…)
+ pin-function tags (PWRIN/PWROUT/…) + net-name role regex
(`+\d+V|GND|VCC…`=power, `CAN|I2C|SPI|UART|USB`=comms, `ADC|SENSE`=analog) + graph
degree (highest-pin-count part in a group = the "anchor" IC).
Roles: Power / Processing / Clock / Comms / Analog / Output / Connector / Other.
- **Code:** `skidl.schematics.cluster.classify_block_role`, `net_classify.classify_net_role`, `is_connector_part`
- **Done:** role classification + connector detection (metadata + prefix). **Pending:** finer sub-types (buck vs LDO vs battery) — currently all "Power".

### Phase 2 — Connectivity strength (edge weight)  🟡
**Dynamic method:** score every part-pair by shared nets, boosted for tight
functional links (anchor↔crystal, IC↔decap on a power pin), so high-score pairs
cluster. Conceptually `w = k1·direct_nets + k2·shared_power + k3·functional_bonus − k4·distance`.
- **Code:** `cluster.compute_net_affinity_weights` (cluster_boost, decap_boost) → fed to `place.net_force_dist` as a per-net multiplier.
- **Done:** cluster + decap affinity boosts. **Pending:** the full 4-term pair score (currently net-level boost, not pairwise matrix).

### Phase 3 — Block ordering (left→right, power→ground)  🟡
**Dynamic method:** order top-level blocks by role
**Power → Processing → Comms → Sensor → Connector → Other** left-to-right;
within a sheet, power nets bias to the top, ground to the bottom, connectors to
edges; X follows BFS signal-flow depth from connectors/inputs.
- **Code:** `place.layout_blocks_by_role`, `place.directional_seed_placement`, `net_classify.part_depth_map`
- **Done:** block role-ordering + directional seed. **Pending:** whitespace balancing between blocks.

### Phase 4 — Affinity rules (keep clusters intact)  🟡
**Dynamic method:** a satellite (crystal / decap / reset R+button / regulator I/O
caps — inferred as low-pin passives on a low-fanout net with an anchor) must stay
in the anchor's cluster and must NOT be split to a label. A net whose pins are ALL
in one detected cluster is **never stubbed on distance** — it renders as a WIRE.
Large connectors (headers/receptacles, >4 pins) are kept OUT of the cluster so
their wide breakout bus (port pins → header) stays a labelled bus, not a fan of
parallel wires; a 2-pin crystal (ref-prefix Q or Y) is pulled IN.
- **Code:** `cluster.detect_clusters` (crystal-in / big-connector-out boundary), `cluster.compute_net_affinity_weights`, `tools/kicadN/gen_schematic._classify_and_stub_complex_nets` (single-cluster wire protection), `place._auto_stub_large_groups` (same guard), `cluster.find_decap_affinities`
- **Done:** cluster nets stay wires (crystal→XTAL, UCAP, USB-R, LED all wired to the MCU on the atmega board — 7/8 functional nets); decap↔VDD affinity; big-connector buses stay labels. **Pending:** guaranteed tight *placement* of every satellite next to its pin (reset R/button still place far → RESET falls back to a label; needs constraint-aware placement, Phase 85).

### Phase 5 — Wire rules  🟡
| Rule | Status | Where |
|---|---|---|
| Manhattan only (90°) | ✅ | router + `beautify_wires` (diagonal→L) |
| Merge collinear segments | ✅ | `beautify_wires` |
| On the connection grid (IPC-3, 1.27mm) | ✅ | `grid_snap` (connectivity-gated) |
| Straight power-symbol stub (no dog-leg) | ✅ | `sexp_schematic._power_symbol_to_sexp` |
| Pin exit **direction** away from body | ✅ | `beautify_wires._flip_l_exits` |
| Pin exit **length** ≥ 150 mil, first bend after it | ⬜ | needs router (moves endpoints) |
| **Equal** exit length same side | ⬜ | needs router |
| Parallel nets stay parallel/equal | ⬜ | needs router (adjacent tracks) |
| No immediate bend / no tiny zig-zag | 🟡 | flip helps; full case = router |

### Phase 6 — Wire clearance  ⬜
Wire↔symbol ≥100 mil, wire↔label ≥50 mil, wire↔wire ≥100 mil. **Router-level**
(track spacing during routing) — a post-pass can't add clearance without re-routing.

### Phase 7 — Label rules  ✅
**Dynamic method:** decide wire-vs-label per net by a score, not by name:
`score = w_d·dist + w_f·fanout + w_x·crossings − w_c·same_cluster`; local/short →
wire, long/cross-page → label, recognized power token → power symbol, `Bus(...)` →
bus. Redundant/dangling labels stripped.
- **Code:** `tools/kicadN/gen_schematic._classify_and_stub_complex_nets`, `sexp_schematic` (power symbol / bus / label), `strip_dangling_labels`

### Phase 8 — Crossing cost  🟡
**Dynamic method:** count different-net segment crossings (a metric), and reduce
by trying part orientations. Full version: crossing +100, overlap +200, wire-thru-symbol +500,
optimizer minimizes total.
- **Code:** `route.Router.count_wire_crossings`, `place.reduce_crossings_by_orientation` (opt-in), `geometry.Segment.intersects`
- **Done:** metric + opt-in orientation reduce. **Pending:** it as a live cost term the placer minimizes.

### Phase 9 — Readability score  🟡
`score = w1·crossings + w2·wire_len + w3·labels + w4·(1−whitespace) + w5·density (+ overlaps + unequal-exits)`.
- **Code:** `skidl.schematics.metrics.readability_score` (wire_length, label_count, density, whitespace, crossings)
- **Done:** the metric exists. **Pending:** using it to ACCEPT/REJECT a regeneration (drive the optimizer) — today it's diagnostic only.

### Phase 10 — Auto beautification (final pass)  🟡
| Step | Status |
|---|---|
| Merge collinear · square diagonals · flip pin-exit direction | ✅ `beautify_wires` |
| Strip dangling labels | ✅ `strip_dangling_labels` |
| Snap connections to grid (kill off-grid, IPC-3) | ✅ `grid_snap` (surgical: wire pts + instance/junction/label `(at)` only — never symbol-internal graphics; connectivity-gated) |
| Equalize exit lengths · align first bends · identical parallels | ⬜ router |
| Align symbols · center clusters · balance whitespace | ⬜ |

---

## Priority-weighted rules (the trade-off ladder)
The optimizer may sacrifice a low rule to satisfy a high one. Never violate ≥95.

| Prio | Rule | Status | Enforced by |
|----:|------|:--:|---|
| 100 | **Electrical correctness** (never violate) | ✅ | connectivity gate (`verify_connectivity`) + revert-guard + ERC |
| 95 | Avoid symbol overlap | ✅ | `place.overlap_force` (repulsion) |
| 90 | Avoid wire crossings | 🟡 | crossing metric + opt-in reduce |
| 85 | Keep functional clusters together | 🟡 | affinity weights + anchor-net protection |
| 80 | Shorten important nets (clock/decap/USB-diff) | 🟡 | cluster/decap boost |
| 75 | Uniform pin-exit lengths | ⬜ | (router) |
| 70 | Align first bends | ⬜ | (router) |
| 65 | Maintain parallel routing | ⬜ | (router) |
| 60 | Reduce unnecessary labels | ✅ | wire-vs-label score + dangling strip |
| 55 | Balance whitespace | 🟡 | block ordering (no explicit balance yet) |
| 50 | Cosmetic alignment | ⬜ | (beautify pass) |

---

## What "dynamic" means here (why no part names)
- **Roles** come from `ref_prefix` + pin-function + net-name-role regex + graph degree — all generic.
- **Clusters** come from low-fanout graph adjacency to an anchor IC — topology, not identity.
- **Wire/label** and **crossing/readability** are numeric scores with tunable weights, not per-part rules.
- **Parameters** (exit 150mil, spacing 100mil, `D_wire`, `R_anchor`) scale from the tool grid / pin pitch / sheet size — see `SCHEMATIC_ENGINE_RULES.md` PART 4.
→ The same pipeline handles MCU / power / analog / FPGA / motor-driver boards unchanged.

---

## IPC compliance gate (every build)
`smart_schematic.build()` ends with `ipc_check.report(...)` — a read-only pass that
scores the generated sheet against the *enforceable* IPC-2612 / IPC-2611 rules and
prints an OK/!! line per rule: **Manhattan 90°, no dangling labels, on connection
grid (IPC-3), junction dots, power-rail symbols, title block (docs)**. Off-grid +
dangling counts come from KiCad ERC (`endpoint_off_grid`, `label_dangling`); the
rest from geometry. Pure visibility — never edits the sheet. (`skidl.anvil.ipc_check`.)

## Honest state (one line)
**Correctness (100) + overlap (95) + labels (60) + Manhattan/grid/no-dangling
(IPC-2612/IPC-3) are enforced and dynamic today; clusters/crossings/short-nets
(80-90) are partial; the router-only cosmetic tier (uniform exits / parallel /
clearance, prio 65-75) is the remaining build — it needs constraint-aware routing
in `route.py`, not a post-pass.** Next build target, in priority order: **90
crossing-as-live-cost → 85 cluster placement → 75/70/65 router exit+parallel →
55/50 whitespace+align.**
