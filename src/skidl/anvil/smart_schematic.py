"""smart_schematic.py -- reusable one-call schematic/PCB front-end for ANY SKiDL circuit.

Produces, from whatever circuit is in the default SKiDL circuit:
  * ERC check
  * <name>.net              (netlist for PCB)
  * <name>.anvil_sch (+ per-@subcircuit sheets)  -- wires where a block can be routed,
                                                     net labels only where a block is too
                                                     dense, decided PER BLOCK dynamically.
  * <name>.anvil_pro        (minimal project so Anvil CAD/KiCad opens it directly)

This relies on the SKiDL route.py per-block fallback patch (Patch A/B). If that patch is
missing (e.g. after a fresh `pip install skidl`), build() emits a RuntimeWarning instead
of silently regressing to the old global labels-only behaviour.

Usage -- from a circuit script named <name>.py, after building the circuit with
@subcircuit blocks:

    from skidl import *
    import smart_schematic
    # ... define nets + @subcircuit blocks + instantiate them ...
    smart_schematic.build()

Env: expects KICAD9_SYMBOL_DIR / KICAD9_FOOTPRINT_DIR to point at the symbol/footprint
libs (see GUIDE.md); no lib-path hack needed.
"""
import inspect
import json
import os
import re
import sys
import warnings

from skidl import ERC, KICAD9, generate_netlist, generate_schematic, set_default_tool

try:
    from skidl.scriptinfo import get_script_name
except Exception:  # pragma: no cover - fallback for older SKiDL
    def get_script_name():
        return os.path.splitext(os.path.basename(sys.argv[0] or "circuit"))[0]

class block:
    """Tag every Part created inside this `with` as one functional block, so the
    schematic gets a labeled BOX drawn around that group on its sheet:

        import smart_schematic
        with smart_schematic.block("POWER SUPPLY"):
            u = Part(...); c = Part(...)

    Works on the normal multi-sheet path (flatness=0.0, correct connectivity): put
    several `with block(...)` groups inside ONE page-level @subcircuit and each becomes
    a boxed section on that page-sheet -- no @subcircuit flattening (which breaks KiCad
    hierarchy traversal). Nested blocks: the innermost tag wins.
    """

    def __init__(self, name):
        self.name = str(name)

    def __enter__(self):
        import builtins
        self._before = {id(p) for p in builtins.default_circuit.parts}
        return self

    def __exit__(self, *exc):
        import builtins
        for p in builtins.default_circuit.parts:
            if id(p) not in self._before and not getattr(p, "group", None):
                try:
                    p.group = self.name
                except Exception:
                    pass
        return False


# Minimal project skeleton -- Anvil CAD/KiCad upgrades it in place on first open.
_PRO_TEMPLATE = {
    "board": {"design_settings": {"defaults": {}, "rules": {}, "track_widths": [],
                                  "via_dimensions": []}, "layer_presets": [], "viewports": []},
    "boards": [],
    "cvpcb": {"equivalence_files": []},
    "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
    "meta": {"filename": "", "version": 1},
    "net_settings": {"classes": [{"name": "Default"}], "meta": {"version": 3}},
    "pcbnew": {"last_paths": {}, "page_layout_descr_file": ""},
    "schematic": {"legacy_lib_dir": "", "legacy_lib_list": [], "meta": {"version": 1}},
    "sheets": [],
    "text_variables": {},
}


def _assert_skidl_patched():
    """Warn (do not monkeypatch) if the route.py per-block fallback patch is absent."""
    ok = False
    try:
        from skidl.schematics.route import SwitchBox
        from skidl.schematics.sch_node import SchNode
        coalesce_src = inspect.getsource(SwitchBox.coalesce)
        ok = (hasattr(SchNode, "stub_internal_nets")
              and "borders fewer than two switchboxes" in coalesce_src)
    except Exception:
        ok = False
    if not ok:
        warnings.warn(
            "smart_schematic: SKiDL route.py is missing the per-block routing-fallback "
            "patch (Patch A/B). Dense blocks may crash or force a global labels-only "
            "schematic. Re-apply the patch in src/skidl/schematics/route.py.",
            RuntimeWarning,
        )


def _write_kicad_pro(sch_path):
    """Write a minimal <name>.anvil_pro next to the schematic (never clobber existing)."""
    name = os.path.splitext(os.path.basename(sch_path))[0]
    root = ""
    try:
        with open(sch_path, encoding="utf-8") as f:
            head = f.read(4000)
        m = re.search(r'\(uuid\s+"?([0-9a-fA-F-]{36})"?\)', head)
        root = m.group(1) if m else ""
    except OSError:
        pass
    pro = json.loads(json.dumps(_PRO_TEMPLATE))  # deep copy
    pro["meta"]["filename"] = name + ".anvil_pro"
    pro["sheets"] = [[root, "Root"]] if root else []
    out = os.path.join(os.path.dirname(sch_path) or ".", name + ".anvil_pro")
    if not os.path.exists(out):
        with open(out, "w", encoding="utf-8") as f:
            json.dump(pro, f, indent=2)
    return out


