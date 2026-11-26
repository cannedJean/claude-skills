#!/usr/bin/env python3
"""chrome_launcher.py - run Gemini Deep Research in a visible Chrome window instead of the patched desktop app.

Chrome supports remote debugging natively when it runs on its own profile directory (since Chrome 136 the switch is
ignored for the default profile), so nothing installed is modified and app updates cannot break the setup.

  - Profile: %LOCALAPPDATA%\\agy-deep-research\\chrome-profile (GEMINI_DR_CHROME_PROFILE). The user signs in to Google
    there once; the user's everyday Chrome profile is never touched.
  - Port: 9224 (GEMINI_DR_CHROME_PORT), distinct from the desktop app's 9223 so a driver never attaches to the app.
  - The window is visible and opens https://gemini.google.com/app. It is started through WMI, like the app launcher,
    so it outlives the relay worker that started it.

Usage:
  python chrome_launcher.py            launch (or reuse) the Chrome window and print JSON
  python chrome_launcher.py --check    report chrome path, profile and port liveness without launching
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

GEMINI_URL = "https://gemini.google.com/app"
DEFAULT_PORT = 9224
CANDIDATE_PATHS = (
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
)
WAIT_ATTEMPTS = 40
WAIT_INTERVAL = 0.5


def chrome_port():
    return int(os.environ.get("GEMINI_DR_CHROME_PORT") or DEFAULT_PORT)


def default_profile_dir():
    if os.environ.get("GEMINI_DR_CHROME_PROFILE"):
        return os.environ["GEMINI_DR_CHROME_PROFILE"]
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "agy-deep-research", "chrome-profile")


def find_chrome():
    override = os.environ.get("GEMINI_DR_CHROME_PATH")
    for cand in ((override,) if override else ()) + tuple(CANDIDATE_PATHS):
        if cand and os.path.isfile(cand):
            return cand
    raise FileNotFoundError("Chrome executable not found; install Google Chrome or set GEMINI_DR_CHROME_PATH")


def build_chrome_command(chrome_path, port, profile_dir):
    return [
        chrome_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--new-window",
        GEMINI_URL,
    ]


def is_cdp_alive(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def _quote(arg):
    return f'"{arg}"' if (" " in arg or not arg) else arg


def spawn_detached(cmd):
    """Start Chrome outside the caller's process tree (agy/relay workers may be killed with their children)."""
    if os.name != "nt":
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    command_line = " ".join(_quote(a) for a in cmd).replace("'", "''")
    ps_cmd = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
              f"-Arguments @{{ CommandLine = '{command_line}' }} | Out-Null")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ensure_chrome_running_with_cdp(port=None, profile_dir=None):
    port = int(port or chrome_port())
    profile_dir = profile_dir or default_profile_dir()
    info = {"browser": "chrome", "cdp_url": f"http://127.0.0.1:{port}", "port": port, "profile_dir": profile_dir}
    if is_cdp_alive(port):
        return dict(info, status="already_running", cdp_alive=True)
    chrome = find_chrome()
    os.makedirs(profile_dir, exist_ok=True)
    spawn_detached(build_chrome_command(chrome, port, profile_dir))
    for _ in range(WAIT_ATTEMPTS):
        if is_cdp_alive(port):
            return dict(info, status="started", chrome_path=chrome, cdp_alive=True)
        time.sleep(WAIT_INTERVAL)
    raise RuntimeError(f"Chrome debugging port did not answer on {port} within {int(WAIT_ATTEMPTS * WAIT_INTERVAL)} s "
                       f"(is another Chrome already using the profile {profile_dir} without the port?)")


def check():
    port = chrome_port()
    try:
        chrome = find_chrome()
    except FileNotFoundError:
        chrome = None
    profile = default_profile_dir()
    return {"browser": "chrome", "chrome_path": chrome, "found": bool(chrome), "profile_dir": profile,
            "profile_exists": os.path.isdir(profile), "port": port, "cdp_url": f"http://127.0.0.1:{port}",
            "cdp_alive": is_cdp_alive(port)}


def main():
    ap = argparse.ArgumentParser(description="Launch Chrome on its own profile with a debugging port for Gemini Deep Research.")
    ap.add_argument("--check", action="store_true", help="report chrome path, profile and port liveness without launching")
    a = ap.parse_args()
    if a.check:
        print(json.dumps(check(), indent=2))
        return 0
    try:
        print(json.dumps(ensure_chrome_running_with_cdp(), indent=2))
        return 0
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
