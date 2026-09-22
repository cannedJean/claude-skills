#!/usr/bin/env python3
"""kaggle_submit.py - Kaggle competition submission automation wrapper.

Wraps the official `kaggle` CLI (https://github.com/Kaggle/kaggle-cli) so an
agent can: bootstrap the CLI + credentials on first run, check a competition,
download data, validate a submission file against the sample, submit, wait for
the score, and keep a local submission log.

Every command prints ONE JSON object on stdout; progress goes to stderr.
Exit codes: 0 ok / 2 validation or precondition failed / 3 kaggle CLI error /
4 setup needed (CLI missing or not authenticated).

Only the standard library is used. The `kaggle` package is invoked as
`<python> -m kaggle`, so PATH problems with kaggle.exe never matter.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

RC_OK, RC_PRECONDITION, RC_CLI, RC_SETUP = 0, 2, 3, 4
SETTINGS_URL = "https://www.kaggle.com/settings/api"
MIN_PY = (3, 11)

# --------------------------------------------------------------------------- utils


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(obj: dict, rc: int = RC_OK) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    sys.exit(rc)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def config_dir() -> Path:
    """Mirror KaggleApi's config-dir resolution (KAGGLE_CONFIG_DIR > ~/.kaggle > XDG on Linux)."""
    env = os.environ.get("KAGGLE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    home = Path.home() / ".kaggle"
    if sys.platform.startswith("linux") and not home.exists():
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        return Path(xdg) / "kaggle"
    return home


def chmod_600(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows: ACLs, ignore


def python_for_kaggle() -> str:
    return os.environ.get("KAGGLE_SUBMIT_PYTHON") or sys.executable


def kaggle_base_cmd() -> list[str]:
    return [python_for_kaggle(), "-W", "ignore", "-m", "kaggle", "--no-warn"]


def kaggle_installed() -> str | None:
    """Return the CLI version string if `python -m kaggle` works, else None."""
    try:
        p = subprocess.run(kaggle_base_cmd() + ["--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Kaggle (?:CLI|API) ([\d.]+)", p.stdout + p.stderr)
    if m:
        return m.group(1)
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip().splitlines()[-1]
    return None


AUTH_NOISE = re.compile(
    r"^(Authentication required|First, you will need|  https://www\.kaggle\.com|Recommended:|No token to manage|"
    r"    kaggle auth login|If you'd rather|and supply it|  Option [AB]:|    export KAGGLE_API_TOKEN|"
    r"    Save the token|Using competition:|Looks like you're using an outdated|Please consider updating)",
)


def clean_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not AUTH_NOISE.match(ln)]


def run_kaggle(args: list[str], timeout: int = 900, cwd: str | None = None) -> tuple[int, str, str]:
    cmd = kaggle_base_cmd() + args
    log("$ kaggle " + " ".join(args))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 127, "", "python not found: " + cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    return p.returncode, p.stdout, p.stderr


def parse_json_output(stdout: str):
    """kaggle --format json prints a JSON doc, sometimes preceded by noise lines."""
    text = stdout.strip()
    for start in (text.find("["), text.find("{")):
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                continue
    return None


def classify_failure(rc: int, out: str, err: str) -> tuple[int, str, list[str]]:
    """Map a failed kaggle call to (exit code, reason, next_steps)."""
    blob = out + "\n" + err
    low = blob.lower()
    if "authentication required" in low or "invalid credentials" in low or " 401" in blob:
        return RC_SETUP, "not_authenticated", [
            f"python {Path(__file__).name} setup  (see printed instructions)",
        ]
    if " 403" in blob or ("accept" in low and "rules" in low):
        return RC_PRECONDITION, "rules_not_accepted_or_forbidden", [
            "Open https://www.kaggle.com/competitions/<slug>/rules in a browser, join the competition and "
            "accept the rules, then retry.",
        ]
    if " 404" in blob or "could not find competition" in low:
        return RC_PRECONDITION, "competition_not_found_or_closed", [
            "Check the slug with: kaggle competitions list -s <keyword>",
        ]
    if "timed out" in low:
        return RC_CLI, "timeout", ["Retry; large uploads may need a larger --timeout."]
    return RC_CLI, "cli_error", ["Read `cli_output`; run the same kaggle command manually for details."]


def fail(rc: int, out: str, err: str, extra: dict | None = None) -> None:
    code, reason, steps = classify_failure(rc, out, err)
    obj = {"ok": False, "reason": reason, "next_steps": steps, "cli_rc": rc,
           "cli_output": clean_lines(out)[-30:], "cli_stderr": clean_lines(err)[-30:]}
    if extra:
        obj.update(extra)
    emit(obj, code)


# --------------------------------------------------------------------------- features


def cli_features() -> dict:
    """Detect optional features that differ between PyPI releases and git main."""
    feats = {"submit_wait": False, "submission_cmd": False, "submission_limits": False, "download_unzip": False}
    _, out, _ = run_kaggle(["competitions", "submit", "--help"], timeout=60)
    feats["submit_wait"] = "--wait" in out
    _, out, _ = run_kaggle(["competitions", "--help"], timeout=60)
    feats["submission_cmd"] = bool(re.search(r"[{,]submission[,}]", out))
    feats["submission_limits"] = "submission-limits" in out
    _, out, _ = run_kaggle(["competitions", "download", "--help"], timeout=60)
    feats["download_unzip"] = "--unzip" in out
    return feats


# --------------------------------------------------------------------------- auth detection


def detect_credentials() -> dict:
    d = config_dir()
    found = []
    if os.environ.get("KAGGLE_API_TOKEN"):
        found.append({"source": "env:KAGGLE_API_TOKEN"})
    if os.environ.get("KAGGLE_USERNAME") and os.environ.get("KAGGLE_KEY"):
        found.append({"source": "env:KAGGLE_USERNAME+KAGGLE_KEY"})
    for name, kind in (("access_token", "api_token_file"), ("kaggle.json", "legacy_kaggle_json"),
                       ("credentials.json", "oauth")):
        p = d / name
        if p.is_file() and p.stat().st_size > 0:
            entry = {"source": kind, "path": str(p)}
            if name == "kaggle.json":
                try:
                    j = json.loads(p.read_text(encoding="utf-8-sig"))
                    entry["username"] = j.get("username")
                    entry["has_key"] = bool(j.get("key"))
                except Exception as e:  # noqa: BLE001
                    entry["parse_error"] = str(e)
            found.append(entry)
    return {"config_dir": str(d), "sources": found}


def live_auth_check() -> dict:
    """Cheapest authenticated call: list 1 competition."""
    _, out, _ = run_kaggle(["config", "view"], timeout=120)
    username = None
    m = re.search(r"username[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_\-]+)", out)
    if m:
        username = m.group(1)
    rc2, out2, err2 = run_kaggle(["competitions", "list", "--page-size", "1", "--format", "json"], timeout=120)
    ok = rc2 == 0 and parse_json_output(out2) is not None and "Authentication required" not in (out2 + err2)
    return {"ok": ok, "username": username, "detail": [] if ok else clean_lines(out2 + err2)[-5:]}


def setup_instructions() -> list[str]:
    py = python_for_kaggle()
    me = str(Path(__file__).resolve())
    return [
        "Pick ONE way to give the Kaggle CLI credentials (the agent must never type or paste the secret itself):",
        f"  (A) OAuth, recommended: run in YOUR OWN terminal ->  {py} -m kaggle auth login   "
        "(a browser opens; sign in with your Kaggle account)",
        f"  (B) Legacy key: {SETTINGS_URL} -> 'Create Legacy API Key' downloads kaggle.json, then ->  "
        f"python \"{me}\" setup --kaggle-json ~/Downloads/kaggle.json",
        f"  (C) API token: {SETTINGS_URL} -> 'Generate New Token', save it into a text file, then ->  "
        f"python \"{me}\" setup --token-file <that file>",
        "  (D) CI/non-interactive: set env var KAGGLE_API_TOKEN (or KAGGLE_USERNAME + KAGGLE_KEY) before running.",
        f"Then verify ->  python \"{me}\" doctor",
    ]


# --------------------------------------------------------------------------- commands


def cmd_doctor(a) -> None:
    pyv = sys.version_info[:3]
    report = {
        "ok": False,
        "python": {"executable": python_for_kaggle(), "version": ".".join(map(str, pyv)),
                   "meets_minimum": pyv >= MIN_PY},
        "kaggle_cli": {"installed": False, "version": None},
        "credentials": detect_credentials(),
        "auth": {"ok": False, "username": None},
        "features": {},
        "next_steps": [],
    }
    ver = kaggle_installed()
    if not ver:
        report["next_steps"].append(f"Install the CLI ->  python \"{Path(__file__).resolve()}\" setup")
        emit(report, RC_SETUP)
    report["kaggle_cli"] = {"installed": True, "version": ver}
    report["features"] = cli_features()
    auth = live_auth_check()
    report["auth"] = auth
    if not auth["ok"]:
        report["next_steps"] = setup_instructions()
        if report["credentials"]["sources"]:
            report["next_steps"].insert(
                0, "Credentials were found but rejected (expired/revoked token or wrong file). "
                   "Regenerate them at " + SETTINGS_URL)
        emit(report, RC_SETUP)
    report["ok"] = True
    if not report["python"]["meets_minimum"]:
        report["warnings"] = [f"kaggle-cli officially requires Python {MIN_PY[0]}.{MIN_PY[1]}+; "
                              f"you have {report['python']['version']}"]
    emit(report, RC_OK)


def cmd_setup(a) -> None:
    steps_done = []
    py = python_for_kaggle()
    ver = kaggle_installed()
    if not ver or a.upgrade:
        log("Installing/upgrading kaggle via pip ...")
        cmd = [py, "-m", "pip", "install", "--upgrade", "--quiet", "kaggle"]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            emit({"ok": False, "reason": "pip_install_failed", "cli_output": clean_lines(p.stdout + p.stderr)[-30:],
                  "next_steps": [f"Run manually: {' '.join(cmd)}"]}, RC_CLI)
        ver = kaggle_installed()
        if not ver:
            emit({"ok": False, "reason": "kaggle_not_importable_after_install",
                  "next_steps": [f"Check that '{py} -m kaggle --version' works; set KAGGLE_SUBMIT_PYTHON to the "
                                 "interpreter that has kaggle installed."]}, RC_CLI)
        steps_done.append(f"installed kaggle {ver}")
    else:
        steps_done.append(f"kaggle {ver} already installed")

    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)

    if a.kaggle_json:
        src = Path(a.kaggle_json).expanduser()
        if not src.is_file():
            emit({"ok": False, "reason": "file_not_found", "path": str(src)}, RC_PRECONDITION)
        try:
            j = json.loads(src.read_text(encoding="utf-8-sig"))
            assert j.get("username") and j.get("key")
        except Exception:  # noqa: BLE001
            emit({"ok": False, "reason": "not_a_kaggle_json", "path": str(src),
                  "next_steps": [f"Expected a JSON file with 'username' and 'key' downloaded from {SETTINGS_URL}"]},
                 RC_PRECONDITION)
        dst = d / "kaggle.json"
        if dst.exists() and not a.force:
            emit({"ok": False, "reason": "kaggle_json_exists", "path": str(dst),
                  "next_steps": ["Re-run with --force to replace it."]}, RC_PRECONDITION)
        shutil.move(str(src), str(dst))
        chmod_600(dst)
        steps_done.append(f"moved {src.name} -> {dst}")

    if a.token_file:
        src = Path(a.token_file).expanduser()
        if not src.is_file():
            emit({"ok": False, "reason": "file_not_found", "path": str(src)}, RC_PRECONDITION)
        token = src.read_text(encoding="utf-8-sig").strip()
        if not token or "\n" in token or " " in token:
            emit({"ok": False, "reason": "token_file_malformed",
                  "next_steps": ["The file must contain exactly the token string on one line."]}, RC_PRECONDITION)
        dst = d / "access_token"
        if dst.exists() and not a.force:
            emit({"ok": False, "reason": "access_token_exists", "path": str(dst),
                  "next_steps": ["Re-run with --force to replace it."]}, RC_PRECONDITION)
        dst.write_text(token + "\n", encoding="utf-8")
        chmod_600(dst)
        if a.delete_source:
            src.unlink()
        steps_done.append(f"wrote token from {src.name} -> {dst}")

    if a.oauth:
        emit({"ok": False, "reason": "oauth_requires_user_terminal", "steps_done": steps_done,
              "next_steps": [f"Run this in your own terminal, then re-run doctor:  {py} -m kaggle auth login",
                             "Headless machine? add --no-launch-browser and open the printed URL elsewhere."]},
             RC_SETUP)

    auth = live_auth_check()
    if not auth["ok"]:
        emit({"ok": False, "reason": "not_authenticated", "steps_done": steps_done,
              "credentials": detect_credentials(), "auth_detail": auth["detail"],
              "next_steps": setup_instructions()}, RC_SETUP)
    steps_done.append(f"authenticated as {auth['username'] or '(unknown)'}")

    if a.competition:
        rc, out, err = run_kaggle(["config", "set", "-n", "competition", "-v", a.competition])
        if rc == 0:
            steps_done.append(f"default competition = {a.competition}")
        else:
            steps_done.append("WARN could not set default competition: " + " ".join(clean_lines(out + err)[-3:]))

    if a.download_path:
        target = str(Path(a.download_path).expanduser().resolve())
        rc, out, err = run_kaggle(["config", "set", "-n", "path", "-v", target])
        if rc == 0:
            steps_done.append(f"default download path = {target}")

    emit({"ok": True, "kaggle_version": ver, "username": auth["username"], "config_dir": str(d),
          "features": cli_features(), "steps_done": steps_done}, RC_OK)


def get_limits(comp: str) -> dict | None:
    rc, out, _ = run_kaggle(["competitions", "submission-limits", comp, "--json"], timeout=120)
    if rc != 0:
        return None
    j = parse_json_output(out)
    if not isinstance(j, dict):
        return None
    norm = {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): v for k, v in j.items()}
    return {"today": norm.get("num_today"), "total": norm.get("num_total"),
            "remaining_today": norm.get("num_allowed_now"), "limited_by_total": norm.get("limited_by_total")}


