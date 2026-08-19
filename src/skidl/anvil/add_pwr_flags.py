"""add_pwr_flags.py -- add PWR_FLAG symbols so KiCad ERC knows where power enters.

Every KiCad schematic whose power rails come only from power symbols (no PSU part
with a power-output pin) trips two ERC errors per rail:

    [power_pin_not_driven]: Input Power pin not driven by any Output Power pins

The standard cure is a PWR_FLAG symbol on each such rail. This module does that
DYNAMICALLY for any circuit:

  1. Read the intended netlist (<name>.net) and find every net that has a
     POWER-IN pin but no POWER-OUT pin -- those are the rails ERC will flag.
  2. For each such net, find one of its power symbols (power:<net>) in the
     generated .anvil_sch and drop a PWR_FLAG at the exact same origin -- the
     two pins coincide, so they connect with no extra wire.

Adds nothing when a rail is genuinely driven (e.g. a regulator VOUT pin typed
power-out), so it can never create a "two power outputs" conflict.

Use:
    import add_pwr_flags
    n = add_pwr_flags.add("myboard")     # processes <name>.anvil_sch [+ <name>_*.anvil_sch]
"""
import glob
import os
import re
import uuid as _uuid

# Canonical KiCad "power:PWR_FLAG" lib symbol (from the stock power library).
_PWR_FLAG_LIB = '''    (symbol "power:PWR_FLAG"
      (power global)
      (pin_numbers
        (hide yes))
      (pin_names
        (offset 0)
        (hide yes))
      (exclude_from_sim no)
      (in_bom yes)
      (on_board yes)
      (property "Reference" "#FLG"
        (at 0 1.905 0)
        (effects
          (font
            (size 1.27 1.27))
          (hide yes)))
      (property "Value" "PWR_FLAG"
        (at 0 3.81 0)
        (effects
          (font
            (size 1.27 1.27))))
      (property "Footprint" ""
        (at 0 0 0)
        (effects
          (font
            (size 1.27 1.27))
          (hide yes)))
      (property "Datasheet" ""
        (at 0 0 0)
        (effects
          (font
            (size 1.27 1.27))
          (hide yes)))
      (property "Description" "Special symbol for telling ERC where power comes from"
        (at 0 0 0)
        (effects
          (font
            (size 1.27 1.27))
          (hide yes)))
      (symbol "PWR_FLAG_0_0"
        (pin power_out line
          (at 0 0 90)
          (length 0)
          (name ""
            (effects
              (font
                (size 1.27 1.27))))
          (number "1"
            (effects
              (font
                (size 1.27 1.27))))))
      (symbol "PWR_FLAG_0_1"
        (polyline
          (pts
            (xy 0 0) (xy 0 1.27) (xy -1.016 1.905) (xy 0 2.54) (xy 1.016 1.905) (xy 0 1.27))
          (stroke
            (width 0)
            (type default))
          (fill
            (type none)))))'''


# pin types KiCad accepts as a "driver" (satisfy an Input / Power-in pin's need)
_DRIVER_TYPES = {"POWER-OUT", "OUTPUT", "BIDIRECTIONAL", "TRI-STATE",
                 "OPEN-COLLECTOR", "OPEN-EMITTER"}
# pin types that REQUIRE a driver on their net or KiCad ERC errors
_NEEDS_DRIVER = {"POWER-IN", "INPUT"}


def _nets_needing_flag(netlist_path):
    """From a SKiDL/KiCad .net: (nets that need a driver flag, set of already-driven
    net names). A net "needs a flag" when it has a POWER-IN or INPUT pin but NO real
    driver -- this covers both `power_pin_not_driven` (VCC/GND fed only by symbols)
    AND `pin_not_driven` (a controller timing/bias INPUT pin like RT fed only by an
    R/C network). A PWR_FLAG (power_out) is KiCad's standard silencer for both.
    Power SYMBOLS never appear in the netlist, so a rail used only through symbols
    shows no POWER-IN here -- the schematic-side scan in _add_to_file covers those."""
    try:
        text = open(netlist_path, encoding="utf-8", errors="replace").read()
    except OSError:
        return [], set()
    need, driven = [], set()
    for block in re.split(r"\(net\s+\(code", text)[1:]:
        nm = re.search(r'\(name\s+"([^"]+)"', block)
        if not nm:
            continue
        types = {
            t.upper().replace("_", "-")
            for t in re.findall(r'\(pintype\s+"([^"]+)"', block)
        }
        if types & _DRIVER_TYPES:
            driven.add(nm.group(1))
        elif types & _NEEDS_DRIVER:
            need.append(nm.group(1))
    return need, driven


