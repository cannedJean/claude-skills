# Architecture, app patch internals, confinement, job layout

Read this when you debug the driver/relay/patcher, need to explain what the app patch changes, or need the job directory and relay file layout. Not needed to run a research.

## Layers

```
Claude Code (lead orchestrator & plan/report reviewer)
  │
  ├─ 1. Orchestration (scripts/deep_research.py — stdlib only)
  │     subcommands preflight, setup, compose-prompt, init, plan, edit, approve, auto, status, collect
  │     enforces the gates in gates-and-exit-codes.md
  │
  ├─ 2. Operator relay (skills/agy-delegate/scripts/agy_task.py run --deep-research → scripts/relay_step.py)
  │     agy runs headlessly under the PreToolUse confinement hook (AGY_CONFINE_MODE="deep-research")
  │     relay_step.py turns <job>/relay/request.json into ONE driver step, writes relay/result.json
  │     approve/auto/collect spawn a detached worker (mode: "detach") that streams relay/progress.jsonl
  │
  ├─ 3. CDP automation (scripts/run_deep_research.py — needs playwright)
  │     connects to http://127.0.0.1:9223, selects model + Deep Research tool (EN/KO selectors)
  │     cancels the auto-start countdown ([data-test-id="cancel-fuse-button"]) so no plan runs unreviewed
  │     extracts plan cards, injects edit feedback, streams thoughts, converts the report DOM to GFM Markdown
  │     binds the step's conversation (/app/<id>) and prints [CONVERSATION] / [REPORT_PROVENANCE]
  │
  ├─ 4a. Chrome launcher (scripts/chrome_launcher.py) — default, `--browser chrome`
  │     opens a visible Chrome window on its own profile with --remote-debugging-port=9224 at gemini.google.com
  │
  └─ 4b. Desktop launcher & patcher (scripts/patch_gemini_app.py) — `--browser app`
        patches %LOCALAPPDATA%\Google\Gemini\app-*\resources\app.asar, launches Gemini.exe hidden via WMI
```

