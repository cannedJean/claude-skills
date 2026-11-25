"""Bring SSAFY course repos local in the data_track layout.

    python -I sync.py --root %USERPROFILE%\\Desktop\\data_track [--lab FILE] [--project FILE]
                      [--only lm,ws,hw] [--dry-run]

--lab / --project default to the newest ssafy_lab_manifest*.json / ssafy_project_manifest*.json
in the user's Downloads folder. Consumed manifests are moved to ~/.ssafy-materials/manifests/.

Layout:
  lm/<group project>                    clone of lab.ssafy.com/<group>/<project>
  ws|hw/<subject>/<repo>                clone of lab.ssafy.com/<user>/<repo>
  ws|hw/<subject>/<repo>/README.md      problem statement (PROBLEM.md if the repo has its own README.md)

Safety: never prompts for credentials (GIT_TERMINAL_PROMPT=0, GCM_INTERACTIVE=never), never
resets, stashes, deletes or pushes. An existing clone is only fast-forwarded when its working
tree is clean; anything else is skipped and reported.
"""
import argparse, builtins, datetime, json, os, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "<!-- ssafy-materials:statement -->"
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
PRACTICE = [
    re.compile(r"^(?P<prefix>.+?)_(?P<kind>ws|hw)_(?P<week>\d+)_(?P<n>\w+)$"),
    re.compile(r"^(?P<prefix>chapter)_(?P<week>\d+)(?:_\d+)*_(?P<n>\d+)_(?P<kind>ws|hw)$"),  # chapter_2_3_ws, chapter_4_2_1_ws
]
AUTH_RE = re.compile(r"Authentication failed|could not read Username|terminal prompts disabled|HTTP Basic: Access denied|403", re.I)


def git(*args, cwd=None):
    p = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout + p.stderr).strip()


def newest(pattern):
    dl = Path.home() / "Downloads"
    files = sorted(dl.glob(pattern), key=lambda f: f.stat().st_mtime, reverse=True)
    return files[0] if files else None


def archive(path):
    dest = Path.home() / ".ssafy-materials" / "manifests"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = dest / f"{path.stem}-{stamp}{path.suffix}"
    shutil.move(str(path), target)
    return target


def sync_repo(url, dest, dry):
    """Return (action, detail). action in cloned|updated|uptodate|skipped|failed|auth|would-clone|would-update."""
    if not dest.exists():
        if dry:
            return "would-clone", url
        made_parent = not dest.parent.exists()
        dest.parent.mkdir(parents=True, exist_ok=True)
        rc, out = git("clone", "--quiet", url, str(dest))
        if rc:
            # remove only the empty folders this call created
            if dest.exists() and not any(dest.iterdir()):
                dest.rmdir()
            if made_parent and not any(dest.parent.iterdir()):
                dest.parent.rmdir()
            return ("auth" if AUTH_RE.search(out) else "failed"), out.splitlines()[-1] if out else f"exit {rc}"
        return "cloned", ""
    if not (dest / ".git").exists():
        return "skipped", "folder exists but is not a git clone"
    rc, origin = git("remote", "get-url", "origin", cwd=dest)
    if rc or origin.rstrip("/").removesuffix(".git").lower() != url.rstrip("/").removesuffix(".git").lower():
        return "skipped", f"origin is {origin or 'missing'}"
    rc, status = git("status", "--porcelain", cwd=dest)
    if status:
        return "skipped", f"local changes ({len(status.splitlines())} files): left untouched"
    if dry:
        return "would-update", ""
    rc, before = git("rev-parse", "HEAD", cwd=dest)
    rc, out = git("pull", "--ff-only", "--quiet", cwd=dest)
    if rc:
        return ("auth" if AUTH_RE.search(out) else "skipped"), (out.splitlines()[-1] if out else f"exit {rc}")
    rc, after = git("rev-parse", "HEAD", cwd=dest)
    return ("updated" if before != after else "uptodate"), ""


