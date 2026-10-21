# JARVIS Decision Playbook — Organized Knowledge Network

Built 2026-10-19 from: 363 PhD repo studies, full v1 failure analysis (210 findings), 2026 upgrade roadmap, seed-to-tree studies.
Purpose: recall the right lesson at implementation time, with honest confidence. Consult before any JARVIS v2 build decision.

---

## 1. VOICE DECISIONS

**V1. Think in full loops, never components.** [confidence: HIGH — v1 autopsy C-V1..C-V4]
The echo bug: TTS fixed in isolation, nobody asked "what does the mic hear next?" Before ANY voice change, trace mic→VAD→STT→brain→TTS→speakers→mic. If you can't draw the loop, don't touch the code.

**V2. Voice = declared stage graph, not hand-wired threads.** [HIGH — MediaPipe, livekit/agents, 4 confirmations]
Stages: mic → VAD → STT → brain → TTS → speakers, declared in config with timestamped streams, back-pressure, cancellation. Barge-in and interruption then work by construction.

**V3. Barge-in on novel WORDS, never raw energy.** [HIGH — voiceloop study, v1 C-V2]
≥70% word-match vs spoken reply = echo (ignore). Genuinely new words accumulating = user interrupting (stop TTS). Energy-based barge-in false-triggers on own voice.

**V4. Record what was SPOKEN, not what was generated.** [HIGH — Pipecat pattern, v1]
If TTS interrupted at word 40 of 100, history keeps 40 words. Brain must never "remember" saying unheard words.

**V5. Staged wakeups: cheap-first.** [HIGH — whisper.cpp, UIA-tree-first, 3+ confirmations]
VAD gates wake-word gates STT. Never run STT continuously. Audio in memory buffers, never temp files.

**V6. TTS chain for v2:** Kokoro-82M (offline default, Apache-2.0, Hindi+British voices) → Bodhi TTS (neural Hinglish tier, needs Owais signup) → Piper (failsafe). [MEDIUM-HIGH — 2026 study; Bodhi untested by us]
Skip: Cohere STT (no Hindi — fatal), Qwen3-TTS primary (no Hindi).

**V7. Wake word:** livekit-wakeword for "hello jarvis" (drop-in ONNX, no voice clips needed, per-word threshold ~0.68). [MEDIUM — claim "100x fewer false positives" unverified, test on our machine]

**V8. Brain for v2:** Qwen3.5-9B local (best small tool-caller), cloud fallback chain that ACTUALLY falls back (v1 C-B1: transport exceptions must trigger fallback, not just HTTP errors). [HIGH]

## 2. SAFETY DECISIONS (non-negotiable, in this order)

**S1. Instruction hierarchy everywhere.** [CRITICAL — v1 C-S1, C-S5, C-S6]
Tool output = UNTRUSTED DATA, always framed as such, never raw-concatenated. No exceptions. Prompt injection = RCE in v1.

**S2. Deny-by-default capabilities.** [HIGH — Deno ×3 confirmations]
Every skill declares capabilities (fs/network/shell/mic/UI). Grant per task. This is what makes "full PC control" shippable.

**S3. Two-dial safety.** [HIGH — codex, superpowers, OpenMontage]
Auto-allow reads. Confirm writes (real out-of-band gate, never LLM-mediated). Sandbox code execution. Plan mode: state plan, take "yes" before multi-step.

**S4. Ship undo for everything.** [HIGH — cline, bolt.diy, gemini-cli ×3]
Snapshot before every mutation. "I can undo anything" = biggest trust unlock.

**S5. Filesystem jail enforced in CODE with tests.** [CRITICAL — v1 C-S3: workspace_only_writes enforced nowhere]
**S6. Secrets in OS keyring, never env/chat/files.** [HIGH — openhuman ×3, v1 C-X]
**S7. Skill installs quarantined + scanned.** [HIGH — 820+ malicious ClawHub skills]
**S8. No LLM-triggerable updates/installs.** [CRITICAL — v1 C-X2, C-X3: unsigned git pull + unverified .exe]

## 3. ARCHITECTURE DECISIONS

**A0. Adapters encode procedures, never facts.** [HIGH — continual-learning position (g), audit C6] LoRA adapters may encode *how* (tool-calling skills, procedures); weights must never encode *what* (facts). Never fine-tune a fact — facts live in retrieval.

