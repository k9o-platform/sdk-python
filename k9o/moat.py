"""
Moat: a local guard around one model call. It runs in your process, makes no network call and needs no account.

Moat does four things:

  1. It pins the system prompt. `messages(untrusted_input)` builds the message list itself: the system message is your
     prompt, the user message is the untrusted input as text. Untrusted text never takes the `system` role, and input
     that carries a role or system marker is refused.
  2. It enforces a declared input schema before the input is placed in a message.
  3. It enforces a declared output schema on the model's answer.
  4. It filters the output for personal data through an optional callback you pass (`filters={"pii": ...}`).

The pin is structural. It stops injected text from becoming the system prompt; it does not stop that text from
reaching the model, so keep treating the model's behaviour as influenced by whatever the input says.

A refusal is an exception, `MoatRefusal`, with `reason` (one of `REFUSAL_REASONS`) and a short `detail`. A schema Moat
cannot enforce raises `MoatConfigError` when the `Moat` is built, so a typo never switches a check off silently.

Schema language (a JSON Schema subset; any other keyword is rejected at construction):
  `type` (object, array, string, number, integer, boolean, null; or a list of these), `properties`, `required`,
  `additionalProperties` (true or false), `items` (one schema), `enum`, `const`, `minLength`, `maxLength`, `pattern`
  (a regular expression, searched, not anchored), `minimum`, `maximum`, `minItems`, `maxItems`.

PII filter: a function `filter(text) -> truthy when the text holds personal data`, or an object with a
`classify(text)` method that answers `"clear"` for clean text (a Barbican instance works as is). A filter that raises
counts as a refusal. With no filter there is no PII step.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Callable, Mapping

REFUSAL_REASONS = ("role_marker", "input_schema", "output_schema", "pii")
"""The closed set of `MoatRefusal.reason` values."""

_TYPES = ("object", "array", "string", "number", "integer", "boolean", "null")
_KEYWORDS = ("type", "properties", "required", "additionalProperties", "items", "enum", "const", "minLength",
             "maxLength", "pattern", "minimum", "maximum", "minItems", "maxItems")


class MoatRefusal(Exception):
    """Moat refused an input or an output. `reason` is one of `REFUSAL_REASONS`; `detail` is a short sentence."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class MoatConfigError(ValueError):
    """The `Moat` was built with something it cannot enforce (an unknown schema keyword, a bad value, no prompt)."""


# ---------------------------------------------------------------------------------------------------------------
# Role and system markers

# Default-ignorable and format code points, removed before matching (one shared table; no platform property lookup).
_IGNORABLE_RANGES = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F),
    (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF),
)
_INVISIBLE = re.compile("[" + "".join(
    f"\\U{lo:08x}" if lo == hi else f"\\U{lo:08x}-\\U{hi:08x}" for lo, hi in _IGNORABLE_RANGES) + "]")
# Cyrillic and Greek look-alikes of the marker letters, read as the Latin letter (comparison text only; version 2 of the
# shared confusables table, 47 entries). This is not general homoglyph detection.
_CONFUSABLES = {
    "а": "a", "А": "a", "с": "c", "С": "c",
    "е": "e", "Е": "e", "і": "i", "І": "i",
    "о": "o", "О": "o", "р": "p", "Р": "p",
    "ѕ": "s", "Ѕ": "s", "у": "y", "У": "y",
    "ԁ": "d", "ӏ": "l", "Ӏ": "l", "Т": "t",
    "М": "m", "Н": "h", "х": "x", "Х": "x",
    "Α": "a", "α": "a", "Ε": "e", "ε": "e",
    "Ι": "i", "ι": "i", "Ο": "o", "ο": "o",
    "Ρ": "p", "ρ": "p", "Τ": "t", "τ": "t",
    "Υ": "y", "υ": "u", "ν": "v", "Μ": "m",
    "Ν": "n", "Χ": "x", "χ": "x", "ϲ": "c",
    "Ϲ": "c", "Η": "h", "т": "t",
}
_LINE_BREAKS = re.compile("\r\n|[\r\u0085\u2028\u2029\x0b\x0c]")
_HSPACE = re.compile("[\t \u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]+")
_ROLE_LINE = re.compile(r"^[ \t]*(system|assistant|developer|tool|user)[ \t]*:", re.MULTILINE)
_ROLE_JSON = re.compile(r"""["']role["']\s*:\s*["'](system|developer|assistant)["']""")
_MARKERS = ("<|system|>", "<|im_start|>", "<|im_end|>", "[inst]", "<<sys>>", "### system", "### instruction")


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if unicodedata.category(c) not in ("Mn", "Me"))
    text = _INVISIBLE.sub("", text)
    text = "".join(_CONFUSABLES.get(c, c) for c in text)
    text = _LINE_BREAKS.sub("\n", text).casefold()
    return _HSPACE.sub(" ", text)


