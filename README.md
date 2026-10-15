# claude-skills

Personal [Claude Code](https://claude.com/claude-code) skills. Each skill is a self-contained folder with a
`SKILL.md` (what the agent reads), a `README.md` (setup for humans) and a Python tool under `scripts/`
(stdlib-only unless its README says otherwise) that can also be run by hand or by other agents.

| Skill | What it does | Backend |
|---|---|---|
| [colab-notebook-runner](skills/colab-notebook-runner/) | Run a local `.ipynb` on a Google Colab GPU/TPU runtime and bring back the executed notebook plus everything it wrote to `$COLAB_OUTPUT_DIR` | official `google-colab-cli` (native on macOS/Linux, via WSL on Windows) |
| [agy-delegate](skills/agy-delegate/) | Hand a bounded coding task to the Antigravity CLI (`agy`) headlessly and get back the exact list of files it created/modified, plus an optional local verification result | Antigravity CLI 1.2+ |
| [agy-outsource](skills/agy-outsource/) | Lead/contractor coding with `agy`: Claude splits a task into unit-testable units (deliverables, exact interface, behaviour rules, dependencies) and writes visible + sealed hidden tests; agy implements one unit per session in a strict delivery format (only the listed files + `DELIVERY.json`); deterministic gates (scope, manifest, AST interface, imports, forbidden patterns, visible/hidden tests) accept or send feedback, units run in dependency order and are integration-tested before `integrate` copies them into the project | Antigravity CLI 1.2+ via agy-delegate; stdlib only |
| [agy-slide-analyzer](skills/agy-slide-analyzer/) | Transcribe and analyse a folder of lecture slide images slide-by-slide through `agy` (persona + chain-of-thought + few-shot analyst), then verify each result without a second LLM look at the image: a sealed local-OCR reference (Tesseract / Windows OCR + layout stats) is compared with the JSON, omissions are retried with OCR-derived feedback, and only disputed lines are cropped for Claude to read blind; outputs structured JSON plus a QA report with an audited residual-error estimate. Holds the evaluation code shared with agy-pdf-analyzer; see [docs/ocr-evaluation-design.md](docs/ocr-evaluation-design.md) | Antigravity CLI 1.2+ via agy-delegate; Pillow; Tesseract (`kor`) and/or `winsdk` |
| [agy-pdf-analyzer](skills/agy-pdf-analyzer/) | The same analysis for a whole lecture PDF of any length, without converting it to images for `agy`: the page count is read from the PDF, `agy` views the PDF a few pages per session and writes one structured JSON per page, each verified against the page's text layer (born-digital) or its embedded image through local OCR (scanned), with the same blind Claude review of disputed crops | Antigravity CLI 1.2+ via agy-delegate + agy-slide-analyzer; `pypdf`; Pillow |
| [kaggle-submit](skills/kaggle-submit/) | End-to-end Kaggle competition submission: first-run `doctor`/`setup` (install CLI, place credentials, verify auth), competition info and rules check, data download, `sample_submission` validation, submit with score polling, local submission log, leaderboard | official `kaggle` CLI (PyPI 2.2.x and git main both supported) |
| [gdrive-bridge](skills/gdrive-bridge/) | Move data bundles and Colab outputs through Google Drive: push a zip into the Drive for Desktop mount, verify a human-made share link for `gdown` (fresh-VM runner path), generate Colab `drive.mount`/`export` cells with manifest verification, and pull results back with sha256 checks | Google Drive for Desktop mount (`G:\` on Windows, `~/Library/CloudStorage` on macOS); stdlib only |

## Install

Copy the skill folders you want into `~/.claude/skills/` (any OS):

```bash
git clone https://github.com/cannedJean/claude-skills.git
cp -r claude-skills/skills/colab-notebook-runner ~/.claude/skills/
cp -r claude-skills/skills/agy-delegate ~/.claude/skills/
cp -r claude-skills/skills/agy-outsource ~/.claude/skills/
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
  agy-outsource/           SKILL.md, README.md, scripts/{outsource,gates,mini_test}.py, scripts/tests/{test_outsource,fake_agy_task}.py,
                           prompts/work_order.md, references/plan-format.md, examples/slugkit/{plan.json,tests/,hidden/}
  agy-slide-analyzer/      SKILL.md, README.md, scripts/{slide_analyze,slide_validate,ocr_ref,ocr_score,crops,textnorm}.py,
                           scripts/tests/{test_ocr_score,fake_agy_task,synth}.py, prompts/{analyst,reviewer}.md
  agy-pdf-analyzer/        SKILL.md, README.md, scripts/{pdf_analyze,pdf_validate}.py, prompts/{analyst,reviewer}.md
docs/
  ocr-evaluation-design.md  why and how the analyzers verify agy's transcriptions without a second LLM look
  kaggle-submit/           SKILL.md, README.md, scripts/kaggle_submit.py, references/{kaggle-cli-competitions,troubleshooting}.md
  gdrive-bridge/           SKILL.md, README.md, scripts/gdrive_bridge.py
```

## License

MIT. See [LICENSE](LICENSE).
