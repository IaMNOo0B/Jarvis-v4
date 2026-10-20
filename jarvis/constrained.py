"""Step 05 - NLP: constrained outputs and dialogue-state discipline.

Three deterministic mechanisms (study Part C, lessons 20 and 29):

1. A JSON-Schema-subset validator for tool-call arguments.  Every
   error carries a JSON-Pointer-style path (``/address/zip``,
   ``/tags/1``) so the failure is located exactly, not vaguely.

2. A repair ladder, ``validate_or_repair``: extract the JSON from a
   model reply (bare, fenced, or embedded in prose), parse it, validate
   it, and return either the parsed object or the *exact* schema errors
   to feed back to the model for a corrected retry.  This is the
   deterministic half of constrained decoding available at the harness
   layer: the logit-masking half (grammar-constrained generation) lives
   inside the inference engine; what lives here - extraction, precise
   validation, precise feedback - is what makes retries converge instead
   of wander.

3. ``SlotState``: dialogue-state tracking with the three update
   invariants from the DST lesson - setting a slot NEVER resets
   untouched slots, an explicit negation clears exactly that slot, and
   a correction overwrites (never appends).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 1. Schema-subset validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaError:
    path: str  # JSON-Pointer style: "" is the root, "/a/0/b" descends
    message: str

    @property
    def display(self) -> str:
        return f"{self.path or '$'}: {self.message}"

    def __str__(self) -> str:
        return self.display


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _type_matches(value, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        # bool is a subclass of int in Python; it is NOT an integer here.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True  # unknown type keyword: not this subset's business


def _escape_pointer(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def validate(instance, schema, path: str = "") -> list:
    """Validate ``instance`` against a JSON-Schema subset.

    Supported keywords: ``type``, ``enum``, ``properties``,
    ``required``, ``additionalProperties`` (bool or schema), ``items``,
    ``minItems``/``maxItems``, ``minimum``/``maximum``,
    ``minLength``/``maxLength``, ``pattern``.  Returns a list of
    ``SchemaError`` (empty when valid).  Every error path points at the
    exact sub-value that failed.
    """
    errors: list[SchemaError] = []
    if not isinstance(schema, dict):
        return errors

    expected = schema.get("type")
    if expected is not None and not _type_matches(instance, expected):
        errors.append(
            SchemaError(path, f"expected type {expected}, got {_type_name(instance)}")
        )
        return errors  # deeper checks are meaningless on the wrong type

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(SchemaError(path, f"value {instance!r} not in enum {schema['enum']!r}"))

    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(SchemaError(path, f"missing required property '{key}'"))
        addl = schema.get("additionalProperties", True)
        for key, sub in instance.items():
            child = f"{path}/{_escape_pointer(str(key))}"
            if key in props:
                errors.extend(validate(sub, props[key], child))
            elif addl is False:
                errors.append(SchemaError(path, f"additional property '{key}' not allowed"))
            elif isinstance(addl, dict):
                errors.extend(validate(sub, addl, child))

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(
                SchemaError(path, f"array has {len(instance)} items, minimum is {schema['minItems']}")
            )
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(
                SchemaError(path, f"array has {len(instance)} items, maximum is {schema['maxItems']}")
            )
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(instance):
                errors.extend(validate(item, item_schema, f"{path}/{idx}"))

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(
                SchemaError(path, f"value {instance} is below minimum {schema['minimum']}")
            )
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(
                SchemaError(path, f"value {instance} is above maximum {schema['maximum']}")
            )

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(
                SchemaError(path, f"string length {len(instance)} below minLength {schema['minLength']}")
            )
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(
                SchemaError(path, f"string length {len(instance)} above maxLength {schema['maxLength']}")
            )
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(
                SchemaError(path, f"string {instance!r} does not match pattern {schema['pattern']!r}")
            )

    return errors


# ---------------------------------------------------------------------------
# 2. Repair ladder: extract -> parse -> validate -> precise feedback
# ---------------------------------------------------------------------------


@dataclass
class RepairResult:
    ok: bool
    value: object = None
    errors: list = field(default_factory=list)  # list[SchemaError]

    @property
    def feedback(self) -> str:
        """The exact text to hand back to the model for a corrected retry."""
        if self.ok:
            return ""
        lines = ["Your previous reply was not valid. Fix these errors:"]
        lines.extend(f"- {e.display}" for e in self.errors)
        return "\n".join(lines)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _json_candidates(raw: str):
    text = raw.strip()
    yield text
    for match in _FENCE_RE.finditer(text):
        yield match.group(1).strip()
    # Embedded JSON: widest span from the first opener to its last closer.
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if starts:
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end > start:
            yield text[start : end + 1]


def extract_json(raw: str):
    """Best-effort extraction of one JSON value from a model reply.

    Tries the whole reply, then any fenced block, then the widest
    brace/bracket span.  Returns ``(value, None)`` on success or
    ``(None, error_message)`` when no candidate parses.
    """
    last_error = "no JSON candidate found"
    for candidate in _json_candidates(raw):
        if not candidate:
            continue
        try:
            return json.loads(candidate), None
        except json.JSONDecodeError as exc:
            last_error = f"JSON parse error: {exc.msg} at line {exc.lineno} column {exc.colno}"
    return None, last_error


def validate_or_repair(raw_text: str, schema) -> RepairResult:
    """Run the repair ladder on a raw model reply.

    Success: ``RepairResult(ok=True, value=<parsed object>)``.
    Failure: ``ok=False`` with precise ``SchemaError``s - either the
    parse failure or the schema violations with exact paths - ready to
    feed back to the model verbatim (``result.feedback``).
    """
    value, parse_error = extract_json(raw_text)
    if parse_error is not None:
        return RepairResult(ok=False, errors=[SchemaError("", parse_error)])
    errors = validate(value, schema)
    if errors:
        return RepairResult(ok=False, errors=errors)
    return RepairResult(ok=True, value=value)


# ---------------------------------------------------------------------------
# 3. Dialogue-state slots (DST invariants)
# ---------------------------------------------------------------------------


class SlotState:
    """Slot-value dialogue state with the three DST update invariants.

    * ``set`` writes one slot and leaves every other slot untouched -
      state is never regenerated wholesale, so untouched slots cannot
      be silently reset.
    * ``negate`` (explicit negation: "no cab", "not tomorrow") clears
      exactly that slot; negating an absent slot is a no-op.
    * ``correct`` ("actually, make it 8") overwrites the slot's value;
      it never appends and never touches other slots.

    Every mutation is appended to ``history`` as
    ``(op, slot, old_value, new_value)`` for audit and tests.
    """

    def __init__(self, initial: dict | None = None):
        self._slots: dict = dict(initial or {})
        self.history: list = []

    def set(self, slot: str, value) -> None:
        old = self._slots.get(slot)
        self._slots[slot] = value
        self.history.append(("set", slot, old, value))

    def correct(self, slot: str, value) -> None:
        old = self._slots.get(slot)
        self._slots[slot] = value
        self.history.append(("correct", slot, old, value))

    def negate(self, slot: str) -> None:
        old = self._slots.pop(slot, None)
        self.history.append(("negate", slot, old, None))

    def get(self, slot: str, default=None):
        return self._slots.get(slot, default)

    def __contains__(self, slot: str) -> bool:
        return slot in self._slots

    def snapshot(self) -> dict:
        """A copy of the current state; mutating it cannot affect us."""
        return dict(self._slots)

    def __eq__(self, other) -> bool:
        if isinstance(other, SlotState):
            return self._slots == other._slots
        if isinstance(other, dict):
            return self._slots == other
        return NotImplemented

    def __repr__(self) -> str:
        return f"SlotState({self._slots!r})"


__all__ = [
    "SchemaError",
    "validate",
    "RepairResult",
    "extract_json",
    "validate_or_repair",
    "SlotState",
]
