# SKiDL / Anvil CAD — Project Reference

A per-file guide to what every module does, its key functions, and where it is used.
Covers the base SKiDL library plus the two new subsystems built in this branch:
the **Anvil schematic front-end** (`src/skidl/anvil/`) and the **AI PCB engine**
(`src/skidl/board/`), and the **MCP server** (`skidl_mcp_server.py`) that drives them.

> Generated 2026-08-01 from a full read of the source. Function lists are grounded
> in the actual code, not invented.

---

## 0. The big picture

SKiDL turns Python circuit descriptions into KiCad netlists, schematics, and PCBs.
On top of the base library, this branch adds an **AI PCB Engineer** pipeline exposed
through 15 MCP tools:

```
text prompt / image / datasheet
        │
        ▼
  parts()      ── ground every part on the real Anvil/KiCad libraries
        │
        ▼
  build()      ── SKiDL script → ERC → .net → .kicad_sch → .kicad_pro   (anvil/smart_schematic.py)
        │
        ▼
  initialize_pcb_project() ── LEARN existing setup → CHOOSE rules → WRITE .kicad_pro + sidecar
        │
        ▼
  create_pcb() ── populate → place → invisible FreeRouting → GND pour → .kicad_pcb   (board/pipeline.py)
        │
        ▼
  run_drc() + verify_board() ── DRC vs Board Setup + board↔netlist connectivity proof
        │
        ▼
  review_design() ── synthesize every gate + design-intent checks → <name>_review.md
        │
        ▼
  approve_design() ── HARD GATE: hash-bound human approval (AI can never self-approve)
        │
        ▼
  export_manufacturing() + package_project() ── Gerbers/drill/P&P → <name>_fab.zip → project.zip
```

Two hard safety rules run through the whole thing: **a schematic/board is never
published unless kicad-cli independently verifies its connectivity**, and
**manufacturing never proceeds without a valid, hash-bound human approval.**

---

## 1. MCP Tools (`skidl_mcp_server.py`)

The server (`FastMCP("anvilcad")`) registers exactly **15** `@server.tool()` functions.
`parts(action=…)` and `build(mode=…)` are dispatchers wrapping internal helpers
(search/describe/add/status/bom/open) that are not themselves tools.

| # | Tool | Phase | What it does |
|---|------|-------|--------------|
| 1 | `diagnostics()` | setup | Health check: confirms this repo's SKiDL is active; reports skidl path, `KICAD9_SYMBOL_DIR`, kicad-cli/Java/FreeRouting detection. Read-only. |
| 2 | `parts(action, items, name, pins, …)` | schematic | Library tool. `search` = batch-check/search the libs (auto-broadens; `total_matches:0` = missing); `describe` = exact `Part(...)` line + every pin + datasheet; `add` = create a missing symbol; `add_footprint` = install/generate a `.kicad_mod`. |
| 3 | `build(name, code, mode, …)` | schematic | Schematic lifecycle. `rules` = mandatory workflow + template; `body` = pre-check a nets/parts snippet then async build; `script` = full `@subcircuit` build; `status` = poll build; `bom` = BOM CSV; `open` = open project. Produces `.net`+`.kicad_sch`+`.kicad_pro`. |
| 4 | `create_pcb(name, layers, route, force)` | PCB | Netlist → placed+routed `.kicad_pcb`. Async: footprints → rule placement → headless FreeRouting → net classes → DRC gate → board-vs-netlist verify. **Gate:** won't overwrite a manually-edited board unless `force=True`. |
| 5 | `analyze_pcb_environment(name)` | PCB step-zero | Read-only discovery of the installed app, kicad-cli capabilities, library roots, routing engines, and the project's existing setup. Returns a `usage_map`; changes nothing. |
| 6 | `get_board_setup(name)` | PCB config | Reads the project's *current* Board Setup live: `raw` / `consumed` / `preserved` + `setup_hash` + `board_setup_changed_since_last_build`. |
| 7 | `update_board_setup(name, stackup, constraints, net_classes, via_rules, mask, drc_severities)` | PCB config | Applies only the passed setup sections; everything else preserved verbatim. Re-stamps the fingerprint; instructs re-running `create_pcb`. |
| 8 | `initialize_pcb_project(name, layers, …, ipc_class, mechanical, currents)` | PCB setup | Learn-first setup: discovers existing config, fills gaps from a manufacturer profile, auto-assigns net classes (power→wider, USB), applies mechanical spec, derives IPC clearances. Every setting carries source + reason. |
| 9 | `assign_footprints(name, assignments)` | PCB | Assigns real footprints (`ref → "Lib:Name"`) to parts lacking one. Verifies each exists; patches **both** the `.net` and the `Part(...)` line in the source `.py`. |
| 10 | `run_drc(name)` | PCB verify | Runs kicad-cli DRC on the `.kicad_pcb`; returns violation/error/unconnected counts + `clean` flag + `board_setup_changed` warning. |
| 11 | `verify_board(name)` | PCB verify | Board↔netlist proof: extracts as-routed copper netlist (IPC-D-356) and compares pin-partition to the intended `.net`. `ok:false` reports `missing` (opens) and `extra` (shorts). |
| 12 | `review_design(name)` | review | Phase-6 review: synthesizes every gate + on-board intent checks (decap distance, track width vs current, connectors on edge, ground pour) into `<name>_review.md`, ending with an honesty block. **Precondition for approval.** |
| 13 | `approve_design(name, approved_by, note)` | approval | Records the human's approval, **hash-bound** to the exact board + setup. AI must never self-approve; gates re-run fresh; any later change auto-invalidates. |
| 14 | `export_manufacturing(name, formats)` | export | Gerbers + drill + pick-and-place (opt. pdf/step) → `<name>_fab.zip`. **Hard gates in order:** valid approval, clean DRC, board matches netlist. |
| 15 | `package_project(name)` | delivery | Bundles the whole project (schematic, sheets, pcb, net, BOM, DRC report, fab zip, review, approval) into `<name>_project.zip`. |