**A1. Chatbot core first, voice as plugin never imported at startup.** [HIGH — v1 R1, Vue progressive adoption]
**A2. Plugin at every stage.** [HIGH — ×4 confirmations] STT/TTS/brain/skills swappable behind interfaces. Upgrades = config swaps.
**A3. Event stream + replayable diagnostics.** [HIGH — OpenHands, Arthas] Every action → typed event on central bus. HUD renders it, debugging replays it.
**A4. Atomic writes + backups + locking for ALL persistence.** [CRITICAL — v1 C-I2, C-B4, H-S11: crash-mid-save = total data loss]
**A5. Config validation with filenames, never crashes.** [HIGH — v1 C-B3, C-I1: one bad string = app won't start]
**A6. SKILL.md standard + progressive disclosure.** [HIGH — industry converged] Folder + frontmatter; load names only, full instructions on trigger.
**A7. Schema-first: generate tool schemas from signatures.** [MEDIUM-HIGH — Prisma, FastAPI] Generated schemas can't drift.
**A8. Content-addressed caching.** [MEDIUM-HIGH — Turborepo ×2] "Yes, sir" never re-synthesizes. Design invalidation first.
**A9. SSE for streaming by default; WebSocket only if continuous two-way is needed.** [MEDIUM-HIGH — audit C2 ruling, Cycle 18] Token/progress streaming (chat UI, Tauri sidecar localhost-HTTP) = Server-Sent Events + REST for commands. WebSocket earns its complexity only for continuous bidirectional traffic (not chat). Simpler ops, matches the sidecar architecture. What would change my mind: a measured need for client→server streaming mid-turn.

## 4. MEMORY DECISIONS

**M1. Bounded two-store + auto-capture + decay + GUI viewer.** [HIGH — hermes, mem0, openhuman]
Hard char caps, frozen per-session snapshots, 12 auto-capture hooks, 4-tier decay lifecycle, "What JARVIS remembers" Settings tab.
**M2. Hermes background review loop.** [HIGH — verified in repo] After each turn, cheap local pass: "anything worth remembering or skill-ifying?" Compound learning at near-zero cost.
**M3. Timeline-first, organize later.** [MEDIUM-HIGH — memos] Append-only SQLite, sub-100ms writes. Nightly consolidation.
**M4. Memory poisoning is a critical threat.** [CRITICAL — v1 C-S6] Provenance on every saved fact, quarantine untrusted sources.

## 5. PRODUCT/UX DECISIONS

**P1. Narrate work live.** [HIGH — OpenManus, bolt] Silent stretches caused the trust rupture. HUD activity feed, one auto-retry with calm status before surfacing failure.
**P2. Opinionated defaults, kill the 31-field panel.** [HIGH — Prettier, Black] Owais said "choose for me." One good voice, one sensitivity, one style.
**P3. Zero-config installer.** [HIGH — uv, Parcel] Self-bootstrapping, deterministic, silent model download. Attacks the #1 friction.
**P4. Honest UX: every failure = actionable message.** [HIGH — v1 R12] Never "couldn't start" with no recovery. Startup preflight (VC++ redist, Python, disk, model checksums).
**P5. Instant-render, async-refresh.** [MEDIUM-HIGH — powerlevel10k] Speak acknowledgment before slow tools finish.

## 6. LICENSE DECISIONS (memorized, no exceptions)

**L1. Borrow ideas AND code:** MIT, Apache-2.0, BSD, Unlicense.
**L2. Ideas ONLY, never code:** AGPL-3.0, GPL-3.0, CC-BY-NC, fair-code, BSL, MPL-2.0 (weak copyleft — reimplement), proprietary.
Known ideas-only: skyvern, VoiceStudio, OpenMontage, openhuman, agenticSeek, co-tracker, worldmonitor-core, RSSHub, n8n, A1111, firecrawl, ChatTTS.
**L3. Code-license ≠ weight-license.** Always verify the specific model artifact (Qwen, FLUX, DeepSeek tiered).

## 7. BUILD ORDER (from failure analysis R1–R14)

1. Text chatbot core (instruction hierarchy, confirm gates, config validation, real fallback)
2. Persistence (atomic writes, backups, locking)
3. Secrets (OS keyring, log redaction)
4. Skills (manifest discovery, fs jail, sandbox, circuit breakers)
5. Updater (signed, rollback, no LLM triggers)
6. Setup (preflight, atomic downloads, zero-config)
7. Privacy (opt-in camera/mic/screen, egress disclosure)
8. UI (queued signals, real watchdog, honest errors)
9. Voice plugin LAST, echo guard wired by construction

---

## CALIBRATION LOG

Record decisions made from this playbook with: decision, confidence, what would change my mind.
(Append as v2 build proceeds.)
