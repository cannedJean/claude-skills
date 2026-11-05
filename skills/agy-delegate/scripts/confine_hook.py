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
the prompt already holds), AGY_CONFINE_MODE, AGY_CONFINE_DR_DIR, AGY_CONFINE_BROWSER_ORIGINS (JSON list of http(s)
origins; when set, agy's Chrome DevTools tools may browse those origins - see decide_browser). Python 3.9+, stdlib only.
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
# folders agy (or git) loads configuration or code from, at any depth: readable, never writable
CONFIG_DIRS = {".agents", ".agent", "_agents", "_agent", ".agy_task", ".git"}
# every tool the confinement knows how to judge; anything else is denied (fails closed on new agy tools)
KNOWN_TOOLS = {"view_file", "view_file_outline", "view_code_item", "view_content_chunk", "read_file", "read_notebook",
               "write_to_file", "write_file", "replace_file_content", "multi_replace_file_content", "edit_file",
               "list_dir", "grep_search", "grep", "find_by_name", "find", "codebase_search", "search_in_file",
               "search_web", "read_url_content", "manage_task", "send_message", "manage_subagents", "invoke_subagent",
               "define_subagent", "finish", "schedule", "ask_question"}
READ_TOOLS = {"view_file", "view_file_outline", "view_code_item", "view_content_chunk", "list_dir", "grep_search",
              "find_by_name", "codebase_search", "search_in_file", "read_file", "read_notebook"}
VIEW_TOOLS = {"view_file", "view_file_outline", "view_code_item", "view_content_chunk", "read_file"}
SHELL_TOOLS = {"run_command", "send_command_input", "read_terminal", "command_status", "shell_exec",
               "run_extension_code", "execute_notebook", "restart_dev_server"}
RESOURCE_TOOLS = {"read_resource", "list_resources"}  # MCP resources: another server's data
NATIVE_BROWSER = re.compile(r"(^|_)browser(_|$)")  # agy's own browser steps: open_browser_url, browser_subagent, ...
# file contents and prose, never a path: skipped when scanning arguments (anything else that looks like a path
# - absolute, rooted, ~, file:, or with a .. segment - is checked whatever its key, e.g. a glob Pattern)
TEXT_KEYS = {"codecontent", "replacementcontent", "targetcontent", "instruction", "description", "toolaction",
             "toolsummary", "query", "searchquery", "commandline", "input", "prompt", "message", "content", "text",
             "explanation", "summary", "title", "notes", "question", "questions", "options", "systemprompt",
             "system_prompt", "role", "name"}
# per-tool prose arguments (agy CLI tools): an edit's old/new text and a report's notes are never paths
TOOL_TEXT_KEYS = {"edit_file": {"target", "replacement"}, "finish": {"notes", "summary", "code"}}
PATH_KEY = re.compile(r"(path|paths|file|files|dir|dirs|directory|directories|folder|cwd|uri|url|(?<!geo)location|root|workspace)$",
                      re.I)
URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]+)://", re.I)
URL_KEY = re.compile(r"(url|uri|urls|uris|href)$", re.I)  # arguments that really take a web URL
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


def to_fs_path(value: str, root: str, url_ok: bool = False):
    """(canonical path, None) or (None, reason to deny) for one argument naming a file or directory.
    url_ok: the argument takes a web URL (its key ends in url/uri/href); only then is an http(s) URL "not a file".
    Anywhere else a scheme-looking value is denied: Node and Win32 resolve "zz://../x" or "http://../x" as an
    ordinary relative path ("zz:" becomes a folder name), so it could point anywhere."""
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
            if url_ok and m.group(1).lower() in ("http", "https"):
                try:
                    host = urlparse(v).hostname
                except ValueError:
                    return None, "malformed URL"
                if not public_host(host or ""):
                    return None, "URL to a local, private or non-public host"
                return None, None  # a public web URL in a URL argument: not a local file, agy's rules decide
            return None, "URL-looking value where a file path is expected"
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


