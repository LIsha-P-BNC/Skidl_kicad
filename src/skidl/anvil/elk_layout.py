"""elk_layout.py -- ELK layered layout as a drop-in placement for schematics.

Force-directed placement packs parts tightly and needs a post-hoc relax pass to
chase collisions; it has a quality ceiling below hand-drawn schematics. ELK's
'layered' algorithm places parts by signal flow (Input->Function->Output),
guarantees non-overlapping boxes and orthogonal spacing BY CONSTRUCTION, and
lays out functional blocks (compound nodes) with their contents inside.

This module builds an ELK graph from a placed SchNode's parts, runs elkjs
(Node.js), and repositions each part's .tx to the ELK coordinate. It runs AFTER
node.place() (so place_bbox / bboxes exist) and BEFORE node.route(), the same
injection point the relax pass uses -- so the existing router + emitter finish
the job. Opt in with generate_schematic(..., placement_mode="elk").

Requires Node.js + elkjs. Locations searched (first hit wins):
  $ANVIL_ELK_DIR/node_modules/elkjs, then a bundled tools/elk/, then the
  process CWD's node_modules. If none found (or Node missing), place_with_elk
  raises ElkUnavailable and the caller falls back to the force-directed placer.
"""
import json
import os
import re
import subprocess
import tempfile

from skidl.geometry import Point, Tx

GROUND_RE = re.compile(r"^(GND|AGND|DGND|PGND|VSS|VEE)\d*$", re.I)
POWER_RE = re.compile(
    r"^(GND|AGND|DGND|PGND|VSS|VEE|VCC|VDD|VBUS|VBAT|VGSM|VGPIO|\+\d[\d.]*V\d*)$", re.I
)

# ELK runs directly in engine units (mils): each ELK node carries the part's
# REAL place_bbox size (body + ref/value text + routing padding), so ELK's
# no-overlap guarantee covers the text too. Scale stays 1.0 unless overridden.
_ELK_SCALE = float(os.environ.get("SKIDL_ELK_SCALE", "1.0"))


class ElkUnavailable(Exception):
    """Node.js and/or elkjs could not be located; caller should fall back."""


def _find_node():
    for c in ("node", "node.exe"):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            p = os.path.join(d, c)
            if os.path.isfile(p):
                return p
    # common Windows install
    for p in (r"C:\Program Files\nodejs\node.exe", r"F:\Program Files\nodejs\node.exe"):
        if os.path.isfile(p):
            return p
    raise ElkUnavailable("node executable not found")


def _find_elk_dir():
    cands = []
    if os.environ.get("ANVIL_ELK_DIR"):
        cands.append(os.environ["ANVIL_ELK_DIR"])
    here = os.path.dirname(os.path.abspath(__file__))
    cands.append(os.path.join(here, "..", "..", "..", "tools", "elk"))  # bundled
    cands.append(os.getcwd())
    for d in cands:
        if os.path.isfile(os.path.join(d, "node_modules", "elkjs", "lib", "elk.bundled.js")) or \
           os.path.isdir(os.path.join(d, "node_modules", "elkjs")):
            return os.path.abspath(d)
    raise ElkUnavailable("elkjs (node_modules/elkjs) not found; set ANVIL_ELK_DIR")


_RUNNER_JS = """
const fs=require('fs');const ELK=require('elkjs');const elk=new ELK();
const g=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
elk.layout(g).then(r=>{fs.writeFileSync(process.argv[3],JSON.stringify(r));})
 .catch(e=>{console.error(String(e));process.exit(1);});
"""


def _gather_parts(node, out=None):
    if out is None:
        out = []
    for p in getattr(node, "parts", []):
        ref_prefix = (getattr(p, "ref_prefix", "") or "").upper()
        if ref_prefix == "NT":  # NetTerminal: synthetic, skip
            continue
        out.append(p)
    for ch in getattr(node, "children", {}).values():
        _gather_parts(ch, out)
    return out


def _pin_net_name(pin):
    net = getattr(pin, "net", None)
    return getattr(net, "name", None) if net is not None else None


def _block_key(p):
    """Same block identity sch_node uses: hierarchy path + optional group tag,
    so a page's function blocks (part.group) each get their own ELK compound."""
    try:
        key = tuple(p.hiertuple)
    except Exception:
        key = ("root",)
    g = getattr(p, "group", None)
    if g:
        key = key + (str(g),)
    return key


