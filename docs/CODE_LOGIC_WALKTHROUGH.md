# SKiDL / Anvil CAD — A-to-Z Code Logic Walkthrough

How the code works end-to-end, **starting from where input is read** and following
every major function to the finished, manufacturable board. This is the data-flow view;
the per-file catalog is in `PROJECT_REFERENCE.md`.

Two pipelines run in sequence:
- **Pipeline 1 — Schematic** : a circuit request → `.net` + `.kicad_sch` + `.kicad_pro`
- **Pipeline 2 — PCB** : that `.net` → placed + routed + verified + approved + fab files

## Process boundaries (read this first)

The stages below do **not** all run in one process. Which process a stage runs in
decides **which log a failure appears in** — the single most useful thing to know when
something hangs or crashes:

| Context | What runs there | Where failures show up |
|---|---|---|
| **MCP server** | the `build()` / `create_pcb()` tools, syntax + AST validation, job bookkeeping | the MCP tool's JSON response |
| **build subprocess** | the actual SKiDL script: library discovery, circuit graph, `smart_schematic.build()` | `OUT/<base>.build.log` |
| **pcb_job subprocess** | the whole PCB pipeline (`build_pcb`) — detached, timeout-immune | `OUT/<base>.pcb_build.log` |
| **external processes** | `kicad-cli` (netlist export, DRC, Gerbers) and **Java → FreeRouting** | captured into whichever subprocess called them |

Each stage below is tagged with the process that runs it.

---

# A. Where input is read

Input enters as a Python SKiDL script that is executed in a subprocess. `build()`
(`skidl_mcp_server.py:1257`) is the entry when the assistant drives it; `mode` selects the action.

For `mode="body"` — the common path — the raw `code` string is wrapped into a runnable
script by the `_HARNESS` template (which injects `import anvil_libs`, `from skidl import *`,
`smart_schematic`), then passes through **four validation phases before any expensive work**:

- **Phase 0 — syntax** *[MCP server]* : `compile(script, …)`. In-process, instant. Body line numbers reported relative to the body.
- **Phase 0b — spec** *[MCP server]* : `_static_review(code)` runs `validate_design.py` as **pure AST — it never executes your code**. Any MUST-level violation blocks the build.
- **Phase 1 — electrical dry-run** *[short-lived subprocess]* : `_dry_run(base, code)` runs ERC + netlist with **no schematic routing** to catch wiring errors in seconds. Its netlist is a throwaway precheck (see D2 for the authoritative one).
- **Phase 2 — real build** *[build subprocess]* : `_start_build(base, script)` launches the routed build in the background.

`mode="script"` (complete hierarchical scripts) skips the electrical pre-check but first
strips any `open_anvilcad.*(...)` GUI-launch lines with two regex passes, so the build stays headless.

### The `_start_build` ↔ `build_status` handshake *[MCP server ↔ build subprocess]*

This is the contract that breaks when a build hangs, so it's worth stating exactly:
- `_start_build` writes `OUT/<base>.py`, deletes any stale `.net`/`.kicad_sch`/`.kicad_pro`, opens `OUT/<base>.build.log`, and spawns `subprocess.Popen([python, <base>.py], stdout=log, stderr=STDOUT, stdin=DEVNULL)`. It records `{proc, logfile, started}` in the in-memory `_BUILDS` dict.
- `build(mode="status")` → `build_status()` polls **three signals**: the process return code (`proc.poll()`), the presence of `<base>.net` + `<base>.kicad_sch` on disk, and the log tail. A build still running returns `status:"building"`; a finished one returns `status:"done"` with the file list.
- A hang is bounded: past `_MAX_BUILD_S` the status call **kills the process** rather than waiting forever.

So a Stage 2–4 failure never surfaces in the MCP response directly — you read it from
`OUT/<base>.build.log` via `build(mode="status", include_log=True)`.

---

# B. Library discovery *[build subprocess — runs at `import anvil_libs`]*

`import anvil_libs` (`anvil/anvil_libs.py`) is the first thing the script executes.