---

## 2. Anvil schematic front-end (`src/skidl/anvil/`)

The one-call schematic generator plus its cleanup/verification helpers. Most helpers
are also runnable as standalone CLIs.

### `smart_schematic.py`  *(the public API, ~40 KB)*
**Concept:** `build()` turns whatever is in the default SKiDL circuit into ERC + `.net`
+ `.kicad_sch` (+ per-block sheets) + `.kicad_pro`, choosing wires-vs-labels dynamically,
sweeping placement seeds for a connectivity-clean route, and running a guarded cleanup chain.
- `class block(name)` — context manager tagging parts with `.group`, drawing a labeled box.
- `build(name, title, auto_stub_fanout, auto_stub_fallback, run_erc, netlist, hierarchy, **overrides)` — the full pipeline; returns `(sch_path, pro_path)`.
- `_assert_skidl_patched()` — warns if the route.py per-block fallback patch is missing.
- `_write_kicad_pro(sch_path)` — writes a minimal project file if none exists.
- Internal steps: `_clean_sheets`, `_sanitize`, `_repair_local_nets`, `_guarded`, `_do_strip/_normalize/_beautify/_labeltaps/_gridsnap/_textfix/_pwrflags`.

**Used by:** `circuit_template.py`, every `mcp_circuits/*.py`, and the MCP server's `build()`.

### `anvil_libs.py`
**Concept:** Auto-discovers the installed Anvil app's KiCad symbol/footprint libraries and
sets `KICAD6/7/8/9_SYMBOL_DIR`/`_FOOTPRINT_DIR`. Must be imported **before** `skidl`.
- `_find_install_symbols()`, `_sync(src)`, `_flatten_symdir(...)`, `_extract_symbols(txt)`.
**Used by:** imported first in nearly every generated circuit script.

### `add_pwr_flags.py`
**Concept:** Adds `PWR_FLAG` symbols to undriven power rails so ERC stops erroring.
- `add(name, netlist=None)`, `_nets_needing_flag(...)`, `_flag_instance(...)`.
**Used by:** `smart_schematic.build()` (last step); also a CLI.

### `beautify_wires.py`
**Concept:** Connectivity-preserving cosmetic cleanup — squares diagonals to Manhattan,
merges collinear segments, drops zero-length wires. Fail-safe.
- `beautify(sch_path)`, `_wire_spans(text)`, `_flip_l_exits(...)`.
**Used by:** `smart_schematic.build()` (guarded); CLI.

