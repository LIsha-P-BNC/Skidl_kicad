# anvil_category.py -- classify an AI-created part into one of the EXISTING
# library categories (the stock library/.pretty/.3dshapes names), so a new part
# lands beside its peers instead of in a synthetic "UserParts" bin.
#
# Design:
#   * The valid category set (the "universe") is discovered at runtime from the
#     installed library tree -- never hard-coded, so it tracks whatever ships.
#   * classify() picks from that universe only. It can NEVER invent a folder the
#     libraries do not already have; worst case it returns the per-kind default.
#   * A caller hint (the model's own guess) wins when it names a real category.
#   * Rules are hints that are themselves validated against the universe, so a
#     rule pointing at a category this install lacks is ignored, not trusted.
#
# stdlib only.

import os
import re
import glob

# --------------------------------------------------------------------------
# per-kind defaults + where categories physically live
# --------------------------------------------------------------------------

# kind -> (default category when nothing matches, glob for category names,
#          how to turn a path into the bare category name)
_KIND_META = {
    "symbol":    ("Device",         ("*.anvil_sym", "*.kicad_sym")),
    "footprint": ("Connector",      ("*.pretty",)),
    "model3d":   ("Connector",      ("*.3dshapes",)),
}


def _strip_ext(base):
    for e in (".anvil_sym", ".kicad_sym", ".pretty", ".3dshapes", ".kicad_symdir"):
        if base.endswith(e):
            return base[: -len(e)]
    return base


