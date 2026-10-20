# JARVIS BUILD FRAME — ai-engineering-from-scratch ke process se

Repo ka 20-phase "AI banane ka process" (00→19), hamare JARVIS pe frame kiya hua.
Repo seekhne ka curriculum tha; ye usi order me **banane ka frame** hai.

## FRAME KE USOOL (har step pe laagu)

1. **Pehle audit, phir build** — jo pehle se hai use apni aankhon se verify karo
   (code padho + test chalao). Jo verified hai, wo dobara nahi banta.
2. **Step = order** — ek step ke gaps band kiye baghair agla step nahi. Neeche se
   upar banta hai JARVIS, jaise repo sikhaata hai.
3. **Step complete ka saboot** — har step ke end me ek test/naap jo dikhaye ki
   step ka kaam sach me hua (Verification Rule: text-file ka daawa saboot nahi).
4. **Repo ki galtiyaan frame se bahar** — nakli MCP-2026 spec aur unverified 2026
   claims is frame me kahin istemal nahi hote (ledger Part 12 guardrails).

Status tags: [HAI-VERIFIED] pehle se hai · [GAP] banana hai · [HALKA] is step me
JARVIS ko zyada kuch nahi chahiye, sirf usool lagu hote hain.

---

## STEP 00 — Setup & Tooling
*Repo essence: environment ready, nothing works until this does.*
- JARVIS me: double-click launch (JARVIS.bat), setup, pehli-run ki taiyaari.
- [HAI] JARVIS.bat / setup.bat / first-run checks maujood hain.
- [GAP] **Route-aware preflight** (P00-L01): har check pe required vs optional +
  fail hone pe exact "Next:" fix — taaki owner ko kabhi andhere me na rakhe.
- [GAP] **Data-dir ka DR manifest** (P17 ki seekh + owner ka data-folder delete
  wala haadsa): data/ ka manifest + backup discipline, taaki keys/memory dobara
  na khoye.
- Saboot: fresh folder pe JARVIS.bat → preflight sab green ya exact fix-message.

## STEP 01 — Math Foundations
*Repo essence: har algorithm ke peeche ki samajh.*
- [HALKA] Code kam, usool zyada: ye samajh JARVIS ke andar teen jagah chalti hai —
  base-rate discipline (rare-event pe faltu nag nahi — proactivity), log-space
  probability (confidence ke hisaab), temperature/top-p = JARVIS ke output dials.
- [GAP] Proactivity ke alert thresholds ko base-rate hisaab se audit karna
  (1/10000 prevalence + 99% sahi test = phir bhi ~99% jhooti ghanti).
- Saboot: proactivity audit note — har alert type ka expected false-alarm rate.

## STEP 02 — ML Fundamentals
*Repo essence: classical ML ab bhi production ki reedh hai.*
- [GAP] **Drift naap (PSI)** golden pairs aur persona pe: waqt ke saath JARVIS ki
  seekh bigad to nahi rahi — decile bins, bands 0.1/0.25.
- [GAP] **Eval ka kanoon**: test set sirf EK baar chhuo; do versions ki tulna
  paired test se (|t| > 2.78) — aage Step 14 ki eval suite ki neev yahi hai.
- [GAP] Training-serving skew ki seekh: jo feature train me hai wahi serve me —
  JARVIS ke liye: jo prompt test me chala, wahi production me (byte-stable, Step 11).
- Saboot: drift report + eval-kanoon ka ek chalta hua harness skeleton.

## STEP 03 — Deep Learning Core
*Repo essence: framework tab, jab khud ek bana liya ho.*
- [HALKA] Ye step team ki samajh ka hai: Whisper/ONNX/Kokoro andar se kaise kaam
  karte hain (autodiff, training loop, losses) — taaki model ki galti pe andha
  bharosa na ho.
- [GAP] Quantization ki PPL-delta paimaish (Step 10 ke vetting tool ki neev):
  koi bhi quant "excellent <0.5 / broken >2" band se naapa jayega, feeling se nahi.
- Saboot: notes-level samajh team ke paas hai (study Part B); koi user-build nahi.

## STEP 04 — Computer Vision
*Repo essence: pixels se samajh tak.*
- [HAI] UIA-tree-first grounding + DVOC refinement + verifier gate maujood hai.
- [GAP] **RoIAlign-style float-coordinate crops** — integer-snapping 125%/150%
  DPI pe alignment kharab karta hai; crop hamesha float boxes se.
- [GAP] **CMER do-not-click gate**: text confidence >0.8 lekin image-text cosine
  <0.25 ⇒ click NAHI — dobara dekho. Verifier ke liye computable signal.
- [GAP] **ByteTrack-style tracking** screen watcher ke liye: UI elements ko
  screenshots ke paar track karo (Kalman + Hungarian + low-confidence dusri
  association pass); ID-switch count = quality naap.
