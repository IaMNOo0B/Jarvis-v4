# LEARN — ai-engineering-from-scratch (rohitg00) — FULL DEEP STUDY

Studied: 2026-10-20, on Owais's order ("ek ek cheez dekho, yaad karo").
Method: 10 parallel study teams, every lesson doc + main code file read in full.
Notes (the memory): `~/workspace/jarvis-knowledge/ai-eng-study/` — 20 files, ~1.18 MB
(parts A–J + B/D chunk files + 3 cheatsheet volumes). Repo cloned at
`~/workspace/research/ai-eng` (MIT).

## COVERAGE RECEIPT (verified by arithmetic, not trust)
- Phases 00–18: 457 lessons (A 56 · B 41 · C 46 · D 43 · E 41 · F 70 · G 54 · H 47 · I 59)
- Phase 19: 85 capstone projects (126 docs)
- **457 + 85 = 542 = the repo's own claimed lesson count. Full coverage.**
- Plus: 155 cheatsheets, book/ (6 volumes), 13 learning-paths, glossary (60+ terms).
- Note: the repo's ROADMAP footer claims 523 lessons / ~332 h vs README's 542 / ~1,081 h
  — internally inconsistent [DISPUTED, in notes].

## THE PROCESS — 20 phases (repo is 00–19; owner calls it the 19-step process)
00 Setup & Tooling — environment ready, nothing works until this does
01 Math Foundations — intuition behind every algorithm, through code
02 ML Fundamentals — classical ML is still production's backbone
03 Deep Learning Core — no frameworks until you build one
04 Computer Vision — pixels to understanding
05 NLP — language is the interface
06 Speech & Audio — hear, understand, speak
07 Transformers Deep Dive — every layer understood
08 Generative AI — create media
09 Reinforcement Learning — RLHF's foundation
10 LLMs from Scratch — build/train/understand an LLM
11 LLM Engineering — LLMs in production apps
12 Multimodal AI — see/hear/read across modalities
13 Tools & Protocols — interfaces between AI and the real world
14 Agent Engineering — agents from first principles
15 Autonomous Systems — agents running without humans, safely
16 Multi-Agent & Swarms — coordination and emergence
17 Infrastructure & Production — ship AI: scale, monitor, optimize
18 Ethics, Safety & Alignment — "not optional"
19 Capstone Projects — prove it with portfolio-grade systems

## TOP MECHANISM PER PHASE (distilled from the 10 team reports)
- 00: route-aware preflight (required vs optional checks, exact `Next:` fix per failure)
- 01: log-space probability + log-sum-exp; base-rate discipline (rare-event posterior ≈1%)
- 02: point-in-time join / training-serving skew (0.948 fake vs 0.893 honest); PSI drift bands
- 03: (in part B) training mechanics; RoIAlign belongs to 04
- 04: RoIAlign (float boxes, no quantisation — DPI-safe crops); CMER hallucination gate
  (text conf >0.8 AND CLIP cosine <0.25 ⇒ do-not-click, re-observe); ByteTrack 2nd-pass assoc.
- 05: hybrid retrieval BM25→dense→RRF(k=60)→cross-encoder; constrained decoding (mask
  invalid logits ⇒ schema-valid by construction); Hindi token-fertility tax ≈3–5×
- 06: **Silero VAD** (1M params, MIT, CPU ~1 ms/30 ms chunk) — energy-only VAD "never ship
  alone"; ~400 ms full-loop budget (20/10/150/100/100/20); Kyutai flush trick (~125 ms)
- 07: KV-cache formula priced for Qwen2.5-7B on the 2060 (≈56 KB/token; total ≈5.35 GB ✓
  matches our Modelfile); speculative decoding accept min(1,q/p), α highest on tool JSON
- 08: LoRA W′=W+α·BA (r 4–16, B zero-init); CFG s≈3–7; ControlNet zero-conv safe bolt-on
- 09: **DPO on golden pairs** (corrections ARE chosen/rejected; β-KL = persona-drift guard,
  KL creep >5–10 = alarm); GRPO verifier rewards = our postcondition checks r∈{0,1}
- 10: quantization PPL-delta bands (<0.5 excellent, >2 broken); config-first model vetting
  (param/KV calculator reproduces published counts — vet BEFORE downloading GBs)
- 11: RAG upgrade spec — RRF hybrid, parent-child chunking (index 128-tok children, return
  512-tok parents), HyDE (Hinglish query → English studies); **byte-stable system prompt**
  (prefix cache: one differing token kills everything after it — validates our
  persona/protocol-never-cut rule); self-consistency voting N=5 @0.7 for high-stakes turns
- 12: visual-token budgeting by content type (UI 729–1024); fixed-budget visual front-ends
  (Q-Former/Perceiver) for the screen watcher; Thinker-Talker latency accounting
- 13: idempotency = business key + argument fingerprint, committed atomically WITH the
  effect; descriptor hash-pinning + drift quarantine; five-layer trust model
  (exposure ≠ permission ≠ approval ≠ sandbox ≠ verification)
- 14: **standing eval suite from real failures** (our 1,862 tests test code; nothing tests
  behaviour); feedback ratchet (corrections → earliest layer: test/policy/automation/
  example/decision-record); ReWOO plan-DAG mode; per-tool circuit breakers; calibrated
  separate reviewer (≥80% agreement on a calibration set before shipping)
