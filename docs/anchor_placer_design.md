# Anchor-Centric Placer — Design Document (Milestone 4, paper only)

**Status:** DESIGN — no placer code is written until this is approved.
**Goal:** replace the sprawled/overlapping force-directed *global* placement with an
anchor-centric, visual-bbox-aware layout that reads like a hand-drawn KiCad schematic —
**generically, for any circuit** (LED board → STM32 → FPGA → 500-part industrial), driven
only by the connectivity graph, never by part names.

This document is grounded in the *existing* engine (`RULE_ENGINE.md`) and reuses its
primitives; it does **not** propose a from-scratch rewrite.

---

## 1. Problem statement (measured, not assumed)

On the STM32F103C8T6 board (31 parts, connectivity 100% correct, ERC 0 errors):

| Symptom | Measurement |
| --- | --- |
| **Overlap** | Closest symbol pairs **6–11 mm** origin-to-origin (R1↔SW1 6.5, R1↔R2 7.6, C11↔R3 8.9, C8↔C9 9.0). A resistor body is ~7 mm + ref/value text → bodies, pin-names and labels overlap. |
| **Sprawl / empty centre** | Sheet span **357 × 217 mm**; sheet centre empty; MCU in a corner (364, 100). |
| **Anchor** | MCU is **not** the visual centre; satellites (crystal 320 mm away) fling to opposite corners. |

**Two root causes, both confirmed in code:**

1. **Power nets merge everything.** `place.group_parts` groups by net connectivity; `GND`
   + `+3.3V` touch nearly every part → **one giant connected component** → one
   force-directed blob (`place.py:1464`). *(This is the classic force-directed enemy the
   professional tools avoid by ignoring power edges.)*
2. **Collision uses body bbox, not visual bbox.** `place.overlap_force` (priority 95)
   repels symbol *bodies* but does not reserve pin-name text, reference, value, or attached
   power-symbols/labels — so text/labels overlap neighbours even when bodies don't.

## 2. Design principles (non-negotiable)

- **P1 — Signal graph, not power graph.** Build the placement graph from *signal* nets
  only; **exclude power/ground nets** (`net_classify.classify_net_role(net) is not None`).
  Power is resolved in a *later* stage as symbols, never as placement edges. This is the
  single most important change — it dissolves the one-blob problem.
- **P2 — Connectivity is untouched.** No change to ERC / netlist / `verify_connectivity` /
  hierarchy-nets. Placement only moves symbols; the connectivity gate + all-label fallback
  still guarantee the drawn netlist.
- **P3 — New module + feature flag.** New `src/skidl/schematics/anchor_place.py`. `place.py`
  gets a `placement_mode` switch (`"legacy"` default | `"anchor"`); **zero impact** on
  existing users until opted in. Enables A/B benchmarking and gradual adoption.
- **P4 — Fully dynamic.** Every decision is a *generic* inference (ref-prefix class, pin
  function, net-role regex, graph degree). No `if STM32` anywhere. Same pipeline for MCU /
  power / analog / FPGA / motor-driver boards.
- **P5 — Visual bounding box everywhere.** Collision reserves the *whole* occupied area
  (body + pin-name text + reference + value + attached labels/power-symbols), not just the
  body.

## 3. Reused vs. new

| Stage | Reuse (exists) | New (this design) |
| --- | --- | --- |
| Signal-graph build | `net_classify.classify_net_role` (power filter) | signal-only adjacency |
| Clustering | `cluster.detect_clusters`, `compute_net_affinity_weights`, `find_decap_affinities` | cluster **sub-segmentation** by signal role |
| Anchor detection | `cluster.classify_block_role` (highest-pin IC = anchor) | per-cluster anchor + global anchor |
| Local placement | `place.net_force_dist` (within-cluster) | anchor-relative ring seed |
| **Global placement** | — | **anchor-centric ring/flow layout** |
| **Visual bbox** | `part.lbl_bbox` (body+fields) | + pin-name + attached-label extents |
| **Collision resolve** | `place.overlap_force` (body only) | **visual-bbox iterative resolver** |
| Wire/label | `_classify_and_stub_complex_nets` (score) | (M6) distance-aware re-tune |
| Metrics | `metrics.readability_score`, `verify_connectivity` | accept/reject gate |

## 4. Pipeline (the algorithm, on paper)

