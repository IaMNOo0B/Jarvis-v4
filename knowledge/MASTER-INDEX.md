# JARVIS Knowledge — Master Index

*Owais's order: "I don't want any ruckus in JARVIS, everything needs to be organized."*
*Organized by v2 subsystem (the thing being built), not by research mission. Every entry: one line, file link, confidence.*

---

## 🧠 BRAIN — local inference, fallback, routing, hardware

| # | What | File | Confidence |
|---|---|---|---|
| B1 | **Fallback-chain recipe** — retryable = transport errors only, hard timeouts, per-provider circuit breakers; the direct C-B1 fix, verified against v1's `brain.py` | [scout-phd-fallback-chain.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-fallback-chain.md) | 95% |
| B2 | **ik_llama.cpp** — run `llama-server` directly, NOT Ollama (Ollama pins a lagging fork); IQ quants + KV-cache quantization | [scout-phd-ik-llama-cpp.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-ik-llama-cpp.md) | 90% |
| B3 | **9B daily driver spec** — Qwen3.5-9B Q4_K_M ≈ 7.8 GB on Windows, 32K context cap, 40–60 tok/s on RTX 3060; hybrid attention makes KV cache 4× cheaper than naive math | [scout-phd-hardware-9b.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-hardware-9b.md) | 92% |
| B4 | **LiteLLM router pattern** — v2's N-brain is a LiteLLM-style router; wrap, don't reimplement (goldmine #6) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 95% |
| B5 | **OpenAI-compatible interface decision** — the control plane outlives any single brain; model swaps = config changes (goldmine #9) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 90% |
| B6 | **Hands-on: ReAct agent** — mock-LLM tool loop with provable termination, token budgets (17/17 tests) | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 4) | proven |
| B7 | **Inference frontiers** — count KV heads not params; never ship q4_0 for the tool brain (63%→20% BFCL); Q4_K_M floor + custom imatrix; 4k-physical strategy = 14× fewer attention FLOPs | [learn-inference-frontiers.md](sandbox://workspace/jarvis-knowledge/learn-inference-frontiers.md) | 85% |
| B8 | **Frontier architectures** — Mamba SKIP (retrieval breaks where agents live), hybrid WATCH, sparse SKIP, RETRO SKIP, RAG ADOPT, diffusion SKIP (no streaming); transformer stays | [learn-frontier-architectures.md](sandbox://workspace/jarvis-knowledge/learn-frontier-architectures.md) | 85% |
| B9 | **Math for JARVIS** — info theory KNOW COLD (KL = the training loss); Bayesian belief ledger (Beta per tool); "information has no value unless it changes a decision"; Brier calibration; training-time math skipped | [learn-math-for-jarvis.md](sandbox://workspace/jarvis-knowledge/learn-math-for-jarvis.md) | 90% |
| B10 | **Continual learning** — never fine-tune a fact; adapter-per-skill (O-LoRA) + retrieval routing; reliability is multiplicative (0.95²⁰ ≈ 0.36) | [learn-continual-learning.md](sandbox://workspace/jarvis-knowledge/learn-continual-learning.md) | 85% |

## 🎙️ VOICE — pipeline, wake word, VAD, TTS, barge-in (plugin phase, designed now)

| # | What | File | Confidence |
|---|---|---|---|
| V1 | **voiceloop** — novel-word barge-in + echo-match filter (fuzzy Levenshtein vs audible prefix), serialized turns; repo cloned, Python pseudocode included; MIT | [scout-phd-voiceloop.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-voiceloop.md) | 88% |
| V2 | **Chatterbox TTS — SHIP** — MIT code+weights, beats ElevenLabs 65/25 blind, Hindi first-class; new ladder Piper→Kokoro→Chatterbox→cloud; Bodhi signup deleted | [scout-phd-chatterbox.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-chatterbox.md) | 85% |
| V3 | **Smart Turn v3.2** — semantic end-of-turn (Whisper Tiny + linear head, 8M params, ~10ms CPU, 23 langs incl. Hindi/Marathi); BSD-2, verified from README | [scout-phd-semantic-vad.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-semantic-vad.md) | 88–90% |
| V4 | **livekit-wakeword** — "hello jarvis" needs NO voice clips (synthetic TTS data → ONNX); threshold 0.68 verified; Apache-2.0 | [scout-phd-wakeword.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-wakeword.md) | 90% |
| V5 | **Moshi Inner Monologue** — brain emits text trace before/while speaking; the Utterance Record pattern (zero new dependencies) | [moshi-deep-dive.md](sandbox://workspace/github-archaeology/phd-studies/moshi-deep-dive.md) | 95% |
| V6 | **livekit/agents pipeline stages** — declared stage graph (stt_node, llm_node); steal stage boundaries, not WebRTC transport (goldmine #1) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 90% |
| V7 | **whisper.cpp staged wakeups** — VAD→wake→STT, cheap stages reject cheaply; audio in memory, never temp files (goldmine #8) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 85% |
| V8 | **Voice stage graph** — typed frames, transport/business-logic separation; false-interruption resume state machine | [scout-phd-semantic-vad.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-semantic-vad.md) | 90% |

## 🧠 MEMORY — evolving, consolidation, verification

| # | What | File | Confidence |
|---|---|---|---|
| M1 | **A-MEM evolution** — new memories rewrite linked neighbors' descriptions (2× multi-hop gains); write-side evolution pass with quarantine for untrusted sources; MIT | [diamond-3-a-mem.md](sandbox://workspace/github-archaeology/phd-studies/diamond-3-a-mem.md) | 85% |
| M2 | **Nocturne sleep consolidation** — Dedupe→Merge→Promote→Forget passes (LLM proposes, code applies) + wake-quiz with forgotten probe; IDEAS ONLY (license unverified) | [scout-phd-nocturne.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-nocturne.md) | 88% |
| M3 | **mem0 two-phase write gate** — ADD/UPDATE/DELETE/NOOP decided before writing; kills memory rot (goldmine #3) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 90% |
| M4 | **Letta OS-tier memory** — core/recall/archival; agent self-edits its own memory (goldmine #7) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 75% |
| M5 | **Memory composition for v2** — mem0 gate at write → A-MEM evolution after → Nocturne verification during idle; untrusted memories quarantined from evolution (poisoning amplification) | [scout-phd-nocturne.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-nocturne.md) | 85% |

## 🧩 SKILLS — plugin system, supply chain, discovery

| # | What | File | Confidence |
|---|---|---|---|
| S1 | **ClawHavoc** — 1,184 malicious skills (Jan–Feb 2026); 4 mandatory defenses: quarantine+scan every version, fail-closed installPolicy, secrets in host process, agent-written skills staged for review | [scout-phd-clawhavoc.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-clawhavoc.md) | 95% |
| S2 | **Agent Plugins 1.0** — build v2's skill format TO the standard (SKILL.md = content, Plugins = packaging, MCP = protocol); spec has NO signing — v2 builds trust on top | [scout-phd-agent-plugins.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-agent-plugins.md) | 90% |
| S3 | **Hands-on: plugin host** — manifest-based discovery via importlib (never exec) + `audit_wiring()` catching "created but never wired" (38/38 tests) | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 1) | proven |
| S4 | **Hands-on: event bus** — typed events + JSONL replay; replay must reduce, never re-emit (21/21 tests) | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 2) | proven |

## 🔒 SECURITY — injection defense, localhost, sandboxing, permissions

| # | What | File | Confidence |
|---|---|---|---|
| SEC1 | **Anthropic managed agents** — structural injection fix (credentials never in the sandbox) + crash recovery via external session log (`wake(sessionId)`); candidate R15 | [diamond-1-managed-agents.md](sandbox://workspace/github-archaeology/phd-studies/diamond-1-managed-agents.md) | 95%/90% |
| SEC2 | **CVE-2026-65105 localhost hijack** — DNS rebinding → unauthenticated Ollama API → chat-template poisoning; 6 v2 requirements VR-S1…S6 (bind 127.0.0.1, auth on loopback, template integrity, wire audit) | [scout-phd-localhost-hijack.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-localhost-hijack.md) | 95% |
| SEC3 | **ScrambleID / RFC 8693** — scope-per-tool tokens; vendor is proprietary, patterns are open IETF standards — implement ourselves at zero license cost | [scout-phd-scrambleid.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-scrambleid.md) | 85% |
| SEC4 | **REA untrusted-content markers** — every external byte marked untrusted by default; evidence model (observed/derived/inferred); MIT | [rea-deep-dive.md](sandbox://workspace/github-archaeology/phd-studies/rea-deep-dive.md) | 90% |
| SEC5 | **Deno deny-by-default permissions** — the shipped permission UX v2's two-dial safety copies (goldmine #2) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 95% |
| SEC6 | **E2B microVM sandboxing** — kernel-level isolation ladder for code execution (goldmine #4) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 85% |
| SEC7 | **Three-layer injection defense** — Anthropic separation (where credentials live) + REA markers (how data is framed) + ScrambleID tokens (what the agent can do when tricked); table of what breaks if each is missing | [scout-phd-scrambleid.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-scrambleid.md) | 85% |
| SEC8 | **Hands-on: hardened tool runner** — framing, fs jail, subprocess sandbox (shell=False, empty env, timeout), secret redaction; 13/13 attack tests blocked | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 5) | proven |
| SEC9 | **Privacy engineering** — 7-stage data lifecycle (file-level mapping); DELETE FROM isn't enough (freelist/FTS5/WAL → 13-item deletion checklist); DP not needed (single-user local); consent GRANT → REVOKED → PURGED | [learn-privacy-engineering.md](sandbox://workspace/jarvis-knowledge/learn-privacy-engineering.md) | 85% |
| SEC10 | **Formal methods** — beartype + deal contracts USE (5–10 security-critical functions); Hypothesis stateful testing USE now; TLA+ LATER; chaos testing USE | [learn-formal-methods.md](sandbox://workspace/jarvis-knowledge/learn-formal-methods.md) | 85% |
| SEC11 | **Unified threat model** (§18 artifact #9) — 8 ranked assets, 7 trust boundaries, 6 adversary profiles, 36-attack catalog, attacks×mitigations coverage matrix (7 uncovered cells, 9 partial), 8 accepted residual risks | [learn-threat-model.md](sandbox://workspace/jarvis-knowledge/learn-threat-model.md) | 85% |
| SEC12 | **App→Cloud data classification (DCL)** — closes threat-model cells C6 (clipboard→cloud) and C7 (full history→cloud): 4-class taxonomy (public/personal/sensitive/secret), 3-tier detection (keyring registry + patterns + PII assist), 4 enforcement points, per-segment policy (withhold/confirm/send), taint propagation, "what left the machine" feed | [learn-data-classification.md](sandbox://workspace/jarvis-knowledge/learn-data-classification.md) | 80% |

## 🖥️ UI/UX — chat UI, error UX, Hinglish, persona, product strategy

| # | What | File | Confidence |
|---|---|---|---|
| U1 | **Chat UI spec** — 10 pre-built components (model fills props only); script-five-conversations-first as hard requirement; fuses competence-shock + error UX + Four Checks + evidence model | [scout-phd-chat-ui.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-chat-ui.md) | 88% |
| U2 | **Error UX doctrine** — Error + suggestion + action, never bare; 10 concrete v2 messages (GUI buttons, zero terminal, calm-butler tone); feature tiers Critical/Important/Optional | [scout-phd-error-ux.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-error-ux.md) | 90% |
| U3 | **Hinglish language spec** — never transliterate; system-prompt recipe; mirroring; 99% phonetic-jailbreak ASR confirmed → multilingual safety alignment is mandatory (R2/R13) | [scout-phd-hinglish.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-hinglish.md) | 88% |
| U4 | **Competence shock + error recovery grace** — v2's product strategy (OpenAI product lead source); chatbot-first IS the floor-raising move; script the first 3 interactions | [scout-phd-competence-shock.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-competence-shock.md) | 90% |
| U5 | **GUI verification doctrine** — judge by the artifact, never the clicks (0.80¹⁵ ≈ 3.5%); Teach & Repeat as zero-terminal PC control; "shall I do it in 3 steps?" | [scout-phd-gui-verification.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-gui-verification.md) | 90% |
| U6 | **Dodaios** — most JARVIS-shaped product in existence; steal Ack-Brain, risk tiers, supervisor pattern; warnings: 238★ single-maintainer, process supervision before voice | [dodaios-deep-dive.md](sandbox://workspace/github-archaeology/phd-studies/dodaios-deep-dive.md) | 90% |
| U7 | **Hands-on: corruption-proof settings** — atomic writes, never silently coerce, merge-don't-replace (39/39 tests; kills C-B4 class) | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 6) | proven |
| U8 | **React streaming chat UI engineering** — batch tokens (rAF), memoize rows, virtualize long histories, STOP path always wired, DOMPurify on model HTML; Vercel AI SDK's value = streaming parser + abort (~100 lines to hand-roll) | [learn-frontend.md](sandbox://workspace/jarvis-knowledge/learn-frontend.md) | 85% |
| U9 | **Multimodal / visual grounding** — VLMs can't point reliably (GPT-4o: 0.9% on ScreenSpot-Pro); screenshots = verification + fallback sensor, never primary control; UIA-tree-first stays | [learn-multimodal.md](sandbox://workspace/jarvis-knowledge/learn-multimodal.md) | 85–90% |

## 🏗️ INFRASTRUCTURE — scheduler, config, updates, evals, proactive behavior

| # | What | File | Confidence |
|---|---|---|---|
| I1 | **Scheduler spec S-0…S-4** — write-before-reason, "no successful runs" alert, 3-strikes→human, zero-token chores; malformed time strings never crash (R7) | [scout-phd-scheduler.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-scheduler.md) | 90% |
| I2 | **DeepEval + persona simulation** — "evals before users"; every v1 critical bug mapped to the eval layer that would have caught it; Hinglish + adversarial personas | [scout-phd-deepeval.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-deepeval.md) | 88% |
| I3 | **Four Checks** — `may_act_unasked()` gating (ACT/SUGGEST/HOLD/MONITOR/BATCH/DROP); gate 1 = data check never self-confidence; gate 4 = permission enforced in code | [scout-phd-four-checks.md](sandbox://workspace/github-archaeology/phd-studies/scout-phd-four-checks.md) | 92% |
| I4 | **Hands-on: architecture patterns** — event bus, safety state machine, backpressure stage graph, circuit breaker (21/21 tests) | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 2) | proven |
| I5 | **Hands-on: testing that proves** — bug-demos as xfail(strict); every except block tested; fault injection (832 pass / 6 bug-demos fail on buggy code) | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) (§ Track 3) | proven |
| I6 | **Advanced CS** — event sourcing LITE (SQLite WAL + snapshots, no server), durable local queue (SQLite, not Kafka), selective caching, fault domains; multi-tenancy explicitly refused | [learn-advanced-cs.md](sandbox://workspace/jarvis-knowledge/learn-advanced-cs.md) | 85% |
| I7 | **Evaluation science** — 18-metric scorecard, 7 release gates, Wilson intervals, Holm step-down; ship needs Holm p<0.05 AND Δ ≥ pre-registered minimum; baselines marked UNTESTED | [learn-evaluation-science.md](sandbox://workspace/jarvis-knowledge/learn-evaluation-science.md) | 90% |
| I8 | **Open problems map** — 50 JARVIS unknowns + 18 field problems ranked (importance × uncertainty × resolvability); the 2060 is the laboratory; top-10 = the measurement backlog | [learn-open-problems.md](sandbox://workspace/jarvis-knowledge/learn-open-problems.md) | ranked map |
| I9 | **Observability** — OTel data model, not the infrastructure: trace_id via contextvars, JSONL span exporter, ~6 instrumented boundaries | [learn-observability.md](sandbox://workspace/jarvis-knowledge/learn-observability.md) | 85% |
| I10 | **Backend control plane** — FastAPI on 127.0.0.1 + auth anyway (DNS rebinding), Pydantic v2 strict mode, lifespan startup/shutdown, uvicorn/httpx; BackgroundTasks ≠ durable queue | [learn-backend.md](sandbox://workspace/jarvis-knowledge/learn-backend.md) | 90% |
| I11 | **Databases** — SQLite WAL verdict for v2 (zero-ops); sessions/memories/chunks schema with PRAGMA user_version migrations; revisit PostgreSQL at >1M chunks | [learn-databases.md](sandbox://workspace/jarvis-knowledge/learn-databases.md) | 90% |

## 📚 FOUNDATIONS — the ground everything stands on

| # | What | File | Confidence |
|---|---|---|---|
| F1 | **Failure analysis** — 210 findings (24 critical, ~55 high, ~130 medium) → 14 architecture requirements R1–R14 (+ candidate R15: external session store) | [FAILURE-ANALYSIS.md](sandbox://workspace/jarvis-build/FAILURE-ANALYSIS.md) | verified |
| F2 | **Decision playbook** — voice/safety/architecture/memory/product/license decisions with confidence + calibration log | [DECISION-PLAYBOOK.md](sandbox://workspace/jarvis-knowledge/DECISION-PLAYBOOK.md) | living doc |
| F3 | **363 repo PhD studies** — 75 repos (2024–2026) + 288 repos (2016–2024, useful-only); top lessons with file refs | [MASTER-SUMMARY.md](sandbox://workspace/github-archaeology/phd-studies/MASTER-SUMMARY.md) · [MASTER-SUMMARY-2016-2024.md](sandbox://workspace/github-archaeology/phd-studies-2016-2024/MASTER-SUMMARY-2016-2024.md) | studies |
| F4 | **10 goldmines** — calibrated EV ranking (livekit, deno, mem0, e2b, UFO, litellm, letta, whisper.cpp, ollama, open-interpreter) | [goldmines-top10.md](sandbox://workspace/github-archaeology/phd-studies/goldmines-top10.md) | 75–95% |
| F5 | **4 diamonds + PhDs** — Anthropic managed agents, Moshi, A-MEM, Dodaios | [diamond-1-managed-agents.md](sandbox://workspace/github-archaeology/phd-studies/diamond-1-managed-agents.md) · [moshi-deep-dive.md](sandbox://workspace/github-archaeology/phd-studies/moshi-deep-dive.md) · [diamond-3-a-mem.md](sandbox://workspace/github-archaeology/phd-studies/diamond-3-a-mem.md) · [dodaios-deep-dive.md](sandbox://workspace/github-archaeology/phd-studies/dodaios-deep-dive.md) | 80–95% |
| F6 | **21 scout PhDs** — the full set, by tier (Tier 0 architecture → Tier 4 product) | [scout-final-report.md](sandbox://workspace/jarvis-knowledge/scout-final-report.md) · [scout-log.md](sandbox://workspace/jarvis-knowledge/scout-log.md) (677 lines) | 85–95% |
| F7 | **Coding sprint** — 180 tests passing across 7 tracks; 6 files ready to seed v2 | [MASTER-REPORT.md](sandbox://workspace/learning/MASTER-REPORT.md) | proven |
| F8 | **REA deep dive** — Reverse Engineer Anything; evidence model + untrusted markers; MIT | [rea-deep-dive.md](sandbox://workspace/github-archaeology/phd-studies/rea-deep-dive.md) | 90% |
| F9 | **Frontier reasoning** — 8-strategy decision table (single-pass ReAct → MCTS); verifier-first (build verifiers before search); belief ledger + causal post-mortem default-on at ~1× cost | [learn-frontier-reasoning.md](sandbox://workspace/jarvis-knowledge/learn-frontier-reasoning.md) | 85% |
| F10 | **Research frontiers** — external ground truth beats introspection; self-reflection limits (the same weights that made the error can't detect it); world models lose to the real environment | [learn-research-frontiers.md](sandbox://workspace/jarvis-knowledge/learn-research-frontiers.md) | 85% |
| F11 | **Coverage ledgers** — Rule Zero audits: Phase 2 (9 tracks skipped, 5 gaps researched) + OMEGA (5 gaps researched, rest skipped at L3–L4) + Lifetime directive (7-status taxonomy audit, 3 gaps dispatched) + Infinity (§20 seven-item audit, Q1–Q7 research cycles) | [learn-phase2-ledger.md](sandbox://workspace/jarvis-knowledge/learn-phase2-ledger.md) · [learn-omega-ledger.md](sandbox://workspace/jarvis-knowledge/learn-omega-ledger.md) · [learn-lifetime-ledger.md](sandbox://workspace/jarvis-knowledge/learn-lifetime-ledger.md) · [learn-infinity-ledger.md](sandbox://workspace/jarvis-knowledge/learn-infinity-ledger.md) | audit |
| F12 | **Infinity research cycles** — Q1 underspecification (ask/act/escalate rule + 30-task golden set) · Q2 SMT verification (Z3: adopt C2 in CI only) · Q3 Bayesian design (SKIP BoED at JARVIS scale) · Q4 fault trees (8 single points of failure on the approval gate) · attack trees for adversarial branches (4 undefended findings, P0 = payload-description laundering) · Q5 causal discovery (SKIP full discovery; run counterfactuals instead) · Q6 bounded rationality (8 rules audited, 0 overturned, 4 proposals) · assistance games / CIRL (belief-over-θ as decision state, off-switch decay warning) · Q7 discriminator protocol (DISC-100: gate tree search on Wilson lower bound ≥90%) · Q27 data classification (DCL: closes C6/C7 — 4-class taxonomy, registry+pattern detection, 4 enforcement points) | [learn-underspecification.md](sandbox://workspace/jarvis-knowledge/learn-underspecification.md) · [learn-smt-verification.md](sandbox://workspace/jarvis-knowledge/learn-smt-verification.md) · [learn-bayesian-design.md](sandbox://workspace/jarvis-knowledge/learn-bayesian-design.md) · [learn-fault-trees.md](sandbox://workspace/jarvis-knowledge/learn-fault-trees.md) · [learn-attack-trees.md](sandbox://workspace/jarvis-knowledge/learn-attack-trees.md) · [learn-causal-discovery.md](sandbox://workspace/jarvis-knowledge/learn-causal-discovery.md) · [learn-bounded-rationality.md](sandbox://workspace/jarvis-knowledge/learn-bounded-rationality.md) · [learn-assistance-games.md](sandbox://workspace/jarvis-knowledge/learn-assistance-games.md) · [learn-asking-economics.md](sandbox://workspace/jarvis-knowledge/learn-asking-economics.md) · [learn-discriminator-protocol.md](sandbox://workspace/jarvis-knowledge/learn-discriminator-protocol.md) · [learn-data-classification.md](sandbox://workspace/jarvis-knowledge/learn-data-classification.md) | 80–90% |

---

## License posture (applies to all reuse)

- **Borrow code + ideas:** MIT, Apache-2.0, BSD (2/3-clause), CC-BY-4.0 (spec text)
- **Ideas ONLY, never code:** AGPL-3.0, GPL-3.0, CC-BY-NC (incl. platonkuch-dev/jarvis-ai, openbin.ai), proprietary (ScrambleID vendor — but its RFC patterns are open)
- **Unverified — treat as ideas-only until checked:** Nocturne (no license stated)
- **Always:** verify the `license:` field on the exact pinned checkpoint before vendoring; per-file headers before copying

## The v2 thesis in three lines (from everything above)

1. **Declaration beats orchestration code** — declare pipelines, stages, capabilities, permissions up front; let a dumb reliable runtime execute.
2. **Push intelligence to the boundaries** — session outside the harness, text trace alongside audio, evolution at write time, verification at read time.
3. **Dependable beats impressive** — raise the floor (reliability), not the ceiling (demos); chatbot-first, voice-last; every failure ships error + suggestion + action.

*Index built 2026-10-19; updated 15:15 IST — master-learning (6) + Phase 2 (6) + OMEGA (6) files indexed. Owais: no ruckus — everything in its place.* 🫡