- `_find_install_symbols()` scans `%LOCALAPPDATA%\Programs`, `Program Files`, `Program Files (x86)` for a folder whose name contains **"CAD"**, and takes its `share\kicad\symbols` dir.
- Symbols there are `<Lib>.kicad_symdir\` folders (one symbol per file). SKiDL wants one `.kicad_sym` per library, so `_extract_symbols()` + `_flatten_symdir()` + `_sync()` merge them into `~\skidl_symbols`, **rebuilding only stale libraries** (mtime check).
- The module tail sets `KICAD6/7/8/9_SYMBOL_DIR` → the cache, and `KICAD*_FOOTPRINT_DIR` → the install's `footprints\` folder (used directly, no cache).

**Symbols and footprints both come from the Anvil install**; only symbols take the cache detour.

---

# C. Building the circuit in memory *[build subprocess]*

`from skidl import *` then the body builds an in-memory graph on the implicit **default circuit**:

| File | Role | Key names |
|---|---|---|
| `part.py` | instantiate a component from a library symbol | `Part`, `PartUnit` |
| `pin.py` | one terminal, tracks direction/drive | `Pin`, `PhantomPin` |
| `net.py` | electrical connection | `Net`, `NCNet` |
| `netpinlist.py` | the `+=` bulk-connect operator | `NetPinList` |
| `circuit.py` | owns all parts/nets/buses | `Circuit.add_parts/add_nets/add_buses` |
| `node.py` | hierarchy | `Node`, `subcircuit`, `Group` |
| `bus.py` | multi-bit signal | `Bus` |

Nothing is drawn yet — it's a pure connectivity graph.

---

# D. Pipeline 1 — Schematic generation *[build subprocess]*

Entry: `anvil/smart_schematic.py:131` — `build()` orchestrates the following.

### D1. Setup & atomic staging
`set_default_tool(KICAD9)`, resolve `name`, `chdir` into the project folder, then into a
hidden `.build_stage_<name>` dir. **All work happens in the stage**; the finished files are
published into the project in one atomic move at the end, so the app never sees a half-built
schematic.

### D2. ERC + the authoritative intended netlist
- `ERC()` runs `erc.py`'s `dflt_circuit_erc`/`dflt_part_erc`/`dflt_net_erc`.
- `generate_netlist(file_=name+".net")` writes the **intended** netlist into the stage dir.
  **This is the authoritative netlist for verification** — not the Phase-1 dry-run's copy.
  It is named explicitly after the project because SKiDL's default names it after the running
  script, which silently diverges when `build(name=…)` is called from elsewhere (e.g. pytest),
  leaving the verifier with no intended netlist and passing vacuously.

### D3. Structure decision
Count real parts (ignore `#`-prefixed virtuals). `big = n_parts > 50`. Detect existing
hierarchy (`@subcircuit`/`Group`) or block tags (`.group`). With `hierarchy="auto"`,
`anchor_place.auto_hierarchy()` turns detected clusters into real hierarchy Nodes with auto
cross-sheet ports. Sets `flatness`: `1.0` = one sheet with boxed blocks; `0.0` = one sheet per block.

### D4. Placement (force-directed, with anchor option)
`SchNode` (`schematics/sch_node.py`) combines the `Placer` + `Router` mixins.
`SchNode.add_circuit(circuit)` partitions the circuit into sheets and creates `NetTerminal`s
(`net_terminal.py`) for cross-sheet nets. `Placer.place()` (`schematics/place.py`) then:
- `net_classify.py` labels each net (power/ground/clock/reset/USB/RF/decoupling) → a **placement weight**.
- `cluster.py` groups each IC with its satellites (crystal, decaps) → **net-affinity weights** so coupled parts pull together.
- A seed placement (`central`/`random`/`directional_seed_placement`) then `evolve_placement()`/`push_and_pull()` iterate a **spring model**: `net_force_dist` attracts connected pins, `overlap_force` repels overlapping bboxes, `total_part_force` sums them; `adjust_orientations` + `reduce_crossings_by_orientation` flip parts to cut crossings.
- **Fallback:** `anchor_place.place_node()` places clusters around a dominant anchor and returns `False` to hand back to the legacy placer — the fallback is exercised in D7's sweep and is always reported, never silent.

