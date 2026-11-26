# Troubleshooting

Read this when a step fails with an environment, UI, or timeout symptom. Gate rejections and conversation-identity recovery are in gates-and-exit-codes.md.

| Symptom | Cause | Fix |
|---|---|---|
| `preflight` exits 4 | agy missing or signed out, playwright missing, or bundled files missing | Read `checks` in the output. `python -m pip install --user playwright` for playwright; `agy_task.py preflight` for agy |
| `sync_status` is `out_of_sync` / `missing_target` | The agy-side copy of the relay scripts differs from this skill | `python scripts/deep_research.py setup` |
| `CHROME_NOT_FOUND` | Chrome mode, but no chrome.exe | The user installs Google Chrome, or set `GEMINI_DR_CHROME_PATH`; app mode is the alternative (app-mode.md) |
| `CHROME_CDP_NOT_READY` | A window of the dedicated profile was opened without the debugging port (for example by hand), so a second launch cannot add it | Ask the user to close every window of that profile, then repeat the step |
| `GEMINI_PAGE_NOT_FOUND` / `UI_SELECTOR_TIMEOUT` in Chrome mode | The Chrome profile is not signed in to Google, or the account has no Deep Research | Ask the user to sign in in the opened window (`python scripts/chrome_launcher.py` reopens it). Never sign in for them |
| The Chrome window was closed between steps | | Nothing to do: the next step (and the relay's probe before `approve` / `collect`) reopens it |
| `CDP_NOT_READY` / `CDP_CONNECT_FAILED` (app mode) | The app is not running with the debugging port | `python scripts/patch_gemini_app.py --check`. If `patched` is false, get the user's consent (app-mode.md) before anything else |
| `ASAR_PATTERN_MISMATCH`, or preflight `patch.pattern_match: false` | An app update changed the guard statement the patch looks for. After a step the app is stopped and not relaunched | Tell the user (and to start Gemini by hand if a step already ran). `python scripts/patch_gemini_app.py --dry-run` prints `context_snippets`: the code around `"security"`, `isPackaged` and `.app.exit(1)`, so the new shape is visible without unpacking the archive. Fixing it means a human edits `SECURITY_GUARD_RE` and adds the new identifiers as a case in `scripts/tests/test_patch_pattern.py`. Do not retry and do not loosen the regex on your own |
| preflight `patch.update_needs_repatch: true` | The app updated since the last patch (`last_patched_version` → `app_version`); the new `app-<version>` directory is stock | Get consent again (app-mode.md). The next step re-patches |
| `GEMINI_PAGE_NOT_FOUND` / `UI_SELECTOR_TIMEOUT` in app mode | The app is not signed in to a Google account | Ask the user to sign in: with their consent run `python scripts/patch_gemini_app.py --show` so the window is visible. Never sign in for them |
| `PLAN_TIMEOUT` | Gemini answered with prose or a clarifying question instead of a plan card, or was slow past the deadline | Make the prompt a multi-dimensional research task (prompt-spec.md), not a lookup or a question. The message says how long it waited; if Gemini was still generating, repeat with a larger `--deadline` |
| `SAFETY_BLOCKED` | Gemini's content filter refused the prompt | Tell the user; agree on new wording with them, then start a new job. Do not silently change the topic |
| `PLAN_CHANGED` / `CONFIRM_MISSING` on `approve` | The plan card count differs from what was reviewed, or the start button is gone | Open the conversation url from `status`. If the plan changed, review it again in a new job |
| `COLLECT_WOULD_START` | `collect` found a plan that was never approved | Run `approve`; `collect` never starts a research |
| `STREAM_TIMEOUT` / `REPORT_TIMEOUT` | The research outlived `--timeout` | It keeps running in the app. `status`, then `collect` later |
| `WORKER_DIED` | The detached worker exited without a result | `collect` re-attaches through the job's recorded conversation |
| `--effort is not supported for model` from agy | A raw friendly model name reached `agy.exe` | Pass the model through `deep_research.py` (it normalizes), or use the slug `gemini-3.8-flash` |
| Log shows `[MODEL_OBSERVED] … Warning` / composer shows `Flash` after `approve` or `collect` | A page reload reset the composer; the model was locked at plan time | No action. The model is applied only in `plan` / `auto` |
| Mode button reads a lower thinking level than requested | The app folds the thinking level into the model-mode menu | The driver switches it through the menu and logs `Switched thinking level to: …` or `WARNING: thinking level is …`. Check the plan log for one of the two lines |
| Log says `no thinking level offered`, or the Argon button reads only `Argon` | Some modes show no level on the button | The driver reads the checked menu item (log line ends with `(menu)`). To request a level with `--gemini-model argon`, set `GEMINI_DR_THINKING` on every command |
| Port conflict on the CDP port | Another process holds it | Set `GEMINI_CDP_PORT` for every command, including the patcher |
| The user wants the app back to stock | | `python scripts/patch_gemini_app.py --restore` copies `app.asar.bak` back. The skill stops working until the app is patched again |