### `check_parts.py`
**Concept:** Reports which confirmed parts ARE / AREN'T in the library. Read-only.
- `check(queries)`, `_pin_count(lib, name)`.
**Used by:** CLI in the recommend→confirm→library-check workflow.

### `find_parts.py`
**Concept:** Windows-safe part search + pin inspector (the built-in one imports Unix `readline`).
- `search(terms)`, `show(lib, part_name)`.
**Used by:** CLI; referenced by `circuit_template.py`.

### `verify_connectivity.py`
**Concept:** Proves a `.kicad_sch` has the intended connectivity by exporting its netlist via
kicad-cli and comparing the pin-partition to the intended `.net` — name-independent, so
shorts (merged nets) and opens (split nets) are caught. The in-loop safety check.
- `verify(name, intended_net=None)`, `export_from_schematic(name)`, `_partition(...)`, `_symmetric_refs()`.
**Used by:** central to `smart_schematic.build()`; its `KICAD_CLI` is reused by `ipc_check`, `strip_dangling_labels`. Has a unit test.

### `ipc_check.py`
**Concept:** Read-only IPC-2611/2612 compliance reporter (diagonals, junctions, power symbols,
title block, off-grid/dangling, ERC error list). Never modifies the sheet.
- `check(name, kicad_cli)`, `report(name, kicad_cli)`, `_erc_errors(report_text)`.
**Used by:** `smart_schematic.build()` (per sheet); CLI.

### `normalize_exits.py`
**Concept:** Lengthens too-short pin-exit stubs by sliding the downstream wire chain outward.
Connectivity-preserving, fail-safe.
- `normalize(sch_path, min_exit_mm=2.54)`, `_movable_anchors(text)`.
**Used by:** `smart_schematic.build()` (guarded); CLI.

### `remove_label_taps.py`
**Concept:** Removes junction dots that exist only to hang a label via a short tap wire.
- `remove(sch_path)`, `_pin_points(text)`.
**Used by:** `smart_schematic.build()` (guarded); CLI.

### `strip_dangling_labels.py`
**Concept:** Removes geometrically-dangling labels that ERC flags, without changing connectivity.
- `strip(sch)`, `erc_dangling(sch)`.
**Used by:** `smart_schematic.build()` (guarded); CLI; has a unit test.

### `grid_snap.py`
**Concept:** Snaps only connection coordinates to the 1.27 mm (50 mil) grid; never touches
symbol-internal graphics. Fail-safe.
- `snap(sch_path)`, `_snap(v)`, `_snap_file(sch_path)`.
**Used by:** `smart_schematic.build()` (guarded); CLI.

### `fix_text_orientation.py`
**Concept:** Normalizes property-text angles so nothing renders upside-down (180→0, 270→90).
- `fix(sch_path)`.
**Used by:** `smart_schematic.build()` (guarded); CLI.

### `sanitize_sch.py`
**Concept:** Fixes malformed `lib_id`s where SKiDL wrote a full Windows path instead of the nickname.
- `fix_file(path)`, `fix_project(name)`, `_basename(match)`.
**Used by:** `smart_schematic.build()` (after every route attempt); CLI.

### `open_anvilcad.py`
**Concept:** Opens a design in the Anvil app, auto-detecting the install on any machine.
Exposes `_find_bin()`, the shared install-detector reused across the repo.
- `open_project(name)`, `open_schematic(name)`, `open_pcb(name)`, `_find_bin()`.
**Used by:** generated scripts; `_find_bin` reused by `verify_connectivity`, `ipc_check`, `board/adapter/pymodel`, `board/rule_discovery`.

### `circuit_template.py`
**Concept:** Copy-and-fill starter showing the canonical circuit-script structure. Not imported.

### `__init__.py`
**Concept:** Puts the subpackage dir on `sys.path` so helpers resolve both as `skidl.anvil.*` and as standalone CLIs.

---

## 3. AI PCB engine (`src/skidl/board/`)

Pure-python board pipeline; shells to `kicad-cli` only for DRC/exports and to the
FreeRouting jar for routing.

### Core

