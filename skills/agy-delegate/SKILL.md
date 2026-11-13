---
name: agy-delegate
description: Delegate a coding task to the Antigravity CLI (`agy`, Google's terminal coding agent) in headless mode (single-agent or multi-agent `/teamwork`) and collect the files it produced, or run the confined `--deep-research` execution profile for headless Gemini Deep Research relay tasks. When using the Argon model, delegate runs proceed via `/teamwork` by default. Use when the user says things like "이건 agy한테 시켜", "Antigravity로 만들어와", "delegate this to agy", "아르곤으로 팀워크 돌려줘", or wants a coding chore done by a second agent and the outputs brought back. Not for tasks that need interactive approval, credentials, or multi-repo changes.
---

# agy-delegate

Runs one bounded coding task through `agy -p` (headless) inside a dedicated work directory,
then reports exactly which files were created / modified / deleted, the agent's summary,
and an optional local verification result. Works on macOS, Linux and Windows (agy runs natively).

Commands below are relative to this skill's directory (`scripts/agy_task.py`).

## Workflow

1. **Preflight** (first use, or after auth errors)
   ```
   python scripts/agy_task.py preflight
   ```
   If `agy not found` → install per README. If the headless probe fails → the user must run `agy`
   once interactively to sign in (browser). Never do the sign-in yourself.

2. **Write the task as a precise spec.** Include: file names to produce, language/version,
   function signatures or interfaces, acceptance criteria, and what NOT to do. Vague prompts give
   vague files. Put long specs in a file and pass `--task-file`.

   **Multi-Agent Delegation via `/teamwork`**:
   Antigravity CLI provides the `/teamwork` slash command, which dispatches an autonomous team of subagents
   (orchestrator, workers, qa) to plan, execute, and verify complex multi-file projects.
   - **Slash command mechanics**: In Antigravity (`agy -p`), slash commands must appear at **index 0** (the leading token)
     of the prompt payload. If preceded by conversational text or wrapper prose, `agy` treats the input as a regular LLM prompt
     instead of intercepting the slash command.
   - **How Claude instructs `/teamwork`**: Prefix the task string or task file with `/teamwork <spec>` (e.g.
     `-t "/teamwork Create utils.py with slugify ..."`). `scripts/agy_task.py` automatically detects leading slash
     commands and preserves them at index 0 without prepending standard wrapper text. Alternatively, pass `--bare`.
   - **Argon model tiers & aliases**:
     - Slugs: `gemini-4-argon-high` (recommended for complex tasks), `gemini-4-argon-medium`, `gemini-4-argon-low`.
     - Aliases: `argon`, `아르곤`, `gemini-4-argon` (automatically normalize to `gemini-4-argon-high`, or map to `--effort`).
   - **Argon Default Rule**: When using Argon, execution proceeds via `/teamwork` by default. `agy_task.py` automatically
     prepends `/teamwork` if omitted. If the user explicitly asks for direct single-agent execution, pass `--single-agent`.

3. **Run** in a fresh or existing work directory. Provide inputs explicitly.
   ```
   # Single-agent delegation
   python scripts/agy_task.py run -w ./work/slugify -t "Create utils.py with slugify(text) ... and test_utils.py with 3 pytest tests" --verify "python -m pytest -q"

   # Multi-agent /teamwork delegation with Argon (default for Argon)
   python scripts/agy_task.py --model gemini-4-argon-high run -w ./work/feature -t "/teamwork Build auth service with JWT RS256 token verification" --verify "python -m pytest -q"
   ```
   - `-w DIR` work directory (default: `./agy_tasks/<stamp>`). Use the project subfolder the change belongs to.
   - `-i PATH` (repeatable) copies files/dirs into the work directory before the run.
   - `--verify CMD` runs locally afterwards (tests, a smoke command); a non-zero rc marks the run `verify-failed`.
   - `--collect DIR` copies created/modified files into another directory.
   - `--effort low|medium|high` as needed. The model is a global option and goes before the
     subcommand: `python scripts/agy_task.py --model <slug> run ...` (`agy models` lists slugs; aliases `argon`, `아르곤` supported).
   - `--timeout` defaults to `auto`: single agent 20m, `/teamwork` 60m, deep research 90m; ×1.5 for medium and
     ×2 for high effort; +1m per 50 KB of `--input` (max +30m). Measured Argon-low `/teamwork` reviews took more
     than 15 minutes each, so leave it on `auto` unless the user gives a limit; pass e.g. `--timeout 2h` to extend.
     The chosen value is in the JSON as `timeout: {value, source}`.
   - `--single-agent` overrides the Argon default to `/teamwork`, running as a single agent.
   - `--dry-run` shows the exact agy command and framed prompt without running (use it to check a spec).
   - Use `python3` on macOS/Linux, `python` on Windows. `--verify` runs in the platform shell.
   - `--allow-all` lets the agent run shell commands (agy's `--dangerously-skip-permissions`). A program it
     runs can read any file, so it needs `--no-confine` too (otherwise exit 5); use both only in a throwaway
     directory and only when the task genuinely needs to run things.
   - `--deep-research` runs under the deep-research confinement profile: automatically passes `--dangerously-skip-permissions`
     to `agy` while keeping confinement active (`AGY_CONFINE_MODE="deep-research"`), sets `DEEP_RESEARCH_FRAME` and
     `DEEP_RESEARCH_SCHEMA`, and merges `<workdir>/relay/result.json` or `started.json` under `summary["deep_research"]`.
     Cannot be combined with `--no-confine`.
   - `--dr-skill-dir PATH` specifies the `local-gemini-deep-research` skill directory (default:
     `~/.gemini/config/skills/local-gemini-deep-research` or `AGY_CONFINE_DR_DIR`).
   - `--inlined` and `--bare` are for callers such as agy-outsource: see README.

   **Independent tasks run in parallel as a batch.** When a job splits into parts that do not need each other's
   output (review dimensions such as bugs / tests / docs / requirement coverage, or modules that touch different
   files), give each part its own task and run them with `batch`: one agy session and one work directory per
   task, at most `--parallel` (default 3) at a time. Parts where one consumes another's result stay sequential
   `run` calls.
   ```
   # manifest.json: [{"name": "bugs", "task": "/teamwork Review ... write review.md", "inputs": ["src"]},
   #                 {"name": "acceptance", "task_file": "accept.md", "inputs": ["README.md", "docs"]}]
   python scripts/agy_task.py --model gemini-4-argon-low batch -m manifest.json -w ./work/review --effort low
   ```
   Task keys: `name` (required, unique), `task` or `task_file`, and optionally `inputs`, `workdir`, `model`,
   `effort`, `timeout`, `verify`, `collect`, `single_agent`, `bare`, `no_schema`, `inlined`, `agent`, `dry_run`.
   The batch prints `{status: ok|partial|failed, elapsed_sec, tasks: [{name, status, workdir, log, result}]}`
   and saves it as `<batch dir>/batch_result.json`; each `result` is that task's normal run JSON.

4. **Read the JSON on stdout** and report to the user: `status`, `changes.created/modified/deleted`,
   `structured.summary` / `structured.notes`, `deep_research` (in deep-research mode), `verify.rc` and tail.
   Then review the produced code yourself before handing it over; agy's output is a draft, not a merge.
   `status: timeout-partial` means agy hit the print timeout and the files in `changes` may be incomplete.
   Next steps, in this order:
   1. Open every file in `changes.created` / `changes.modified` and tell the user what is finished and what is missing.
   2. `resume <conversation_id> -w <same workdir> -t "Finish <the missing parts> in <file>"`.
   3. Only if the resume also times out, re-run with a longer `--timeout` (e.g. `2h`).
   Exit codes: 0 ok, 2 agent status not SUCCESS, verify failed, timeout-partial, or a batch with some failed
   tasks, 3 agy error (also agy could not start,
   e.g. a prompt over the Windows command-line limit of 32,767 characters: use a shorter task), 4 preflight,
   5 bad arguments (`--allow-all` without `--no-confine`, `--deep-research` with `--no-confine`, an `--inlined`
   path that is not work-dir-relative, `resume` confined to `~` or a drive root).

5. **Iterate** with the same conversation when a fix is small:
   ```
   python scripts/agy_task.py resume <conversation_id> -w ./work/slugify -t "Also handle unicode letters"
   ```

## Rules

- **Argon /teamwork Default Rule**: When delegating with the Argon model (canonical slugs `gemini-4-argon-high`,
  `gemini-4-argon-medium`, `gemini-4-argon-low`, or aliases `argon`, `아르곤`, `gemini-4-argon`), delegation MUST
  proceed via `/teamwork` by default. Claude must structure the task with `/teamwork <objective and instructions>`
  (e.g. `python scripts/agy_task.py --model gemini-4-argon-high run -w ... -t "/teamwork <objective>"`), unless the user
  explicitly requests direct single-agent execution (e.g. "single-agent only", "팀워크 없이 단일 에이전트로", or passes
  `--single-agent`).
- **Slash Command Leading Token Requirement**: Antigravity's slash command processor strictly requires `/teamwork` (or
  any slash command) to appear as the very first token (index 0) of the prompt payload. Do not prepend conversational text,
  greetings, or wrapper prose before `/teamwork`. `scripts/agy_task.py` automatically ensures leading slash commands
  remain at index 0.
- One task per run, one work directory per task. Never point `-w` at `~`, a drive root, or an
  unrelated repo; the agent gets write access to the whole directory.
- Do not pass `--allow-all` on the user's real project tree unless they asked for it.
- Do not edit `~/.gemini/antigravity-cli/settings.json`. With confinement on (the default) agy runs no
  shell command at all, whatever settings.json allows: use `--verify` to run tests. A `permissions.allow`
  rule (e.g. `"command(regex:python -m pytest.*)"`) only matters with `--no-confine`; tell the user the
  rule to add instead of adding it.
- Everything the agent should see must be inside the work directory (`-i`) or in the prompt — with
  confinement on it cannot read anything else.
- agy is **confined to the work directory** by default (do not pass `--no-confine` unless the user asked):
  a hook denies paths outside it (any form, links included), every shell, browser and MCP tool, and writes into
  `.agents/` (except `/teamwork`'s `.agents/teamwork/**`) or `.agy_task/`. For `/teamwork` it also allows agy's
  scratch folder of the run's own conversation and `brain/<id>/teamwork_prompt_<that conversation>.md` (the first
  conversation used is pinned; others stay denied), and reading `~/.gemini/config/plugins/**`. `ask_question` is
  refused with "proceed with reasonable defaults and state your assumptions" (nobody answers a headless run).
  `confinement.enabled: false` means the hook did not see every tool call: the run
  was **not** confined; say so. Report a non-zero `confinement.denied_count` (check `denied[].rule` / `reason`).
  Details: README.md "Permissions model".
- Under `--deep-research` confinement profile:
  - **Read tools**: permitted inside `<workdir>` and `AGY_CONFINE_DR_DIR`. Reading outside both is denied (`rule: "path"`).
  - **Write tools**: permitted only inside `<workdir>`. Writing to `AGY_CONFINE_DR_DIR` is strictly denied (`rule: "path"`).
    Caller control files inside `<workdir>` (`relay/request.json`, `prompt.md`, `goal.md`, `feedback_*.md`,
    `relay/result.json`, `relay/started.json`) are read-only to file tools (`rule: "protected"`).
  - **Shell tools**: `run_command` is permitted only for clean Python execution of `relay_step.py --job <workdir>`
    or `patch_gemini_app.py`. Shell chaining and redirection (`&&`, `;`, `|`, `>`, `<`, newlines, `` ` ``, `$()`)
    and all other commands/scripts are blocked (`rule: "shell"`).
  - **Browser & MCP tools**: unconditionally denied (`rule: "tool"`).

## Why these flags

`--add-dir <workdir>` is mandatory: an untrusted cwd makes agy write into its own scratch folder
(`~/.gemini/antigravity-cli/scratch`) instead of the task directory. `--mode accept-edits` auto-approves
file edits so headless runs do not stall; `--json-schema` yields a `structured_output` with summary and
file list; `stream-json` gives live progress on stderr.

## Artifacts per run

```
<workdir>/.agy_task/<stamp>/
  prompt.md       exact prompt sent
  schema.json     structured-output schema
  events.jsonl    raw stream-json events
  stderr.txt      agy stderr (if any)
  result.json     the JSON printed on stdout
  confine.jsonl   one line per tool call: tool, decision (ask/deny), rule, reason, paths

<workdir>/relay/  (under --deep-research)
  request.json    caller step request
  started.json    detached worker process record
  result.json     step result data
  progress.jsonl  streamed execution progress log
```
