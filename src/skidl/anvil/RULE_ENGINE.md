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

### Phase 3b — M11 group-aware anchor packing (2026-09-08)  🟡
**Authored `block()` groups = placement clusters.** `anchor_place.place_node` now
detects `.group` tags: each authored block packs tight around its own anchor and the
blocks lay out **left-to-right in author (signal-flow) order** (`_place_cluster_flow_row`);
`smart_schematic` engages `placement_mode="anchor"` whenever groups exist (kill switch
`SKIDL_ANCHOR_BLOCKS=0`). Three bugs fixed en route: (1) NetTerminals missing
bboxes/anchor-pins in `place_node` (latent M10 gap — one combined
`add_anchor_pull_pins(real + terminals)` call like the legacy path); (2) generation
aborts skipping `finalize_parts_and_nets` (catch-all restore in `gen_schematic`);
(3) completed sweep attempts leaving rescue/stub flags on the shared circuit — the
sweep now snapshots wire/stub flags and restores them at the anchor→legacy transition.
- **Quality order enforced:** anchor-wired → legacy-wired → partial → all-label. If the
  anchor layout can't fully wire, the build prints
  `placement = legacy (anchor wouldn't wire)` and re-runs the wired sweep cold.
- **2026-09-09 — cross-block labeling implemented:** the classifier now enforces
  "wire in-block, label cross-block" (gates 3/3b/3c in `gen_schematic.py`): function-
  forced wires are scoped to ONE block, and any net spanning blocks LABELS at the
  block edge (kills the snaking-wire anti-pattern; USB_DP went from a 13-inch forced
  wire to clean edge labels; ungrouped flat designs collapse to one block id → zero
  behavior change).
- **Known limitation (current anchor blocker):** in the PACKED (M11) layout the
  NetTerminal label for a stubbed cross-block net lands offset from its pin
  (observed: U2.33's USB_DP label at the correct X but 2000 mil off in Y →
  dangling → verify fails every seed → clean legacy fallback). Root cause is in
  terminal placement / Y-transform for row-centred clusters — M12-tier
  (constraint-aware terminal placement). `SKIDL_SWEEP_DEBUG=1` now also keeps each
  failed sheet as `<name>.debug_fail_seed<N>.anvil_sch` for inspection.
- **Debug:** `SKIDL_SWEEP_DEBUG=1` prints per-seed generation exceptions + verify
  mismatches (unwanted/missing groups) and keeps each failed sheet on disk.
  `SKIDL_CROSS_BLOCK_LABEL=0` restores the pre-scoping classifier for A/B.
- **2026-09-09 — in-block label origin mapped (arduino_nano_demo case):** module↔
  satellite nets inside ONE block render as labels even though (a) the classifier
  says WIRE (gates 3b), (b) `auto_stub_max_group=40` keeps the placement group
  whole, (c) `SKIDL_MAX_LOCAL_NET_SPAN=4000` relaxes the sch_node span filter, and
  (d) **`net.stub` is NEVER set** (verified with a property trap). The labels are
  **NetTerminal-drawn**: `sch_node.add_circuit()` creates terminals for these nets
  and the ANCHOR path keeps/places them (`place_net_terminals`) instead of pruning
  them once the net wires locally. Next work item: prune/skip a net's terminals in
  the anchor path when all its pins are wired in one placement cluster (the legacy
  path's wire-vs-terminal reconciliation). Wire/label truth today has FOUR deciders:
  smart_schematic pre-stub (fanout>3/block) → classifier gates → placer group-split
  → sch_node NetTerminal creation. They must converge on the classifier's answer.
- **2026-09-09 — wire fanout default 3 → 4** (`SKIDL_WIRE_MAX_FANOUT`): 4-pin
  function networks (RESET_N = pin+button+cap+TP) now WIRE instead of pre-
  labeling — verified: stm32 benchmark wires at seed=1 with RESET_N as a wired
  tree (0 labels), regr t1/t2 unchanged. (A blanket function-net EXEMPTION from
  the pre-stub was tried and reverted — it also unstubbed power rails via the
  'decoupling' classification and collapsed the sheet to all-label.)
- **RESOLVED 2026-09-10 (P1) — build determinism fixed (functional):** six
  set-iteration/order fixes landed: (1) `place.group_parts` returns ref-sorted
  LISTS (was sets); (2) `add_anchor_pull_pins` iterates pins/parts in stable
  (ref, pin-num) order (force sums now identical); (3) classifier hands
  `detect_clusters`/congestion a ref-sorted part list; (4) router growth
  direction `random.choice(sorted(...))`; (5) router start-face selection +
  route-tree iteration in stable PIN order (was `list(set)`/set iteration --
  this one flipped the build INTO the good variant permanently); (6) jog
  segments geometry-sorted. RESULT: every run now produces the GOOD layout
  (stm32 benchmark: 71 wires, RESET_N/OSC/BOOT/LED_STATUS all WIRED, zero
  in-block labels) with identical topology; regr t1/t2/t3 unchanged.
  A 7th fix (rowbased BFS neighbor order) landed 2026-09-10. Residual
  (cosmetic ONLY -- verified every run keeps ALL core in-block nets wired,
  0 labels): wire-segment count flaps 69<->74 and one region can shift by one
  grid -- the remaining order-sensitivity is in the NetTerminal placement /
  label-position tier (place_net_terminals force iteration), NOT in part
  placement or routing topology. Hunt method: 2-run uuid-stripped diff
  harness; suspects: terminal evolve loops and `visited_faces`/adjacent
  iteration in the global router search.
- **historical (2026-09-09) — original nondeterminism note:**
  the SAME script + env (PYTHONHASHSEED=0) + SAME pinned seed produces different
  layouts run-to-run (observed: seed=2 gave 77 wires/RESET_N-wired in one run,
  45 wires/RESET_N-labeled in the next). `random.seed()` and hash pinning are NOT
  enough because placement/cluster code keys dicts and breaks ties by **`id(part)`**
  (memory addresses — different every process): e.g. `order_ix = {id(p): i}`,
  `clusters_by_anchor[id(a)]`, `sorted(claims.items())` over id-keyed dicts in
  `anchor_place.build_clusters`, `place.py` group machinery. FIX (next session):
  audit every `id(...)`-keyed ordering/tie-break in `schematics/place.py`,
  `anchor_place.py`, `cluster.py` and replace with stable keys (ref string /
  creation index). Until then a "good" layout (72-77 wires, all in-block nets
  wired -- achieved multiple times) cannot be reproduced on demand; keep the
  good .anvil_sch when a run produces it.
- **2026-09-09 addendum — net NAMING biases toward LABEL:** giving the two LED
  anode nets explicit names (`PWR_LED_A`) flipped the build into the label-heavy
  variant DETERMINISTICALLY (8/8 identical runs, 45 wires); reverting to unnamed
  anode nets restored the good wired variant on the first try (73 wires). So the
  wire/label attractor is sensitive to net naming/creation order, and named nets
  lean label somewhere in the terminal path — audit alongside the id() fix.
- **superseded detail — MCP-vs-local build divergence:** the same rev-D
  stm32 script produced 72 wires / 0 in-block labels when run locally
  (PYTHONHASHSEED=0, seed=1) but 45 wires with all in-block function nets
  labeled when built through the MCP server subprocess — despite
  `_subprocess_env()` pinning PYTHONHASHSEED=0 and the same code/scripts.
  ANVIL_REQUIRE_FOOTPRINTS ruled out by A/B. Some env/state delta still makes
  the server-side sweep take a different seed path (seed-0 label-heavy variant
  verifies first). Until root-caused, the GOOD wired target state is
  reproducible via a direct local run of the project .py.
- **2026-09-10 — voltage→clearance engine added:** `board/voltage_engine.py` —
  the voltage-side twin of `width_engine.py`. IPC-2221 Table 6-1 copied
  VERBATIM from the app's own PCB Calculator source (formula/table parity:
  Tools > Calculator > Electrical Spacing gives identical numbers). Per-net
  voltage heuristics with basis strings (rail-name → volts, USB → 5 V,
  mains-pattern → 230 V flagged VERIFY, signal → 3.3 V), user
  `voltages={net: V}` always wins, `clearance_between(v1, v2)` uses the
  voltage DIFFERENCE, >300 V refuses to guess (use the app calculator).
  Verified dynamically against stm32_usb_devboard (all 17 nets ≤5 V → 0.2 mm
  class floor already safe). NOT yet wired into the pipeline: integration
  point = `initialize_pcb_project` (add `voltages` param → net-class
  clearances / .kicad_dru rules) + `review_design` clearance-vs-voltage check.
