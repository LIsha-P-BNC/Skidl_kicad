"""
tests/unit_tests/test_dynamic_any_circuit.py

THE DYNAMISM CONTRACT: the engine must accept ANY circuit topology -- chain,
star, bridge, cross-coupled, H-bridge -- and any scale, with no special cases.
Each topology below is structurally different; each runs through the real
SKiDL pipeline (real Device library parts, real netlist generation, real
logical ERC) in its own subprocess, exactly like a production build body.
If any topology class were special-cased or hardcoded, these would diverge.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_PRELUDE = """\
import sys
from skidl import *
set_default_tool(KICAD9)
"""

_POSTLUDE = """
import io, contextlib, re
generate_netlist(file_="{net}")
txt = open("{net}", encoding="utf-8").read()
n = len(re.findall(r"\\(comp\\s", txt))   # '(comp' node; excludes (components/(company
assert n == {expect}, "expected {expect} comps, netlist has %d" % n
with contextlib.redirect_stderr(io.StringIO()):
    ERC()          # logical ERC must RUN (warnings ok, crash is a failure)
print("OK", n)
"""

# name -> (body, expected component count). Every pin connected (the same
# contract the production floating-pin gate enforces).
TOPOLOGIES = {
    # linear chain: N series resistors
    "chain": ("""
vcc, gnd = Net('VCC'), Net('GND')
prev = vcc
for i in range(8):
    r = Part('Device', 'R', value='1k')
    prev & r
    prev = r[2]
prev += gnd
""", 8),
    # star: 12 pull-downs from one hub net
    "star": ("""
hub, gnd = Net('HUB'), Net('GND')
for i in range(12):
    r = Part('Device', 'R', value='10k')
    r[1] += hub
    r[2] += gnd
""", 12),
    # wheatstone bridge + detector: a true mesh, not a chain
    "bridge": ("""
vcc, gnd, a, b = Net('VCC'), Net('GND'), Net('MID_A'), Net('MID_B')
r1, r2, r3, r4, rdet = [Part('Device', 'R', value='1k') for _ in range(5)]
r1[1] += vcc; r1[2] += a
r2[1] += a;   r2[2] += gnd
r3[1] += vcc; r3[2] += b
r4[1] += b;   r4[2] += gnd
rdet[1] += a; rdet[2] += b
""", 5),
    # diode bridge rectifier + load: polarity-carrying parts
    "diode_bridge": ("""
ac1, ac2, vp, vm = Net('AC1'), Net('AC2'), Net('V+'), Net('V-')
d1, d2, d3, d4 = [Part('Device', 'D', value='1N4007') for _ in range(4)]
d1['A'] += ac1; d1['K'] += vp
d2['A'] += ac2; d2['K'] += vp
d3['A'] += vm;  d3['K'] += ac1
d4['A'] += vm;  d4['K'] += ac2
rload = Part('Device', 'R', value='1k')
rload[1] += vp; rload[2] += vm
""", 5),
    # cross-coupled discrete astable: feedback topology (2 BJT + 4R + 2C)
    "astable": ("""
vcc, gnd = Net('VCC'), Net('GND')
q1, q2 = [Part('Device', 'Q_NPN_BCE', value='2N3904') for _ in range(2)]
rc1, rc2, rb1, rb2 = [Part('Device', 'R', value='10k') for _ in range(4)]
c1, c2 = [Part('Device', 'C', value='10uF') for _ in range(2)]
rc1[1] += vcc; rc1[2] += q1['C']
rc2[1] += vcc; rc2[2] += q2['C']
rb1[1] += vcc; rb1[2] += q1['B']
rb2[1] += vcc; rb2[2] += q2['B']
c1[1] += q1['C']; c1[2] += q2['B']
c2[1] += q2['C']; c2[2] += q1['B']
q1['E'] += gnd
q2['E'] += gnd
""", 8),
    # H-bridge: 4 switches + 4 base resistors + bridged load
    "h_bridge": ("""
vcc, gnd, ma, mb = Net('VCC'), Net('GND'), Net('MOT_A'), Net('MOT_B')
qs = [Part('Device', 'Q_NPN_BCE', value='2N2222') for _ in range(4)]
rs = [Part('Device', 'R', value='1k') for _ in range(4)]
for i, (q, r) in enumerate(zip(qs, rs)):
    drive = Net('DRV%d' % i)
    r[1] += drive; r[2] += q['B']
qs[0]['C'] += vcc; qs[0]['E'] += ma
qs[1]['C'] += vcc; qs[1]['E'] += mb
qs[2]['C'] += ma;  qs[2]['E'] += gnd
qs[3]['C'] += mb;  qs[3]['E'] += gnd
rload = Part('Device', 'R', value='MOTOR')
rload[1] += ma; rload[2] += mb
""", 9),
}


def _run_body(body, expect, tmp_path, tag):
    net = (tmp_path / (tag + ".net")).as_posix()
    code = _PRELUDE + body + _POSTLUDE.format(net=net, expect=expect)
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=120, env=env,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, (
        f"{tag} failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}")
    assert f"OK {expect}" in proc.stdout, proc.stdout[-500:]


def test_every_topology_class_builds(tmp_path):
    for tag, (body, expect) in TOPOLOGIES.items():
        _run_body(body, expect, tmp_path, tag)


def test_scale_free_same_code_any_size(tmp_path):
    # The SAME generator at 3, 25 and 80 parts -- scale must not change
    # behaviour (no small-circuit or big-circuit special cases).
    for n in (3, 25, 80):
        body = f"""
vcc, gnd = Net('VCC'), Net('GND')
prev = vcc
for i in range({n}):
    r = Part('Device', 'R', value='1k')
    prev & r
    prev = r[2]
prev += gnd
"""
        _run_body(body, n, tmp_path, f"scale{n}")
