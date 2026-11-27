# Resume prompt — paste into a new Claude Code session

Paste everything below the line into a new Claude Code session opened at the repository root (branch `feat/agy-deep-research-uia`).

---

You are the lead engineer continuing the rewrite of the skill `skills/agy-deep-research`: its Gemini driver is being moved from CDP/Playwright to Windows UI Automation (COM, `comtypes`) on the unmodified Gemini desktop app, running on a hidden Windows desktop with a dedicated profile. Most of the work is done; you finish it.

## Read first, in this order
1. `docs/superpowers/handoff/2026-11-27-agy-deep-research-uia-handoff.md` — state, decisions, remaining work, open review items, incidents. It is the source of truth for where things stand.
2. `docs/superpowers/specs/2026-11-26-agy-deep-research-uia-design.md` — the design. §23 and §21 override earlier sections.
3. `docs/superpowers/plans/2026-11-27-agy-deep-research-uia-research.md` — plan 3 (R1–R3); R3 is the next feature task.
4. Then the code it points to, under `skills/agy-deep-research/scripts/` and `skills/agy-delegate/scripts/confine_hook.py`.

If a folder named in the handoff's "Optional material" exists on this PC, you may read it; never require it.

## Rules — no exceptions
- **Never run the real Gemini app, a browser, an app patcher, or anything with `--remote-debugging-port`. Never write anything under `%LOCALAPPDATA%\Google\Gemini\`.** A test once patched the real app; the user had to reinstall it. Tests run only with fakes, and every test file imports `tests/sandbox.py` first. `run_verification.py` enforces this — keep it that way.
- **Never send anything to Gemini** (prompts, edits, research) without a live budget the user approved in this session. The budget is zero now.
- Never put conversation URLs, account names/e-mails, or local user paths (`C:\Users\<name>\…`) in commits, docs, logs or agy inputs. Use placeholders. Scan every input before it goes to agy.
- Never open, read or log the user's other Gemini conversations (the sidebar is off-limits; `privacy.allowed_ids` enforces it).
- Never push, open a PR, edit `~/.claude/skills` or `~/.gemini`, or install into system Python without the user's approval. Commit locally.
- Every subagent brief you write repeats these rules verbatim as a SAFETY block and says: fakes only, never run the app, drivers (`deep_research.py`, `relay_step.py`, `run_deep_research.py`), `fg_tracer.py` or agy as real processes.

## How to work
- Use superpowers skills: `subagent-driven-development` (implementer subagent → task-review subagent → fix loop → scoped re-review, ledger per plan), `test-driven-development` (tests first, see them fail), `receiving-code-review` (verify every finding against the code before acting), `writing-skills` for the docs rewrite, `verification-before-completion` before you claim anything is done.
- Implementation and task reviews: Claude subagents. Run independent tasks in parallel only when they touch disjoint files.
- Cross-validation at a milestone (X2): `python <agy-delegate skill>/scripts/agy_task.py --model gemini-4-neon-high run … --effort high`, one work dir per task under the git-ignored `agy_tasks/`, built with `git archive HEAD`. Judge agy output by diff and tests, never by its summary. If agy is blocked by quota or errors, stop and tell the user; do not swap the cross-validator.
- Tests: `python -m unittest discover -s tests` from `skills/agy-deep-research/scripts`; `python scripts/tests/run_verification.py` from `skills/agy-deep-research` (must be 7/7); agy-delegate with system Python: `py -3 -m unittest discover -s skills/agy-delegate/scripts/tests`.

## First task
Close the open review items listed in handoff §2 and §3 step 1 (R1 `expect_cards` semantics; the B5 item named there). Then R3, then M3 X2, then the docs rewrite — the order in handoff §3.

## Stop and report to the user (short report: status, what needs their decision, evidence, next step)
- **G4:** when everything up to the docs rewrite is done and a full E2E through the agy relay is the next step — state the exact live budget you need (plan sends, edits, research runs) and wait.
- **G5:** before a PR or replacing the installed copies.
- **G-X, immediately:** anything written or changed under the app folder, unmasked account or conversation data in any log or file, our Gemini window appearing on the user's desktop, any budget overrun, or a finding that changes the design.
