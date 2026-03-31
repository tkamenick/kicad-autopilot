"""KiCad s-expression parser.

Parses .kicad_pcb files (and other KiCad s-expression formats) into nested Python lists.

Parse result type: SExpr = list[str | SExpr]
Each node is [token, *children] where token is the node name (string) and
children are either strings (atoms) or nested SExpr nodes.

Example:
    parse('(footprint "MCU:SOIC-10" (at 130 100) (layer "F.Cu"))')
    → ['footprint', 'MCU:SOIC-10', ['at', '130', '100'], ['layer', 'F.Cu']]

Numbers remain as strings in the raw tree; callers convert as needed.
"""
from __future__ import annotations

from typing import Union

SExpr = list[Union[str, "SExpr"]]


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def tokenize(text: str) -> list[str]:
    """Split KiCad s-expression text into a flat token list.

    Tokens are: '(', ')', quoted strings (with surrounding quotes stripped),
    and bare words.
    """
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\n\r":
            i += 1
        elif c == "(":
            tokens.append("(")
            i += 1
        elif c == ")":
            tokens.append(")")
            i += 1
        elif c == '"':
            # Quoted string — scan to closing quote, handling \" escapes
            i += 1
            buf: list[str] = []
            while i < n:
                ch = text[i]
                if ch == "\\":
                    i += 1
                    if i < n:
                        esc = text[i]
                        buf.append("\n" if esc == "n" else "\t" if esc == "t" else esc)
                        i += 1
                elif ch == '"':
                    i += 1
                    break
                else:
                    buf.append(ch)
                    i += 1
            tokens.append("".join(buf))
        else:
            # Bare word: read until whitespace, paren, or EOF
            start = i
            while i < n and text[i] not in " \t\n\r()":
                i += 1
            tokens.append(text[start:i])
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse(text: str) -> SExpr:
    """Parse a KiCad s-expression string into a nested list.

    Returns the root SExpr node. If the text has multiple top-level
    expressions, only the first is returned.

    Raises ValueError on unmatched parentheses.
    """
    tokens = tokenize(text)
    stack: list[SExpr] = []
    root: list[SExpr] = []  # collect top-level expressions

    for tok in tokens:
        if tok == "(":
            new_node: SExpr = []
            if stack:
                stack[-1].append(new_node)
            else:
                root.append(new_node)
            stack.append(new_node)
        elif tok == ")":
            if not stack:
                raise ValueError("Unmatched closing parenthesis")
            stack.pop()
        else:
            if stack:
                stack[-1].append(tok)
            # bare atoms at top level are ignored (shouldn't appear in valid KiCad files)

    if stack:
        raise ValueError(f"Unclosed parenthesis: {len(stack)} level(s) not closed")

    if not root:
        raise ValueError("No top-level expression found")

    return root[0]


def parse_file(path: str) -> SExpr:
    """Parse a KiCad file and return the root s-expression."""
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


# ---------------------------------------------------------------------------
# Navigation helpers
# ---------------------------------------------------------------------------

def find_all(node: SExpr, token: str) -> list[SExpr]:
    """Return all direct child nodes whose first element matches token."""
    return [child for child in node if isinstance(child, list) and child and child[0] == token]


def find_one(node: SExpr, token: str) -> SExpr | None:
    """Return the first direct child node whose first element matches token, or None."""
    for child in node:
        if isinstance(child, list) and child and child[0] == token:
            return child
    return None


def get_xy(node: SExpr, token: str = "at") -> tuple[float, float] | None:
    """Extract (x, y) from a named child node like (at X Y) or (start X Y).

    Returns None if the node is not found or malformed.
    """
    child = find_one(node, token)
    if child is None or len(child) < 3:
        return None
    try:
        return (float(child[1]), float(child[2]))
    except (ValueError, TypeError):
        return None


def get_at(node: SExpr) -> tuple[float, float, float]:
    """Extract (x, y, rotation) from an 'at' child node.

    Returns (0.0, 0.0, 0.0) if not found. Rotation defaults to 0 if omitted.
    """
    child = find_one(node, "at")
    if child is None:
        return (0.0, 0.0, 0.0)
    try:
        x = float(child[1]) if len(child) > 1 else 0.0
        y = float(child[2]) if len(child) > 2 else 0.0
        rot = float(child[3]) if len(child) > 3 else 0.0
        return (x, y, rot)
    except (ValueError, TypeError):
        return (0.0, 0.0, 0.0)


def get_float(node: SExpr, token: str) -> float | None:
    """Extract a single float value from a named child node like (width 0.5).

    Returns None if the node is not found or has no value.
    """
    child = find_one(node, token)
    if child is None or len(child) < 2:
        return None
    try:
        return float(child[1])
    except (ValueError, TypeError):
        return None


def get_str(node: SExpr, token: str) -> str | None:
    """Extract a single string value from a named child node like (layer "F.Cu").

    Returns None if not found.
    """
    child = find_one(node, token)
    if child is None or len(child) < 2:
        return None
    val = child[1]
    return val if isinstance(val, str) else None


def get_strings(node: SExpr, token: str) -> list[str]:
    """Extract all string values from a named child node like (layers "F.Cu" "B.Cu").

    Returns empty list if not found.
    """
    child = find_one(node, token)
    if child is None:
        return []
    return [v for v in child[1:] if isinstance(v, str)]
