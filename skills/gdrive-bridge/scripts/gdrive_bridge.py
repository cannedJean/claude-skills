#!/usr/bin/env python3
"""gdrive-bridge: move files between the local machine and Google Colab through Google Drive.

Google Drive for Desktop mounts the account's Drive as a local folder
(Windows: a drive letter such as G:\\, macOS: ~/Library/CloudStorage/GoogleDrive-<email>/).
Colab mounts the same Drive at /content/drive.  This tool maps between the two views:

    local   <root>/<My Drive>/foo/bar.zip        <->  colab  /content/drive/MyDrive/foo/bar.zip
    local   <root>/<Shared drives>/T/x.csv       <->  colab  /content/drive/Shareddrives/T/x.csv

Drive paths given to this tool use the Colab spelling minus the "/content/drive/" prefix:
"MyDrive/foo/bar.zip", "Shareddrives/T/x.csv".  A bare "foo/bar.zip" means "MyDrive/foo/bar.zip".
Full Colab paths ("/content/drive/MyDrive/...") are accepted too.

Commands (every command prints one JSON object on stdout; progress goes to stderr):
    status                         detect the mount, print the mapping and the My Drive root listing
    ls DRIVE_PATH [-R] [--depth N]
    push LOCAL... --to DRIVE_DIR [--zip NAME.zip] [--exclude GLOB]... [--overwrite]
    pull DRIVE_PATH [--to LOCAL_DIR] [--wait] [--marker _DONE] [--timeout S] [--interval S]
                    [--include GLOB]... [--exclude GLOB]... [--force] [--no-verify]
    share-id LINK_OR_ID [--local FILE] [--expect-name NAME]
                                   verify a "anyone with the link" share for gdown: public? name? size vs local?
    snippet fetch  DRIVE_PATH [--unzip-to P] [--env VAR] [--no-hash] [--no-manifest]   (Colab UI: drive.mount)
    snippet gdown  FILE_ID    [--unzip-to P] [--env VAR] [--size N] [--sha256 H]        (fresh VM, no mount)
    snippet export DRIVE_DIR  [--src /content/outputs] [--run-id ID] [--no-remount]

Exit codes: 0 ok, 2 verification failed / marker timeout, 3 I/O error, 4 no Drive mount found.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from html import unescape
from pathlib import Path, PurePosixPath

IS_WINDOWS = sys.platform.startswith("win")
MY_DRIVE_NAMES = ["My Drive", "내 드라이브", "マイドライブ", "我的云端硬盘", "Mon Drive", "Meine Ablage", "Mi unidad"]
SHARED_NAMES = ["Shared drives", "공유 드라이브", "共有ドライブ", "Drive partagés", "Geteilte Ablagen", "Unidades compartidas"]
DRIVEFS_SIGNATURE = ".shortcut-targets-by-id"
DEFAULT_EXCLUDES = [".git", "__pycache__", ".ipynb_checkpoints", ".DS_Store", "Thumbs.db", "desktop.ini", "*.pyc",
                    ".pytest_cache", "*.part"]
CHUNK = 8 * 1024 * 1024
MANIFEST_DIR = "_manifest.json"       # written inside a pushed/exported directory tree
MANIFEST_SUFFIX = ".manifest.json"    # written next to a pushed single file
DEFAULT_MARKER = "_DONE"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(obj: dict, rc: int = 0) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return rc


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def ts(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


# --------------------------------------------------------------------------- mount detection
class Mount:
    def __init__(self, root: Path, my_drive: Path | None, shared: Path | None, how: str):
        self.root, self.my_drive, self.shared, self.how = root, my_drive, shared, how

    def local_for(self, drive_path: str) -> Path:
        kind, rel = split_drive_path(drive_path)
        base = self.my_drive if kind == "MyDrive" else self.shared
        if base is None:
            raise FileNotFoundError(f"{kind} folder not found under {self.root}")
        return base.joinpath(*rel.parts) if rel.parts else base

    def drive_for(self, local: Path) -> str | None:
        local = Path(os.path.abspath(local))
        for base, name in ((self.my_drive, "MyDrive"), (self.shared, "Shareddrives")):
            if base is None:
                continue
            try:
                rel = local.relative_to(base)
            except ValueError:
                continue
            return name + ("/" + rel.as_posix() if rel.parts else "")
        return None

    def as_dict(self) -> dict:
        return {"root": str(self.root),
                "my_drive_local": str(self.my_drive) if self.my_drive else None,
                "shared_drives_local": str(self.shared) if self.shared else None,
                "my_drive_colab": "/content/drive/MyDrive",
                "shared_drives_colab": "/content/drive/Shareddrives",
                "detected_via": self.how}


def split_drive_path(p: str) -> tuple[str, PurePosixPath]:
    s = p.replace("\\", "/").strip()
    # Git Bash / MSYS rewrites "/content/drive/..." into "C:/Program Files/Git/content/drive/..."; tolerate that
    i = s.find("/content/drive/")
    if i >= 0:
        s = s[i + len("/content/drive/"):]
    parts = [x for x in s.strip("/").split("/") if x not in ("", ".")]
    if parts and parts[0] in ("MyDrive", "My Drive"):
        return "MyDrive", PurePosixPath(*parts[1:])
    if parts and parts[0] in ("Shareddrives", "Shared drives"):
        return "Shareddrives", PurePosixPath(*parts[1:])
    return "MyDrive", PurePosixPath(*parts)


def norm_drive_path(p: str) -> str:
    kind, rel = split_drive_path(p)
    return kind + ("/" + rel.as_posix() if rel.parts else "")


def colab_path(drive_path: str) -> str:
    return "/content/drive/" + norm_drive_path(drive_path)


def vm_path(s: str) -> str:
    """Undo Git Bash's rewrite of a '/content/...' argument into 'C:/Program Files/Git/content/...'."""
    i = s.replace("\\", "/").find("/content")
    if i > 0 and s[1:3] in (":/", ":\\"):
        return s.replace("\\", "/")[i:]
    return s