def _build_graph(parts):
    """Build a hierarchical ELK graph: block -> parts -> pin ports."""
    blocks = {}
    for p in parts:
        blocks.setdefault(_block_key(p), []).append(p)

    def _part_size(p):
        """Real footprint of the part on the sheet, in engine units (mils):
        place_bbox includes body + ref/value text + routing padding, so ELK's
        no-overlap guarantee covers text collisions too."""
        for attr in ("place_bbox", "lbl_bbox", "bbox"):
            bb = getattr(p, attr, None)
            if bb is not None:
                try:
                    if bb.w > 0 and bb.h > 0:
                        return float(bb.w), float(bb.h)
                except Exception:
                    pass
        return 500.0, 400.0

    def make_part_node(p):
        pins = list(getattr(p, "pins", []))
        sig = [pin for pin in pins if not POWER_RE.match(_pin_net_name(pin) or "")]
        ports = []
        for pin in pins:
            name = _pin_net_name(pin) or ""
            pid = "%s::%s" % (p.ref, getattr(pin, "num", getattr(pin, "name", "?")))
            if POWER_RE.match(name):
                side = "SOUTH" if GROUND_RE.match(name) else "NORTH"
            else:
                # signal flows in LEFT, out RIGHT (reference convention);
                # direction-less pins fall back to the index split.
                from skidl.pin import pin_types as _pt
                fn = getattr(pin, "func", None)
                if fn in (_pt.OUTPUT, getattr(_pt, "TRISTATE", None),
                          getattr(_pt, "OPENCOLL", None), getattr(_pt, "OPENEMIT", None)):
                    side = "EAST"
                elif fn == _pt.INPUT:
                    side = "WEST"
                else:
                    try:
                        i = sig.index(pin)
                    except ValueError:
                        i = 0
                    side = "WEST" if i < (len(sig) + 1) // 2 else "EAST"
            ports.append({"id": pid, "layoutOptions": {"port.side": side}})
        w, hgt = _part_size(p)
        # 2-pin series parts may be rotated 90 deg after layout (chain
        # orientation); give them a SQUARE slot so rotation never overflows.
        if len(sig) == 2 and len(pins) == 2:
            w = hgt = max(w, hgt)
        return {
            "id": p.ref,
            "width": w,
            "height": hgt,
            "layoutOptions": {"portConstraints": "FIXED_SIDE"},
            "ports": ports,
        }

    # count intra-block signal edges: a block whose parts connect only via
    # power rails (e.g. a decoupling/bulk-cap bank) has no layering signal --
    # layered ELK would stack it into one tall column. Pack those as a grid.
    def _block_has_signal_edges(ps):
        refs = {p.ref for p in ps}
        seen = {}
        for p in ps:
            for pin in getattr(p, "pins", []):
                nm = _pin_net_name(pin)
                if not nm or POWER_RE.match(nm):
                    continue
                seen.setdefault(nm, set()).add(p.ref)
        return any(len(v & refs) >= 2 for v in seen.values())

    children = []
    for key, ps in blocks.items():
        if _block_has_signal_edges(ps):
            algo_opts = {
                "elk.algorithm": "layered", "elk.direction": "RIGHT",
                "elk.spacing.nodeNode": "110",
                "elk.layered.spacing.nodeNodeBetweenLayers": "260",
            }
        else:
            algo_opts = {  # grid pack (decap banks etc.)
                "elk.algorithm": "rectpacking",
                "elk.spacing.nodeNode": "200",
            }
        opts = {"elk.padding": "[top=220,left=180,bottom=220,right=180]"}
        opts.update(algo_opts)
        children.append({
            "id": "BLK::" + "/".join(str(k) for k in key),
            "layoutOptions": opts,
            "children": [make_part_node(p) for p in ps],
        })

    # signal-net edges (exclude power/ground): connect pins of each net
    seen = {}
    for p in parts:
        for pin in getattr(p, "pins", []):
            nm = _pin_net_name(pin)
            if not nm or POWER_RE.match(nm):
                continue
            pid = "%s::%s" % (p.ref, getattr(pin, "num", getattr(pin, "name", "?")))
            seen.setdefault(nm, []).append(pid)
    edges = []
    ei = 0
    for nm, pids in seen.items():
        if len(pids) < 2:
            continue
        for k in range(1, len(pids)):
            edges.append({"id": "e%d" % ei, "sources": [pids[0]], "targets": [pids[k]]})
            ei += 1

    # Root level: BOX packing arranges the block rectangles compactly (row/
    # grid) instead of layered's diagonal staircase. ELK's inter-block edge
    # routes are unused anyway -- the engine's own router draws the wires from
    # the final part positions -- so no layered routing is needed at root.
    return {
        "id": "root",
        "layoutOptions": {
            "elk.algorithm": "box",
            "elk.spacing.nodeNode": "900",
            "elk.aspectRatio": "1.6",
        },
        "children": children, "edges": edges,
    }


