#!/usr/bin/env python3
"""colab_nb_run.py - run a local Jupyter notebook on a Google Colab runtime and
bring the executed notebook plus generated files back to the local machine.

Backend: the official `google-colab-cli` (`colab` command).
  macOS / Linux : the CLI runs natively.
  Windows       : the CLI is unsupported natively, so it runs inside WSL
                  (default distro) and Windows paths are mapped to /mnt/<drive>/...

Subcommands
  setup                     install uv + google-colab-cli (from git main)
  preflight                 check CLI, credentials, scopes
  run NOTEBOOK [options]    new session -> upload -> run cells -> collect -> stop
  sessions                  list Colab sessions
  stop NAME                 stop a session created by this tool
  colab -- <args>           pass-through to the colab CLI

Exit codes: 0 ok, 2 a notebook cell raised, 3 infrastructure/CLI error,
4 preflight failure, 5 bad arguments. Requires Python 3.9+, stdlib only.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path, PurePosixPath

IS_WINDOWS = sys.platform == "win32"
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TIMEOUT = float(os.environ.get("COLAB_NB_TIMEOUT", "86400"))
DEFAULT_CHUNK_MB = int(os.environ.get("COLAB_NB_CHUNK_MB", "100"))
REMOTE_ROOT = "/content"
REMOTE_OUTPUTS = "/content/outputs"
REMOTE_STAGE = "/content/_colab_nb_run"
GPU_TYPES = ["T4", "L4", "G4", "H100", "A100"]
TPU_TYPES = ["v5e1", "v6e1"]
REQUIRED_SCOPE = "https://www.googleapis.com/auth/colaboratory"
GCLOUD_SCOPES = (
    "openid,https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/userinfo.email,"
    "https://www.googleapis.com/auth/colaboratory"
)
CLI_GIT = "git+https://github.com/googlecolab/google-colab-cli"
MANIFEST_TAG = "COLAB_NB_MANIFEST "
ENV_TAG = "COLAB_NB_ENV "
RISKY_PATTERNS = [
    (r"drive\.mount\(", "google.colab drive.mount() needs an interactive browser login; save to $COLAB_OUTPUT_DIR instead."),
    (r"files\.upload\(", "google.colab files.upload() needs the browser UI; pass data with --upload instead."),
    (r"files\.download\(", "google.colab files.download() needs the browser UI; write to $COLAB_OUTPUT_DIR instead."),
    (r"\binput\(", "input() has no stdin under the CLI."),
    (r"getpass", "getpass() has no stdin under the CLI."),
    (r"userdata\.get\(", "google.colab userdata (secrets) may be unavailable in CLI-created runtimes."),
]


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    sys.stderr.write(f"[colab-nb {_dt.datetime.now():%H:%M:%S}] {msg}\n")
    sys.stderr.flush()


def die(msg: str, code: int):
    log("ERROR: " + msg)
    print(json.dumps({"status": "error", "exit_code": code, "message": msg}, ensure_ascii=False))
    sys.exit(code)


def decode_out(data: bytes) -> str:
    """Linux/mac process output is UTF-8; wsl.exe's own messages are UTF-16LE."""
    if not data:
        return ""
    if data[:2] == b"\xff\xfe" or (len(data) > 3 and data[1:2] == b"\x00" and data[3:4] == b"\x00"):
        return data.decode("utf-16-le", errors="replace").replace("\x00", "")
    return data.decode("utf-8", errors="replace")


def strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- bridges
class Bridge:
    """Runs a POSIX argv where the colab CLI lives and maps local paths for it."""
    name = "local"

    def __init__(self, adc, colab_bin):
        self.adc = adc            # ADC json path as seen by the CLI, or None for library default
        self._colab_bin = colab_bin

    def argv(self, posix_argv: list) -> list:
        return posix_argv

    def path(self, local) -> str:
        return str(Path(local).resolve())

    def run(self, posix_argv: list, timeout=120, check=False) -> subprocess.CompletedProcess:
        cmd = self.argv(posix_argv)
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        res = subprocess.CompletedProcess(cmd, cp.returncode, decode_out(cp.stdout), decode_out(cp.stderr))
        if check and cp.returncode != 0:
            raise RuntimeError(f"command failed ({cp.returncode}): {' '.join(posix_argv)}\n{res.stdout}\n{res.stderr}")
        return res

    def sh(self, script: str, timeout=120) -> subprocess.CompletedProcess:
        return self.run(["sh", "-lc", script], timeout=timeout)

    def colab_bin(self) -> str:
        if not self._colab_bin:
            cp = self.sh('command -v colab || ls "$HOME/.local/bin/colab" 2>/dev/null')
            lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
            if not lines:
                raise RuntimeError("`colab` CLI not found. Run: python colab_nb_run.py setup")
            self._colab_bin = lines[-1]
        return self._colab_bin

    def colab_argv(self, args: list) -> list:
        env = ["env", "PYTHONIOENCODING=utf-8", "PYTHONUNBUFFERED=1", "TERM=dumb", "NO_COLOR=1"]
        if self.adc:
            env.append(f"GOOGLE_APPLICATION_CREDENTIALS={self.adc}")
        return env + [self.colab_bin(), "--auth", "adc"] + args

    def colab(self, args: list, timeout=300, check=False) -> subprocess.CompletedProcess:
        return self.run(self.colab_argv(args), timeout=timeout, check=check)

    def colab_stream(self, args: list, prefix: str = "  | "):
        """Run a colab command, stream output to stderr live, return (rc, text)."""
        proc = subprocess.Popen(self.argv(self.colab_argv(args)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        buf = []
        for raw in iter(proc.stdout.readline, b""):
            line = strip_ansi(decode_out(raw)).rstrip("\r\n")
            buf.append(line)
            sys.stderr.write(prefix + line + "\n")
            sys.stderr.flush()
        proc.wait()
        return proc.returncode, "\n".join(buf)

    def describe(self) -> str:
        cp = self.sh("uname -sr")
        return f"{self.name}: {cp.stdout.strip()}"


class WslBridge(Bridge):
    name = "wsl"

    def __init__(self, adc, colab_bin, distro=None):
        super().__init__(adc, colab_bin)
        self.distro = distro
        self.wsl = shutil.which("wsl.exe") or "wsl.exe"

    def argv(self, posix_argv: list) -> list:
        cmd = [self.wsl]
        if self.distro:
            cmd += ["-d", self.distro]
        return cmd + ["--cd", "~", "-e"] + posix_argv  # -e: no shell, argv verbatim

    def path(self, local) -> str:
        s = str(Path(local).resolve())
        if s.startswith("\\\\"):
            raise ValueError(f"UNC paths are not supported: {s}")
        drive, rest = os.path.splitdrive(s)
        return "/mnt/" + drive[0].lower() + rest.replace("\\", "/")

    def describe(self) -> str:
        cp = self.sh('echo "$WSL_DISTRO_NAME $(uname -r)"')
        return f"wsl: {cp.stdout.strip()}"


def default_adc_local() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    return Path(os.environ.get("CLOUDSDK_CONFIG", Path.home() / ".config" / "gcloud")) / "application_default_credentials.json"


def make_bridge(a: argparse.Namespace) -> Bridge:
    mode = a.bridge
    if mode == "auto":
        mode = "wsl" if IS_WINDOWS else "local"
    if mode == "wsl":
        # WSL reads the Windows ADC file through /mnt/<drive>; pass it explicitly.
        b = WslBridge(None, a.colab_bin, a.distro)
        if a.adc != "none":
            p = Path(a.adc) if a.adc else default_adc_local()
            if p.exists():
                b.adc = b.path(p)
        return b
    # native: google-auth finds ~/.config/gcloud ADC by itself unless overridden
    adc = str(Path(a.adc).resolve()) if a.adc and a.adc != "none" else None
    return Bridge(adc, a.colab_bin)


def gcloud_login_hint() -> str:
    g = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not g and IS_WINDOWS:
        for c in [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd",
            Path("C:/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"),
            Path("C:/Program Files/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"),
        ]:
            if c.exists():
                g = str(c)
                break
    cmd = f'"{g}"' if g and " " in g else (g or "gcloud")
    if IS_WINDOWS and cmd.startswith('"'):
        cmd = "& " + cmd
    return f"{cmd} auth application-default login --scopes={GCLOUD_SCOPES}"


# --------------------------------------------------------------------------- setup / preflight
def stream(b: Bridge, posix_argv: list, prefix: str = "  | "):
    """Run any POSIX argv through the bridge, streaming output; returns (rc, text)."""
    proc = subprocess.Popen(b.argv(posix_argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buf = []
    for raw in iter(proc.stdout.readline, b""):
        line = strip_ansi(decode_out(raw)).rstrip("\r\n")
        buf.append(line)
        sys.stderr.write(prefix + line + "\n")
    proc.wait()
    return proc.returncode, "\n".join(buf)


def parse_whoami(text: str) -> dict:
    info = {"email": None, "scopes": [], "expires": None}
    for line in strip_ansi(text).splitlines():
        s = line.strip()
        if s.lower().startswith("email:"):
            info["email"] = s.split(":", 1)[1].strip()
        elif s.lower().startswith("expires in:"):
            info["expires"] = s.split(":", 1)[1].strip()
        elif s.startswith("- "):
            info["scopes"].append(s[2:].strip())
    return info


def preflight(b: Bridge) -> dict:
    r = {"ok": False, "bridge": b.name, "checks": {}}
    try:
        r["checks"]["host"] = b.describe()
    except Exception as e:  # noqa: BLE001
        r["error"] = f"cannot reach execution host ({b.name}): {e}"
        return r
    try:
        cp = b.colab(["version"], timeout=120)
        r["checks"]["colab_cli"] = strip_ansi(cp.stdout).strip() or "unknown"
        if cp.returncode != 0:
            raise RuntimeError(cp.stdout + cp.stderr)
    except Exception as e:  # noqa: BLE001
        r["error"] = f"colab CLI not usable: {e}"
        return r
    r["checks"]["adc"] = b.adc or "(library default ADC)"
    cp = b.colab(["whoami"], timeout=120)
    who = parse_whoami(cp.stdout + cp.stderr)
    r["checks"]["account"] = who["email"]
    r["checks"]["token_expires"] = who["expires"]
    r["checks"]["has_colaboratory_scope"] = REQUIRED_SCOPE in who["scopes"]
    if cp.returncode != 0 or not who["email"]:
        r["error"] = "Application Default Credentials not usable. Run:\n  " + gcloud_login_hint()
        return r
    if REQUIRED_SCOPE not in who["scopes"]:
        r["error"] = (f"ADC token for {who['email']} lacks the `colaboratory` scope. Re-mint it with the "
                      "Google account that holds the Colab subscription:\n  " + gcloud_login_hint())
        return r
    cp = b.colab(["sessions"], timeout=120)
    r["checks"]["sessions"] = [ln.strip() for ln in strip_ansi(cp.stdout).splitlines() if ln.strip()]
    r["ok"] = cp.returncode == 0
    if not r["ok"]:
        r["error"] = "colab sessions failed: " + (cp.stdout + cp.stderr).strip()
    return r


# --------------------------------------------------------------------------- notebook helpers
def load_nb(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        nb = json.load(f)
    if nb.get("nbformat", 4) < 4:
        raise ValueError("Only nbformat 4 notebooks are supported")
    return nb


def cell_source(cell: dict) -> str:
    src = cell.get("source", "")
    return "".join(src) if isinstance(src, list) else src


def scan_risky(nb: dict) -> list:
    hits = []
    for i, c in enumerate(nb["cells"]):
        if c.get("cell_type") == "code":
            for pat, why in RISKY_PATTERNS:
                if re.search(pat, cell_source(c)):
                    hits.append({"cell": i, "why": why})
    return hits


def single_cell_nb(template: dict, cell: dict) -> dict:
    return {"nbformat": template.get("nbformat", 4), "nbformat_minor": template.get("nbformat_minor", 5),
            "metadata": copy.deepcopy(template.get("metadata", {})), "cells": [cell]}


def code_cell(src: str, cell_id: str) -> dict:
    return {"cell_type": "code", "id": cell_id, "metadata": {}, "execution_count": None, "outputs": [], "source": src}


def outputs_text(outputs: list) -> str:
    parts = []
    for o in outputs:
        if o.get("output_type") == "stream":
            t = o.get("text", "")
        elif o.get("output_type") in ("execute_result", "display_data"):
            t = o.get("data", {}).get("text/plain", "")
        else:
            continue
        parts.append("".join(t) if isinstance(t, list) else t)
    return "".join(parts)


def first_error(outputs: list):
    for o in outputs:
        if o.get("output_type") == "error":
            return {"ename": o.get("ename"), "evalue": o.get("evalue"),
                    "traceback": [strip_ansi(t) for t in o.get("traceback", [])][-15:]}
    return None


# --------------------------------------------------------------------------- code run on the VM
PRELUDE = r'''
import os, sys, subprocess, json, glob
os.makedirs({outputs!r}, exist_ok=True); os.makedirs({stage!r}, exist_ok=True)
os.environ["COLAB_OUTPUT_DIR"] = {outputs!r}; os.environ["COLAB_NB_RUN"] = "1"; os.environ["COLAB_NB_SESSION"] = {session!r}
for base in sorted(set(p.rsplit(".part", 1)[0] for p in glob.glob({stage!r} + "/*.part*"))):
    with open(base, "wb") as w:
        for p in sorted(glob.glob(base + ".part*")):
            with open(p, "rb") as r: w.write(r.read())
            os.remove(p)
for tgz in sorted(glob.glob({stage!r} + "/*.upload.tgz")):
    subprocess.run(["tar", "xzf", tgz, "-C", {root!r}], check=True); os.remove(tgz)
info = {{"cwd": os.getcwd(), "python": sys.version.split()[0]}}
try: info["gpu"] = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True).stdout.strip() or "none"
except Exception as e: info["gpu"] = "n/a (" + str(e) + ")"
try:
    import psutil; info["ram_gb"] = round(psutil.virtual_memory().total / 2**30, 1)
except Exception: pass
print("COLAB_NB_ENV " + json.dumps(info))
'''

COLLECT = r'''
import os, subprocess, hashlib, json, glob
out, stage = {outputs!r}, {stage!r}
res = {{"empty": True}}
if os.path.isdir(out) and any(os.scandir(out)):
    for p in glob.glob(stage + "/_out.tgz*"): os.remove(p)
    tgz = stage + "/_out.tgz"
    subprocess.run(["tar", "czf", tgz, "-C", os.path.dirname(out), os.path.basename(out)], check=True)
    h = hashlib.sha256()
    with open(tgz, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    subprocess.run(["split", "-b", "{chunk_mb}m", "-d", "-a", "4", tgz, tgz + ".part"], check=True)
    files = [[os.path.relpath(os.path.join(dp, n), out), os.path.getsize(os.path.join(dp, n))]
             for dp, _, fn in os.walk(out) for n in fn]
    res = {{"empty": False, "size": os.path.getsize(tgz), "sha256": h.hexdigest(),
            "parts": sorted(glob.glob(tgz + ".part*")), "files": files}}
print("COLAB_NB_MANIFEST " + json.dumps(res))
'''


# --------------------------------------------------------------------------- runner
class Runner:
    def __init__(self, a: argparse.Namespace, b: Bridge):
        self.a, self.b = a, b
        self.nb_path = Path(a.notebook).resolve()
        name = a.session or f"nb-{self.nb_path.stem[:20]}-{_dt.datetime.now():%m%d%H%M%S}"
        self.session = re.sub(r"[^a-z0-9-]", "-", name.lower())
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dest = Path(a.dest).resolve() if a.dest else self.nb_path.parent / "colab_results" / f"{self.nb_path.stem}_{stamp}"
        self.work = self.nb_path.parent / ".colab_nb_run" / self.session
        self.summary = {"status": "unknown", "session": self.session, "notebook": str(self.nb_path),
                        "dest": str(self.dest), "gpu": a.gpu, "tpu": a.tpu,
                        "started": _dt.datetime.now().isoformat(timespec="seconds"),
                        "cells": {"total": 0, "executed": 0}, "artifacts": [], "warnings": []}
        self.session_created = False
        self.failed = False
        self.nb, self.result_nb = {}, {}

    # -- helpers ------------------------------------------------------------
    def exec_nb_file(self, local_nb: Path, timeout: float):
        """`colab exec` a local .ipynb; returns (rc, output_nb_or_None, streamed_text)."""
        out_path = local_nb.with_name(local_nb.stem + "_output.ipynb")
        if out_path.exists():
            out_path.unlink()
        rc, text = self.b.colab_stream(["exec", "-s", self.session, "-f", self.b.path(local_nb), "--timeout", str(timeout)])
        out_nb = None
        if out_path.exists():
            try:
                out_nb = load_nb(out_path)
            except Exception as e:  # noqa: BLE001
                log(f"could not parse {out_path.name}: {e}")
        return rc, out_nb, text

    def exec_snippet(self, name: str, code: str, timeout: float = 3600):
        f = self.work / f"{name}.ipynb"
        f.write_text(json.dumps(single_cell_nb(self.nb, code_cell(code, name))), encoding="utf-8")
        rc, out_nb, text = self.exec_nb_file(f, timeout)
        outputs = out_nb["cells"][0].get("outputs", []) if out_nb and out_nb.get("cells") else []
        return rc, outputs, text

    @staticmethod
    def tagged_json(outputs: list, streamed: str, tag: str):
        for line in (outputs_text(outputs) + "\n" + streamed).splitlines():
            line = strip_ansi(line).strip()
            if line.startswith(tag):
                try:
                    return json.loads(line[len(tag):])
                except json.JSONDecodeError:
                    continue
        return None

    def upload_file(self, local: Path, remote: str) -> None:
        chunk = self.a.chunk_mb * 1024 * 1024
        if local.stat().st_size <= chunk:
            self.b.colab(["upload", "-s", self.session, self.b.path(local), remote], timeout=3600, check=True)
            return
        with open(local, "rb") as f:
            idx = 0
            while True:
                data = f.read(chunk)
                if not data:
                    break
                part = self.work / f"{local.name}.part{idx:04d}"
                part.write_bytes(data)
                self.b.colab(["upload", "-s", self.session, self.b.path(part), f"{remote}.part{idx:04d}"], timeout=3600, check=True)
                part.unlink()
                idx += 1
        log(f"uploaded {local.name} in {idx} chunks")

    # -- phases ---------------------------------------------------------------
    def phase_new(self) -> None:
        args = ["new", "-s", self.session] + (["--gpu", self.a.gpu] if self.a.gpu else []) + (["--tpu", self.a.tpu] if self.a.tpu else [])
        log(f"allocating Colab runtime: colab {' '.join(args)}")
        rc, text = self.b.colab_stream(args)
        if rc != 0:
            raise RuntimeError(f"colab new failed (rc={rc}). Output:\n{text}")
        self.session_created = True

    def phase_upload(self) -> None:
        if self.a.requirements:
            req = Path(self.a.requirements).resolve()
            log(f"installing requirements from {req.name}")
            rc, _ = self.b.colab_stream(["install", "-s", self.session, "-r", self.b.path(req)])
            if rc != 0:
                self.summary["warnings"].append("colab install -r returned non-zero")
        rc, outputs, _ = self.exec_snippet("_mkstage", f"import os; os.makedirs({REMOTE_STAGE!r}, exist_ok=True)", timeout=300)
        if rc != 0 or first_error(outputs):
            raise RuntimeError("could not create staging dir on the VM")
        for item in self.a.upload or []:
            p = Path(item).resolve()
            if not p.exists():
                raise FileNotFoundError(f"--upload path not found: {p}")
            if p.is_dir():
                tgz = self.work / f"{p.name}.upload.tgz"
                log(f"packing directory {p.name}")
                with tarfile.open(tgz, "w:gz") as tf:
                    tf.add(p, arcname=p.name)
                self.upload_file(tgz, f"{REMOTE_STAGE}/{p.name}.upload.tgz")
                tgz.unlink()
            else:
                self.upload_file(p, f"{REMOTE_ROOT}/{p.name}")
            log(f"uploaded {p.name} -> {REMOTE_ROOT}/{p.name}")

    def phase_prelude(self) -> None:
        code = PRELUDE.format(outputs=REMOTE_OUTPUTS, stage=REMOTE_STAGE, root=REMOTE_ROOT, session=self.session)
        rc, outputs, text = self.exec_snippet("_prelude", code, timeout=1800)
        err = first_error(outputs)
        if rc != 0 or err:
            raise RuntimeError(f"prelude failed: rc={rc} err={err}")
        self.summary["runtime"] = self.tagged_json(outputs, text, ENV_TAG)
        log(f"runtime ready: {self.summary['runtime']}")

    def phase_cells(self) -> None:
        cells = self.nb["cells"]
        code_idx = [i for i, c in enumerate(cells) if c.get("cell_type") == "code" and cell_source(c).strip()]
        self.summary["cells"]["total"] = len(code_idx)

        if self.a.whole:
            log(f"executing whole notebook ({len(code_idx)} code cells) in one colab exec")
            tmp = self.work / f"{self.nb_path.stem}.ipynb"
            shutil.copy2(self.nb_path, tmp)
            rc, out_nb, _ = self.exec_nb_file(tmp, self.a.timeout)
            if out_nb is None:
                raise RuntimeError(f"colab exec produced no output notebook (rc={rc})")
            self.result_nb = out_nb
            for i, c in enumerate(out_nb["cells"]):
                if c.get("cell_type") == "code":
                    self.summary["cells"]["executed"] += 1
                    err = first_error(c.get("outputs", []))
                    if err and not self.failed:
                        self.failed = True
                        self.summary["failed_cell"] = {"index": i, **err}
            if rc != 0:
                raise RuntimeError(f"colab exec exited rc={rc} (timeout or lost session)")
            return

        self.result_nb = copy.deepcopy(self.nb)
        for n, i in enumerate(code_idx, 1):
            src = cell_source(cells[i])
            log(f"cell {n}/{len(code_idx)} (nb index {i}): {src.strip().splitlines()[0][:70]}")
            cell_copy = copy.deepcopy(cells[i])
            cell_copy["outputs"], cell_copy["execution_count"] = [], None
            nb_file = self.work / f"cell_{i:04d}.ipynb"
            nb_file.write_text(json.dumps(single_cell_nb(self.nb, cell_copy)), encoding="utf-8")
            t0 = time.time()
            rc, out_nb, text = self.exec_nb_file(nb_file, self.a.timeout)
            outputs = out_nb["cells"][0].get("outputs", []) if out_nb and out_nb.get("cells") else []
            rcell = self.result_nb["cells"][i]
            rcell["outputs"], rcell["execution_count"] = outputs, n
            rcell.setdefault("metadata", {})["colab_nb_run"] = {"seconds": round(time.time() - t0, 1), "rc": rc}
            self.summary["cells"]["executed"] = n
            self.write_result_nb()  # checkpoint after every cell
            if rc != 0:
                raise RuntimeError(f"colab exec exited rc={rc} on cell index {i} (timeout/lost session). Output:\n{text[-2000:]}")
            err = first_error(outputs)
            if err:
                self.failed = True
                self.summary["failed_cell"] = {"index": i, "ordinal": n, **err}
                log(f"cell index {i} raised {err['ename']}: {err['evalue']}")
                if not self.a.continue_on_error:
                    log("stopping at first error (--continue-on-error to keep going)")
                    break

    def write_result_nb(self) -> Path:
        self.dest.mkdir(parents=True, exist_ok=True)
        out = self.dest / f"{self.nb_path.stem}_output.ipynb"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(self.result_nb, f, ensure_ascii=False, indent=1)
        self.summary["output_notebook"] = str(out)
        return out

    def phase_collect(self) -> None:
        log(f"collecting {REMOTE_OUTPUTS} from the VM")
        code = COLLECT.format(outputs=REMOTE_OUTPUTS, stage=REMOTE_STAGE, chunk_mb=self.a.chunk_mb)
        rc, outputs, text = self.exec_snippet("_collect", code, timeout=3600)
        man = self.tagged_json(outputs, text, MANIFEST_TAG)
        if rc != 0 or man is None:
            raise RuntimeError(f"collect step failed rc={rc}: {first_error(outputs)}\n{text[-1500:]}")
        if man.get("empty"):
            log(f"{REMOTE_OUTPUTS} is empty; nothing to download (write files to $COLAB_OUTPUT_DIR)")
            return
        tgz = self.work / "_out.tgz"
        with open(tgz, "wb") as wf:
            for k, part in enumerate(man["parts"], 1):
                local_part = self.work / PurePosixPath(part).name
                log(f"downloading part {k}/{len(man['parts'])}")
                self.b.colab(["download", "-s", self.session, part, self.b.path(local_part)], timeout=3600, check=True)
                wf.write(local_part.read_bytes())
                local_part.unlink()
        digest = sha256_file(tgz)
        if digest != man["sha256"]:
            raise RuntimeError(f"sha256 mismatch after download: {digest} != {man['sha256']}")
        art_dir = self.dest / "outputs"
        if art_dir.exists():
            shutil.rmtree(art_dir)
        with tarfile.open(tgz, "r:gz") as tf:
            try:
                tf.extractall(self.dest, filter="data")
            except TypeError:  # Python < 3.12 without the filter argument
                tf.extractall(self.dest)
        tgz.unlink()
        self.summary["artifacts"] = [{"path": str(art_dir / f), "bytes": n} for f, n in man["files"]]
        log(f"artifacts: {len(man['files'])} files, {man['size']/2**20:.1f} MB compressed -> {art_dir}")

    def phase_log(self) -> None:
        f = self.dest / "session_log.md"
        cp = self.b.colab(["log", "-s", self.session, "-o", self.b.path(f)], timeout=300)
        if cp.returncode == 0 and f.exists():
            self.summary["session_log"] = str(f)

    def phase_stop(self) -> None:
        if self.a.keep or (self.a.keep_on_error and self.summary["status"] != "ok"):
            log(f"session '{self.session}' KEPT; run `colab_nb_run.py stop {self.session}` when done")
            self.summary["session_kept"] = True
            return
        log(f"stopping session '{self.session}'")
        cp = self.b.colab(["stop", "-s", self.session], timeout=300)
        if cp.returncode != 0:
            self.summary["warnings"].append(f"colab stop failed: {(cp.stdout + cp.stderr).strip()[-300:]}")
        self.summary["session_kept"] = False

    # -- main -----------------------------------------------------------------
    def run(self) -> int:
        if not self.nb_path.exists():
            die(f"notebook not found: {self.nb_path}", 5)
        self.nb = load_nb(self.nb_path)
        for h in scan_risky(self.nb):
            self.summary["warnings"].append(f"cell {h['cell']}: {h['why']}")
            log(f"WARNING cell {h['cell']}: {h['why']}")
        if self.a.dry_run:
            self.summary["status"] = "dry-run"
            self.summary["cells"]["total"] = sum(1 for c in self.nb["cells"] if c.get("cell_type") == "code" and cell_source(c).strip())
            plan = ["new", "-s", self.session] + (["--gpu", self.a.gpu] if self.a.gpu else []) + (["--tpu", self.a.tpu] if self.a.tpu else [])
            try:
                self.summary["would_run"] = " ".join(self.b.argv(self.b.colab_argv(plan)))
            except Exception as e:  # noqa: BLE001
                self.summary["would_run"] = f"(colab CLI not resolvable: {e})"
            print(json.dumps(self.summary, ensure_ascii=False, indent=2))
            return 0

        pf = preflight(self.b)
        self.summary["account"] = pf["checks"].get("account")
        if not pf["ok"]:
            die(pf.get("error", "preflight failed"), 4)

        self.work.mkdir(parents=True, exist_ok=True)
        self.dest.mkdir(parents=True, exist_ok=True)
        t0, code = time.time(), 3
        try:
            self.phase_new()
            self.phase_upload()
            self.phase_prelude()
            self.phase_cells()
            self.write_result_nb()
            self.phase_collect()
            self.phase_log()
            self.summary["status"], code = ("cell-error", 2) if self.failed else ("ok", 0)
        except KeyboardInterrupt:
            self.summary["status"], self.summary["error"], code = "interrupted", "KeyboardInterrupt", 130
        except Exception as e:  # noqa: BLE001
            self.summary["status"], self.summary["error"], code = "error", str(e)[-3000:], 3
            log(f"ERROR: {e}")
        finally:
            if self.session_created:
                try:
                    self.phase_stop()
                except Exception as e:  # noqa: BLE001
                    self.summary["warnings"].append(f"stop failed: {e}")
            self.summary["elapsed_sec"] = round(time.time() - t0, 1)
            self.summary["exit_code"] = code
            if not self.a.keep_work and self.work.exists():
                shutil.rmtree(self.work, ignore_errors=True)
                try:
                    self.work.parent.rmdir()
                except OSError:
                    pass
            try:
                (self.dest / "run_summary.json").write_text(json.dumps(self.summary, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
            print(json.dumps(self.summary, ensure_ascii=False, indent=2))
        return code


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="colab_nb_run.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bridge", choices=["auto", "local", "wsl"], default=os.environ.get("COLAB_NB_BRIDGE", "auto"),
                   help="where the colab CLI runs: auto = wsl on Windows, local elsewhere")
    p.add_argument("--distro", default=os.environ.get("COLAB_NB_DISTRO"), help="WSL distro (default: WSL's default distro)")
    p.add_argument("--adc", default=os.environ.get("COLAB_NB_ADC"),
                   help="ADC json path, or 'none' to let google-auth discover it. Default: gcloud's standard location")
    p.add_argument("--colab-bin", default=os.environ.get("COLAB_NB_BIN"), help="absolute path of the colab binary (auto-detected)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="install uv + google-colab-cli where the bridge runs")
    sub.add_parser("preflight", help="verify CLI + credentials")
    r = sub.add_parser("run", help="run a notebook on Colab and fetch results")
    r.add_argument("notebook")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--gpu", choices=GPU_TYPES)
    g.add_argument("--tpu", choices=TPU_TYPES)
    r.add_argument("--session", "-s", help="session name (auto: nb-<notebook>-<stamp>)")
    r.add_argument("--dest", help="local results dir (default: <nb dir>/colab_results/<nb>_<stamp>)")
    r.add_argument("--upload", "-u", action="append", help="local file or directory to place under /content (repeatable)")
    r.add_argument("--requirements", "-r", help="requirements.txt to install on the VM first")
    r.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"per-cell timeout seconds (default {DEFAULT_TIMEOUT:.0f})")
    r.add_argument("--chunk-mb", type=int, default=DEFAULT_CHUNK_MB, help="transfer chunk size in MB")
    r.add_argument("--whole", action="store_true", help="run all cells in one colab exec (no stop-on-error)")
    r.add_argument("--continue-on-error", action="store_true", help="keep executing cells after an error")
    r.add_argument("--keep", action="store_true", help="keep the VM alive after the run")
    r.add_argument("--keep-on-error", action="store_true", help="keep the VM alive only if the run failed")
    r.add_argument("--keep-work", action="store_true", help="keep the local .colab_nb_run temp dir")
    r.add_argument("--dry-run", action="store_true", help="parse + scan the notebook and print the plan only")
    sub.add_parser("sessions", help="list sessions")
    s = sub.add_parser("stop", help="stop a session created by this tool")
    s.add_argument("name")
    c = sub.add_parser("colab", help="pass-through: colab_nb_run.py colab -- <colab args>")
    c.add_argument("args", nargs=argparse.REMAINDER)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    b = make_bridge(a)
    if a.cmd == "setup":
        rc, _ = stream(b, ["bash", b.path(SKILL_DIR / "scripts" / "setup.sh")])
        return rc
    if a.cmd == "preflight":
        res = preflight(b)
        res["gcloud_login_command"] = gcloud_login_hint()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 4
    if a.cmd == "sessions":
        return b.colab_stream(["sessions"], prefix="")[0]
    if a.cmd == "stop":
        if not a.name.startswith("nb-"):
            die(f"refusing to stop '{a.name}': not a session created by this tool (names start with nb-)", 5)
        return b.colab_stream(["stop", "-s", a.name], prefix="")[0]
    if a.cmd == "colab":
        args = a.args[1:] if a.args[:1] == ["--"] else a.args
        return b.colab_stream(args, prefix="")[0]
    if a.cmd == "run":
        return Runner(a, b).run()
    return 5


if __name__ == "__main__":
    sys.exit(main())