LOCAL_SUFFIXES = ("localhost", "local", "lan", "internal", "intranet", "home", "corp", "home.arpa", "localdomain")
METADATA_IPS = {"168.63.129.16", "100.100.100.200", "fd00:ec2::254"}


def public_host(host: str) -> bool:
    """Lexical check that a URL host names a public site: plain ASCII DNS name with a dot and no local-only suffix,
    or a public IP literal (loopback, private, link-local, CGNAT, multicast, metadata and obfuscated forms fail)."""
    import ipaddress
    import socket
    h = (host or "").lower()
    ip = None
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        if re.fullmatch(r"(0x[0-9a-f]*|\d+)(\.(0x[0-9a-f]*|\d+)){0,3}", h):
            try:
                ip = ipaddress.ip_address(socket.inet_aton(h))
            except OSError:
                return False
    if ip is not None:
        if ip.version == 6:  # unwrap IPv4 carried in IPv6 (mapped, NAT64 64:ff9b::/96, 6to4)
            if ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            elif ip in ipaddress.ip_network("64:ff9b::/96"):
                ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
            elif ip.sixtofour:
                ip = ip.sixtofour
        return ip.is_global and not ip.is_multicast and str(ip) not in METADATA_IPS
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", h):
        return False
    labels = h.split(".")
    return labels[-1] not in LOCAL_SUFFIXES and ".".join(labels[-2:]) not in LOCAL_SUFFIXES


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


def scan(args, root: str, key: str = "", text_keys=frozenset()):
    """Yield (key, raw value, canonical path or None, deny reason or None) for every path in the arguments."""
    if isinstance(args, dict):
        for k, v in args.items():
            yield from scan(v, root, str(k), text_keys)
    elif isinstance(args, list):
        for v in args:
            yield from scan(v, root, key, text_keys)
    elif isinstance(args, str) and args.strip():
        k = key.lower()
        if k in TEXT_KEYS or k in text_keys:
            return
        if PATH_KEY.search(k) or looks_like_path(args):
            path, reason = to_fs_path(args, root, url_ok=bool(URL_KEY.search(k)))
            if path is not None or reason:
                yield key, args, path, reason


def is_protected_file(rel: str, mode: str = "default") -> bool:
    rel_clean = rel.replace("\\", "/")
    base_rel = rel_clean.split(":")[0]
    first_part = base_rel.split("/")[0]
    if first_part in PROTECTED:
        return True
    if any(part.lower().rstrip(". ") in CONFIG_DIRS for part in base_rel.split("/")):
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


# Opt-in browser allowlist (AGY_CONFINE_BROWSER_ORIGINS, set by agy_task.py --allow-browser-origin). Only agy's built-in
# Chrome DevTools MCP tools qualify; every other browser/MCP tool stays denied. Navigation is limited to the listed
# origins; text entry, page scripts, uploads, heap dumps and closing pages are denied (they could submit forms, call APIs
# from the page or read local files); files the browser writes must land inside the root (never .agents/.agy_task);
# unknown browser tools fail closed. agy_task.py refuses browser mode while the user's own Chrome exposes a DevTools port,
# because agy's MCP auto-connects to it: the tools then drive a separate browser, never the user's signed-in one.
BROWSER_PREFIX = "mcp_chrome_devtools_"
BROWSER_ALLOWED = {"take_snapshot", "take_screenshot", "wait_for", "list_console_messages", "get_console_message",
                   "list_network_requests", "get_network_request", "get_css_styles", "lighthouse_audit",
                   "performance_start_trace", "performance_stop_trace", "performance_analyze_insight",
                   "screencast_start", "screencast_stop", "handle_dialog", "emulate", "resize_page", "click", "hover",
                   "drag", "press_key", "navigate_page", "new_page", "list_pages", "select_page"}
BROWSER_KEYS = {"tab", "shift+tab", "escape", "enter", "space", "arrowup", "arrowdown", "arrowleft", "arrowright",
                "pageup", "pagedown", "home", "end"}
