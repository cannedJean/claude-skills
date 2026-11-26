# agy-deep-research

A Claude Code skill that runs Google Gemini Deep Research without an API key. Claude orchestrates; the Antigravity CLI (`agy`) operates the locally installed Windows Gemini desktop app over a local debugging port; the user's own Gemini subscription does the research. Claude reviews Gemini's research plan against acceptance criteria before any quota is spent, and checks the finished report against the same criteria.

By default the research runs in a visible Chrome window on a dedicated profile at gemini.google.com (`--browser chrome`, `scripts/chrome_launcher.py`); nothing installed is modified, and the user signs in to Google in that window once.

**App mode (`--browser app`) modifies the installed Gemini app.** To open the debugging port it patches a startup check in the app's `app.asar` (a backup `app.asar.bak` is kept; `python scripts/patch_gemini_app.py --restore` reverts it; an app update removes the patch). See [references/architecture.md](references/architecture.md).

## Files

| Path | For | Content |
|---|---|---|
| `SKILL.md` | Claude | The procedure: consent, preflight, prompt and goal, plan review, approval, report review |
| `references/prompt-spec.md` | Claude | 5-block XML prompt specification |
| `references/cli.md` | Claude, humans | All subcommands and options, model and effort settings, environment variables |
| `references/gates-and-exit-codes.md` | Claude, humans | Every gate, exit code, and the conversation-identity recovery steps |
| `references/troubleshooting.md` | Claude, humans | Symptoms and fixes |
| `references/app-mode.md` | Claude | Consent and failures for `--browser app` jobs |
| `references/architecture.md` | humans | Layers, patch internals, confinement, job directory layout |
| `references/SKILL.md` | **agy** | Not a reference: the body of the agy-side skill `local-gemini-deep-research`. `setup` copies it to `~/.gemini/config/skills/local-gemini-deep-research/SKILL.md`, and `preflight` compares its hash. Its path is fixed by `BUNDLED_REF_SKILL` in `deep_research.py` |
| `prompts/agy_relay_task.md` | agy | Task template for one relay step |
| `prompts/plan_review_guide.md` | Claude | What to look for when reviewing a plan |
| `scripts/` | | `deep_research.py` (orchestrator, stdlib only), `relay_step.py`, `run_deep_research.py` (need playwright), `chrome_launcher.py`, `patch_gemini_app.py` |

## Requirements

- Windows 10 or 11 (x64) with Google Chrome (Chrome mode, default) and a Google account with Deep Research access.
- For app mode only: the Google Gemini desktop app installed under `%LOCALAPPDATA%\Google\Gemini\app-*` and signed in to an account with Deep Research access. The patch pattern is tested against app versions 1.16 to 1.19 (`scripts/tests/test_patch_pattern.py`); on 2026-11-25 a read-only `--dry-run` also matched 1.21.1. Every app update installs a fresh `app-<version>` directory without the patch, so the patch is reapplied after each update, and a changed guard statement (`ASAR_PATTERN_MISMATCH`) has to be fixed by hand in `SECURITY_GUARD_RE`.
- Python 3.9 or newer (developed and tested on 3.12).
- `python -m pip install --user playwright`. Only the client library is needed; it connects to the running app, no browser download.
- Antigravity CLI (`agy`) signed in, and the sibling skill `agy-delegate` (`../agy-delegate/scripts/agy_task.py`).

## Setup

```bash
python scripts/deep_research.py setup       # copy relay scripts and the agy-side SKILL.md to the agy skill directory
python scripts/deep_research.py preflight   # agy, playwright, app install, debugging port, sync status
python scripts/patch_gemini_app.py --check  # read-only: is the app patched and reachable
```

## Tests

Everything runs offline against a simulator (`scripts/tests/fake_agy_dr.py`); no Gemini app, network, or quota is used.

```bash
python -m unittest discover -s scripts/tests
```

`scripts/tests/test_docs_consistency.py` keeps the documentation honest: numbers quoted in the docs must equal the constants in `deep_research.py`, example commands must parse with the real argument parser, and `SKILL.md` must stay within its line budget. `scripts/tests/run_verification.py` and `rubric_assessment.py` are standalone audit scripts (syntax, `--help`, frontmatter, port consistency, a scored rubric); they are not part of the unittest run.

Tests of the confinement hook live in `../agy-delegate/scripts/tests`.
