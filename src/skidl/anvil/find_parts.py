"""find_parts.py -- Windows-safe part search + pin inspector for the Anvil CAD/SKiDL libs.

WHY: the built-in `skidl-part-search` CLI crashes on Windows (it imports the Unix-only
`readline` module).  Use THIS instead to ground yourself (or an AI) on REAL library
names, REAL part names and REAL pin numbers BEFORE writing a circuit -- that is how you
make sure name / pin / value / component are all correct and SKiDL actually runs.

USAGE
  python find_parts.py <terms>              search parts:  "resistor"  "LED"  "opamp|comparator"
  python find_parts.py --show <Lib> <Part>  show a part's pins + default value (verify pins!)

EXAMPLES
  python find_parts.py resistor
  python find_parts.py stm32 lqfp64
  python find_parts.py --show Device R
  python find_parts.py --show Regulator_Switching LM2596T-5
"""
import sys

import anvil_libs  # noqa: F401  -- sets KICAD*_SYMBOL_DIR if the shell lacks them; MUST precede skidl
from skidl import KICAD9, set_default_tool

set_default_tool(KICAD9)


def search(terms):
    """Print every part whose name/alias/description/keywords match `terms`."""
    from skidl.part_query import PartSearchDB

    db = PartSearchDB(tool=KICAD9)
    db.load_from_lib_search_paths()
    results = db.search(terms)
    if not results:
        print(f"(no parts found for: {terms!r} -- try a simpler / different word)")
        return
    results.sort(key=lambda p: (p.lib_name, p.part_name))
    print(f"{len(results)} match(es) for {terms!r}:\n")
    for p in results:
        desc = (p.description or "").strip()
        print(f'  Part("{p.lib_name}", "{p.part_name}")   {desc}')
    print('\n-> copy a Part("Lib","Name") line straight into your circuit, then')
    print('   run  python find_parts.py --show Lib Name  to confirm its pins.')


def show(lib, part_name):
    """Print a part's pin numbers + names + default value (so connections are correct)."""
    from skidl.part_query import show_part

    part = show_part(lib, part_name)
    if part is None:
        print(f'NOT FOUND: Part("{lib}", "{part_name}")  -- search for the right name first.')
        return
    print(f'Part("{lib}", "{part_name}")')
    val = getattr(part, "value", None)
    if val and str(val) != part_name:
        print(f"  default value: {val}")
    pins = list(part.pins)
    print(f"  {len(pins)} pin(s):")
    for pin in sorted(pins, key=lambda x: str(x.num)):
        name = (pin.name or "").strip()
        print(f"    pin {str(pin.num):>3}   {name}")
    print("\n-> connect by number:  part[1] += net      or by name:  part['VCC'] += net")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] in ("--show", "-s"):
        if len(args) < 3:
            print("usage: python find_parts.py --show <Lib> <Part>")
            return
        show(args[1], args[2])
    else:
        search(" ".join(args))


if __name__ == "__main__":
    main()