def _candidate_dirs(kind):
    """Directories that may hold the category libraries for this kind, most
    authoritative first. Resolved from the same env the app/SKiDL already set."""
    dirs = []
    shared = os.environ.get("ANVIL_SHARED_LIB_DIR")
    sub = {"symbol": "symbols", "footprint": "footprints", "model3d": "3dmodels"}[kind]
    if shared:
        dirs.append(os.path.join(shared, sub))
    # The install tree the AI backend already points at (anvil_libs sets these).
    if kind == "symbol":
        for v in ("KICAD9_SYMBOL_DIR", "KICAD8_SYMBOL_DIR", "ANVIL1_SYMBOL_DIR"):
            if os.environ.get(v):
                dirs.append(os.environ[v])
    elif kind == "footprint":
        for v in ("KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "ANVIL1_FOOTPRINT_DIR"):
            if os.environ.get(v):
                dirs.append(os.environ[v])
    else:
        for v in ("KICAD9_3DMODEL_DIR", "ANVIL1_3DMODEL_DIR"):
            if os.environ.get(v):
                dirs.append(os.environ[v])
    return [d for d in dirs if d and os.path.isdir(d)]


_universe_cache = {}


def load_universe(kind):
    """Set of real category names for this kind on this machine. Cached."""
    if kind in _universe_cache:
        return _universe_cache[kind]
    _default, globs = _KIND_META[kind]
    names = set()
    for d in _candidate_dirs(kind):
        for g in globs:
            for p in glob.glob(os.path.join(d, g)):
                names.add(_strip_ext(os.path.basename(p)))
    _universe_cache[kind] = names
    return names


def reset_cache():
    _universe_cache.clear()


# --------------------------------------------------------------------------
# keyword rules -- ordered, first hit wins; each result is validated against
# the universe before use. Keep specific patterns ABOVE generic ones.
# --------------------------------------------------------------------------

# (regex over the lowercased haystack, category name)
_RULES = [
    # --- MCUs / processors (specific families first) ---
    (r"\bstm32f0", "MCU_ST_STM32F0"), (r"\bstm32f1", "MCU_ST_STM32F1"),
    (r"\bstm32f2", "MCU_ST_STM32F2"), (r"\bstm32f3", "MCU_ST_STM32F3"),
    (r"\bstm32f4", "MCU_ST_STM32F4"), (r"\bstm32f7", "MCU_ST_STM32F7"),
    (r"\bstm32h7", "MCU_ST_STM32H7"), (r"\bstm32l0", "MCU_ST_STM32L0"),
    (r"\bstm32l1", "MCU_ST_STM32L1"), (r"\bstm32l4", "MCU_ST_STM32L4"),
    (r"\bstm32", "MCU_ST_STM32F1"), (r"\bstm8", "MCU_ST_STM8"),
    (r"\besp32|\besp8266|esp32|esp-", "MCU_Espressif"),
    (r"\batmega", "MCU_Microchip_ATmega"), (r"\battiny", "MCU_Microchip_ATtiny"),
    (r"\bpic32", "MCU_Microchip_PIC32"), (r"\bpic18", "MCU_Microchip_PIC18"),
    (r"\bpic16", "MCU_Microchip_PIC16"),
    (r"\brp2040|\braspberry pi pico", "MCU_Module"),
    (r"\bnrf5|\bnordic", "MCU_Nordic"),
    (r"msp430", "MCU_Texas_MSP430"),
    # --- battery / power management (your test family) ---
    (r"\bbms\b|battery monitor|battery management|cell balanc|fuel gauge|"
     r"coulomb|gas gauge|bq769|bq76|bq40|bq27", "Battery_Management"),
    (r"\bpmic\b|power management ic|load switch|power path", "Power_Management"),
    (r"supervisor|voltage monitor|reset ic|watchdog ic|brown.?out", "Power_Supervisor"),
    (r"\besd\b|tvs|surge|overvoltage prot|ovp\b", "Power_Protection"),
    # --- regulators / converters ---
    (r"\bldo\b|low.?dropout|linear regulator|ams1117|lm1117|lp2985|\b1117\b",
     "Regulator_Linear"),
    (r"buck|boost|switching regulator|step.?down|step.?up|\bsmps\b", "Regulator_Switching"),
    (r"dc.?dc|isolated converter", "Converter_DCDC"),
    (r"ac.?dc|offline converter|flyback controller", "Converter_ACDC"),
    (r"charge pump|switched.?cap", "Regulator_SwitchedCapacitor"),
    (r"voltage reference|\bvref\b|bandgap", "Reference_Voltage"),
    # --- amplifiers / analog ---
    (r"op.?amp|operational amplifier", "Amplifier_Operational"),
    (r"instrumentation amp", "Amplifier_Instrumentation"),
    (r"audio amp|class.?d amp", "Amplifier_Audio"),
    (r"comparator", "Comparator"),
    (r"\badc\b|analog.?to.?digital", "Analog_ADC"),
    (r"\bdac\b|digital.?to.?analog", "Analog_DAC"),
    (r"analog switch|analog mux", "Analog_Switch"),
    # --- discretes ---
    (r"mosfet|\bnmos\b|\bpmos\b|\bn-?channel\b|\bp-?channel\b|\bfet\b|ao3400|irlml",
     "Transistor_FET"),
    (r"\bigbt\b", "Transistor_IGBT"),
    (r"\bnpn\b|\bpnp\b|\bbjt\b|bipolar|2n2222|bc547|mmbt", "Transistor_BJT"),
    (r"\btriac\b|thyristor|\bscr\b", "Triac_Thyristor"),
    (r"schottky|zener|rectifier|\b1n4|\b1n5|diode", "Diode"),
    (r"\bbridge rectifier", "Diode_Bridge"),
    (r"\bled\b|light emitting", "LED"),
    # --- memory ---
    (r"eeprom|24c\d|at24", "Memory_EEPROM"),
    (r"\bnor flash|spi flash|w25q|25q\d|serial flash", "Memory_Flash"),
    (r"\bsram\b|\bpsram\b", "Memory_RAM"),
    # --- interface / comms ---
    (r"\busb\b|usb-c|usb hub|usb transceiver", "Interface_USB"),
    (r"\bcan\b|\blin\b|can transceiver", "Interface_CAN_LIN"),
    (r"rs.?232|rs.?485|\buart\b|max232", "Interface_UART"),
    (r"ethernet|\bphy\b|rmii|rgmii", "Interface_Ethernet"),
    (r"level shifter|level translat", "Logic_LevelTranslator"),
    (r"\bi2c expander|gpio expander", "Interface_Expansion"),
    (r"opto.?couple|opto.?isolat|optocoupler", "Isolator"),
    # --- RF / wireless ---
    (r"\bwifi\b|wi-fi|802\.11", "RF_WiFi"),
    (r"bluetooth|\bble\b", "RF_Bluetooth"),
    (r"\bgps\b|gnss", "RF_GPS"),
    (r"lora|zigbee", "RF_Module"),
    # --- sensors ---
    (r"temperature sensor|thermistor|rtd|\bntc\b", "Sensor_Temperature"),
    (r"accelerat|gyro|\bimu\b|magnetometer", "Sensor_Motion"),
    (r"hall|magnetic sensor", "Sensor_Magnetic"),
    (r"humidity", "Sensor_Humidity"),
    (r"pressure sensor|barometer", "Sensor_Pressure"),
    (r"current sensor|current monitor|\bina\d", "Sensor_Current"),
    (r"proximity|\btof\b|time.?of.?flight", "Sensor_Proximity"),
    # --- timing / misc ---
    (r"\brtc\b|real.?time clock", "Timer_RTC"),
    (r"\bpll\b", "Timer_PLL"),
    (r"\b555\b|\btimer\b", "Timer"),
    (r"oscillator|crystal|\bxtal\b|\btcxo\b|resonator", "Oscillator"),
    (r"optocoupler|isolator|digital isolat", "Isolator"),
    (r"relay", "Relay"),
    (r"\bfpga\b", "FPGA_Xilinx"),
    (r"\bcpld\b", "CPLD_Xilinx"),
    (r"\bshift register|\b74hc|\b74lv|\b74ls|logic gate", "74xx"),
    # --- passives & generic (LAST: these live in Device) ---
    (r"resistor|\bres\b|potentiometer|\brheostat", "Device"),
    (r"capacitor|\bcap\b|\bmlcc\b|tantalum|electrolytic", "Device"),
    (r"inductor|\bferrite\b|\bchoke\b|\bbead\b", "Device"),
    (r"crystal|fuse|varistor|thermistor", "Device"),
    # --- connectors (symbols usually land in Connector_Generic) ---
    (r"\bconnector\b|\bheader\b|terminal block|\bjst\b|molex|\bidc\b|\busb conn",
     "Connector"),
]

_COMPILED = [(re.compile(p), c) for p, c in _RULES]

# Footprints and 3D models are filed by PACKAGE, not function, and the part name
# usually IS the package (e.g. "TQFP-48_7x7mm", "R_0603", "SOT-23"). These map a
# package token to the category; SMD/THT is resolved separately below.
_PKG_RULES = [
    (r"tqfp|lqfp|\bqfp\b|pqfp", "Package_QFP"),
    (r"\bqfn\b|\bdfn\b|\bmlf\b|\bmlp\b|\bvqfn\b|\bwqfn\b", "Package_DFN_QFN"),
    (r"\bson\b|\bwson\b|\buson\b", "Package_SON"),
    (r"soic|\bso-?\d|\bsop\b|tssop|\bmsop\b|ssop|\bvsso|htssop|\bso8\b|\bso14\b",
     "Package_SO"),
    (r"\bbga\b|\bfbga\b|\bufbga\b|\blfbga\b|\bwlcsp\b", "Package_BGA"),
    (r"\bcsp\b|\bwlp\b", "Package_CSP"),
    (r"\blga\b", "Package_LGA"),
    (r"\blcc\b|\bplcc\b", "Package_LCC"),
    (r"directfet", "Package_DirectFET"),
    (r"\bdip\b|\bpdip\b|\bcdip\b", "Package_DIP"),
    (r"\bsip\b|\bsil\b", "Package_SIP"),
    (r"to-?220|to-?247|to-?92|to-?263|to-?126|\bdpak\b|d2pak|to-?251",
     "Package_TO_SOT_THT"),          # resolved to SMD below when SMD markers present
    (r"sot-?\d|sc-?70|sc-?89|\bsod\b|\bsma\b|\bsmb\b|\bsmc\b|tsot|wson",
     "Package_TO_SOT_SMD"),
    (r"\bto-?\d", "Package_TO_SOT_THT"),
    # passives: the footprint name carries R_/C_/L_/D_ + an imperial size
    (r"\br[_-]?\d{4}|resistor", "Resistor"),
    (r"\bc[_-]?\d{4}|capacitor|tantalum|\bcp_", "Capacitor"),
    (r"\bl[_-]?\d{4}|inductor|\bchoke\b|ferrite", "Inductor"),
    (r"\bd[_-]?\d{4}|\bdiode\b|\bsod-?\d", "Diode"),
    (r"\bled\b", "LED"),
    (r"crystal|\bxtal\b|resonator|hc-?49", "Crystal"),
    (r"button|tactile|switch", "Button_Switch_SMD"),
    (r"\bjst\b|molex|\bidc\b|header|terminal|connector|\bffc\b|\bfpc\b", "Connector"),
    (r"relay", "Relay"),
    (r"transformer", "Transformer"),
    (r"buzzer|beeper", "Buzzer_Beeper"),
    (r"battery|coin cell|cr20\d", "Battery"),
]
_PKG_COMPILED = [(re.compile(p), c) for p, c in _PKG_RULES]

_SMD_HINT = re.compile(
    r"smd|smt|\b0201\b|\b0402\b|\b0603\b|\b0805\b|\b1206\b|\b1210\b|\b2010\b|"
    r"\b2512\b|\b1608\b|\b2012\b|\b3216\b|chip\b|handsolder|dpak|d2pak|tsot|sc-?70")
_THT_HINT = re.compile(
    r"\btht\b|through.?hole|\bradial\b|\bazial\b|\baxial\b|to-?92|to-?220|to-?247|"
    r"\bdip\b|\bpdip\b|p[_-]?\d\.\d{2}mm|pitch.*\dmm|leaded")


def _resolve_smd_tht(cat, hay, universe):
    """For categories that split into _SMD / _THT, pick the variant from markers."""
    smd, tht = cat + "_SMD", cat + "_THT"
    if smd not in universe and tht not in universe and cat in universe:
        return cat                      # no split for this family (e.g. Crystal)
    is_tht = bool(_THT_HINT.search(hay))
    is_smd = bool(_SMD_HINT.search(hay))
    if is_smd and not is_tht and smd in universe:
        return smd
    if is_tht and not is_smd and tht in universe:
        return tht
    # ambiguous: SMD is the overwhelmingly common default in modern designs
    return smd if smd in universe else (tht if tht in universe else cat)


def _haystack(name, meta):
    meta = meta or {}
    parts = [name or ""]
    for k in ("value", "description", "keywords", "footprint", "datasheet", "mpn",
              "manufacturer", "summary"):
        v = meta.get(k)
        if v:
            parts.append(str(v))
    return " ".join(parts).lower()


_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(s):
    return set(t for t in _TOKEN.findall(s.lower()) if len(t) > 2)


def classify(kind, name, meta=None, hint=None):
    """Return (category, how) where category is guaranteed to be a real one for
    this install (or the per-kind default). `how` is 'hint'|'rule'|'token'|'default'
    for logging/telemetry."""
    if kind not in _KIND_META:
        return ("Device", "default")
    universe = load_universe(kind)
    default = _KIND_META[kind][0]

    # 1) caller hint wins if it names a real category (case-insensitive match)
    if hint:
        h = str(hint).strip()
        if h in universe:
            return (h, "hint")
        low = {c.lower(): c for c in universe}
        if h.lower() in low:
            return (low[h.lower()], "hint")

    hay = _haystack(name, meta)

    # 2) keyword rules -- package-based for footprint/3D, function-based for symbols
    if kind in ("footprint", "model3d"):
        for rx, cat in _PKG_COMPILED:
            if rx.search(hay):
                resolved = _resolve_smd_tht(cat, hay, universe) if universe else cat
                if not universe or resolved in universe:
                    return (resolved, "rule")
    else:
        for rx, cat in _COMPILED:
            if rx.search(hay):
                if not universe or cat in universe:
                    return (cat, "rule")
            # rule's category absent here: fall through to token match
    # 3) token overlap against the real category names
    if universe:
        htok = _tokens(hay)
        best, best_score = None, 0
        for cat in universe:
            score = len(htok & _tokens(cat.replace("_", " ")))
            if score > best_score:
                best, best_score = cat, score
        if best and best_score >= 1:
            return (best, "token")

    # 4) give up gracefully
    if not universe or default in universe:
        return (default, "default")
    return (sorted(universe)[0], "default")