def _classify_root(root: Path) -> tuple[Path | None, Path | None] | None:
    try:
        names = os.listdir(root)
    except Exception:
        return None
    my = next((root / n for n in MY_DRIVE_NAMES if n in names), None)
    sh = next((root / n for n in SHARED_NAMES if n in names), None)
    if my is None and DRIVEFS_SIGNATURE in names:
        for n in sorted(names):  # unknown localisation: first ordinary directory
            if n.startswith(".") or n.startswith("$") or n in SHARED_NAMES:
                continue
            if (root / n).is_dir():
                my = root / n
                break
    if my is None and sh is None:
        return None
    return my, sh


def detect_mount() -> Mount | None:
    env = os.environ.get("GDRIVE_BRIDGE_ROOT")
    if env:
        p = Path(env)
        if p.name in MY_DRIVE_NAMES:
            cls = _classify_root(p.parent) or (p, None)
            return Mount(p.parent, cls[0] or p, cls[1], "GDRIVE_BRIDGE_ROOT")
        cls = _classify_root(p)
        if cls:
            return Mount(p, cls[0], cls[1], "GDRIVE_BRIDGE_ROOT")
        return Mount(p, p, None, "GDRIVE_BRIDGE_ROOT (treated as My Drive)")
    candidates: list[Path] = []
    if IS_WINDOWS:
        candidates += [Path(f"{c}:\\") for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    else:
        cs = Path.home() / "Library" / "CloudStorage"
        if cs.is_dir():
            candidates += sorted(p for p in cs.iterdir() if p.name.startswith("GoogleDrive"))
        candidates += [Path("/Volumes/GoogleDrive"), Path.home() / "Google Drive"]
    for root in candidates:
        try:
            if not root.exists():
                continue
            if IS_WINDOWS and DRIVEFS_SIGNATURE not in os.listdir(root):
                continue
        except Exception:
            continue
        cls = _classify_root(root)
        if cls:
            return Mount(root, cls[0], cls[1], "auto")
    return None


def require_mount() -> Mount:
    m = detect_mount()
    if not m:
        emit({"ok": False, "error": "no Google Drive for Desktop mount found",
              "hint": "install/start Google Drive for Desktop, or set GDRIVE_BRIDGE_ROOT to the mount root "
                      "(e.g. G:\\ on Windows, ~/Library/CloudStorage/GoogleDrive-<email> on macOS)"})
        sys.exit(4)
    return m


# --------------------------------------------------------------------------- helpers
def excluded(rel: PurePosixPath, patterns: list[str]) -> bool:
    return any(any(fnmatch.fnmatch(part, pat) for part in rel.parts) or fnmatch.fnmatch(rel.as_posix(), pat)
               for pat in patterns)


def included(rel: PurePosixPath, patterns: list[str]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatch(rel.name, pat) or fnmatch.fnmatch(rel.as_posix(), pat) for pat in patterns)


def walk_files(base: Path, excludes: list[str], includes: list[str] | None = None):
    """Yield (abs_path, rel) for files under base. Prunes excluded directories early: DriveFS directory
    listings are slow, so never descend into what will be skipped anyway."""
    stack = [Path(base)]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except Exception as e:
            log(f"  ! cannot list {d}: {e}")
            continue
        for e in entries:
            rel = PurePosixPath(Path(e.path).relative_to(base).as_posix())
            if excluded(rel, excludes):
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                stack.append(Path(e.path))
            elif included(rel, includes or []):
                yield Path(e.path), rel


class Progress:
    def __init__(self, label: str, total: int | None = None):
        self.label, self.total, self.done, self.t0, self.last = label, total, 0, time.time(), 0.0

    def add(self, n: int, force: bool = False) -> None:
        self.done += n
        now = time.time()
        if force or now - self.last >= 5:
            self.last = now
            el = max(now - self.t0, 1e-6)
            pct = f" {100 * self.done / self.total:.0f}%" if self.total else ""
            log(f"  {self.label}: {human(self.done)}{pct}  ({human(self.done / el)}/s, {el:.0f}s)")


def copy_hash(src: Path, dst: Path, prog: Progress | None = None) -> tuple[int, str]:
    """Copy src -> dst in one pass while hashing. Writes dst.part and renames at the end so a half-written
    file never sits under its final name (Drive would start uploading it)."""
    h = hashlib.sha256()
    size = 0
    tmp = dst.with_name(dst.name + ".part")
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb", buffering=0) as f, open(tmp, "wb") as g:
        for b in iter(lambda: f.read(CHUNK), b""):
            g.write(b)
            h.update(b)
            size += len(b)
            if prog:
                prog.add(len(b))
    try:
        st = src.stat()
        os.utime(tmp, (st.st_atime, st.st_mtime))
    except Exception:
        pass
    if dst.exists():
        dst.unlink()
    os.replace(tmp, dst)
    return size, h.hexdigest()


def write_json_atomic(path: Path, obj: dict) -> None:
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    if path.exists():
        path.unlink()
    os.replace(tmp, path)


def make_zip(sources: list[Path], out: Path, excludes: list[str]) -> int:
    """Zip files/dirs into out. Directories keep their top-level name inside the archive.
    Already-compressed media is stored, the rest deflated."""
    stored = {".jpg", ".jpeg", ".png", ".webp", ".zip", ".gz", ".mp4", ".safetensors"}
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for src in sources:
            items = [(p, f"{src.name}/{rel.as_posix()}") for p, rel in walk_files(src, excludes)] if src.is_dir() \
                else [(src, src.name)]
            for p, arc in items:
                zf.write(p, arc, compress_type=zipfile.ZIP_STORED if p.suffix.lower() in stored else zipfile.ZIP_DEFLATED)
                count += 1
                if count % 500 == 0:
                    log(f"  zipped {count} files ...")
    return count


def load_manifest(src: Path) -> dict | None:
    cand = (src / MANIFEST_DIR) if src.is_dir() else src.with_name(src.name + MANIFEST_SUFFIX)
    if cand.exists():
        try:
            return json.loads(cand.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"  ! unreadable manifest {cand}: {e}")
    return None


# --------------------------------------------------------------------------- commands
def cmd_status(a) -> int:
    m = detect_mount()
    if not m:
        return emit({"ok": False, "mount": None, "error": "no Google Drive for Desktop mount found",
                     "hint": "install/start Google Drive for Desktop, or set GDRIVE_BRIDGE_ROOT"}, 4)
    listing = []
    if m.my_drive and m.my_drive.is_dir():
        for e in sorted(os.scandir(m.my_drive), key=lambda e: e.name):
            if e.name == "desktop.ini" or e.name.startswith("."):
                continue
            try:
                st = e.stat()
                listing.append({"name": e.name, "type": "dir" if e.is_dir() else "file",
                                "size": None if e.is_dir() else st.st_size, "mtime": ts(st.st_mtime)})
            except Exception:
                listing.append({"name": e.name, "type": "?"})
    try:
        free = human(shutil.disk_usage(m.root).free)
    except Exception:
        free = None
    return emit({"ok": True, "mount": m.as_dict(), "local_cache_free": free, "my_drive_root": listing,
                 "notes": ["Upload progress is not observable here; verify on the Colab side (snippet fetch) "
                           "or via share-id after the file shows up in Drive web.",
                           "This Drive must belong to the same Google account that Colab uses."]})


def cmd_ls(a) -> int:
    m = require_mount()
    dp = norm_drive_path(a.drive_path)
    base = m.local_for(dp)
    if not base.exists():
        return emit({"ok": False, "error": f"not found: {base}", "drive_path": dp}, 3)
    if base.is_file():
        st = base.stat()
        return emit({"ok": True, "drive_path": dp, "local": str(base), "colab": colab_path(dp),
                     "entries": [{"path": base.name, "type": "file", "size": st.st_size, "mtime": ts(st.st_mtime)}]})
    depth = a.depth if a.depth is not None else (None if a.recursive else 0)
    entries, total = [], 0
    stack = [(base, 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            items = sorted(os.scandir(d), key=lambda e: e.name)
        except Exception as e:
            entries.append({"path": str(d), "type": "error", "error": str(e)})
            continue
        for e in items:
            rel = Path(e.path).relative_to(base).as_posix()
            try:
                st = e.stat()
                if e.is_dir():
                    entries.append({"path": rel + "/", "type": "dir", "mtime": ts(st.st_mtime)})
                    if depth is None or lvl + 1 <= depth:
                        stack.append((Path(e.path), lvl + 1))
                else:
                    total += st.st_size
                    entries.append({"path": rel, "type": "file", "size": st.st_size, "mtime": ts(st.st_mtime)})
            except Exception as ex:
                entries.append({"path": rel, "type": "error", "error": str(ex)})
    entries.sort(key=lambda x: x["path"])
    return emit({"ok": True, "drive_path": dp, "local": str(base), "colab": colab_path(dp),
                 "count": len(entries), "total_size": total, "total_size_h": human(total), "entries": entries})


def cmd_push(a) -> int:
    m = require_mount()
    t0 = time.time()
    dest_dir = m.local_for(a.to)
    excludes = DEFAULT_EXCLUDES + (a.exclude or [])
    sources = [Path(s).resolve() for s in a.local]
    for s in sources:
        if not s.exists():
            return emit({"ok": False, "error": f"local source not found: {s}"}, 3)
    dest_dir.mkdir(parents=True, exist_ok=True)
    pushed: list[dict] = []

    def push_file(src: Path, final: Path, extra: dict) -> None:
        prog = Progress(final.name, src.stat().st_size)
        size, digest = copy_hash(src, final, prog)
        prog.add(0, force=True)
        man = final.with_name(final.name + MANIFEST_SUFFIX)
        write_json_atomic(man, {"kind": "file", "name": final.name, "size": size, "sha256": digest,
                                "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": "gdrive-bridge", **extra})
        dp = m.drive_for(final)
        pushed.append({"drive_path": dp, "colab": colab_path(dp), "local": str(final), "size": size,
                       "sha256": digest, "manifest": m.drive_for(man)})

    if a.zip:
        name = a.zip if a.zip.lower().endswith(".zip") else a.zip + ".zip"
        final = dest_dir / name
        if final.exists() and not a.overwrite:
            return emit({"ok": False, "error": f"already exists on Drive: {final} (use --overwrite)"}, 3)
        with tempfile.TemporaryDirectory(prefix="gdrive_bridge_") as td:
            tmp_zip = Path(td) / name
            log(f"zipping {len(sources)} source(s) -> {tmp_zip}")
            count = make_zip(sources, tmp_zip, excludes)
            log(f"  {count} files, {human(tmp_zip.stat().st_size)}; copying to Drive: {final}")
            push_file(tmp_zip, final, {"files_in_zip": count, "sources": [str(s) for s in sources]})
    else:
        for s in sources:
            final = dest_dir / s.name
            if final.exists() and not a.overwrite:
                return emit({"ok": False, "error": f"already exists on Drive: {final} (use --overwrite)"}, 3)
            if s.is_file():
                push_file(s, final, {"sources": [str(s)]})
                continue
            files = list(walk_files(s, excludes))
            total = sum(p.stat().st_size for p, _ in files)
            log(f"copying tree {s} -> {final}: {len(files)} files, {human(total)}")
            prog = Progress(s.name, total)
            entries = []
            for abs_p, rel in files:
                size, digest = copy_hash(abs_p, final.joinpath(*rel.parts), prog)
                entries.append({"path": rel.as_posix(), "size": size, "sha256": digest})
            prog.add(0, force=True)
            write_json_atomic(final / MANIFEST_DIR, {"kind": "dir", "name": s.name, "file_count": len(entries),
                                                     "total_size": total, "files": entries,
                                                     "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                     "tool": "gdrive-bridge"})
            dp = m.drive_for(final)
            pushed.append({"drive_path": dp, "colab": colab_path(dp), "local": str(final),
                           "file_count": len(entries), "total_size": total, "manifest": dp + "/" + MANIFEST_DIR})
    here = Path(__file__).resolve()
    return emit({"ok": True, "pushed": pushed, "elapsed_sec": round(time.time() - t0, 1),
                 "next": "Drive for Desktop uploads in the background (not observable here; keep the machine awake). "
                         "Colab UI notebook: paste the `snippet fetch` cell. Fresh-VM runner: once the file shows in "
                         "Drive web, have a human share it 'anyone with the link' and run `share-id`.",
                 "snippet_commands": [f'python "{here}" snippet fetch {p["drive_path"]}' for p in pushed]})


def cmd_pull(a) -> int:
    m = require_mount()
    t0 = time.time()
    dp = norm_drive_path(a.drive_path)
    src = m.local_for(dp)
    if a.wait:
        marker_dir = src if (src.is_dir() or not src.exists()) else src.parent
        marker = marker_dir / a.marker
        deadline = time.time() + a.timeout
        n = 0
        while not marker.exists():
            if time.time() > deadline:
                return emit({"ok": False, "error": f"marker not found within {a.timeout}s: {m.drive_for(marker)}",
                             "hint": "the Colab export cell writes this marker last; check the notebook is still "
                                     "running, or pull without --wait to take whatever is there"}, 2)
            n += 1
            if n == 1 or n % 10 == 0:
                log(f"waiting for {m.drive_for(marker) or marker} ... ({int(time.time() - t0)}s)")
            time.sleep(a.interval)
        log(f"marker present: {marker}")
    if not src.exists():
        return emit({"ok": False, "error": f"not found on Drive (local view): {src}",
                     "hint": "if Colab just wrote it, Drive for Desktop may still be syncing metadata; retry shortly"}, 3)
    excludes = DEFAULT_EXCLUDES + (a.exclude or [])
    includes = a.include or []
    manifest = load_manifest(src)
    expected: dict[str, dict] = {}
    if manifest and manifest.get("kind") == "dir":
        expected = {f["path"]: f for f in manifest.get("files", [])}
    elif manifest and manifest.get("kind") == "file":
        expected = {src.name: manifest}
    if src.is_file():
        dest = Path(a.to).resolve() if a.to else (Path.cwd() / "gdrive_pull").resolve()
        files = [(src, PurePosixPath(src.name))]
        target_of = lambda rel: dest / rel.name  # noqa: E731
    else:
        dest = Path(a.to).resolve() if a.to else (Path.cwd() / "gdrive_pull" / src.name).resolve()
        files = list(walk_files(src, excludes, includes))
        target_of = lambda rel: dest.joinpath(*rel.parts)  # noqa: E731
    total = 0
    for p, _ in files:
        try:
            total += p.stat().st_size
        except OSError:
            pass
    log(f"pulling {len(files)} file(s), {human(total)} -> {dest}")
    prog = Progress("pull", total)
    copied, skipped, mismatched, unreadable = [], [], [], []
    for abs_p, rel in files:
        tgt = target_of(rel)
        try:
            st = abs_p.stat()
        except OSError as e:
            unreadable.append({"path": rel.as_posix(), "error": str(e)})
            continue
        if not a.force and tgt.exists():
            tst = tgt.stat()
            if tst.st_size == st.st_size and int(tst.st_mtime) >= int(st.st_mtime):
                skipped.append(rel.as_posix())
                prog.add(st.st_size)
                continue
        try:
            size, digest = copy_hash(abs_p, tgt, prog)
        except OSError as e:
            unreadable.append({"path": rel.as_posix(), "error": str(e)})
            continue
        rec = {"path": rel.as_posix(), "size": size, "sha256": digest, "local": str(tgt)}
        exp = expected.get(rel.as_posix())
        if exp and not a.no_verify:
            rec["verified"] = exp.get("size") == size and exp.get("sha256") == digest
            if not rec["verified"]:
                mismatched.append(rel.as_posix())
        copied.append(rec)
    prog.add(0, force=True)
    absent = []
    if expected and src.is_dir() and not includes:
        present = {r.as_posix() for _, r in files}
        absent = [p for p in expected if p not in present and not excluded(PurePosixPath(p), excludes)]
    ok = not mismatched and not unreadable and not absent
    return emit({"ok": ok, "drive_path": dp, "colab": colab_path(dp), "source_local": str(src), "dest": str(dest),
                 "copied": len(copied), "skipped_up_to_date": len(skipped),
                 "bytes": sum(r["size"] for r in copied), "manifest_found": bool(manifest),
                 "verified": sum(1 for r in copied if r.get("verified")) if manifest else None,
                 "mismatched": mismatched, "unreadable": unreadable, "listed_in_manifest_but_absent": absent,
                 "files": copied, "elapsed_sec": round(time.time() - t0, 1)}, 0 if ok else 2)


# --------------------------------------------------------------------------- share-id (gdown path)
def parse_file_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"[?&]id=([\w-]+)", s) or re.search(r"/d/([\w-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{20,}", s):
        return s
    raise SystemExit(f"cannot find a Drive file id in: {s}")


def size_str_to_bytes(s: str) -> float:
    m = re.fullmatch(r"([\d.]+)\s*([KMGT]?)B?", s.strip(), re.I)
    if not m:
        return -1
    return float(m.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2).upper()]


def probe_public(file_id: str) -> dict:
    """GET the same URL gdown uses. Public large files answer with a 'virus scan warning' page that names the
    file and its size; small files answer with the download itself; private files bounce to a login page."""
    url = f"https://drive.google.com/uc?id={file_id}&export=download"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    out = {"file_id": file_id, "gdown_url": url, "public": True, "name": None, "size_bytes_approx": None}
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            final = r.geturl()
            ctype = r.headers.get("Content-Type", "")
            disp = r.headers.get("Content-Disposition", "")
            length = r.headers.get("Content-Length")
            body = r.read(200_000).decode("utf-8", "replace") if "text/html" in ctype else ""
    except urllib.error.HTTPError as e:
        out.update(public=False, reason=f"HTTP {e.code} — no such file id, or not shared")
        return out
    except urllib.error.URLError as e:
        out.update(public=False, reason=f"network error: {e.reason}")
        return out
    if "accounts.google.com" in final or "ServiceLogin" in body:
        out["public"] = False
        out["reason"] = "login required — not shared as 'anyone with the link'"
        return out
    if disp:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', disp)
        out["name"] = unescape(m.group(1)) if m else None
        out["size_bytes_approx"] = int(length) if length else None
        return out
    m = re.search(r'class="uc-name-size"><a[^>]*>([^<]+)</a> \(([^)]+)\)', body)
    if m:
        out["name"] = unescape(m.group(1))
        out["size_bytes_approx"] = size_str_to_bytes(m.group(2))
    elif "Virus scan warning" not in body:
        out["public"] = False
        out["reason"] = "no download page (deleted, not shared, or download quota exceeded)"
    return out


def cmd_share_id(a) -> int:
    fid = parse_file_id(a.link)
    probe = probe_public(fid)
    problems = []
    if not probe["public"]:
        problems.append(probe.get("reason", "not public"))
    if a.expect_name and probe.get("name") and probe["name"] != a.expect_name:
        problems.append(f"Drive file is '{probe['name']}', expected '{a.expect_name}'")
    local_size = None
    if a.local:
        lp = Path(a.local)
        if not lp.exists():
            problems.append(f"--local not found: {lp}")
        else:
            local_size = lp.stat().st_size
            if probe.get("size_bytes_approx") and abs(local_size - probe["size_bytes_approx"]) / local_size > 0.06:
                problems.append(f"size mismatch: Drive≈{probe['size_bytes_approx']:.0f} B vs local {local_size} B")
            if probe.get("name") and lp.name != probe["name"]:
                problems.append(f"name differs: Drive '{probe['name']}' vs local '{lp.name}'")
    here = Path(__file__).resolve()
    return emit({"ok": not problems, "file_id": fid, "probe": probe, "local_size": local_size, "problems": problems,
                 "colab_cell": f'python "{here}" snippet gdown {fid}' + (f" --size {local_size}" if local_size else "")},
                0 if not problems else 2)


# --------------------------------------------------------------------------- snippets
def _sha_fn() -> list[str]:
    return ["def sha256(p, chunk=8 << 20):",
            "    h = hashlib.sha256()",
            "    with open(p, 'rb') as f:",
            "        for b in iter(lambda: f.read(chunk), b''):",
            "            h.update(b)",
            "    return h.hexdigest()", ""]


def _unzip_lines(zip_var: str, unzip_to: str, env: str) -> list[str]:
    lines = [f"UNZIP_TO = {unzip_to!r}",
             "os.makedirs(UNZIP_TO, exist_ok=True)",
             f"with zipfile.ZipFile({zip_var}) as zf:",
             "    zf.extractall(UNZIP_TO)",
             "print('extracted', sum(len(f) for _, _, f in os.walk(UNZIP_TO)), 'files under', UNZIP_TO)"]
    if env:
        lines += [f"os.environ[{env!r}] = UNZIP_TO", f"print({env!r}, '=', UNZIP_TO)"]
    return lines


def snippet_fetch(a) -> str:
    cpath = colab_path(a.drive_path)
    is_zip = cpath.lower().endswith(".zip")
    lines = [
        f"# --- gdrive-bridge: fetch {cpath} via drive.mount (generated {time.strftime('%Y-%m-%d %H:%M')}) ---",
        "import os, json, time, hashlib, shutil, zipfile",
        "from google.colab import drive",
        "drive.mount('/content/drive')",
        "",
        f"SRC = {cpath!r}",
        f"MAN = os.path.join(SRC, {MANIFEST_DIR!r}) if os.path.isdir(SRC) else SRC + {MANIFEST_SUFFIX!r}",
        f"WAIT_SEC, POLL = {a.wait}, 30",
        f"VERIFY_HASH = {'False' if a.no_hash else 'True'}",
        f"REQUIRE_MANIFEST = {'False' if a.no_manifest else 'True'}   # False: file was placed by hand, no manifest",
        "",
        *_sha_fn(),
        "def ready():",
        "    \"\"\"True once what Drive for Desktop uploaded matches the manifest written on the local side.\"\"\"",
        "    if not os.path.exists(SRC):",
        "        return False, 'not visible in Drive yet'",
        "    if not os.path.exists(MAN):",
        "        if not REQUIRE_MANIFEST:",
        "            return True, 'no manifest (REQUIRE_MANIFEST=False); trusting the file as-is'",
        "        return False, 'manifest not visible yet'",
        "    m = json.load(open(MAN))",
        "    if m.get('kind') == 'file':",
        "        if os.path.getsize(SRC) != m['size']:",
        "            return False, f\"size {os.path.getsize(SRC)} != {m['size']} (still uploading?)\"",
        "        if VERIFY_HASH and sha256(SRC) != m['sha256']:",
        "            return False, 'sha256 mismatch (still uploading?)'",
        "        return True, f\"{m['size']} bytes ok\"",
        "    bad = []",
        "    for f in m['files']:",
        "        p = os.path.join(SRC, f['path'])",
        "        if not os.path.exists(p) or os.path.getsize(p) != f['size']:",
        "            bad.append(f['path'])",
        "        elif VERIFY_HASH and sha256(p) != f['sha256']:",
        "            bad.append(f['path'])",
        "    return (not bad), (f\"{len(m['files'])} files ok\" if not bad else f'{len(bad)} not ready, e.g. {bad[:3]}')",
        "",
        "t0 = time.time()",
        "while True:",
        "    ok, why = ready()",
        "    print(f'[{int(time.time()-t0):4d}s] {why}')",
        "    if ok:",
        "        break",
        "    if time.time() - t0 > WAIT_SEC:",
        "        raise SystemExit('gdrive-bridge: upload not complete after WAIT_SEC; is Drive for Desktop running locally?')",
        "    time.sleep(POLL)",
        "",
    ]
    if is_zip:
        lines += _unzip_lines("SRC", a.unzip_to, a.env)
    else:
        lines += ["# Copy to the VM's local disk: the Drive FUSE mount is slow for many small random reads.",
                  f"LOCAL = {a.unzip_to!r}",
                  "if os.path.isdir(SRC):",
                  "    shutil.copytree(SRC, LOCAL, dirs_exist_ok=True); print('copied tree to', LOCAL)",
                  "else:",
                  "    os.makedirs(LOCAL, exist_ok=True); shutil.copy2(SRC, LOCAL); print('copied file to', LOCAL)"]
        if a.env:
            lines += [f"os.environ[{a.env!r}] = LOCAL", f"print({a.env!r}, '=', LOCAL)"]
    return "\n".join(lines) + "\n"


def snippet_gdown(a) -> str:
    fid = parse_file_id(a.drive_path)
    lines = [
        f"# --- gdrive-bridge: gdown {fid} on a fresh VM, no Drive mount (generated {time.strftime('%Y-%m-%d %H:%M')}) ---",
        "import os, hashlib, zipfile, gdown",
        f"FILE_ID = {fid!r}",
        "ZIP = '/content/bundle.zip'",
        f"EXPECT_SIZE = {a.size if a.size else 'None'}",
        f"EXPECT_SHA256 = {a.sha256!r}" if a.sha256 else "EXPECT_SHA256 = None",
        "",
        *_sha_fn(),
        "gdown.download(id=FILE_ID, output=ZIP, quiet=False)   # needs 'anyone with the link' sharing",
        "got = os.path.getsize(ZIP)",
        "if EXPECT_SIZE and got != EXPECT_SIZE:",
        "    raise SystemExit(f'gdrive-bridge: size {got} != {EXPECT_SIZE} (quota page instead of file? wrong id?)')",
        "if EXPECT_SHA256 and sha256(ZIP) != EXPECT_SHA256:",
        "    raise SystemExit('gdrive-bridge: sha256 mismatch')",
        "print('downloaded', got, 'bytes')",
        *_unzip_lines("ZIP", a.unzip_to, a.env),
    ]
    return "\n".join(lines) + "\n"


def snippet_export(a) -> str:
    dp = norm_drive_path(a.drive_path)
    cpath = colab_path(dp)
    run_expr = repr(a.run_id) if a.run_id else "time.strftime('%Y%m%d-%H%M%S')"
    lines = [
        f"# --- gdrive-bridge: export {a.src} -> {cpath}/<run_id> (generated {time.strftime('%Y-%m-%d %H:%M')}) ---",
        "import os, json, time, hashlib, shutil",
        "from google.colab import drive",
        "drive.mount('/content/drive')",
        "",
        f"SRC = {a.src!r}",
        f"RUN_ID = {run_expr}",
        f"DST = os.path.join({cpath!r}, RUN_ID)",
        f"MARKER = {a.marker!r}",
        "",
        *_sha_fn(),
        "assert os.path.isdir(SRC), f'nothing to export: {SRC}'",
        "os.makedirs(DST, exist_ok=True)",
        "files, total = [], 0",
        "for root, _, names in os.walk(SRC):",
        "    for n in sorted(names):",
        "        p = os.path.join(root, n)",
        "        rel = os.path.relpath(p, SRC).replace(os.sep, '/')",
        "        q = os.path.join(DST, rel)",
        "        os.makedirs(os.path.dirname(q), exist_ok=True)",
        "        shutil.copyfile(p, q + '.part'); os.replace(q + '.part', q)",
        "        sz = os.path.getsize(p); total += sz",
        "        files.append({'path': rel, 'size': sz, 'sha256': sha256(p)})",
        "        print(f'  {rel}  {sz:,} B')",
        "manifest = {'kind': 'dir', 'name': RUN_ID, 'file_count': len(files), 'total_size': total, 'files': files,",
        "            'exported_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'tool': 'gdrive-bridge', 'source': SRC}",
        f"with open(os.path.join(DST, {MANIFEST_DIR!r}), 'w') as f:",
        "    json.dump(manifest, f, indent=2)",
        "with open(os.path.join(DST, MARKER), 'w') as f:   # written LAST: the local `pull --wait` keys on it",
        "    f.write(time.strftime('%Y-%m-%dT%H:%M:%S') + '\\n')",
        "print(f'exported {len(files)} files, {total:,} B -> {DST}')",
        "drive.flush_and_unmount()   # push Colab's write cache to Drive now",
    ]
    if not a.no_remount:
        lines.append("drive.mount('/content/drive', force_remount=True)   # re-attach for later cells")
    lines.append(f"print('local side: python gdrive_bridge.py pull {dp}/' + RUN_ID + ' --wait')")
    return "\n".join(lines) + "\n"


def cmd_snippet(a) -> int:
    a.unzip_to = vm_path(a.unzip_to)
    a.src = vm_path(a.src)
    code = {"fetch": snippet_fetch, "gdown": snippet_gdown, "export": snippet_export}[a.what](a)
    if a.out:
        Path(a.out).write_text(code, encoding="utf-8")
        log(f"wrote {a.out}")
    print(code, end="")
    return 0


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gdrive_bridge.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    sp.add_parser("status", help="detect the Drive mount and show the local<->colab mapping").set_defaults(fn=cmd_status)

    q = sp.add_parser("ls", help="list a Drive path (non-recursive by default; DriveFS listings are slow)")
    q.add_argument("drive_path")
    q.add_argument("-R", "--recursive", action="store_true")
    q.add_argument("--depth", type=int, default=None, help="max directory depth when recursing")
    q.set_defaults(fn=cmd_ls)

    q = sp.add_parser("push", help="copy local files/dirs into Drive (optionally as one zip) and write a manifest")
    q.add_argument("local", nargs="+", help="local files or directories")
    q.add_argument("--to", required=True, help="Drive directory, e.g. MyDrive/project/bundles")
    q.add_argument("--zip", metavar="NAME.zip", help="bundle all sources into one zip with this name")
    q.add_argument("--exclude", action="append", metavar="GLOB", help="extra exclude pattern (repeatable)")
    q.add_argument("--overwrite", action="store_true", help="replace an existing Drive file/dir of the same name")
    q.set_defaults(fn=cmd_push)

    q = sp.add_parser("pull", help="copy a Drive file/dir (Colab outputs) to local disk, verifying the manifest")
    q.add_argument("drive_path", help="e.g. MyDrive/runs/exp-003/20260922-1530  or a single file")
    q.add_argument("--to", help="local destination directory (default ./gdrive_pull/<name>)")
    q.add_argument("--wait", action="store_true", help="block until the marker file exists in the source dir")
    q.add_argument("--marker", default=DEFAULT_MARKER, help=f"marker written last by the export cell (default {DEFAULT_MARKER})")
    q.add_argument("--timeout", type=float, default=6 * 3600, help="seconds to wait for the marker (default 6h)")
    q.add_argument("--interval", type=float, default=30, help="poll interval seconds (default 30)")
    q.add_argument("--include", action="append", metavar="GLOB", help="only files matching (name or relative path)")
    q.add_argument("--exclude", action="append", metavar="GLOB", help="skip files/dirs matching (e.g. '*.safetensors')")
    q.add_argument("--force", action="store_true", help="re-copy even if the local copy looks up to date")
    q.add_argument("--no-verify", action="store_true", help="skip sha256 comparison against the manifest")
    q.set_defaults(fn=cmd_pull)

    q = sp.add_parser("share-id", help="check a Drive share link/id the way gdown will use it (public? name? size?)")
    q.add_argument("link", help="share link (open?id=..., /file/d/<id>/view) or bare file id")
    q.add_argument("--local", help="local copy of the same file; size and name are compared")
    q.add_argument("--expect-name", help="expected file name on Drive")
    q.set_defaults(fn=cmd_share_id)

    q = sp.add_parser("snippet", help="print a Colab cell: fetch (drive.mount, waits for upload), gdown (fresh VM), export (VM->Drive + marker)")
    q.add_argument("what", choices=["fetch", "gdown", "export"])
    q.add_argument("drive_path", help="fetch: pushed file/dir; gdown: file id or share link; export: Drive dir that receives runs")
    q.add_argument("--unzip-to", default="/content/data", help="fetch/gdown: extraction (or copy) target on the VM")
    q.add_argument("--env", default="DATA_ROOT", help="fetch/gdown: env var set to the extracted path ('' to skip)")
    q.add_argument("--wait", type=int, default=3600, help="fetch: max seconds to wait for the upload to complete")
    q.add_argument("--no-hash", action="store_true", help="fetch: compare sizes only (faster for multi-GB files)")
    q.add_argument("--no-manifest", action="store_true", help="fetch: the file was put on Drive by hand; do not wait for a manifest")
    q.add_argument("--size", type=int, help="gdown: expected byte size (from share-id --local)")
    q.add_argument("--sha256", help="gdown: expected sha256")
    q.add_argument("--src", default="/content/outputs", help="export: VM directory to export")
    q.add_argument("--run-id", help="export: fixed run id instead of a timestamp")
    q.add_argument("--marker", default=DEFAULT_MARKER, help="export: marker file written last")
    q.add_argument("--no-remount", action="store_true", help="export: do not remount Drive after flush_and_unmount")
    q.add_argument("--out", help="also write the cell to this file")
    q.set_defaults(fn=cmd_snippet)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except SystemExit as e:
        if isinstance(e.code, str):
            return emit({"ok": False, "error": e.code}, 3)
        raise
    except (OSError, PermissionError) as e:
        return emit({"ok": False, "error": f"{type(e).__name__}: {e}"}, 3)


if __name__ == "__main__":
    sys.exit(main())