- **`model.py`** — The KiCad-type-free in-memory board (mm/deg). Dataclasses `Pad`, `Footprint` (`pad_world_xy`, `courtyard_world_bbox`), `Net`, `Track`, `Via`, `Zone`, `KeepoutArea`, and `BoardModel` (`footprint(ref)`, `get_or_add_net(...)`). Used by nearly every board module.
- **`pipeline.py`** — One-call driver: `build_pcb(...)` → populate → place → paper fit → route (DSN→FreeRouting→SES) → GND pour → write. Helpers `_ground_net`, `_add_mounting_holes`.
- **`pcb_job.py`** — Runs the whole build as one background process immune to MCP timeouts (multi-attempt, DRC, heal, verify). `run_pcb_job(...)`, `_route_budget`, `_drc`. Runnable as `python -m skidl.board.pcb_job`.
- **`pcb_writer.py`** — Stage [A]: parses `.net`+footprints into a `BoardModel` and serializes a `.kicad_pcb`; folds net-class rules into `.kicad_pro`. `build_board(...)`, `write_pcb(...)`, `read_project_rules(...)`, `update_project_net_classes(...)`, `canonical_class_entry(...)`.
- **`board_setup.py`** — Reads the user's *live* Board Setup schema-free (raw/consumed/preserved + hash). `get_board_setup(...)`, `update_board_setup(...)`, `compute_setup_hash(...)`, `sexp_to_data(...)`.
- **`rule_discovery.py`** — "Learn first": discovers app, version, capabilities, and every rule the user already set. `discover_rules(...)`, `resolve(setting, …)` (the first-hit-wins ladder), `discover_application(kicad_cli)`, `read_board_setup(...)`.
- **`project_init.py`** — `initialize_pcb_project`'s engine: LEARN→CHOOSE→WRITE→REPORT, never overwriting user values. `initialize_project(...)`, `_auto_net_classes(...)`, `_has_fine_pitch_parts(...)`, `_write_project(...)`.
- **`profiles.py`** — Bottom rung of the ladder: manufacturer capability profiles + IPC class overlays + mounting-hole IDs. `PROFILES`, `MOUNTING_HOLES`, `IPC_CLASSES`, `get_profile(...)`, `derive_ipc_rules(...)`.
- **`width_engine.py`** — IPC-2152 advisory trace-width planning. `estimate_net_current(...)`, `required_width(...)`, `net_width_plan(...)`, `bucket_classes(...)`.
- **`review.py`** — Phase 6/8 human-review gate + engineering report. `review_design(...)`, `approve_design(...)` (hash-bound), `check_approval(...)`, plus gate builders `_schematic_gates`, `_board_gates`, `_intent_checks`, `_thermal_advisory`.
- **`verify.py`** — Stage [D]: board↔netlist proof via IPC-D-356 partition diff. `verify_board(...)`, `board_partition(...)`, `netlist_partition(...)`.
- **`manufacture.py`** — Stage [E]: kicad-cli Gerbers/drill/P&P (opt. pdf/step) → `<name>_fab.zip`. `export_manufacturing(...)`.
- **`footprint_libs.py`** — Resolves `Lib:Name` → `.kicad_mod`, parses pads + courtyard. `resolve_path(...)`, `load_pads_and_courtyard(...)`, `load_footprint_source(...)`.
- **`footprint_create.py`** — Creates footprints the libs lacked. `install_footprint(...)`, `generate_footprint(...)`, `grid_pads(...)`.
- **`sexp.py`** — Minimal read-only S-expression parser for `.kicad_mod`/`.net`/`.ses`. `read(...)`, `find_all/find_one/atom_at`.
- **`__init__.py`** — Re-exports the core model dataclasses.

### place/

- **`simple.py`** — Stage [B], M1 coarse-but-legal placer (connectors→edge, anchor center, decaps beside ICs, ring rows, de-overlap, grid snap). `place_board(board)`, `count_courtyard_overlaps(...)`, `_build_views(...)`. The automatic fallback for the rules placer.
- **`rules.py`** — M2 rule-based placer: blocks ordered left→right by role, clusters as units, pad-aware decap/crystal snap, RF keepouts; keeps the lower-HPWL of two orderings. `place_board_rules(board, mech)`, `_place_block(...)`, `_snap_beside(...)`.
- **`mech.py`** — Mechanical-first support: `MechPlan` from the sidecar, pre-place + lock holes/edge-connectors/keepouts. `build_mech_plan(...)`, `apply_fixed(...)`, `make_mounting_hole(...)`.
- **`metrics.py`** — Report-only placement quality: `weighted_hpwl(board)`, `utilization(board)`, `congestion_map(...)`.
- **`global_opt.py`** — Deterministic spring-model block placement. `optimize_blocks(...)`, `block_bbox(...)`. Standalone (not wired into the current path).