- **2026-09-10 — schematic-side design calculator added:** `anvil/design_calc.py`
  — authoring-time value calculations with the same honesty contract (every
  result carries formula + basis): LED series R (typical-Vf table, datasheet
  wins), crystal load caps (AN2867 `2*(CL - Cstray)`), voltage divider, I2C
  pull-up window (spec Rmin/Rmax from VOL/IOL + rise-time), RC cutoff,
  E12/E24 standard-value rounding. Validated against the stm32 benchmark's
  own values (crystal CL 15 pF → 20 pF caps exact match; 4.7 k I2C pull-up in
  the 967-11.8 k window). Closes the audit's "value calculation is AI-process
  only" gap: the AI now has a repo calculator to CALL instead of hand-math.
- **2026-09-10 — voltage→clearance WIRED INTO THE PIPELINE:** `project_init.py`
  now mirrors the width-plan pattern for voltage: `net_clearance_plan` runs on
  every net during `initialize_pcb_project`; any net whose IPC-2221 spacing row
  exceeds its class clearance RAISES that class's clearance (router + every
  DRC then honor it with zero router changes). Sidecar `voltages` overrides
  heuristics; ≤30 V boards = safe no-op; verified: synthetic +48 V rail raised
  Power class 0.2 → 0.6 mm.
- **App-Calculator ↔ pipeline usage map (2026-09-10):** the application's own
  PCB Calculator panels and where each is/should be used by the engine:
  | panel | status |
  |---|---|
  | Track Width (IPC-2221) | ✅ wired — `width_engine` (formula parity verified) |
  | Electrical Spacing (IPC-2221) | ✅ wired — `voltage_engine` (table verbatim) |
  | E-Series / R-calculator | ✅ wired — `design_calc.e_series_nearest` |
  | Regulator divider | ✅ wired — `design_calc.regulator_feedback_divider` (LM317-family, E24) |
  | Via Size | ✅ wired — `width_engine.required_via_drill` + `project_init` raises class via_drill/size per net current (verified: 2 A → 0.906 mm drill) |
  | Transline `c_microstrip` | ✅ `board/impedance.py` — IPC-2141 closed-form (LABELED approximation; app's Hammerstad-Jensen + the fab's tool win for final). USB 90 ohm @ h=0.15 mm: w=0.26/gap=0.39 mm verified |
  | Electrical Spacing IEC-60664 | ⏸ mains/isolation creepage — port the app's multi-table (pollution degree / material group) VERBATIM before any AC design; until then AC boards need manual clearances |
  | Fusing current | ✅ `width_engine.fusing_current_a` — ported VERBATIM from the app's energy-balance model; AUTO-RUN per design in `project_init` (per-class fusing margin vs worst estimated current, `settings["fusing_margin"]`). USB diff geometry likewise auto-suggests when a design has USB-class nets (`settings["usb_diff_geometry"]`, assumed-h labeled) |
  | Board class / cable / RF / wavelength / color / corrosion | manual reference tools (no pipeline role) |
- **Regression suite:** `test_circuits/regr_schematic/` (t1 flat / t2 auto-group /
  t3 @subcircuit) — run before touching the classifier or placers. Verified
  2026-09-09: t1/t2 wire cleanly with the new gates; t3's all-label outcome is
  PRE-EXISTING (same result with the old classifier), tracking the known
  fragile @subcircuit-page flatten path.

