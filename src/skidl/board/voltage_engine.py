# -*- coding: utf-8 -*-

"""
IPC-2221-informed VOLTAGE -> minimum conductor spacing (clearance) planning,
the voltage-side twin of width_engine.py (current -> width).

HONESTY CONTRACT (same as width_engine): every voltage is either the user's
own number (sidecar "voltages": {net: volts}) or a clearly-labeled heuristic;
every clearance carries its basis string.

Spacing table = IPC-2221 Table 6-1, copied VERBATIM from the application's own
PCB Calculator source (pcb_calculator/calculator_panels/
panel_electrical_spacing_ipc2221.cpp, `clist`), so a manual check in
Tools > Calculator > Electrical Spacing gives IDENTICAL numbers.

Columns: B1 internal, B2 external uncoated (sea level), B3 external uncoated
(>3050 m), B4 external polymer-coated, A5 external conformal-coated assembly,
A6 external component lead uncoated, A7 component lead conformal coated.
"""

# (v_max, (B1, B2, B3, B4, A5, A6, A7)) in mm -- verbatim from the app.
_IPC2221_TABLE = [
    (15,  (0.05, 0.1,  0.1,  0.05, 0.13, 0.13, 0.13)),
    (30,  (0.05, 0.1,  0.1,  0.05, 0.13, 0.25, 0.13)),
    (50,  (0.1,  0.6,  0.6,  0.13, 0.13, 0.4,  0.13)),
    (100, (0.1,  0.6,  1.5,  0.13, 0.13, 0.5,  0.13)),
    (150, (0.2,  0.6,  3.2,  0.4,  0.4,  0.8,  0.4)),
    (170, (0.2,  1.25, 3.2,  0.4,  0.4,  0.8,  0.4)),
    (250, (0.2,  1.25, 6.4,  0.4,  0.4,  0.8,  0.4)),
    (300, (0.2,  1.25, 12.5, 0.4,  0.4,  0.8,  0.8)),
]

_COLUMNS = {"internal": 0, "external": 1, "external_high_altitude": 2,
            "external_coated": 3, "conformal": 4}

import re

_MAINS_RE = re.compile(r"(MAINS|(^|_)AC(_|$)|230V|110V|LINE_L|LINE_N)", re.I)
_VOLT_RE = re.compile(r"(?:^|_)\+?(\d+)(?:V|V\d)?(?:_|$)|^\+(\d+(?:\.\d+)?)V", re.I)
_RAIL_RE = re.compile(r"^\+(\d+(?:\.\d+)?)V?", re.I)


def estimate_net_voltage(net_name, voltages=None):
    """(volts, basis). The user's sidecar figure always wins."""
    n = net_name or ""
    if voltages and n in voltages:
        return float(voltages[n]), "user-declared (sidecar voltages)"
    if _MAINS_RE.search(n):
        return 230.0, "heuristic: mains/AC net name -> 230 V (VERIFY THIS)"
    # NVN notation first: +3V3 = 3.3 V, +1V8 = 1.8 V (the bare _RAIL_RE would
    # read +3V3 as 3 V and under-estimate the rail).
    m = re.match(r"^\+?(\d+)V(\d+)$", n, re.I)
    if m:
        v = float(f"{m.group(1)}.{m.group(2)}")
        return v, f"heuristic: rail name -> {v:g} V"
    m = _RAIL_RE.match(n)
    if m:
        return float(m.group(1)), f"heuristic: rail name -> {m.group(1)} V"
    if n in ("VBUS",) or n.upper().startswith("USB"):
        return 5.0, "heuristic: USB net -> 5 V"
    if n.upper().startswith(("VIN", "VSUP")):
        return 12.0, "heuristic: supply input -> 12 V (conservative)"
    if n.upper().startswith("VBAT"):
        return 5.0, "heuristic: battery rail -> 5 V (1S Li conservative)"
    if n == "GND" or n.upper().endswith("GND"):
        return 0.0, "heuristic: ground -> 0 V"
    return 3.3, "heuristic: signal net -> 3.3 V (logic level)"


def required_clearance(volts, column="external"):
    """Minimum spacing in mm for a conductor at `volts` (IPC-2221 Table 6-1).

    Above 300 V this engine refuses to guess -- use the app's Electrical
    Spacing calculator (its >500 V slope handling) and declare the result.
    """
    col = _COLUMNS.get(column, 1)
    v = abs(float(volts))
    for v_max, row in _IPC2221_TABLE:
        if v <= v_max:
            return row[col]
    raise ValueError(
        f"{volts} V exceeds this engine's 300 V table -- consult the "
        "application's Electrical Spacing calculator and set the clearance "
        "explicitly."
    )


def clearance_between(v1, v2, column="external"):
    """Spacing between two nets: driven by the voltage DIFFERENCE between
    them (a 12 V rail beside a 12 V rail needs only low-voltage spacing;
    beside GND it needs the 12 V row)."""
    return required_clearance(abs(float(v1) - float(v2)), column=column)


def net_clearance_plan(net_names, voltages=None, column="external",
                       floor_mm=0.0):
    """Per-net clearance plan: {net: {volts, basis, clearance_mm}}.

    clearance is vs. 0 V (worst-case neighbor = ground); `floor_mm` lets the
    caller impose the manufacturer/class minimum so the plan never dips below
    what the fab can make.
    """
    plan = {}
    for n in net_names:
        volts, basis = estimate_net_voltage(n, voltages)
        c = required_clearance(volts, column=column)
        plan[n] = {
            "volts": volts,
            "basis": basis,
            "clearance_mm": max(c, floor_mm),
            "ipc_row_mm": c,
        }
    return plan
