# -*- coding: utf-8 -*-

"""
Schematic-side DESIGN VALUE calculations -- the authoring-time twin of the
board package's width_engine/voltage_engine.

HONESTY CONTRACT: every result carries the formula and its inputs, so the
value can be checked by hand (or against the application's own Calculator
tools where one exists, e.g. the E-series panel).

These are the calculations an engineer does while WRITING the circuit:
LED series resistors, dividers, crystal load caps (AN2867), pull-ups,
RC filters -- with results rounded to standard E-series values.
"""

# E-series bases (multiplied across decades).
_E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
_E24 = [1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
        3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1]

# Typical LED forward voltages by color (volts) -- datasheet always wins.
LED_VF = {"RED": 1.8, "GREEN": 2.1, "YELLOW": 2.0, "ORANGE": 2.0,
          "BLUE": 3.0, "WHITE": 3.0, "IR": 1.2, "UV": 3.3}


def e_series_nearest(value, series="E24"):
    """Nearest standard value from the E-series (any decade)."""
    import math
    base = _E24 if series == "E24" else _E12
    if value <= 0:
        return value
    decade = 10 ** math.floor(math.log10(value))
    candidates = [b * decade for b in base] + [base[0] * decade * 10]
    return min(candidates, key=lambda c: abs(c - value))


def led_series_resistor(v_supply, i_ma=2.0, color="GREEN", v_forward=None):
    """Series resistor for an LED: R = (Vsupply - Vf) / I.

    Default 2 mA suits modern high-efficiency indicators; power/legacy LEDs
    may want 5-20 mA -- the datasheet decides.
    """
    vf = float(v_forward) if v_forward is not None else LED_VF.get(
        str(color).upper(), 2.0)
    basis = ("datasheet Vf" if v_forward is not None
             else f"typical Vf for {color} ({vf} V) -- datasheet wins")
    i_a = float(i_ma) / 1000.0
    r_exact = (float(v_supply) - vf) / i_a
    r_std = e_series_nearest(r_exact)
    i_actual_ma = (float(v_supply) - vf) / r_std * 1000.0
    return {
        "r_exact_ohm": round(r_exact, 1),
        "r_standard_ohm": r_std,
        "i_actual_ma": round(i_actual_ma, 2),
        "formula": f"R = (Vs - Vf)/I = ({v_supply} - {vf})/{i_ma}mA",
        "basis": basis,
    }


def crystal_load_caps(crystal_cl_pf, stray_pf=5.0):
    """AN2867: CL = (C1*C2)/(C1+C2) + Cstray; with C1 = C2:
    C1 = C2 = 2*(CL - Cstray). Stray default 5 pF (pin + PCB)."""
    c = 2.0 * (float(crystal_cl_pf) - float(stray_pf))
    if c <= 0:
        raise ValueError(
            f"crystal CL {crystal_cl_pf} pF <= stray {stray_pf} pF -- "
            "pick a higher-CL crystal or reduce assumed stray.")
    c_std = e_series_nearest(c, "E24")  # 20/22/24 pF etc. are stock C0G values
    return {
        "c_each_exact_pf": round(c, 1),
        "c_each_standard_pf": c_std,
        "formula": f"C1=C2 = 2*(CL - Cstray) = 2*({crystal_cl_pf} - {stray_pf})",
        "basis": "AN2867 load-capacitance equation; stray = pins + traces",
    }


def voltage_divider(v_in, v_out, r_bottom=10000.0):
    """R_top for Vout = Vin * Rb/(Rt+Rb): R_top = Rb*(Vin - Vout)/Vout."""
    r_top = float(r_bottom) * (float(v_in) - float(v_out)) / float(v_out)
    r_std = e_series_nearest(r_top)
    v_actual = float(v_in) * float(r_bottom) / (r_std + float(r_bottom))
    return {
        "r_top_exact_ohm": round(r_top, 1),
        "r_top_standard_ohm": r_std,
        "r_bottom_ohm": float(r_bottom),
        "v_out_actual": round(v_actual, 3),
        "formula": f"Rt = Rb*(Vin-Vout)/Vout = {r_bottom}*({v_in}-{v_out})/{v_out}",
        "basis": "resistive divider; check loading vs the tap's input impedance",
    }