def _run_elk(graph):
    node_exe = _find_node()
    elk_dir = _find_elk_dir()
    tmp = tempfile.mkdtemp(prefix="anvil_elk_")
    gin = os.path.join(tmp, "in.json")
    gout = os.path.join(tmp, "out.json")
    runner = os.path.join(tmp, "run.js")
    with open(gin, "w") as f:
        json.dump(graph, f)
    with open(runner, "w") as f:
        f.write(_RUNNER_JS)
    env = dict(os.environ, NODE_PATH=os.path.join(elk_dir, "node_modules"))
    r = subprocess.run([node_exe, runner, gin, gout], cwd=elk_dir, env=env,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not os.path.isfile(gout):
        raise ElkUnavailable("elkjs run failed: " + (r.stderr or "")[:200])
    with open(gout) as f:
        return json.load(f)


def _abs_centers(result, ax=0.0, ay=0.0, out=None):
    """Absolute ELK center for every leaf node (part), recursing compounds."""
    if out is None:
        out = {}
    for ch in result.get("children", []):
        x = ax + ch.get("x", 0.0)
        y = ay + ch.get("y", 0.0)
        if ch.get("children"):
            _abs_centers(ch, x, y, out)
        else:
            out[ch["id"]] = (x + ch.get("width", 0) / 2.0,
                             y + ch.get("height", 0) / 2.0)
    return out


def _sheet_nodes(node, out=None):
    """All nodes that render as their own drawing frame (root + unflattened)."""
    if out is None:
        out = [node]
    for ch in getattr(node, "children", {}).values():
        if getattr(ch, "flattened", False):
            continue  # inlined block: renders on parent sheet
        out.append(ch)
        _sheet_nodes(ch, out)
    return out


def _sheet_parts(node, out=None):
    """Parts rendered ON this sheet: own parts + flattened descendants'."""
    if out is None:
        out = []
    for p in getattr(node, "parts", []):
        if (getattr(p, "ref_prefix", "") or "").upper() == "NT":
            continue
        out.append(p)
    for ch in getattr(node, "children", {}).values():
        if getattr(ch, "flattened", False):
            _sheet_parts(ch, out)
    return out


def place_with_elk(node, **options):
    """ELK layered layout, PER SHEET.

    Each drawing frame gets its own ELK canvas: laying out every page's parts
    on ONE global canvas put each page's content mostly outside its own frame.
    Per sheet: build graph (blocks = compounds), run elkjs, orient 2-pin series
    parts along their chain, and anchor the layout's top-left to the sheet's
    original top-left (which was inside the frame).
    """
    scale = float(options.get("elk_scale", _ELK_SCALE))
    TX_CW_90 = Tx(a=0, b=-1, c=1, d=0)
    moved_total = 0

    sheets = _sheet_nodes(node)
    for sheet in sheets:
        parts = _sheet_parts(sheet)
        if len(parts) < 2:
            continue
        graph = _build_graph(parts)
        result = _run_elk(graph)
        centers = _abs_centers(result)

        def _cur_center(p):
            try:
                return p.place_bbox.ctr * p.tx
            except Exception:
                return Point(0, 0)

        targeted = [(p, centers[p.ref]) for p in parts if p.ref in centers]
        if not targeted:
            continue

        # Orient 2-pin series parts ALONG their chain (reference convention).
        for p, c in targeted:
            spins = [q for q in getattr(p, "pins", [])
                     if _pin_net_name(q) and not POWER_RE.match(_pin_net_name(q) or "")]
            if len(spins) != 2:
                continue
            nbr = []
            for q in spins:
                for op in getattr(getattr(q, "net", None), "pins", []) or []:
                    part = getattr(op, "part", None)
                    if part is not None and part is not p and getattr(part, "ref", None) in centers:
                        nbr.append(centers[part.ref])
                        break
            if len(nbr) < 2:
                continue
            horiz_flow = abs(nbr[0][0] - nbr[1][0]) >= abs(nbr[0][1] - nbr[1][1])
            try:
                a = spins[0].pt * p.tx
                b = spins[1].pt * p.tx
            except Exception:
                continue
            horiz_now = abs(a.x - b.x) >= abs(a.y - b.y)
            if horiz_flow != horiz_now:
                p.tx = p.tx * TX_CW_90

        # Anchor this sheet's layout to its own old top-left corner.
        old_min_x = min(_cur_center(p).x for p, _ in targeted)
        old_min_y = min(_cur_center(p).y for p, _ in targeted)
        new_min_x = min(c[0] * scale for _, c in targeted)
        new_min_y = min(c[1] * scale for _, c in targeted)
        off_x = old_min_x - new_min_x
        off_y = old_min_y - new_min_y
        from skidl.schematics.place import snap_to_grid
        for p, c in targeted:
            target = Point(c[0] * scale + off_x, c[1] * scale + off_y)
            p.tx = p.tx * Tx().move(target - _cur_center(p))
            # CRITICAL: ELK targets are raw floats; unsnapped parts put pins
            # off-grid, so emitted wire endpoints round differently from the
            # symbol pin position and KiCad sees them UNCONNECTED (this was
            # the 79-split connectivity bug). Snap like the engine's own
            # placer does.
            try:
                snap_to_grid(p)
            except Exception:
                pass
            moved_total += 1

    # Recompute every SHEET's bbox (children first) so the per-sheet frame
    # transform sees the moved parts -- a stale child bbox left whole pages
    # rendered outside their drawing frame.
    # Post-order recalc of EVERY node's bbox (flattened group blocks too --
    # a stale group bbox fed internal_bbox(), picking too-small paper and
    # mis-centering the sheet frame).
    def _recalc(n):
        for ch in getattr(n, "children", {}).values():
            _recalc(ch)
        try:
            n.calc_bbox()
        except Exception:
            pass

    _recalc(node)
    return moved_total