### Phase 4c — single-anchor FLOW guard (2026-09-10)  ✅ FIXED
**A single-function, single-anchor circuit now flows left->right instead of
scattering.** Root cause: the anchor placer's spiral+collision separates the
signal core from the power-only passives and spreads them (buck: anchor
component Y-span 163 mm). Fix: `smart_schematic` only REQUESTS `placement_mode
="anchor"` when there are >=2 authored blocks OR >=2 anchors; a lone
regulator/MCU + passives goes to the LEGACY directional placer (BFS signal
depth -> X, power/gnd -> Y), which lays the buck in a compact 38 mm-tall band
(measured, both fully wired, 0 in-block labels, deterministic). Multi-block
designs (stm32) still request anchor as before. Kill switch: force the old
behavior with `SKIDL_ANCHOR_SINGLE=1` (place_node guard) — but the
smart_schematic gate is the primary control. Anchor packing is now reserved
for what it is actually good at: packing MULTIPLE clusters against each other.

### Phase 4b — decap-hug for single-anchor circuits (M12, 2026-09-10)  ✅ DONE
`anchor_place.hug_power_satellites(node)` runs after placement, before
wire/label classification: each power-only 2-pin decap (both pins on rails) is
re-seated in a tidy column just outside the IC it shares a rail with, so a
lone regulator/MCU + its passives reads as a TIGHT decoupling cluster instead
of a stranded row. Measured buck_12v_5v_demo: caps 155-200 mm -> 26-50 mm from
U1, still fully wired (0 in-block labels), deterministic (identical hash across
runs). **Guarded to SINGLE-anchor designs only** (`len(anchors)==1`): with >=2
ICs the anchor packer already clusters each IC's decaps and hugging fights it
(measured stm32 regressed 72 wired -> 49 with core nets labeled, so it's
skipped there -> stays 80 wired, RESET_N=0). Kill switch `SKIDL_HUG_DECAPS=0`;
hooked in `gen_schematic.py` after `node.place()`, recurses child sheets,
failure swallowed so it can never break a build. Regression suite (t1-t4) all
still route with wires. The remaining Phase 4b work is the MULTI-anchor case
(tight decap clusters inside a packed multi-IC block) -- harder, deferred.

### Phase 4d — author FLOW-HINT placement (2026-09-10)  ✅ DONE
`anchor_place.flow_place_hinted(node)` — the reliable answer to "full flow".
Auto-detecting input/output rails on switchers is impossible (below), so
instead the AUTHOR states the flow: tag each part `part.flow_x = <column>`
(0 = input left ... N = output right) and optionally `part.flow_y` (row within
a column). The engine lays those columns out left-to-right. Delivered the
EXACT ideal on buck_12v_5v_demo: J1 -> C1/C2 -> U1 -> L1/D1 -> C3/C4 -> R1/D2
-> J2, fully wired (29 wires, 0 labels), verified, deterministic. Runs before
the decap-hug (hints win; no hints -> hug/normal, zero change to un-hinted
circuits: regr t1/t2/t4 + stm32 all unchanged). Hooked in `gen_schematic.py`
after place(). This is AUTOMATIC from a user prompt: the skidl-circuit skill
(which understands the circuit as it writes the script) emits the flow_x tags,
so the user types a request and gets a flow-ordered schematic. Recommend the
skill set flow_x for every multi-stage function.

### Phase 5g-CONFIRMED — stm32 33 labels ROOT CAUSE (2026-09-15)  🔍 router-internal
CONFIRMED chain (instrumented end-to-end): pre-place gates PASS (SKIDL_NET_DEBUG
'OSC_IN': crosses=False, candidate=True -> wired), classify gates KEEP wire
(same_block -> gate 3/3b continue), hug NOT the cause (fails with
SKIDL_HUG_DECAPS=0 too). The labels come from route.py's CHILD-SHEET fallback:
`child.route()` raises RoutingFailure for the stm32 group-children
(POWER_+_USB_IN and MCU_CORE) at EVERY seed tried, and stub_internal_nets()
converts the whole child to labels -- previously SILENT. SHIPPED: (a) the
fallback now prints LOUDLY; (b) 3 offset-seed retries (seed+101/202/303) per
child before surrendering (heals seed-sensitive cases; stm32's is structural).
REMAINING (true router work) -- NOW LOCALIZED to rt_srch (route.py:2327
GlobalRoutingFailure): the child's LOCAL face graph is UNREACHABLE between
named pin faces (verified get_internal_pins IS node-local, sch_node.py:735 --
the exception text just lists all net.pins). Failing nets captured:
POWER_+_USB_IN child: USB_DP (J1/D+ -> R3/NT1); MCU_CORE child: BOOT0
(R4 -> U2/44). So the frontier exhausts inside the child's own switchbox/face
adjacency graph -- suspects: (a) the same-part adjacency exclusion around the
big MCU walls off its pin faces, (b) zero-capacity faces from hugged
satellites' tight spacing. UPDATE (same day): capacity-relax retry SHIPPED in rt_srch (one re-search
ignoring face capacity before failing; matrix stable). GR_DEBUG probe (shipped,
env SKIDL_ROUTE_DEBUG) then PROVED both classes are GRAPH DISCONNECTION, not
capacity: BOOT0's stop face has adjacent==0 (orphan; 676 faces visited, still
unreachable) and USB_DP's faces sit in a tiny island (visited=3-16 even
relaxed). ROOT FIX LOCATION: face-graph construction -- add_adjacencies()
(route.py:580) silently `return`s when SwitchBox(face) raises NoSwitchBox,
leaving that face with ZERO adjacencies. NEXT SESSION: make orphan pin faces
impossible -- when SwitchBox construction fails for a pin face, fall back to
connecting it to its geometrically coincident/overlapping track faces (or fix
the SwitchBox geometry cause); verify with draw_switchbox on MCU_CORE.
Diagnostic tool added: SKIDL_NET_DEBUG=<substr> prints net keys/candidate at
sch_node.add_circuit. Matrix stable 10/11 after change (arduino flap only).
gen_schematic gate (3) already forces clock/reset/decap nets to WIRE when
same_block (hiertuple+group identical, :472-496) -- so OSC_IN/RESET_N labeling
means same_block is FALSE for them. Leading hypothesis: some satellites (TP4
reset testpoint, R2/D2 LED, R4/R5 boot) are NOT in the MCU_CORE group (tag
missing/different) -> their parts stay in a DIFFERENT node -> net crosses
nodes -> add_circuit emits NetTerminals/labels BEFORE any wire gate runs.
VERIFY: print each labeled net's pins' (hiertuple, group). FIX direction: at
build time, ADOPT ungrouped stray parts into the group they share most signal
nets with (set p.group before node partition) -- mirrors place_node's
stray-adoption, but at GROUP level so node membership (and thus wire gates)
see it. Satellite-hug seating (Phase 5f item 2) is already in place and will
then get the wires. Also NOTE: satellite-hug extension shipped gated
(_rowed skip) -- ERC 0, no regression, awaiting this fix to show value.