### route/

- **`dsn_export.py`** — Stage [C] leg 1: `BoardModel` → Specctra `.dsn` (Y-flipped, guard-banded). `export_dsn(board, out_path)`.
- **`freerouting.py`** — Stage [C] leg 2: run the FreeRouting jar headless (`.dsn`→`.ses`). `route_with_freerouting(...)`, `find_java()`, `find_freerouting_jar(s)()`.
- **`ses_import.py`** — Stage [C] leg 3: fold `.ses` wires/vias back into the board, self-calibrating the coordinate scale. `import_ses(board, ses_path)`, `_calibrate_scale(...)`.
- **`heal.py`** — Post-route sliver healing gated by DRC (removes short cross-net fragments, keeps only strictly-better results). `heal_slivers(...)`.

### adapter/

- **`base.py`** — `BoardBackend(ABC)`: the single seam between AI reasoning and how a board is read/written/checked (so a future pcbnew backend is a one-line swap).
- **`pymodel.py`** — `PyModelBackend`: the v1 pure-python backend (dataclasses are truth; shells to kicad-cli only for DRC). Routing methods delegate to their M1 owners.

---

## 4. Schematic placement/routing engine (`src/skidl/schematics/`)

The force-directed placer and switchbox router. `place.py`, `route.py`, `sch_node.py`
are modified core files; `cluster.py`, `net_classify.py`, `metrics.py`, `anchor_place.py`
are **new** modules.

### `sch_node.py`  *(core, modified)*
**Concept:** `SchNode(Placer, Router)` — the hierarchical sheet node; builds the sheet tree
from a circuit and creates cross-sheet net terminals.
- `add_circuit(circuit)`, `add_part(...)`, `add_terminal(net)`, `get_internal/boundary_nets`, `calc_bbox`, `flatten(...)`.
**Used by:** all tools' `gen_schematic.py`; `anvil/smart_schematic.py`.

### `place.py`  *(core, modified)*
**Concept:** Force-directed part placement — bboxes, orientations, net/overlap forces, block
layout, and the `Placer` mixin; now optionally delegates to the new anchor placer and consumes
cluster affinity weights.
- `Placer` mixin (`group_parts`, `place_connected_parts`, `place_blocks`, `place(node, tool, **options)`), `PlacementFailure`, force functions (`net_force_dist`, `overlap_force`, `total_part_force`), `evolve_placement(...)`, `layout_blocks_by_role(...)`.
**Used by:** `sch_node.py`; `PlacementFailure` imported by all `gen_schematic.py`. Calls `anchor_place`.

### `route.py`  *(core, modified)*
**Concept:** Switchbox/global router — tracks, faces, terminals, switchboxes; the `Router` mixin
and `SwitchBox`.
- `Router` mixin (`create_routing_tracks`, `global_router`, `switchbox_router`, `cleanup_wires`, `add_junctions`, `stub_internal_nets`, `route(node, tool, **options)`), `SwitchBox`, `Face`, `Terminal`, `RoutingFailure`.
**Used by:** `sch_node.py`; `SwitchBox` used by `anvil/smart_schematic.py`.

### `anchor_place.py`  *(NEW)*
**Concept:** Anchor-centric placement — signal-only graph → functional clusters → dominant
"anchor" (MCU/FPGA) → pack clusters as units; also builds real hierarchy and scores sheets.
- `place_node(node, **options)` (main entry; returns False to fall back to legacy), `detect_signal_clusters(...)`, `cluster_anchor/global_anchor`, `classify_topology(...)`, `auto_hierarchy(...)`, `benchmark(sch_path)`.
**Used by:** `place.py` (fallback wiring); `anvil/smart_schematic.py`.

