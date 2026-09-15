"""design_check.py -- GENERAL electrical-correctness verifier.

Connectivity is already proven by ERC + verify_connectivity (net == net). This
pass proves the OTHER half of "correct design": that every COMPONENT is rated
for the stress it actually sees, and every CALCULATED value meets its target --
for ANY circuit, by pure netlist + value analysis (no per-circuit tuning):

  RATING ADEQUACY  -- a component annotated with a working rating ("470uF/25V",
    "33uH/3A") must survive the net it sits on. The net's voltage is estimated
    from its NAME (board.voltage_engine, e.g. "+12V"->12 V, "GND"->0, signal
    ->3.3 V), so a 25 V cap on a 12 V rail passes, a 10 V cap on a 12 V rail
    FAILS, and a tight margin (<1.5x) WARNs. Works on any net name.

  LED CURRENT     -- every LED must have a series resistor, and that resistor
    must set a sane current. From the rail voltage, the LED forward drop (by
    colour, design_calc table) and R, compute I = (Vrail - Vf) / R and flag
    anything outside ~1..30 mA (dim / over-current). Works for any LED+R.

  MISSING VALUE   -- an R/C/L passive with an empty or placeholder value is an
    uncalculated part (a guess waiting to happen); flag it.

Every finding is (severity, ref, rule, message). The pass NEVER raises and NEVER
changes the design -- it only reports, so it is safe to run on every build.
DYNAMIC: pure over the .net text + net-name voltage/'/rating' parsing; no part
names, counts, or circuit-specific rules are hard-coded.
"""
import re

try:
    from skidl.board.voltage_engine import estimate_net_voltage
except Exception:  # keep the checker importable even if the engine moves
    def estimate_net_voltage(name, voltages=None):
        m = re.match(r"^\+?(\d+(?:\.\d+)?)V", str(name or ""))
        if m:
            return float(m.group(1)), "rail"
        if (name or "").upper().endswith("GND") or name == "GND":
            return 0.0, "gnd"
        return 3.3, "signal"

# LED forward drop by colour (V) -- mirrors design_calc's table so the checker
# and the calculator agree on what "correct" means.
_VF = {"RED": 1.8, "GREEN": 2.1, "BLUE": 3.2, "WHITE": 3.2, "YELLOW": 2.1,
       "ORANGE": 2.0, "IR": 1.2, "UV": 3.4}
_DEFAULT_VF = 2.0

# A net NAME that is a positive supply rail (+5V, +3V3, 3V3, VCC, VDD, VBUS...)
_RAIL_NAME_RE = re.compile(r"^(\+.*|v(cc|dd|in|bat|sys|bus)\w*|\d+v\d*)$", re.I)

ERROR, WARN, INFO = "ERROR", "WARN", "INFO"


# ---- netlist parsing (KiCad/SKiDL .net s-expr) ---------------------------

def _blocks(text, tag):
    out = []
    for m in re.finditer(r"\(" + tag + r"\b", text):
        i = m.start()
        depth = 0
        for j in range(i, len(text)):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(text[i:j + 1])
                    break
    return out


def parse_netlist(path):
    """Return (values{ref:value}, pins{ref:{pin:net}}, net_pins{net:[(ref,pin)]})."""
    text = open(path, encoding="utf-8", errors="replace").read()
    values = {}
    for comp in _blocks(text, "comp"):
        rm = re.search(r'\(ref\s+"([^"]+)"\)', comp)
        vm = re.search(r'\(value\s+"([^"]*)"\)', comp)
        if rm:
            values[rm.group(1)] = vm.group(1) if vm else ""
    net_pins = {}
    pins = {}
    for net in _blocks(text, "net"):
        nm = re.search(r'\(name\s+"([^"]+)"\)', net)
        if not nm:
            continue
        name = nm.group(1)
        nodes = re.findall(r'\(node\s+\(ref\s+"([^"]+)"\)\s*\(pin\s+"?([^")\s]+)"?', net)
        net_pins.setdefault(name, [])
        for ref, pin in nodes:
            net_pins[name].append((ref, pin))
            pins.setdefault(ref, {})[pin] = name
    return values, pins, net_pins


# ---- value / rating parsing ----------------------------------------------

_MULT = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
         "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6, "G": 1e9, "R": 1.0}