### Phase 5f — "all three" batch (2026-09-15)  ✅ 2.5/3 / ⬜ open items
1. BLOCK FLOW ORDER + TOP-DOWN ROWS ✅: layout_blocks_by_role orders blocks by
   part CREATION index (author's input->process->output; falls back to role
   order) and _wrap now steps later rows DOWNWARD in placer-Y so reading order
   on the rendered sheet is top->bottom (was: block 04 ABOVE block 01).
   power_board: 01 INPUT -> 02 5V -> 03 3V3 top row, 04 OUTPUT next row. 
2. SHEET PACKER ✅ (V1, part-count proxy): big (>50) FLAT design with blocks ->
   fill sheets with WHOLE blocks in flow order (~SKIDL_SHEET_FILL_PARTS=40 per
   sheet), each bundle a real Node (auto_hierarchy part-move -> ports), pages
   render blocks as boxes. t6_big_pack (56 parts / 8 blocks) -> 2 sheets, ERC 0,
   permanent check_all row. Kill switch SKIDL_SHEET_PACK=0. V2 = geometric
   area fill (needs measured block bboxes).
3. ROUTER TAIL ⬜ (LED chains on packed rows): series-horizontal orientation
   hypothesis tried and REVERTED (no effect). Needs failed-child-sheet
   artifacts to diagnose (sweep keeps only the root); repair path keeps
   correctness meanwhile.
4. verify_connectivity.export_from_schematic: one retry (0.5 s) on kicad-cli
   failure; check_all: rebuild-once flap absorber before declaring FAIL.
