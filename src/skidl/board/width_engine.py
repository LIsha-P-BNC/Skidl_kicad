"""
src/skidl/board/width_engine.py

IPC-2152-informed trace-width planning: estimated current -> allowed
temperature rise -> copper weight -> required width, rounded UP to
0.05mm and floored at the fab minimum.

HONESTY CONTRACT: every current is either the user's own number
(sidecar "currents": {net: amps}) or a clearly-labeled heuristic; the
whole plan is an ADVISORY ESTIMATE reported as such -- never presented
as verified capacity. The width formula is the widely published
IPC-2221 fitted curve (the public stand-in for the IPC-2152 charts):

    area_mil2 = (I / (k * dT^0.44)) ^ (1/0.725)
    width_mil = area_mil2 / (1.378 * oz)
    k = 0.048 external layers, 0.024 internal
"""

from __future__ import annotations
import re

# Heuristic per-net current guesses (amps) with WHY strings.
_MOTOR_RE = re.compile(r"(MOTOR|VM\b|PHASE|COIL|SOLENOID|PUMP)", re.I)
_VIN_RE = re.compile(r"(^|_)(VIN|VBAT|VBUS|VSUP|12V|24V|\+12|\+24)", re.I)


def estimate_net_current(net_name: str, net_class: str,
                         currents: dict = None) -> tuple:
    """(amps, basis). The user's sidecar figure always wins."""
    if currents and net_name in currents:
        return float(currents[net_name]), "user-declared (sidecar currents)"
    if _MOTOR_RE.search(net_name or ""):
        return 2.0, "heuristic: motor/coil net name -> 2.0 A"
    if _VIN_RE.search(net_name or ""):
        return 1.5, "heuristic: supply-input net name -> 1.5 A"
    if net_class in ("Power",) or (net_name or "").startswith("+"):
        return 1.0, "heuristic: power-class rail -> 1.0 A"
    return 0.1, "heuristic: signal net -> 0.1 A"


def required_width(amps: float, delta_t: float = 10.0, copper_oz: float = 1.0,
                   internal: bool = False) -> float:
    """Required trace width in mm for `amps` at `delta_t` degC rise --
    IPC-2221 fitted curve (public IPC-2152 stand-in). Estimate only."""
    if amps <= 0:
        return 0.0
    k = 0.024 if internal else 0.048
    area_mil2 = (amps / (k * (delta_t ** 0.44))) ** (1.0 / 0.725)
    width_mil = area_mil2 / (1.378 * copper_oz)
    return width_mil * 0.0254


def net_width_plan(net_names_by_class: dict, profile: dict,
                   currents: dict = None, delta_t: float = 10.0) -> dict:
    """Per-net width plan: {net: {amps, basis, width_mm}}. Only nets
    whose required width EXCEEDS their class width appear -- the rest
    are already covered. Widths round up to 0.05mm, floored at the fab
    minimum track."""
    copper_oz = profile.get("copper_oz", 1)
    fab_min = profile.get("rules", {}).get("min_track_width", 0.127)
    classes = profile.get("classes", {})
    plan = {}
    _ground_re = re.compile(r"^(GND\w*|AGND|DGND|PGND|VSS\w*|EARTH)$", re.I)
    for cls, nets in (net_names_by_class or {}).items():
        cls_width = (classes.get(cls) or {}).get("width", 0.25)
        for net in nets:
            if _ground_re.match(net or ""):
                continue    # plane-served; width plan is meaningless
            amps, basis = estimate_net_current(net, cls, currents)
            w = required_width(amps, delta_t=delta_t, copper_oz=copper_oz)
            w = max(w, fab_min)
            w = round(((w * 20) + 0.999) // 1 / 20, 3)   # ceil to 0.05
            if w > cls_width + 1e-9:
                plan[net] = {"amps": amps, "basis": basis, "width_mm": w,
                             "class": cls, "class_width_mm": cls_width}
    return plan


def bucket_classes(plan: dict, max_buckets: int = 4) -> dict:
    """Group planned widths into synthesized net classes (PWR_0.4MM
    style) so the DSN per-class rules carry them to the router with no
    router changes. Returns {class_name: {"width": w, "nets": [...]}}
    capped at max_buckets widest-first."""
    widths = sorted({v["width_mm"] for v in plan.values()}, reverse=True)
    widths = widths[:max_buckets]
    out = {}
    for net, v in plan.items():
        # Assign to the smallest bucket >= required width; nets whose
        # width exceeded every kept bucket use the widest bucket.
        cands = [w for w in widths if w >= v["width_mm"]]
        w = min(cands) if cands else max(widths)
        name = f"PWR_{str(w).replace('.', '_')}MM"
        out.setdefault(name, {"width": w, "nets": []})["nets"].append(net)
    return out
