"""
The Kilimanjaro JCS code-point profile, implemented here without a dependency.

`canonicalize(value)` returns the canonical UTF-8 bytes of a JSON value made of dict (str keys), list/tuple, str,
bool, None and int (or a float with an integral value):

  - object members are sorted by the UTF-8 bytes of their names (the same as Unicode code-point order, not UTF-16
    code-unit order); no whitespace anywhere;
  - strings use the short escapes for `"`, `\\`, backspace, tab, line feed, form feed and carriage return, lowercase
    `\\u00xx` for the other control characters below U+0020, and literal characters for everything else (`/`, U+2028
    and U+2029 included);
  - numbers are integers only, written in base 10 with no exponent (`-0` is `0`); a fraction, NaN, Infinity or an
    absolute value above 9007199254740991 (2^53 - 1) raises `ValueError`;
  - a non-string key, a lone surrogate and any other Python type raise `ValueError`.
"""

from __future__ import annotations

from typing import Any

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r"}


def _string(value: str) -> str:
    out = ['"']
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError("a string with a lone surrogate cannot be canonicalised")
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


MAX_SAFE_INTEGER = 9007199254740991


def _number(value: int | float) -> str:
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("NaN and Infinity cannot be canonicalised")
        if value != int(value):
            raise ValueError("a fractional number cannot be canonicalised: only integers are allowed")
        value = int(value)
    if abs(value) > MAX_SAFE_INTEGER:
        raise ValueError("an integer above 9007199254740991 in absolute value cannot be canonicalised")
    return str(value)


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, (int, float)):
        out.append(_number(value))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise ValueError("an object key must be a string")
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: k.encode("utf-8", "surrogatepass"))):
            if i:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        raise ValueError(f"{type(value).__name__} is not a JSON value")


def canonicalize_to_string(value: Any) -> str:
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def canonicalize(value: Any) -> bytes:
    """The code-point-profile canonical form of `value` as UTF-8 bytes."""
    return canonicalize_to_string(value).encode("utf-8")