### D5. Routing (switchbox router) — `schematics/route.py`
`Router.route()`: `create_routing_tracks` → `create_terminals` → `global_router` →
`create_switchboxes` → `switchbox_router` routes nets through a grid of `SwitchBox`es between
part faces; `cleanup_wires` + `add_junctions` finalize geometry. The **wire-vs-label decision**
is per net: `net_classify.is_always_wire_net` + distance + congestion decide wire vs. net label;
dense blocks that can't route drop to labels (`stub_internal_nets`) — decided **per block, dynamically**.

### D6. Emit the KiCad files *[calls external kicad-cli later, not here]*
`tools/kicad9/gen_schematic.py` + `sexp_schematic.py` serialize the placed+routed `SchNode`
tree into `<name>.kicad_sch` (+ child sheets) and a minimal `<name>.kicad_pro`.

### D7. Verify → repair → publish (the "first-time-correct" core)

This is the part that makes the output trustworthy. Two things are **not** cosmetic cleanup and
must not be confused with it:
- **`sanitize_sch.fix_project`** runs **after every route attempt, before each verify** — it fixes path-prefixed `lib_id`s so the netlist can even be extracted. It's a **correctness precondition**, not post-route polish.
- **`ipc_check.report`** is **read-only reporting** (IPC-2611/2612 metrics) — it changes nothing.

