"""Step 05 tests: token-fertility tax measurement."""

import pytest

from jarvis.token_tax import (
    adjusted_budget,
    fertility,
    heuristic_token_count,
    measure_token_tax,
    word_count,
)


def test_heuristic_counter_exact_small_cases():
    assert heuristic_token_count("") == 0
    assert heuristic_token_count("abcd") == 1  # 4 latin chars / 4 = 1
    assert heuristic_token_count("कक") == 2  # 2 devanagari chars / 1.5 -> ceil 2
    assert heuristic_token_count("abcdefgh") == 2


def test_fertility_with_injected_counter_is_exact():
    counter = lambda t: 3 * word_count(t)  # noqa: E731 - test stub
    assert fertility("one two three four", counter=counter) == 3.0
    assert fertility("", counter=counter) == 0.0


def test_measure_with_injected_counter_exact_ratios():
    samples = {
        "english": "a b c d e f g h i j",  # 10 words
        "devanagari": "क ख ग घ ङ च छ ज झ ञ",  # 10 words
    }
    table = {samples["english"]: 13, samples["devanagari"]: 40}
    report = measure_token_tax(samples=samples, counter=lambda t: table[t])
    assert report.band("english").tokens_per_word == pytest.approx(1.3)
    assert report.band("devanagari").tokens_per_word == pytest.approx(4.0)
    assert report.band("devanagari").ratio_vs_english == pytest.approx(40 / 13)
    assert report.recommended_multiplier == pytest.approx(40 / 13)


def test_default_samples_show_the_tax():
    report = measure_token_tax()
    eng = report.band("english").tokens_per_word
    deva = report.band("devanagari").tokens_per_word
    roman = report.band("roman_hinglish").tokens_per_word
    # Devanagari is heavily taxed; Roman Hinglish is not cheaper than
    # English by any large margin; the multiplier tracks the worst band.
    assert deva > 1.5 * eng
    assert roman >= 0.8 * eng
    assert report.recommended_multiplier == pytest.approx(
        report.band("devanagari").ratio_vs_english
    )
    assert 1.0 < report.recommended_multiplier <= 6.0


def test_adjusted_budget_grows_with_the_tax():
    budget = adjusted_budget(1000)
    assert budget > 1000


def test_multiplier_clamped_for_pathological_counter():
    samples = {"english": "a b", "devanagari": "क ख"}
    table = {samples["english"]: 2, samples["devanagari"]: 10000}
    report = measure_token_tax(samples=samples, counter=lambda t: table[t])
    assert report.recommended_multiplier == 6.0
