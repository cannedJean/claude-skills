# claude-skills

Personal [Claude Code](https://claude.com/claude-code) skills. Each skill is a self-contained folder with a
`SKILL.md` (what the agent reads), a `README.md` (setup for humans) and a Python tool under `scripts/`
(stdlib-only unless its README says otherwise) that can also be run by hand or by other agents.

| Skill | What it does | Backend |
|---|---|---|
| [colab-notebook-runner](skills/colab-notebook-runner/) | Run a local `.ipynb` on a Google Colab GPU/TPU runtime and bring back the executed notebook plus everything it wrote to `$COLAB_OUTPUT_DIR` | official `google-colab-cli` (native on macOS/Linux, via WSL on Windows) |
| [agy-delegate](skills/agy-delegate/) | Hand a bounded coding task to the Antigravity CLI (`agy`) headlessly and get back the exact list of files it created/modified, plus an optional local verification result | Antigravity CLI 1.2+ |
| [agy-slide-analyzer](skills/agy-slide-analyzer/) | Transcribe and analyse a folder of lecture slide images slide-by-slide through `agy` (persona + chain-of-thought + few-shot analyst), then verify each result with a deterministic validator and an independent `agy` reviewer; outputs structured JSON plus a QA report | Antigravity CLI 1.2+ via agy-delegate |
| [agy-pdf-analyzer](skills/agy-pdf-analyzer/) | The same analysis for a whole lecture PDF of any length, without converting it to images: the page count is read from the PDF, `agy` views the PDF a few pages per session and writes one structured JSON per page, each checked by the validator and an independent `agy` reviewer | Antigravity CLI 1.2+ via agy-delegate; `pypdf` |
| [kaggle-submit](skills/kaggle-submit/) | End-to-end Kaggle competition submission: first-run `doctor`/`setup` (install CLI, place credentials, verify auth), competition info and rules check, data download, `sample_submission` validation, submit with score polling, local submission log, leaderboard | official `kaggle` CLI (PyPI 2.2.x and git main both supported) |
| [gdrive-bridge](skills/gdrive-bridge/) | Move data bundles and Colab outputs through Google Drive: push a zip into the Drive for Desktop mount, verify a human-made share link for `gdown` (fresh-VM runner path), generate Colab `drive.mount`/`export` cells with manifest verification, and pull results back with sha256 checks | Google Drive for Desktop mount (`G:\` on Windows, `~/Library/CloudStorage` on macOS); stdlib only |

## Install

Copy the skill folders you want into `~/.claude/skills/` (any OS):

```bash
git clone https://github.com/cannedJean/claude-skills.git
cp -r claude-skills/skills/colab-notebook-runner ~/.claude/skills/
cp -r claude-skills/skills/agy-delegate ~/.claude/skills/
cp -r claude-skills/skills/agy-slide-analyzer ~/.claude/skills/
cp -r claude-skills/skills/agy-pdf-analyzer ~/.claude/skills/
cp -r claude-skills/skills/kaggle-submit ~/.claude/skills/
cp -r claude-skills/skills/gdrive-bridge ~/.claude/skills/
```

Windows PowerShell:

```powershell
git clone https://github.com/cannedJean/claude-skills.git
Copy-Item -Recurse claude-skills\skills\* "$env:USERPROFILE\.claude\skills\"
```

Then follow each skill's `README.md` for the one-time backend setup (Colab CLI + gcloud auth, `agy` sign-in, Kaggle API credentials, or Google Drive for Desktop)
and run its `preflight` command. Restart Claude Code so the new skills are picked up.

## Layout

```
skills/
  colab-notebook-runner/   SKILL.md, README.md, scripts/colab_nb_run.py, scripts/setup.sh, examples/smoke_test.ipynb
  agy-delegate/            SKILL.md, README.md, scripts/agy_task.py
  agy-slide-analyzer/      SKILL.md, README.md, scripts/{slide_analyze,slide_validate}.py, prompts/{analyst,reviewer}.md
  agy-pdf-analyzer/        SKILL.md, README.md, scripts/{pdf_analyze,pdf_validate}.py, prompts/{analyst,reviewer}.md
  kaggle-submit/           SKILL.md, README.md, scripts/kaggle_submit.py, references/{kaggle-cli-competitions,troubleshooting}.md
  gdrive-bridge/           SKILL.md, README.md, scripts/gdrive_bridge.py
```

## License

MIT. See [LICENSE](LICENSE).
