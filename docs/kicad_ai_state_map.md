# KiCad ↔ AI Communication Map

> **Architecture rule** (the reason this document exists):
> *The saved KiCad project files are the single source of truth. The AI
> sidecar is supporting metadata only. Every AI operation must READ the
> current saved state, ACT through the write API, READ BACK, VERIFY per
> field, and only then report. Manual user edits saved in KiCad are
> detected, diffed and auto-adopted — KiCad is truth. Only destructive
> regeneration over a hand-placed layout asks for confirmation.*

This map lists every state domain: where it lives on disk, how the AI
reads it, how the AI writes it, how a change is verified, and what is
honestly out of scope. It is the acceptance checklist for the
READ → ACT → VERIFY architecture. Status column: ✅ shipped ·
🚫 out of scope (documented future work).

**Shipped 2026-08-11.** Key entry points: `board_setup.read_saved_state` /
`board_edit_status` / `compute_pending_changes` / `adopt_manual_changes` /
`write_state_snapshot` + `diff_saved_vs_snapshot` (READ + adopt),
`board_setup.update_board_setup` with per-field `verified` (ACT),
`conformance.check_config_conformance` (VERIFY),
`rule_discovery.resolve_board_config` (one canonical resolution).
Tests: `tests/unit_tests/test_board_truth.py` +
`tests/unit_tests/test_board_requirements.py`.

## Truth & arbitration model

| Rule | Mechanism |
|---|---|
| Saved files win when the USER saved them | Board fingerprint (`<base>.board_fingerprint.json`, sha256 of the generated `.kicad_pcb`): hash matches → machine-written; mismatch or foreign generator → user-edited. mtime is only the no-fingerprint fallback. |
| Sidecar value ≠ saved value on a machine-written board | A **pending change** (`pending_regeneration`) — applied at the next `create_pcb`, reported until then. |
| Sidecar value ≠ saved value on a user-edited board | Saved file wins; the value is **auto-adopted** into the sidecar with source `board-user`. |
| One arbitration implementation | `board_setup.board_edit_status` + `compute_pending_changes`, consumed by `resolve_board_config`, `get_board_setup`, and `build_pcb`. ✅ |
| Change detection | `setup_hash` (saved files only — approvals bind to the physical board) + `state_hash` (files + sidecar) detect OUT-OF-BAND drift; explicit tool writes (build, update_board_setup) consume both. The not-yet-applied signal is `pending_changes`, the who-changed-what signal is `manual_changes` + `edit_status`. ✅ |

## Board Setup (per field)

