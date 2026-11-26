# App mode (`--browser app`): the patched Gemini desktop app

Read this only when the user asked for the Windows Gemini desktop app instead of the Chrome window. New jobs use Chrome mode unless `init` / `auto` gets `--browser app`; jobs created before the option existed stay in app mode.

App mode modifies the installed app: it patches a startup guard in `app.asar` so the app opens a debugging port (9223). Every app update installs a fresh, unpatched `app-<version>` directory, and when the guard's code changes shape the patch fails until someone edits `SECURITY_GUARD_RE` by hand (details in architecture.md). Prefer Chrome mode unless the user has a reason.

## Consent, before the first `plan` of an app-mode job

Run `$DR preflight` and read `checks.gemini_app.patch`:

- `pattern_match: false`: this app version cannot be patched (`ASAR_PATTERN_MISMATCH`), and a step would close Gemini without relaunching it. Stop and tell the user; see troubleshooting.md. Chrome mode still works.
- `patched: false`: stop and tell the user, then wait for a yes. Say it is an app update when `update_needs_repatch` is true (name `last_patched_version` → `app_version`), otherwise a first run:
  1. The skill modifies the installed Gemini app's `app.asar` so it opens a local debugging port (9223) that the automation connects to.
  2. A backup `app.asar.bak` is made first; `patch_gemini_app.py --restore` puts the original back.
  3. After each Gemini app update the patch has to be applied again.
  Also say a running Gemini window will be closed and restarted hidden. If the user declines, offer Chrome mode.
- `patched: true`: continue without asking.

Do not run `patch_gemini_app.py` yourself without `--check` or `--dry-run`; the first `plan` applies the patch. In Chrome mode the patcher refuses to patch (`refused_chrome_mode`).

## App-mode failures

| Code | Meaning | Do |
|---|---|---|
| `CDP_NOT_READY` / `CDP_CONNECT_FAILED` | App not running with the debugging port | `python scripts/patch_gemini_app.py --check`; if unpatched, consent above |
| `ASAR_PATTERN_MISMATCH` | An app update reshaped the guard | Tell the user (Gemini stays closed); troubleshooting.md. Do not retry |
| `APP_NOT_INSTALLED` | No `app-*` directory | Use Chrome mode, or the user installs the app |
| `GEMINI_PAGE_NOT_FOUND` / `UI_SELECTOR_TIMEOUT` | App not signed in | With consent, `python scripts/patch_gemini_app.py --show` so the user can sign in |

To put the app back to stock: `python scripts/patch_gemini_app.py --restore`.
