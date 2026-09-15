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
    # The Default netclass MUST carry the full key set the app itself writes.
    # A skeletal {"name": "Default"} loads in the CLI but leaves the GUI's
    # NET_SETTINGS without a usable default class -- schematic connectivity
    # then computes EVERY pin as unconnected (wires ignored, junction dots
    # rendered at wire_width 0), which is exactly the "AI project: manual
    # wires won't connect / no dots" bug. Values = the app's own defaults.
    "net_settings": {"classes": [{
        "bus_width": 12,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": 2147483647,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.2,
        "tuning_profile": "",
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "wire_width": 6,
    }], "meta": {"version": 3}},
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
          hierarchy=None, notes=None, **overrides):
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
        notes: OPTIONAL on-sheet notes (rule C2) -- a string or list of strings.
            Each becomes one numbered line in a "Notes:" block drawn in the top
            sheet's top-left corner (jumper settings, DNP/NP marks, layout
            constraints, warnings). Decorative text only -- never affects ERC.
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
    # Default the project name to the design name so the title block is populated
    # out of the box (a blank project field is what makes our sheets look unfinished
    # vs a professional one). Caller-supplied project always wins.
    _tb_meta.setdefault("project", name)
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
    # DEBUG WEAPON (env SKIDL_STUB_TRAP2=<net-substr>): intercept EVERY write
    # to Pin.stub and print the writer's stack when the pin's net matches --
    # the definitive way to find which of the FOUR wire/label deciders stubbed
    # a net (they are scattered and some are silent).
    if os.environ.get("SKIDL_STUB_TRAP2"):
        from skidl.pin import Pin as _PinCls
        if not getattr(_PinCls, "_stub_trapped", False):
            _PinCls._stub_trapped = True

            def _stub_get(self):
                return self.__dict__.get("_stub_val", False)

            def _stub_set(self, v):
                _t = os.environ.get("SKIDL_STUB_TRAP2", "")
                _n = str(getattr(getattr(self, "net", None), "name", ""))
                if v and _t and _t in _n:
                    import traceback as _tb
                    _fr = "".join(_tb.format_stack(limit=7)[:-1])
                    print(f">>> PIN_STUB_SET net={_n} "
                          f"pin={getattr(getattr(self,'part',None),'ref','?')}/"
                          f"{getattr(self,'num','?')}\n{_fr}>>> END_STACK")
                self.__dict__["_stub_val"] = v

            def _stub_del(self):
                self.__dict__.pop("_stub_val", None)
            _PinCls.stub = property(_stub_get, _stub_set, _stub_del)

    import builtins
    _real_parts = [p for p in builtins.default_circuit.parts
                   if not str(getattr(p, "ref", "") or "").startswith("#")]
    n_parts = len(_real_parts)
    # DYNAMIC, config-driven (no magic number): a design is "big" once it exceeds
    # SKIDL_HIER_MIN_PARTS real parts -- the point past which a single page gets
    # crowded and a per-section sheet split earns its keep. Scale-free: same rule
    # for a 5-part or a 500-part circuit, just a different side of the threshold.
    _hier_min = int(os.environ.get("SKIDL_HIER_MIN_PARTS", "50"))
    big = n_parts > _hier_min

    # D.0 BLOCK-SANITY GATE (docs SCHEMATIC_DESIGN_RULES section D): "a very
    # small single-function circuit is ONE block (or none) -- splitting one
    # small function's stages into separate boxes is over-partitioning". If a
    # SMALL design arrives with SEVERAL authored/auto blocks (e.g. a 10-part
    # regulator chain split into INPUT / REG / STATUS LED / OUT), MERGE them:
    # clear the group tags so the sheet renders as one clean function (row +
    # ladder rails), and suppress the leftover section boxes. A small design
    # with EXACTLY ONE authored block keeps it (the buck's titled box).
    # Enforced HERE so a badly-partitioned script still ships a correct sheet
    # -- the engine encodes the rule, not the author's discipline.
    _one_block_max = int(os.environ.get("SKIDL_ONE_BLOCK_MAX", "12"))
    _auth_groups = {str(getattr(p, "group", None))
                    for p in _real_parts if getattr(p, "group", None)}
    if n_parts <= _one_block_max and len(_auth_groups) > 1:
        for _p in _real_parts:
            try:
                _p.group = None
            except Exception:
                pass
        opts["suppress_block_boxes"] = True
        print(f">>> smart_schematic: {n_parts} parts / {len(_auth_groups)} blocks "
              "-> ONE function (D.0: merged over-split blocks; single clean sheet)")

    # Does the SCRIPT itself provide structure? Two independent signals:
    #   * real SKiDL hierarchy (@subcircuit / Group) -> the root Node has children
    #   * explicit smart_schematic.block()/group tags -> parts carry .group
    _root_children = getattr(getattr(builtins.default_circuit, "root", None),
                             "children", None) or {}
    _has_hier = bool(_root_children)
    # How many TOP-LEVEL @subcircuit pages the script actually built. Splitting is
    # only meaningful with >=2 pages (one page is just one sheet either way), so
    # the hierarchy decision keys on this, not on part count alone.
    try:
        _n_pages = len(_root_children)
    except Exception:
        _n_pages = 0
    _has_groups = any(getattr(p, "group", None) for p in _real_parts)

    # --- M8 AUTO HIERARCHY (opt-in: hierarchy="auto") ---
    # SHEET PACKER ("sheet full -> next sheet", docs D + Schemalyzer #30): a BIG
    # flat design with authored blocks must SPLIT into sheets instead of growing
    # one oversized page. Fill each sheet with WHOLE blocks in flow (creation)
    # order up to a per-sheet budget (SKIDL_SHEET_FILL_PARTS, docs heuristic
    # ~30-45 parts/page -- the part-count proxy for "page full"; a block is
    # NEVER split across sheets). Each bundle becomes a REAL hierarchy Node
    # (the proven auto_hierarchy part-move mechanism), so cross-sheet nets get
    # ports and every page still renders its blocks as boxed sections. Kill
    # switch: SKIDL_SHEET_PACK=0.
    if (big and not _has_hier and _has_groups
            and os.environ.get("SKIDL_SHEET_PACK", "1") != "0"):
        try:
            _budget = int(os.environ.get("SKIDL_SHEET_FILL_PARTS", "40"))
            # blocks in creation order, with their parts
            _blk_order, _blk_parts = [], {}
            for _p in _real_parts:
                _g = getattr(_p, "group", None)
                if not _g:
                    continue
                _g = str(_g)
                if _g not in _blk_parts:
                    _blk_order.append(_g)
                    _blk_parts[_g] = []
                _blk_parts[_g].append(_p)
            _bundles, _cur, _cnt = [], [], 0
            for _g in _blk_order:
                _n = len(_blk_parts[_g])
                if _cur and _cnt + _n > _budget:
                    _bundles.append(_cur)
                    _cur, _cnt = [], 0
                _cur.append(_g)
                _cnt += _n
            if _cur:
                _bundles.append(_cur)
            if len(_bundles) >= 2:
                from skidl.node import Node
                _root = builtins.default_circuit.root
                for _i, _bnd in enumerate(_bundles, start=1):
                    # H&C-style ordered sheet names: "01-POWER_IN.SchDoc" look
                    _nm = f"{_i:02d}-" + re.sub(r"[^\w.+-]+", "_",
                                                str(_bnd[0])).strip("_")
                    _node = Node(_nm, tag=_nm, circuit=builtins.default_circuit)
                    builtins.default_circuit.nodes.add(_node)
                    _root.add_child(_node)
                    for _g in _bnd:
                        for _p in _blk_parts[_g]:
                            try:
                                _p.node.parts.remove(_p)
                            except (ValueError, AttributeError):
                                pass
                            _node.parts.append(_p)
                            _p.node = _node
                _has_hier = True
                _root_children = _root.children
                _n_pages = len(_bundles)
                print(f">>> smart_schematic: SHEET PACK -- {n_parts} parts / "
                      f"{len(_blk_order)} blocks -> {len(_bundles)} sheets "
                      f"(~{_budget} parts/sheet, whole blocks, flow order)")
        except Exception as _e:
            warnings.warn(f"smart_schematic: sheet-pack skipped: {_e}",
                          RuntimeWarning)

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
    #
    # BUT part count alone is the wrong gate: a small circuit that is CLEARLY made
    # of several functional blocks (e.g. a few MOSFET driver channels, or 2-3 ICs)
    # should read as boxed blocks with inter-block LABELS -- the professional look
    # the user asked for -- even under the count threshold. So we also trigger when
    # the connectivity graph yields >=2 distinct anchor clusters. A trivial
    # anchor-less circuit (plain R/C/LED) produces no clusters and stays flat,
    # exactly as before -- no over-boxing of tiny designs. Fully dynamic.
    _auto_group_min = int(os.environ.get("SKIDL_AUTO_GROUP_MIN", "25"))
    # min distinct blocks that make a small design "worth boxing" (env-tunable).
    _auto_group_min_blocks = int(os.environ.get("SKIDL_AUTO_GROUP_MIN_BLOCKS", "2"))
    # HIERARCHICAL scripts get the SAME treatment PER PAGE: every @subcircuit
    # child sheet is itself laid out "single-sheet-with-blocks", so its clusters
    # must carry .group tags too (a group renders as a boxed section INSIDE its
    # page -- it never splits sheets, so the multi-sheet netlist caveat below
    # does not apply). On a page the bar is lower: even ONE anchor cluster is
    # boxed (env SKIDL_PAGE_GROUP_MIN_BLOCKS) so each sheet reads as the boxed
    # block(s) the single-sheet mode would have drawn. Anchor-less pages (bare
    # connectors / test points) yield no clusters and stay flat. Fully dynamic:
    # buckets come from the design's own hierarchy, nothing circuit-specific.
    _page_group_min_blocks = int(os.environ.get("SKIDL_PAGE_GROUP_MIN_BLOCKS", "1"))
    if not _has_groups:
        try:
            from skidl.schematics.cluster import detect_clusters
            # One bucket per @subcircuit page; a flat script = one top bucket,
            # which reproduces the old whole-design behavior exactly.
            _buckets = {}
            for _p in _real_parts:
                try:
                    _key = tuple(_p.hiertuple)
                except Exception:
                    _key = ("top",)
                _buckets.setdefault(_key, []).append(_p)
            _made = 0
            for _key, _bparts in _buckets.items():
                _clusters = [c for c in detect_clusters(_bparts) if len(c) >= 2]
                _min_blocks = (_page_group_min_blocks if len(_key) > 1
                               else _auto_group_min_blocks)
                # Group if the page is big (count gate) OR it yields enough blocks.
                _should_group = (len(_bparts) > _auto_group_min
                                 or len(_clusters) >= _min_blocks)
                if not _should_group:
                    continue
                for _cl in _clusters:
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
                      f"block(s) from the connectivity graph "
                      f"across {len(_buckets)} page(s)")
        except Exception as _e:
            warnings.warn(f"smart_schematic: auto-grouping skipped: {_e}",
                          RuntimeWarning)

    # --- M11: anchor-pack authored/auto functional blocks ---
    # Blocks present -> use the group-aware anchor packer: each block packs
    # TIGHT around its own anchor IC and the blocks read left-to-right in
    # author (signal-flow) order. Safe: place_node returns False / raises ->
    # legacy placer; and the build-level verify gate re-runs with legacy
    # ("legacy (anchor fell back)") if the layout can't publish. Kill switch:
    # SKIDL_ANCHOR_BLOCKS=0.
    # Anchor packing earns its keep only when there are MULTIPLE blocks or
    # anchors to pack against each other. A SINGLE-function, single-anchor
    # circuit (one regulator/MCU + its passives) lays out with cleaner
    # left->right FLOW under the legacy directional placer -- the anchor spiral
    # otherwise separates the signal core from the power-only passives and
    # scatters them (measured buck: legacy Y-span 38 mm vs anchor 163 mm).
    _n_groups = len({str(getattr(p, "group", "")) for p in _real_parts
                     if getattr(p, "group", None)})
    _n_anchors = sum(
        1 for p in _real_parts
        if (getattr(p, "ref_prefix", "") or "").upper() in ("U", "IC", "A")
        and len([pp for pp in getattr(p, "pins", [])]) >= 3
    )
    if (_has_groups and os.environ.get("SKIDL_ANCHOR_BLOCKS", "1") != "0"
            and (_n_groups >= 2 or _n_anchors >= 2)):
        opts.setdefault("placement_mode", "anchor")

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
    # --- 3-TIER LAYOUT AUTO-DECISION (the "where does each mode get used" rule) ---
    #   Tier 1  small / one function        -> SINGLE sheet, mostly WIRES.
    #   Tier 2  medium, several functions   -> SINGLE sheet with boxed BLOCKS
    #                                          (wire IN-block, label BETWEEN blocks).
    #   Tier 3  BIG design WITH @subcircuit  -> HIERARCHY: one child SHEET per
    #           pages                          @subcircuit page; each page is itself
    #                                          laid out single-sheet-with-blocks
    #                                          (blocks boxed, wired inside; cross-
    #                                          sheet nets become hierarchical labels;
    #                                          power nets stay power symbols).
    # Tier 3 is only taken when the SCRIPT actually built @subcircuit pages (real
    # SKiDL hierarchy) AND the design is big enough to overflow one page -- so we
    # never explode a small circuit into pages. If a hierarchical layout can't be
    # verified, the safety net further below rebuilds it as ONE single-sheet-with-
    # blocks page (always connectivity-correct). Passing flatness= explicitly
    # overrides this whole decision. Set env SKIDL_PAGE_MODE=0 to force single-sheet.
    # DYNAMIC hierarchy trigger: split into sheets ONLY when the design is both
    # big (> SKIDL_HIER_MIN_PARTS parts) AND actually made of >=2 functional pages
    # (@subcircuit). One page, or a small design, stays a single sheet-with-blocks.
    # Nothing here is circuit-specific -- it reads the design's own size + structure.
    _auto_multisheet = (big and _has_hier and _n_pages >= 2
                        and os.environ.get("SKIDL_PAGE_MODE") != "0")
    _page_mode = _auto_multisheet
    try:
        from skidl.schematics import sch_node as _sch_node
        _sch_node.set_page_mode(_page_mode)
    except Exception:
        pass
    if "flatness" not in opts:
        if _auto_multisheet:
            # Tier 3: one hierarchical sheet per @subcircuit page.
            opts["flatness"] = 0.0
            msg = (f"HIERARCHY -- one sheet per @subcircuit page "
                   f"(each page: boxed blocks, wire in-block, label between)")
        else:
            # Tier 1/2: a single page. block()/@subcircuit tags become boxed
            # functional sections; within-block nets are wired and only inter-block
            # signals become labels. Preferred for small/medium designs because it
            # avoids page explosion and the KiCad multi-sheet annotation caveat.
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

    # BLOCK-AWARE (user rule): the cutoff applies ONLY to a net that LEAVES its
    # block. A net living entirely INSIDE one functional block is wired as a
    # chain no matter how many pins it has (IC -> R -> C -> D ...); the per-block
    # router draws it and, if that one block is genuinely too dense, only THAT
    # block falls back to labels. Labels are reserved for nets that travel
    # between blocks -- that is where a dragged wire would overlap/short.
    def _net_block_key(_p):
        _prt = getattr(_p, "part", None)
        _g = getattr(_prt, "group", None) if _prt is not None else None
        if _g:
            return ("g", str(_g))
        try:
            return ("h", tuple(_prt.hiertuple))
        except Exception:
            return ("_",)

    # Default raised 3 -> 4 (2026-09-09): a 4-pin function network (reset =
    # chip pin + button + cap + test point) is the classic WIRED circuit and
    # the router handles it (stm32_usb_devboard wires at seed=1 with RESET_N
    # as a 4-pin tree; regr suite t1/t2 unchanged). Big rails (5+ pins/block)
    # still pre-label. Env override: SKIDL_WIRE_MAX_FANOUT.
    _wire_max = int(os.environ.get("SKIDL_WIRE_MAX_FANOUT", "4"))
    if _wire_max > 0:
        _stubbed = 0
        for _net in builtins.default_circuit.nets:
            try:
                if type(_net).__name__ == "NCNet" or getattr(_net, "stub", False):
                    continue
                # NOTE (tried & reverted 2026-09-09): exempting FUNCTION-
                # critical nets (reset/clock/decap/usb_diff) from this fanout
                # pre-stub regressed the stm32_usb_devboard benchmark to ALL-
                # LABEL -- the router cannot yet draw a >3-pin wire tree across
                # a spread legacy layout (every seed failed, safety fallback
                # ate the whole sheet). Professionals DO wire those networks
                # (docs par E), so revisit AFTER M12 satellite packing +
                # NetTerminal pruning give the router a tight layout to wire.
                # Count pins PER BLOCK: what the router must draw as one wire
                # tree is the within-block segment (a cross-block net gets one
                # NetTerminal per block and each block wires only ITS pins to
                # it). So a 4-pin net split 3+1 across two blocks is fine
                # (each side is a wireable <=3-pin tree), while >3 pins in ONE
                # block still becomes a label -- forcing those to wires makes
                # the router fail the whole sheet (verified by experiment).
                _per_block = {}
                for _p in _net.pins:
                    _pt = getattr(_p, "part", None)
                    if _pt is None:
                        continue
                    _k = _net_block_key(_p)
                    _per_block[_k] = _per_block.get(_k, 0) + 1
                if _per_block and max(_per_block.values()) > _wire_max:
                    _t = os.environ.get("SKIDL_NET_DEBUG")
                    if _t and _t in str(getattr(_net, "name", "")):
                        print(f">>> FANOUT_GATE_STUB net={_net.name}")
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
                  f"-> labels (chain segments stay wires)")

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

    # Inter-block/-node nets -> stub EVERY pin. SUPERSEDED by the group-aware
    # NetTerminal mechanism in sch_node.add_circuit(): a net spanning blocks now
    # gets ONE terminal label per block and its within-block pins are WIRED to
    # it (the professional look), instead of a label sprinkled on every pin.
    # Kept behind SKIDL_XBLOCK_STUB=1 as an emergency fallback only.
    if (_has_groups or _has_hier) and os.environ.get("SKIDL_XBLOCK_STUB") == "1":
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
        # ...and strip placement/routing scratch attrs from the SHARED circuit
        # Part/Pin objects so every generation attempt starts geometry-clean.
        # Leak observed (M11): the anchor pass sets pin.place_pt/route_pt for
        # EVERY net, but a later legacy pass only re-sets them for the nets in
        # its per-group lists -- a cross-block net (e.g. USB_DP) then draws
        # from stale anchor-era coordinates and verify fails on every seed
        # even though a cold legacy run verifies fine.
        try:
            import builtins as _b
            from skidl.utilities import rmv_attr as _rmv
            _parts = _b.default_circuit.parts
            _rmv(_parts, ("anchor_pins", "pull_pins", "pin_ctrs",
                          "saved_anchor_pins", "saved_pull_pins"))
            for _prt in _parts:
                _rmv(_prt.pins, ("place_pt", "route_pt"))
        except Exception:
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
            _t = os.environ.get("SKIDL_NET_DEBUG")
            if _t and _t in str(getattr(net, "name", "")):
                print(f">>> REPAIR_STUB net={net.name}")
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

    def _snapshot_wire_flags():
        """Record every net/pin wire-vs-label flag so a placer retry can start
        from the exact cold state (completed generation attempts leave rescue/
        stub flags on the SHARED circuit that change later classifications)."""
        _ns = [(_n, getattr(_n, "stub", None), getattr(_n, "_stub", None),
                getattr(_n, "_direct_wired", None))
               for _n in builtins.default_circuit.nets]
        _ps = [(_p, getattr(_p, "stub", None), getattr(_p, "direct_wired", None))
               for _prt in builtins.default_circuit.parts
               for _p in getattr(_prt, "pins", [])]
        return _ns, _ps

    def _restore_wire_flags(_snap):
        def _setb(_o, _k, _v):
            try:
                if _v is None:
                    if _k in getattr(_o, "__dict__", {}):
                        delattr(_o, _k)
                else:
                    setattr(_o, _k, _v)
            except Exception:
                pass
        _ns, _ps = _snap
        for _n, _s, _s2, _dw in _ns:
            _setb(_n, "stub", _s)
            _setb(_n, "_stub", _s2)
            _setb(_n, "_direct_wired", _dw)
        for _p, _s, _dw in _ps:
            _setb(_p, "stub", _s)
            _setb(_p, "direct_wired", _dw)

    _flags_snap = _snapshot_wire_flags()
    routed_seed = None
    for _placer_pass in range(2):
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
                if os.environ.get("SKIDL_SWEEP_DEBUG"):
                    import traceback as _dbg_tb
                    print(f">>> [sweep-debug] seed={sd} generation raised:")
                    _dbg_tb.print_exc()
                # HEAL ROLLBACK: the face-graph orphan-heal/island-bridge can
                # synthesize a global hop with no backing switchbox, which the
                # detailed router then fails EVERY seed (dense sheets). If a
                # generation attempt raises while healing is on, turn healing
                # off for the REST of the sweep -- the pre-heal behavior
                # (child label fallback) then routes as before, instead of the
                # whole design collapsing to all-label mode.
                if os.environ.get("SKIDL_ORPHAN_HEAL", "1") != "0":
                    os.environ["SKIDL_ORPHAN_HEAL"] = "0"
                    print(">>> smart_schematic: generation raised with face-"
                          "heal ON -> disabling SKIDL_ORPHAN_HEAL and "
                          "RETRYING THIS SEED (pre-heal routing behavior)")
                    _clean_sheets()
                    try:
                        # Same seed again WITHOUT healing -- otherwise the
                        # best (usually seed-0) layout is burned by the heal
                        # interplay and an inferior later seed wins (measured
                        # arduino: 55w/0l seed-0 lost -> 13w/24l seed-2).
                        generate_schematic(seed=sd,
                                           auto_stub_fallback="raise", **opts)
                    except Exception:
                        continue
                else:
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
                    # INFRA vs DESIGN failure: a kicad-cli export hiccup is NOT
                    # a routing defect -- it must not burn the seed (this
                    # intermittent flip IS the arduino/stm32 "all-label flap").
                    # One extra verify attempt after a pause.
                    if not ok and "could not export" in str(_msg):
                        print(">>> smart_schematic: verify INFRA failure "
                              f"(cli export) -- retrying once: {_msg}")
                        import time as _time_v
                        _time_v.sleep(1.0)
                        ok, _msg, _u, _m = verify_connectivity.verify(name)
                except Exception:
                    ok = True
                # Repair on SINGLE-sheet builds too (was multi-sheet-only): a lone
                # dropped local net (a 3-pin T-junction the router couldn't draw)
                # otherwise collapses the WHOLE sheet to all-label -- verified with
                # cap+cap+LED and regulator+caps circuits. Now only THAT net becomes
                # a label and every other wire survives. Unwanted-short mismatches
                # (_u) still abort to the next seed -- those are never repairable.
                if ok or _u:
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
            if os.environ.get("SKIDL_SWEEP_DEBUG"):
                print(f">>> [sweep-debug] seed={sd} verify FAILED: {_msg!r} "
                      f"unwanted={_u!r} missing={_m!r}")
                try:
                    import shutil as _sh
                    _dbg = f"{name}.debug_fail_seed{sd}.anvil_sch"
                    _sh.copyfile(name + ".anvil_sch", _dbg)
                    print(f">>> [sweep-debug] failed sheet kept: {_dbg}")
                except Exception:
                    pass
        if routed_seed is not None or opts.get("placement_mode") != "anchor":
            break
        # QUALITY ORDER: a WIRED legacy sheet beats an all-label anchor sheet.
        # The anchor-packed layout is denser than the router can wire on this
        # design -> drop to the legacy placer and redo the WIRED sweep before
        # degrading to the partial / all-label tiers below. Restore the
        # cold-state wire/stub flags first so the retry behaves exactly like a
        # fresh legacy run (anchor attempts leave rescue/stub flags behind).
        print(">>> smart_schematic: anchor layout wouldn't wire-route -> "
              "retrying wired sweep with legacy placer")
        _restore_wire_flags(_flags_snap)
        opts.pop("placement_mode", None)
        _placer_used = "legacy (anchor wouldn't wire)"

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
            # FINAL SAFETY NET (multi-sheet -> single-sheet): the engine's
            # cross-sheet routing/labelling of block() groups inside @subcircuit
            # PAGES is fragile and can drop connectivity. Rather than hard-fail a
            # big hierarchical design, rebuild the WHOLE thing as ONE sheet with
            # boxed functional blocks -- the proven-correct, always-verifiable
            # layout (labels connect by name on a single page, so they can never
            # be "lost across a sheet boundary"). The user still gets a neat,
            # verified schematic; only the page split is sacrificed.
            if not _ok and opts.get("flatness", 1.0) == 0.0:
                warnings.warn(
                    "smart_schematic: multi-sheet build did not verify; falling "
                    "back to a single sheet with boxed functional blocks.",
                    RuntimeWarning,
                )
                opts["flatness"] = 1.0
                _multisheet = False
                try:
                    _sch_node.set_page_mode(False)
                except Exception:
                    pass
                # Try to WIRE the single sheet first (neatest); commit the first
                # seed that verifies clean.
                routed_seed = None
                for sd in seeds:
                    _clean_sheets()
                    try:
                        generate_schematic(seed=sd, auto_stub_fallback="raise", **opts)
                    except Exception:
                        continue
                    _sanitize()
                    try:
                        if verify_connectivity is None or verify_connectivity.verify(name)[0]:
                            routed_seed = sd
                            _ok = True
                            print(f">>> smart_schematic: single-sheet fallback "
                                  f"routed with wires (seed={sd}); connectivity OK")
                            break
                    except Exception:
                        routed_seed = sd
                        _ok = True
                        break
                if not _ok:
                    # Single-sheet all-label ALWAYS connects (names, one page).
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
                    _ok = (verify_connectivity is None
                           or verify_connectivity.verify(name)[0])
                    if _ok:
                        print(">>> smart_schematic: single-sheet all-label "
                              "fallback -> connectivity OK")

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

    def _do_powerrail(p):
        # Ladder rails: collapse "a power symbol on every pin" into ONE horizontal
        # rail wire + short vertical stubs + ONE source symbol per rail (the
        # hand-drawn/TRACKER-V2 idiom). AUTO-ON for flow-hinted single-row layouts
        # (where a clean horizontal rail fits); SKIDL_DRAW_RAILS=0/1 force it
        # off/on for any layout. In flow mode the ground net also becomes a bottom
        # rail (SKIDL_DRAW_RAILS_GND=0 keeps GND as per-pin symbols). The
        # connectivity revert-guard rolls the whole pass back if any net fuses/splits.
        force = os.environ.get("SKIDL_DRAW_RAILS", "").lower()
        if force in ("0", "false", "no", "off"):
            return 0
        flow_mode = False
        try:
            import builtins
            # ladder-ready layouts: author flow_x hints (whole circuit) OR
            # blocks the engine itself arranged as tight rows (flow_place_block
            # tags parts _flow_rowed) -- both give every rail pin a clear
            # vertical path, so the ladder rails can draw.
            flow_mode = any(hasattr(pt, "flow_x") or getattr(pt, "_flow_rowed", False)
                            for pt in builtins.default_circuit.parts)
        except Exception:
            pass
        if not flow_mode and force not in ("1", "true", "yes", "on"):
            return 0  # only draw rails when the layout is flow-hinted (or forced)
        gnd = os.environ.get("SKIDL_DRAW_RAILS_GND", "1").lower() not in ("0", "false", "no", "off")
        import draw_power_rail
        r = draw_power_rail.draw(p, enable_gnd=gnd)
        if r:
            print(f">>> smart_schematic: drew {r} power rail(s) as horizontal ladder(s)")
        return r

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

    def _do_junctions(p):
        # FINAL connectivity heal: the editor connects wires only at endpoint
        # coincidences or explicit (junction) records -- a wire end touching
        # another wire's interior with no junction record is electrically dead.
        # The router's add_junctions() runs before the geometry passes above,
        # so any strap/stub/merge they produce can ship a dotless T. Re-derive
        # junctions from the final sheet text and add every missing record.
        import ensure_junctions
        j = ensure_junctions.ensure(p)
        if j:
            print(f">>> smart_schematic: added {j} missing wire junction(s)")
        return j

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
        # Ladder rails (guarded, gated): after label taps so the pin->symbol stubs
        # are still intact to read pin points from, before grid-snap+junctions so
        # the new rail geometry gets snapped and dotted like any other wire.
        _guarded(sheet_path, _do_powerrail, "power-rail draw")
        _guarded(sheet_path, _do_gridsnap, "grid snap")
        # AFTER every wire-geometry pass: heal any dotless wire-T the passes
        # above (or the router's ordering-sensitive add_junctions) left behind.
        _guarded(sheet_path, _do_junctions, "junction heal")
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

    # C2 on-sheet notes: draw the caller's jumper/DNP/warning text in the top
    # sheet's top-left corner. Decorative (no connection point) -> runs unguarded
    # after the connectivity passes, exactly like PWR_FLAG insertion above.
    if notes:
        try:
            import add_notes
            _nn = add_notes.add(name + ".anvil_sch", notes)
            if _nn:
                print(f">>> smart_schematic: added {_nn} on-sheet note line(s) (C2)")
        except Exception as _e:
            warnings.warn(f"smart_schematic: on-sheet notes skipped: {_e}",
                          RuntimeWarning)

    _write_kicad_pro(os.path.abspath(name + ".anvil_sch"))

    # IPC compliance gate: report every build against the enforceable IPC-2612 /
    # IPC-2611 schematic rules (Manhattan 90deg, on-grid, no dangling labels,
    # junction dots, power rail symbols, title block) PLUS the real KiCad ERC
    # error list -- exactly what the GUI's ERC dialog will show. Read-only.
    try:
        import ipc_check
        cli = verify_connectivity.KICAD_CLI if verify_connectivity else ""
        for _i, sheet_path in enumerate(sheet_paths):
            # sheet_paths[0] is the ROOT; the rest are child sheets whose
            # standalone ERC reports "non-existent parent" artifacts (filtered).
            ipc_check.report(os.path.splitext(sheet_path)[0], cli, child=_i > 0)
    except Exception as e:
        warnings.warn(f"smart_schematic: IPC compliance report skipped: {e}", RuntimeWarning)

    # ELECTRICAL-CORRECTNESS gate (runs for EVERY circuit): beyond connectivity
    # (ERC) and IPC readability, verify every component is RATED for the stress
    # its net actually carries and every calculated value meets its target --
    # by pure netlist + value + net-name voltage/current analysis (design_check,
    # no per-circuit rules). Catches an under-rated cap, an under-rated inductor,
    # a missing/uncalculated value, or a bad LED current before the design ships.
    try:
        import design_check
        _dc = design_check.check(name + ".net")
        _dc_err = [f for f in _dc if f[0] == "ERROR"]
        if _dc:
            print(">>> Electrical-correctness (component ratings + calculated values):")
            for _sev, _ref, _rule, _msg in _dc:
                _mk = "!!" if _sev == "ERROR" else ("~" if _sev == "WARN" else "OK")
                print(f"    {_mk} {_msg}")
        if _dc_err:
            print(f">>> smart_schematic: {len(_dc_err)} electrical-correctness "
                  "ERROR(s) above -- component rating/value needs fixing")
    except Exception as e:
        warnings.warn(f"smart_schematic: design-check skipped: {e}", RuntimeWarning)

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
