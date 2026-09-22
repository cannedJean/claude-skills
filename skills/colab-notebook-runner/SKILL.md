---
name: colab-notebook-runner
description: Run a local Jupyter notebook (.ipynb) on a Google Colab GPU/TPU runtime and bring the executed notebook plus generated artifacts (models, checkpoints, logs, figures) back to the local machine. Use when the user asks to run/train a notebook on Colab, e.g. "코랩에서 돌려줘", "run this notebook on Colab", "학습을 코랩 GPU로 실행하고 결과 가져와". Works on macOS, Linux and Windows (via WSL). Does only this one job.
---

# Colab Notebook Runner

Executes a local `.ipynb` on a Colab runtime through the official `google-colab-cli`, then downloads
the executed notebook and everything the notebook wrote under `/content/outputs`.

All commands below are relative to this skill's directory (`scripts/colab_nb_run.py`).
On macOS/Linux the CLI runs natively; on Windows the script transparently runs it inside WSL.

## Workflow

1. **Preflight** (first use, or after any auth error)
   ```
   python scripts/colab_nb_run.py preflight
   ```
   - `colab CLI not found` → `python scripts/colab_nb_run.py setup`
   - `has_colaboratory_scope: false` or no account → tell the user to run the printed
     `gcloud_login_command` themselves (browser login with the account that holds Colab Pro).
     Never attempt the OAuth flow yourself.

2. **Inspect the notebook** with `--dry-run`. Fix or propose fixes for warnings
   (`drive.mount`, `files.upload/download`, `input()`), and make sure artifacts are written under
   `os.environ["COLAB_OUTPUT_DIR"]` (= `/content/outputs`); only that directory is fetched.
   ```
   python scripts/colab_nb_run.py run train.ipynb --gpu A100 --dry-run
   ```

3. **Run** (long trainings: run in the background and tail stderr)
   ```
   python scripts/colab_nb_run.py run train.ipynb --gpu A100 -u data/ -r requirements.txt
   ```
   - `--gpu {T4,L4,G4,H100,A100}` / `--tpu {v5e1,v6e1}`; on a 400/412 allocation error retry with `--gpu T4`.
   - `-u PATH` (repeatable): files land in `/content/<name>`, directories in `/content/<dirname>/`.
   - Default: cell-by-cell, stop at the first error, kernel state kept between cells.
     `--continue-on-error` or `--whole` (single `colab exec`, no stop-on-error) change that.
   - `--timeout` per cell, default 86400 s. `--keep-on-error` keeps the VM for debugging.

4. **Report** from the final JSON on stdout: `status`, `output_notebook`, `artifacts`, `failed_cell`
   (traceback tail), `elapsed_sec`. Exit codes: 0 ok, 2 cell error, 3 infra error, 4 preflight failure.

5. **Cleanup**: the tool stops its own session in `finally`. If `--keep` was used, run
   `python scripts/colab_nb_run.py stop <session>` afterwards. Idle VMs burn compute units.

## Rules

- **Never stop sessions this tool did not create.** Accounts may be shared; `sessions` may list
  other people's runtimes (shown as `[?]`). `stop` only accepts names starting with `nb-`.
- Do not run `colab repl/console/auth/drivemount` from the agent: they need a TTY.
- Recommend disabling sleep on the local machine during long runs (the websocket must stay up).

## Output layout

```
<nb dir>/colab_results/<nb>_<stamp>/     (or --dest)
  <nb>_output.ipynb    executed notebook, checkpointed after every cell
  outputs/             copy of /content/outputs (tar + 100 MB chunks, sha256-verified)
  run_summary.json     the same JSON printed on stdout
  session_log.md       CLI session log
```

## Troubleshooting

- `AttributeError: module 'jupyter_kernel_client' has no attribute 'KernelClient'` → the PyPI wheel
  got installed; rerun `setup` (installs from git main).
- `403 SCOPE_NOT_PERMITTED` / keep-alive failure → ADC lacks the `colaboratory` scope; re-run the
  gcloud command from `preflight`.
- Windows: WSL default distro is used; override with `--distro NAME` or `COLAB_NB_DISTRO`.
