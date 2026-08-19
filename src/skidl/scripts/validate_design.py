# -*- coding: utf-8 -*-
"""Static + netlist validator for generated SKiDL designs.

Checks a generated SKiDL ``.py`` (and, optionally, its generated ``.net``) against the
rules in ``docs/schematic_generation_spec.md``. Findings are reported by stable rule ID
(e.g. ``PWR-2``) so they map 1:1 to the specification and its checklist.

The ``.py`` is analysed **statically** with the :mod:`ast` module -- the target script is
never imported or executed, so validation is safe and fast. The optional ``.net`` check
parses the KiCad netlist s-expression to catch a few connectivity-level issues.

Usage::

    python -m skidl.scripts.validate_design my_design.py
    python -m skidl.scripts.validate_design my_design.py --netlist my_design.net
    python -m skidl.scripts.validate_design my_design.py --strict   # warnings also fail
    python -m skidl.scripts.validate_design my_design.py --json

Exit code: 0 if no MUST violations (and no SHOULD violations under ``--strict``), else 1.
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import re
import sys

# --- severities -----------------------------------------------------------------------

ERROR = "ERROR"   # a MUST / MUST NOT violation -> fails the build
WARN = "WARN"     # a SHOULD / SHOULD NOT deviation or a heuristic match
INFO = "INFO"     # a reminder for a Human-class rule the validator cannot decide

_SEVERITY_RANK = {ERROR: 0, WARN: 1, INFO: 2}

# --- authoritative patterns (kept in sync with the generator) -------------------------

# Mirror of _POWER_NET_RE in src/skidl/tools/kicad*/gen_schematic.py. Any name also
# matching ``^\+`` is treated as power by the generator.
POWER_NET_RE = re.compile(
    r"^(\+\d[\d.]*V[\d]*|GND|AGND|DGND|PGND|VCC|VDD|VSS|VEE|VBUS|VBAT|AVCC|AVDD|DVCC|DVDD)$",
    re.IGNORECASE,
)

# Auto / placeholder net names that must never survive into a shipped design (NET-2).
AUTO_NET_RE = re.compile(r"^(n\$\d+|wire\d+|net\d+)$", re.IGNORECASE)

# Standard reference-designator forms (REF-1). Optional trailing suffix letter (REF-2).
REF_RE = re.compile(r"^(R|C|L|D|Q|U|Y|X|J|K|SW|S|F|T|TP)\d+[A-Z]?$")

# Engineering-notation component values (VAL-1). Deliberately permissive.
VALUE_RE = re.compile(
    r"""^(
        \d+(\.\d+)?[pnuµμmkKMG][FHΩR]? |   # 100n, 4.7u, 100nF, 4.7uF, 10k, 1M, 2.2nF
        \d+[RkKMG]\d* |                    # embedded-decimal: 0R, 4R7, 1k5, 4k7
        \d+(\.\d+)?[FHΩ] |                 # explicit unit only
        \d{1,3}                            # small plain integer ohms: 0, 47, 100
    )$""",
    re.VERBOSE,
)

# Values that are legitimately non-numeric on a passive (not-populated markers).
VALUE_WHITELIST = {"", "NP", "DNP", "DNI", "N.M.", "NM", "TBD"}

# Part names treated as R/C/L passives for value-format checking (VAL-1/2).
PASSIVE_NAME_RE = re.compile(r"^[RCL](_.*)?$")

# Generic connectors sometimes misused as a placeholder for an IC (LIB-1).
CONNECTOR_NAME_RE = re.compile(r"^Conn_0[12]x(\d+)", re.IGNORECASE)


class Finding:
    __slots__ = ("rule", "severity", "line", "message")

    def __init__(self, rule, severity, line, message):
        self.rule = rule
        self.severity = severity
        self.line = line
        self.message = message

    def as_dict(self):
        return {
            "rule": self.rule,
            "severity": self.severity,
            "line": self.line,
            "message": self.message,
        }


# --- helpers --------------------------------------------------------------------------


def _const_str(node):
    """Return the string value of a str-literal AST node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _kw(call, name):
    """Return the keyword-argument value node for ``name`` on a Call, else None."""
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _num_value(node):
    """Return a numeric constant's value, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(
        node.value, bool
    ):
        return node.value
    return None


def _is_truthy_const(node):
    return isinstance(node, ast.Constant) and bool(node.value) is True


def _call_name(call):
    """Best-effort dotted name of a Call's function (e.g. 'smart_schematic.build')."""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        parts = [f.attr]
        cur = f.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


