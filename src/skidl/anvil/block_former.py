"""block_former.py -- merge fine-grained @subcircuits into subsystem PAGES.

A script often defines many small @subcircuit blocks (19 on the reference
tracker). With flatness=0.0 each becomes its own sheet -- far too many. The
reference design uses ~6 sheets, each holding several function blocks.

form_pages() restructures the circuit hierarchy BEFORE schematic generation:

  1. Treats each top-level @subcircuit as a candidate BLOCK.
  2. Greedily merges blocks into PAGES by INTERFACE COST: repeatedly merge the
     two clusters sharing the most signal nets (power/ground excluded --
     they connect everything), while the merged page stays under max_parts.
  3. Re-parents every part onto its page node and tags it with
     part.group = <original block name>. sch_node treats a group tag as an
     extra hierarchy level with is_group=True, so each original block renders
     as a titled inline box on its page sheet.

Result with flatness=0.0: one sheet per PAGE, function blocks inside each
sheet, local labels between blocks on a page, ports only between pages --
the reference structure. Pure restructuring: connectivity is untouched.
"""
from collections import defaultdict

from skidl.utilities import export_to_all


def _descendant_parts(node, out=None):
    if out is None:
        out = []
    out.extend(getattr(node, "parts", []))
    for ch in getattr(node, "children", []):
        _descendant_parts(ch, out)
    return out


def _page_name(members, idx):
    """Derive a page name from its member block names (common first token)."""
    tokens = defaultdict(int)
    for m in members:
        t = (m.name or "").split("_")[0].strip()
        if t:
            tokens[t] += 1
    if tokens:
        best = max(tokens, key=lambda k: (tokens[k], -len(k)))
        if tokens[best] > 1:
            return "%02d_%s" % (idx, best)
    return "%02d_%s" % (idx, (members[0].name or "page").split("_")[0])


@export_to_all
def form_pages(circuit=None, max_parts=45, min_page_parts=4):
    """Merge top-level blocks into subsystem pages. Returns #pages (0 = no-op)."""
    import builtins

    from skidl.node import Node
    from skidl.schematics.net_classify import classify_net_role

    circuit = circuit or builtins.default_circuit
    root = getattr(circuit, "root", None)
    if root is None:
        return 0
    blocks = [c for c in list(root.children) if _descendant_parts(c)]
    if len(blocks) < 3:
        return 0  # nothing worth merging

    parts_of = {id(b): _descendant_parts(b) for b in blocks}

    # signal nets shared between blocks (interface cost between clusters)
    net_blocks = defaultdict(set)
    for b in blocks:
        for p in parts_of[id(b)]:
            for pin in getattr(p, "pins", []):
                net = getattr(pin, "net", None)
                if net is None or classify_net_role(net) is not None:
                    continue  # skip power/ground rails
                name = getattr(net, "name", None)
                if name:
                    net_blocks[name].add(id(b))

    clusters = [[b] for b in blocks]

    def csize(cl):
        return sum(len(parts_of[id(b)]) for b in cl)

    def _tok(b):
        return (b.name or "").split("_")[0]

    def shared(c1, c2):
        ids1 = {id(b) for b in c1}
        ids2 = {id(b) for b in c2}
        s = sum(1 for bs in net_blocks.values() if bs & ids1 and bs & ids2)
        # same-subsystem name prefix (power_*, gsm_*) is a strong merge hint
        if {_tok(b) for b in c1} & {_tok(b) for b in c2}:
            s += 3
        return s

    # 1) merge by strongest interface first (min-cut greedy)
    while True:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if csize(clusters[i]) + csize(clusters[j]) > max_parts:
                    continue
                s = shared(clusters[i], clusters[j])
                if s > 0 and (best is None or s > best[0]):
                    best = (s, i, j)
        if best is None:
            break
        _, i, j = best
        clusters[i].extend(clusters[j])
        del clusters[j]

    # 2) absorb tiny leftover clusters into their most-connected page
    changed = True
    while changed:
        changed = False
        for j, cl in enumerate(clusters):
            if csize(cl) >= min_page_parts or len(clusters) == 1:
                continue
            cands = [
                (shared(cl, other), k)
                for k, other in enumerate(clusters)
                if k != j and csize(other) + csize(cl) <= max_parts
            ]
            if not cands:
                continue
            _, k = max(cands)
            clusters[k].extend(cl)
            del clusters[j]
            changed = True
            break

    # 2b) still above the target page count: merge the most-connected pair
    #     that fits, even with weak interfaces, until target reached.
    target_pages = 6
    while len(clusters) > target_pages:
        best = None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if csize(clusters[i]) + csize(clusters[j]) > max_parts:
                    continue
                s = shared(clusters[i], clusters[j])
                if best is None or s > best[0]:
                    best = (s, i, j)
        if best is None:
            break
        _, i, j = best
        clusters[i].extend(clusters[j])
        del clusters[j]

    if len(clusters) >= len(blocks):
        return 0  # no merge happened

    # 3) restructure: page nodes own the parts; original block name -> group tag
    for idx, cl in enumerate(sorted(clusters, key=lambda c: -csize(c)), 1):
        page = Node(_page_name(cl, idx), circuit=circuit)
        # Node() construction may auto-activate; force it into place explicitly.
        page.parent = root
        page.children = []
        if page not in root.children:
            root.children.append(page)
        for b in cl:
            for p in parts_of[id(b)]:
                p.group = b.name
                try:
                    p.node.parts.remove(p)
                except (AttributeError, ValueError):
                    pass
                page.parts.append(p)
                p.node = page
            if b in root.children:
                root.children.remove(b)
    return len(clusters)
