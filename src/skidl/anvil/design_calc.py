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


# Commonly stocked capacitor values (C0G/X7R), farads -- used to pick a timing
# cap that lands the timing resistor in a practical 1k..1M window.
_STD_CAPS = [1e-9, 2.2e-9, 4.7e-9, 10e-9, 22e-9, 47e-9, 100e-9, 220e-9,
             470e-9, 1e-6, 2.2e-6, 4.7e-6, 10e-6, 22e-6, 47e-6, 100e-6]


def _pick_timing_cap(k_over_c, r_target=100000.0):
    """Pick a stock cap so the timing resistor R = k_over_c / C lands nearest
    r_target and, where possible, inside a practical 1k..1M window.
    k_over_c is the topology constant with R = k_over_c / C."""
    inside = [(abs(k_over_c / c - r_target), c) for c in _STD_CAPS
              if 1.0e3 <= k_over_c / c <= 1.0e6]
    pool = inside or [(abs(k_over_c / c - r_target), c) for c in _STD_CAPS]
    return min(pool)[1]


def ne555_astable(freq_hz, duty=0.60, c_farad=None):
    """Classic NE555 astable (R1: Vcc->DIS, R2: DIS->THR/TRG, C: THR->GND).

    f = 1.44 / ((R1 + 2*R2) * C);  duty = (R1 + R2) / (R1 + 2*R2).
    Solve R1,R2 for a target frequency + duty, given (or auto-picking) C:
        S = R1 + 2*R2 = 1.44/(f*C);  R1 = S*(2D-1);  R2 = S*(1-D).
    The bipolar topology's duty is ALWAYS > 50% (R1 > 0) -- for <=50% add a
    diode across R2 (charge path bypasses R1); that variant is NOT modelled.
    """
    f = float(freq_hz)
    d = float(duty)
    if f <= 0:
        raise ValueError("freq_hz must be > 0")
    if not (0.5 < d < 1.0):
        raise ValueError(
            f"duty {d} unreachable: the classic 555 astable spans >50%..<100% "
            "only. For <=50% add a diode across R2 (not modelled here).")
    s_const = 1.44 / f                      # = (R1 + 2*R2) * C
    c = float(c_farad) if c_farad else _pick_timing_cap(s_const, 100000.0)
    s = s_const / c                         # R1 + 2*R2
    r1 = e_series_nearest(s * (2 * d - 1))
    r2 = e_series_nearest(s * (1 - d))
    s_act = r1 + 2 * r2
    f_act = 1.44 / (s_act * c)
    d_act = (r1 + r2) / s_act
    return {
        "r1_ohm": r1, "r2_ohm": r2, "c_farad": c,
        "f_target_hz": round(f, 3), "f_actual_hz": round(f_act, 3),
        "duty_target": round(d, 4), "duty_actual": round(d_act, 4),
        "t_high_s": 0.693 * (r1 + r2) * c, "t_low_s": 0.693 * r2 * c,
        "formula": ("f = 1.44/((R1+2*R2)*C); duty=(R1+R2)/(R1+2*R2); "
                    f"R1=S*(2D-1), R2=S*(1-D), S=1.44/(f*C)=1.44/({f}*{c})"),
        "basis": "NE555 bipolar astable timing equations; datasheet wins",
    }


def ne555_monostable(pulse_s, c_farad=None):
    """NE555 monostable (one-shot): t = 1.1 * R * C. Solve R for a target
    pulse width, given (or auto-picking) the timing cap C."""
    t = float(pulse_s)
    if t <= 0:
        raise ValueError("pulse_s must be > 0")
    k = t / 1.1                             # = R * C
    c = float(c_farad) if c_farad else _pick_timing_cap(k, 100000.0)
    r = e_series_nearest(k / c)
    t_act = 1.1 * r * c
    return {
        "r_ohm": r, "c_farad": c,
        "pulse_target_s": round(t, 6), "pulse_actual_s": round(t_act, 6),
        "formula": f"t = 1.1*R*C  ->  R = t/(1.1*C) = {t}/(1.1*{c})",
        "basis": "NE555 monostable timing equation; datasheet wins",
    }