- Saboot: glitchy DPI fixture pe grounding tests + CMER gate ke click-block cases.

## STEP 05 — NLP Foundations to Advanced
*Repo essence: language hi interface hai.*
- [HAI] Transliteration canonicalization + Hinglish samajh maujood hai.
- [GAP] **Constrained decoding**: tool-call JSON schema-valid BY CONSTRUCTION
  (invalid tokens mask) — local 7B ke "aksar-sahi" JSON ki jagah "hamesha-sahi".
- [GAP] **Dialogue-state ke niyam** session state pe: untouched slots kabhi reset
  nahi, negation clear karta hai, correction overwrite karta hai.
- [GAP] Hindi token-fertility tax (3–5×) ko prompt budget me ginna — abhi budget
  English hisaab se hai; Hinglish turns ka asli token kharcha naapa jayega.
- Saboot: malformed-JSON regression set pe 100% schema-valid output.

## STEP 06 — Speech & Audio
*Repo essence: suno, samjho, bolo.*
- [HAI] 8-stage voice loop, EchoGuard, learned endpointing, Oriserve STT, Kokoro
  Hindi TTS — sab maujood hai.
- [GAP] **Silero VAD tier**: hamara energy-only VAD wo tier hai jise repo kehta
  hai "kabhi akele ship mat karo". Silero (MIT, 1M params, CPU pe ~1ms) beech ki
  seedhi bane: energy gate → Silero → semantic turn detector.
- [GAP] **400ms loop budget = likhit SLO**: stages 20/10/150/100/100/20ms; pehli
  awaaz end-of-utterance ke <800ms baad; barge-in LLM ko bhi 100–150ms me roke.
- [GAP] Kyutai **flush trick**: STT look-ahead ka intezaar kiye baghair ~125ms me
  transcript flush.
- [GAP] (Optional) ECAPA speaker verification — "sirf Owais ki awaaz" wala gate.
- Saboot: owner ke PC pe voice-loop latency naap SLO ke khilaf (PC-wala kaam).

## STEP 07 — Transformers Deep Dive
*Repo essence: har layer samjhi hui.*
- [HAI-VERIFIED] KV-cache ka hisaab hamare Modelfile se match karta hai
  (Qwen2.5-7B ≈56KB/token, kul ≈5.35GB — 2060 ke 6GB me fit, study se tasdeeq).
- [GAP] **Speculative decoding ki tuning**: draft 0.5B + verifier 7B; acceptance
  α asli Hinglish traffic pe naapo; optimal N≈5–7. (Draft Modelfile pehle se
  QUEUED tha — ab uska hisaab pakka hai.)
- [GAP] num_ctx honesty: RoPE scaling ki warning — context badhana muft nahi hai;
  har num_ctx change VRAM hisaab ke saath.
- Saboot: on-card benchmark (owner ke PC pe) — tokens/sec pehle vs baad.

## STEP 08 — Generative AI
*Repo essence: media banao.*
- [HALKA] JARVIS ka media kaam halka hai (images kabhi-kabhi); is step ka asal
  tohfa **LoRA** hai — W′=W+α·BA, r 4–16, B zero-init — jo Step 09/10 me JARVIS ki
  personalization ki machine banegi.
- [GAP] Adapter ka load/unload discipline: adapter JARVIS ki shakhsiyat badle,
  buniyaad kabhi nahi; har adapter removable.
- Saboot: adapter registry test — ek adapter active, bakiyon ki haalat saaf.

## STEP 09 — Reinforcement Learning
*Repo essence: karte-karte seekhna; RLHF ki neev.*
- [GAP] **DPO on golden pairs**: owner ki corrections = chosen/rejected pairs.
  Pehle SFT (response-only masked loss), phir DPO (β 0.1–0.3) — LoRA pe, 2060 pe.
- [GAP] **Verifier rewards**: JARVIS ke postcondition checks hi r∈{0,1} rewards
  hain — tool-call sudhaar bina insaani label ke.
- [GAP] KL-drift alarm: seekh ki seema — KL creep >5–10 = persona bigadne ki
  ghanti (hamare ±0.05 relationship clamp ka hi usool).
- Saboot: eval suite (Step 14) pe DPO se pehle vs baad ke number — paired test
  (Step 02) se significant ho tab hi adapter active.

## STEP 10 — LLMs from Scratch
*Repo essence: ek LLM banao, train karo, samjho.*
- [GAP] **Model vetting calculator**: koi bhi naya brain lene se pehle config se
  hisaab — parameters, KV cache, VRAM fit (2060 6GB). Download pehle nahi,
  hisaab pehle.
