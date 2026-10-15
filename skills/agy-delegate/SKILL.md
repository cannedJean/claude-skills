---
name: agy-delegate
description: Delegate a small, self-contained coding task to the Antigravity CLI (`agy`, Google's terminal coding agent) in headless mode and collect the files it produced. Use when the user says things like "이건 agy한테 시켜", "Antigravity로 만들어와", "delegate this to agy", or wants a bounded coding chore (write a script/module/tests/boilerplate/refactor of a few files) done by a second agent and the outputs brought back. Not for tasks that need interactive approval, credentials, or multi-repo changes.
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

3. **Run** in a fresh or existing work directory. Provide inputs explicitly.
   ```
   python scripts/agy_task.py run -w ./work/slugify -t "Create utils.py with slugify(text) ... and test_utils.py with 3 pytest tests" --verify "python -m pytest -q"
   ```
   - `-w DIR` work directory (default: `./agy_tasks/<stamp>`). Use the project subfolder the change belongs to.
   - `-i PATH` (repeatable) copies files/dirs into the work directory before the run.
   - `--verify CMD` runs locally afterwards (tests, a smoke command); a non-zero rc marks the run `verify-failed`.
   - `--collect DIR` copies created/modified files into another directory.
   - `--effort low|medium|high`, `--timeout 10m` as needed. The model is a global option and goes before the
     subcommand: `python scripts/agy_task.py --model <slug> run ...` (`agy models` lists slugs).
   - `--dry-run` shows the exact agy command and framed prompt without running (use it to check a spec).
   - Use `python3` on macOS/Linux, `python` on Windows. `--verify` runs in the platform shell.
   - `--allow-all` lets the agent run shell commands (agy's `--dangerously-skip-permissions`). A program it
     runs can read any file, so it needs `--no-confine` too (otherwise exit 5); use both only in a throwaway
     directory and only when the task genuinely needs to run things.
   - `--inlined` and `--bare` are for callers such as agy-outsource: see README.

4. **Read the JSON on stdout** and report to the user: `status`, `changes.created/modified/deleted`,
   `structured.summary` / `structured.notes`, `verify.rc` and tail. Then review the produced code
   yourself before handing it over; agy's output is a draft, not a merge.
   Exit codes: 0 ok, 2 agent status not SUCCESS or verify failed, 3 agy error (also agy could not start,
   e.g. a prompt over the Windows command-line limit of 32,767 characters: use a shorter task), 4 preflight,
   5 bad arguments (`--allow-all` without `--no-confine`, an `--inlined` path that is not work-dir-relative,
   `resume` confined to `~` or a drive root).

5. **Iterate** with the same conversation when a fix is small:
   ```
   python scripts/agy_task.py resume <conversation_id> -w ./work/slugify -t "Also handle unicode letters"
   ```

## Rules

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
  `.agents/` or `.agy_task/`. `confinement.enabled: false` means the hook did not see every tool call: the run
  was **not** confined; say so. Report a non-zero `confinement.denied_count` (check `denied[].rule` / `reason`).
  Details: README.md "Permissions model".

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
```
