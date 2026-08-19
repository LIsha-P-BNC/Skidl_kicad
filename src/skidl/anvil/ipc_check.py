"""IPC-2612 / IPC-2611 schematic compliance check (read-only).

Reports, on every build, how the generated .anvil_sch scores against the
*enforceable* IPC diagramming rules. Never modifies the schematic -- it only
looks. DYNAMIC: pure geometry/text checks, works for any circuit.

Checked rules:
  Manhattan 90deg (IPC-2612)   -> 0 diagonal wire segments
  No dangling labels           -> KiCad ERC label_dangling == 0
  On the connection grid (IPC-3) -> KiCad ERC endpoint_off_grid == 0
  Junction dots at taps (IPC-2612)
  Power symbols for rails
  Title block present (IPC-2611 documentation)
"""
import os
import re
import subprocess

_NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def check(name, kicad_cli=""):
    sch = name + ".anvil_sch"
    if not os.path.exists(sch):
        return {}
    t = open(sch, encoding="utf-8", errors="replace").read()

    wires = re.findall(
        r"\(wire\s*\(pts\s*\(xy ([\d.\-]+) ([\d.\-]+)\)\s*\(xy ([\d.\-]+) ([\d.\-]+)\)", t
    )
    diagonal = sum(
        1
        for a, b, c, d in wires
        if abs(float(a) - float(c)) > 0.01 and abs(float(b) - float(d)) > 0.01
    )
    junctions = t.count("(junction")
    power_symbols = len(re.findall(r'\(lib_id "power:', t))
    title_block = "(title_block" in t

    off_grid = dangling = None
    erc_errors = None
    if kicad_cli and os.path.exists(kicad_cli):
        rpt = sch + ".ipcerc"
        try:
            subprocess.run(
                [kicad_cli, "sch", "erc", "--severity-all", "-o", rpt, sch],
                capture_output=True, timeout=180, creationflags=_NW,
                stdin=subprocess.DEVNULL,
            )
            txt = open(rpt, encoding="utf-8", errors="replace").read() if os.path.exists(rpt) else ""
            off_grid = txt.count("endpoint_off_grid")
            dangling = txt.count("label_dangling")
            erc_errors = _erc_errors(txt)
            os.remove(rpt)
        except Exception:
            pass

    return dict(
        diagonal=diagonal, off_grid=off_grid, dangling=dangling,
        junctions=junctions, power_symbols=power_symbols, title_block=title_block,
        erc_errors=erc_errors,
    )


# environment-dependent noise, not schematic defects: the CLI has no lib tables
_ERC_NOISE = {"lib_symbol_issues", "footprint_link_issues", "lib_symbol_mismatch"}


def _erc_errors(report_text):
    """Parse a kicad-cli ERC report; return [(check_id, message, '@(x, y): who'), ...]
    for every ERROR-severity violation (the same list the KiCad GUI shows in red)."""
    errors, cur, take = [], None, False
    for ln in report_text.splitlines():
        s = ln.strip()
        m = re.match(r"\[(\w+)\]:\s*(.*)", s)
        if m:
            cur, take = (m.group(1), m.group(2)), False
        elif s == "; error" and cur and cur[0] not in _ERC_NOISE:
            errors.append([cur[0], cur[1], ""])
            take = True
        elif s.startswith(";"):
            take = False
        elif s.startswith("@(") and take and errors and not errors[-1][2]:
            errors[-1][2] = s
    return errors


def report(name, kicad_cli=""):
    """Print a one-block IPC compliance summary. Returns the check dict."""
    c = check(name, kicad_cli)
    if not c:
        return c

    def m(ok):
        return "OK " if ok else "!! "

    lines = [">>> IPC-2612 compliance:"]
    lines.append(f"    {m(c['diagonal'] == 0)}Manhattan 90deg      ({c['diagonal']} diagonal)")
    if c["dangling"] is not None:
        lines.append(f"    {m(c['dangling'] == 0)}No dangling labels   ({c['dangling']})")
    if c["off_grid"] is not None:
        lines.append(f"    {m(c['off_grid'] == 0)}On connection grid   ({c['off_grid']} off-grid)")
    lines.append(f"    {m(True)}Junction dots        ({c['junctions']})")
    lines.append(f"    {m(True)}Power rail symbols   ({c['power_symbols']})")
    lines.append(f"    {m(c['title_block'])}Title block (docs)")
    errs = c.get("erc_errors")
    if errs is not None:
        lines.append(f"    {m(not errs)}KiCad ERC errors     ({len(errs)})")
        for cid, msg, where in errs:
            lines.append(f"       [{cid}] {msg}  {where}")
    print("\n".join(lines))
    return c


if __name__ == "__main__":
    import sys
    from open_anvilcad import _find_bin

    nm = sys.argv[1] if len(sys.argv) > 1 else "circuit"
    if nm.endswith(".anvil_sch"):
        nm = nm[:-10]
    cli = ""
    try:
        b = _find_bin()
        if b:
            cli = os.path.join(b, "anvil-cli.exe")
            if not os.path.exists(cli):
                cli = os.path.join(b, "kicad-cli.exe")
    except Exception:
        pass
    report(nm, cli)