- 15: bounded self-improvement's four gates; **evaluator firewall** (DGM deleted its own
  hallucination markers — the evaluator must live outside the edit surface); spend-velocity
  trip (>$X/10 min ⇒ cut); **memory canary entries** (planted sentinel facts = exfil alarm);
  propose-then-commit + verify-by-re-read (idempotency key = hash(thread + action signature))
- 16: supervisor pattern (lead never reads raw material; **surface synthesis conflicts,
  never silently resolve**); memory poisoning defences (provenance on every write,
  append-only supersession, unwritable verifier); MAST taxonomy — Specification 41.77% /
  Coordination 36.94% / Verification 21.30% (our live planner mismatch = Specification,
  the biggest bucket); OG-Narrator split (code computes, LLM only narrates)
- 17: goodput SLOs (never averages); prefix-cache prompt discipline; cascade routing with
  drift gates; DCL design independently confirmed (+ egress allowlists, ≤90-day rotation)
- 18: **Information Flow Control** — label content trusted/untrusted; untrusted content may
  inform but never originate actions; EchoLeak three-boundary rule; adaptive-attack
  humility (>90% of published IPI defences fell to adaptive attacks); Goodhart law (why
  RLHF brains are sycophantic by construction); the event log is the audit record, never
  the model's self-report
- 19: Capstone 03 = "build JARVIS's voice loop" (~400 ms turn budget; turn-commit =
  completion >0.6 AND 500 ms silence; filler for tools >300 ms); harness chain 20–29
  (loop state machine, JSON-pointer schema errors, **ObservationLedger** — the bookkeeping
  whose absence caused "JARVIS goes silent"); capstone 13 approval = record bound to
  actor + tool + canonical-JSON arg digest + expiry; **over-refusal eval** (capstone 84 —
  our bland-deflection failure is an unmeasured over-refusal problem); during-generation
  streaming checkpoint (87) — a layer JARVIS lacks entirely; SFT-39/DPO-40 = the golden-pair
  training recipe (response-only masked loss → DPO on triples)

## THE JARVIS ADOPT QUEUE (from this study; build only on owner orders)
1. Standing behaviour eval suite built from real failures (P14-L30) — highest value.
2. Idempotency ledger for phone/email/file side effects (P13-L29 + P14-L34 + P15-L15/16
   converge) — upgrades "unknown outcome" from a report to a recoverable fact.
3. Silero VAD tier in the voice loop (P06-L14); written voice SLOs from the 400 ms budget.
4. Knowledge-skill RAG upgrade: RRF hybrid + parent-child chunking + HyDE (P11-L07, P05-L14).
5. DPO fine-tune path on golden pairs, LoRA on the local 7B (P09 + P10-L08 + P19-39/40).
6. Byte-stable prompt audit across turns (P11-L15/P17-06) — mostly already true; verify.
7. CMER-style do-not-click gate in the verifier (P04-L25) + RoIAlign-style float crops.
8. Memory canary entries + provenance/append-only supersession hardening (P15-L14, P16-L13).
9. Spend-velocity trip in the supervisor (P15-L13).
10. Over-refusal measurement on the fallback path (P19-84) — names our live failure.
11. Feedback ratchet replacing prose-only TEACHING.md accumulation (P14-L46/L54).
12. IFC content labels on tool outputs (P18-L15) — untrusted content never originates actions.

## GUARDRAILS — what in this repo must NOT be built as taught [NEGATIVE]
- **The MCP "2026-07-28" stateless profile is NOT the real spec** (P13 lessons 06–18/23/
  28–31 + capstone 13 + cheatsheets): no `initialize`, mandatory `server/discover`,
  `resultType`/`ttlMs`, error codes −32020/21/22 — none of it exists in the released MCP
  spec (2025-11-25: initialize handshake, `Mcp-Session-Id`, −32002). Steal the security
  PATTERNS (hash-pinning, action-bound approvals, idempotency); never the wire facts —
  building them would break interop with every real MCP server. Our bridge stays as-is.
- **Pervasive invented "2026 frontier" claims** — Claude Opus 4.7 (incl. an impossible
  MMAU audio score; Claude has no audio input), Gemini 3.x, "Spark-X2.5 4B", Llama Guard 4
  specifics, RSP v3.0 details, several arXiv 2602/2603/2604 IDs: recorded UNVERIFIED in the
  notes, never to be cited as fact.
- Repo errors caught by the teams (100+ [DISPUTED] items total across the notes): Kokoro
  "English-only" is WRONG (v1.0 ships Hindi — JARVIS uses it); Whisper mel count is 128 for
  Large-v3/Turbo (not flat 80); ScreenSpot-Pro conflated with ScreenSpot (~43% real vs ~85%
  claimed); Janus-Pro MMMU invented; several arithmetic errors in the repo's own worked
  examples (softmax summing to 0.90; param counts off ~4×; 8B full-FT memory ~56 GB vs the
  real ~128 GB AdamW figure); MusicGen weights are CC-BY-NC (not MIT).
- Process note: two tool outputs during the study carried embedded fake "system-hint"
  persona-reassignment text (prompt injection in data). Ignored; task unchanged. (The repo's
  own P18 lessons describe exactly this attack class.)