# --- static (.py) validator -----------------------------------------------------------


class DesignValidator(ast.NodeVisitor):
    def __init__(self):
        self.findings = []
        self.subcircuit_funcs = set()   # names of @subcircuit-decorated functions
        # stack of (func_name, {param names}, is_subcircuit) for enclosing functions
        self.func_stack = []
        self.loop_depth = 0
        self.subcircuit_call_lines = {}  # func name -> list of call linenos
        # net-name literal -> set of scopes it was created in (NET-5)
        self.net_scopes = {}
        self.saw_generation_call = False
        # module-level net variable name -> net name literal (for the matrix)
        self.module_net_vars = {}
        # net name -> set of @subcircuit block names it is passed into (the matrix)
        self.net_to_blocks = {}

    # -- reporting --
    def add(self, rule, severity, line, message):
        self.findings.append(Finding(rule, severity, line, message))

    # -- pre-pass: collect @subcircuit-decorated function names --
    def collect_subcircuits(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for dec in node.decorator_list:
                    dname = dec.attr if isinstance(dec, ast.Attribute) else (
                        dec.id if isinstance(dec, ast.Name) else None
                    )
                    if isinstance(dec, ast.Call):
                        inner = dec.func
                        dname = inner.attr if isinstance(inner, ast.Attribute) else (
                            inner.id if isinstance(inner, ast.Name) else None
                        )
                    if dname == "subcircuit":
                        self.subcircuit_funcs.add(node.name)

    # -- pre-pass: bind module-level `var = Net("NAME")` assignments --
    def collect_module_nets(self, tree):
        for stmt in getattr(tree, "body", []):
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                val = stmt.value
                if isinstance(val, ast.Call) and (
                        (_call_name(val) or "").split(".")[-1] == "Net"):
                    a = val.args[0] if val.args else _kw(val, "name")
                    nm = _const_str(a) if a is not None else None
                    if nm:
                        self.module_net_vars[stmt.targets[0].id] = nm

    # -- scope + loop tracking --
    def visit_FunctionDef(self, node):
        params = set()
        a = node.args
        for grp in (getattr(a, "posonlyargs", []), a.args, a.kwonlyargs):
            for arg in grp:
                params.add(arg.arg)
        self.func_stack.append((node.name, params, node.name in self.subcircuit_funcs))
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_For(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def visit_While(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    def _visit_comprehension(self, node):
        self.loop_depth += 1
        self.generic_visit(node)
        self.loop_depth -= 1

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    # -- the workhorse --
    def visit_Call(self, node):
        name = _call_name(node)
        short = name.split(".")[-1] if name else None

        if short == "Part":
            self._check_part(node)
        elif short == "Net":
            self._check_net(node)
        elif short in ("generate_schematic",):
            self.saw_generation_call = True
            self._check_generate(node, smart=False)
        elif short == "build" and name and "smart_schematic" in name:
            self.saw_generation_call = True
            self._check_generate(node, smart=True)

        # HIER-2: an @subcircuit function called inside a loop, or more than once.
        if short in self.subcircuit_funcs:
            self.subcircuit_call_lines.setdefault(short, []).append(node.lineno)
            if self.loop_depth > 0:
                self.add(
                    "HIER-2", ERROR, node.lineno,
                    "@subcircuit function '%s' is called inside a loop -- every call "
                    "spins off its own schematic page. Convert it to a plain (undecorated) "
                    "helper function." % short,
                )
            # Connectivity matrix: map each net argument -> this block.
            arg_nodes = list(node.args) + [kw.value for kw in node.keywords if kw.arg != "tag"]
            for arg in arg_nodes:
                nn = self._resolve_net_arg(arg)
                if nn:
                    self.net_to_blocks.setdefault(nn, set()).add(short)

        self.generic_visit(node)

    def _resolve_net_arg(self, node):
        """Resolve a call argument to a net name (module var or inline Net('...'))."""
        if isinstance(node, ast.Name):
            return self.module_net_vars.get(node.id)
        if isinstance(node, ast.Call) and (_call_name(node) or "").split(".")[-1] == "Net":
            a = node.args[0] if node.args else _kw(node, "name")
            return _const_str(a) if a is not None else None
        return None

    def visit_Assign(self, node):
        # HIER-5: a sheet-pin parameter reassigned to a fresh Net(...) inside a
        # @subcircuit discards the parent's net object and breaks the connection.
        if self.func_stack and self.func_stack[-1][2]:
            fname, params, _ = self.func_stack[-1]
            val = node.value
            if isinstance(val, ast.Call) and (_call_name(val) or "").split(".")[-1] == "Net":
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id in params:
                        self.add(
                            "HIER-5", ERROR, node.lineno,
                            "Sheet-pin parameter '%s' in @subcircuit '%s' is reassigned to a "
                            "new Net(...) -- this discards the net the parent passed in and "
                            "silently breaks the cross-sheet connection. Use the parameter "
                            "directly; never recreate it." % (t.id, fname),
                        )
        self.generic_visit(node)

    # -- Part(...) checks --
    def _check_part(self, node):
        lib = _const_str(node.args[0]) if len(node.args) >= 1 else None
        pname = _const_str(node.args[1]) if len(node.args) >= 2 else None
        if lib is None:
            lib = _const_str(_kw(node, "lib") or ast.Constant(value=None))
        if pname is None:
            pname = _const_str(_kw(node, "name") or ast.Constant(value=None))

        # PWR-2: manual power / flag parts.
        if lib and lib.lower() == "power":
            self.add(
                "PWR-2", ERROR, node.lineno,
                'Manual power part Part("power", ...) -- the generator emits power symbols '
                "automatically from the net name. Delete this and just name the net "
                "(e.g. Net('+5V')).",
            )
        if pname and "PWR_FLAG" in pname.upper():
            self.add(
                "PWR-2", ERROR, node.lineno,
                "Manual PWR_FLAG part -- the pipeline adds power flags itself. Remove it.",
            )

        # LIB-1: large generic connector possibly standing in for an IC.
        if pname:
            m = CONNECTOR_NAME_RE.match(pname)
            if m and int(m.group(1)) >= 20:
                self.add(
                    "LIB-1", INFO, node.lineno,
                    "Large generic connector '%s' -- confirm this is a real connector and "
                    "not a placeholder for an IC/MCU (a generic connector has no pin names "
                    "or ERC types, so miswiring passes silently)." % pname,
                )

        # META-1: stable tag on every part.
        if _kw(node, "tag") is None:
            self.add(
                "META-1", ERROR, node.lineno,
                "Part(...) has no tag= -- SKiDL auto-generates a random tag per run, "
                "breaking UUID stability across regenerations. Add a stable tag "
                '(e.g. tag="r1").',
            )

        # VAL-1/2: engineering-notation value on R/C/L passives.
        val_node = _kw(node, "value")
        val = _const_str(val_node) if val_node is not None else None
        is_passive = bool(pname and PASSIVE_NAME_RE.match(pname))
        if is_passive and val is not None and val.strip() not in VALUE_WHITELIST:
            v = val.strip()
            if re.fullmatch(r"\d{4,}", v) or re.match(r"^0?\.\d{4,}", v) or v.startswith("."):
                self.add(
                    "VAL-2", ERROR, node.lineno,
                    "Raw/malformed component value '%s' -- use engineering notation "
                    "(e.g. 10k, 100nF, 4.7uF, 0R)." % v,
                )
            elif not VALUE_RE.match(v):
                self.add(
                    "VAL-1", WARN, node.lineno,
                    "Value '%s' is not standard engineering notation "
                    "(e.g. 10k, 4.7k, 100nF, 4.7uF, 1M, 0R)." % v,
                )

        # REF-1: explicit reference designator, if given, must be standard.
        ref_node = _kw(node, "ref")
        ref = _const_str(ref_node) if ref_node is not None else None
        if ref is not None and not REF_RE.match(ref):
            self.add(
                "REF-1", ERROR, node.lineno,
                "Non-standard reference designator ref='%s' -- use a standard prefix "
                "(R/C/L/D/Q/U/Y/X/J/K/SW/F/T/TP) + number, e.g. 'U3'." % ref,
            )

    # -- Net(...) checks --
    def _check_net(self, node):
        nm_node = node.args[0] if node.args else _kw(node, "name")
        nm = _const_str(nm_node) if nm_node is not None else None

        if nm is None:
            self.add(
                "NET-1", WARN, node.lineno,
                "Net() created without a name -- meaningful signals should be named by "
                "function (e.g. Net('UART_TX')); anonymous nets render as N$1, N$2...",
            )
            return

        # NET-2: auto/placeholder names.
        if AUTO_NET_RE.match(nm):
            self.add(
                "NET-2", ERROR, node.lineno,
                "Auto/placeholder net name '%s' on a meaningful signal -- give it a "
                "function-based name." % nm,
            )

        # NET-4: active-low must use ASCII _N suffix.
        if nm.startswith("/") or nm.startswith("~") or "̅" in nm or "̄" in nm:
            self.add(
                "NET-4", ERROR, node.lineno,
                "Active-low net name '%s' uses '/'/'~'/overbar -- use the ASCII '_N' "
                "suffix (e.g. RESET_N) for the net name." % nm,
            )

        is_power = bool(nm.startswith("+") or POWER_NET_RE.match(nm))

        # PWR-4: looks like a rail but won't be recognised as power.
        if not is_power:
            looks_rail = bool(
                re.search(r"\d+V\d*", nm)
                or nm.upper().startswith(("VCC", "VDD", "VBAT", "VBUS", "VEE", "VSS"))
            )
            if looks_rail:
                self.add(
                    "PWR-4", WARN, node.lineno,
                    "Net '%s' looks like a power rail but does not match the power pattern, "
                    "so it renders as a plain wire, not a power symbol. Rename to a "
                    "recognised form (e.g. +3.3V, GND, VCC)." % nm,
                )

        # NET-3: uppercase / underscore style (skip power rails like +3.3V).
        elif not is_power:
            pass

        if not is_power and not AUTO_NET_RE.match(nm) and nm != nm.upper() and nm.isascii():
            self.add(
                "NET-3", WARN, node.lineno,
                "Net name '%s' should be UPPERCASE with underscores "
                "(e.g. %s)." % (nm, re.sub(r"[^0-9A-Za-z]+", "_", nm).upper()),
            )

        # NET-5: same non-power name created in two different scopes.
        if not is_power:
            scope = self.func_stack[-1][0] if self.func_stack else "<module>"
            self.net_scopes.setdefault(nm, {})
            self.net_scopes[nm].setdefault(scope, node.lineno)

    # -- generation call checks --
    def _check_generate(self, node, smart):
        if not smart:
            # CONN-1: auto_stub must be enabled on a raw generate_schematic call.
            au = _kw(node, "auto_stub")
            if au is None:
                self.add(
                    "CONN-1", ERROR, node.lineno,
                    "generate_schematic(...) is missing auto_stub=True -- without it the "
                    "engine won't emit power symbols or fall back to labels on congested "
                    "nets.",
                )
            elif not _is_truthy_const(au):
                self.add(
                    "CONN-1", ERROR, node.lineno,
                    "generate_schematic(..., auto_stub=...) is not True -- set "
                    "auto_stub=True.",
                )
        else:
            # META-3: smart build should carry title-block metadata.
            missing = [k for k in ("rev", "company", "engineer") if _kw(node, k) is None]
            if missing:
                self.add(
                    "META-3", WARN, node.lineno,
                    "smart_schematic.build(...) is missing title-block metadata: %s -- "
                    "pass them so the KiCad title block carries version info."
                    % ", ".join(missing),
                )

        # GEN-1: flatness should be 0.0 or 1.0, never an intermediate value.
        fl = _kw(node, "flatness")
        if fl is not None:
            v = _num_value(fl)
            if v is not None and v not in (0, 0.0, 1, 1.0):
                self.add(
                    "GEN-1", WARN, node.lineno,
                    "flatness=%s is an intermediate value; it does not control page count "
                    "reliably (only 1.0 merges repeated pages, and that erases hierarchy). "
                    "Keep flatness=0.0 and control pages at the source (HIER-2)." % v,
                )

    # -- deferred checks that need the whole tree --
    def finalize(self):
        # NET-5: names created in >1 scope.
        for nm, scopes in self.net_scopes.items():
            if len(scopes) > 1:
                where = ", ".join("%s (line %d)" % (s, ln) for s, ln in scopes.items())
                first_line = min(scopes.values())
                self.add(
                    "NET-5", WARN, first_line,
                    "Net('%s') is created in multiple scopes: %s. In SKiDL these are "
                    "SEPARATE, unconnected nets. Create it once and pass the same Net "
                    "object between blocks." % (nm, where),
                )
        # HIER-2 (soft): an @subcircuit called more than once outside a loop.
        for fn, lines in self.subcircuit_call_lines.items():
            if len(lines) > 1:
                self.add(
                    "HIER-4", INFO, min(lines),
                    "@subcircuit '%s' is called %d times (lines %s) -- that is %d separate "
                    "pages. If these are repeated identical units, use a plain helper "
                    "function instead." % (fn, len(lines), lines, len(lines)),
                )
        # NET-6: a module-level shared signal net never passed to any block, in a
        # design that clearly uses hierarchy (some net did reach a block). Power nets
        # are global by name, so they are exempt.
        if self.net_to_blocks:
            for nm in set(self.module_net_vars.values()):
                if nm.startswith("+") or POWER_NET_RE.match(nm):
                    continue
                if not self.net_to_blocks.get(nm):
                    self.add(
                        "NET-6", WARN, 0,
                        "Shared net '%s' is created at module level but is never passed to "
                        "any @subcircuit -- likely a missed connection (or a dead net)."
                        % nm,
                    )
        if not self.saw_generation_call:
            self.add(
                "GEN-0", INFO, 0,
                "No generate_schematic(...) / smart_schematic.build(...) call found -- "
                "if this file is a design (not a library), it won't produce a schematic.",
            )


def analyze_text(src, filename="<design>"):
    """Parse and analyse design source text; return the populated DesignValidator.

    Raises SyntaxError if the source does not parse. Uses only the ast module --
    the source is never imported or executed, so this is safe to call in-process
    (e.g. from the MCP server) without triggering any skidl import side effects.
    """
    tree = ast.parse(src, filename=filename)
    v = DesignValidator()
    v.collect_subcircuits(tree)
    v.collect_module_nets(tree)
    v.visit(tree)
    v.finalize()
    return v


def validate_text(src, filename="<design>"):
    """Return the list of findings for design source text (public API)."""
    try:
        return analyze_text(src, filename).findings
    except SyntaxError as exc:
        return [Finding("SYNTAX", ERROR, exc.lineno or 0, "Python syntax error: %s" % exc.msg)]


def analyze_source(path):
    """Parse and analyse a design file; return the populated DesignValidator.

    Raises SyntaxError if the file does not parse.
    """
    with open(path, "r", encoding="utf-8") as fh:
        src = fh.read()
    return analyze_text(src, filename=path)


def validate_source(path):
    """Return the list of findings for a design .py (public API)."""
    try:
        return analyze_source(path).findings
    except SyntaxError as exc:
        return [Finding("SYNTAX", ERROR, exc.lineno or 0, "Python syntax error: %s" % exc.msg)]


# --- optional netlist (.net) validator ------------------------------------------------


def _parse_sexpr(text):
    """Minimal KiCad-netlist s-expression parser -> nested lists of strings."""
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+', text)
    pos = [0]

    def parse():
        out = []
        while pos[0] < len(tokens):
            tok = tokens[pos[0]]
            pos[0] += 1
            if tok == "(":
                out.append(parse())
            elif tok == ")":
                return out
            else:
                if tok.startswith('"') and tok.endswith('"'):
                    tok = tok[1:-1]
                out.append(tok)
        return out

    top = parse()
    return top[0] if len(top) == 1 else top


def _find(node, tag):
    """Yield child lists whose head token == tag."""
    if isinstance(node, list):
        for child in node:
            if isinstance(child, list) and child and child[0] == tag:
                yield child


def _first(node, tag):
    for c in _find(node, tag):
        return c
    return None


def _scalar(node, tag):
    c = _first(node, tag)
    if c and len(c) >= 2 and isinstance(c[1], str):
        return c[1]
    return None


def validate_netlist(path):
    findings = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        root = _parse_sexpr(text)
    except Exception as exc:  # best-effort: a parse failure must not crash the run
        return [Finding("NET-PARSE", INFO, 0, "Could not parse netlist '%s': %s" % (path, exc))]

    # ref -> part name (from libsource) and ref -> value
    ref_part, ref_value = {}, {}
    comps = _first(root, "components")
    if comps:
        for comp in _find(comps, "comp"):
            ref = _scalar(comp, "ref")
            if not ref:
                continue
            ref_value[ref] = _scalar(comp, "value")
            lib = _first(comp, "libsource")
            ref_part[ref] = _scalar(lib, "part") if lib else None

    # net name -> [(ref, pin), ...]
    nets = {}
    nets_block = _first(root, "nets")
    if nets_block:
        for net in _find(nets_block, "net"):
            nm = _scalar(net, "name") or ""
            nodes = []
            for nd in _find(net, "node"):
                nodes.append((_scalar(nd, "ref"), _scalar(nd, "pin")))
            nets.setdefault(nm, []).extend(nodes)

    # map ref -> set of net names it touches
    ref_nets = {}
    for nm, nodes in nets.items():
        for ref, _pin in nodes:
            if ref:
                ref_nets.setdefault(ref, set()).add(nm)

    def is_power(nm):
        return bool(nm and (nm.startswith("+") or POWER_NET_RE.match(nm)))

    def is_ground(nm):
        return bool(nm and re.match(r"^[ADP]?GND$", nm, re.IGNORECASE))

    # ELE-1: each supply rail (power, non-ground) should have decoupling caps.
    for nm, nodes in nets.items():
        if is_power(nm) and not is_ground(nm):
            caps = [r for r, _ in nodes if r and r.upper().startswith("C")]
            if not caps:
                findings.append(Finding(
                    "ELE-1", WARN, 0,
                    "Supply rail '%s' has no capacitor on it -- add decoupling/bulk caps."
                    % nm,
                ))

    # ELE-6: LED without a series resistor neighbour, or straight across rails.
    for ref, part in ref_part.items():
        looks_led = (part and "LED" in part.upper()) or (
            ref and ref.upper().startswith("D")
            and (ref_value.get(ref) or "").upper() in {"LED", "RED", "GREEN", "BLUE", "YELLOW"}
        )
        if not looks_led:
            continue
        touched = ref_nets.get(ref, set())
        neighbours = set()
        for nm in touched:
            for r, _ in nets.get(nm, []):
                if r and r != ref:
                    neighbours.add(r)
        has_series_r = any(r.upper().startswith("R") for r in neighbours)
        if not has_series_r:
            findings.append(Finding(
                "ELE-6", ERROR, 0,
                "LED '%s' has no series resistor on either pin -- add a current-limiting "
                "resistor (rail -> R -> anode, cathode -> GND)." % ref,
            ))

    # Floating: a non-power net with only one pin is probably unconnected.
    for nm, nodes in nets.items():
        real = [n for n in nodes if n[0]]
        if len(real) == 1 and not is_power(nm) and not AUTO_NET_RE.match(nm or ""):
            findings.append(Finding(
                "NET-FLOAT", WARN, 0,
                "Net '%s' has only one pin (%s.%s) -- it may be unconnected."
                % (nm, real[0][0], real[0][1]),
            ))

    return findings


# --- golden verification (produced .net <-> .anvil_sch artifacts) ---------------------
# Phase 10: a .anvil_sch merely EXISTING is not proof the design is correct -- a part may
# be dropped, or a symbol missing. Golden verification cross-checks the produced artifacts
# against each other: every netlist component must appear as a schematic symbol, no UUID
# may be duplicated, and the project structure must be sane. Comparison against the
# ORIGINAL source (image/Altium/PDF) stays human-assisted (use the BOM + connectivity
# matrix); this function verifies internal consistency of what was generated.


def _prop(node, name):
    """Value of a (property "<name>" "<value>" ...) child, else None."""
    for c in _find(node, "property"):
        if len(c) >= 3 and c[1] == name and isinstance(c[2], str):
            return c[2]
    return None


def _net_component_refs(net_path):
    """Set of component reference designators declared in a KiCad .net."""
    with open(net_path, "r", encoding="utf-8") as fh:
        root = _parse_sexpr(fh.read())
    refs = set()
    comps = _first(root, "components")
    if comps:
        for comp in _find(comps, "comp"):
            r = _scalar(comp, "ref")
            if r:
                refs.add(r)
    return refs


def _sch_symbols(sch_path):
    """(instances, sheet_uuids) from a .anvil_sch. instances = [(ref, uuid, lib_id)].

    Only DIRECT children of the root are read, so library symbol DEFINITIONS nested
    under (lib_symbols ...) are never mistaken for placed instances."""
    with open(sch_path, "r", encoding="utf-8") as fh:
        root = _parse_sexpr(fh.read())
    children = root[1:] if (isinstance(root, list) and root and root[0] == "kicad_sch") \
        else (root if isinstance(root, list) else [])
    instances, sheet_uuids = [], []
    for ch in children:
        if not isinstance(ch, list) or not ch:
            continue
        if ch[0] == "symbol":
            instances.append((_prop(ch, "Reference"), _scalar(ch, "uuid"),
                              _scalar(ch, "lib_id")))
        elif ch[0] == "sheet":
            u = _scalar(ch, "uuid")
            if u:
                sheet_uuids.append(u)
    return instances, sheet_uuids


def golden_verify(net_path, sch_paths, pro_path=None, expected_sheets=None):
    """Cross-verify produced artifacts. Returns a list of GV-* findings; the build
    is 'verified' iff none are ERROR-severity."""
    findings = []
    try:
        net_refs = _net_component_refs(net_path)
    except Exception as exc:
        return [Finding("GV-NET", ERROR, 0, "could not parse netlist '%s': %s"
                        % (net_path, exc))]

    all_syms, all_uuids, parsed = [], [], 0
    for sp in sch_paths:
        try:
            syms, sheet_uuids = _sch_symbols(sp)
            parsed += 1
        except Exception:
            findings.append(Finding("GV-SCH", WARN, 0,
                                    "could not parse schematic '%s'" % sp))
            continue
        all_syms.extend(syms)
        all_uuids.extend([u for (_, u, _) in syms if u])
        all_uuids.extend(sheet_uuids)

    if parsed == 0:
        findings.append(Finding("GV-4", ERROR, 0,
            "no .anvil_sch could be parsed -- the schematic artifact was not produced "
            "(build is INCOMPLETE, not successful)"))
        return findings

    sch_refs = {r for (r, _, _) in all_syms if r and re.match(r"^[A-Za-z]+\d", r)}

    # GV-1: every netlist component must have a symbol somewhere -- catches a part
    # that was dropped from the schematic even though the netlist is fine.
    missing = sorted(net_refs - sch_refs)
    if missing:
        findings.append(Finding("GV-1", ERROR, 0,
            "%d netlist component(s) have NO symbol in any sheet (dropped from the "
            "schematic): %s" % (len(missing),
                                ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""))))

    # GV-2: duplicate UUIDs corrupt cross-references / annotation.
    counts = {}
    for u in all_uuids:
        counts[u] = counts.get(u, 0) + 1
    dups = sorted(u for u, n in counts.items() if n > 1)
    if dups:
        findings.append(Finding("GV-2", ERROR, 0,
            "%d duplicate UUID(s) across sheets: %s"
            % (len(dups), ", ".join(dups[:8]) + (" ..." if len(dups) > 8 else ""))))

    # GV-3: component-count parity (net vs schematic).
    if len(net_refs) != len(sch_refs):
        extra = sorted(sch_refs - net_refs)
        note = ""
        if extra:
            note = " (in schematic but not netlist -- e.g. power symbols: %s)" % (
                ", ".join(extra[:10]) + (" ..." if len(extra) > 10 else ""))
        findings.append(Finding("GV-3", WARN, 0,
            "component count differs: netlist=%d, placed symbols=%d%s"
            % (len(net_refs), len(sch_refs), note)))

    # GV-4: project structure.
    if pro_path is not None and not os.path.isfile(pro_path):
        findings.append(Finding("GV-4", INFO, 0,
            "no .anvil_pro yet (%s) -- KiCad creates it on first open; the .anvil_sch is "
            "the deliverable" % os.path.basename(pro_path)))

    # GV-5: sheet count vs the hierarchy plan (informational -- flattening is legitimate).
    if expected_sheets is not None and parsed != expected_sheets:
        findings.append(Finding("GV-5", INFO, 0,
            "sheet count: plan expects ~%d (1 + @subcircuit call sites), found %d "
            ".anvil_sch file(s) (small designs may be legitimately flattened to one sheet)"
            % (expected_sheets, parsed)))

    return findings


# --- reporting / CLI ------------------------------------------------------------------


def _sort_key(f):
    return (_SEVERITY_RANK.get(f.severity, 9), f.line, f.rule)


def _print_report(py_path, findings, use_color):
    def col(s, c):
        if not use_color:
            return s
        codes = {"red": "31", "yellow": "33", "cyan": "36", "green": "32", "bold": "1"}
        return "\x1b[%sm%s\x1b[0m" % (codes[c], s)

    n_err = sum(1 for f in findings if f.severity == ERROR)
    n_warn = sum(1 for f in findings if f.severity == WARN)
    n_info = sum(1 for f in findings if f.severity == INFO)

    print(col("SKiDL design validation: %s" % py_path, "bold"))
    print("-" * 72)
    if not findings:
        print(col("  OK - no issues found.", "green"))
    for f in sorted(findings, key=_sort_key):
        tag = {ERROR: col("ERROR", "red"), WARN: col("WARN ", "yellow"),
               INFO: col("INFO ", "cyan")}[f.severity]
        loc = ("L%d" % f.line) if f.line else "-"
        print("  [%s] %-9s %-5s %s" % (tag, f.rule, loc, f.message))
    print("-" * 72)
    summary = "%d error(s), %d warning(s), %d note(s)" % (n_err, n_warn, n_info)
    print(col("  " + summary, "red" if n_err else ("yellow" if n_warn else "green")))
    return n_err, n_warn, n_info


def _print_matrix(validator):
    """Print the net -> @subcircuit connectivity matrix derived from the source."""
    names = set(validator.module_net_vars.values()) | set(validator.net_to_blocks.keys())
    print()
    print("Net connectivity matrix (net -> @subcircuit blocks that receive it):")
    print("-" * 72)
    if not names:
        print("  (no module-level nets passed into @subcircuit blocks found)")
        print("-" * 72)
        return

    def sort_key(n):
        is_pwr = n.startswith("+") or bool(POWER_NET_RE.match(n))
        return (0 if is_pwr else 1, n)

    for nm in sorted(names, key=sort_key):
        blocks = sorted(validator.net_to_blocks.get(nm, set()))
        kind = "power" if (nm.startswith("+") or POWER_NET_RE.match(nm)) else "signal"
        target = ", ".join(blocks) if blocks else "(not passed to any block)"
        print("  %-6s %-14s -> %s" % (kind, nm, target))
    print("-" * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="validate_design",
        description="Validate a generated SKiDL design against the schematic spec "
                    "(docs/schematic_generation_spec.md).",
    )
    ap.add_argument("pyfile", help="the generated SKiDL .py to check")
    ap.add_argument("--netlist", metavar="NET", help="also check the generated .net")
    ap.add_argument("--strict", action="store_true",
                    help="treat SHOULD/WARN findings as failures too")
    ap.add_argument("--json", action="store_true", help="emit findings as JSON")
    ap.add_argument("--matrix", action="store_true",
                    help="also print the net -> @subcircuit connectivity matrix")
    ap.add_argument("--golden", action="store_true",
                    help="Phase-10 golden verification: cross-check the produced "
                         ".net/.anvil_sch artifacts (auto-discovered next to the .py)")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colour")
    args = ap.parse_args(argv)

    validator = None
    try:
        validator = analyze_source(args.pyfile)
        findings = list(validator.findings)
    except SyntaxError as exc:
        findings = [Finding("SYNTAX", ERROR, exc.lineno or 0,
                            "Python syntax error: %s" % exc.msg)]
    if args.netlist:
        findings = findings + validate_netlist(args.netlist)

    golden = None
    if args.golden:
        d = os.path.dirname(os.path.abspath(args.pyfile))
        base = os.path.splitext(os.path.basename(args.pyfile))[0]
        net = args.netlist or os.path.join(d, base + ".net")
        sch_paths = sorted(glob.glob(os.path.join(d, base + "*.anvil_sch")))
        pro = os.path.join(d, base + ".anvil_pro")
        expected = None
        if validator is not None:
            expected = 1 + sum(len(v) for v in validator.subcircuit_call_lines.values())
        if not os.path.isfile(net):
            golden = [Finding("GV-NET", ERROR, 0,
                              "netlist '%s' not found -- build did not reach the netlist" % net)]
        else:
            golden = golden_verify(net, sch_paths, pro_path=pro, expected_sheets=expected)
        findings = findings + golden

    if args.json:
        payload = {
            "file": args.pyfile,
            "netlist": args.netlist,
            "findings": [f.as_dict() for f in sorted(findings, key=_sort_key)],
            "errors": sum(1 for f in findings if f.severity == ERROR),
            "warnings": sum(1 for f in findings if f.severity == WARN),
            "notes": sum(1 for f in findings if f.severity == INFO),
        }
        print(json.dumps(payload, indent=2))
        n_err = payload["errors"]
        n_warn = payload["warnings"]
    else:
        use_color = sys.stdout.isatty() and not args.no_color
        n_err, n_warn, _ = _print_report(args.pyfile, findings, use_color)
        if args.matrix and validator is not None:
            _print_matrix(validator)
        if args.golden and golden is not None:
            gv_err = sum(1 for f in golden if f.severity == ERROR)
            verdict = "PASSED - BUILD SUCCESSFUL (Verified)" if gv_err == 0 \
                else "FAILED - artifacts do not match the design"
            line = "GOLDEN VERIFICATION: " + verdict
            if use_color:
                line = "\x1b[%sm%s\x1b[0m" % ("32" if gv_err == 0 else "31", line)
            print("\n" + line)

    if n_err or (args.strict and n_warn):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