⬜ OPEN ANOMALY: arduino builds ALL-LABEL (12w/24l) when run FROM check_all's
   python subprocess (fails even with retry, regardless of position in the
   list) but passes ISOLATED from a shell twice (55w/0l). Not t6-adjacency,
   not transient cli (retry didn't fix). Suspect an environment-sensitive
   ordering (id()/env/path-style) in the seed sweep; needs a dedicated diff
   session: dump SKIDL_SWEEP_DEBUG in both contexts and compare seed verdicts.

### Phase 5e — D.0 block-sanity gate ENFORCED (2026-09-15)  ✅ DONE
User caught the engine shipping an over-split small circuit (t5 authored as 4
blocks -- "STATUS LED"/"OUT" boxes for a 10-part regulator chain; violates docs
D.0 "a very small single-function circuit is ONE block (or none)" and the
satellite rule). The rule is now ENGINE-ENFORCED, not author discipline:
smart_schematic gate (after part census): n_parts <= SKIDL_ONE_BLOCK_MAX (12)
AND >1 authored/auto group -> clear every part.group + set
opts["suppress_block_boxes"]; gen_schematic flags node tree _no_box; the
sexp box-draw skips flagged nodes (flattened page boxes included). A small
design with EXACTLY ONE authored block keeps its titled box (buck). Result:
t5 default = single clean sheet, 0 boxes, 33 wires, 3 ladder rails, ERC 0;
buck keeps 1 box; power_board (16 parts) keeps its 4 blocks; 10/10 check_all.
TIER RULES now all enforced in-engine: <=12 parts + multi-split -> ONE
function; <=50 -> single sheet with (sane) blocks; >50 + >=2 pages ->
hierarchy (each page same block treatment).

### Phase 5d — blocks INSIDE hierarchy pages verified (2026-09-15)  ✅ / ⬜ LED-chain tail
New regression t5_hier_blocks (2 @subcircuit pages, each with 2 authored
blocks; forced hierarchy): each child sheet renders EXACTLY like a flat
single-sheet-with-blocks -- 2 dashed boxed sections per page, in-block WIRES,
per-block ladder rails, cross-page STATUS_SIG as sheet pin + hier label +
global label, top sheet = pure block diagram, ERC 0 on all 3 sheets.
power_pg is PERFECT (16 wires, 0 local labels). ⬜ KNOWN TAIL: the io page's
R->LED chain (N$1) repaired to labels -- same class as t3 load: the router
draws tiny R->LED chains on a packed row as shorts, seeds fail verify, the
repair path labels them (correct by construction, sub-optimal look). The
signature both times: a 2-part LED chain block, and in t5 the block also
holds a cross-sheet net endpoint (R2.1 -> STATUS_SIG terminal). Fix belongs
in the router/row geometry, not the gates. t5 is a permanent check_all row
(10 rows, all OK).

### Phase 5c — hierarchy pages get the ladder too (2026-09-15)  ✅ DONE / ⬜ router tail
1. flow_place_block now also rows an UNGROUPED @subcircuit PAGE node (a page IS
   one function, docs D.0): `_is_page = node.parent is not None and None in
   groups`. A flat ungrouped ROOT still bails to legacy. Same size gates.
2. sch_node._net_is_locally_compact: an IN-FUNCTION net (all pins in one group,
   or ungrouped in one node) is never span-vetoed -- that veto ran on
   PRE-PLACEMENT coordinates and was labeling 2-pin R->LED nets the row later
   put 350 mil apart. Post-placement gates (crossings/congestion/distance/
   fanout) still apply. Result: t3 FLAT improved 8 labels -> 2.
3. RESULT forced-hierarchy t3: psu page = FULL ladder (rails drawn, 0 labels,
   one symbol per rail); load page = rails drawn + rows, but its 3 R->LED
   chains stay labels: the ROUTER drew them as shorts on the tight row (seeds
   fail verify) and the repair path correctly labeled them -- correctness
   preserved by design. Wider page rows made seeds worse (tried, reverted).
   ⬜ open tail: router success on dense page rows.
4. check_all: buck/power_board contract extended with a LADDER invariant
   (a merged horizontal rail run >= 20 mm must exist) after a transient buck
   rail regression slipped past the labels/ERC-only contract. 9 rows OK.

### Phase 5b — PER-BLOCK ladder rails (2026-09-15)  ✅ DONE
The ladder is now a PER-FUNCTION shape on multi-block sheets, not a buck-only
feature. Three pieces:
1. flow_place_block tags rowed parts `_flow_rowed`; smart_schematic._do_powerrail
   treats that like author flow hints -> rail-draw AUTO-ON for any sheet whose
   blocks the engine rowed (no env, no per-circuit setup).
2. draw_power_rail partitions every power net's pins BY DASHED BLOCK BOX
   (_block_boxes parses the drawn `(rectangle ... (type dash))` records): one
   rail + ONE kept symbol per (net, block); a rail never crosses a block
   boundary; pins outside any box form their own partition. Junctions per
   partition; body-crossing exclusion per partition.
3. The block recipe that WIRES everything is row + HUG together (gen_schematic
   _hug_all): flow_place_block row THEN hug_power_satellites. Measured: row
   without hug -> SW_NODE/N$1 regress to labels (row too wide, router falls
   back); hug without row -> 2-D scatter. Row+hug+per-block-rails ->
   power_board 0 labels, 8 rails, ERC 0; stm32 gained rails on its small
   blocks (45->73 wires); buck ladder unchanged; all 9 check_all rows OK.

### Phase 5a — hierarchy (multi-sheet) ERC-clean (2026-09-15)  ✅ DONE
Forcing t3_subcircuit into hierarchy (SKIDL_HIER_MIN_PARTS=5) exposed 4 real
defects in the multi-sheet path; all fixed, root+children now ERC 0:
1. CHILD HIER LABELS: the parent's sheet boxes emit a sheet PIN for every
   global-scope signal net (create_hierarchical_sheet_sexp), but the child's
   hierarchical-label emission SKIPPED stubbed nets and placed labels floating
   at a fixed margin -> every stubbed pin unmatched (hier_label_mismatch).
   node_to_sexp_schematic now emits one hierarchical_label per iface net using
   the SAME selection rule as the parent pins, COINCIDENT with the net's first
   pin point in the sheet (recursing into inline flattened children).
2. SHEET-PIN GRID: sheet boxes sat off the 1.27 mm grid; grid_snap then moved
   the coincident top-sheet global label onto the grid but NOT the sheet pin
   (0.25 mm apart) -> label read dangling (stripped) + pin_not_connected.
   create_hierarchical_sheet_sexp now snaps the sheet box (and so every edge
   pin) to the 1.27 mm grid.
3. STRIP ANCHOR: strip_dangling_labels now treats SHEET-PIN points as anchors
   -- a top-sheet label coincident with a sheet pin is the pin's tie into the
   flat net and must never be stripped.
4. CHILD-STANDALONE ERC ARTIFACTS: each child sheet is also ERC'd as a
   standalone file, where kicad-cli treats it as a root -> "hierarchical label
   ... non-existent parent sheet" and power_pin_not_driven (PWR_FLAG lives ONCE
   per project, on a sibling sheet). ipc_check.report(child=True) filters
   exactly those two artifact classes; the whole-project ROOT ERC remains the
   real gate and still catches genuine defects.
check_all.py now runs a permanent t3_HIERARCHY row (forced multi-sheet) so all
THREE tiers -- single circuit, single sheet with blocks, hierarchy -- are
regression-checked by one command (9 rows, all OK).

### Phase 4j — auto within-block wire-flow for linear blocks (2026-09-15)  ✅ DONE / ⬜ MCU blocks
`flow_place_block` (anchor_place.py) + hook in gen_schematic `_hug_all`: after
manual flow hints (flow_place_hinted) and before the decap hug, a SINGLE-FUNCTION
BLOCK with no author hints is laid out as a TIGHT flow ROW (creation order ==
the author's input->process->output order) centred on the block's current spot,
so the block's LOCAL signal nets are short and the router WIRES them instead of
labeling. GATED to small LINEAR stages: skip if >10 parts OR any part >16 pins
(a big MCU/IC fans out in 2-D; rowing it strings the fan-out and LABELS MORE --
measured stm32 12->33, arduino 0->24, so the gate is essential). Kill switch
SKIDL_FLOW_BLOCK=0. RESULT: power_board_demo (4 blocks: 12V input, 5V buck, 3V3
LDO, output) went 5 within-block labels -> 0; buck 0, arduino 0 -- all ERC 0.
This is the DYNAMIC within-block-wire rule for linear blocks (any power stage /
analog chain), not a per-circuit hack.
REMAINING (⬜ dense MCU blocks): an MCU block's signal satellites (crystal on
OSC, reset R-C-button on NRST, boot Rs on BOOTx, indicator on a GPIO) still
LABEL because they are not hugged to the MCU PIN they connect to (only decaps
are). A generalized "hug EVERY satellite to its shared anchor pin" was tried and
REGRESSED the linear-block circuits (it runs after flow_place_block and
over-moves parts: power_board 0->5, arduino 0->24) -- reverted to decap-only hug.
The correct fix is MCU-block-only satellite-pin hugging that does NOT fight
flow_place_block; deferred. stm32 stays ERC 0 with ~33 labels (many are correct
cross-block MCU I/O: SWCLK/SWDIO/UART/USB).