### `cluster.py`  *(NEW)*
**Concept:** Functional clustering + block-role classification — groups an IC with its satellites
(crystal, decaps, headers) to bias placement forces, and classifies blocks for left-to-right order.
- `detect_clusters(parts, max_hops=2)`, `find_anchor_parts(...)`, `compute_net_affinity_weights(...)`, `classify_block_role(...)`, `order_blocks_by_role(...)`, `find_decap_affinities(...)`.
**Used by:** `place.py`, `net_classify.py`, `anchor_place.py`, `board/review.py`, `board/place/rules.py`, `board/place/simple.py`, all tools' `gen_schematic.py`.

### `net_classify.py`  *(NEW)*
**Concept:** Pure Net/Part role classification — power/ground detection, functional categories
(clock/reset/USB/RF/decoupling), placement weights, wire-vs-label and local-vs-global decisions,
signal-flow depth.
- `classify_net_role(net)`, `classify_net_function(net)`, `placement_weight(net)`, `is_always_wire_net(net)`, `classify_label_scope(net)`, `is_cyclic_bus_net(net)`, `part_depth_map(parts)`.
**Used by:** widely — `cluster`, `anchor_place`, `place`, all `gen_schematic.py`, and most of `board/`.

### `metrics.py`  *(NEW)*
**Concept:** Objective, unit-testable readability metrics (wire length, density, whitespace,
crossings) for regression-tracking layout quality. Operates on plain data.
- `total_wire_length(wires)`, `label_count(nets)`, `component_density(...)`, `whitespace_ratio(...)`, `readability_score(...)`.
**Used by:** standalone diagnostic API (no `src` imports yet). Distinct from `board/place/metrics.py`.

### `net_terminal.py`  *(core)*
**Concept:** `NetTerminal` — a one-pin `Part` that attaches a label to a net crossing sheet boundaries.
**Used by:** `place.py`, `route.py`, `sch_node.py`, all tool generators.

### `debug_draw.py`  *(core)*
**Concept:** PyGame-based visual debugging of placement/routing (lazy pygame import).
- `draw_placement(...)`, `draw_routing(...)`, `draw_part/draw_net/draw_force`.
**Used by:** `route.py`, `place.py` (debug only).

---

## 5. Core base library (`src/skidl/`)

The foundational abstractions (mostly upstream SKiDL).

| File | Core abstraction | Key names |
|------|------------------|-----------|
| `circuit.py` | `Circuit` = central container of all parts/nets/buses; drives ERC + output. | `add_parts/nets/buses`, `ERC`, `generate_netlist/pcb/xml/schematic/svg/graph`, `reset` |
| `net.py` | `Net` = named electrical connection between pins. | `Net`, `NCNet` (no-connect) |
| `part.py` | `Part` = instantiated component with pins/attrs/footprint. | `Part`, `PartUnit`, `PinNumberSearch`, `PinNameSearch` |
| `pin.py` | `Pin` = one connection point with an electrical function. | `Pin`, `PhantomPin`, `pin_drives`, `pin_types` |
| `bus.py` | `Bus` = ordered collection of related nets. | `Bus` |
| `erc.py` | Default electrical-rule checks over the graph. | `dflt_circuit_erc`, `dflt_part_erc`, `dflt_net_erc` |
| `design_class.py` | Part/Net classification metadata. | `DesignClass`, `PartClass`, `NetClass`, `PartClasses`, `NetClasses` |
| `interface.py` | `Interface` = named bundle of nets/buses/pins. | `Interface` (dict subclass) |
| `geometry.py` | 2D primitives/transforms for placement. | `Tx`, `Point`, `Vector`, `BBox`, `Segment`, `to_mils`, `to_mms` |
| `netpinlist.py` | `NetPinList` = list supporting bulk series/parallel connect. | `NetPinList` |
| `netlist_to_skidl.py` | Reverse: KiCad netlist → hierarchical SKiDL Python. | `netlist_to_skidl`, `HierarchicalConverter`, `*Sexp` nodes |
| `schlib.py` | `SchLib` = loaded symbol library yielding parts. | `SchLib`, `load_backup_lib` |
| `skidlbaseobj.py` | Shared base for Circuit/Part/Net/Bus/Pin/Interface. | `SkidlBaseObject`, `OK/WARNING/ERROR` |
| `alias.py` | Alternative names on objects. | `Alias` |
| `network.py` | Series/parallel two-pin arrangements via `&`/`|`. | `Network`, `tee` |
| `node.py` | `Node` = hierarchy-tree node grouping parts/sub-nodes. | `Node`, `subcircuit`/`SubCircuit`, `Group`, `HIER_SEP` |
| `note.py` | Free-text annotations. | `Note` |
| `part_query.py` | Part/footprint search + display. | `search_parts`, `show_part`, `search_footprints`, `PartSearchDB`, `FootprintCache` |
| `utilities.py` | Helpers used throughout. | `export_to_all`, `flatten`, `expand_buses`, `get_unique_name`, `consistent_hash`, `TriggerDict` |
| `logger.py` | Runtime + ERC logging with error/warning counts. | `active_logger`, `rt_logger`, `erc_logger`, `ActiveLogger` |
| `mixins.py` | Reusable pin-management mixin. | `PinMixin` |
| `skidl.py` | Module-level API over the implicit default circuit. | `ERC`, `generate_*`, `get/set_default_tool`, `config`, `NC`, `POWER`, `KICAD` |
| `__init__.py` | Public namespace + version; tool constants. | re-exports `Part/Net/Bus/Pin/Circuit/Interface`, `KICAD5..9`, `SPICE` |