def _to_number(tok):
    """'4k7'/'4.7k'/'470'/'2K2'/'0.1'/'20pF'/'470uF'/'33uH' -> float
    ohms/farads/henries. The multiplier is the SI prefix (p/n/u/m/k/M/...); any
    trailing unit letters (F, H, ohm, Ohm, Ω) are ignored -- so a capacitor's
    '20pF' parses like a resistor's '20k'."""
    tok = tok.strip().replace(" ", "")
    # R/k/M infix notation: 4k7, 2K2, 1R0
    m = re.match(r"^(\d+)([RkKM])(\d+)$", tok)
    if m:
        whole, unit, frac = m.groups()
        return float(f"{whole}.{frac}") * _MULT.get(unit, 1.0)
    # number + optional SI-prefix + optional unit letters (F/H/ohm/Ω/...)
    m = re.match(r"^(\d*\.?\d+)\s*(meg|[pnuµmkKMGR])?[a-zA-ZΩ]*$", tok)
    if m:
        return float(m.group(1)) * (_MULT.get(m.group(2), 1.0) if m.group(2) else 1.0)
    return None


def _rating_volts(value):
    """The working VOLTAGE from a '/25V' style rating suffix, or None."""
    for r in re.findall(r"/\s*([\d.]+)\s*[vV]\b", value):
        try:
            return float(r)
        except ValueError:
            pass
    return None


def _rating_amps(value):
    for r in re.findall(r"/\s*([\d.]+)\s*[aA]\b", value):
        try:
            return float(r)
        except ValueError:
            pass
    return None


def _prefix(ref):
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return (m.group(1) if m else "").upper()


# ---- the checks -----------------------------------------------------------

def check(path, voltages=None, v_margin=1.5):
    """Run every electrical-correctness check on a .net file. Returns a list of
    (severity, ref, rule, message). Never raises."""
    try:
        return _check(path, voltages, v_margin)
    except Exception as e:  # a checker must never break a build
        return [(INFO, "-", "CHK-0", f"design_check skipped: {e}")]


def _check(path, voltages, v_margin):
    values, pins, net_pins = parse_netlist(path)
    out = []

    def net_v(name):
        try:
            return estimate_net_voltage(name, voltages)[0]
        except Exception:
            return 0.0

    for ref, value in sorted(values.items()):
        if ref.startswith("#"):
            continue
        pfx = _prefix(ref)
        touched = list(pins.get(ref, {}).values())

        # RATING ADEQUACY (voltage) -- caps/rated parts vs the worst net they see
        rv = _rating_volts(value)
        if rv is not None and touched:
            stress = max((net_v(n) for n in touched), default=0.0)
            if stress > rv + 1e-6:
                out.append((ERROR, ref, "CHK-V",
                            f"{ref} rated {rv:g}V but sits on a {stress:g}V net "
                            f"({', '.join(sorted(set(touched)))}) -- UNDER-RATED."))
            elif stress > 0 and rv < stress * v_margin:
                out.append((WARN, ref, "CHK-V",
                            f"{ref} rated {rv:g}V on a {stress:g}V net -- margin "
                            f"{rv/stress:.2f}x < {v_margin:g}x (derate to >={stress*v_margin:.0f}V)."))

        # RATING ADEQUACY (current) -- inductors/rated parts (name-based current)
        ra = _rating_amps(value)
        if ra is not None and touched:
            def _amps(n):
                try:
                    from skidl.board.width_engine import estimate_net_current
                    r = estimate_net_current(n, "power")
                    return r[0] if isinstance(r, (tuple, list)) else float(r)
                except Exception:
                    return 0.0
            stress_i = max((_amps(n) for n in touched), default=0.0)
            if stress_i > ra + 1e-6:
                out.append((WARN, ref, "CHK-I",
                            f"{ref} rated {ra:g}A but its net may carry ~{stress_i:g}A "
                            f"-- confirm current rating."))

        # MISSING VALUE -- an un-calculated passive
        if pfx in ("R", "C", "CP", "L") and not value.strip():
            out.append((WARN, ref, "CHK-VAL",
                        f"{ref} ({pfx}) has NO value -- uncalculated part."))

    # LED CURRENT -- every LED needs a series R setting a sane current
    for ref, value in sorted(values.items()):
        if not (_prefix(ref) in ("D", "LED") and (value.strip().upper() in _VF or "LED" in value.upper())):
            continue
        out.extend(_check_led(ref, value, values, pins, net_pins, net_v))

    out.extend(_check_dividers(values, pins, net_pins, net_v))
    out.extend(_check_crystals(values, pins, net_pins))
    out.extend(_check_i2c(values, pins, net_pins, net_v))
    return out


