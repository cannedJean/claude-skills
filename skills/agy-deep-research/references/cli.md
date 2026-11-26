# CLI reference: subcommands, options, model and effort settings

Read this when you need an option that SKILL.md does not show (compose flags, timeouts, `collect --url`, model pinning, environment variables). `python scripts/deep_research.py <subcommand> --help` is the source of truth; timeouts and thresholds are the constants at the top of `scripts/deep_research.py`.

## Naming a job

Every job subcommand takes `--job` (aliases `--job-dir`, `-j`).

- A bare id (`--job vector-db`) resolves under the isolated jobs root (see architecture.md). **Use this form.**
- Omitting `--job` on `init` / `auto` allocates `<timestamp>-<hex>`; read `job_id` from the JSON output.
- A path (absolute, or containing `/`, `\`, or a leading `.`) is used as given and refused with `GIT_REPO_ESCAPE` when any parent holds `.git`. A relative path typed inside a repository always fails.

`--out` may point anywhere, including the user's repository; only the job directory must be outside git.

## Subcommands

| Subcommand | Options | What it does |
|---|---|---|
| `preflight` | `[--target-dir DIR]` | Checks agy, playwright, Gemini app install, CDP liveness, and script sync. Exit 4 on failure. |
| `setup` | `[--target-dir DIR]` | Copies the bundled scripts and `references/SKILL.md` into the agy skill dir. |
| `compose-prompt` | `--objective TEXT` `[--goal TEXT]` `[--context-scope TEXT]` `[--axes TEXT]...` `[--sources-policy TEXT]` `[--output-spec TEXT]` `[--workspace-sources TEXT]` `[--entities TEXT]...` `[--metrics TEXT]...` `[--exclusions TEXT]` `[--prompt-out FILE]` `[--goal-out FILE]` | Prints (and optionally writes) a 5-block XML prompt and a goal rubric. Starts nothing. |
| `init` | `[--job ID]` `[--browser {chrome,app}]` `--prompt TEXT` \| `--prompt-file FILE` `--goal TEXT` \| `--goal-file FILE` `[--out PATH]` `[--compose-xml]` `[--strict-xml]` + compose flags | Creates the job, writes `prompt.md` / `goal.md`, locks model settings. Output `warnings` lists sensitive-keyword hits, `architecture_warnings` lists local-path and JSON-only traps. |
| `plan` | `--job ID` `[--deadline SEC]` `[--strict-xml]` `[--show]` | Runs the plan step, cancels the auto-start countdown, saves `plan_v1.md`. |
| `edit` | `--job ID` `--feedback TEXT` \| `--feedback-file FILE` `[--allow-questions]` `[--deadline SEC]` `[--show]` | Sends imperative feedback, saves `plan_vN.md`. |
| `approve` | `--job ID` `--reason TEXT` `[--out PATH]` `[--no-wait]` `[--timeout SEC]` `[--poll-interval SEC]` `[--show]` | Approval gate, then starts the detached research. Without `--no-wait` it streams progress and finalizes the report. |
| `auto` | `init` options (including `--browser`) + `[--reason TEXT]` `[--no-wait]` `[--timeout SEC]` `[--poll-interval SEC]` `[--show]` | `init` + unreviewed research + report gates. |
| `status` | `--job ID` | Job state, worker pid liveness, latest progress. `ready_to_collect` means the worker finished. |
| `collect` | `--job ID` `[--out PATH]` `[--url URL]` `[--no-wait]` `[--detach]` `[--timeout SEC]` `[--poll-interval SEC]` | Fetches the report from the job's own conversation and runs the report gates. Never starts a research (`COLLECT_WOULD_START`). `--url` binds a job that never recorded its conversation. |

`compose-prompt`, `init`, `plan`, `edit`, `approve`, `auto`, `collect` also accept `--agy-model`, `--agy-effort`, `--gemini-model`. `plan`, `edit`, `approve`, `auto`, `collect` accept `--dr-skill-dir` (override the agy skill dir).

Defaults for `--deadline` (plan/edit) and `--timeout` (approve/auto/collect) come from `DEFAULT_PLAN_DEADLINE` / `DEFAULT_TIMEOUT`, and from `ARGON_PLAN_DEADLINE` / `ARGON_TIMEOUT` when the Gemini model is Argon. The driver keeps waiting for a plan while Gemini is visibly generating, up to `GEMINI_DR_PLAN_MAX_WAIT` (env, read by `run_deep_research.py`).

Supporting scripts (each has `--help`):

```bash
python scripts/patch_gemini_app.py [--port PORT] [--check] [--dry-run] [--show] [--restore]
python scripts/relay_step.py --job <job_dir> [--dry-run]
python scripts/run_deep_research.py --step {plan,edit,approve,auto} --prompt <text> [--output <path>] [--dry-run]
```

`patch_gemini_app.py` without `--check` / `--dry-run` / `--restore` modifies and relaunches the installed app.

## Where the research runs (`--browser`)

Set on `init` / `auto` and fixed for the job (stored as `browser` in `job.json`; precedence: option, then `AGY_DR_BROWSER`, then `chrome`). Jobs created before the option existed have no field and run in app mode.

| Value | What happens | Port |
|---|---|---|
| `chrome` (default) | `scripts/chrome_launcher.py` opens a visible Chrome window on its own profile at gemini.google.com and the driver connects to it. Nothing installed is modified. The user signs in to Google in that window once | 9224 (`GEMINI_DR_CHROME_PORT`) |
| `app` | The Gemini desktop app is patched and started hidden (references/app-mode.md) | 9223 (`GEMINI_CDP_PORT`) |

```bash
python scripts/chrome_launcher.py --check   # read-only: chrome.exe path, profile, port liveness
python scripts/chrome_launcher.py           # open (or reuse) the window, e.g. for the first sign-in
```

Chrome-mode environment: `GEMINI_DR_CHROME_PATH` (chrome.exe), `GEMINI_DR_CHROME_PROFILE` (default `%LOCALAPPDATA%\agy-deep-research\chrome-profile`), `GEMINI_DR_CHROME_PORT`. `deep_research.py` passes `GEMINI_DR_BROWSER` to the relay and driver (the driver itself defaults to the app when it is unset).

## Model and effort settings

| Option | Controls | Default |
|---|---|---|
| `--agy-model` | agy model that operates the relay (passed to `agy_task.py --model`) | `flash 3.8` |
| `--agy-effort` | agy reasoning effort: `low`, `medium`, `high` | `high` |
| `--gemini-model` | Gemini app mode that does the research, with thinking level | `Pro`, `high` |

Leave the agy settings at their defaults: agy only runs `relay_step.py`, so a larger agy model adds startup latency and no research quality.

| `--gemini-model` | Use for |
|---|---|
| `flash` | Fast, broad scoping with low quota use. Spell out sub-steps in `<investigation_axes>`. |
| `pro` (default) | Standard literature reviews and technical comparisons. |
| `argon` | Frontier reasoning and verification. One run uses a large share of the subscription quota, so never run it in `auto`; always review the plan first. Longer deadline and timeout apply automatically. |

**Model lock.** `--gemini-model` is applied only in the new chat during `plan` / `auto`. `edit`, `approve`, `collect` never touch the model menu (they log `[MODEL_OBSERVED]`). To change the model, start a new job.

**Aliases** (case-insensitive; anything else is passed through verbatim):

| Setting | Canonical | Aliases |
|---|---|---|
| agy model | `flash 3.8` (agy slug `gemini-3.8-flash`) | `flash`, `flash 3.8`, `flash-3.8`, `flash3.8`, `3.8 flash`, `3.8-flash`, `플래시` |
| agy model | `pro` | `pro`, `pro 3.8`, `pro-3.8`, `pro3.8`, `3.8 pro`, `프로` |
| agy effort | `high` | `high`, `높음`, `h`, `최대`, `hi` |
| agy effort | `medium` | `medium`, `중간`, `보통`, `m`, `med`, `mid` |
| agy effort | `low` | `low`, `낮음`, `l`, `lo` |
| Gemini model | `Pro` / `high` | `pro`, `3.8 pro`, `pro 3.8`, `high`, `높음`, `pro high`, `pro-high`, `pro (높음)`, `프로`, `advanced` |
| Gemini model | `Flash` / `high` | `flash`, `flash 3.8`, `flash-3.8`, `flash3.8`, `3.8 flash`, `플래시` |
| Gemini model | `Argon` / `high` | `argon`, `아르곤` |

An invalid effort exits 5 (`INVALID_EFFORT`). `agy_task.py` maps the friendly agy model name to the binary slug, because `agy.exe` rejects `--effort` together with a raw name such as `"flash 3.8"`.

**Resolution order**, highest first:

1. Command-line option on the subcommand.
2. `job.json` (settings resolved earlier in the job are inherited by later steps).
3. `AGY_DR_MODEL`, `AGY_DR_EFFORT`, `GEMINI_DR_MODEL`; `GEMINI_DR_THINKING` (`low` / `medium` / `high`) overrides the thinking level.
4. `AGY_MODEL` or `AGY_TASK_MODEL`; `AGY_EFFORT` or `AGY_TASK_EFFORT`; `GEMINI_MODEL` or `GEMINI_DR_MODE`.
5. Built-in defaults (`DEFAULT_AGY_MODEL`, `DEFAULT_AGY_EFFORT`, `DEFAULT_GEMINI_MODEL`, `DEFAULT_GEMINI_THINKING`).

Other environment variables: `AGY_DR_JOBS` (jobs root), `AGY_DR_SKILL_DIR` (agy skill dir), `AGY_TASK` (path of `agy_task.py`), `GEMINI_CDP_PORT`, `AGY_DR_POLL_INTERVAL`, `GEMINI_APP_BASE_DIR` (where the `app-*` directories are; default `%LOCALAPPDATA%\Google\Gemini`), `AGY_DR_PATCH_STATE` (patch state file; default `%LOCALAPPDATA%\agy-deep-research\patch_state.json`).

`preflight` reports the app patch under `checks.gemini_app.patch`: `app_version`, `patched`, `pattern_match` (whether `SECURITY_GUARD_RE` still finds the guard), `last_patched_version` and `update_needs_repatch`. It reads only; `patch_gemini_app.py --check` shows the version fields too.

## Examples

Pin models with Korean aliases, files for the two inputs, report into the current project:

```bash
python scripts/deep_research.py init --job vector-db \
  --prompt-file prompt.md --goal-file goal.md \
  --gemini-model "프로" --agy-effort "높음" \
  --out ./reports/vector_db_report.md
```

Let the tool wrap a plain objective into the 5-block prompt and a goal rubric:

```bash
python scripts/deep_research.py init --job vector-db --compose-xml \
  --prompt "Compare HNSW, DiskANN and IVF-PQ for 100M-embedding vector search." \
  --goal "Every index type has Recall@10, p99 latency and memory numbers from primary benchmarks." \
  --entities HNSW --entities DiskANN --entities IVF-PQ \
  --metrics "Recall@10" --metrics "p99 latency" --exclusions "vendor marketing pages"
```

Bind and collect a research whose job lost its conversation url:

```bash
python scripts/deep_research.py collect --job vector-db --url https://gemini.google.com/app/<conversation_id>
```