def _design_gates():
    """Hard design gates run for EVERY build path (MCP body mode, MCP script
    mode, direct library use) -- the single chokepoint no caller can bypass.

    1) FLOATING PINS: every pin must be either connected or explicitly marked
       no-connect (pin += NC). Library-declared no-connect pins (func ==
       NOCONNECT) are exempt: the symbol itself says they connect to nothing.
       A silently floating pin is exactly how boards ship with missing MCU /
       STAT / collector connections, so it stops the build here.
    2) BARE FOOTPRINTS (only when ANVIL_REQUIRE_FOOTPRINTS is set, as the MCP
       pipeline does): every real part must carry a footprint, otherwise the
       netlist is written with silent (footprint "") holes that only surface
       at PCB time. Direct library/test use stays permissive by default.
    """
    import builtins
    from skidl.pin import pin_types

    floating, nofp = [], []
    for part in builtins.default_circuit.parts:
        ref = str(getattr(part, "ref", "") or "")
        if ref.startswith("#"):
            continue
        pins = list(getattr(part, "pins", []))
        if not pins:
            continue
        for pin in pins:
            try:
                if not pin.nets and pin.func != pin_types.NOCONNECT:
                    floating.append("%s[%s] %s" % (ref, pin.num,
                                                   str(pin.name or "").strip()))
            except Exception:
                pass
        if os.environ.get("ANVIL_REQUIRE_FOOTPRINTS"):
            if not str(getattr(part, "footprint", "") or "").strip():
                nofp.append("%s (%s)" % (ref, str(getattr(part, "name", "") or "")))
    errs = []
    if floating:
        errs.append("FLOATING pin(s) -- connect each, or mark it no-connect "
                    "(part[pin] += NC): " + ", ".join(sorted(floating)))
    if nofp:
        errs.append("part(s) with NO footprint -- assign footprint=\"Lib:Name\" "
                    "to each: " + ", ".join(sorted(nofp)))
    if errs:
        raise RuntimeError("smart_schematic design gates FAILED -- " +
                           " | ".join(errs))


