"""Step 05 - NLP: the Hindi/Hinglish token-fertility tax, measured.

Tokenizers are trained mostly on English, so equivalent Hindi text
costs roughly 3-5x the tokens (study Part C, tokenization lessons):
*fertility* - tokens per word - is far higher for Devanagari, and
Roman Hinglish sits in between.  A prompt budget sized on English
assumptions silently under-budgets every Hinglish turn.

This module makes the tax a *measurement*, not a vibe:

* ``heuristic_token_count`` - a deterministic stand-in counter with a
  per-script cost model (Devanagari characters are far more expensive
  per character than Latin ones).  It exists so the measurement
  pipeline runs anywhere, with zero dependencies.
* Any real tokenizer can be injected instead: pass any
  ``count(text) -> int`` callable (e.g. a tiktoken/transformers
  wrapper) and every number below comes from that counter.
* ``measure_token_tax`` reports tokens-per-word per language band and
  a *budget multiplier*: how much bigger the prompt budget must be so
  a Hinglish conversation gets the same headroom an English one gets.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# Equivalent-meaning reference sentences, one per language band.
DEFAULT_SAMPLES = {
    "english": (
        "Please book a cab for tomorrow morning at seven and remind me "
        "to carry the documents with me."
    ),
    "devanagari": (
        "कृपया कल सुबह सात बजे के लिए एक कैब बुक करो और मुझे "
        "दस्तावेज़ साथ ले जाने की याद दिलाओ।"
    ),
    "roman_hinglish": (
        "Please kal subah saat baje ke liye ek cab book karo aur mujhe "
        "documents saath le jaane ki yaad dilao."
    ),
}

_DEVANAGARI_LO = "\u0900"
_DEVANAGARI_HI = "\u097f"

# Heuristic per-script costs (tokens per character), chosen so typical
# prose lands near real BPE behaviour: ~4 Latin chars/token, while a
# Devanagari character - 3 UTF-8 bytes, sparsely covered by most
# vocabularies - costs roughly 2/3 of a token on its own.
_LATIN_CHARS_PER_TOKEN = 4.0
_DEVANAGARI_CHARS_PER_TOKEN = 1.5
_OTHER_CHARS_PER_TOKEN = 2.0


def word_count(text: str) -> int:
    """Whitespace word count - adequate for all three language bands."""
    return len(text.split())


def heuristic_token_count(text: str) -> int:
    """Deterministic stand-in tokenizer (documented heuristic).

    Counts Devanagari-block characters at the Devanagari rate, ASCII
    and Latin text at the Latin rate, everything else at a middle
    rate; each class rounds up.  Deterministic and monotone - good
    enough to size a budget until a real tokenizer is injected.
    """
    if not text:
        return 0
    deva = latin = other = 0
    for ch in text:
        if _DEVANAGARI_LO <= ch <= _DEVANAGARI_HI:
            deva += 1
        elif ord(ch) < 0x250:  # Latin-1 and below: English/Hinglish text
            latin += 1
        else:
            other += 1
    return (
        math.ceil(deva / _DEVANAGARI_CHARS_PER_TOKEN)
        + math.ceil(latin / _LATIN_CHARS_PER_TOKEN)
        + math.ceil(other / _OTHER_CHARS_PER_TOKEN)
    )


def fertility(text: str, counter=heuristic_token_count) -> float:
    """Tokens per word of ``text`` under ``counter`` (0.0 for no words)."""
    words = word_count(text)
    if words == 0:
        return 0.0
    return counter(text) / words


@dataclass
class LanguageBand:
    name: str
    text: str
    tokens: int
    words: int
    tokens_per_word: float
    ratio_vs_english: float = 1.0


@dataclass
class TokenTaxReport:
    bands: dict = field(default_factory=dict)  # name -> LanguageBand
    recommended_multiplier: float = 1.0

    def band(self, name: str) -> LanguageBand:
        return self.bands[name]


def measure_token_tax(samples: dict | None = None, counter=heuristic_token_count) -> TokenTaxReport:
    """Measure fertility per language band and the budget multiplier.

    ``samples`` maps band name -> representative text; it should
    include an ``"english"`` band, which anchors every ratio.  The
    recommended multiplier is the worst band's fertility divided by
    English fertility, clamped to ``[1.0, 6.0]`` - the study's observed
    tax tops out around 5x, and a clamp keeps a pathological sample
    from exploding the budget.
    """
    samples = dict(samples or DEFAULT_SAMPLES)
    bands = {}
    for name, text in samples.items():
        tokens = counter(text)
        words = word_count(text)
        bands[name] = LanguageBand(
            name=name,
            text=text,
            tokens=tokens,
            words=words,
            tokens_per_word=(tokens / words) if words else 0.0,
        )
    english = bands.get("english")
    multiplier = 1.0
    if english and english.tokens_per_word > 0:
        for band in bands.values():
            band.ratio_vs_english = band.tokens_per_word / english.tokens_per_word
        worst = max(b.ratio_vs_english for b in bands.values())
        multiplier = min(6.0, max(1.0, worst))
    return TokenTaxReport(bands=bands, recommended_multiplier=multiplier)


def adjusted_budget(base_tokens: int, report: TokenTaxReport | None = None) -> int:
    """Prompt budget for mixed/Hinglish traffic given an English-sized base.

    ``base_tokens`` is the budget that works for English; the returned
    budget gives Hinglish turns the same effective headroom.
    """
    report = report or measure_token_tax()
    return math.ceil(base_tokens * report.recommended_multiplier)


__all__ = [
    "DEFAULT_SAMPLES",
    "word_count",
    "heuristic_token_count",
    "fertility",
    "LanguageBand",
    "TokenTaxReport",
    "measure_token_tax",
    "adjusted_budget",
]
