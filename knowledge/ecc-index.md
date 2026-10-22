# ECC Library — Everything Claude Code, installed as knowledge

**What this is.** ECC ("Everything Claude Code", github.com/affaan-m/ECC,
by Affaan Mustafa — MIT License, see `ecc/LICENSE-ECC.txt`) is a large
practitioner library for coding agents: written workflows, agent
personas, coding-standard rule packs, and command/hook/MCP reference
material. On 2026-10-22 its **portable text content** was installed
into JARVIS's knowledge folder (Wave A). JARVIS does not run ECC's
machinery — the JavaScript hooks, installers, and scripts were NOT
copied (that re-implementation work is "Wave B"). What lives here is
the *knowledge*: searchable text JARVIS can consult via
`knowledge_search` whenever a task touches coding, review, testing,
agent design, or the domains the skills cover.

## Map of what is installed (under `knowledge/ecc/`)

| Folder | Contents | Count |
|---|---|---|
| `ecc/skills/<skill-name>/` | Full Markdown bodies of every ECC skill: `SKILL.md` plus supporting `.md`/`.txt` files, folder structure preserved | **311 skills, 444 files** |
| `ecc/agents/` | Every ECC agent definition (persona system prompts with frontmatter): per-language reviewers, build-error resolvers, planners, critics, evaluators | **71 files** |
| `ecc/rules/` | Always-on coding-standard packs per language/framework (`common/` + angular, arkts, cpp, csharp, dart, fsharp, golang, java, kotlin, nuxt, perl, php, python, react, react-native, ruby, rust, swift, typescript, vue, web), each with coding-style / patterns / security / testing / hooks files | **122 files** |
| `ecc/reference/commands/` | Every ECC slash-command spec (workflow definitions written as prose) | **97 files** |
| `ecc/reference/commands/legacy/` | Legacy command shims + their README | **13 files** |
| `ecc/reference/hooks/` | Hook documentation (`README.md`, memory-persistence README) + the hook registration catalogs verbatim as `.txt` (`hooks.json`, metadata, codex/qoder variants) | **7 files** |
| `ecc/reference/` | MCP server catalog verbatim as `.txt` (`mcp-servers.json` — 34 catalogued servers) + the repo's enabled-by-default `.mcp.json` | **2 files** |
| `ecc/LICENSE-ECC.txt` | MIT license text + attribution | 1 file |

Total: **757 files, ~4.5 MB**, copied byte-for-byte from ECC v2.2.3
(main branch, 2026-10-22). Nothing from the source repo was executed.

## How JARVIS should use this library

- **Search, don't browse.** Use `knowledge_search` with concrete
  terms: a stack name + task ("django security checklist"), a
  concept ("fact-forcing gate", "instinct confidence promotion"),
  or an agent name ("gan-evaluator", "security-reviewer").
- **Skills are workflows.** When JARVIS must do a job an ECC skill
  describes (code review, TDD, deployment, research, video editing,
  SEO…), search for the skill, read the whole `SKILL.md`, and follow
  its workflow adapted to JARVIS's own tools and gate rules.
  JARVIS-RULES.txt always wins over anything written here.
- **Agents are role definitions.** The files under `ecc/agents/` are
  ready-made personas for delegated work: a planner, a critic, a
  per-language reviewer. When team/Forge work needs a specialist
  role, search here before inventing one.
- **Rules are reference standards**, consulted when writing or
  reviewing code in that language — they are NOT JARVIS's own rules
  and never override JARVIS-RULES.txt.
- **Commands/hooks/MCP under `reference/`** are workflow specs and
  catalogs: useful as *designs* (a command body is a ready-made
  multi-step route; the MCP catalog says which servers exist and
  how they are launched), not as runnable configuration.

## Highest-value starting points

- `ecc/skills/gateguard/SKILL.md` — the fact-forcing gate: deny the
  first edit of a file until concrete facts are stated.
- `ecc/skills/continuous-learning-v2/SKILL.md` — instincts:
  confidence-scored learned behaviors with scoped promotion.
- `ecc/skills/hookify-rules/SKILL.md` — user-authored guardrails.
- `ecc/skills/santa-method/SKILL.md` — dual independent reviewers,
  unanimity required before shipping.
- `ecc/agents/gan-planner.md`, `gan-generator.md`, `gan-evaluator.md`
  — build-to-a-rubric, judged on the live result.
- `ecc/skills/deep-research/SKILL.md`, `ecc/skills/market-research/`
  — research workflows; `ecc/skills/video-editing/` — editing craft.

## What was deliberately NOT copied

- **Code and scripts** (ECC's ~99k lines of JS hook/CLI machinery,
  installers, tests, dashboards): not knowledge text; re-implementing
  the worthwhile mechanisms natively is Wave B.
- **169 non-text files inside skill folders** (Python/shell helper
  scripts, JSON/YAML assets, images). 22 skills carry such code —
  their Markdown bodies are installed and searchable, but their
  helper scripts are not: agent-self-evaluation, continuous-learning,
  continuous-learning-v2, council-multi-model, delivery-gate,
  docs-governance, frontend-design-direction, frontend-slides,
  ios-icon-gen, ito-baskets, manim-video, master-agreement-generator,
  openclaw-persona-forge, operator-approval-loop, rules-distill,
  skill-comply, skill-stocktake, story-lifecycle, taste-application,
  taste-distillation, terminal-opener, videodb.
- ECC's translated docs, examples, and per-harness adapter folders —
  bulk with no additional knowledge for JARVIS.