STATUS_MAP = {"0": "PENDING", "1": "COMPLETE", "2": "ERROR"}


def normalize_submission(s: dict) -> dict:
    st = str(s.get("status", "")).split(".")[-1].upper()
    st = STATUS_MAP.get(st, st)
    return {"ref": s.get("ref"), "fileName": s.get("fileName"), "date": s.get("date"),
            "description": s.get("description"), "status": st, "publicScore": s.get("publicScore"),
            "privateScore": s.get("privateScore"), "errorDescription": s.get("errorDescription")}


def list_submissions(comp: str, n: int = 20) -> list[dict]:
    rc, out, err = run_kaggle(["competitions", "submissions", comp, "--format", "json", "--page-size", str(n)],
                              timeout=180)
    if rc != 0:
        fail(rc, out, err, {"competition": comp})
    j = parse_json_output(out)
    if j is None:
        return []
    items = j if isinstance(j, list) else j.get("submissions", [])
    return [normalize_submission(s) for s in items]


def cmd_info(a) -> None:
    comp = a.competition
    result = {"ok": True, "competition": comp, "url": f"https://www.kaggle.com/competitions/{comp}"}
    rc, out, err = run_kaggle(["competitions", "list", "-s", comp, "--format", "json", "--page-size", "50"],
                              timeout=120)
    if rc != 0:
        fail(rc, out, err, {"competition": comp})
    listing = parse_json_output(out) or []
    meta = [c for c in listing if str(c.get("ref", "")).rstrip("/").split("/")[-1] == comp]
    result["meta"] = meta[0] if meta else None
    if result["meta"] is None:
        result["warnings"] = ["Competition not in the general listing (inClass/private?). "
                              "Continuing with files/limits."]

    rc, out, err = run_kaggle(["competitions", "files", comp, "--format", "json", "--page-size", "200"], timeout=180)
    if rc != 0:
        fail(rc, out, err, {"competition": comp,
                            "hint": "403 here almost always means the rules have not been accepted yet."})
    files = parse_json_output(out) or []
    if isinstance(files, dict):
        files = files.get("files", [])
    result["files"] = files
    names = [f.get("name", "") for f in files]
    sample = ([n for n in names if re.search(r"sample.*submission|submission.*sample", n, re.I)]
              or [n for n in names if re.search(r"submission", n, re.I)])
    result["sample_submission_file"] = sample[0] if sample else None
    result["limits"] = get_limits(comp)
    result["recent_submissions"] = list_submissions(comp, 5)
    if a.pages:
        rc, out, _ = run_kaggle(["competitions", "pages", comp, "--page-name", "evaluation", "--content"],
                                timeout=120)
        result["evaluation_page"] = "\n".join(clean_lines(out))[:4000] if rc == 0 else None
    emit(result, RC_OK)


