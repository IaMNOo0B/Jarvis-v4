"""Dictation Polisher — JARVIS v4 (Sentient install, Spec 8).

Spoken dictation reaches the brain tidied up, never slowed
down. Two layers, in the order that keeps the voice path
honest about latency:

1. **Deterministic tidy (always, instant).** Filler words
   (um, uh, erm, hmm) are dropped, whitespace collapsed,
   spacing before punctuation fixed, and the first letter
   capitalized. This alone fixes most of what raw STT
   output looks like, and it never waits on a model.

2. **Local-model polish (longer utterances only).** With
   four or more words, a tool-free LOCAL brain may clean
   further — but its output is admitted only under a
   word-preservation check in code: every non-filler word
   of the input must appear in the output, in order, and
   no new words may appear. A model that rephrases,
   translates, drops or invents words is rejected
   wholesale and the deterministic tidy is used instead.

Voice never blocks on polish: any failure — no model,
timeout, garbage — returns the tidied input. Roman
Hinglish passes through untouched when its words are
preserved, which is the whole point: cleanup, never
translation.
"""

from __future__ import annotations

import re

FILLERS = {"um", "uh", "erm", "hmm", "mm", "uhm", "uhh"}
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MIN_WORDS_FOR_MODEL = 4


def _norm(word: str) -> str:
    return word.lower().strip(".,!?;:'\"()[]")


def words_of(text: str) -> list:
    return [_norm(w) for w in _WORD_RE.findall(str(text))
            if _norm(w)]


def tidy(text: str) -> str:
    """The deterministic cleanup — the floor every
    utterance gets, model or no model."""
    out = str(text or "")
    out = re.sub(r"\s+", " ", out).strip()
    kept = []
    for piece in out.split(" "):
        core = _norm(piece)
        if core in FILLERS:
            continue
        kept.append(piece)
    out = " ".join(kept)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    out = re.sub(r"([,.!?;:])([^\s])", r"\1 \2", out)
    for i, ch in enumerate(out):
        if ch.isalpha():
            out = out[:i] + ch.upper() + out[i + 1:]
            break
    return out.strip()


def _preserves(input_words: list, output_words: list) -> bool:
    """The admission check: output words == input words
    minus fillers, in the same order. (Case and
    punctuation are ignored — they are exactly what the
    polish is allowed to change.)"""
    wanted = [w for w in input_words if w not in FILLERS]
    return output_words == wanted


def polish(text: str, brain_call=None) -> str:
    """Tidy one utterance. `brain_call(prompt) -> str` is
    the optional local-model hook; without it (or on any
    doubt) the deterministic tidy is the answer."""
    base = tidy(text)
    if brain_call is None:
        return base
    in_words = words_of(text)
    if len([w for w in in_words if w not in FILLERS]) \
            < _MIN_WORDS_FOR_MODEL:
        return base
    prompt = (
        "Tum dictation safai-karta ho. Neeche ek bola hua "
        "jumla hai (STT output). Use saaf karo: fillers "
        "hatao, punctuation aur capitalization theek "
        "karo. SHABD BILKUL NA BADLO — koi shabd na "
        "hatao, na jodo, na badlo, na translate karo; "
        "tarteeb waisi hi. Sirf saaf jumla wapas karo, "
        "kuch aur nahi.\n"
        f"Jumla: {str(text)[:500]}")
    try:
        out = str(brain_call(prompt) or "").strip()
    except Exception:
        return base
    if not out:
        return base
    if _preserves(in_words, words_of(out)):
        return out
    return base
