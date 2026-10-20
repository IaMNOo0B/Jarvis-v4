"""Step 17 — system-prompt byte stability (prefix-cache discipline).

The stable prefix (persona + protocol, above the boundary) must be
byte-identical across turns; anything dynamic lives below the boundary.
A one-word change inside the prefix must be caught with its offset and
its section; changes below the boundary must pass.
"""

import pytest

from jarvis.prompt_stability import (DEFAULT_BOUNDARY, assert_stable,
                                     check_stability, common_prefix_len,
                                     split_stable)

B = DEFAULT_BOUNDARY

PREFIX = (
    "## ROLE\n"
    "You are JARVIS, a calm desktop assistant.\n"
    "## PROTOCOL\n"
    "Answer briefly. Use tools when asked.\n"
)


def _turn(dynamic: str) -> str:
    return PREFIX + B + "\n" + dynamic


def test_stable_sequence_passes():
    prompts = [_turn("Time now: Monday morning"),
               _turn("Time now: Monday evening, different memory excerpt"),
               _turn("Completely different dynamic tail 12345")]
    report = check_stability(prompts)
    assert report.ok
    assert report.checked == 3
    assert report.reference_len == len(PREFIX)
    assert report.first_divergence_offset is None


def test_one_word_change_inside_prefix_is_caught_with_offset_and_section():
    changed = PREFIX.replace("briefly", "shortly")  # one word, in PROTOCOL
    prompts = [_turn("a"), PREFIX.replace("briefly", "shortly") + B + "\nb"]
    report = check_stability(prompts)
    assert not report.ok
    assert report.first_divergence_offset == PREFIX.index("briefly")
    div = [v for v in report.violations if v.kind == "prefix_diverged"]
    assert len(div) == 1
    assert div[0].prompt_index == 1
    assert div[0].section == "PROTOCOL"


def test_change_in_role_section_names_role():
    changed = PREFIX.replace("calm", "warm")
    report = check_stability([_turn("a"), changed + B + "\nb"])
    div = [v for v in report.violations if v.kind == "prefix_diverged"][0]
    assert div.offset == PREFIX.index("calm")
    assert div.section == "ROLE"


def test_dynamic_timestamp_above_boundary_is_flagged():
    bad = ("## ROLE\nYou are JARVIS.\nSession started 2026-10-20T23:20:00\n"
           + B + "\ntail")
    report = check_stability([bad])
    kinds = {v.kind for v in report.violations}
    assert "dynamic_in_stable_prefix" in kinds
    assert not report.ok


def test_missing_boundary_is_a_violation():
    report = check_stability([PREFIX + "no marker here"])
    assert not report.ok
    assert any(v.kind == "boundary_missing" for v in report.violations)


def test_empty_sequence_proves_nothing():
    report = check_stability([])
    assert not report.ok
    assert report.violations[0].kind == "no_prompts"


def test_assert_stable_raises_and_split_works():
    with pytest.raises(ValueError):
        assert_stable([_turn("a"), _turn("b").replace("JARVIS", "JARVIS!")])
    stable, dynamic = split_stable(_turn("the tail"))
    assert stable == PREFIX and "the tail" in dynamic
    assert split_stable("no marker") is None


def test_common_prefix_len():
    assert common_prefix_len("abc", "abd") == 2
    assert common_prefix_len("abc", "abc") == 3
    assert common_prefix_len("", "abc") == 0
