# V4 BUILD STEPS — ai-engineering-from-scratch process, ek ke baad ek

Har step: audit → gap build → saboot. Saboot aaye baghair step complete nahi.
Poora frame: docs/BUILD-FRAME.md

- [ ] 00 Setup & Tooling — route-aware preflight + data-dir discipline
      Saboot: fresh folder launch, preflight green ya exact fix-message
- [ ] 01 Math Foundations — proactivity false-alarm audit (base-rate)
      Saboot: har alert type ka expected false-alarm rate likha hua
- [ ] 02 ML Fundamentals — drift (PSI) + eval kanoon harness
      Saboot: harness chalta hai, report number deti hai
- [ ] 03 Deep Learning Core — team samajh + quantization PPL bands
      Saboot: notes + band table (koi user-build nahi)
- [ ] 04 Computer Vision — CMER do-not-click gate + float crops + tracking
      Saboot: DPI fixture tests + click-block cases
- [ ] 05 NLP — constrained decoding (schema-valid JSON) + DST niyam + token tax
      Saboot: malformed-JSON regression set 100% schema-valid
- [ ] 06 Speech & Audio — Silero VAD tier + 400ms SLO + flush trick
      Saboot: PC pe latency naap SLO ke khilaf
- [ ] 07 Transformers — speculative decoding tuning (alpha naap)
      Saboot: on-card benchmark, tokens/sec pehle vs baad
- [ ] 08 Generative AI — LoRA primitive (09/10 ki machine)
      Saboot: adapter load/unload test
- [ ] 09 Reinforcement Learning — DPO on golden pairs + verifier rewards
      Saboot: eval suite DPO se pehle vs baad (number)
- [ ] 10 LLMs from Scratch — model vetting calculator
      Saboot: published counts se match (Llama-3-8B exact)
- [ ] 11 LLM Engineering — RAG upgrade (RRF + parent-child + HyDE) + voting
      Saboot: recall@5 purana vs naya, Hinglish set pe
- [ ] 12 Multimodal — watcher token budget + grounding discipline
      Saboot: per-glance token cost budget ke andar
- [ ] 13 Tools & Protocols — idempotency ledger + hash-pinning + argv requests
      Saboot: kill-mid-send test — dohraya asar ZERO
- [ ] 14 Agent Engineering — behaviour eval suite (full) + ratchet + ReWOO
      Saboot: har guardrail ke saath uska eval case; baseline number
- [ ] 15 Autonomous Systems — evaluator firewall + canaries + spend trip
      Saboot: canary-leak drill alarm bajata hai
- [ ] 16 Multi-Agent — memory provenance + append-only supersession
      Saboot: poisoning drill — jhooti entry fact nahi banti
- [ ] 17 Infra & Production — goodput SLO + prompt byte-stability + DR
      Saboot: provider-bad drill; data-restore drill
- [ ] 18 Ethics, Safety & Alignment — IFC labels + attack regression sets
      Saboot: injection set pe koi untrusted-origin action nahi
- [ ] 19 Capstone — JARVIS khud: over-refusal naap + acceptance din
      Saboot: acceptance list — sab chala, sab naapa


## STATUS — 2026-10-20 (raat) — Ovi ki apni verification ke baad

- **440 tests green** (meri apni full-suite run, still tree pe).
- **Behaviour eval baseline (stub mode): 11/11** — `evals/BASELINE-V4.md`.
  Saare asli failure cases (open chrome, web search, Hinglish reminder,
  bland deflection, lambi baat-cheet ka sannata, 404 imaandari) ab
  pipeline se pass hote hain.
- Jinda smoke (meri VM pe): time fast-path sahi jawab; greeting asli
  provider chain se; remember→recall chakkar pura.
- **PC-side saboot baaki** (sirf owner ke PC pe naape jayenge, isiliye
  kuch boxes khule hain): Step 06 awaaz ki asli latency SLOs ke khilaf;
  Step 07 α asli Hinglish traffic pe + on-card benchmark; Step 09 DPO
  training run (data machine tayyaar hai); Step 17 outage/restore
  drills; Step 19 acceptance din. `ACCEPTANCE.md` in sab ko
  NOT-MEASURED dikhata hai — andazay se nahi bhara gaya.
- Step 08/11 ke boxes: core machines ban chuki hain; unke PC-side
  hisse upar wali list me ginne gaye hain.
