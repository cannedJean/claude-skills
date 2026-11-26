---
name: agy-deep-research
description: Use when the user asks for Gemini Deep Research ("딥리서치", "Gemini Deep Research", "/local-gemini-deep-research") or wants exhaustive multi-source web research run on their own Gemini subscription in a local Chrome window on gemini.google.com (or the Windows Gemini desktop app), with no API key. Not for delegating coding work to agy (agy-delegate) and not for research Claude performs itself with its own web tools (anthropic-skills:deep-research).
---

# agy-deep-research

You (Claude) are the orchestrator. `scripts/deep_research.py` drives Gemini through the Antigravity CLI (`agy`), by default in a visible Chrome window on its own profile (`--browser chrome`; the patched desktop app is `--browser app`): Gemini proposes a research plan, you review it against acceptance criteria you wrote beforehand, Gemini researches for 5–15 minutes, and you check the report against the same criteria before handing it over. The script's gates reject malformed steps; whether the plan and the report are good enough is your judgment.

`DR` below means `python "<this skill's directory>/scripts/deep_research.py"`. Every command prints JSON; on failure read `code` and `hint`.

**Use for** multi-source comparisons, market or literature surveys, anything worth minutes of autonomous browsing with citations.
**Do not use for** single-fact lookups (search directly), machines without Google Chrome (or the Gemini desktop app), or when the user wants Claude's own research.

## Rules

- **Supervised by default.** Use `auto` only when the user said to skip plan review *and* the model is not Argon. A bad plan costs a full research run of the user's quota.
- **One job drives Gemini at a time.** The Chrome window (or app) has one shared Gemini page; a second job's step makes the first one's result get refused.
- **Never run a research step before step 0 is settled.** The first step opens the Chrome window (app mode: patches the installed app) without asking.
- **Do not rewrite the user's topic to dodge a filter.** If `init` returns `warnings` (sensitive Korean keywords; substring match, so `정치경제학` also hits) or a step fails with `SAFETY_BLOCKED`, tell the user which words hit and agree on wording with them. A warning alone blocks nothing; a harmless hit can proceed once the user knows.

## Workflow

### 0. Preflight, then tell the user what will open

```bash
$DR preflight
```

Read-only. New jobs run in Chrome mode; look at `checks.chrome`:

- `found: false`: Google Chrome is missing. Tell the user; stop, or use the app only if they ask for it.
- `profile_exists: false` (first run): before the first `plan`, tell the user and wait for a yes:
  1. A visible Chrome window opens on a separate profile (`profile_dir`), not their everyday Chrome, showing gemini.google.com.
  2. They sign in to their Google account in that window once. You never sign in for them.
  3. While it is open, the window listens on a local debugging port (`port`), which local programs could use to control it. Closing the window stops it; nothing installed is modified.
- Otherwise continue. The window may already be open (`cdp_alive`); the next step reuses it, or reopens it if it was closed.

Only if the user asks for the desktop app, pass `--browser app` to `init` and follow [references/app-mode.md](references/app-mode.md) instead (it patches the installed app and needs its own consent).

### 1. Fix what preflight reports

Exit 4: fix what `checks` shows before going on. `sync_status` other than `in_sync`: run `$DR setup` (copies the relay scripts to the agy skill directory), then preflight again.

### 2. Write `prompt.md` and `goal.md` as two different files

If the request does not say what to compare, by which measures, at what scale, or in which language the report should be, ask the user once before writing: the goal is fixed before any plan exists, and a guessed scope cannot be reviewed against. Write both files to a temp directory, not the job directory (init creates that).

- **`prompt.md` is for Gemini**: what to research and how to report. Use the 5-block XML form in [references/prompt-spec.md](references/prompt-spec.md), or write a plain objective and add `--compose-xml` to `init`, which wraps it. Inline any code or schema the research needs: Gemini cannot read local paths.
- **`goal.md` is for you**: the checklist you will hold the plan and the report against. Gemini never sees it. Write it before you see any plan, so the plan cannot talk you into its own scope. Identical prompt and goal text is rejected (`IDENTICAL_PROMPT_AND_GOAL`).

`goal.md` has four parts, each item checkable as yes/no:

```markdown
# Acceptance criteria: vector DB comparison
## Must cover
- [ ] Milvus, Qdrant, Weaviate, pgvector (each one, not "several")
## Must report
- [ ] Recall@10, p99 latency, memory per 10M vectors, each with dataset and hardware named
- [ ] Monthly cost at 100M vectors, managed vs. self-hosted
## Out of scope
- [ ] No embedding-model comparison, nothing older than 2023
## Source standard
- [ ] Numbers come from papers, official docs, or reproducible benchmarks, not vendor blog claims alone
```

### 3. Create the job

```bash
$DR init --job vector-db --prompt-file prompt.md --goal-file goal.md --out ./reports/vector-db.md
```

Pass a bare id to `--job` (or omit it and read `job_id` from the output). The job is then created in the isolated jobs root outside any repository. A path such as `./jobs/x` inside a git repository is rejected (`GIT_REPO_ESCAPE`); only `--out` may point into the project (its folder is created). The Gemini model defaults to Pro; pass `--gemini-model argon` only when the user asks for it ([references/cli.md](references/cli.md)). Check `warnings` (rule above) and `architecture_warnings` (local-path reference, JSON-only request) and fix the prompt before continuing.

### 4. Get the plan and review it against `goal.md`

```bash
$DR plan --job vector-db
```