```
netlist (SKiDL)
   │
   ▼
[1] SIGNAL GRAPH        exclude power/GND edges (P1); nodes=parts, edges=shared signal nets
   │
   ▼
[2] CLUSTER DETECTION   detect_clusters on the SIGNAL graph -> functional clusters
   │                    (power/clock/comms/memory now separate, not one blob)
   ▼
[3] ANCHOR SELECTION    per cluster: anchor = max pin-count active IC (classify_block_role)
   │                    global anchor = highest-degree anchor across clusters (the MCU/FPGA)
   ▼
[4] LOCAL PLACEMENT     within each cluster: place satellites (crystal/decap/reset) in a
   │                    RING around the cluster anchor, nearest-pin-first; short = wire
   ▼
[5] GLOBAL PLACEMENT    global anchor at sheet centre; other clusters placed around it by
   │                    signal-flow role: inputs/power left+top, outputs right+bottom
   │                    (net_classify.part_depth_map gives BFS flow depth)
   ▼
[6] VISUAL-BBOX COLLISION  every item's keep-out = body + pin-names + ref + value +
   │                    attached labels/power-symbols; iterative resolve (sec 5)
   ▼
[7] COMPACTION         pull clusters inward to kill empty centre while honouring keep-outs
   │
   ▼
[8] POWER SYMBOLS + LABELS  emit (pin-aware orientation already done, M3a); re-run collision
   │
   ▼
[9] ACCEPT / REJECT    verify_connectivity == OK (hard gate, P2) AND readability_score
   │                    improved vs legacy; else fall back to legacy placement
   ▼
.kicad_sch
```

## 5. Visual bounding box + collision resolver (the overlap fix)

**Visual bbox of an item** = union of:
- symbol body polygon bbox (`part.place_bbox`),
- each visible pin's name-text extent (pin length + `len(name) · char_w` along pin dir),
- Reference + Value field text bboxes (`part.lbl_bbox` already includes fields),
- any power-symbol / net-label stub attached to the item (stub len + label text).

**Resolver (deterministic, O(n log n) sweep + local nudge):**
```
build keep-out rects for all items
sort by area desc (place big anchors first)
for each item in order:
    while item.rect intersects any already-placed rect:
        push item along the vector away from the deepest overlap centroid,
        snapped to grid (IPC-3 1.27 mm), by max(overlap depth, 1 grid)
        (cap iterations; if stuck, grow the parent cluster's ring radius)
re-route the moved item's stub (power symbol / label follows its pin)
```
Correctness: only *positions* change; nets are unchanged → `verify_connectivity` must
still pass (P2). If a nudge would break a wire route, the guard reverts and the net drops
to a label (existing all-label safety).

## 6. Feature-flag integration (no risk to existing users)

```python
# place.py (coordinator only)
def place(node, **options):
    if options.get("placement_mode") == "anchor":
        from skidl.schematics import anchor_place
        return anchor_place.place(node, **options)
    ...  # existing legacy path, byte-for-byte unchanged
```
`smart_schematic.build(placement_mode="anchor")` opts in; default stays `"legacy"`.

## 7. Acceptance benchmarks (A/B vs legacy, every build)

| Metric | Tool | Target (anchor vs legacy) |
| --- | --- | --- |
| Connectivity | `verify_connectivity` | **OK** (hard gate — never regress) |
| **Overlap count** | visual-bbox intersection count | **0** (legacy: 12+ pairs <12 mm apart) |
| Sheet span | bbox | ↓ ≥30 % (legacy 357 mm) |
| **Component density** | Σ item-bbox area / sheet-bbox area | ↑ (legacy ~18 %; target ~50 % — kills empty centre) |
| **Nearest functional distance** | dist(anchor, its key satellite, e.g. MCU↔crystal) | ↓ (legacy 320 mm → target <30 mm) |
| Anchor centrality | dist(anchor, sheet centre) | ↓ (legacy 170 mm) |
| Readability | `metrics.readability_score` | ↑ (wire_len, labels, whitespace, crossings) |
| Wired vs label | count | ≥ legacy (short nets stay wires) |

These are computed by a shared `anchor_place.benchmark(sch_path)` helper so legacy and
anchor runs are scored identically for A/B comparison.

A run that fails the connectivity gate or does not beat legacy on readability **auto-falls
back to legacy** — so the flag can be turned on safely and measured.

## 8. Milestone breakdown (implementation)

- **M5 — DONE (partial):** analysis (stages [1]–[3]) validated; coordinate assignment
  implemented as a **flat spiral of all parts** — a SHORTCUT that skipped stages [4]/[5]
  (place cluster INTERNALLY, then place cluster as a unit). Result: compact geometry
  (103×94 mm, MCU centred) but it does **not publish** — a flat spiral packs every part's
  labels into shared space → all-label MISMATCH. Build-level auto-fallback added, so it is
  safe/opt-in today.