### Phase 4i — pin-aware decap hug (2026-09-11)  ✅ DONE
`hug_power_satellites` used to stack ALL of an IC's decoupling caps in ONE tall
column off the IC's left edge -- which reads as a strung-out vertical chain (the
stm32 MCU block: C3..C11 in a line). Grounded in the user's H&C_CTRL reference
(the GD32 MCU's caps sit tight on ALL FOUR sides, each by the power pin it
decouples), the pass now seats each decap RADIALLY OUTSIDE the anchor power PIN
it shares a rail with (near the IC body, not the label bbox), distributed around
the IC, preferring a rail (+V) pin over ground and spreading caps across the
anchor's several VDD pins. Duplicate caps on one pin stack further out along the
same radial. Instrumented first (SKIDL_HUG_DEBUG in gen_schematic._hug_all) to
confirm the runtime path: hug runs PER BLOCK via recursion (moved 6 caps in the
stm32 MCU block, 2 in power) -- it WAS running, just column-stacking. Result:
stm32 MCU decaps now hug U2 around its VDD pins (top + right) instead of a left
column; ERC 0; buck ladder + arduino unchanged (both use flow hints, which skip
hug). Kill switch unchanged (SKIDL_HUG_DECAPS=0). This is the intra-block
compactness prerequisite for clean 2D block packing + sheet-fit.

### Phase 4g — TRACKER "ladder" rail-draw (2026-09-10)  ✅ DONE
`skidl/anvil/draw_power_rail.py` + `_do_powerrail` in smart_schematic's guarded
post-pass loop (after label-tap removal, before grid-snap+junctions). It is NOT
a router -- it is a pure text transform over the finished .anvil_sch that
collapses "a power symbol on every pin" into the ladder: per rail it reads each
per-pin power symbol's stub far-endpoint (the part pin), draws ONE horizontal
spine at the topmost symbol row (+rails) or below all pins (GND), drops a short
vertical stub from every pin to the spine, EMITS ITS OWN junction dot at each
interior tap (critical: the connectivity guard verifies BEFORE ensure_junctions
runs, so an un-dotted interior tap reads as a split net -> revert), and keeps
exactly ONE power:<net> symbol relocated onto the spine as the source (so
add_pwr_flags still finds it and ERC stays clean). ensure_junctions then splits
the spine into collinear endpoint-to-endpoint segments (standard KiCad form).
AUTO-ON when the circuit has flow_x hints (the single-row layout where a
straight rail fits); SKIDL_DRAW_RAILS=0/1 forces off/on, SKIDL_DRAW_RAILS_GND=0
keeps GND per-pin. WHY the maze router couldn't (Phase 4e): connectivity in
KiCad is coincident-endpoint only, so a rail is trivial to generate directly but
a router that plans around obstacles collapses wide rails to fallback. WHY it's
safe: every step is inside smart_schematic._guarded, which re-extracts the
netlist via kicad-cli and reverts on ANY pin-partition change -- so a rail that
would fuse/split a net is rolled back automatically (worst case = no ladder,
never a wrong net). RESULT on buck_12v_5v_demo (no env vars, pure auto-detect):
+12V rail 61 mm, +5V rail 138 mm, GND rail 199 mm, ONE symbol each (power
symbols 23 -> 6), 18 junction dots, ERC 0 errors, guard accepted, verified
visually via anvil-cli PDF export. Non-flow circuits (arduino/stm32) skip it
(flow_mode=False) -> zero regression.