- [HAI] Ye step Wave 1 me ban chuka: `jarvis/model_vet.py` — Llama-3-8B aur
  Gemma-2-9B ke published counts EXACT reproduce hue.
- [GAP] MinHash dedup golden-pair store ki safai ke liye (Step 09 ki pipeline me).
- Saboot: calculator published counts se match kare (ho chuka, verified).

## STEP 11 — LLM Engineering
*Repo essence: LLM ko production app me.*
- [HAI] v2 me: prompt-budget compaction, FallbackChain, /key system — v4 me ye
  sab frame ke hisaab se dobara, saaf tareeke se judega.
- [GAP] **RAG upgrade** (knowledge skill): RRF hybrid (BM25+vector, k=60),
  parent-child chunking (index 128-token child, return 512-token parent),
  **HyDE** — Hinglish sawaal se English farzi-document bana ke khoj (bhasha ka
  pul). [Core Wave 1 me ban chuka: `jarvis/rag.py`.]
- [GAP] **Byte-stable system prompt audit**: persona/protocol hissa har turn
  byte-identical (prefix cache) — ek badla hua token poora cache uda deta hai.
- [GAP] **Self-consistency voting** high-stakes turns pe: N=5 samples @0.7,
  majority — sirf local GPU ka waqt kharcha hota hai.
- [GAP] System-prompt **canary token** — prompt chori ki koshish chup-chaap na
  rahe, log ho.
- Saboot: knowledge recall@5 purana vs naya, Hinglish sawaalon ke set pe.

## STEP 12 — Multimodal AI
*Repo essence: dekho, suno, padho — sab ek saath.*
- [GAP] **Visual token budget** screen watcher ke liye: har nazar (glance) ka
  token kharcha pehle se bandha (UI 729–1024) — fixed-budget front-end taaki
  context kabhi behisaab na bade. [Ban chuka: `jarvis/watcher_budget.py`.]
- [HAI] Grounding discipline (element_desc ke saath re-grounding) v2 se seekhi
  hui; v4 me Step 04 ke gates ke saath judegi.
- [GAP] Thinker-Talker latency hisaab voice loop ke budget me (Step 06 ke saath).
- Saboot: per-glance token cost naapa hua, budget ke andar.

## STEP 13 — Tools & Protocols
*Repo essence: AI aur asli duniya ke beech ke interfaces.*
- [GAP] **Idempotency ledger**: har side-effect (message bhejna, file likhna,
  reminder) business key + argument fingerprint ke saath — pehle PENDING likho,
  phir kaam karo, phir commit. Crash ke baad "hua ya nahi?" ka jawab hamesha
  milega. [Ban chuka: `jarvis/idempotency.py`, journal ke upar.]
- [GAP] MCP tools ke liye **descriptor hash-pinning + drift quarantine** — tool
  ka description/schema badla to pehchan ruki, dobara approval. (Wire protocol
  ASLI MCP spec 2025-11-25 hi rahega — repo ki nakli 2026 wali nahi.)
- [GAP] Structured action requests: shell strings ki jagah argv[]/paths[]/
  network[] — gate ko saaf faisla mile.
- Saboot: kill-mid-send test — process beech me maara, dohraya asar ZERO.

## STEP 14 — Agent Engineering
*Repo essence: agent pehle usool se.*
- [GAP] **STANDING BEHAVIOUR EVAL SUITE** (is frame ka sabse aham step):
  har asli failure ek case; har guardrail ke saath uska eval case; har badlaav
  regression-gated. [Neenv ban chuki: `jarvis/behaviour_eval.py` + 11 cases,
  staging baseline 10/11 — v4 ki apni pipeline pe Step 11/13 ke baad sakriya.]
- [GAP] **Feedback ratchet**: corrections prose me jama nahi hongi — root cause
  ke hisaab se test / policy / automation / example / decision-record me
  jayengi, fingerprint + recurrence count + retirement date ke saath.
- [GAP] **ReWOO plan-DAG mode** bade kaamo ke liye (ReAct ke saath): pehle
  poora plan, har node pe evidence + proof; bina-proof node reject.
- [GAP] Task-class budgets (ek hi global 3-round/12-call cap ki jagah) + per-tool
  circuit breakers.
- [GAP] Memory validity intervals (invalidate, delete nahi) + calibrated
  separate reviewer (self-critique ki jagah; ≥80% calibration agreement).
- Saboot: eval suite ka baseline number — aur har aane wala badlaav usi se naapa.

## STEP 15 — Autonomous Systems
*Repo essence: bina insaan ke chalte agent, surakshit tareeke se.*
- [HAI] Supervisor (backoff/FATAL/verified restart) ka usool v2 se; v4 me
  journal + ledger ke saath.
