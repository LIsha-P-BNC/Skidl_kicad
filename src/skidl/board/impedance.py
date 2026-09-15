# -*- coding: utf-8 -*-

"""
Controlled-impedance geometry estimates -- IPC-2141 closed-form
approximations for microstrip and edge-coupled differential pairs.

HONESTY CONTRACT: these are the published IPC-2141 APPROXIMATIONS (a few
percent error in their valid range, 0.1 < w/h < 2.0, er < 15). The
application's Transline calculator (full Hammerstad-Jensen model) and the
fab's own impedance tool are MORE accurate -- verify the final stackup
there. Every result says so.
"""

import math

_NOTE = ("IPC-2141 approximation -- verify with the application's Transline "
         "calculator (Hammerstad-Jensen) or the fab's impedance tool")


def microstrip_z0(w_mm, h_mm, t_mm=0.035, er=4.5):
    """Single-ended surface microstrip: Z0 = 87/sqrt(er+1.41) *
    ln(5.98h / (0.8w + t))."""
    return (87.0 / math.sqrt(er + 1.41)) * math.log(
        5.98 * h_mm / (0.8 * w_mm + t_mm))


def differential_z(w_mm, s_mm, h_mm, t_mm=0.035, er=4.5):
    """Edge-coupled microstrip pair:
    Zdiff = 2 * Z0 * (1 - 0.48 * exp(-0.96 * s/h))."""
    z0 = microstrip_z0(w_mm, h_mm, t_mm, er)
    return 2.0 * z0 * (1.0 - 0.48 * math.exp(-0.96 * s_mm / h_mm))


def usb_diff_pair_geometry(z_target=90.0, h_mm=0.15, t_mm=0.035, er=4.5,
                           s_over_w=1.5):
    """Solve trace width + gap for a differential target (USB = 90 ohm).

    h_mm = dielectric to the reference plane (typical 4-layer prepreg
    ~0.1-0.2 mm; the STACKUP decides -- ask the fab). Scans width with
    gap = s_over_w * width and returns the closest hit.
    """
    best = None
    w = 0.05
    while w <= 1.0:
        s = s_over_w * w
        z = differential_z(w, s, h_mm, t_mm, er)
        err = abs(z - z_target)
        if best is None or err < best["error_ohm"]:
            best = {"width_mm": round(w, 3), "gap_mm": round(s, 3),
                    "z_diff_ohm": round(z, 1), "error_ohm": round(err, 2)}
        w += 0.005
    best.update({
        "target_ohm": z_target, "h_mm": h_mm, "er": er,
        "formula": "Zdiff = 2*Z0*(1-0.48*exp(-0.96*s/h)); "
                   "Z0 = 87/sqrt(er+1.41)*ln(5.98h/(0.8w+t))",
        "basis": _NOTE,
    })
    return best
