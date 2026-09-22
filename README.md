# claude-skills

Personal [Claude Code](https://claude.com/claude-code) skills. Each skill is a self-contained folder with a
`SKILL.md` (what the agent reads), a `README.md` (setup for humans) and a stdlib-only Python tool under
`scripts/` that can also be run by hand or by other agents.

| Skill | What it does | Backend |
|---|---|---|
| [colab-notebook-runner](skills/colab-notebook-runner/) | Run a local `.ipynb` on a Google Colab GPU/TPU runtime and bring back the executed notebook plus everything it wrote to `$COLAB_OUTPUT_DIR` | official `google-colab-cli` (native on macOS/Linux, via WSL on Windows) |
| [agy-delegate](skills/agy-delegate/) | Hand a bounded coding task to the Antigravity CLI (`agy`) headlessly and get back the exact list of files it created/modified, plus an optional local verification result | Antigravity CLI 1.2+ |

## Install

Copy the skill folders you want into `~/.claude/skills/` (any OS):

```bash
git clone https://github.com/cannedJean/claude-skills.git
cp -r claude-skills/skills/colab-notebook-runner ~/.claude/skills/
cp -r claude-skills/skills/agy-delegate ~/.claude/skills/
```

Windows PowerShell:

```powershell
git clone https://github.com/cannedJean/claude-skills.git
Copy-Item -Recurse claude-skills\skills\* "$env:USERPROFILE\.claude\skills\"
```

Then follow each skill's `README.md` for the one-time backend setup (Colab CLI + gcloud auth, or `agy` sign-in)
and run its `preflight` command. Restart Claude Code so the new skills are picked up.

## Layout

```
skills/
  colab-notebook-runner/   SKILL.md, README.md, scripts/colab_nb_run.py, scripts/setup.sh, examples/smoke_test.ipynb
  agy-delegate/            SKILL.md, README.md, scripts/agy_task.py
```

## License

MIT. See [LICENSE](LICENSE).