NO-WIRE-OVER-COMPONENT rule (2026-09-10): the router already treats a part bbox
as un-routable (route.py:1104 "a part bbox that wires cannot be routed
through"), which is WHY signal nets detour around parts. The rail-draw bypasses
the router, so it enforces the same rule itself: _part_bodies + _stub_crosses_body
estimate each part's body box (passives tiny, ICs ~13x16 mm) and any power pin
whose straight stub to the rail would run THROUGH a body is EXCLUDED from the
rail and KEEPS its own power symbol (the standard idiom: a regulator's VIN/FB on
the far side from the rail carry their own +rail symbols; the passive filter caps
share the rail). Verified on buck: the U1 VIN->+12V stub that crossed U1's body
is gone, 0 body crossings across all parts, ERC still 0. This is the same root
cause as the SW-node detour (Phase 4h): the IC's pins face the wrong way for a
flat row -- the full cure is IC orientation / a compact 2D switching cluster.

### Phase 4h — function-aware gaps + the SW-detour finding (2026-09-10)  ✅ gaps / ⬜ detour HARD
`flow_place_hinted` now spaces parts on their PIN SPAN + a FUNCTION-AWARE gap,
not the wide value-text box: two neighbours sit TIGHT when they share a function
(same flow_x column, OR a routed signal net -- e.g. U1-L1-D1 all on SW_NODE) and
get a wider gap at a function boundary (user's rule: "gap based on function").
MIN_SLOT=250, INTRA_GAP=220, INTER_GAP=620 (placer units). Result on buck:
199 mm -> 173 mm, ERC 0, ladder intact. IMPORTANT LIMITS learned:
(1) There is a MINIMUM spacing floor -- pack tighter (tried INTRA 150/INTER 450)
and the M6 label-collision relaxer (gen_schematic._relax_label_collisions) fights
back, scrambles the row, and breaks connectivity/ERC. To go tighter than the
current width the VALUE TEXT itself must shrink or move (a gen_schematic text
change), not just the gap.
(2) The SW-node "suthi" (detour) is NOT fixed by tighter spacing -- it gets
WORSE: U1.OUT exits UPWARD (pin on top of the rot-90 IC) and L1/D1 sit to the
right on the flat baseline, so the SW signal must climb over; when packed tight
the router escapes ABOVE the rails (y~84) to find a clear lane, a longer/uglier
loop. The real fix is placement+orientation: either rotate U1 so OUT faces the
neighbour, or let the switching cluster (U1,L1,D1) be a COMPACT 2D sub-block off
the flat row so the SW node is a short straight segment. That is a bigger, risky
change (co-design with rail-draw + junctions, per-part orientation logic) -- do
it as a dedicated phase with visual verification, not a spacing tweak. Only
affects flow-hinted circuits; non-flow designs skip flow_place_hinted entirely.

### Phase 4f — single-row (ladder-ready) placement (2026-09-10)  ✅ DONE
`flow_place_hinted` lays flow-hinted parts in ONE horizontal row (all on one
baseline; buck Y-span 4 mm). This is the placement PREREQUISITE for the ladder
(Phase 4g): with no vertical stacking, every part's power pin has a clear
vertical path up to a top rail and every ground pin a clear path down to a
bottom rail. Flow order preserved (J1 C1 C2 U1 L1 D1 C3 C4 R1 D2 J2), wired,
verified. Function-aware gaps refined in Phase 4h; rail-draw is Phase 4g.

### Phase 4e — TRACKER "ladder" rail-wiring (attempted, reverted 2026-09-10)  ⬜ HARD
Tried to render power rails as horizontal WIRES (TRACKER ladder: power along
the top, GND along the bottom, components as wired rungs) instead of a symbol
per pin, gated on flow-hints. Got the INPUT rail (+12V, 4 pins, localised) to
wire, but: (1) wide rails (+5V 6 pins spanning all columns, GND 11 pins
touching everything) FAIL to route as one wire -> the sweep collapses to a
worse partial/all-label fallback; (2) a wired rail with a PWRIN pin and no
driving symbol trips `power_pin_not_driven` ERC (exactly what the original
PWRIN-exception guarded) -- add_pwr_flags didn't reliably flag every wired
rail. Reverted all three stub-point edits. CONCLUSION: the ladder needs a
dedicated RAIL ROUTER (draw a straight horizontal rail wire + place ONE
driving power symbol per rail) -- the general maze router can't, and bypassing
the symbol stubbing breaks ERC. This is a major feature, not a tweak; the
TRACKER's ladder is hand-drawn. Kept: flow-hint ordering + tight spacing +
decap-hug (all working). Power connections remain symbol-per-pin (valid,
standard) until a rail router exists.

### Phase 4d-note — why AUTO rail-detection failed (kept for the record)  ⬜ HARD
Tried to order a single-anchor function INPUT->IC->OUTPUT left-to-right by
classifying each rail as input/output from the anchor's pin functions. FAILED
on switching regulators (the fundamental blocker): a buck's output rail cannot
be identified from pin functions -- the FB pin is func "input" yet senses the
OUTPUT rail, the OUT pin sits on the SWITCH node (not the output rail), and the
real output rail is DOWNSTREAM of the inductor with no direct IC pin. So
in/out rail detection mislabels and the flow comes out scrambled. Reverted;
kept the decap-hug (Phase 4b). CONCLUSION: perfect signal-flow placement needs
real topology understanding (source->switch->filter->load tracing), not pin
functions -- it is genuinely hard and is why professional schematics
(incl. the user's own TRACKER, hand-placed by its engineer) are auto-generated
for CONNECTIVITY then MANUALLY ARRANGED. Auto-placement delivers a correct,
wired, blocked, clustered STARTING POINT; final flow polish is human work in
every EDA tool.

### Phase 4b-multi — decap-hug within a packed multi-IC block  ⬜ HARD
**Measured on buck_12v_5v_demo:** the signal core (regulator U1 + L1 + D1, joined
by the SW net) packs TIGHT (36-50 mm), but power-only decaps sit 155-200 mm away and
the whole block spreads to ~200 mm wide. Root cause CONFIRMED by experiment:
reordering decaps to inner spiral cells had ZERO effect -- `_resolve_collisions`
physics dominates, pushing big-bbox parts (electrolytics) off the big-bbox anchor
(TO-263 + its label). This is the `_pack_cluster` NOTE's own caveat ("a big IC's
label bbox means 'just outside the symbol' is already ~40 mm from centre; the
collision resolver pushes small parts further off"). REAL fix = body-bbox-aware
CONSTRAINT placement (place a decap at the IC's power-pin exit, lock it, resolve
collisions around the locked pair) -- a deep change, deliberately NOT rushed while
the engine is freshly stabilized + deterministic. Electrical/wire/label output is
unaffected (correct + verified); this is cosmetic packing only.

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
| Max 3 lines per node — no 4-way junctions (stagger into two T's) | ⬜ | needs a `beautify_wires`/`ensure_junctions` pass; see `docs/SCHEMATIC_DESIGN_RULES.md` C1 |
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

### Documentation rules (from industry review 2026-09 — `docs/SCHEMATIC_DESIGN_RULES.md`)  ⬜
| Rule | Status | Blocker |
|---|:--:|---|
| On-sheet notes (jumpers, DNP, layout constraints) — per-sheet OR dedicated Notes sheet | ⬜ | `sexp_schematic` has no `(text …)` emission — add `notes=` to `smart_schematic.build()` |
| Revision-history page (structured: rev/date/desc/author/reviewer) | ⬜ | needs `(text)` emission (above); title block carries `rev=` only |
| Block-diagram overview as page 1 (multi-sheet) | ⬜ | `block_former` knows blocks + inter-block nets; render as sheet boxes + arrows |
| Ordered ALPHABETICAL page naming by role (`A_Block_Diagram`, `B_Power`, …) | ⬜ | sheet names come from `tag=`; derive prefixes from the existing block role classification (missing capability, not partial) |
| Cross-sheet ports snapped to left/right page edges | 🟡 | hierarchical/global labels placed with no edge preference |
| Table-of-contents page for multi-page designs | ⬜ | generate from block/page classification; needs `(text)` emission |

Priority order (docs/SCHEMATIC_DESIGN_RULES.md §F, peer-review 2026-09-09; §E
ground-truth measurement: 0 global labels across 14/15 real KiCad demo projects
-> C8 confirmed by data):
**C8 sheet-pin ports → C4/C5/C6 → C1 junction beautify → C2/C3/C9 (text emission) → C7.**

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
- **Parameters** (exit 150mil, spacing 100mil, `D_wire`, `R_anchor`) scale from the tool grid / pin pitch / sheet size — tunables table in `WIRE_LABEL_RULES.md` ("Tunable parameters"). (An older `SCHEMATIC_ENGINE_RULES.md` reference pointed to a file that no longer exists.)
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

### Phase 5h — ORPHAN-FACE HEAL shipped (2026-09-15)  ✅ partial / ⬜ islands
create_routing_tracks now HEALS zero-adjacency part faces (connect to nearest
overlapping parallel-track face, same-part/boundary exclusions; kill switch
SKIDL_ORPHAN_HEAL=0; loud print). stm32: 16+31 orphans healed per child;
BOOT0-class failures GONE. REMAINING: USB_DP island-class — a face COMPONENT
of size 3-18 disconnected from the stops even capacity-relaxed (J1 USB
connector region). Next: heal must guarantee CONNECTED components (repeat
heal transitively or BFS component merge), then matrix + revert check.
NOTE: full check_all NOT yet run after heal — run FIRST next session.

### Phase 5h-UPDATE — island merge shipped (2026-09-15)  ✅
_merge_islands in create_routing_tracks: union-find over the face adjacency
graph; every PART-bearing island bridged to the rest via the cheapest
parallel-track overlapping pair (same exclusions; <=50 bridges; loud print).
RESULT stm32: 2-7 islands bridged per pass and the CHILD-SHEET FALLBACK NO
LONGER FIRES -- both children now global-route at seed 0, no verify failures,
no repairs. Matrix 10/11 stable (arduino flap only; stm32 74 wires).
⬜ REMAINING: 33 labels persist WITHOUT any route fallback -- some OTHER
decider still stubs OSC_IN(3 labels)/RESET_N(4)/BOOT*/LED_STATUS/N$*. All
known deciders ruled out earlier for THESE nets (pre-place candidate=True,
classify gates keep wire, fanout gate no, stub_internal_nets not invoked).
NEXT HUNT: property-trap net._stub / pin.stub setters for OSC_IN during a
full build to catch the writer red-handed (suspect: _stub_pin route.py:129
"suppress redundant boundary label" path, or NetTerminal-side rendering).

### Phase 5i — stub-writer hunt + UNIFIED FLAP THEORY (2026-09-15)  🔍
Hunt instrumentation SHIPPED (all env-gated prints, zero behavior change;
matrix 11/11 green after): SKIDL_NET_DEBUG now traps every wire/label decider
-- add_circuit keys/candidate, classify _stub(gate-id), placer group-split,
ERC-fix stub, fanout gate, repair stub, Net.stub setter (stack), label
emission point (LABEL_EMIT pin+stub+force), route _stub_pin, GR_DEBUG,
SKIDL_STUB_TRAP2 (Pin.stub property trap -- WARNING: perturbs the sweep,
diagnostic only). FINDINGS: in wired-mode runs NO python-level writer fires
for OSC_IN yet write-time pins are stub=True exactly once (final pass) --
every individual decider ruled out. KEY UNIFICATION: during heavy-
instrumentation runs stm32 flapped to "no seed produced a connectivity-clean
wired schematic -> all-label mode" (writer = the LAST-RESORT loop, caught by
NET_STUB_SETTER stack) -- the SAME signature as the arduino matrix flap
(13w/24l). THEORY: the per-seed verify (kicad-cli export) intermittently
fails under load/timing -> seeds "fail" -> all-label; the 33-label wired-mode
residue is the tail of a PARTIALLY flapped earlier pass whose flags leak into
the final generation. NEXT: (1) log verify failures per seed loudly with the
cli stderr; (2) extend the export retry to the verify()/ERC paths + backoff;
(3) if flap gone, re-measure stm32 labels (may drop below 33 naturally).

### Phase 5j — FLAP FIXED standalone; stm32 in-block labels SOLVED (2026-09-15) ✅/⬜
SHIPPED: (1) verify export 3x retry + backoff + LOUD stderr; (2) sweep
INFRA-vs-DESIGN verify distinction + retry; (3) HEAL-ROLLBACK -- generation
exception with face-heal ON disables SKIDL_ORPHAN_HEAL for remaining seeds
(synthetic hop without backing switchbox broke detailed routing every seed on
dense sheets). RESULT standalone stm32 3/3 runs: NO all-label flap; labels
33 -> 15 and ALL remaining are legitimate cross-block (USB_DP/DM, UART, SWD,
N$1); OSC/BOOT/RESET/LED_STATUS satellites now WIRED. wires 80.
⬜ RESIDUE: inside check_all's python-subprocess context stm32 still lands 33
and arduino flaps (13w/24l) while STANDALONE shell builds are stable-good --
an environment-sensitive divergence (not transient: retry-persistent).
NEXT: diff the two contexts (env, cwd form, stdio buffering) by dumping
sweep-debug per-seed verdicts in both and comparing; suspect subprocess env
or path-form-dependent behavior in kicad-cli/verify.

### Phase 5k — CHILD-SCOPED HEAL: flap fixed, both flagships best-ever (2026-09-15) ✅
Root-cause of the heal interplay: healing the ROOT node's face graph created
global hops with no backing switchbox -> detailed router raised EVERY seed,
burning seed-0's good layout (arduino 55w/0l -> 13w/24l). THREE fixes landed:
(1) heal/island-merge now run for CHILD nodes ONLY (route.py gate:
node.parent is not None) -- the orphan/island defects live in dense child
blocks, the root never needed healing; (2) sweep heal-rollback now RETRIES
THE SAME SEED with heal off instead of burning it; (3) cleanup: debug
artifacts removed, all 13 modified modules py_compile OK.
RESULTS: arduino 48-49w/0 labels (standalone AND in-matrix -- the matrix flap
is GONE); stm32 standalone 121 wires/15 labels (best ever; satellites all
wired; remaining 15 all legitimate cross-block: USB/UART/SWD/N$1);
check_all 11/11 ALL GREEN. Residual: stm32 in-matrix lands 45w/33l variant
(ERC 0, correct, cosmetic variance only) -- low-priority determinism tail.
