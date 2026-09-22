# colab-notebook-runner

Run a local Jupyter notebook on a Google Colab (Pro) runtime and fetch the results.
A Claude Code skill plus a standalone CLI tool; any agent (Antigravity, Gemini CLI, Codex) or a human
can call `scripts/colab_nb_run.py` directly.

Backend: the official [google-colab-cli](https://github.com/googlecolab/google-colab-cli)
(`colab new/exec/upload/download/stop`). It supports macOS and Linux natively; on Windows the
tool runs it inside WSL and maps paths automatically.

## Install

Requirements: Python 3.9+, `git`, and the [gcloud CLI](https://cloud.google.com/sdk/docs/install)
for authentication. Windows additionally needs WSL with a Linux distro (`wsl --install`).

```bash
python scripts/colab_nb_run.py setup
```

This installs `uv` and `google-colab-cli` from git main (natively, or inside WSL on Windows).
The PyPI wheel is deliberately avoided: as of 2026-09 it pulls an incompatible
`jupyter-kernel-client` and `colab exec` fails.

## Authenticate (human step, once)

Sign in with the Google account that holds the Colab subscription. The `colaboratory` scope is
mandatory; the default gcloud scopes are not enough.

```bash
gcloud auth application-default login --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
```

Then verify:

```bash
python scripts/colab_nb_run.py preflight
```

Expect `"ok": true`, your account under `account`, and `"has_colaboratory_scope": true`.
`preflight` prints the exact gcloud command for your OS if something is missing.

## Use

```bash
python scripts/colab_nb_run.py run train.ipynb --gpu A100 -u data -r requirements.txt
python scripts/colab_nb_run.py run train.ipynb --gpu T4 --dry-run     # inspect only
python scripts/colab_nb_run.py sessions
python scripts/colab_nb_run.py stop nb-train-0922153000
python scripts/colab_nb_run.py colab -- status -s nb-train-0922153000  # raw CLI
```

Notebook convention: write artifacts under `COLAB_OUTPUT_DIR`; that directory is what comes back.

```python
import os
OUT = os.environ.get("COLAB_OUTPUT_DIR", "./outputs"); os.makedirs(OUT, exist_ok=True)
torch.save(model.state_dict(), f"{OUT}/model.pt")
```

Results land in `<notebook dir>/colab_results/<name>_<stamp>/` (or `--dest`):
`<name>_output.ipynb`, `outputs/`, `run_summary.json`, `session_log.md`.

Smoke test: `python scripts/colab_nb_run.py run examples/smoke_test.ipynb --gpu T4`
(about one minute on a T4; produces three files and releases the VM).

## Options and environment

| Flag | Env | Meaning |
|---|---|---|
| `--bridge auto\|local\|wsl` | `COLAB_NB_BRIDGE` | where the CLI runs (auto: wsl on Windows) |
| `--distro NAME` | `COLAB_NB_DISTRO` | WSL distro (default: WSL default) |
| `--adc PATH\|none` | `COLAB_NB_ADC` | ADC file (default: gcloud's standard location) |
| `--colab-bin PATH` | `COLAB_NB_BIN` | colab binary (auto-detected) |
| `--timeout SEC` | `COLAB_NB_TIMEOUT` | per-cell timeout (default 86400) |
| `--chunk-mb N` | `COLAB_NB_CHUNK_MB` | transfer chunk size (default 100) |

## Notes and limits

- The tool stops only the session it created. Other sessions listed by `sessions` are left alone.
- `drive.mount`, `files.upload/download`, `input()` do not work under the CLI (no browser, no TTY).
- File transfer goes through Colab's contents API (base64 JSON per request), hence chunking.
  Keep only what you need in `COLAB_OUTPUT_DIR`; multi-GB artifacts are slow.
- Each cell is a separate `colab exec` call (a few seconds of overhead); kernel state persists.
- If the local machine sleeps, the websocket drops and the run fails. Disable sleep for long jobs.