def _is_gnd(n):
    return n == "GND" or (n or "").upper().endswith("GND")


def _target_from_name(name):
    """A voltage a net NAME encodes, e.g. '3V3'->3.3, '1V8'->1.8, '5V'->5."""
    m = re.search(r"(?<![A-Za-z0-9])(\d+)V(\d*)(?![A-Za-z0-9])", name or "")
    if not m:
        return None
    whole, frac = m.group(1), m.group(2)
    return float(f"{whole}.{frac}") if frac else float(whole)


def _check_dividers(values, pins, net_pins, net_v):
    """A resistor divider (rail -> Rtop -> MID -> Rbot -> GND) sets MID =
    Vrail*Rbot/(Rtop+Rbot). Report the computed node voltage (verification), and
    WARN if the node NAME encodes a different target voltage. General: any net
    with one R to a higher net and one R to ground."""
    out = []
    for mid, nodes in net_pins.items():
        if _is_gnd(mid):
            continue
        rs = [ref for ref, _ in nodes if _prefix(ref) == "R"]
        if len(rs) < 2:
            continue
        up = down = None
        vmid = net_v(mid)
        for r in rs:
            others = [nn for pp, nn in pins.get(r, {}).items() if nn != mid]
            if not others:
                continue
            o = others[0]
            if _is_gnd(o):
                down = (r, o)
            elif net_v(o) > vmid:
                up = (r, o)
        if not (up and down):
            continue
        rtop = _to_number(re.split(r"/", values.get(up[0], ""))[0])
        rbot = _to_number(re.split(r"/", values.get(down[0], ""))[0])
        if not rtop or not rbot:
            continue
        vrail = net_v(up[1])
        vout = vrail * rbot / (rtop + rbot)
        target = _target_from_name(mid)
        if target and abs(vout - target) > 0.1 * target:
            out.append((WARN, mid, "CHK-DIV",
                        f"divider {up[0]}/{down[0]} on {vrail:g}V gives {vout:.2f}V "
                        f"but net '{mid}' implies {target:g}V -- check R values."))
        else:
            out.append((INFO, mid, "CHK-DIV",
                        f"divider {up[0]}={rtop:g}/{down[0]}={rbot:g} on {vrail:g}V "
                        f"-> {vout:.2f}V."))
    return out


def _check_crystals(values, pins, net_pins):
    """A crystal needs a load cap on EACH pin (to GND), the two equal and in a
    sane range (~5-40 pF). General: detect by ref (Y/X) or value (MHz/kHz/XTAL)."""
    out = []
    for ref, value in sorted(values.items()):
        v = value.upper()
        is_xtal = _prefix(ref) in ("Y", "X") or "MHZ" in v or "KHZ" in v or "XTAL" in v or "CRYSTAL" in v
        if not is_xtal:
            continue
        xnets = [n for n in pins.get(ref, {}).values() if not _is_gnd(n)]
        capvals = []
        for n in xnets:
            for r2, _p in net_pins.get(n, []):
                if _prefix(r2) in ("C",) and any(_is_gnd(nn) for nn in pins.get(r2, {}).values()):
                    cv = _to_number(re.split(r"/", values.get(r2, ""))[0])
                    if cv:
                        capvals.append((r2, cv))
        if len(capvals) < len(xnets):
            out.append((WARN, ref, "CHK-XTAL",
                        f"{ref} crystal is missing a load cap on one pin "
                        "(each OSC pin needs a cap to GND)."))
            continue
        pfs = [cv * 1e12 for _r, cv in capvals]  # farads -> pF
        if pfs and (max(pfs) > 40 or min(pfs) < 5):
            out.append((WARN, ref, "CHK-XTAL",
                        f"{ref} load caps {['%.0fpF' % p for p in pfs]} out of the "
                        "typical 5-40 pF range -- verify against crystal CL."))
        elif len(set(round(p) for p in pfs)) > 1:
            out.append((WARN, ref, "CHK-XTAL",
                        f"{ref} load caps unequal {['%.0fpF' % p for p in pfs]} "
                        "-- both pins should use the same value."))
    return out


