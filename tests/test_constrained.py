"""Step 05 tests: schema validation with exact paths, repair ladder, DST."""

from jarvis.constrained import SlotState, validate, validate_or_repair

TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["open_app", "close_app"]},
        "count": {"type": "integer", "minimum": 1, "maximum": 10},
        "target": {"type": "string", "minLength": 1},
        "tags": {"type": "array", "items": {"type": "string"}},
        "address": {
            "type": "object",
            "properties": {"zip": {"type": "string", "pattern": "^[0-9]{5}$"}},
            "required": ["zip"],
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


def _paths(errors):
    return [e.path for e in errors]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_valid_instance_has_no_errors():
    ok = {
        "action": "open_app",
        "count": 3,
        "target": "chrome",
        "tags": ["a", "b"],
        "address": {"zip": "12345"},
    }
    assert validate(ok, TOOL_SCHEMA) == []


def test_missing_required_reports_property_at_object_path():
    errors = validate({"count": 3}, TOOL_SCHEMA)
    assert len(errors) == 1
    assert errors[0].path == ""
    assert "'action'" in errors[0].message


def test_minimum_violation_points_at_the_field():
    errors = validate({"action": "open_app", "count": 0}, TOOL_SCHEMA)
    assert _paths(errors) == ["/count"]
    assert "minimum" in errors[0].message


def test_bool_is_not_an_integer():
    errors = validate({"action": "open_app", "count": True}, TOOL_SCHEMA)
    assert _paths(errors) == ["/count"]
    assert "integer" in errors[0].message


def test_enum_violation():
    errors = validate({"action": "dance"}, TOOL_SCHEMA)
    assert _paths(errors) == ["/action"]
    assert "enum" in errors[0].message


def test_nested_pattern_error_path():
    errors = validate({"action": "open_app", "address": {"zip": "ABC"}}, TOOL_SCHEMA)
    assert _paths(errors) == ["/address/zip"]


def test_nested_missing_required_path():
    errors = validate({"action": "open_app", "address": {}}, TOOL_SCHEMA)
    assert _paths(errors) == ["/address"]
    assert "'zip'" in errors[0].message


def test_array_item_error_path():
    errors = validate({"action": "open_app", "tags": ["ok", 7]}, TOOL_SCHEMA)
    assert _paths(errors) == ["/tags/1"]


def test_additional_property_rejected():
    errors = validate({"action": "open_app", "bogus": 1}, TOOL_SCHEMA)
    assert any("additional property 'bogus'" in e.message for e in errors)


def test_root_type_error():
    errors = validate([1, 2], TOOL_SCHEMA)
    assert _paths(errors) == [""]
    assert "object" in errors[0].message


# ---------------------------------------------------------------------------
# Repair ladder
# ---------------------------------------------------------------------------


def test_repair_accepts_fenced_json():
    raw = '```json\n{"action": "open_app", "count": 2}\n```'
    result = validate_or_repair(raw, TOOL_SCHEMA)
    assert result.ok
    assert result.value == {"action": "open_app", "count": 2}


def test_repair_extracts_json_from_prose():
    raw = 'Sure! Here is the call: {"action": "close_app"} - hope that helps.'
    result = validate_or_repair(raw, TOOL_SCHEMA)
    assert result.ok
    assert result.value["action"] == "close_app"


def test_repair_returns_exact_schema_errors_for_feedback():
    raw = '{"action": "open_app", "count": 0}'
    result = validate_or_repair(raw, TOOL_SCHEMA)
    assert not result.ok
    assert "/count" in result.feedback
    assert "minimum" in result.feedback


def test_repair_reports_parse_failure():
    result = validate_or_repair("I cannot do that.", TOOL_SCHEMA)
    assert not result.ok
    assert "parse error" in result.feedback


# ---------------------------------------------------------------------------
# SlotState - the three DST invariants
# ---------------------------------------------------------------------------


def test_set_never_resets_untouched_slots():
    s = SlotState()
    s.set("cuisine", "indian")
    s.set("time", "19:00")
    s.set("people", 2)
    assert s.snapshot() == {"cuisine": "indian", "time": "19:00", "people": 2}


def test_correction_overwrites_only_that_slot():
    s = SlotState()
    s.set("cuisine", "indian")
    s.set("time", "19:00")
    s.correct("cuisine", "chinese")
    assert s.snapshot() == {"cuisine": "chinese", "time": "19:00"}


def test_negation_clears_exactly_one_slot():
    s = SlotState()
    s.set("cuisine", "indian")
    s.set("time", "19:00")
    s.negate("time")
    assert s.snapshot() == {"cuisine": "indian"}
    s.negate("nonexistent")  # no-op, no exception
    assert s.snapshot() == {"cuisine": "indian"}


def test_history_records_operations_in_order():
    s = SlotState()
    s.set("a", 1)
    s.correct("a", 2)
    s.negate("a")
    assert [h[0] for h in s.history] == ["set", "correct", "negate"]
    assert s.history[1] == ("correct", "a", 1, 2)


def test_snapshot_is_a_defensive_copy():
    s = SlotState()
    s.set("a", 1)
    snap = s.snapshot()
    snap["b"] = 99
    assert "b" not in s
