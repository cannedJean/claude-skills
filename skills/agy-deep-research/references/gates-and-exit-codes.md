# Gates, exit codes, conversation identity and recovery

Read this when a command exits non-zero with a code SKILL.md does not cover, or after `CONVERSATION_MISMATCH` / `REPORT_PROVENANCE_MISSING` when you need the exact recovery steps. Thresholds are the constants at the top of `scripts/deep_research.py`.

Every failure prints JSON with `status: "error"`, `exit_code`, `code`, `message`, and often `hint`. Follow the `hint` first.

## Exit codes

| Exit | Meaning | Codes |
|---|---|---|
| 0 | Success, all gates passed | |
| 2 | Gate rejection or research failure | `PRE_PLAN_GATE_PROMPT_MISSING`, `PRE_PLAN_GATE_GOAL_MISSING`, `APPROVAL_GATE_INVALID_STATE`, `APPROVAL_GATE_NO_PLAN`, `EDIT_INVALID_STATE`, `PLAN_TIMEOUT`, `SAFETY_BLOCKED`, `PLAN_CHANGED`, `CONFIRM_MISSING`, `COLLECT_WOULD_START`, `STREAM_TIMEOUT`, `REPORT_TIMEOUT`, `REPORT_FILE_MISSING`, `REPORT_QUALITY_GATE_FAILED`, `REPORT_STRUCTURE_INVALID`, `CONVERSATION_MISMATCH`, `REPORT_PROVENANCE_MISSING`, `NO_CONVERSATION_URL` |
| 3 | Runtime, driver, or environment error | `PLAYWRIGHT_MISSING`, `CHROME_NOT_FOUND`, `CHROME_CDP_NOT_READY`, `GEMINI_PAGE_NOT_FOUND`, `UI_SELECTOR_TIMEOUT`, `CDP_NOT_READY`, `ASAR_PATTERN_MISMATCH`, `WORKER_DIED`, `NO_RELAY_RESULT`, `AGY_TASK_NOT_FOUND` |
| 4 | Preflight failure | |
| 5 | Bad arguments or isolation violation | `GIT_REPO_ESCAPE`, `INVALID_JOB_DIR`, `MISSING_JOB`, `JOB_NOT_FOUND`, `EMPTY_PROMPT`, `EMPTY_GOAL`, `EMPTY_FEEDBACK`, `IDENTICAL_PROMPT_AND_GOAL`, `PROMPT_XML_STRUCTURE_INVALID`, `FEEDBACK_INTERROGATIVE_DENIED`, `APPROVAL_GATE_REASON_TOO_SHORT`, `INVALID_EFFORT`, `INVALID_BROWSER`, `INVALID_CONVERSATION_URL`, `CONVERSATION_URL_CONFLICT` |

## Gates

| Gate | Enforced at | Check | Codes |
|---|---|---|---|
| Job Isolation | every job command | Job directory is not inside a git repository, not the home directory or a drive root | `GIT_REPO_ESCAPE`, `INVALID_JOB_DIR` |
| Dual-File & XML | `init`, `plan`, `auto`, `compose-prompt` | `prompt.md` and `goal.md` differ after whitespace/case normalization; with `--strict-xml` all 5 XML blocks are present. Local-path references and JSON-only requests are reported as `architecture_warnings`, not rejected | `IDENTICAL_PROMPT_AND_GOAL`, `PROMPT_XML_STRUCTURE_INVALID` |
| Sensitive keywords | `init`, `plan`, `edit`, `approve` | Substring match of `SENSITIVE_KEYWORDS` against the prompt. **Warning only** (`warnings` in the output and `job.json`); nothing is blocked | |
| Pre-Plan | `plan`, `edit`, `approve` | `prompt.md` and `goal.md` exist and are non-empty | `PRE_PLAN_GATE_PROMPT_MISSING`, `PRE_PLAN_GATE_GOAL_MISSING` |
| Edit Feedback | `edit` | Feedback has no line ending in `?` and no "why / can you explain / 왜 …인가요" phrasing, unless `--allow-questions` | `FEEDBACK_INTERROGATIVE_DENIED` |
| Plan Approval | `approve` | Status is `plan_ready` or `plan_revised`, a plan is recorded, `--reason` has at least `MIN_REASON_CHARS` characters | `APPROVAL_GATE_INVALID_STATE`, `APPROVAL_GATE_NO_PLAN`, `APPROVAL_GATE_REASON_TOO_SHORT` |
| Worker Liveness | `approve`, `auto`, `collect` | The detached worker pid is alive while waiting for `relay/result.json` | `WORKER_DIED` |
| Conversation Identity | `plan`, `edit`, `approve`, `auto`, `collect` | See below | `CONVERSATION_MISMATCH`, `REPORT_PROVENANCE_MISSING`, `NO_CONVERSATION_URL`, `INVALID_CONVERSATION_URL`, `CONVERSATION_URL_CONFLICT` |
| Report Quality | `approve`, `auto`, `collect` | Report has at least `REPORT_MIN_CHARS` characters and a Markdown heading | `REPORT_QUALITY_GATE_FAILED`, `REPORT_STRUCTURE_INVALID` |
| Report warnings | `approve`, `auto`, `collect` | Accepted reports are flagged, not rejected: `NO_SOURCES` (no source entry or link counted), `REFUSAL_SUSPECTED` (a refusal/error phrase near the top and no sources), `GOAL_CHANGED_SINCE_INIT` (`goal.md` hash differs from the one recorded at `init`). Listed in `report_warnings` of the output, `job.json` and `<out>.meta.json` | |