---

## 6. tools/ backend (KiCad 5–9)

`src/skidl/tools/` holds the per-ECAD-tool backends, auto-discovered by `tools/__init__.py`
(each subpackage advertises a `lib_suffix` to register itself).

- **`kicad5/` … `kicad9/`** — one folder per KiCad major version, same module pattern each:
  - `gen_netlist.py`, `gen_pcb.py`, `gen_xml.py`, `gen_svg.py`, `gen_schematic.py` — output generators.
  - `sexp_schematic.py` — S-expression schematic writer (kicad6–9; kicad5 uses `draw_objs.py` for its older format).
  - `lib.py` — load/parse that version's symbol libraries into SKiDL parts.
  - `bboxes.py` — symbol bounding boxes for placement. `constants.py` — version constants.
  - Default tool is **KiCad 9**.
- **`tools/skidl/libs/`** — ~220 generated `*_sklib.py` symbol libraries (SKiDL's native format), one Python module per KiCad symbol library. Bulk generated asset, not documented individually.
- **`tools/inject_labels.py`** — post-processes a generated KiCad 8/9 `.kicad_sch`, reading the companion `.net` to inject `(global_label …)` at each connected pin (handles rotation/mirror/multi-unit).
- **`tools/spice/spice.py`** — SPICE simulation backend for exporting circuits to SPICE.

---

## 7. CLI scripts (`src/skidl/scripts/`)

- **`validate_design.py`** (954 lines) — Static + netlist validator checking a generated `.py`
  (and optionally its `.net`) against `docs/schematic_generation_spec.md`, reporting by rule ID
  (`PWR-2`, `NET-5`, `HIER-5`, `GV-1`, …). The `.py` is analyzed purely with `ast` (never executed),
  so it is safe to call in-process — which is how the MCP server and `board/review.py` use it.
  `DesignValidator(ast.NodeVisitor)`, `validate_source/text`, `validate_netlist`, `golden_verify`.
  Run: `python -m skidl.scripts.validate_design my.py [--netlist my.net] [--strict] [--matrix] [--golden]`.
- **`part_search_cli.py`** (511 lines) — CLI/interactive search over part libraries.
  Console-script `skidl-part-search` (or `python -m …`). `perform_search_and_display`, `interactive_browse`.
- **`netlist_to_skidl_main.py`** (126 lines) — CLI wrapper: KiCad netlist → SKiDL Python.
  Console-script `netlist_to_skidl`. E.g. `netlist_to_skidl -i design.net -o out_dir --overwrite`.

---

## 8. Design docs & specs

- **`docs/schematic_generation_spec.md`** — normative professional design rules (IEEE 315) that every generated design must follow; the companion to `validate_design.py`.
- **`docs/anchor_placer_design.md`** — design paper for the anchor-centric placer (`schematics/anchor_place.py`).
- **`src/skidl/anvil/RULE_ENGINE.md`, `WIRE_LABEL_RULES.md`** — the wire-vs-label and placement rule engines the anvil helpers implement.
