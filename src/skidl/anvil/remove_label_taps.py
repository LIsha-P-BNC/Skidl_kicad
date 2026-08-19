"""remove_label_taps.py -- eliminate junction dots created only to hang a net label.

The router sometimes attaches a wire's net-name label via a short TAP wire:

        ----+----------------   main run
            |                   <- tap wire (creates a junction dot at the T)
         LABEL

A KiCad label connects to any wire it SITS ON -- the tap is unnecessary, and the
junction dot it forces is fragile (a viewer that drops junctions silently splits
the net). This step finds every 3-way junction whose one branch is a dead-end
chain of wires terminating at exactly one label (and touching no symbol pin),
then deletes the branch, moves the label onto the junction point, and removes
the junction. Result: same connectivity, fewer wires, no junction dot.

Pure geometry, works for any circuit; connectivity-guarded by the caller.

Use:
    import remove_label_taps
    n = remove_label_taps.remove("myboard.anvil_sch")   # returns taps removed
"""
import re


def _blocks(text, token):
    """Yield (start, end_exclusive, blocktext) for every '(token ...' s-expr."""
    for m in re.finditer(r"\(" + token + r"\b", text):
        i = m.start()
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    yield i, j + 1, text[i : j + 1]
                    break


def _pt(x, y):
    return (round(float(x), 3), round(float(y), 3))


def _pin_points(text):
    """Absolute connect-points of every symbol pin (embedded lib defs + transforms)."""
    m = re.search(r"\(lib_symbols", text)
    if not m:
        return set()
    depth, lib_end = 0, len(text)
    for j in range(m.start(), len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                lib_end = j + 1
                break
    lib_text = text[m.start() : lib_end]

    libpins = {}
    for _, _, sym in _blocks(lib_text[1:], "symbol"):
        nm = re.match(r'\(symbol\s+"([^"]+)"', sym)
        if not nm:
            continue
        base = re.sub(r"_\d+_\d+$", "", nm.group(1))
        for pm in re.finditer(
            r"\(pin\s+\w+\s+\w+\s*\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+[\d.\-]+\)", sym
        ):
            libpins.setdefault(base, []).append(
                (float(pm.group(1)), float(pm.group(2)))
            )

    pts = set()
    for _, _, sym in _blocks(text[lib_end:], "symbol"):
        lm = re.search(r'\(lib_id\s+"([^"]+)"\)', sym)
        am = re.search(r"\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\)", sym)
        if not (lm and am):
            continue
        sx, sy, rot = float(am.group(1)), float(am.group(2)), float(am.group(3))
        mm = re.search(r"\(mirror\s+(\w+)\)", sym)
        mirror = mm.group(1) if mm else ""
        for px, py in libpins.get(lm.group(1), []):
            x, y = px, -py  # lib is Y-up, sheet is Y-down
            if mirror == "y":
                x = -x
            elif mirror == "x":
                y = -y
            r = rot % 360
            if r == 90:
                x, y = y, -x
            elif r == 180:
                x, y = -x, -y
            elif r == 270:
                x, y = -y, x
            pts.add(_pt(sx + x, sy + y))
    return pts


def remove(sch_path):
    """Strip label-tap branches + their junctions from <sch_path>. Returns count."""
    with open(sch_path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    wires = []
    for s, e, b in _blocks(text, "wire"):
        xy = re.findall(r"\(xy\s+([\d.\-]+)\s+([\d.\-]+)\)", b)
        if len(xy) == 2:
            wires.append(
                dict(span=(s, e), a=_pt(*xy[0]), b=_pt(*xy[1]))
            )
    junctions = []
    for s, e, b in _blocks(text, "junction"):
        am = re.search(r"\(at\s+([\d.\-]+)\s+([\d.\-]+)", b)
        if am:
            junctions.append(dict(span=(s, e), at=_pt(am.group(1), am.group(2))))
    labels = []
    for s, e, b in _blocks(text, "label"):
        lm = re.match(
            r'\(label\s+"[^"]*"\s*\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+([\d.\-]+)\)', b
        )
        if lm:
            labels.append(
                dict(span=(s, e), at=_pt(lm.group(1), lm.group(2)), block=b)
            )
    pins = _pin_points(text)

    def other(w, p):
        return w["b"] if w["a"] == p else w["a"]

    dead_wires, dead_junctions, label_moves = set(), set(), {}
    used_labels = set()
    for jn in junctions:
        J = jn["at"]
        touching = [i for i, w in enumerate(wires) if J in (w["a"], w["b"])]
        if len(touching) != 3:
            continue
        for wi in touching:
            chain = [wi]
            cur = other(wires[wi], J)
            ok = True
            while True:
                if cur in pins:
                    ok = False
                    break
                here_lbls = [i for i, L in enumerate(labels) if L["at"] == cur]
                nxt = [
                    i
                    for i, w in enumerate(wires)
                    if i not in chain and i not in dead_wires and cur in (w["a"], w["b"])
                ]
                if not nxt:
                    ok = (
                        ok
                        and len(here_lbls) == 1
                        and here_lbls[0] not in used_labels
                    )
                    term_lbl = here_lbls[0] if ok else None
                    break
                if len(nxt) == 1 and not here_lbls:
                    chain.append(nxt[0])
                    cur = other(wires[nxt[0]], cur)
                    continue
                ok = False
                break
            if ok and term_lbl is not None:
                dead_wires.update(chain)
                dead_junctions.add(junctions.index(jn))
                label_moves[term_lbl] = J
                used_labels.add(term_lbl)
                break

    if not dead_wires:
        return 0

    # rebuild the file text: drop dead spans, rewrite moved labels' (at ...)
    edits = []
    for i in sorted(dead_wires):
        edits.append((wires[i]["span"], ""))
    for i in dead_junctions:
        edits.append((junctions[i]["span"], ""))
    for li, J in label_moves.items():
        L = labels[li]
        nb = re.sub(
            r"\(at\s+[\d.\-]+\s+[\d.\-]+\s+[\d.\-]+\)",
            f"(at {J[0]} {J[1]} 0)",
            L["block"],
            count=1,
        )
        edits.append((L["span"], nb))
    edits.sort(key=lambda it: it[0][0], reverse=True)
    for (s, e), repl in edits:
        text = text[:s] + repl + text[e:]

    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(text)
    return len(label_moves)


if __name__ == "__main__":
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else "circuit.anvil_sch"
    if not p.endswith(".anvil_sch"):
        p += ".anvil_sch"
    print(f"label taps removed: {remove(p)}")