- **M6 — HIERARCHICAL CLUSTER PACKING (the real next step; supersedes "rotation-first").**
  Implement stages [4]/[5] properly, as a `Cluster` object:
  ```
  for each cluster:  build -> pack members INTERNALLY (anchor + satellites, tight) ->
                     compute one bounding box  ->  WIRE inside the cluster
  global placer:     move CLUSTERS (not parts) — hub=radial, chain=band
  between clusters:  LABEL (not wire)
  ```
  Why this is the keystone (and why it beats rotation): "wire inside a cluster, label
  between clusters" is *exactly* why hierarchical `@subcircuit` sheets PUBLISH while flat
  sheets MISMATCH. Packing clusters keeps each cluster's labels local, so the all-label
  collision that blocks M5 disappears — cluster packing fixes **both** readability **and**
  the publish blocker at once. (Feedback 2026-07-28, confirmed against the legacy render.)
- **M7 — ROUTER-FRIENDLY cluster placement (the wire-restoration lever).** The wire/label
  DECISION engine already exists and is generic (`WIRE_LABEL_RULES.md`: FUNCTION/AFFINITY →
  wire, DISTANCE/FANOUT/CROSSINGS/CONGESTION → label, power → symbol — matches the user's
  cost-function decision tree 1:1, no device rules). It is NOT applied to the anchor sheet
  because the switchbox router (`route.py`, grid built from part faces) cannot route the
  irregular anchor SPIRAL → the build falls to all-label → every "wire" decision collapses
  to a label. So M7 is NOT a new decision engine — it is:
  1. Pack each cluster in an **aligned row/column** layout (router-friendly), not a spiral,
     so `route.py` produces wired intra-cluster connections;
  2. then the EXISTING wire/label engine automatically wires local nets (rules 2/3) and
     labels cross-cluster/high-fanout nets (rules 6/7). Crystal-adjacency, decap-adjacency
     and "wire-in/label-between" all fall out of (1)+(2).
  Then: visual-bbox collision (0 overlaps), `readability_score` gate, A/B benchmark, and
  finally enable `placement_mode="anchor"` in the MCP. Realistic goal (user, confirmed):
  95–99% clean auto first-draft; manual polish stays optional for presentation, never for
  connectivity.

## 8.1 M11/M12 acceptance conventions (frozen from user review, 2026-07-28)

Professional pin/wire/label conventions the routed output must satisfy (readability
rules, applied generically to every component — regulator, MCU, op-amp, connector):

1. **Pin-exit rule:** the FIRST wire segment leaves in the pin's facing direction
   (left pin → left, right → right, top → up, bottom → down); 90° turns only after.
   Never backward toward the body. (`beautify_wires._flip_l_exits` partial ✅.)
2. **Stub-before-label:** never attach a label directly to a pin; pin → 150–300 mil
   straight stub → label/power symbol. (`normalize_exits` + `POWER_SYMBOL_STUB_LEN_MM`
   partial; extend to net labels.)
3. **Local parts wire, block-local:** a regulator's Cin/Cout, an IC's decaps sit
   adjacent (2–5 mm schematic distance) and connect by WIRE. (Clustering now puts them
   on the right sheet — M10 places them adjacent within it.)
4. **Labels horizontal** where possible; no label/label or label/wire overlap.
5. **Uniform exits:** equal stub lengths on the same side; first bends aligned
   (RULE_ENGINE prio 75/70, router tier).
Status quo gap (measured): cross-sheet wired mode breaks NetTerminal hookup (SDA/SCL
split; see M11 below) — wired multi-sheet is the M11 deliverable, these conventions are
its acceptance bar together with `verify_connectivity == OK`.

## 9. Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| New placement breaks router switchbox assumptions (documented in place.py) | flag-gated; all-label fallback; connectivity gate reverts |
| Anchor mis-detected on a headless/analog board | `classify_block_role` degrades to "Other" → falls back to legacy ordering |
| Collision resolver oscillates | iteration cap + grid snap + cluster-radius growth; deterministic order |
| Regression for existing users | default `legacy`; anchor is strictly opt-in until benchmarks pass |

---

**Sign-off (approved 2026-07-28):**
1. ✅ **Power/ground nets fully excluded** from the placement graph (P1) — `classify_net_role(net) is None` = signal net.
2. ✅ **Auto per-topology** global layout: `hub` topology → radial (anchor centre + rings);
   `chain` topology → left-to-right band. Chosen dynamically from signal-graph shape +
   `part_depth_map` — no fixed choice, works for any circuit.
3. ✅ Acceptance bar: **0 overlaps + ≥30 % tighter span + readability↑, else auto-fallback
   to legacy** (connectivity is always a hard gate).
