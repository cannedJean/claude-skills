#!/usr/bin/env bash
# Install uv + google-colab-cli. Runs natively on macOS/Linux, or inside WSL on Windows.
# Preferred entry point: python scripts/colab_nb_run.py setup
set -u
export PATH="$HOME/.local/bin:$PATH"

if ! command -v git >/dev/null 2>&1; then
  echo "[setup] git is required (macOS: xcode-select --install, Ubuntu: sudo apt-get install -y git)"; exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh || exit 1
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "[setup] uv $(uv --version)"

# Install from git main, not the PyPI wheel: the 0.6.0 wheel leaves jupyter-kernel-client
# unpinned and >=1.0 renamed KernelClient, which breaks `colab exec` with
# "module 'jupyter_kernel_client' has no attribute 'KernelClient'".
echo "[setup] installing google-colab-cli from git main"
uv tool install --force "git+https://github.com/googlecolab/google-colab-cli" || exit 1
echo "[setup] colab $(colab version) at $(command -v colab)"
echo "[setup] next: authenticate with gcloud (see README.md), then: python scripts/colab_nb_run.py preflight"
