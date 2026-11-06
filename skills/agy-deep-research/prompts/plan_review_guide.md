# Research Plan Review & Prompt Architecture Guide (Caller Verification Rubric)

## 1. Dual-File Separation Contract (`prompt.md` vs. `goal.md`)

`prompt.md` and `goal.md` have completely separate consumers and MUST NEVER contain identical text (`IDENTICAL_PROMPT_AND_GOAL` exit code `5`):

- **`prompt.md` (`--prompt` / `--prompt-file`) — Consumed by `Gemini.exe` Deep Research Agent**:
  Must follow the 5-block XML specification (use `deep_research.py compose-prompt` or `--compose-xml` to generate):
  1. `<research_objective>`: Frame a multi-dimensional comparative/synthesis investigation using explicit action verbs (`Compare`, `Analyze`, `Synthesize`, `Evaluate`). Never submit a simple factoid lookup or open-ended question, which causes Gemini to reply with clarifying questions instead of generating the `[data-test-id="cancel-fuse-button"]` plan card (`PLAN_TIMEOUT` exit code `2`).
  2. `<context_and_scope>`: Provide self-contained context (`confine_hook.py` blocks reads outside `<job_dir>`). Inline any local code/schema excerpts before `init`. If internal documents exist in Google Workspace (Gmail, Google Drive, Google Chat), specify the exact Drive folder/document title or Gmail thread query so `Gemini.exe` reads them natively via the authenticated Google Workspace permission graph. Declare temporal/domain bounds and excluded introductory topics.
  3. `<investigation_axes>`: Seed high-entropy technical terms (e.g., `HNSW`, `DiskANN`, `Vamana graph`, `IVF-PQ`, `Lucene 10 BM25 quantization`, `SIMD block-max WAND`, `Recall@10`, `p99 latency`) and include a **Conditional Depth-First Trigger** instructing the agent to trace hardware specs, dataset distributions, and measurement methodologies in primary literature whenever sources report conflicting numbers.
  4. `<source_governance>`: Prioritize Tier-1 primary sources (peer-reviewed papers with DOI/arXiv links, official engineering specs, reproducible benchmarks) with numbered inline citations and verification dates. Separate confirmed empirical facts from hypotheses. Enforce the negative-finding rule: state `'검증 가능한 공개 근거 불충분 (Insufficient verified evidence)'` when public evidence is missing rather than fabricating numbers.
  5. `<output_format>`: Require GitHub-Flavored Markdown (GFM) of `>= 2,500` characters (strictly `>= 400` characters) with explicit top-level `#` and section `##` Markdown headings (`REPORT_QUALITY_GATE_FAILED` / `REPORT_STRUCTURE_INVALID` exit code `2`). If structured JSON is needed by downstream tools, embed a fenced ```` ```json ```` block inside a dedicated `##` section—never request raw/pure JSON without Markdown headings.

- **`goal.md` (`--goal` / `--goal-file`) — Consumed by Caller Orchestrator (Claude) During Plan Review**:
  Defines the fixed acceptance checklist (mandatory entities/algorithms, required quantitative metrics, scope exclusions, and Tier-1 source verification) used to evaluate `plan_vN.md`.

---

## 2. Reviewing `plan_vN.md` Against `goal.md`

Before approving a Gemini Deep Research plan (`deep_research.py approve`), compare `latest_plan` (`<job>/plan_v*.md`) against `<job>/goal.md`:

1. **Subtopic & Entity Coverage**: Does the plan explicitly cover every required entity, algorithm, technical dimension, and quantitative metric listed in `goal.md`?
2. **Scope & Exclusions**: Does the plan respect the scope boundaries and avoid excluded topics or time periods stated in `goal.md`?
3. **Methodology & Primary Evidence**: Does the plan gather primary sources, benchmarks, specifications, or Google Workspace entities required by `goal.md` rather than superficial summaries?
4. **Action Decision**:
   - **Missing or drifted items (`edit`)** → Run `deep_research.py edit --job <id> --feedback "<imperative delta instructions>"`.
     - **CRITICAL (`FEEDBACK_INTERROGATIVE_DENIED` exit code `5`)**: Never ask conversational or interrogative questions (e.g., `"Why did you choose DiskANN?"` or sentences ending with `?`) in `--feedback`. Questions cause Gemini to exit plan-editing mode and reply with conversational prose, triggering `PLAN_CHANGED` or `PLAN_TIMEOUT`. Issue **imperative delta instructions only** (`Add`, `Remove`, `Reorder`, `Pin`).
   - **All criteria in `goal.md` satisfied (`approve`)** → Run `deep_research.py approve --job <id> --reason "<20+ char explanation mapping plan_vN.md items to goal.md criteria>" --out <report.md>`.
