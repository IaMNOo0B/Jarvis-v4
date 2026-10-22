"""Validate — JARVIS v4 (NOVA's schema discipline).

A tool's advertised schema is a CONTRACT; until now nothing
checked a call against it — a model could pass a string where
a number belonged, drop a required field, or invent keys, and
the op would fail deep inside (or worse, half-work). Now the
registry validates every call against the schema BEFORE the
op runs and refuses with the exact field named.

The validator covers the JSON-Schema subset this codebase
actually declares: object properties, required, types
(string / number / integer / boolean / array / object),
minLength, minimum / maximum, additionalProperties: false,
and one level of nested objects. A tool with no schema is
unchecked (its own op is then the only guard) — coverage is
measured by the test suite, not assumed.
"""

from __future__ import annotations

_TYPE_OK = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: (isinstance(v, int)
                          and not isinstance(v, bool))
                         or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float))
                        and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _check_value(name: str, spec: dict, value) -> str:
    kind = spec.get("type")
    if kind and kind in _TYPE_OK and not _TYPE_OK[kind](value):
        return f"{name}: {kind} chahiye tha, mila " \
               f"{type(value).__name__}"
    if kind == "string" and "minLength" in spec:
        if len(value) < spec["minLength"]:
            return f"{name}: kam se kam {spec['minLength']} " \
                   "characters chahiye"
    if kind in ("number", "integer"):
        if "minimum" in spec and value < spec["minimum"]:
            return f"{name}: {spec['minimum']} se chhota hai"
        if "maximum" in spec and value > spec["maximum"]:
            return f"{name}: {spec['maximum']} se bada hai"
    if kind == "object":
        return validate_args(spec, value, prefix=name + ".")
    return ""


def validate_args(schema, args, prefix: str = "") -> str:
    """'' when the call honours the schema; otherwise the first
    problem, naming the field. Never raises."""
    if not isinstance(schema, dict) or not schema:
        return ""
    if schema.get("type") not in (None, "object"):
        return ""
    if not isinstance(args, dict):
        return "arguments object hone chahiye"
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in args or args[key] is None:
            return f"{prefix}{key}: ye field zaroori hai"
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in props:
                return f"{prefix}{key}: ye field schema me " \
                       "hai hi nahi"
    for key, value in args.items():
        spec = props.get(key)
        if isinstance(spec, dict) and value is not None:
            problem = _check_value(prefix + key, spec, value)
            if problem:
                return problem
    return ""