def _marker_in(text: str) -> str | None:
    norm = _normalise(text)
    m = _ROLE_LINE.search(norm)
    if m:
        return f"a line starting with '{m.group(1)}:'"
    for marker in _MARKERS:
        if marker in norm:
            return f"the marker '{marker}'"
    if _ROLE_JSON.search(norm):
        return "a JSON object with a system, developer or assistant role"
    return None


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_strings(item)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(key)
            yield from _walk_strings(item)


def _role_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        role = value.get("role")
        if isinstance(role, str) and _normalise(role).strip() in ("system", "developer", "assistant"):
            return True
        return any(_role_key(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_role_key(v) for v in value)
    return False


# ---------------------------------------------------------------------------------------------------------------
# Schema subset

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_schema(schema: Any, path: str) -> None:
    if not isinstance(schema, dict):
        raise MoatConfigError(f"{path}: a schema must be a dict")
    for key in schema:
        if key not in _KEYWORDS:
            raise MoatConfigError(f"{path}: unsupported schema keyword {key!r} (supported: {', '.join(_KEYWORDS)})")
    if "type" in schema:
        t = schema["type"]
        names = t if isinstance(t, list) else [t]
        if not names or not all(isinstance(n, str) and n in _TYPES for n in names):
            raise MoatConfigError(f"{path}.type: must be one of {', '.join(_TYPES)}, or a list of them")
    if "properties" in schema:
        props = schema["properties"]
        if not isinstance(props, dict):
            raise MoatConfigError(f"{path}.properties: must be a dict of schemas")
        for name, sub in props.items():
            _check_schema(sub, f"{path}.properties.{name}")
    if "required" in schema and not (isinstance(schema["required"], list)
                                     and all(isinstance(r, str) for r in schema["required"])):
        raise MoatConfigError(f"{path}.required: must be a list of strings")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise MoatConfigError(f"{path}.additionalProperties: must be true or false")
    if "items" in schema:
        _check_schema(schema["items"], f"{path}.items")
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        raise MoatConfigError(f"{path}.enum: must be a non-empty list")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and not (isinstance(schema[key], int) and not isinstance(schema[key], bool)
                                  and schema[key] >= 0):
            raise MoatConfigError(f"{path}.{key}: must be a whole number of 0 or more")
    for key in ("minimum", "maximum"):
        if key in schema and not (_is_number(schema[key]) and math.isfinite(schema[key])):
            raise MoatConfigError(f"{path}.{key}: must be a number")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise MoatConfigError(f"{path}.pattern: must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error as e:
            raise MoatConfigError(f"{path}.pattern: not a valid regular expression ({e})") from e


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, str):
        return "string"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "integer" if v.is_integer() else "number"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def _same(a: Any, b: Any) -> bool:
    """JSON equality: true and 1 differ, 1 and 1.0 are equal."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    return type(a) is type(b) and a == b


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> str | None:
    """None when the value fits, else a short description of the first problem."""
    kind = _kind(value)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        ok = kind in names or (kind == "integer" and "number" in names)
        if not ok:
            return f"{path} is {kind}, expected {' or '.join(names)}"
    if "enum" in schema and not any(_same(value, e) for e in schema["enum"]):
        return f"{path} is not one of the allowed values"
    if "const" in schema and not _same(value, schema["const"]):
        return f"{path} is not the required value"
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} is shorter than {schema['minLength']} characters"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} is longer than {schema['maxLength']} characters"
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return f"{path} does not match the pattern"
    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} is below {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} is above {schema['maximum']}"
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} has fewer than {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} has more than {schema['maxItems']} items"
        if "items" in schema:
            for i, item in enumerate(value):
                problem = _validate(item, schema["items"], f"{path}[{i}]")
                if problem:
                    return problem
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                return f"{path} is missing {name!r}"
        props = schema.get("properties", {})
        for name, item in value.items():
            if name in props:
                problem = _validate(item, props[name], f"{path}.{name}")
                if problem:
                    return problem
            elif schema.get("additionalProperties") is False:
                return f"{path} has the unexpected key {name!r}"
    return None


def _wants_json(schema: Mapping[str, Any]) -> bool:
    t = schema.get("type")
    names = t if isinstance(t, list) else [t]
    return "string" not in names


# ---------------------------------------------------------------------------------------------------------------

class Moat:
    """A pinned prompt plus optional input schema, output schema and PII filter. See the module notes.

    Args:
      prompt: the system prompt, pinned. A non-empty string.
      input_schema: schema the untrusted input must fit (a string input is validated as a string).
      output_schema: schema the model output must fit. Unless its `type` includes `string`, a string answer is parsed
        as JSON first (a parse failure is an `output_schema` refusal).
      filters: `{"pii": filter}`; see the module notes. No other key is accepted.
    """

    def __init__(self, *, prompt: str, input_schema: Mapping[str, Any] | None = None,
                 output_schema: Mapping[str, Any] | None = None,
                 filters: Mapping[str, Any] | None = None) -> None:
        if not isinstance(prompt, str) or not prompt:
            raise MoatConfigError("prompt must be a non-empty string")
        for name, schema in (("input_schema", input_schema), ("output_schema", output_schema)):
            if schema is not None:
                _check_schema(schema, name)
        self._prompt = prompt
        self._input_schema = dict(input_schema) if input_schema is not None else None
        self._output_schema = dict(output_schema) if output_schema is not None else None
        pii: Callable[[str], Any] | None = None
        if filters is not None:
            unknown = set(filters) - {"pii"}
            if unknown:
                raise MoatConfigError(f"unknown filter {sorted(unknown)[0]!r} (supported: pii)")
            raw = filters.get("pii")
            if raw is not None:
                if callable(raw):
                    pii = raw
                elif callable(getattr(raw, "classify", None)):
                    pii = lambda text, _c=raw.classify: _c(text) != "clear"  # noqa: E731
                else:
                    raise MoatConfigError("filters['pii'] must be a function or an object with classify(text)")
        self._pii = pii

    def prompt_sha256(self) -> str:
        """Lowercase hex SHA-256 of the pinned prompt (UTF-8), for example to pass along when you call Rampart."""
        return hashlib.sha256(self._prompt.encode("utf-8")).hexdigest()

    def messages(self, untrusted_input: Any) -> list[dict[str, str]]:
        """`[{"role": "system", "content": prompt}, {"role": "user", "content": <input as text>}]`.

        A string input is the user text as it is; any other JSON value is sent as compact JSON text. Raises
        `MoatRefusal` with `role_marker` when the input carries a role or system marker, or `input_schema` when it
        does not fit the input schema (the marker check runs first).
        """
        try:
            text = untrusted_input if isinstance(untrusted_input, str) else json.dumps(
                untrusted_input, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as e:
            raise MoatRefusal("input_schema", "the input is not a JSON value") from e
        found = _marker_in(text)
        if found is None:
            for s in _walk_strings(untrusted_input):
                found = _marker_in(s)
                if found:
                    break
        if found is None and _role_key(untrusted_input):
            found = "a JSON object with a system, developer or assistant role"
        if found:
            raise MoatRefusal("role_marker", f"the input contains {found}")
        if self._input_schema is not None:
            problem = _validate(untrusted_input, self._input_schema, "input")
            if problem:
                raise MoatRefusal("input_schema", problem)
        return [{"role": "system", "content": self._prompt}, {"role": "user", "content": text}]

    def check_output(self, model_output: Any) -> Any:
        """The validated (and, for a JSON schema, parsed) output, or `MoatRefusal` (`output_schema` or `pii`)."""
        value = model_output
        parsed = False
        if self._output_schema is not None:
            if isinstance(model_output, str) and _wants_json(self._output_schema):
                try:
                    value = json.loads(model_output)
                    parsed = True
                except ValueError as e:
                    raise MoatRefusal("output_schema", "the output is not valid JSON") from e
            problem = _validate(value, self._output_schema, "output")
            if problem:
                raise MoatRefusal("output_schema", problem)
        if self._pii is not None:
            # The filter must see what the caller receives. A parsed output is written back as compact JSON, so a
            # JSON escape in the raw text cannot hide what the filter looks for; the raw text is checked as well.
            if isinstance(model_output, str):
                texts = [model_output]
                if parsed:
                    texts.append(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
            else:
                texts = [json.dumps(value, ensure_ascii=False, default=str)]
            try:
                flagged = False
                for text in texts:
                    if self._pii(text):
                        flagged = True
                        break
            except Exception as e:  # a filter that fails is a refusal, never a pass
                raise MoatRefusal("pii", f"the PII filter failed ({type(e).__name__})") from e
            if flagged:
                raise MoatRefusal("pii", "the output contains personal data")
        return value