**The connectivity-aware seed sweep** *[each verify spawns external kicad-cli]* :
`verify_connectivity.verify()` exports a netlist *from the generated schematic* via `kicad-cli`
and compares its **pin-partition** to the intended `.net` (name-independent, so it catches
**shorts** = merged nets and **opens** = split nets; symmetric passive pins are collapsed so
pin-swaps don't false-positive). The build sweeps seeds through **three tiers**, each with a
time budget (`SKIDL_ROUTE_BUDGET_S`, default ~240 s), every tier guaranteed at least one attempt:

1. **Wired** — full wired sheet; accept the first seed that verifies clean.
2. **Partial-wire** — wire whatever routes per block, label the rest.
3. **All-label** — labels never short, so this is the guaranteed-correct floor.

If even all-label mismatches, the build **refuses to publish** (`RuntimeError` — seen in the
stm32 build log). There is also a **legacy-placement fallback**: if the anchor placer's sweep
fails, the whole sweep re-runs with the legacy placer (wired first, then legacy all-label).

**The guarded cleanup chain** — each step is wrapped in `_guarded()`, which **reverts the step
if it breaks connectivity**: `strip_dangling_labels` → `normalize_exits` → `beautify_wires` →
`remove_label_taps` → `grid_snap` → `strip_dangling_labels` (final) → `fix_text_orientation`.

**`add_pwr_flags` runs LAST and UNGUARDED.** This is deliberate and is the one place a pass
runs after the last verify: a `PWR_FLAG` only adds a `#FLG` pin coincident with an existing
power symbol, which cannot change the pin-partition — so it needs no guard. (If you ever make
`add_pwr_flags` do more than that, it must move inside the guard.)

### D8. Atomic publish
The finished stage files move into the project folder in one pass; `.build_stage_*` is discarded.
Result: `<name>.net` + `<name>.kicad_sch` + `<name>.kicad_pro`.

---

# E. Pipeline 2 — PCB generation

MCP `create_pcb(name, layers, route, force)` *[MCP server]* spawns `board/pcb_job.py:run_pcb_job()`
as a **detached background process** *[pcb_job subprocess]* (immune to MCP timeouts). That calls
`board/pipeline.py:30` — `build_pcb()`, the real engine. Failures land in `OUT/<base>.pcb_build.log`.

### E1. Read input netlist + learn setup
- Input read: the `.net` from Pipeline 1.
- **Sidecar** `<base>.board_config.json` (the AI's init choices) is loaded.
- **User rules**: `read_project_rules()` reads whatever the user set in KiCad's Board-Setup/net-class dialogs from `.kicad_pro`. Precedence: **explicit arg > user's `.kicad_pro` > built-in defaults**.
- **Carry-forward**: if a previous `.kicad_pcb` was saved by the *user* (generator ≠ `skidl_board`), its Board Setup / layer stack is preserved, with "latest user action wins" logic reconciling a user-edited board against a newer sidecar.

### E2. Populate the board model
`PyModelBackend.populate()` → `pcb_writer.build_board()`:
- `_read_netlist()` (`pcb_writer.py:160`) parses the `.net` into `comps {ref:{value,footprint}}` and `nets {name:{class,pins}}`.
- Per comp, `footprint_libs.resolve_path("Lib:Name")` finds the `.kicad_mod`; `load_pads_and_courtyard()` parses pads + courtyard in local coordinates.
- Builds a `BoardModel` (`board/model.py`) of `Footprint`/`Pad`/`Net` dataclasses — pure Python.
- Net classes resolve **case-insensitively, user's spelling wins**.

### E3. Mechanical-first
`place/mech.py`: `build_mech_plan(sidecar)` turns the enclosure spec (fixed outline, hole map,
edge connectors, keepouts) into a `MechPlan`; `apply_fixed()` pre-places and **locks** those
before auto-placement. The placer works *inside* them.

### E4. Placement
- **M2 rules placer first** (`place/rules.py:place_board_rules`): blocks ordered left→right by role (`cluster.classify_block_role`), clusters placed as units, decaps/crystals pad-snapped beside their IC, RF keepouts, connectors to edge. Runs two orderings, keeps the lower-HPWL one.
- If it fails or leaves courtyard overlaps → **fall back to M1 simple placer** (`place/simple.py:place_board`): coarse-but-legal. The fallback is **reported, never silent**. Under mechanical constraints there is **no fallback** — violating the enclosure silently would be worse than failing honestly.
- Then grow the outline to requested size, add mounting holes, and **paper-fit** (pick the smallest sheet, center the board on it *before* routing so tracks inherit the shift).

### E5. Routing — three legs, all headless *[external Java + kicad-cli]*
1. `route/dsn_export.py:export_dsn()` — placed board → Specctra `.dsn` (Y-flipped, guard-banded clearances, per-class rules).
2. `route/freerouting.py:route_with_freerouting()` — run the FreeRouting jar; `find_java()` + `find_freerouting_jar()` auto-detect. `.dsn` → `.ses`, streaming per-pass progress, **no window shown**.
3. `route/ses_import.py:import_ses()` — fold the session's wires/vias into `board.tracks`/`.vias`, self-calibrating scale against the placement echo.

**No fallback router.** If `find_java`/`find_freerouting_jar` fail (or routing errors), the board
is saved **PLACED but UNROUTED** and flagged honestly (`build_pcb` reports `routing.ok=false`
with a note to route manually in KiCad or fix the engine and re-run). `pcb_job.py` then runs
post-route `heal_slivers()` (`route/heal.py`): DRC is the oracle — short cross-net fragments in
DRC errors are removed one at a time, keeping only strictly-better results.

### E6. Ground pour + write *[external kicad-cli computes the fill]*
`_ground_net()` picks the most-connected ground net; a `Zone` is added on `B.Cu`.
`write_pcb()` (`pcb_writer.py:207`) serializes the `BoardModel` to `<base>.kicad_pcb`,
**embedding each footprint's raw `.kicad_mod` verbatim** (real silk/fab art). `update_project_net_classes()`
folds net-class rules into `.kicad_pro`. The consumed **setup hash** is recorded to the sidecar
so later ops can detect Board-Setup drift.

---

# F. Verify → Review → Approve → Export (the gates) *[MCP server, shelling to kicad-cli]*

- **`run_drc()`** — kicad-cli DRC vs **your** Board Setup; violation/error/unconnected counts + `clean` flag.
- **`verify_board()`** (`board/verify.py`) — extracts the **as-routed** copper netlist (IPC-D-356) and diffs its pin-partition against the intended `.net`; `ok:false` reports `missing` (opens) + `extra` (shorts).
- **`review_design()`** (`board/review.py`) — synthesizes every gate + **on-board intent checks** no DRC can do (decap distance, track width vs. estimated current via `width_engine.py`, connectors on edge, ground pour) into `<base>_review.md`, ending with an **honesty block** (verified vs. NOT-verified: EMI, SI, PI, thermal, regulatory).
- **`approve_design()`** — records the human's approval, **hash-bound** to `board_sha256` + `setup_hash` + `review_sha256`. The AI can never self-approve; gates re-run fresh; any later change auto-invalidates.
- **`export_manufacturing()`** (`board/manufacture.py`) — Gerbers + drill + P&P (+ optional PDF/STEP) → `<name>_fab.zip`, behind **three hard gates in order:** valid approval → clean DRC → board matches netlist.
- **`package_project()`** — bundles the whole project into `<name>_project.zip`.

---

# G. The three invariants that run through everything

1. **Verify before publish.** Neither a schematic nor a board is written to the project unless `kicad-cli` independently confirms its connectivity matches the intended netlist. A short or open → hard stop.
2. **The user's setup is the source of truth.** Board Setup is re-read live before every op and hash-tracked; the AI only fills gaps and reports the source + reason for each value. Manual edits are never overwritten without `force=True`.
3. **No manufacturing without a human.** Export is gated behind a hash-bound human approval the AI cannot forge or self-grant.

---

# H. Function-call map

```
INPUT
 └─ build(mode="body")                       [MCP server]   skidl_mcp_server.py:1257
     ├─ compile()               phase 0       [MCP server]   syntax
     ├─ _static_review()        phase 0b      [MCP server]   AST spec-validation (no exec)
     ├─ _dry_run()              phase 1       [subprocess]   ERC + throwaway netlist
     └─ _start_build()          phase 2       [MCP server]   writes <base>.py + .build.log, Popen
          │   (poll via build_status: proc.poll() + files-on-disk + log tail)
          └─ <base>.py                        [build subprocess]
              ├─ import anvil_libs            → sets KICAD*_DIR env
              ├─ from skidl import *; parts/nets → Circuit graph
              └─ smart_schematic.build()      anvil/smart_schematic.py:131
                  ├─ ERC(); generate_netlist(name+".net")   ← AUTHORITATIVE intended netlist
                  ├─ SchNode.add_circuit()    schematics/sch_node.py
                  ├─ Placer.place()           schematics/place.py (+cluster, net_classify, anchor_place)
                  ├─ Router.route()           schematics/route.py (wire-vs-label)
                  ├─ gen_schematic/sexp_schematic → .kicad_sch + .kicad_pro
                  ├─ SEED SWEEP: wired → partial-wire → all-label   (each verify → kicad-cli)
                  │    _sanitize() before every verify; refuses to publish if none verifies
                  ├─ guarded cleanup: strip→normalize→beautify→labeltaps→gridsnap→strip→textfix
                  ├─ add_pwr_flags   ← UNGUARDED, LAST (only adds #FLG pin, can't change partition)
                  └─ atomic publish

 └─ create_pcb()                              [MCP server]
     └─ pcb_job.run_pcb_job (detached)        [pcb_job subprocess]   board/pcb_job.py
          └─ build_pcb()                       board/pipeline.py:30
               ├─ _read_netlist()              board/pcb_writer.py:160   ← reads .net
               ├─ resolve footprints           board/footprint_libs.py
               ├─ mech plan + apply_fixed        board/place/mech.py
               ├─ place_board_rules → place_board  board/place/rules.py / simple.py (fallback, reported)
               ├─ export_dsn → freerouting → import_ses   board/route/*   [external Java]
               │    (no fallback router: failure → PLACED-but-UNROUTED)
               ├─ _ground_net + write_pcb        board/pcb_writer.py:207
               └─ heal_slivers                   board/route/heal.py       [external kicad-cli]

 GATES [MCP server → kicad-cli]:
   run_drc → verify_board → review_design → approve_design → export_manufacturing → package_project
```

---

# Appendix — Reverse path (netlist → SKiDL)

Not part of the forward pipeline. `netlist_to_skidl_main.py` (CLI `netlist_to_skidl`) reads an
existing KiCad `.net` and **generates** the equivalent hierarchical SKiDL Python via
`netlist_to_skidl.netlist_to_skidl()` / `HierarchicalConverter`. Use it to import a board that
wasn't authored in SKiDL.