def buck_converter(v_in, v_out, i_out_a, f_sw_hz, ripple_ratio=0.3,
                   v_ripple_out=0.01):
    """Buck (step-down) power-stage sizing: inductor + output capacitor.

    D = Vout/Vin (ideal, losses ignored -- the IC datasheet's D wins);
    dIL = ripple_ratio * Iout (30 % is the standard starting point);
    L = Vout*(Vin - Vout) / (Vin * dIL * fsw);
    Cout >= dIL / (8 * fsw * dVout)   (capacitive ripple only; add ESR).

    Sizing guidance only -- the regulator datasheet's own component table
    ALWAYS wins (internal comp networks constrain L/C choice).
    """
    v_in, v_out, i_out = float(v_in), float(v_out), float(i_out_a)
    f_sw = float(f_sw_hz)
    if not (0 < v_out < v_in):
        raise ValueError(f"buck needs 0 < Vout ({v_out}) < Vin ({v_in})")
    if i_out <= 0 or f_sw <= 0:
        raise ValueError("i_out_a and f_sw_hz must be > 0")
    duty = v_out / v_in
    d_il = float(ripple_ratio) * i_out
    l_exact = v_out * (v_in - v_out) / (v_in * d_il * f_sw)
    l_std = e_series_nearest(l_exact, "E12")   # inductors stock in ~E12
    d_il_act = v_out * (v_in - v_out) / (v_in * l_std * f_sw)
    c_min = d_il_act / (8.0 * f_sw * float(v_ripple_out))
    return {
        "duty": round(duty, 3),
        "l_exact_h": l_exact, "l_standard_h": l_std,
        "i_ripple_a": round(d_il_act, 3),
        "i_l_peak_a": round(i_out + d_il_act / 2.0, 3),
        "i_l_sat_min_a": round(1.3 * (i_out + d_il_act / 2.0), 3),
        "c_out_min_f": c_min,
        "formula": ("D=Vout/Vin; L=Vout*(Vin-Vout)/(Vin*dIL*fsw), "
                    f"dIL={ripple_ratio}*Iout; Cout>=dIL/(8*fsw*dVout); "
                    "Isat >= 1.3*Ipeak"),
        "basis": ("standard buck CCM equations (e.g. TI SLVA477); the "
                  "regulator datasheet's component table wins"),
    }


def boost_converter(v_in, v_out, i_out_a, f_sw_hz, ripple_ratio=0.3,
                    v_ripple_out=0.01):
    """Boost (step-up) power-stage sizing: inductor + output capacitor.

    D = 1 - Vin/Vout (ideal); IL_avg = Iout/(1-D) -- the INPUT current;
    L = Vin*D / (dIL * fsw), dIL = ripple_ratio * IL_avg;
    Cout >= Iout*D / (fsw * dVout)  (output cap alone feeds the load
    during the ON phase -- boost needs much more C than a buck).
    """
    v_in, v_out, i_out = float(v_in), float(v_out), float(i_out_a)
    f_sw = float(f_sw_hz)
    if not (0 < v_in < v_out):
        raise ValueError(f"boost needs 0 < Vin ({v_in}) < Vout ({v_out})")
    if i_out <= 0 or f_sw <= 0:
        raise ValueError("i_out_a and f_sw_hz must be > 0")
    duty = 1.0 - v_in / v_out
    i_l_avg = i_out / (1.0 - duty)
    d_il = float(ripple_ratio) * i_l_avg
    l_exact = v_in * duty / (d_il * f_sw)
    l_std = e_series_nearest(l_exact, "E12")
    d_il_act = v_in * duty / (l_std * f_sw)
    c_min = i_out * duty / (f_sw * float(v_ripple_out))
    return {
        "duty": round(duty, 3),
        "l_exact_h": l_exact, "l_standard_h": l_std,
        "i_l_avg_a": round(i_l_avg, 3),
        "i_ripple_a": round(d_il_act, 3),
        "i_l_peak_a": round(i_l_avg + d_il_act / 2.0, 3),
        "i_l_sat_min_a": round(1.3 * (i_l_avg + d_il_act / 2.0), 3),
        "c_out_min_f": c_min,
        "formula": ("D=1-Vin/Vout; IL=Iout/(1-D); L=Vin*D/(dIL*fsw), "
                    f"dIL={ripple_ratio}*IL; Cout>=Iout*D/(fsw*dVout)"),
        "basis": ("standard boost CCM equations (e.g. TI SLVA372); input "
                  "current exceeds Iout -- rate the inductor for IL_peak"),
    }


def current_sense_shunt(i_max_a, v_sense_mv=50.0, derate=0.5):
    """Low-side/high-side sense shunt: R = Vsense/Imax, P = Imax^2 * R.

    v_sense_mv defaults to 50 mV full-scale (common amp input range --
    the sense amplifier's datasheet wins). The returned power rating is
    the dissipation divided by `derate` (0.5 = use a resistor rated at
    2x the worst-case dissipation, the normal derating practice).
    """
    i_max = float(i_max_a)
    if i_max <= 0:
        raise ValueError("i_max_a must be > 0")
    r_exact = (float(v_sense_mv) / 1000.0) / i_max
    r_std = e_series_nearest(r_exact)
    p_w = i_max * i_max * r_std
    v_actual_mv = r_std * i_max * 1000.0
    return {
        "r_exact_ohm": r_exact, "r_standard_ohm": r_std,
        "v_sense_actual_mv": round(v_actual_mv, 2),
        "p_dissipated_w": round(p_w, 4),
        "p_rating_min_w": round(p_w / float(derate), 3),
        "formula": (f"R = Vsense/Imax = {v_sense_mv}mV/{i_max}A; "
                    f"P = I^2*R; rating >= P/{derate}"),
        "basis": ("shunt sizing; full-scale Vsense from the sense "
                  "amplifier's datasheet; 2x power derating standard"),
    }