BROWSER_REQUIRED_PATH = {"lighthouse_audit": "outputdirpath", "screencast_start": "filepath"}  # else they write to temp


PLAIN_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")  # ASCII labels only: no %, no unicode, no empty/trailing dot


def origin_key(url: str):
    """(scheme, host, port) of an http(s) URL without credentials whose host is plain ASCII, else None.
    Hosts a browser would rewrite (percent-encoding, full-width or other unicode forms, trailing dots) never match."""
    try:
        u = urlparse(str(url).strip())
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("https", "http") or not u.hostname or u.username is not None or u.password is not None:
        return None
    host = u.hostname.lower()
    if not PLAIN_HOST.match(host) or port == 0:
        return None
    return u.scheme, host, port if port is not None else (443 if u.scheme == "https" else 80)


def parse_origins(raw) -> frozenset:
    try:
        items = json.loads(raw) if isinstance(raw, str) else list(raw or ())
    except (ValueError, TypeError):
        return frozenset()
    return frozenset(k for k in (origin_key(o) for o in (items if isinstance(items, list) else ())) if k)


def decide_browser(tool: str, args: dict, root: str, origins: frozenset):
    """(decision, reason, path hits, rule) for one Chrome DevTools tool call (tool = name without the prefix)."""
    if tool not in BROWSER_ALLOWED:
        return "deny", f"browser tool {tool} is not allowed here (no text entry, page scripts, uploads or unknown tools)", [], "tool"
    lower = {str(k).lower(): v for k, v in args.items()}
    if len(lower) != len(args):  # "promptText" and "PROMPTTEXT" together: which one the MCP reads is not ours to guess
        return "deny", f"browser tool {tool}: argument names that differ only in case", [], "tool"
    if lower.get("initscript"):
        return "deny", "page scripts (initScript) are not allowed", [], "tool"
    if tool == "handle_dialog" and lower.get("prompttext"):
        return "deny", "handle_dialog promptText would type text into the page", [], "tool"
    if tool == "emulate" and (lower.get("useragent") or lower.get("extrahttpheaders")):
        return "deny", "emulate userAgent/extraHttpHeaders are not allowed (viewport, colours and throttling are)", [], "tool"
    for tool_name, key in BROWSER_REQUIRED_PATH.items():
        v = lower.get(key)
        if tool == tool_name and (not isinstance(v, str) or not v.strip()):
            return "deny", f"{tool} must name its output ({key}) inside {root}", [], "path"
    for k, v in args.items():
        if str(k).lower() != "url" and PATH_KEY.search(str(k)):
            vals = v if isinstance(v, list) else [v]
            if not vals or any(not isinstance(x, str) or not x.strip() for x in vals):
                return "deny", f"{k}={v!r}: a file argument must be a non-blank absolute path", [], "path"
    # every argument that names a file (any key, fail closed) must stay inside the root; URLs are checked below
    hits = []
    for key, raw, path, reason in scan({k: v for k, v in args.items() if str(k).lower() != "url"}, root):
        if reason or path is None or not inside(path, root):
            return "deny", f"{key}={raw!r}: the browser may only use files inside {root}", [], "path"
        r = str(raw).strip()
        if not (r.lower().startswith("file:") or os.path.isabs(r)):
            return "deny", (f"{key}={raw!r}: give the browser an absolute path inside {root} (it resolves relative "
                            "paths against its own folder, not the work dir)"), [], "path"
        if is_protected_file(os.path.relpath(path, root)):
            return "deny", f"{key}={raw!r} is in a protected (configuration) folder", [], "protected"
        if hard_linked(path):
            return "deny", f"{key}={raw!r} is a hard link (its data may live outside {root})", [], "hard_link"
        hits.append((key, raw, path, None))
    if tool in ("navigate_page", "new_page"):
        url = args.get("url")
        nav = str(args.get("type") or ("url" if url or tool == "new_page" else "")).lower()
        if nav == "url":
            if not url or origin_key(url) not in origins:
                return "deny", f"navigation to {url!r} is outside the allowed origins", [], "url"
        elif nav not in ("back", "forward", "reload") or url:
            return "deny", f"navigation type {nav!r} is not allowed", [], "url"
    if tool == "press_key" and str(args.get("key") or "").strip().lower() not in BROWSER_KEYS:
        return "deny", f"press_key {args.get('key')!r}: only navigation keys (text entry is disabled)", [], "tool"
    return "ask", "", hits, ""