def i2c_pullup(v_rail, bus_cap_pf=100.0, mode="standard"):
    """I2C pull-up window: Rmin from VOL/IOL sink limit, Rmax from the
    rise-time budget (t_r = 0.8473 * R * Cb per the I2C spec)."""
    t_r_ns = {"standard": 1000.0, "fast": 300.0, "fast_plus": 120.0}[mode]
    r_min = (float(v_rail) - 0.4) / 0.003          # VOL 0.4 V @ IOL 3 mA
    r_max = (t_r_ns * 1e-9) / (0.8473 * float(bus_cap_pf) * 1e-12)
    r_pick = e_series_nearest((r_min * r_max) ** 0.5)
    r_pick = min(max(r_pick, e_series_nearest(r_min)), e_series_nearest(r_max))
    return {
        "r_min_ohm": round(r_min, 0),
        "r_max_ohm": round(r_max, 0),
        "r_pick_standard_ohm": r_pick,
        "formula": ("Rmin=(Vdd-0.4)/3mA; Rmax=t_r/(0.8473*Cb); "
                    f"t_r={t_r_ns}ns, Cb={bus_cap_pf}pF"),
        "basis": f"I2C {mode}-mode spec limits",
    }


def regulator_feedback_divider(v_out, v_ref=1.25, r_bottom=240.0):
    """Adjustable-regulator feedback pair (LM317/AMS1117-ADJ family):
    Vout = Vref * (1 + R2/R1)  ->  R2 = R1 * (Vout/Vref - 1).

    r_bottom (R1, VREF->ADJ) defaults to 240R per the LM317 datasheet's
    minimum-load recommendation; the regulator's own datasheet wins.
    Mirrors the application's Calculator > Regulators panel.
    """
    if v_out <= v_ref:
        raise ValueError(f"Vout {v_out} V must exceed Vref {v_ref} V")
    r_top = float(r_bottom) * (float(v_out) / float(v_ref) - 1.0)
    r_std = e_series_nearest(r_top)
    v_actual = float(v_ref) * (1.0 + r_std / float(r_bottom))
    return {
        "r1_bottom_ohm": float(r_bottom),
        "r2_top_exact_ohm": round(r_top, 1),
        "r2_top_standard_ohm": r_std,
        "v_out_actual": round(v_actual, 3),
        "formula": f"R2 = R1*(Vout/Vref - 1) = {r_bottom}*({v_out}/{v_ref} - 1)",
        "basis": "adjustable-regulator feedback equation; Vref from datasheet",
    }


def rc_cutoff(r_ohm=None, c_farad=None, fc_hz=None):
    """First-order RC: fc = 1/(2*pi*R*C) -- give any two, get the third."""
    import math
    given = [x is not None for x in (r_ohm, c_farad, fc_hz)]
    if sum(given) != 2:
        raise ValueError("provide exactly two of r_ohm / c_farad / fc_hz")
    if fc_hz is None:
        fc_hz = 1.0 / (2 * math.pi * r_ohm * c_farad)
        out = {"fc_hz": round(fc_hz, 2)}
    elif r_ohm is None:
        r_ohm = 1.0 / (2 * math.pi * fc_hz * c_farad)
        out = {"r_ohm": round(r_ohm, 1),
               "r_standard_ohm": e_series_nearest(r_ohm)}
    else:
        c_farad = 1.0 / (2 * math.pi * fc_hz * r_ohm)
        out = {"c_farad": c_farad}
    out["formula"] = "fc = 1/(2*pi*R*C)"
    out["basis"] = "first-order RC corner"
    return out
