# JARVIS v4 — ACCEPTANCE REPORT (Step 19)

Assembled by `python -m jarvis.acceptance` from the repo's own modules. Every number below is a real local value or is marked **NOT-MEASURED** with the place its measurement will come from. No number is estimated.

## 1. Preflight (Step 00)
- Result: **NOT READY** — 7 checks, 1 required failed, 4 optional missing (this machine; the owner's PC run is the one that counts).
  - [ok] Python version (required): found Python 3.12.3; JARVIS v4 needs 3.11 or newer
  - [ok] JARVIS program files (required): program folder found at /home/hatch/workspace/jarvis-v4/jarvis
  - [MISS] Data folder (required): data folder not found at /home/hatch/workspace/jarvis-v4/data
  - [MISS] Ollama (offline brain) (optional): Ollama is not answering (URLError)
  - [MISS] Wake-word models folder (optional): models folder not found at /home/hatch/workspace/jarvis-v4/models
  - [MISS] Voice typing pack (faster-whisper) (optional): faster_whisper is not installed
  - [MISS] Kokoro voice pack (optional): kokoro is not installed

## 2. Behaviour eval baseline (Step 14, stub mode)
- Totals: **10/11 passed** (stub brain — proves the pipeline, NOT the real brain; real-brain numbers come from provider mode on the owner's PC).
  - coordination: 1/1
  - over_refusal: 1/1
  - specification: 6/7
  - verification: 2/2

## 3. Provider goodput (Step 17, SLO 10000 ms, goodput = ok AND quality AND within SLO; never averages)
- **NOT-MEASURED** — no captured provider traffic (evals/goodput.jsonl) in this checkout. Goodput is measured from live turns on the owner's PC, where the real providers answer.

## 4. Over-refusal rate (Step 19 — capstone 84: reported SEPARATELY from plain failures)
- **NOT-MEASURED** — no captured reply set (evals/replies.jsonl) in this checkout. The rate is computed by jarvis.acceptance.measure_over_refusal over real replies from a provider-mode run on the owner's PC.
  - Stub-mode signal only (NOT the live rate): the baseline's over_refusal category passed 1/1.

## 5. Journal integrity (hash chain)
- Live journal: **NOT-MEASURED** on this machine — no data/journal.jsonl in this checkout; the live journal is created and verified on the owner's PC.
- Integrity drill (fresh journal, 3 probe records, mechanism check): chain VERIFIED.

## Verdict
- Measured here: preflight, eval baseline (stub), journal integrity drill.
- **NOT-MEASURED**: provider goodput, over-refusal rate, live journal integrity — each names the owner's-PC run that will produce it.
- Standing rule: an item without a measurement is NOT-MEASURED, never 'done'.