def cmd_download(a) -> None:
    comp = a.competition
    dest = Path(a.path).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    args = ["competitions", "download", comp, "-p", str(dest)]
    if a.file:
        args += ["-f", a.file]
    if a.force:
        args += ["-o"]
    rc, out, err = run_kaggle(args, timeout=a.timeout)
    if rc != 0:
        fail(rc, out, err, {"competition": comp, "hint": "403 = accept the rules first; then retry."})
    extracted = []
    if a.unzip:
        for z in sorted(dest.glob("*.zip")):
            log(f"unzipping {z.name}")
            with zipfile.ZipFile(z) as zf:
                zf.extractall(dest)
                extracted += zf.namelist()
            z.unlink()
    files = sorted(p.name for p in dest.iterdir() if p.is_file())
    sample = [n for n in files if re.search(r"sample.*submission|submission.*sample", n, re.I)]
    emit({"ok": True, "competition": comp, "path": str(dest), "files": files, "extracted": extracted,
          "sample_submission_file": str(dest / sample[0]) if sample else None,
          "cli_output": clean_lines(out)[-5:]}, RC_OK)


# --------------------------------------------------------------------------- validation


def read_csv_all(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        rows = list(reader)
    return header, rows


def validate_submission(sub: Path, sample: Path | None, id_col: str | None = None) -> dict:
    problems, warnings, stats = [], [], {}
    if not sub.is_file():
        return {"ok": False, "problems": [f"file not found: {sub}"], "warnings": [], "stats": {}}
    stats["size_bytes"] = sub.stat().st_size
    if stats["size_bytes"] == 0:
        return {"ok": False, "problems": ["file is empty"], "warnings": [], "stats": stats}
    try:
        header, rows = read_csv_all(sub)
    except UnicodeDecodeError as e:
        return {"ok": False, "problems": [f"not UTF-8 decodable: {e}"], "warnings": [], "stats": stats}
    except csv.Error as e:
        return {"ok": False, "problems": [f"CSV parse error: {e}"], "warnings": [], "stats": stats}
    if not header:
        return {"ok": False, "problems": ["no header row"], "warnings": [], "stats": stats}
    stats["columns"] = header
    stats["rows"] = len(rows)
    width = len(header)
    ragged = sum(1 for r in rows if len(r) != width)
    if ragged:
        problems.append(f"{ragged} rows have a different number of fields than the header ({width})")
    empties = sum(1 for r in rows for v in r if v.strip() == "" or v.strip().lower() in ("nan", "none", "null"))
    if empties:
        problems.append(f"{empties} empty/NaN cells found")
    idx = 0
    if id_col and id_col in header:
        idx = header.index(id_col)
    elif sample is not None and sample.is_file():
        # The sample's first column is the id column by Kaggle convention, even if the user reordered columns.
        try:
            with sample.open("r", encoding="utf-8-sig", newline="") as fh:
                first = next(csv.reader(fh), None)
            if first and first[0] in header:
                idx = header.index(first[0])
        except (OSError, csv.Error):
            pass
    ids = [r[idx] for r in rows if len(r) > idx]
    dup = len(ids) - len(set(ids))
    if dup:
        problems.append(f"{dup} duplicate values in id column '{header[idx]}'")
    if sample is not None:
        if not sample.is_file():
            warnings.append(f"sample file not found, skipped comparison: {sample}")
        else:
            sh, srows = read_csv_all(sample)
            stats["sample_columns"] = sh
            stats["sample_rows"] = len(srows)
            if sh != header:
                if sorted(sh) == sorted(header):
                    warnings.append(f"columns match but order differs: sample={sh} submission={header}")
                else:
                    problems.append(f"header mismatch: sample={sh} submission={header}")
            if len(srows) != len(rows):
                problems.append(f"row count mismatch: sample={len(srows)} submission={len(rows)}")
            sidx = sh.index(header[idx]) if header[idx] in sh else 0
            sids = set(r[sidx] for r in srows if len(r) > sidx)
            missing = sids - set(ids)
            extra = set(ids) - sids
            if missing:
                problems.append(f"{len(missing)} ids from sample are missing (e.g. {sorted(missing)[:3]})")
            if extra:
                problems.append(f"{len(extra)} ids not present in sample (e.g. {sorted(extra)[:3]})")
    return {"ok": not problems, "problems": problems, "warnings": warnings, "stats": stats}


def find_sample(near: Path) -> Path | None:
    pats = ["*sample*submission*.csv", "*submission*sample*.csv"]
    dirs = [near.parent, near.parent / "data", Path.cwd(), Path.cwd() / "data", Path.cwd() / "input"]
    for d in dirs:
        for pat in pats:
            hits = [h for h in sorted(glob.glob(str(d / pat))) if Path(h).resolve() != near.resolve()]
            if hits:
                return Path(hits[0])
    return None


def cmd_validate(a) -> None:
    sub = Path(a.file).expanduser()
    sample = Path(a.sample).expanduser() if a.sample else find_sample(sub)
    res = validate_submission(sub, sample, a.id_col)
    res.update({"file": str(sub), "sample": str(sample) if sample else None})
    emit(res, RC_OK if res["ok"] else RC_PRECONDITION)


# --------------------------------------------------------------------------- submit / status


def poll_until_scored(comp: str, ref, wait: int, poll_max: int, match: dict | None = None) -> dict | None:
    """Poll `submissions` until the target reaches COMPLETE/ERROR or `wait` seconds pass (0 = forever)."""
    start = time.time()
    interval = 5
    while True:
        subs = list_submissions(comp, 20)
        target = None
        if ref is not None:
            target = next((s for s in subs if str(s["ref"]) == str(ref)), None)
        elif match:
            target = next((s for s in subs if all(s.get(k) == v for k, v in match.items())), None)
        if target and target["status"] in ("COMPLETE", "ERROR"):
            return target
        if wait and (time.time() - start) > wait:
            return target
        log(f"status={target['status'] if target else 'not visible yet'}; sleeping {interval}s")
        time.sleep(interval)
        interval = min(poll_max, int(interval * 1.6))


def append_log(path: Path, record: dict) -> None:
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        log(f"WARN could not write log {path}: {e}")


def build_submit_args(a, comp: str, sub_path: Path | None) -> list[str]:
    args = ["competitions", "submit", comp, "-m", a.message]
    if a.kernel:
        args += ["-k", a.kernel, "-f", a.file, "-v", str(a.version)]
    else:
        args += ["-f", str(sub_path.resolve())]
    if a.sandbox:
        args.append("--sandbox")
    return args


def cmd_submit(a) -> None:
    comp = a.competition
    is_code = bool(a.kernel)
    result: dict = {"ok": False, "competition": comp, "message": a.message, "started": now_iso()}
    sub_path = None
    if not is_code:
        sub_path = Path(a.file).expanduser()
        result["file"] = str(sub_path.resolve()) if sub_path.exists() else str(sub_path)
        if not sub_path.is_file():
            emit({**result, "reason": "file_not_found"}, RC_PRECONDITION)
        if not a.no_validate:
            sample = Path(a.sample).expanduser() if a.sample else find_sample(sub_path)
            v = validate_submission(sub_path, sample, a.id_col)
            result["validation"] = {**v, "sample": str(sample) if sample else None}
            if not v["ok"] and not a.force:
                result["reason"] = "validation_failed"
                result["next_steps"] = ["Fix the file, or re-run with --force to submit anyway, or --no-validate."]
                emit(result, RC_PRECONDITION)
    else:
        if not (a.file and a.version):
            emit({**result, "reason": "code_competition_needs_file_and_version",
                  "next_steps": ["Provide -k user/kernel -f <output file name> -v <kernel version>"]},
                 RC_PRECONDITION)
        result["kernel"] = a.kernel
        result["kernel_version"] = a.version
        result["file"] = a.file

    limits = get_limits(comp)
    result["limits_before"] = limits
    if limits and limits.get("remaining_today") == 0 and not a.force:
        result["reason"] = "daily_submission_limit_reached"
        result["next_steps"] = ["Wait for the daily reset (UTC midnight) or re-run with --force if you believe "
                                "the count is stale."]
        emit(result, RC_PRECONDITION)

    if a.dry_run:
        result.update({"ok": True, "dry_run": True,
                       "would_run": "kaggle " + " ".join(build_submit_args(a, comp, sub_path))})
        emit(result, RC_OK)

    before_refs = {str(s["ref"]) for s in list_submissions(comp, 20)}
    rc, out, err = run_kaggle(build_submit_args(a, comp, sub_path), timeout=a.timeout)
    result["cli_output"] = clean_lines(out)[-10:]
    blob = out + err
    if (rc != 0 or "Could not find competition" in blob or "Could not submit" in blob
            or "Authentication required" in blob):
        code, reason, steps = classify_failure(rc, out, err)
        result.update({"reason": reason, "next_steps": steps, "cli_stderr": clean_lines(err)[-15:]})
        emit(result, code)

    ref = None
    m = re.search(r"Submission ref:\s*(\d+)", blob)
    if m:
        ref = m.group(1)
    else:
        # PyPI 2.2.x prints only the server message; identify the new row by diffing refs.
        for _ in range(6):
            newer = [s for s in list_submissions(comp, 20) if str(s["ref"]) not in before_refs]
            if newer:
                fname = Path(a.file).name
                pick = [s for s in newer if s.get("fileName") == fname] or newer
                ref = pick[0]["ref"]
                break
            time.sleep(5)
    result["submission_ref"] = ref
    m = re.search(r"(\d+) submissions remaining today", blob)
    if m:
        result["remaining_today"] = int(m.group(1))

    final = None
    if a.wait is not None:
        final = poll_until_scored(comp, ref, a.wait, a.poll_interval,
                                  match=None if ref else {"description": a.message})
    elif ref is not None:
        final = next((s for s in list_submissions(comp, 20) if str(s["ref"]) == str(ref)), None)
    result["submission"] = final
    result["finished"] = now_iso()
    result["ok"] = True
    if final and final["status"] == "ERROR":
        result["ok"] = False
        result["reason"] = "scoring_error"
        result["next_steps"] = ["Read submission.errorDescription; usually a format problem the sample-file "
                                "check cannot catch."]
    elif a.wait is not None and (final is None or final["status"] != "COMPLETE"):
        result["ok"] = False
        result["reason"] = "wait_timeout"
        result["next_steps"] = [f"python \"{Path(__file__).name}\" status {comp} --ref {ref} --wait 0"]

    if not a.no_log:
        append_log(Path(a.log).expanduser(), {
            "time": result["started"], "competition": comp, "file": result.get("file"), "message": a.message,
            "ref": ref, "status": final["status"] if final else None,
            "publicScore": final.get("publicScore") if final else None,
            "privateScore": final.get("privateScore") if final else None,
            "kernel": a.kernel, "kernel_version": a.version, "note": a.note,
        })
        result["log"] = str(Path(a.log).expanduser().resolve())
    if result["ok"]:
        emit(result, RC_OK)
    emit(result, RC_PRECONDITION if result.get("reason") == "scoring_error" else RC_CLI)


def cmd_status(a) -> None:
    comp = a.competition
    if a.wait is not None:
        target = poll_until_scored(comp, a.ref, a.wait, a.poll_interval)
    else:
        subs = list_submissions(comp, 20)
        if a.ref:
            target = next((s for s in subs if str(s["ref"]) == str(a.ref)), None)
        else:
            target = subs[0] if subs else None
    if target is None:
        emit({"ok": False, "reason": "submission_not_found", "competition": comp, "ref": a.ref}, RC_PRECONDITION)
    ok = target["status"] == "COMPLETE"
    emit({"ok": ok, "competition": comp, "submission": target, "limits": get_limits(comp)},
         RC_OK if ok else (RC_PRECONDITION if target["status"] == "ERROR" else RC_CLI))


def cmd_submissions(a) -> None:
    subs = list_submissions(a.competition, a.n)
    scored = [s for s in subs if s["status"] == "COMPLETE" and s.get("publicScore") not in (None, "")]
    best = None
    if scored:
        try:
            pick = min if a.lower_is_better else max
            best = pick(scored, key=lambda s: float(s["publicScore"]))
        except ValueError:
            best = None
    emit({"ok": True, "competition": a.competition, "count": len(subs), "best_public": best,
          "limits": get_limits(a.competition), "submissions": subs}, RC_OK)


def cmd_leaderboard(a) -> None:
    rc, out, err = run_kaggle(["competitions", "leaderboard", a.competition, "--show", "--format", "json",
                               "--page-size", str(a.n)], timeout=180)
    if rc != 0:
        fail(rc, out, err, {"competition": a.competition})
    emit({"ok": True, "competition": a.competition, "top": parse_json_output(out) or []}, RC_OK)


def cmd_log(a) -> None:
    p = Path(a.log).expanduser()
    if not p.is_file():
        emit({"ok": True, "log": str(p), "entries": []}, RC_OK)
    entries = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            try:
                entries.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
    if a.competition:
        entries = [e for e in entries if e.get("competition") == a.competition]
    emit({"ok": True, "log": str(p), "entries": entries[-a.n:]}, RC_OK)


# --------------------------------------------------------------------------- main


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("doctor", help="check python, kaggle CLI, credentials, live auth; print next steps")
    p.set_defaults(fn=cmd_doctor)

    p = sp.add_parser("setup", help="first-run setup: pip install kaggle, place credentials, verify, set defaults")
    p.add_argument("--kaggle-json", help="path to the legacy kaggle.json downloaded from " + SETTINGS_URL
                                         + " (file is MOVED into the config dir)")
    p.add_argument("--token-file", help="path to a text file containing an API token generated at " + SETTINGS_URL)
    p.add_argument("--delete-source", action="store_true", help="delete --token-file after copying")
    p.add_argument("--oauth", action="store_true",
                   help="print the `kaggle auth login` command for the user to run themselves")
    p.add_argument("--competition", help="also set the default competition slug")
    p.add_argument("--download-path", help="also set the default download folder")
    p.add_argument("--upgrade", action="store_true", help="pip install --upgrade kaggle even if present")
    p.add_argument("--force", action="store_true", help="overwrite existing credential files")
    p.set_defaults(fn=cmd_setup)

    p = sp.add_parser("info", help="competition metadata, data files, sample file name, submission limits, "
                                   "recent submissions")
    p.add_argument("competition")
    p.add_argument("--pages", action="store_true", help="also fetch the evaluation page text")
    p.set_defaults(fn=cmd_info)

    p = sp.add_parser("download", help="download competition data (all files or -f one), optional unzip")
    p.add_argument("competition")
    p.add_argument("-p", "--path", default="data")
    p.add_argument("-f", "--file")
    p.add_argument("--unzip", action="store_true")
    p.add_argument("-o", "--force", action="store_true")
    p.add_argument("--timeout", type=int, default=3600)
    p.set_defaults(fn=cmd_download)

    p = sp.add_parser("validate", help="check a submission CSV (header/rows/ids/NaN) against the sample submission")
    p.add_argument("file")
    p.add_argument("--sample", help="sample submission csv; auto-detected next to the file / ./data if omitted")
    p.add_argument("--id-col", help="id column name (default: first column)")
    p.set_defaults(fn=cmd_validate)

    p = sp.add_parser("submit", help="validate -> check limits -> submit -> (wait for score) -> log")
    p.add_argument("competition")
    p.add_argument("-f", "--file", required=True,
                   help="submission file path, or output file NAME for code competitions (-k)")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("-k", "--kernel", help="code competition: user/kernel-slug")
    p.add_argument("-v", "--version", help="code competition: kernel version number")
    p.add_argument("--sample", help="sample submission csv for validation (auto-detected if omitted)")
    p.add_argument("--id-col")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--force", action="store_true", help="submit even if validation fails or the daily limit reads 0")
    p.add_argument("--wait", type=int, nargs="?", const=0, default=None,
                   help="wait for scoring; seconds, bare flag = no limit")
    p.add_argument("--poll-interval", type=int, default=60)
    p.add_argument("--sandbox", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=int, default=3600, help="upload timeout seconds")
    p.add_argument("--log", default="kaggle_submissions.jsonl")
    p.add_argument("--no-log", action="store_true")
    p.add_argument("--note", help="free text stored in the local log (e.g. CV score, config)")
    p.set_defaults(fn=cmd_submit)

    p = sp.add_parser("status", help="status/score of one submission (--ref) or the latest; --wait polls")
    p.add_argument("competition")
    p.add_argument("--ref")
    p.add_argument("--wait", type=int, nargs="?", const=0, default=None)
    p.add_argument("--poll-interval", type=int, default=60)
    p.set_defaults(fn=cmd_status)

    p = sp.add_parser("submissions", help="list your submissions with the best public score")
    p.add_argument("competition")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--lower-is-better", action="store_true")
    p.set_defaults(fn=cmd_submissions)

    p = sp.add_parser("leaderboard", help="top of the public leaderboard")
    p.add_argument("competition")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_leaderboard)

    p = sp.add_parser("log", help="show the local submission log (kaggle_submissions.jsonl)")
    p.add_argument("--competition")
    p.add_argument("--log", default="kaggle_submissions.jsonl")
    p.add_argument("-n", type=int, default=20)
    p.set_defaults(fn=cmd_log)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