There is no gate on the number of `edit` rounds, and no code checks the report against `goal.md`: both are Claude's job (SKILL.md steps 4 and 7).

## Conversation Identity Gate

The Gemini app has one page that every driver shares, so another job or the user can move it at any time.

- The driver binds the step's conversation (`/app/<id>`). A new chat owns only the conversation whose single user query is its own prompt.
- The page url is re-checked before and after every poll and the extraction, and the report panel title must match the conversation's research chip.
- The relay accepts a report only when the driver printed `[REPORT_PROVENANCE]` right before the report block, and `deep_research.py` only when that provenance names the conversation in `job.json`. The report is written to the staging file `relay/report_<seq>.md` and copied to `--out` / `report.md` only after this.
- Two jobs never share a conversation: `plan`, `auto` and `collect --url` refuse one that another job's `job.json` names. `edit` / `approve` / `collect` never fall back to whatever conversation is open.
- An `auto` job records its conversation as soon as its new chat is verified (`relay/conversation.json`), so `collect` still reaches the research after `REPORT_TIMEOUT`, `STREAM_TIMEOUT` or `WORKER_DIED`.

**One job drives the app at a time.** A second job's `plan` / `approve` / `collect` moves the shared page; the gate turns that into a refusal instead of a wrong report, and the refused job then needs a later `collect`. Never run `collect` while a detached `approve` worker is alive (two CDP clients can force a navigation, `DRIVER_CRASH`).

Limitation: the report panel is matched by research title. Two concurrent researches with the same title cannot be told apart on the panel (the url checks still apply).

### Recovery after `CONVERSATION_MISMATCH` or `REPORT_PROVENANCE_MISSING`

1. Nothing was saved to the job or to `--out`; an earlier accepted report is untouched. The refused result, staged report and progress are kept as `relay/rejected_<seq>.*` and listed in `job.json` `rejections`.
2. Wait until no other job is driving the app (`status` of the other job). For `REPORT_PROVENANCE_MISSING` run `setup` first: the agy-side script copy is older than this skill.
3. After `approve` / `auto` / `collect`: run `collect` on the same job, without `--url`. It navigates to the job's own conversation.
4. An `approve` refused before the start click leaves the plan at `plan_ready` / `plan_revised`: approve again. (After `approve --no-wait`, the next `collect` reports the refusal and restores that state.)
5. A `plan` or `edit` refused before anything was typed: repeat the step. An `edit` refused after the feedback was typed (`prompt_sent: true`) leaves the job in `error`: look at the conversation in the app and start a new job if the plan changed.

A refused re-`collect` of a completed job keeps the job `completed` with its accepted report. Re-running `init` on an existing job id moves the old `relay/` to `relay.prev-<time>`, so nothing from the earlier generation passes as the new one's.

### Other identity codes

| Code | Cause | Fix |
|---|---|---|
| `NO_CONVERSATION_URL` | The job only ran `init`, or `plan` failed before a conversation was verified | Run `plan`. If the research already exists in the app, `collect --url https://gemini.google.com/app/<id>` binds it once |
| `CONVERSATION_URL_CONFLICT` | `collect --url` names a conversation other than the job's, or one another job owns | Collect the job that owns it, or start a new job |
| `INVALID_CONVERSATION_URL` | `--url` is not a `gemini.google.com/app/<id>` url | Pass the conversation url from the app |
