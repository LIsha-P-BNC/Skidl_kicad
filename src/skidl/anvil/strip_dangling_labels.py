"""Remove geometrically-dangling global labels (spurious extra labels the
generator placed off any pin/wire) from a .anvil_sch, WITHOUT changing
connectivity: every affected net still keeps its on-pin labels, which connect
by name. Verifies with kicad-cli ERC before/after.
"""
import os, re, subprocess, sys
import verify_connectivity as vc

CLI = vc.KICAD_CLI
NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def erc_dangling(sch):
    rpt = sch + ".ercchk"
    subprocess.run([CLI, "sch", "erc", "--severity-error", "-o", rpt, sch],
                   capture_output=True, timeout=300, creationflags=NW, stdin=subprocess.DEVNULL)
    txt = open(rpt, encoding="utf-8", errors="replace").read() if os.path.exists(rpt) else ""
    try: os.remove(rpt)
    except OSError: pass
    # positions of dangling labels, global AND local:
    #   "@(190.50 mm, 49.53 mm): Global Label 'PB3'"
    #   "@(143.51 mm, 90.17 mm): Label 'VIN'"
    if "label_dangling" not in txt:
        return []
    return re.findall(
        r"@\(([\d.]+) mm, ([\d.]+) mm\): (?:Global )?Label '([^']+)'", txt
    )

def _blocks(text, tag):
    """Yield (start, end) spans of each top-level (tag ...) s-expr."""
    for m in re.finditer(r"\(" + tag + r"\b", text):
        i = m.start(); depth = 0
        for j in range(i, len(text)):
            if text[j] == "(": depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    yield (i, j + 1); break

def strip(sch):
    dangling = erc_dangling(sch)
    print(f"dangling global labels found: {len(dangling)}")
    if not dangling:
        return 0
    text = open(sch, encoding="utf-8").read()
    targets = {(round(float(x), 2), round(float(y), 2), n) for x, y, n in dangling}
    remove = []
    for tag in ("global_label", "label"):
        for s, e in _blocks(text, tag):
            blk = text[s:e]
            nm = re.search(r"\(" + tag + r'\s+"([^"]+)"', blk)
            at = re.search(r"\(at\s+([\d.\-]+)\s+([\d.\-]+)", blk)
            if nm and at:
                key = (round(float(at.group(1)), 2), round(float(at.group(2)), 2), nm.group(1))
                if key in targets:
                    remove.append((s, e, nm.group(1), at.group(1), at.group(2)))
    # remove from the end so offsets stay valid
    for s, e, nm, x, y in sorted(remove, key=lambda r: -r[0]):
        text = text[:s] + text[e:]
        print(f"  removed dangling label {nm} @({x},{y})")
    # tidy leftover blank lines
    text = re.sub(r"\n[ \t]*\n[ \t]*\n", "\n\n", text)
    open(sch, "w", encoding="utf-8").write(text)
    return len(remove)

if __name__ == "__main__":
    sch = sys.argv[1] if len(sys.argv) > 1 else "atmega32u2_usb_mcu.anvil_sch"
    n = strip(sch)
    print(f"stripped {n} dangling labels")
    left = erc_dangling(sch)
    print(f"dangling labels remaining after strip: {len(left)}")