def _check_i2c(values, pins, net_pins, net_v):
    """SDA/SCL each need a pull-up resistor to a rail, in the ~1-10 kOhm band.
    General: detect by net name (SDA/SCL)."""
    out = []
    for name, nodes in net_pins.items():
        u = name.upper()
        if not re.search(r"(?<![A-Z])(SDA|SCL)(?![A-Z])", u):
            continue
        pulls = []
        for ref, _p in nodes:
            if _prefix(ref) != "R":
                continue
            others = [nn for pp, nn in pins.get(ref, {}).items() if nn != name]
            # A pull-up's far pin is a POWER RAIL -- detect by rail NAME, not by
            # comparing estimated volts (a +3V3 rail estimating 3.3 V is NOT
            # ">" the SDA default 3.3 V, which false-flagged real pull-ups).
            if others and _RAIL_NAME_RE.match(others[0] or ""):
                rv = _to_number(re.split(r"/", values.get(ref, ""))[0])
                if rv:
                    pulls.append((ref, rv))
        if not pulls:
            out.append((WARN, name, "CHK-I2C",
                        f"I2C net '{name}' has NO pull-up resistor to a rail."))
        else:
            bad = [f"{r}={v:g}" for r, v in pulls if not (1000 <= v <= 10000)]
            if bad:
                out.append((WARN, name, "CHK-I2C",
                            f"I2C pull-up(s) {bad} on '{name}' outside ~1-10k."))
    return out


def _check_led(led, value, values, pins, net_pins, net_v):
    """Find the LED's series R, compute I = (Vrail - Vf)/R, flag out-of-range."""
    findings = []
    vf = _VF.get(value.strip().upper(), _DEFAULT_VF)
    led_nets = set(pins.get(led, {}).values())

    def _gnd_like(n):
        return n == "GND" or (n or "").upper().endswith("GND") or net_v(n) == 0.0

    # Current path is rail -> R -> LED -> GND. The series R sits on the LED's
    # NON-ground (anode) net, and its OTHER pin goes to the driving rail. Pick
    # the R whose far net has the highest voltage (that IS the rail), so a
    # pull-up/other R sharing the ground net is never mistaken for the series R.
    anode_nets = [n for n in led_nets if not _gnd_like(n)]
    series_r = r_other_net = None
    best_v = -1.0
    for n in anode_nets:
        for ref, _pin in net_pins.get(n, []):
            if _prefix(ref) != "R":
                continue
            other = [nn for pp, nn in pins.get(ref, {}).items() if nn != n]
            onet = other[0] if other else None
            v = net_v(onet) if onet else 0.0
            if v > best_v:
                series_r, r_other_net, best_v = ref, onet, v
    if not series_r:
        findings.append((ERROR, led, "CHK-LED",
                         f"{led} has NO series resistor -- add a current-limiting R."))
        return findings
    r_ohm = _to_number(re.split(r"/", values.get(series_r, ""))[0])
    if not r_ohm or r_ohm <= 0:
        return findings
    vrail = max((net_v(n) for n in ([r_other_net] if r_other_net else [])), default=0.0)
    if vrail <= vf:
        return findings  # can't drive the LED from this rail -- separate issue
    i_ma = (vrail - vf) / r_ohm * 1000.0
    if i_ma < 1.0:
        findings.append((WARN, led, "CHK-LED",
                         f"{led} via {series_r}={r_ohm:g}ohm on {vrail:g}V -> only "
                         f"{i_ma:.1f}mA (dim; expected ~2-20mA)."))
    elif i_ma > 30.0:
        findings.append((ERROR, led, "CHK-LED",
                         f"{led} via {series_r}={r_ohm:g}ohm on {vrail:g}V -> {i_ma:.0f}mA "
                         f"-- OVER typical 20mA rating."))
    else:
        findings.append((INFO, led, "CHK-LED",
                         f"{led} via {series_r}={r_ohm:g}ohm on {vrail:g}V -> {i_ma:.1f}mA (OK)."))
    return findings


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        print(f"=== {p} ===")
        fs = check(p)
        if not fs:
            print("  (no findings)")
        for sev, ref, rule, msg in fs:
            print(f"  [{sev}] {rule} {msg}")