- [GAP] **Bounded self-improvement ke 4 gates** evolution ke liye: uneditable
  invariant checker, edit-surface se bahar pinned objective, multi-objective
  constraints, regression pe pause.
- [GAP] **Evaluator firewall**: jo JARVIS ko sudhaare, wo apne number khud na
  badal sake — evaluator hamesha edit-surface se bahar.
- [GAP] **Memory canary entries**: memory me jaan-boojh ke sentinel facts;
  kisi untrusted channel ke output me dikhe to exfiltration alarm.
- [GAP] **Spend-velocity trip**: >$X/10min = access cut (restart caps ke saath
  ye missing ghanti hai).
- [GAP] Propose-then-commit phone R1 actions pe (Step 13 ledger ke saath).
- Saboot: canary-leak drill — jaan-boojh ke leak pe alarm bajta hai ya nahi.

## STEP 16 — Multi-Agent & Swarms
*Repo essence: coordination aur emergence.*
- [HALKA] JARVIS khud single-agent hi rahega (repo bhi yahi kehta hai:
  frameworks pe single + deterministic routing jeet-ta hai). Ye step zyadatar
  Ovi ke apne kaam ka aaina hai (parallel teams + coordinator).
- [GAP] **Memory poisoning ki hifazat**: har memory write pe provenance,
  append-only supersession (purani entry mit-ti nahi, supersede hoti hai),
  aur ek unwritable verifier ka usool.
- [GAP] MAST failure accounting: har failure Specification / Coordination /
  Verification bucket me gina jayega (eval suite ke saath).
- Saboot: poisoning drill — jhooti entry memory me "fact" nahi ban pati.

## STEP 17 — Infrastructure & Production
*Repo essence: ship karo — scale, monitor, optimize.*
- [GAP] **Goodput SLOs** providers ke liye: average latency nahi — kitne %
  request waqt + quality dono pe khare utre. Fallback chain ka judge yahi.
- [GAP] Prefix-cache discipline ka production roop (Step 11 ka audit chalta
  rahe): immutable system hissa pehle, dynamic baad me.
- [GAP] Per-task cost attribution + provider-outage **chaos drills** (jaankari
  ke liye provider band karke dekho — chain sambhalti hai ya nahi).
- [GAP] Data-dir ka **DR manifest** (Step 00 se aaya): restore ka drill asal me
  karke dikhana, sirf likhna nahi.
- Saboot: outage drill report + restore drill report.

## STEP 18 — Ethics, Safety & Alignment
*Repo essence: ye optional nahi hai.*
- [GAP] **IFC labels**: har content trusted/untrusted labelled; untrusted
  content (web, tool output, file) sirf inform kar sakta hai — action
  ORIGINATE kabhi nahi kar sakta. EchoLeak three-boundary rule.
- [GAP] Attack regression sets: PAIR / many-shot / encoding attacks ke cases
  eval suite me — har gate badlaav pe chalein. (Adaptive-attack humility:
  >90% published defences gir chuki hain — blocklist saboot nahi hai.)
- [GAP] Normalize-before-detect: base64/rot13/zero-width ko pehle saaf karo,
  phir jaancho.
- [HAI] Usool pehle se: event log hi audit record hai, model ki self-report
  kabhi nahi.
- Saboot: injection regression set pe ZERO untrusted-origin actions.

## STEP 19 — Capstone Projects
*Repo essence: portfolio-grade system bana ke saabit karo.*
- **JARVIS khud hi capstone hai.** Is step me koi naya hissa nahi banta —
  sab kuch jud ke ek acceptance din hota hai.
- [GAP] **Over-refusal eval**: mana karne ko alag naapo (bland deflection =
  chhupa hua failure) — fallback path ki quality ka number.
- [GAP] Voice-loop capstone ke number (Step 06 SLOs) acceptance ka hissa.
- [GAP] During-generation streaming checkpoint — jawab bante-bante jaanch.
- Saboot: acceptance list — kya chala, kya naapa, kya baaki raha (imaandaar
  "NOT-MEASURED" nishaan un cheezon pe jo sirf owner ke PC pe naapi jayengi).

---

## FRAME KA NAQSHA (ek nazar me)

- Steps 00–05: **neenv** — setup, hisaab, naap, nazar, zubaan
- Steps 06–13: **core** — awaaz, dimaag ki raftaar, seekh, khoj, auzaar
- Steps 14–18: **agent-grade** — eval, khud-kaari ki hifazat, production, safety
- Step 19: **saboot ka din** — JARVIS khud capstone

*Ye frame `ai-engineering-from-scratch` (rohitg00, MIT) ke 20-phase process aur
uski deep study (jarvis-knowledge/ai-eng-study/) se bana hai. Repo ki galtiyaan
(ledger Part 12) is frame se bahar rakhi gayi hain.*
