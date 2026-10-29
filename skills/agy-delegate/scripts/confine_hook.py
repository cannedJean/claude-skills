#!/usr/bin/env python3
"""confine_hook.py - agy PreToolUse hook that confines every tool call to one directory (the work dir).

agy_task.py installs it as <workdir>/.agents/hooks.json for the duration of a run. agy runs hook commands
with the working directory set to the folder holding hooks.json and pipes the tool call as JSON on stdin;
the hook answers with {"decision": ...} on stdout:

  deny  a hard block: any path argument resolving outside the root, UNC / device / drive-relative paths,
        file: URLs outside the root, hard-linked files (their data may live outside; realpath cannot tell),
        traversal look-alikes such as ".. " or "...", writes into the root's .agents/ or .agy_task/, every shell tool
        (a program agy starts could read any file, which no argument check can see), browser and MCP tools.
        The log entry names the rule that fired: shell, tool, path, hard_link, protected, inlined or error.
  ask   no objection: agy's own permission rules decide as usual (measured: it never grants more than
        agy would without the hook; an empty or unreadable answer counts as deny, so this hook fails closed).

In deep-research mode (AGY_CONFINE_MODE="deep-research"):
  - READ_TOOLS are permitted inside <workdir> and AGY_CONFINE_DR_DIR.
  - Write tools are permitted only inside <workdir>, and caller control files (relay/request.json,
    prompt.md, goal.md, feedback_*.md, relay/result.json, relay/started.json) are denied with rule "protected".
  - run_command is permitted only for clean Python execution of relay_step.py --job <workdir> or
    patch_gemini_app.py without shell chaining / redirections; all other shell commands are denied with rule "shell".

Environment (set by agy_task.py): AGY_CONFINE_ROOT (the root; default: the parent of the cwd),
AGY_CONFINE_LOG (JSON-lines decision log), AGY_CONFINE_INLINED (JSON list of root-relative files whose full text
the prompt already holds), AGY_CONFINE_MODE, AGY_CONFINE_DR_DIR. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

IS_WINDOWS = sys.platform == "win32"
PROTECTED = (".agents", ".agy_task")  # hook config and launcher metadata: readable, never writable
READ_TOOLS = {"view_file", "view_file_outline", "view_code_item", "view_content_chunk", "list_dir", "grep_search",
              "find_by_name", "codebase_search", "search_in_file", "read_file", "read_notebook"}
VIEW_TOOLS = {"view_file", "view_file_outline", "view_code_item", "view_content_chunk", "read_file"}
SHELL_TOOLS = {"run_command", "send_command_input", "read_terminal", "command_status"}
# file contents and prose, never a path: skipped when scanning arguments (anything else that looks like a path
# - absolute, rooted, ~, file:, or with a .. segment - is checked whatever its key, e.g. a glob Pattern)
TEXT_KEYS = {"codecontent", "replacementcontent", "targetcontent", "instruction", "description", "toolaction",
             "toolsummary", "query", "searchquery", "commandline", "input", "prompt", "message", "content", "text",
             "explanation", "summary", "title"}
PATH_KEY = re.compile(r"(path|paths|file|files|dir|dirs|directory|directories|folder|cwd|uri|url|location|root|target)$",
                      re.I)
URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]+)://", re.I)
DOTDOT = re.compile(r"(^|[\\/])\.\.([\\/]|$)")

# Metacharacters for shell chaining and redirection
SHELL_META_RE = re.compile(r"""[&|;`><\r\n]|\$\(""")
PYTHON_EXE_RE = re.compile(r"^python(3(\.\d+)?)?(\.exe)?$", re.I)
ALLOWED_PYTHON_FLAGS = frozenset({"-u", "-B", "-E", "-s", "-S", "-O", "-OO"})


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
    if v.lower().startswith("file:"):
        u = urlparse(v)
        if u.netloc and u.netloc.lower() != "localhost":
            return None, "file URL with a host"
        try:
            v = url2pathname(unquote(u.path)) if IS_WINDOWS else unquote(u.path)
        except Exception:
            return None, "malformed file URL"
    else:
        m = URL_RE.match(v)
        if m and len(m.group(1)) > 1:
            return None, None  # http(s) and friends: not a local file, agy's permission rules decide
    if v.startswith("~"):
        v = os.path.expanduser(v)
    for part in re.split(r"[\\/]", v):  # "... ", ".. ", ". ." : traversal look-alikes, never a real name
        if part and part not in (".", "..") and not part.strip(" ."):
            return None, "malformed path component " + repr(part)
    if IS_WINDOWS:
        v = v.replace("/", "\\")
        if v.startswith("\\\\?\\") and re.match(r"^\\\\\?\\[A-Za-z]:\\", v):
            v = v[4:]  # \\?\C:\x is C:\x
        if v.startswith("\\\\") or v.startswith("\\??\\"):
            return None, "UNC, device or NT object path"
        if re.match(r"^[A-Za-z]:(?![\\])", v):
            return None, "drive-relative path"
    test_path = v[2:] if re.match(r"^[A-Za-z]:", v) else v
    if ":" in test_path:
        return None, "alternate data stream or illegal colon in path"
    path = v if os.path.isabs(v) else os.path.join(root, v)
    return canon(path), None


def hard_linked(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return not os.path.isdir(path) and st.st_nlink > 1


def looks_like_path(value: str) -> bool:
    v = value.strip()
    return bool(
        re.match(r"^([A-Za-z]:|[\\/~])", v)
        or v.lower().startswith("file:")
        or DOTDOT.search(v)
        or "::" in v
        or re.search(r":[^/\\:]", v)
    )


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


def is_protected_file(rel: str, mode: str = "default") -> bool:
    rel_clean = rel.replace("\\", "/")
    base_rel = rel_clean.split(":")[0]
    first_part = base_rel.split("/")[0]
    if first_part in PROTECTED:
        return True
    if mode == "deep-research":
        rel_lower = base_rel.lower()
        if rel_lower in (
            "relay/request.json",
            "prompt.md",
            "goal.md",
            "relay/result.json",
            "relay/started.json",
        ):
            return True
        if re.match(r"^feedback_[^/]+\.md$", rel_lower):
            return True
    return False


def split_command_line(cmd: str) -> list[str]:
    """Tokenizes command line on whitespace while respecting single and double quotes.
    Handles mid-token quotes like --job="C:\\work dir".
    """
    tokens = []
    current = []
    in_quote = None
    for char in cmd:
        if in_quote:
            if char == in_quote:
                in_quote = None
            else:
                current.append(char)
        elif char in ('"', "'"):
            in_quote = char
        elif char.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(char)
    if in_quote:
        raise ValueError(f"unclosed quote: {in_quote}")
    if current:
        tokens.append("".join(current))
    return tokens


def validate_deep_research_command(cmd: str, root: str, dr_dir: str, cwd: str = ""):
    """Validates run_command for deep-research mode. Returns (decision, reason, rule)."""
    if not cmd or not isinstance(cmd, str) or not cmd.strip():
        return "deny", "empty command line", "shell"

    if SHELL_META_RE.search(cmd):
        return "deny", "shell chaining and redirection metacharacters are disabled in deep-research mode", "shell"

    try:
        tokens = [t.strip('"\'') for t in split_command_line(cmd)]
    except ValueError as e:
        return "deny", f"malformed command syntax: {e}", "shell"

    if not tokens:
        return "deny", "empty command line", "shell"

    # Token 0: Python interpreter validation
    exe_token = tokens[0]
    exe_base = os.path.basename(exe_token).lower()
    is_python = bool(PYTHON_EXE_RE.match(exe_base) or exe_base in ("py", "py.exe"))
    if not is_python:
        try:
            if canon(exe_token) == canon(sys.executable):
                is_python = True
        except Exception:
            pass
    if not is_python:
        return "deny", f"executable {exe_token!r} is not a supported Python interpreter", "shell"

    # Locate script token while skipping permitted python options
    script_token = None
    script_idx = -1
    for i, tok in enumerate(tokens[1:], start=1):
        if tok.startswith("-"):
            if tok in ALLOWED_PYTHON_FLAGS:
                continue
            return "deny", f"python option {tok!r} is not permitted in deep-research mode", "shell"
        script_token = tok
        script_idx = i
        break

    if not script_token:
        return "deny", "no Python script specified in command line", "shell"

    script_base = os.path.basename(script_token).lower()
    if script_base not in ("relay_step.py", "patch_gemini_app.py"):
        return ("deny",
                f"script {script_token!r} is not permitted in deep-research mode: only relay_step.py and patch_gemini_app.py are allowed",
                "shell")

    cmd_cwd = cwd if cwd else root
    if os.path.dirname(script_token):
        script_path = canon(script_token if os.path.isabs(script_token) else os.path.join(cmd_cwd, script_token))
        if not (inside(script_path, root) or (bool(dr_dir) and inside(script_path, dr_dir))):
            return "deny", f"script path {script_token!r} must be located inside workdir or dr_dir", "shell"

    script_args = tokens[script_idx + 1:]

    if script_base == "relay_step.py":
        job_target = None
        i = 0
        while i < len(script_args):
            arg = script_args[i]
            if arg == "--job":
                if i + 1 < len(script_args):
                    job_target = script_args[i + 1]
                    i += 2
                    continue
                else:
                    return "deny", "--job missing argument value", "shell"
            elif arg.startswith("--job="):
                job_target = arg.split("=", 1)[1].strip('"\'')
                i += 1
                continue
            elif arg == "--worker":
                i += 1
                continue
            else:
                return "deny", f"unrecognized argument {arg!r} for relay_step.py", "shell"

        if not job_target:
            return "deny", "relay_step.py requires --job <workdir> argument", "shell"

        job_path = canon(job_target if os.path.isabs(job_target) else os.path.join(cmd_cwd, job_target))
        if job_path != canon(root):
            return "deny", f"--job target {job_target!r} does not match workdir {root}", "shell"

    elif script_base == "patch_gemini_app.py":
        allowed_patch_args = {"--restore", "--show"}
        for arg in script_args:
            if arg not in allowed_patch_args:
                return "deny", f"argument {arg!r} is not permitted for patch_gemini_app.py", "shell"

    return "ask", "", ""


def decide(payload: dict, root: str, inlined=frozenset(), mode: str = "default", dr_dir: str = ""):
    """(decision, reason, path hits, rule). inlined: canonical paths of files whose full text is in the prompt
    (view tools on them are denied). rule names why a call was denied - shell, tool, path (unreadable or outside the
    root), hard_link, protected, inlined or error - so a caller can tell an attempt to get out from a token-saving refusal."""
    root = canon(root)
    if dr_dir:
        dr_dir = canon(dr_dir)

    tc = payload.get("toolCall") or {}
    name = str(tc.get("name") or "").lower()
    args = tc.get("args") or {}

    # Browser and MCP tools are unconditionally denied
    if name.startswith("browser") or "mcp" in name:
        return "deny", f"tool {name} is disabled: this session may only touch files inside " + root, [], "tool"

    # Shell tools handling
    if mode == "deep-research":
        if name in SHELL_TOOLS:
            if name != "run_command":
                return "deny", f"shell tool {name} is disabled in deep-research mode", [], "shell"
            cmd = args.get("CommandLine", "")
            cwd_arg = args.get("Cwd")
            cmd_cwd = root
            if cwd_arg:
                cwd_path, cwd_reason = to_fs_path(cwd_arg, root)
                if cwd_reason or not inside(cwd_path, root):
                    return "deny", f"Cwd={cwd_arg!r} is outside {root}; only paths inside it are allowed", [], "path"
                cmd_cwd = cwd_path

            dec, reason, rule = validate_deep_research_command(cmd, root, dr_dir, cmd_cwd)
            if dec != "ask":
                return dec, reason, [], rule
            return "ask", "", [], ""
    else:
        if name in SHELL_TOOLS:
            return "deny", "shell commands are disabled: this session may only touch files inside " + root, [], "shell"

    # Filesystem tools path scanning
    hits = list(scan(args, root))
    for key, raw, path, reason in hits:
        if reason:
            return "deny", f"{key}={raw!r}: {reason}; only paths inside {root} are allowed", hits, "path"

        # Boundary checks
        if mode == "deep-research" and name in READ_TOOLS:
            if not (inside(path, root) or (bool(dr_dir) and inside(path, dr_dir))):
                return "deny", f"{key}={raw!r} is outside {root} and {dr_dir}; only paths inside them are allowed", hits, "path"
        else:
            # For write tools, or default mode: must be inside root
            if not inside(path, root):
                return "deny", f"{key}={raw!r} is outside {root}; only paths inside it are allowed", hits, "path"

        if hard_linked(path):
            return ("deny", f"{key}={raw!r} is a hard link (it shares its data with another path); not allowed",
                    hits, "hard_link")

        # Protected files check inside root for write tools
        if inside(path, root):
            rel = os.path.relpath(path, root)
            if name not in READ_TOOLS and is_protected_file(rel, mode):
                return "deny", f"{rel.replace('\\', '/')} is read-only in this session", hits, "protected"

    # last, after every argument passed the checks above: a token-saving refusal must never hide an attempt to get
    # out made in the same call (that one has to be logged and counted under its own rule)
    if name in VIEW_TOOLS:
        for key, raw, path, _ in hits:
            if path in inlined:
                rel = os.path.relpath(path, root).replace("\\", "/")
                return "deny", (f"the full text of {rel} is already in your prompt: read it there instead of "
                                f"opening the file"), hits, "inlined"

    return "ask", "", hits, ""


def inlined_paths(root: str) -> frozenset:
    rels = json.loads(os.environ.get("AGY_CONFINE_INLINED") or "[]")
    return frozenset(canon(os.path.join(root, r)) for r in rels if isinstance(r, str) and r)


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
        mode = (os.environ.get("AGY_CONFINE_MODE") or "default").strip()
        dr_dir_env = (os.environ.get("AGY_CONFINE_DR_DIR") or "").strip()
        dr_dir = canon(dr_dir_env) if dr_dir_env else ""
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
        tc = payload.get("toolCall") or {}
        decision, reason, hits, rule = decide(payload, root, inlined_paths(root), mode=mode, dr_dir=dr_dir)
        entry.update(tool=tc.get("name"), decision=decision, reason=reason,
                     paths=[{"key": k, "value": v} for k, v, _, _ in hits][:8])
        if rule:
            entry["rule"] = rule
        if tc.get("name") in SHELL_TOOLS:
            entry["command"] = str((tc.get("args") or {}).get("CommandLine", ""))[:300]
    except Exception as e:  # fail closed
        decision, reason = "deny", f"confinement hook error: {type(e).__name__}: {e}"
        entry.update(decision=decision, reason=reason, rule="error")
    audit(entry)
    out = {"decision": decision}
    if reason:
        out["reason"] = reason
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