def build(name=None, title="SKiDL-Generated Schematic", auto_stub_fanout=None,
          auto_stub_fallback="labels", run_erc=True, netlist=True,
          hierarchy=None, **overrides):
    """Generate ERC + netlist + per-block schematic + project for the default circuit.

    Args:
        name: base file name (defaults to the running script's name).
        auto_stub_fanout: OPTIONAL safety valve -- pin count at/above which a bus-like
            net is pre-labeled before placement. Default None = OFF; wire-vs-label is
            decided by GEOMETRY (distance + congestion) after placement, so a local net
            is wired no matter how many pins it has. Set a finite value only to tame a
            huge shared net that distorts placement.
        auto_stub_fallback: what a *dense* block does if it still can't route ("labels").
        run_erc / netlist: toggle those stages.
        overrides: any extra kwargs forwarded to generate_schematic (e.g. flatness,
                   auto_stub_max_wire_pins, seed).
    Returns (schematic_path, project_path).
    """
    set_default_tool(KICAD9)
    _assert_skidl_patched()
    _design_gates()
    name = name or get_script_name()

    # Route ALL generated files into the project's own folder. This project script
    # already chdir'd into its own directory before calling build() (self-contained
    # project: script + engine helpers + outputs together, not redirected into a
    # separate AnvilCAD Projects tree) -- so just stay put and use that directory.
    _proj = os.getcwd()
    os.makedirs(_proj, exist_ok=True)
    os.chdir(_proj)

    # ---- ATOMIC BUILD STAGING (first-time-correct rule) ----
    # The build rewrites <name>.anvil_sch MANY times (seed sweep + cleanup passes).
    # If the CAD app has the project open -- or opens it mid-build -- it can load a
    # half-done intermediate (e.g. wires without their junction dots), show phantom
    # "pin not connected" ERC errors, and a user Ctrl+S then overwrites the good
    # final file with that broken buffer. So: do ALL the work in a hidden stage
    # dir and publish the finished files into the project in one atomic pass at
    # the end. The on-disk project is never in a half-built state.
    import shutil as _shutil
    # per-project stage dir so concurrent builds of different circuits in the
    # same folder can never rmtree each other's work-in-progress
    _stage = os.path.join(_proj, ".build_stage_" + name)
    _shutil.rmtree(_stage, ignore_errors=True)
    os.makedirs(_stage, exist_ok=True)
    os.chdir(_stage)  # process-per-build: on a crash the stage is simply discarded

    if run_erc:
        ERC()
    if netlist:
        # name the netlist EXPLICITLY after the project: skidl's default uses the
        # running script's name, which silently diverges when build(name=...) is
        # called from elsewhere (e.g. pytest) -- then the verifier finds no
        # intended netlist and passes vacuously.
        generate_netlist(file_=name + ".net")

    opts = dict(tool=KICAD9, top_name=name, title=title, auto_stub=True,
                erc_max_iterations=8)
    if auto_stub_fanout is not None:
        opts["auto_stub_fanout"] = auto_stub_fanout

    # Title-block metadata (revision / company / engineer for professional
    # version tracking). These feed the KiCad title block, NOT generate_schematic,
    # so pull them out of overrides before forwarding the rest.
    _tb_meta = {k: overrides.pop(k) for k in
                ("rev", "company", "engineer", "project", "date")
                if k in overrides}
    if _tb_meta:
        try:
            from skidl.tools.kicad9 import sexp_schematic as _sxp
            _sxp.set_title_block_meta(**_tb_meta)
        except Exception as _e:
            warnings.warn(f"smart_schematic: title-block metadata skipped: {_e}",
                          RuntimeWarning)

    opts.update(overrides)

    # --- the 50-part rule: single-sheet-with-blocks vs multiple sheets ---
    # flatness=1.0 flattens all @subcircuit blocks onto ONE sheet (blocks stay grouped,
    # wires inside each block, labels between) -- like a hand-drawn single-sheet schematic.
    # flatness=0.0 emits ONE hierarchical sheet PER TOP-LEVEL @subcircuit block. So for a big
    # design the SHEET COUNT == the number of top-level blocks: to get readable ~30-40-part
    # pages, the .py must group parts into functional "page" @subcircuits (~30-40 parts each,
    # plain helper fns for repeated units) -- NOT one @subcircuit per small unit (that yields
    # dozens of tiny sheets). Split into sheets only once the design is big (>50 real parts).
    import builtins
    _real_parts = [p for p in builtins.default_circuit.parts
                   if not str(getattr(p, "ref", "") or "").startswith("#")]
    n_parts = len(_real_parts)
    big = n_parts > 50

    # Does the SCRIPT itself provide structure? Two independent signals:
    #   * real SKiDL hierarchy (@subcircuit / Group) -> the root Node has children
    #   * explicit smart_schematic.block()/group tags -> parts carry .group
    _has_hier = bool(getattr(getattr(builtins.default_circuit, "root", None),
                             "children", None))
    _has_groups = any(getattr(p, "group", None) for p in _real_parts)

    # --- M8 AUTO HIERARCHY (opt-in: hierarchy="auto") ---
    # Build REAL hierarchy nodes from detected functional clusters BEFORE any
    # netlist/schematic generation, so the generator emits one hierarchical
    # sheet per cluster with auto-created cross-sheet ports (NetTerminals) --
    # exactly as if the user had written @subcircuit blocks. Explicit hierarchy
    # always wins (the builder refuses to act on a non-flat design).
    if hierarchy == "auto" and not _has_hier:
        try:
            from skidl.schematics import anchor_place
            _made_nodes = anchor_place.auto_hierarchy(builtins.default_circuit)
            if _made_nodes >= 2:
                _has_hier = True
                opts.setdefault("flatness", 0.0)   # one sheet per cluster node
                # M10: anchor-pack each child sheet (each is one cluster with its
                # IC as anchor -> satellites tight around it). The root node (sheet
                # boxes) has no direct parts, so place_node returns False there and
                # the legacy block-diagram row (M9) still arranges the sheet boxes.
                opts.setdefault("placement_mode", "anchor")
                print(f">>> smart_schematic: AUTO-HIERARCHY built {_made_nodes} "
                      "functional sheet(s) from the connectivity graph")
        except Exception as _e:
            warnings.warn(f"smart_schematic: auto-hierarchy skipped: {_e}",
                          RuntimeWarning)

    # --- DYNAMIC functional grouping (auto-blocks) ---
    # A flat script (no @subcircuit blocks, no `with smart_schematic.block(...)`
    # tags) gives the placer nothing to organize around. On a BIG design that is
    # fatal, not just ugly: the router tries to place+route ~100 parts on ONE
    # sheet and THRASHES -- observed thrashing for 45 min with no output. If the
    # script did not group parts itself, derive the blocks FROM THE CONNECTIVITY
    # GRAPH: every anchor IC plus its low-fanout satellites (detect_clusters)
    # becomes one named block. The design then places as tidy boxes and routes in
    # seconds. Fully dynamic, works for any circuit; explicit structure always
    # wins and this only fills in when absent. Small circuits place tighter as one
    # flat cluster, so only auto-group once a sheet is big enough to scatter.
    _auto_group_min = int(os.environ.get("SKIDL_AUTO_GROUP_MIN", "25"))
    if (n_parts > _auto_group_min and not _has_hier and not _has_groups):
        try:
            from skidl.schematics.cluster import detect_clusters
            _made = 0
            for _cl in detect_clusters(_real_parts):
                if len(_cl) < 2:
                    continue
                _anchor = max(_cl, key=lambda p: len(getattr(p, "pins", [])))
                _gname = f"{_anchor.ref} {getattr(_anchor, 'name', '')}".strip()
                for _p in _cl:
                    if not getattr(_p, "group", None):
                        try:
                            _p.group = _gname
                        except Exception:
                            pass
                _made += 1
            if _made:
                _has_groups = True
                print(f">>> smart_schematic: auto-grouped {_made} functional "
                      f"block(s) from the connectivity graph")
        except Exception as _e:
            warnings.warn(f"smart_schematic: auto-grouping skipped: {_e}",
                          RuntimeWarning)

    # --- flatness / sheet decision ---
    # flatness=0.0 -> one hierarchical SHEET per top-level @subcircuit block.
    # flatness=1.0 -> a single sheet; .group tags become boxed sections on it.
    # MULTI-SHEET is only reliable when the SCRIPT built real @subcircuit
    # hierarchy (which emits proper cross-sheet terminals). Auto-derived .group
    # blocks do NOT export a connected multi-sheet netlist (observed unwanted
    # shorts + missing net-groups), so a big FLAT design -- even after
    # auto-grouping -- is rendered as ONE grouped, connectivity-safe sheet rather
    # than split into sheets. Multi-sheet therefore stays opt-in: build real
    # @subcircuit blocks in the script to get one sheet per block.
    _page_mode = big and _has_hier and os.environ.get("SKIDL_PAGE_MODE") == "1"
    try:
        from skidl.schematics import sch_node as _sch_node
        _sch_node.set_page_mode(_page_mode)
    except Exception:
        pass
    if "flatness" not in opts:
        # DEFAULT: a SINGLE page with boxed functional blocks. Within-block
        # connections are wired and only inter-block signals become labels (the
        # professional "wire inside a block, label between blocks" rule, enforced
        # by the inter-block stubbing below). This is preferred over one-sheet-
        # per-block because (a) it avoids the page explosion when a design has many
        # small functions, and (b) it sidesteps the KiCad multi-sheet annotation
        # caveat. Multi-sheet (one page per top-level @subcircuit block) stays
        # available by passing flatness=0.0 explicitly.
        opts["flatness"] = 1.0
        if _has_hier or _has_groups:
            msg = ("SINGLE sheet with boxed functional blocks "
                   "(wire in-block, label cross-block)")
        else:
            msg = "SINGLE sheet"
        print(f">>> smart_schematic: {n_parts} parts -> {msg}")

    # --- hard label cutoff (SKIDL_WIRE_MAX_FANOUT) ---
    # Default 3 (user rule): a net joining up to 3 pins may be drawn as WIRES
    # (the geometry engine still labels it if it is genuinely far/crowded);
    # any net with MORE than 3 pins becomes a named label -- multi-drop nets
    # as wire trees are what tangles a sheet. Set 0 to let geometry decide
    # everything, or another N to move the cutoff.
    # Junction robustness note: the router splits wires at T-points, so the
    # sheet's connectivity never depends on the (junction ...) dot elements --
    # verified: deleting every junction still yields 0 KiCad ERC errors. Dots
    # are kept for IPC-2612 readability only.
    _multisheet = opts.get("flatness", 1.0) == 0.0

    _wire_max = int(os.environ.get("SKIDL_WIRE_MAX_FANOUT", "3"))
    if _wire_max > 0:
        _stubbed = 0
        for _net in builtins.default_circuit.nets:
            try:
                if type(_net).__name__ == "NCNet" or getattr(_net, "stub", False):
                    continue
                if len(_net.pins) > _wire_max:
                    # same flags the guaranteed-correct all-label mode uses, so the
                    # per-block cluster protection cannot wire this net anyway
                    _net._direct_wired = False
                    _net.stub = True
                    for _p in _net.pins:
                        _p.direct_wired = False
                        _p.stub = True
                    _stubbed += 1
            except Exception:
                pass
        if _stubbed:
            print(f">>> smart_schematic: {_stubbed} net(s) with >{_wire_max} pins "
                  f"-> labels (junction-free sheet)")

    # --- inter-block nets -> LABELS, intra-block nets stay WIRES ---
    # The professional rule is "wire the connections INSIDE a functional block,
    # LABEL the signals that travel BETWEEN blocks". The fanout cutoff above does
    # not capture this: a 2-3 pin net whose ends live in two different blocks is
    # still wire-eligible, but the router cannot reliably drag that wire across
    # the block boundary -- it leaves the net BROKEN, and one broken cross-block
    # wire fails the connectivity check and collapses the WHOLE sheet to labels
    # (observed: current-sense R-C-to-ADC and CAN connector-to-transceiver nets).
    # Pre-labeling every net whose pins span >1 block removes those from the wire
    # router, so only genuinely-local connections are wired and the wired result
    # verifies clean. Power rails are skipped (they become power symbols).
    try:
        from skidl.schematics.net_classify import classify_net_role as _cnr
    except Exception:
        _cnr = None

    def _block_key(_part):
        _g = getattr(_part, "group", None)
        if _g:
            return ("g", str(_g))
        try:
            return ("h", tuple(_part.hiertuple))
        except Exception:
            return ("_",)

    # Inter-block/-node nets -> stub. On a single sheet these become LOCAL labels
    # (blocks share the page); on a multi-sheet build the same stub renders as a
    # GLOBAL label (the net's pins span >1 hiertuple, so classify_label_scope
    # picks global_label), which is exactly what connects a signal across sheets.
    if _has_groups or _has_hier:
        _xblock = 0
        for _net in builtins.default_circuit.nets:
            try:
                if type(_net).__name__ == "NCNet" or getattr(_net, "stub", False):
                    continue
                if _cnr is not None and _cnr(_net) is not None:
                    continue  # power/ground rail -> power symbol, not a label
                _keys = set()
                for _p in _net.pins:
                    _pt = getattr(_p, "part", None)
                    if _pt is None:
                        continue
                    if (getattr(_pt, "ref_prefix", "") or "").upper() == "NT":
                        continue
                    if str(getattr(_pt, "ref", "") or "").startswith("#"):
                        continue
                    _keys.add(_block_key(_pt))
                    if len(_keys) > 1:
                        break
                if len(_keys) > 1:
                    _net._direct_wired = False
                    _net.stub = True
                    for _p in _net.pins:
                        _p.direct_wired = False
                        _p.stub = True
                    _xblock += 1
            except Exception:
                pass
        if _xblock:
            print(f">>> smart_schematic: {_xblock} inter-block net(s) -> labels "
                  f"(within-block connections stay wires)")

    # --- dynamic routing: sweep placement seeds so the sheet routes as WIRES ---
    # SKiDL's router is seed-dependent: one seed may fail to route (falling back to a scatter
    # of labels) while another routes the very same circuit cleanly with wires. Try several
    # seeds, keep the first that routes, and only drop to labels if every seed fails.
    import glob as _glob

    def _clean_sheets():
        # remove this project's sheet files so a retry doesn't leave "_1" duplicate sheets
        for _p in _glob.glob(name + ".anvil_sch") + _glob.glob(name + "_*.anvil_sch"):
            try:
                os.remove(_p)
            except OSError:
                pass

    user_seed = opts.pop("seed", None)
    seeds = [user_seed] if user_seed is not None else [0, 1, 2, 3, 5, 8, 13, 21]

    def _sanitize():
        # SKiDL can write a symbol's lib nickname as a full file path; strip those
        # back to "Lib:Part" so Anvil CAD/KiCad (and the netlist extractor) accept it.
        try:
            import sanitize_sch
            return sanitize_sch.fix_project(name)
        except Exception as e:
            warnings.warn(f"smart_schematic: lib_id sanitize skipped: {e}", RuntimeWarning)
            return 0

    def _repair_local_nets(missing_groups):
        """M11 local-net repair. verify_connectivity returns 'missing' pin-groups
        (frozensets of 'REF.PIN'). The wired router occasionally drops a 2-pin
        LOCAL leaf net (LED-R, connector-IC) -- one such failure otherwise sinks
        a whole multi-sheet build to all-label. Map each missing group to its net
        and, IFF the net is LOCAL (all real pins in one hierarchy node), stub it
        so it renders as a within-sheet LABEL (which connects) instead of a
        dropped wire. Returns the count stubbed. Returns 0 (abort) if ANY missing
        net is CROSS-NODE -- that is a real hierarchy problem, not a local-repair
        case, and must fall through to the next tier."""
        nets = list(builtins.default_circuit.nets)
        by_refs = {}
        for _n in nets:
            _r = frozenset(
                str(getattr(getattr(_p, "part", None), "ref", "") or "")
                for _p in getattr(_n, "pins", [])
                if getattr(_p, "part", None) is not None
                and (getattr(_p.part, "ref_prefix", "") or "").upper() != "NT"
                and not str(getattr(_p.part, "ref", "") or "").startswith("#")
            )
            if _r:
                by_refs.setdefault(_r, _n)
        count = 0
        for grp in missing_groups:
            refs = frozenset(str(s).split(".")[0] for s in grp)
            net = by_refs.get(refs)
            if net is None or getattr(net, "stub", False):
                continue
            hts = set()
            for _p in net.pins:
                _pt = getattr(_p, "part", None)
                if _pt is None or (getattr(_pt, "ref_prefix", "") or "").upper() == "NT":
                    continue
                if str(getattr(_pt, "ref", "") or "").startswith("#"):
                    continue
                try:
                    hts.add(tuple(_pt.hiertuple))
                except Exception:
                    pass
            if len(hts) != 1:
                return 0  # a cross-node net is missing -> not repairable here
            net._direct_wired = False
            net.stub = True
            for _p in net.pins:
                _p.direct_wired = False
                _p.stub = True
            count += 1
        return count

    try:
        import verify_connectivity
    except Exception:
        verify_connectivity = None

    # CONNECTIVITY-AWARE seed sweep. The router is seed-dependent, and a seed that
    # merely ROUTES can still draw a wrong connection (an unwanted short / broken
    # wire). So for each seed: route, then verify the drawn netlist == intended,
    # and keep the FIRST seed that BOTH routes AND verifies clean. Only if no seed
    # yields a correct wired sheet do we drop to all-label mode. The functional
    # wire count now comes from the cluster-net PROTECTION in gen_schematic
    # (_classify_and_stub_complex_nets keeps single-cluster local nets as wires),
    # not from picking a lucky seed. (SCHEMATIC_ENGINE_RULES F1 + F4.)
    # TIME BUDGET: the sweep is 8 seeds x 2 tiers = up to 16 full place+route
    # attempts; on a topology that can never route as wires (repeated ladders,
    # high-fanout rails) that thrashes for 30+ minutes before falling back to
    # labels. Budget each tier (env SKIDL_ROUTE_BUDGET_S, default 240 s): every
    # tier always gets at least ONE attempt, but no NEW attempt starts past its
    # budget -- so easy circuits are untouched and hard ones degrade to the next
    # tier in minutes, not half-hours.
    import time as _time
    _budget = float(os.environ.get("SKIDL_ROUTE_BUDGET_S", "240"))

    # Build-level auto-fallback bookkeeping: if placement_mode="anchor" is
    # requested but the anchor layout can't be verified/published, the build
    # transparently retries with the legacy placer so a valid .anvil_sch is
    # ALWAYS produced. See docs/anchor_placer_design.md (P3 + acceptance gate).
    _placer_used = "anchor" if opts.get("placement_mode") == "anchor" else "legacy"

    routed_seed = None
    _t0 = _time.time()
    for _i, sd in enumerate(seeds):
        if _i and _time.time() - _t0 > _budget:
            print(f">>> smart_schematic: wire-route budget exhausted "
                  f"({int(_time.time() - _t0)}s > {int(_budget)}s after {_i} seed(s)) "
                  f"-> trying partial-wire mode")
            break
        _clean_sheets()  # each attempt starts clean -> no leftover _1 duplicate sheets
        try:
            generate_schematic(seed=sd, auto_stub_fallback="raise", **opts)
        except Exception:
            continue  # this seed couldn't route -> try the next
        _sanitize()  # fix lib_id paths before extracting the netlist for verify
        if verify_connectivity is None:
            routed_seed = sd
            print(f">>> smart_schematic: routed with wires (seed={sd}); verify unavailable")
            break
        # Verify, and on a multi-sheet build attempt LOCAL-NET REPAIR (M11): a
        # dropped 2-pin local net is stubbed to a within-sheet label and the seed
        # is re-routed, up to a few rounds. Cross-node misses abort the repair.
        ok = True
        _repaired = 0
        for _round in range(4):
            try:
                ok, _msg, _u, _m = verify_connectivity.verify(name)
            except Exception:
                ok = True
            if ok or not _multisheet or _u:
                break
            _n = _repair_local_nets(_m)
            if _n == 0:
                break  # nothing locally repairable -> next seed / next tier
            _repaired += _n
            _clean_sheets()
            try:
                generate_schematic(seed=sd, auto_stub_fallback="raise", **opts)
            except Exception:
                ok = False
                break
            _sanitize()
        if ok:
            routed_seed = sd
            _rmsg = f" [repaired {_repaired} local net(s) -> labels]" if _repaired else ""
            print(f">>> smart_schematic: routed with wires (seed={sd}); "
                  f"connectivity OK{_rmsg}")
            break
        # connectivity mismatch on this seed -> discard, try the next seed

    if routed_seed is None:
        # MIDDLE TIER: no seed FULLY routed every wire (auto_stub_fallback="raise"
        # abandons a seed the moment one block can't route). Rather than collapse
        # straight to all-label, try a PARTIAL route -- wire whatever routes per
        # block and label ONLY the blocks that can't (auto_stub_fallback="labels").
        # Commit the first seed that verifies clean. This keeps the functional wires
        # that DO route instead of throwing them all away, so a hard-to-route sheet
        # degrades to "mostly wired" rather than "all labels". (Reliability fix for
        # the dense-routing flakiness introduced by cluster-net wire protection.)
        _t1 = _time.time()
        for _i, sd in enumerate(seeds):
            if _i and _time.time() - _t1 > _budget:
                print(f">>> smart_schematic: partial-route budget exhausted "
                      f"({int(_time.time() - _t1)}s > {int(_budget)}s after {_i} "
                      f"seed(s)) -> falling back to all-label mode")
                break
            _clean_sheets()
            try:
                generate_schematic(seed=sd, auto_stub_fallback="labels", **opts)
            except Exception:
                continue
            _sanitize()
            ok = True
            if verify_connectivity is not None:
                try:
                    ok = verify_connectivity.verify(name)[0]
                except Exception:
                    ok = True
            if ok:
                routed_seed = sd
                print(f">>> smart_schematic: partial wire route (seed={sd}); "
                      f"connectivity OK (some dense nets labeled)")
                break

    if routed_seed is None:
        # LAST RESORT: force ALL non-power nets to labels (labels connect by name
        # and can never short), guaranteeing correctness on a sheet nothing routes.
        warnings.warn(
            "smart_schematic: no seed produced a connectivity-clean wired schematic; "
            "using all-label mode for guaranteed-correct connectivity.",
            RuntimeWarning,
        )
        import builtins

        for net in builtins.default_circuit.nets:
            try:
                net._direct_wired = False
                net.stub = True  # forces a label (_stub_explicit); power stays a symbol
                for p in net.pins:
                    p.direct_wired = False
                    p.stub = True
            except Exception:
                pass
        _clean_sheets()
        generate_schematic(auto_stub_fallback=auto_stub_fallback, **opts)
        _sanitize()
        if verify_connectivity is not None:
            _ok, msg2, _u, _m = verify_connectivity.verify(name)
            print(f">>> smart_schematic: all-label mode -> {msg2}")
            if not _ok and opts.get("placement_mode") == "anchor":
                # BUILD-LEVEL AUTO-FALLBACK: the anchor layout didn't verify.
                # Retry the whole route with the LEGACY placer (which is known
                # to publish this design) so the user still gets a valid sheet.
                print(f">>> smart_schematic: ANCHOR placement FAILED ({msg2.split('-- ',1)[-1]}) "
                      "-> falling back to LEGACY placer")
                opts.pop("placement_mode", None)
                _placer_used = "legacy (anchor fell back)"
                routed_seed = None
                # Re-run the wired seed sweep with legacy placement first...
                for sd in seeds:
                    _clean_sheets()
                    try:
                        generate_schematic(seed=sd, auto_stub_fallback="raise", **opts)
                    except Exception:
                        continue
                    _sanitize()
                    try:
                        if verify_connectivity.verify(name)[0]:
                            routed_seed = sd
                            _ok = True
                            print(f">>> smart_schematic: routed with wires (seed={sd}); "
                                  "connectivity OK [legacy fallback]")
                            break
                    except Exception:
                        routed_seed = sd
                        _ok = True
                        break
                if routed_seed is None:
                    # ...else legacy all-label (labels never short -> guaranteed).
                    import builtins as _b
                    for _net in _b.default_circuit.nets:
                        try:
                            _net._direct_wired = False
                            _net.stub = True
                            for _p in _net.pins:
                                _p.direct_wired = False
                                _p.stub = True
                        except Exception:
                            pass
                    _clean_sheets()
                    generate_schematic(auto_stub_fallback=auto_stub_fallback, **opts)
                    _sanitize()
                    _ok, msg2, _u, _m = verify_connectivity.verify(name)
                    print(f">>> smart_schematic: all-label mode (legacy fallback) -> {msg2}")
            if not _ok:
                raise RuntimeError(
                    "smart_schematic: refusing to publish an unverified schematic; "
                    "kicad-cli could not validate the generated connectivity."
                )

    # DYNAMIC post-route cleanup -- runs for EVERY circuit, no per-project hook:
    #   * strip spurious FLOATING (dangling) global labels the router leaves
    #   * beautify wires: square any diagonal to Manhattan L, merge collinear
    #   * grid-align: snap connection coords to the 1.27mm grid (IPC-3)
    # Each step is INDEPENDENTLY connectivity-guarded: it is applied to a backup,
    # re-verified, and rolled back on its own if it changed connectivity -- so a
    # revert of one step (e.g. beautify on a dense sheet) never discards the gains
    # of the others (grid alignment). Each is also fail-safe (skips on error).
    sheet_paths = [name + ".anvil_sch"] + sorted(_glob.glob(name + "_*.anvil_sch"))
    import shutil

    def _guarded(sheet_path, step, note):
        """Run one cleanup on one sheet; roll it back if project connectivity changes."""
        bak = sheet_path + ".step_bak"
        try:
            shutil.copyfile(sheet_path, bak)
        except Exception:
            bak = None
        try:
            changed = step(sheet_path)
        except Exception as e:
            warnings.warn(f"smart_schematic: {note} skipped: {e}", RuntimeWarning)
            changed = 0
        if bak:
            ok = True
            if changed:
                try:
                    ok = verify_connectivity.verify(name)[0] if verify_connectivity else True
                except Exception:
                    ok = True
            if changed and not ok:
                shutil.copyfile(bak, sheet_path)
                print(f">>> smart_schematic: {note} reverted for "
                      f"{os.path.basename(sheet_path)} (connectivity guard)")
                changed = 0
            try:
                os.remove(bak)
            except Exception:
                pass
        return changed

    def _do_strip(p):
        import strip_dangling_labels
        n = strip_dangling_labels.strip(p)
        if n:
            print(f">>> smart_schematic: stripped {n} spurious dangling labels")
        return n

    def _do_beautify(p):
        import beautify_wires
        m = beautify_wires.beautify(p)
        if m:
            print(f">>> smart_schematic: beautified wires ({m} segments merged/reshaped)")
        return m

    def _do_gridsnap(p):
        # IPC-3: snap connection coords to KiCad's 1.27mm grid so off-grid pins/wires
        # (from symbols whose pins sit at non-50mil offsets) land on grid.
        import grid_snap
        g = grid_snap.snap(p)
        if g:
            print(f">>> smart_schematic: grid-aligned {g} coordinates to 1.27mm (IPC-3)")
        return g

    def _do_normalize(p):
        # Phase 5 geometry: lengthen too-short pin-exit stubs by sliding the
        # downstream chain + its movable label outward. Safe subset only (a
        # pin->pin exit needs a router-planned jog, so it is left alone).
        # min_exit_mm = 3.81 (150 mil) -- the professional pin->label stub length;
        # 100 mil left labels/power-symbols sitting on the pin (the "VO -> +3.3V
        # too close" case). Guarded by the connectivity revert-guard.
        import normalize_exits
        e = normalize_exits.normalize(p, min_exit_mm=3.81)
        if e:
            print(f">>> smart_schematic: normalized {e} short pin-exit(s) (>=150 mil)")
        return e

    def _do_textfix(p):
        # IEEE 315: field text reads horizontally or bottom-up only -- never
        # upside-down (a rotated/mirrored part can leave 180/270-deg fields).
        import fix_text_orientation
        tfx = fix_text_orientation.fix(p)
        if tfx:
            print(f">>> smart_schematic: normalized {tfx} upside-down field text angle(s)")
        return tfx

    def _do_pwrflags(p):
        # Every undriven power rail gets a PWR_FLAG, so the sheet passes KiCad
        # ERC first time -- no "Input Power pin not driven" on any circuit.
        import add_pwr_flags
        k = add_pwr_flags.add(name)
        if k:
            print(f">>> smart_schematic: added {k} PWR_FLAG(s) -- power rails ERC-clean")
        return k

    def _do_labeltaps(p):
        # a label connects by sitting ON a wire -- tap wires (and the junction
        # dot their T forces) are removable; fewer wires, zero fragile dots
        import remove_label_taps
        lt = remove_label_taps.remove(p)
        if lt:
            print(f">>> smart_schematic: removed {lt} label tap(s) -> label sits on wire")
        return lt

    # Order (RULE_ENGINE Phase 10): dangling-strip -> pin-exit normalize ->
    # wire beautify -> label-tap removal -> grid-snap (final sheet on-grid) ->
    # text orientation -> PWR_FLAGs last (they copy final power-symbol coords).
    for sheet_path in sheet_paths:
        _guarded(sheet_path, _do_strip, "dangling-label strip")
        _guarded(sheet_path, _do_normalize, "pin-exit normalize")
        _guarded(sheet_path, _do_beautify, "wire beautify")
        _guarded(sheet_path, _do_labeltaps, "label-tap removal")
        _guarded(sheet_path, _do_gridsnap, "grid snap")
        # The geometry passes above can detach a decorative net-name label from
        # its wire, so make one final guarded cleanup pass on every hierarchy page.
        _guarded(sheet_path, _do_strip, "dangling-label strip (final)")
        _guarded(sheet_path, _do_textfix, "field-text orientation")
    # PWR_FLAG add runs UNGUARDED: a flag only adds a `#FLG` pin coincident with
    # an existing net anchor, and the verifier strips `#` refs, so it can NEVER
    # change real connectivity. The guard's kicad-cli re-export is occasionally
    # flaky and would then falsely revert the flags (observed: 4 ERC errors left
    # on a 51-part sheet whose flags verified fine standalone). Applying it after
    # the guarded chain keeps the flags.
    _do_pwrflags(name + ".anvil_sch")

    _write_kicad_pro(os.path.abspath(name + ".anvil_sch"))

    # IPC compliance gate: report every build against the enforceable IPC-2612 /
    # IPC-2611 schematic rules (Manhattan 90deg, on-grid, no dangling labels,
    # junction dots, power rail symbols, title block) PLUS the real KiCad ERC
    # error list -- exactly what the GUI's ERC dialog will show. Read-only.
    try:
        import ipc_check
        cli = verify_connectivity.KICAD_CLI if verify_connectivity else ""
        for sheet_path in sheet_paths:
            ipc_check.report(os.path.splitext(sheet_path)[0], cli)
    except Exception as e:
        warnings.warn(f"smart_schematic: IPC compliance report skipped: {e}", RuntimeWarning)

    # ---- atomic publish: move the finished artifacts into the project dir ----
    os.chdir(_proj)
    for _old in _glob.glob(os.path.join(_proj, name + "_*.anvil_sch")):
        if not os.path.exists(os.path.join(_stage, os.path.basename(_old))):
            try:
                os.remove(_old)  # stale child sheet from a previous layout
            except OSError:
                pass
    for _f in os.listdir(_stage):
        _src = os.path.join(_stage, _f)
        _dst = os.path.join(_proj, _f)
        if _f == name + ".anvil_pro":
            if not os.path.exists(_dst):  # never clobber an existing project file
                os.replace(_src, _dst)
            continue
        if _f.endswith(".anvil_sch") or _f in (name + ".net", name + ".erc"):
            os.replace(_src, _dst)
    _shutil.rmtree(_stage, ignore_errors=True)

    sch = os.path.join(_proj, name + ".anvil_sch")
    pro = os.path.join(_proj, name + ".anvil_pro")
    if not os.path.exists(pro):
        pro = _write_kicad_pro(sch)
    print(f">>> smart_schematic: {name}.net + {name}.anvil_sch + "
          f"{os.path.basename(pro)} ready (published atomically)")
    print(f">>> smart_schematic: placement = {_placer_used}")
    return sch, pro