Takes several minutes (run it in the background if your shell call would time out). Then go through `goal.md` item by item against the returned `plan` and mark each covered / missing / contradicted. Do not approve a plan because it "looks thorough". [prompts/plan_review_guide.md](prompts/plan_review_guide.md) lists what to look for.

### 5. Fix gaps with `edit`, at most twice

```bash
$DR edit --job vector-db --feedback "Add a step measuring pgvector HNSW memory per 10M vectors. Remove the embedding-model comparison."
```

Feedback is imperative changes only (Add, Remove, Reorder, Pin). A question makes Gemini answer in prose instead of producing a plan card and is rejected (`FEEDBACK_INTERROGATIVE_DENIED`). Re-review the new plan the same way. The code has no limit on rounds; **after two edits that still leave goal items missing, stop and show the user the plan and the missing items** rather than looping, since each round costs minutes and the prompt is usually what needs fixing.

### 6. Approve, then wait in the background

```bash
$DR approve --job vector-db --no-wait --reason "plan_v2 covers all four engines, the three metrics with dataset/hardware, cost at 100M; embedding comparison removed."
$DR collect --job vector-db
```

`--reason` must say which plan items satisfy which goal items (20+ characters, enforced; it is the audit record). `approve --no-wait` returns once the research has started. Start `collect` as a **background command**: it tails the worker and exits when the report is in, so you are notified without polling. Tell the user it is running and stop; do not sleep-loop on `status`, and do not start another job meanwhile.

### 7. Check the report against `goal.md` before reporting

When `collect` returns `completed`, read `report_file` and `goal.md` and:

1. Judge every goal item: **met** (with the section that meets it), **partial** (what is thin or unsourced), or **missing**.
2. Search the report for `검증 가능한 공개 근거 불충분` / `Insufficient verified evidence` and list what Gemini could not verify.
3. Look at `sources_count` and `report_warnings` in the output. `NO_SOURCES` or `REFUSAL_SUSPECTED` means read the report top before anything else; `GOAL_CHANGED_SINCE_INIT` means the criteria were edited mid-job.
4. Spot-check that the numbers answering "Must report" items carry a citation.

### 8. Report to the user

Give the report path, then the verdict per goal item, the unverified points, the source count, and the Gemini conversation `url`. If anything is partial or missing, say exactly what, and let the user decide between accepting it, a new job with a tightened prompt, or filling the gap another way. Do not rerun on your own and do not describe a report with missing items as complete.

## `auto` (no plan review)

```bash
$DR auto --job quick-scan --prompt-file prompt.md --goal-file goal.md --out ./reports/quick-scan.md --no-wait
```

Only under the rule above. Steps 0–2 and 7–8 still apply; follow it with a background `$DR collect --job quick-scan`.

## Common failures

| Code (exit) | Meaning | Do |
|---|---|---|
| `GIT_REPO_ESCAPE` (5) | Job path is inside a git repository | Pass a bare id to `--job` |
| `IDENTICAL_PROMPT_AND_GOAL` (5) | Same text for prompt and goal | Rewrite `goal.md` as the checklist in step 2 |
| `FEEDBACK_INTERROGATIVE_DENIED` (5) | Edit feedback asks a question | Restate as Add / Remove / Reorder / Pin |
| `APPROVAL_GATE_REASON_TOO_SHORT` (5) | `--reason` under 20 characters | Map plan items to goal items |
| `PLAN_TIMEOUT` / `SAFETY_BLOCKED` (2) | No plan card: prompt read as a question, or filtered | Reshape the prompt as a research task; for a filter hit, involve the user |
| `CONVERSATION_MISMATCH` (2) | The shared app page was moved during the step | Nothing was saved. When no other job is running, `collect` again (or repeat the refused `plan` / `edit` / `approve`) |
| `REPORT_QUALITY_GATE_FAILED` / `REPORT_STRUCTURE_INVALID` (2) | Report too short or has no Markdown heading (e.g. the prompt asked for JSON only) | Show the user the staged `relay/report_<seq>.md`; fix the prompt in a new job |
| `CHROME_NOT_FOUND` / `CHROME_CDP_NOT_READY` (3) | Chrome missing, or a window of the dedicated profile was opened without the port | Tell the user to install Chrome or close those windows; repeat the step |
| `GEMINI_PAGE_NOT_FOUND` / `UI_SELECTOR_TIMEOUT` (3) | Usually not signed in | Ask the user to sign in in the Chrome window, then repeat the step |
| `CDP_NOT_READY` / `ASAR_PATTERN_MISMATCH` (3) | App mode only | [references/app-mode.md](references/app-mode.md) |

## References

- [references/prompt-spec.md](references/prompt-spec.md): read before hand-writing `prompt.md`, or when `init` reports missing XML blocks.
- [references/cli.md](references/cli.md): read when you need an option not shown here (model pinning, Argon, timeouts, `collect --url`, env vars).
- [references/gates-and-exit-codes.md](references/gates-and-exit-codes.md): read when a code is not in the table above, or for the full recovery steps after `CONVERSATION_MISMATCH` / `REPORT_PROVENANCE_MISSING`.
- [references/troubleshooting.md](references/troubleshooting.md): read on environment, sign-in, UI, or timeout symptoms.
- [references/app-mode.md](references/app-mode.md): read only for `--browser app` jobs (desktop app patch, consent, app updates).
- [references/architecture.md](references/architecture.md): read when debugging the scripts or explaining Chrome mode or the app patch; holds the job directory layout.
- `references/SKILL.md` is not for you: it is the body of the agy-side skill `local-gemini-deep-research`, which `setup` copies.