The agy side has its own skill, `local-gemini-deep-research` (`~/.gemini/config/skills/local-gemini-deep-research`). `deep_research.py setup` copies `scripts/relay_step.py`, `scripts/run_deep_research.py`, `scripts/patch_gemini_app.py` and `references/SKILL.md` (that skill's body) there; `preflight` compares SHA-256 hashes and reports `sync_status`.

## Chrome mode

Chrome accepts `--remote-debugging-port` when it runs on a non-default `--user-data-dir` (since Chrome 136 the switch is ignored on the default profile), so no installed program is modified and app updates do not matter. `chrome_launcher.py` starts `chrome.exe` through WMI (it outlives the relay worker) with that profile, port 9224, `--disable-renderer-backgrounding`, `--disable-background-timer-throttling`, `--disable-backgrounding-occluded-windows`, and https://gemini.google.com/app. A running instance on the port is reused. The driver connects to the first gemini.google.com tab, or opens one when the user closed it; the relay reopens the window before its read-only probe. The Conversation Identity Gate works unchanged: conversation urls are the same `gemini.google.com/app/<id>` as in the app.

The user's everyday Chrome profile is never used. The dedicated profile holds the Google sign-in; while the window is open, any local program can drive it through the port (the same exposure the patched app has on 9223).

In Chrome mode `patch_gemini_app.py` refuses to patch (`refused_chrome_mode`, `GEMINI_DR_BROWSER=chrome` in its environment), and the agy relay task tells agy not to run it.

## What the app patch changes

Packaged Electron apps exit when they see an unauthorized command-line switch:

```javascript
var Pg=wg(h.app.isPackaged&&P!=="dev",process.argv,process.execArgv);
Pg?(c.error("security",_g,`(switch: --${Pg})`),Dt(),h.app.exit(1)):Rb();
```

`patch_gemini_app.py`:

1. Copies `app.asar` to `app.asar.bak` (once), then rewrites the guard statement so the app appends `remote-debugging-port=9223` and `disable-renderer-backgrounding` itself and continues startup.
2. Finds the statement with `SECURITY_GUARD_RE`. Every identifier group is `[\w$]+` because minified names change per release and may contain `$` (`m.app` in 1.16.x, `h.app` in 1.17+, `$m` logger argument in 1.19.0). If the statement itself changes shape the patch fails with `ASAR_PATTERN_MISMATCH`: update `SECURITY_GUARD_RE` and `scripts/tests/test_patch_pattern.py`.
3. Keeps the exact byte length by padding the replacement with a trailing comment (`/*    */`), so ASAR header offsets stay valid.
4. After writing, it records the patched version in the patch state file (`%LOCALAPPDATA%\agy-deep-research\patch_state.json`, outside the app directory that updates replace), which lets `preflight` tell an update from a first run. On a mismatch `--dry-run` returns `context_snippets` around the guard's anchors.
5. `--check` and `--dry-run` never write; `--restore` copies `app.asar.bak` back. An app update installs a fresh `app-*` directory, which is unpatched until the patch runs again.

**Updates.** The guard is a deliberate vendor block: its switch list includes `remote-debugging-port`, `remote-debugging-pipe`, `inspect`, `proxy-server` and `load-extension`, so no command-line-only route exists. Each update ships a new `app-<version>` directory, unpatched. Re-patching is automatic (next paragraph) but unannounced; a reshaped guard statement stops the skill until someone edits `SECURITY_GUARD_RE` and its test by hand. If the vendor enables Electron's ASAR integrity fuse, this patching approach stops working altogether.

**Who triggers the patch.** `run_deep_research.py` calls `ensure_gemini_running_with_cdp()` at the start of every step: when CDP is not alive or the archive is unpatched it stops running `Gemini.exe` processes, patches, and relaunches hidden. The relay task template (`prompts/agy_relay_task.md`) also lets agy run `patch_gemini_app.py` once after `CDP_NOT_READY` / `CDP_CONNECT_FAILED`. Neither path asks the user, which is why SKILL.md makes Claude run `--check` and get consent before the first `plan` / `auto`.

**Launch and throttling.** `Gemini.exe --hide` is started through WMI `Win32_Process.Create`, so no window or taskbar entry steals focus (`--show` launches visibly, used for the first sign-in). Chromium throttles hidden windows; the patched switch `--disable-renderer-backgrounding` plus CDP `Page.setWebLifecycleState("active")` and `Emulation.setFocusEmulationEnabled` keep the research running at full speed.

**Port.** 9223 everywhere (avoids Chrome's default debugging port); override with `GEMINI_CDP_PORT`.

**Driver resilience.** `is_generation_in_progress` detects the stop button / thinking overlay so a long thinking phase is never interrupted by a reload; prompts are sent with `send_btn.click(force=True)` and a keyboard `Enter` fallback (Angular/Quill ignores some synthetic clicks).

## Confinement of the agy operator

`agy_task.py run --deep-research -w <job_dir>` runs agy under `confine_hook.py`:

- **Reads**: only `<job_dir>` and the deep-research skill dir (`AGY_CONFINE_DR_DIR`). Elsewhere denied (`rule: "path"`).
- **Writes**: only `<job_dir>`. Caller control files (`relay/request.json`, `prompt.md`, `goal.md`, `feedback_*.md`, `relay/result.json`, `relay/started.json`) are denied (`rule: "protected"`).
- **Shell**: only a plain Python invocation of `relay_step.py --job <job_dir>` or `patch_gemini_app.py`, with Python flags from `-u -B -E -s -S -O -OO`. Chaining, redirection, backticks, subshells, `python -c` / `-m` are denied (`rule: "shell"`).
- **Browser and MCP tools**: denied (`rule: "tool"`).

This is why a prompt must be self-contained: neither agy nor Gemini can read the user's repository.

## Job directory

Default root: `%LOCALAPPDATA%\agy-deep-research\jobs\<job_id>` (Windows), `~/.cache/agy-deep-research/jobs/<job_id>` (POSIX), or `$AGY_DR_JOBS/<job_id>`.

```
<job_dir>/
  job.json              orchestrator state: status, seq, url, plan count, resolved models, goal_sha256, rejections
  prompt.md             exact research prompt
  goal.md               fixed acceptance criteria
  plan_v1.md, plan_v2.md
  feedback_2.md         edit feedback for plan_v2
  approval.md           recorded approval reason
  report.md             accepted report
  relay/
    request.json        step request written by deep_research.py
    started.json        detached worker pid
    progress.jsonl      {"ts", "elapsed_s", "text"} per line
    result.json         step result written by relay_step.py
    conversation.json   the conversation the last relay run bound
    report_<seq>.md     staged report; promoted to --out / report.md only after the gates
    rejected_<seq>.*    report/result/progress refused by the Conversation Identity Gate
```

`<out>.meta.json` next to the report records prompt, goal, `goal_sha256`, approved plan, approval reason, conversation, report statistics, `report_warnings`, and the resolved models. The `request.json` / `result.json` field lists are in `references/SKILL.md` (relay mode section).

## How model settings travel

`deep_research.py` → `agy_task.py --model <slug> run --effort <effort> --deep-research` → `relay/request.json` (`gemini_model`, `gemini_thinking`, `env.GEMINI_DR_MODE`, `env.GEMINI_DR_THINKING`) → `run_deep_research.py` `ensure_pro_mode` selects the mode and thinking level in the app UI → resolved values are stored in `job.json` and `<out>.meta.json`.
