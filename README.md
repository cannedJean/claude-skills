# claude-skills

Personal [Claude Code](https://claude.com/claude-code) skills. Each skill is a self-contained folder with a
`SKILL.md` (what the agent reads), a `README.md` (setup for humans) and a stdlib-only Python tool under
`scripts/` that can also be run by hand or by other agents.

| Skill | What it does | Backend |
|---|---|---|
| [colab-notebook-runner](skills/colab-notebook-runner/) | Run a local `.ipynb` on a Google Colab GPU/TPU runtime and bring back the executed notebook plus everything it wrote to `$COLAB_OUTPUT_DIR` | official `google-colab-cli` (native on macOS/Linux, via WSL on Windows) |
| [agy-delegate](skills/agy-delegate/) | Hand a bounded coding task to the Antigravity CLI (`agy`) headlessly and get back the exact list of files it created/modified, plus an optional local verification result | Antigravity CLI 1.2+ |
| [kaggle-submit](skills/kaggle-submit/) | End-to-end Kaggle competition submission: first-run `doctor`/`setup` (install CLI, place credentials, verify auth), competition info and rules check, data download, `sample_submission` validation, submit with score polling, local submission log, leaderboard | official `kaggle` CLI (PyPI 2.2.x and git main both supported) |
| [gdrive-bridge](skills/gdrive-bridge/) | Move data bundles and Colab outputs through Google Drive: push a zip into the Drive for Desktop mount, verify a human-made share link for `gdown` (fresh-VM runner path), generate Colab `drive.mount`/`export` cells with manifest verification, and pull results back with sha256 checks | Google Drive for Desktop mount (`G:\` on Windows, `~/Library/CloudStorage` on macOS); stdlib only |

## Install

Copy the skill folders you want into `~/.claude/skills/` (any OS):

```bash
git clone https://github.com/cannedJean/claude-skills.git
cp -r claude-skills/skills/colab-notebook-runner ~/.claude/skills/
cp -r claude-skills/skills/agy-delegate ~/.claude/skills/
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
  kaggle-submit/           SKILL.md, README.md, scripts/kaggle_submit.py, references/{kaggle-cli-competitions,troubleshooting}.md
  gdrive-bridge/           SKILL.md, README.md, scripts/gdrive_bridge.py
```

## License

MIT. See [LICENSE](LICENSE).