def _close_of(text, start):
    """Index of the ')' closing the s-expr that opens at text[start] == '('."""
    depth = 0
    for j in range(start, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    raise ValueError("unbalanced s-expression")


def _junction_instance(x, y, uuid):
    """A junction dot at (x,y) -- forces KiCad to bond a pin that lands on a
    wire mid-segment (a flag slid onto the stub midpoint for visual de-dup)."""
    return (f'  (junction\n    (at {x} {y})\n    (diameter 0)\n'
            f'    (color 0 0 0 0)\n    (uuid {uuid})\n  )\n')


def _flag_instance(x, y, ref, proj, path, u_sym, u_pin):
    return f'''  (symbol
    (lib_id "power:PWR_FLAG")
    (at {x} {y} 0)
    (unit 1)
    (exclude_from_sim no)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (fields_autoplaced yes)
    (uuid {u_sym})
    (property "Reference" "{ref}"
      (at {x} {round(y - 1.905, 4)} 0)
      (effects
        (font
          (size 1.27 1.27))
        (hide yes)))
    (property "Value" "PWR_FLAG"
      (at {x} {round(y - 3.81, 4)} 0)
      (effects
        (font
          (size 1.27 1.27))
        (hide yes)))
    (property "Footprint" ""
      (at {x} {y} 0)
      (effects
        (font
          (size 1.27 1.27))
        (hide yes)))
    (property "Datasheet" ""
      (at {x} {y} 0)
      (effects
        (font
          (size 1.27 1.27))
        (hide yes)))
    (pin "1"
      (uuid {u_pin}))
    (instances
      (project "{proj}"
        (path "{path}"
          (reference "{ref}")
          (unit 1)))))
'''


def _add_to_file(sch_path, nets, driven=frozenset(), already=frozenset()):
    """Add PWR_FLAG for each net in `nets` whose power symbol lives in this sheet,
    PLUS every power-symbol rail on the sheet not driven by a POWER-OUT pin
    (a rail used only through power symbols shows no POWER-IN in the netlist,
    but KiCad still errors on the symbol's own power-input pin). Nets in
    `already` were flagged on another sheet -- global rails need exactly ONE
    flag per project (two PWR_FLAGs on a net is itself an ERC conflict).
    Returns list of net names flagged here."""
    text = open(sch_path, encoding="utf-8", errors="replace").read()

    rails = set(re.findall(r'\(lib_id\s+"power:([^"]+)"\)', text))
    rails.discard("PWR_FLAG")
    nets = list(nets) + sorted(rails - set(nets) - set(driven))
    nets = [n for n in nets if n not in already]
    if not nets:
        return []

    # instance-path metadata copied from any existing symbol on this sheet
    pm = re.search(r'\(instances\s*\(project\s+"([^"]*)"\s*\(path\s+"([^"]+)"', text)
    proj, path = (pm.group(1), pm.group(2)) if pm else ("", "/")

    # next free #FLG number (KiCad numbers them #FLG01, #FLG02, ...)
    used = [int(n) for n in re.findall(r'"#FLG0*(\d+)"', text)]
    nxt = max(used) + 1 if used else 1

    done, inserts = [], []
    for net in nets:
        m = re.search(
            r'\(symbol\s*\(lib_id\s+"power:'
            + re.escape(net)
            + r'"\)\s*\(at\s+([\d.\-]+)\s+([\d.\-]+)\s+[\d.\-]+\)',
            text,
        )
        if not m:
            # No power symbol (a switched/non-standard rail name like "+15V_ON"
            # renders as labels, not symbols) -- attach the flag at one of the
            # net's LABELS instead: the label anchor sits on a wire, and a
            # coincident pin connects there just the same.
            m = re.search(
                r'\(label\s+"' + re.escape(net) + r'"\s*\(at\s+([\d.\-]+)\s+([\d.\-]+)',
                text,
            )
        if not m:
            continue  # net not represented on this sheet
        x, y = float(m.group(1)), float(m.group(2))
        # Visual de-dup: a flag stamped exactly ON the power symbol reads as a
        # DOUBLED symbol (QA: "duplicate component"). If this anchor has its
        # pin->symbol stub wire, put the flag at the stub's MIDPOINT instead --
        # a pin anywhere on a wire connects identically in KiCad, and the
        # result is the standard "PWR_FLAG on the wire" idiom. H/V stubs keep
        # the shared axis coordinate exact, so the midpoint stays on the wire.
        wm = re.search(
            r'\(wire\s*\(pts\s*\(xy\s+([\d.\-]+)\s+([\d.\-]+)\)\s*\(xy\s+'
            + re.escape(m.group(1)) + r"\s+" + re.escape(m.group(2)) + r"\)\)",
            text,
        ) or re.search(
            r'\(wire\s*\(pts\s*\(xy\s+' + re.escape(m.group(1)) + r"\s+"
            + re.escape(m.group(2)) + r"\)\s*\(xy\s+([\d.\-]+)\s+([\d.\-]+)\)\)",
            text,
        )
        moved_to_midpoint = False
        if wm:
            ox, oy = float(wm.group(1)), float(wm.group(2))
            if abs(ox - x) < 0.001:
                y = round((y + oy) / 2.0, 2)   # vertical stub: keep exact x
                moved_to_midpoint = True
            elif abs(oy - y) < 0.001:
                x = round((x + ox) / 2.0, 2)   # horizontal stub: keep exact y
                moved_to_midpoint = True
        ref = f"#FLG{nxt:02d}"
        nxt += 1
        u_sym = _uuid.uuid5(_uuid.NAMESPACE_URL, f"pwr-flag:{sch_path}:{net}")
        u_pin = _uuid.uuid5(_uuid.NAMESPACE_URL, f"pwr-flag-pin:{sch_path}:{net}")
        inserts.append(_flag_instance(x, y, ref, proj, path, u_sym, u_pin))
        # A flag on a wire ENDPOINT connects there directly; a flag on a wire
        # MID-SEGMENT does NOT unless a junction marks the node (KiCad only
        # bonds a pin to a wire at an endpoint or a junction). So whenever the
        # de-dup above slides the flag onto the stub midpoint, drop a junction
        # at that exact point -- the standard "PWR_FLAG taps the wire" idiom.
        if moved_to_midpoint:
            u_j = _uuid.uuid5(_uuid.NAMESPACE_URL, f"pwr-flag-junc:{sch_path}:{net}")
            inserts.append(_junction_instance(x, y, u_j))
        done.append(net)

    if not done:
        return []

    # embed the PWR_FLAG lib symbol once
    if '"power:PWR_FLAG"' not in text:
        ls = re.search(r"\(lib_symbols", text)
        if not ls:
            return []
        end = _close_of(text, ls.start())
        text = text[:end] + "\n" + _PWR_FLAG_LIB + text[end:]

    # append instances just before the file's final closing paren
    end = _close_of(text, text.index("(kicad_sch"))
    text = text[:end] + "\n" + "".join(inserts) + text[end:]

    with open(sch_path, "w", encoding="utf-8") as f:
        f.write(text)
    return done


def add(name, netlist=None):
    """Flag every undriven power rail of project <name>. Returns count of flags added."""
    base = name[:-10] if name.endswith(".anvil_sch") else name
    nets, driven = _nets_needing_flag(netlist or base + ".net")
    remaining, flagged = list(nets), set()
    total = 0
    for sch in glob.glob(base + ".anvil_sch") + glob.glob(base + "_*.anvil_sch"):
        placed = _add_to_file(sch, remaining, driven, flagged)
        total += len(placed)
        flagged.update(placed)
    return total


if __name__ == "__main__":
    import sys

    nm = sys.argv[1] if len(sys.argv) > 1 else "circuit"
    print(f"PWR_FLAGs added: {add(nm)}")