def decide(payload: dict, root: str, inlined=frozenset(), mode: str = "default", dr_dir: str = "",
           browser_origins=frozenset()):
    """(decision, reason, path hits, rule). inlined: canonical paths of files whose full text is in the prompt
    (view tools on them are denied). rule names why a call was denied - shell, tool, path (unreadable or outside the
    root), hard_link, protected, inlined, url or error - so a caller can tell an attempt to get out from a
    token-saving refusal. browser_origins: opt-in allowlist for agy's Chrome DevTools tools (see decide_browser)."""
    root = canon(root)
    if dr_dir:
        dr_dir = canon(dr_dir)

    tc = payload.get("toolCall") or {}
    name = str(tc.get("name") or "").lower()
    args = tc.get("args") or {}

    if browser_origins and mode != "deep-research" and name.startswith(BROWSER_PREFIX):
        if not isinstance(args, dict):
            return "deny", f"tool {name}: arguments must be an object", [], "tool"
        return decide_browser(name[len(BROWSER_PREFIX):], args, root, browser_origins)

    # Browser, MCP and MCP-resource tools are otherwise unconditionally denied
    if NATIVE_BROWSER.search(name) or "mcp" in name or name in RESOURCE_TOOLS:
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

    if name not in KNOWN_TOOLS:
        return "deny", f"tool {name} is not one this confinement knows how to check, so it is denied", [], "tool"
    if not isinstance(args, dict):
        return "deny", f"tool {name}: arguments must be an object", [], "tool"
    if name == "invoke_subagent":
        specs = [args] + [x for k, v in args.items() if str(k).lower() == "subagents" and isinstance(v, list)
                          for x in v if isinstance(x, dict)]
        for spec in specs:
            for k, v in spec.items():
                kl = str(k).lower()
                if kl in ("typename", "type", "agent", "agenttype", "subagenttype") and str(v).strip().lower() == "browser"                         and not browser_origins:
                    return "deny", "the browser subagent needs browser mode (--allow-browser-origin)", [], "tool"
                if kl.startswith("workspace"):
                    for w in (v if isinstance(v, list) else [v]):
                        if str(w).strip().lower() in ("", "inherit", "none"):
                            continue  # agy's default: the subagent shares this work dir
                        wpath, wreason = to_fs_path(str(w), root)
                        if wreason or wpath != root:
                            return "deny", f"a subagent must work in {root}, not {w!r}", [], "path"

    # Filesystem tools path scanning
    hits = list(scan(args, root, text_keys=frozenset(TOOL_TEXT_KEYS.get(name, ()))))
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
        origins = parse_origins(os.environ.get("AGY_CONFINE_BROWSER_ORIGINS") or "[]")
        decision, reason, hits, rule = decide(payload, root, inlined_paths(root), mode=mode, dr_dir=dr_dir,
                                              browser_origins=origins)
        entry.update(tool=tc.get("name"), decision=decision, reason=reason,
                     paths=[{"key": k, "value": v} for k, v, _, _ in hits][:8])
        if rule:
            entry["rule"] = rule
        targs = tc.get("args") or {}
        if str(tc.get("name") or "").lower().startswith(BROWSER_PREFIX) and isinstance(targs, dict):
            seen = {k: str(targs[k])[:300] for k in ("url", "type", "key") if targs.get(k)}
            if seen:
                entry["browser"] = seen
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