| Field | Saved-file home | READ | WRITE | VERIFY | Status |
|---|---|---|---|---|---|
| Copper layers | `.kicad_pcb` layer table | `get_board_setup.consumed.layers` / `read_saved_state` | `update_board_setup(stackup.layers)` → pending; applied by regeneration | `verified` return (pending_regeneration) + post-build conformance (built copper count) | ✅ |
| Thickness | `.kicad_pcb (general thickness)` | same | `update_board_setup(stackup.thickness)` → pending, patched into carried general block at regen | `verified` + conformance | ✅ |
| Copper weight (oz) | sidecar `copper_oz` (KiCad has no field) | `sidecar_meta` | `update_board_setup(stackup.copper_oz)` | plumbed into IPC-2152 width engine (was a dead field) | ✅ |
| Mask clearance | `.kicad_pcb (setup pad_to_mask_clearance)` | `consumed.pad_to_mask_clearance` | `update_board_setup(mask)` — live regex patch, else pending + insert at regen | `verified` (applied vs pending) | ✅ |
| Board outline / size | `.kicad_pcb` Edge.Cuts | `read_saved_state.edge_cuts` extents (**was: read by nothing**) | `update_board_setup(board.{width,height})` → pending; user-drawn outline auto-adopted; **non-rect user shapes carried VERBATIM through regeneration** | conformance (extents vs requested) | ✅ |
| Design-rule minimums | `.kicad_pro design_settings.rules` | `consumed.design_rules` | `update_board_setup(constraints)` — live | `verified` (applied) | ✅ |
| DRC severities | `.kicad_pro rule_severities` | `consumed.drc_severities` (**was: write-only**) | `update_board_setup(drc_severities)` — live | `verified` (applied) | ✅ |
| Net classes / via rules | `.kicad_pro net_settings.classes` | `consumed.net_classes` (live each build) | `update_board_setup(net_classes / via_rules)` — live | `verified`; conformance samples routed widths; rule-less user classes preserved | ✅ |
| Custom DRC rules | `.kicad_dru` | `consumed.custom_dru` (presence) | none (user-authored; honored by kicad-cli as-is) | DRC gate | ✅ |
| Mounting holes | sidecar (+ NPTH pads in built board) | `sidecar_meta` | `update_board_setup(board.mounting_holes)` / init | conformance NPTH count | ✅ |
| Ground pour | sidecar + zones in built board | `sidecar_meta` | `update_board_setup(board.ground_pour)` / init | conformance zone presence | ✅ |
| Manufacturer / IPC class | sidecar (KiCad has no field) | `sidecar_meta` | `update_board_setup(board.*)` / init (questionnaire path) | `verified` (applied to sidecar) | ✅ |
| Per-net currents | sidecar `currents` + `currents_meta` provenance | `sidecar_meta` / `resolve_board_config` | `update_board_setup(board.currents)` / init | width plan consumes; provenance never upgraded (heuristic stays unconfirmed) | ✅ |
| Mechanical spec (enclosure) | sidecar `mechanical` | `sidecar_meta` | init (validated, fail-honest) | mech-fit errors + conformance size | ✅ |

## Other state domains

| Domain | Saved-file home | READ | WRITE | VERIFY | Status |
|---|---|---|---|---|---|
| Schematic (symbols, nets, values) | `.kicad_sch`, `.net` | netlist parse (`pcb_writer._read_netlist`), ERC | schematic build pipeline (regeneration) | ERC + netlist↔board connectivity (`verify_board`) | ✅ |
| PCB content — tracks/vias/zones | `.kicad_pcb` | DRC, `verify_board` (ipcd356), track-width sampler | **regeneration only** (place+route pipeline) | DRC + connectivity + conformance | ✅/🔧 |
| PCB content — component positions | `.kicad_pcb` footprints | fingerprint detects manual layout; positions not individually read | **regeneration only** | manual layout protected by destructive-regeneration gate | ✅ |
| **Component-level incremental ops** ("move J1 to the right edge") | — | — | — | — | 🚫 future work: needs KiCad IPC API (kipy) or s-expr surgical editing + per-component fingerprints, incremental re-route, zone re-fill. Documented, not promised. |
| Manual-change awareness | all saved files | `<base>.state_snapshot.json` diff → `manual_changes` | auto-adopt into sidecar (`board-user`) | reported in `create_pcb` / `get_board_setup` | ✅ |
| Requirements intake | sidecar `requirements_confirmed/_asked` | `create_pcb` gate + questionnaire (`board/requirements.py`) | `initialize_pcb_project` (submit path) | resolved_configuration + soft 'Proceed?' | ✅ |
| Engine overrides (escalation) | sidecar `engine_overrides` + `_sources` | `get_board_setup` / resolver (`engine-override` status — not re-asked) | written by the engine only, with was/now/reason | surfaced in build result as `overridden_user_values` | ✅ |
| DRC / ERC state | kicad-cli reports | `run_drc` | — | gate in `pcb_job` | ✅ |
| Manufacturing config / export | Gerber/drill/P&P outputs | `export_manufacturing` (approval-gated) | — | hash-bound human approval (`approve_design`) | ✅ |
| Config conformance (built == configured) | built `.kicad_pcb` vs resolved config | `conformance.check_config_conformance` | — | layers / size / NPTH / widths / pour; mismatch without recorded override = hard fail | ✅ |

*(Function citations are finalized against the shipped code in the last
phase of the milestone — this file doubles as the acceptance checklist.)*
