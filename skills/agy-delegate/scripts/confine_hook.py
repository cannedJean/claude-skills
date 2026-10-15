#!/usr/bin/env python3
"""confine_hook.py - agy PreToolUse hook that confines every tool call to one directory (the work dir).

agy_task.py installs it as <workdir>/.agents/hooks.json for the duration of a run. agy runs hook commands
with the working directory set to the folder holding hooks.json and pipes the tool call as JSON on stdin;
the hook answers with {"decision": ...} on stdout:

  deny  a hard block: any path argument resolving outside the root, UNC / device / drive-relative paths,
        file: URLs outside the root, writes into the root's .agents/ or .agy_task/, every shell tool
        (a program agy starts could read any file, which no argument check can see), browser and MCP tools.
  ask   no objection: agy's own permission rules decide as usual (measured: it never grants more than
        agy would without the hook; an empty or unreadable answer counts as deny, so this hook fails closed).

Environment (set by agy_task.py): AGY_CONFINE_ROOT (the root; default: the parent of the cwd) and
AGY_CONFINE_LOG (JSON-lines decision log). Python 3.9+, stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

IS_WINDOWS = sys.platform == "win32"
PROTECTED = (".agents", ".agy_task")  # hook config and launcher metadata: readable, never writable
READ_TOOLS = {"view_file", "view_file_outline", "view_code_item", "view_content_chunk", "list_dir", "grep_search",
              "find_by_name", "codebase_search", "search_in_file", "read_file", "read_notebook"}
SHELL_TOOLS = {"run_command", "send_command_input", "read_terminal", "command_status"}
# file contents and prose, never a path: skipped when scanning arguments (anything else that looks like a path
# - absolute, rooted, ~, file:, or with a .. segment - is checked whatever its key, e.g. a glob Pattern)
TEXT_KEYS = {"codecontent", "replacementcontent", "targetcontent", "instruction", "description", "toolaction",
             "toolsummary", "query", "searchquery", "commandline", "input", "prompt", "message", "content", "text",
             "explanation", "summary", "title"}
PATH_KEY = re.compile(r"(path|paths|file|files|dir|dirs|directory|directories|folder|cwd|uri|url|location|root|target)$",
                      re.I)
URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]+):")
DOTDOT = re.compile(r"(^|[\\/])\.\.([\\/]|$)")


def canon(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives
        return False


def to_fs_path(value: str, root: str):
    """(canonical path, None) or (None, reason to deny) for one argument naming a file or directory."""
    v = value.strip()
    m = URL_RE.match(v)
    if m and len(m.group(1)) > 1:  # a URL (a one-letter scheme is a drive letter)
        if m.group(1).lower() != "file":
            return None, None  # http(s) and friends: not a local file, agy's permission rules decide
        u = urlparse(v)
        if u.netloc and u.netloc.lower() != "localhost":
            return None, "file URL with a host"
        v = url2pathname(unquote(u.path)) if IS_WINDOWS else unquote(u.path)
    if v.startswith("~"):
        v = os.path.expanduser(v)
    if IS_WINDOWS:
        v = v.replace("/", "\\")
        if v.startswith("\\\\?\\") and re.match(r"^\\\\\?\\[A-Za-z]:\\", v):
            v = v[4:]  # \\?\C:\x is C:\x
        if v.startswith("\\\\") or v.startswith("\\??\\"):
            return None, "UNC, device or NT object path"
        if re.match(r"^[A-Za-z]:(?![\\])", v):
            return None, "drive-relative path"
    path = v if os.path.isabs(v) else os.path.join(root, v)
    return canon(path), None


def looks_like_path(value: str) -> bool:
    v = value.strip()
    return bool(re.match(r"^([A-Za-z]:|[\\/~])", v) or v.lower().startswith("file:") or DOTDOT.search(v))


def scan(args, root: str, key: str = ""):
    """Yield (key, raw value, canonical path or None, deny reason or None) for every path in the arguments."""
    if isinstance(args, dict):
        for k, v in args.items():
            yield from scan(v, root, str(k))
    elif isinstance(args, list):
        for v in args:
            yield from scan(v, root, key)
    elif isinstance(args, str) and args.strip():
        k = key.lower()
        if k in TEXT_KEYS:
            return
        if PATH_KEY.search(k) or looks_like_path(args):
            path, reason = to_fs_path(args, root)
            if path is not None or reason:
                yield key, args, path, reason


def decide(payload: dict, root: str):
    tc = payload.get("toolCall") or {}
    name = str(tc.get("name") or "").lower()
    args = tc.get("args") or {}
    if name in SHELL_TOOLS:
        return "deny", "shell commands are disabled: this session may only touch files inside " + root, []
    if name.startswith("browser") or "mcp" in name:
        return "deny", f"tool {name} is disabled: this session may only touch files inside " + root, []
    hits = list(scan(args, root))
    for key, raw, path, reason in hits:
        if reason:
            return "deny", f"{key}={raw!r}: {reason}; only paths inside {root} are allowed", hits
        if not inside(path, root):
            return "deny", f"{key}={raw!r} is outside {root}; only paths inside it are allowed", hits
        rel = os.path.relpath(path, root).replace("\\", "/")
        if name not in READ_TOOLS and rel.split("/")[0] in PROTECTED:
            return "deny", f"{rel} is read-only in this session", hits
    return "ask", "", hits


def audit(entry: dict) -> None:
    log = os.environ.get("AGY_CONFINE_LOG")
    if not log:
        return
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main() -> int:
    entry = {"at": _dt.datetime.now().isoformat(timespec="seconds")}
    try:
        root = canon(os.environ.get("AGY_CONFINE_ROOT") or str(Path.cwd().parent))
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        tc = payload.get("toolCall") or {}
        decision, reason, hits = decide(payload, root)
        entry.update(tool=tc.get("name"), decision=decision, reason=reason,
                     paths=[{"key": k, "value": v} for k, v, _, _ in hits][:8])
        if tc.get("name") in SHELL_TOOLS:
            entry["command"] = str((tc.get("args") or {}).get("CommandLine", ""))[:300]
    except Exception as e:  # fail closed
        decision, reason = "deny", f"confinement hook error: {type(e).__name__}: {e}"
        entry.update(decision=decision, reason=reason)
    audit(entry)
    out = {"decision": decision}
    if reason:
        out["reason"] = reason
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
