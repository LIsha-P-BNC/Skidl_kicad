"""check_parts.py -- report whether each confirmed part is IN the Anvil CAD/SKiDL library.

WORKFLOW STEP 4: AFTER the user confirms which parts to use, run this to see which chosen
parts ARE in the library and which are NOT.  It ONLY reports -- it NEVER adds anything.
(Adding a missing part is a separate, opt-in step, done only when the user asks.)

USAGE
  python check_parts.py <part> [<part> ...]

  Each <part> can be:
    - an exact  Lib:Name     e.g.  Device:R_Potentiometer   (checked directly)
    - a keyword / part name  e.g.  R_Potentiometer  potentiometer  LED  LM7805
                             (searched across all libraries)

EXAMPLES
  python check_parts.py Device:R_Potentiometer Device:R Device:LED
  python check_parts.py potentiometer resistor LED LM7805
"""
import sys

import anvil_libs  # noqa: F401  -- sets KICAD*_SYMBOL_DIR if the shell lacks them; MUST precede skidl
from skidl import KICAD9, set_default_tool

set_default_tool(KICAD9)


def _pin_count(lib, name):
    """Return the pin count of Part(lib, name), or None if it does not resolve."""
    from skidl.part_query import show_part

    part = show_part(lib, name)
    if part is None:
        return None
    return len(list(part.pins))


def check(queries):
    """Print an IN-LIBRARY / NOT-IN-LIBRARY row per query. Adds nothing."""
    from skidl.part_query import PartSearchDB

    db = PartSearchDB(tool=KICAD9)
    db.load_from_lib_search_paths()

    present, missing = [], []
    print(f"Checking {len(queries)} part(s) against the library:\n")
    for q in queries:
        # Exact "Lib:Name" -> check that symbol directly.
        if ":" in q:
            lib, name = q.split(":", 1)
            pins = _pin_count(lib, name)
            if pins is not None:
                print(f'  [IN LIBRARY]   {q:<26} Part("{lib}","{name}")   {pins} pins')
                present.append(q)
            else:
                print(f'  [NOT IN LIB]   {q:<26} (exact Lib:Name not found -- try a keyword)')
                missing.append(q)
            continue

        # Keyword / part-name -> search across libraries.
        results = db.search(q)
        if not results:
            print(f'  [NOT IN LIB]   {q:<26} (no match -- adding is a separate opt-in step)')
            missing.append(q)
            continue
        # Prefer an exact part-name match; else take the first result as the closest.
        exact = [r for r in results if r.part_name.lower() == q.lower()]
        r = exact[0] if exact else results[0]
        pins = _pin_count(r.lib_name, r.part_name)
        pin_txt = f"{pins} pins" if pins is not None else "? pins"
        extra = "" if exact else f"  (closest of {len(results)} matches)"
        print(f'  [IN LIBRARY]   {q:<26} Part("{r.lib_name}","{r.part_name}")   {pin_txt}{extra}')
        present.append(q)

    print(f"\nSummary: {len(present)} in library, {len(missing)} missing.")
    if missing:
        print("Missing: " + ", ".join(missing))
        print("(Nothing was added. Adding a missing part is a separate step, only if you ask.)")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    check(args)


if __name__ == "__main__":
    main()