def write_statement(repo_dir, prob, dry):
    readme = repo_dir / "README.md"
    own_readme = readme.exists() and MARK not in readme.read_text(encoding="utf-8", errors="replace")
    target = repo_dir / ("PROBLEM.md" if own_readme else "README.md")
    kind = {"ws": "실습 (ws)", "hw": "과제 (hw)"}.get(prob.get("kind"), prob.get("kind") or "?")
    body = "\n".join([
        MARK,
        f"# {prob['title']}",
        "",
        f"- 과정: {prob['course']} / {prob['chapter']}",
        f"- 유형: {kind} {prob.get('level', '')}".rstrip(),
        f"- 출처: {prob.get('url', 'project.ssafy.com')}",
        f"- 수집: {datetime.date.today().isoformat()} (개인 학습용 사본, 외부 공유 금지)",
        "",
        prob.get("statement", "").strip(),
        "",
    ])
    if dry:
        return target.name
    target.write_text(body, encoding="utf-8")
    # keep the statement out of `git status` so later fast-forwards stay clean
    exclude = repo_dir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    if f"/{target.name}" not in lines:
        exclude.write_text("\n".join(lines + [f"/{target.name}"]) + "\n", encoding="utf-8")
    return target.name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--lab")
    ap.add_argument("--project", nargs="*", help="project manifests to merge (default: all in Downloads)")
    ap.add_argument("--only", default="lm,ws,hw")
    ap.add_argument("--map", default=str(HERE / "subject_map.json"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-manifests", action="store_true", help="do not move manifests out of Downloads")
    a = ap.parse_args()
    root, only, dry = Path(a.root), set(a.only.split(",")), a.dry_run
    if not root.is_dir():
        sys.exit(f"--root {root} does not exist")
    lab_path = Path(a.lab) if a.lab else newest("ssafy_lab_manifest*.json")
    if not lab_path or not lab_path.exists():
        sys.exit("no ssafy_lab_manifest*.json: run scripts/lab_lister.js in Chrome first")
    lab = json.loads(lab_path.read_text(encoding="utf-8"))
    # every project manifest is merged (a later run for one course fills gaps of an earlier one)
    dl = Path.home() / "Downloads"
    proj_paths = [Path(p) for p in a.project] if a.project else sorted(dl.glob("ssafy_project_manifest*.json"), key=lambda f: f.stat().st_mtime)
    merged = {}
    for pp in proj_paths:
        for p in json.loads(pp.read_text(encoding="utf-8"))["problems"]:
            # the collector's row key exists even when the problem page failed to open (no problemId yet)
            key = ((p["courseId"], p["chapterId"], p["listIndex"]) if all(k in p for k in ("courseId", "chapterId", "listIndex"))
                   else p.get("problemId") or (p["course"], p["title"]))
            if p.get("repo") or key not in merged:  # newer runs win, but never lose a known repo
                merged[key] = p
    proj = {"problems": list(merged.values())}
    smap = json.loads(Path(a.map).read_text(encoding="utf-8"))
    # user additions live outside the skill folder and win over the defaults
    user_map = Path.home() / ".ssafy-materials" / "subject_map.json"
    if user_map.exists():
        extra = json.loads(user_map.read_text(encoding="utf-8"))
        for key in ("course", "prefix"):
            smap[key].update(extra.get(key, {}))
    by_repo = {p["repo"].split("/")[-1]: p for p in proj["problems"] if p.get("repo")}

    jobs, unknown, empty, mismatch = [], set(), [], []
    if "lm" in only:
        for p in lab["groupProjects"]:
            if p.get("empty_repo"):
                empty.append(p["path"])
            else:
                jobs.append(("lm", p["path"], p["http_url_to_repo"], root / "lm" / p["path"], None))
    for p in lab["ownProjects"]:
        m = next((r.match(p["path"]) for r in PRACTICE if r.match(p["path"])), None)
        prob = by_repo.get(p["path"])
        if not m and not prob:
            continue  # not a practice repo (team projects etc.)
        # the site's 유형 decides: 실습 -> ws, 과제 -> hw; the repo name is only the fallback
        kind = prob["kind"] if prob and prob.get("kind") in ("ws", "hw") else (m and m["kind"])
        if kind not in only:
            continue
        prefix = m["prefix"] if m else p["path"].split("_")[0]
        if m and prob and m["kind"] != kind:
            mismatch.append((p["path"], m["kind"], kind))
        subject = ((prob and smap["course"].get(prob["course"])) or smap["prefix"].get(prefix)
                   or smap["prefix"].get(prefix.rstrip("0123456789")))  # data_science3 -> data_science
        if not subject:
            subject = prefix
            unknown.add(prefix)
        jobs.append((kind, p["path"], p["http_url_to_repo"], root / kind / subject / p["path"], prob))

    results, auth_fails = [], 0
    for kind, name, url, dest, prob in jobs:
        if auth_fails >= 3:
            results.append((kind, name, "not-run", "stopped after 3 authentication failures", dest, ""))
            continue
        action, detail = sync_repo(url, dest, dry)
        auth_fails = auth_fails + 1 if action == "auth" else 0
        stmt = ""
        # statements go only into clones this run owns (not into skipped or failed folders)
        if prob and prob.get("statement") and action in ("cloned", "updated", "uptodate", "would-clone", "would-update"):
            stmt = write_statement(dest, prob, dry)
        results.append((kind, name, action, detail, dest, stmt))

    # report: printed and also saved as UTF-8 (Windows consoles often garble the Korean)
    report_path = Path.home() / ".ssafy-materials" / "last_report.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines_out = []
    def print(*parts):  # noqa: A001 - every report line is also kept for the file
        line = " ".join(str(p) for p in parts)
        lines_out.append(line)
        builtins.print(line)
    counts = {}
    for r in results:
        counts[r[2]] = counts.get(r[2], 0) + 1
    print(f"root: {root}{'  (dry run: nothing changed)' if dry else ''}")
    print("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for kind, name, action, detail, dest, stmt in results:
        if action not in ("uptodate", "updated", "cloned") or detail:
            print(f"  [{action}] {kind} {name} -> {dest}" + (f"  ({detail})" if detail else ""))
    stmts = sum(1 for r in results if r[5])
    print(f"statements written: {stmts}")
    own_names = {p["path"] for p in lab["ownProjects"]}
    no_repo = [p for p in proj["problems"] if not p.get("repo")]
    missing = [p for p in proj["problems"] if p.get("repo") and p["repo"].split("/")[-1] not in own_names]
    if no_repo:
        print(f"problems without a repo ({len(no_repo)}): " + "; ".join(f"{p['course']} {p['title']}" + (f" [{p['error']}]" if p.get('error') else "") for p in no_repo[:20]) + (" ..." if len(no_repo) > 20 else ""))
    if missing:
        print(f"repos started after the lab manifest was taken ({len(missing)}): re-run lab_lister.js, then sync again")
    if empty:
        print("empty group repos (nothing to clone yet): " + ", ".join(empty))
    if unknown:
        print(f"unmapped prefixes (used as folder names; to map them, add them to {user_map}): " + ", ".join(sorted(unknown)))
    if mismatch:
        print("repo name says ws/hw but the site's 유형 says otherwise (placed by 유형): " + ", ".join(f"{n} ({a}->{b})" for n, a, b in mismatch))
    other = [p["path"] for p in lab["ownProjects"] if not any(r.match(p["path"]) for r in PRACTICE) and p["path"] not in by_repo]
    if other:
        print("own projects that are not ws/hw practice repos (not synced): " + ", ".join(other))
    if not dry and not a.keep_manifests:
        for f in {lab_path, *proj_paths}:
            if f and f.exists() and f.parent == dl:
                print(f"manifest moved to {archive(f)}")
    report_path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    sys.stdout.write(f"full report (UTF-8): {report_path}\n")
    return 1 if counts.get("auth") or counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
