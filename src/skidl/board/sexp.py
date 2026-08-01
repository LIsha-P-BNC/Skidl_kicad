"""
src/skidl/board/sexp.py

Minimal S-expression reader for KiCad file formats (.kicad_mod, .net).
Only handles what M0 needs: parens, double-quoted strings (with \\" and
\\\\ escapes), and bare atoms (symbols / numbers) -- returned as a tree
of Python lists / str.

This is a *reader* only. Everything this package writes (.kicad_pcb in
pcb_writer.py) is built from explicit string templates instead --
KiCad's serializer has enough quoting/ordering quirks across versions
that a templated writer, validated against your real kicad-cli, is more
predictable than a generic round-trip writer would be.
"""

from __future__ import annotations
from pathlib import Path
from typing import Union

SExpr = Union[str, list]


def read(text: str) -> list:
    """Parse `text` as a sequence of top-level s-expressions and return
    them as a list. A .kicad_mod file has exactly one top-level
    expression; callers that expect that can do `read(text)[0]`."""
    tokens = _tokenize(text)
    exprs = []
    pos = 0
    while pos < len(tokens):
        expr, pos = _parse_one(tokens, pos)
        exprs.append(expr)
    return exprs


def read_file(path: Union[str, Path]) -> list:
    return read(Path(path).read_text(encoding="utf-8"))


def _tokenize(text: str):
    tokens = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            tokens.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            tokens.append(("str", "".join(buf)))
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in " \t\r\n()":
                j += 1
            tokens.append(("atom", text[i:j]))
            i = j
    return tokens


def _parse_one(tokens, pos):
    tok = tokens[pos]
    if tok == "(":
        pos += 1
        items = []
        while tokens[pos] != ")":
            item, pos = _parse_one(tokens, pos)
            items.append(item)
        return items, pos + 1
    elif tok == ")":
        raise ValueError("unbalanced parens: unexpected ')'")
    else:
        _kind, val = tok
        return val, pos + 1


def find_all(expr, head: str) -> list:
    """All immediate children of `expr` that are lists starting with the
    atom `head`, e.g. find_all(footprint_expr, "pad")."""
    if not isinstance(expr, list):
        return []
    return [e for e in expr if isinstance(e, list) and e and e[0] == head]


def find_one(expr, head: str):
    """First matching child, or None."""
    hits = find_all(expr, head)
    return hits[0] if hits else None


def atom_at(expr, index: int, default=None):
    """expr[index] if present, else default -- guards against footprint
    variants that omit optional positional fields."""
    return expr[index] if isinstance(expr, list) and index < len(expr) else default
