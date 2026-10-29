import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request

GEMINI_BASE_DIR = os.path.expandvars(r"%LOCALAPPDATA%\Google\Gemini")
CDP_PORT = int(os.environ.get("GEMINI_CDP_PORT", 9223))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"


def find_latest_app_dir():
    pattern = os.path.join(GEMINI_BASE_DIR, "app-*")
    dirs = [d for d in glob.glob(pattern) if os.path.isdir(d)]
    if not dirs:
        raise FileNotFoundError(f"No Gemini app-* directory found in {GEMINI_BASE_DIR}")
    dirs.sort(key=lambda p: [int(x) if x.isdigit() else x for x in os.path.basename(p).replace("app-", "").split(".")])
    return dirs[-1]


def is_cdp_alive(port=CDP_PORT):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as resp:
            return resp.status == 200
    except Exception:
        return False


def stop_gemini_processes():
    subprocess.run(["taskkill", "/F", "/IM", "Gemini.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)


def patch_asar(asar_path, port=CDP_PORT):
    with open(asar_path, "rb") as f:
        data = bytearray(f.read())

    data_offset = struct.unpack("<I", data[4:8])[0] + 8
    header_size = struct.unpack("<I", data[12:16])[0]
    header_bytes = data[16 : 16 + header_size]
    header_str = header_bytes.decode("utf-8", errors="ignore").rstrip("\x00")
    header = json.loads(header_str)

    node = header["files"]["src"]["files"]["main.js"]
    off = int(node["offset"]) + data_offset
    sz = int(node["size"])
    main_bytes = bytes(data[off : off + sz])

    patch_marker = f'appendSwitch("remote-debugging-port","{port}");m.app.commandLine.appendSwitch("disable-renderer-backgrounding")'.encode("utf-8")
    if patch_marker in main_bytes:
        return False  # Already patched

    # Restore from backup first if an older patch was applied
    backup_path = asar_path + ".bak"
    if os.path.exists(backup_path) and b'appendSwitch("remote-debugging-port"' in main_bytes:
        shutil.copy2(backup_path, asar_path)
        with open(asar_path, "rb") as f:
            data = bytearray(f.read())
        main_bytes = bytes(data[off : off + sz])

    pattern = re.compile(
        rb'var (\w+)=(\w+)\(m\.app\.isPackaged&&(\w+)!=="dev",process\.argv,process\.execArgv\);'
        rb'\1\?\(u\.error\("security",(\w+),`\(switch: --\$\{\1\}\)`\),st\(\),m\.app\.exit\(1\)\):(\w+)\(\);'
    )
    match = pattern.search(main_bytes)
    if not match:
        raise RuntimeError("Target pattern not found in main.js; app version structure may have changed.")
    target = match.group(0)
    cont = match.group(5).decode("ascii")

    replacement_core = (
        f'm.app.commandLine.appendSwitch("remote-debugging-port","{port}");'
        f'm.app.commandLine.appendSwitch("disable-renderer-backgrounding");{cont}();'
    ).encode("utf-8")

    pad_len = len(target) - len(replacement_core)
    if pad_len < 4:
        replacement_core = f'm.app.commandLine.appendSwitch("remote-debugging-port","{port}");{cont}();'.encode("utf-8")
        pad_len = len(target) - len(replacement_core)

    replacement = replacement_core + b"/*" + (b" " * (pad_len - 4)) + b"*/"
    assert len(replacement) == len(target), f"Length mismatch: {len(replacement)} != {len(target)}"

    new_main_bytes = main_bytes.replace(target, replacement, 1)
    assert len(new_main_bytes) == sz

    old_hash = node.get("integrity", {}).get("hash", "")
    new_hash = hashlib.sha256(new_main_bytes).hexdigest()
    if old_hash and len(old_hash) == len(new_hash):
        old_hash_bytes = old_hash.encode("ascii")
        new_hash_bytes = new_hash.encode("ascii")
        header_region = bytes(data[16 : 16 + header_size]).replace(old_hash_bytes, new_hash_bytes)
        data[16 : 16 + header_size] = header_region

    data[off : off + sz] = new_main_bytes

    backup_path = asar_path + ".bak"
    if not os.path.exists(backup_path):
        shutil.copy2(asar_path, backup_path)

    with open(asar_path, "wb") as f:
        f.write(data)

    return True


def restore_asar():
    app_dir = find_latest_app_dir()
    asar_path = os.path.join(app_dir, "resources", "app.asar")
    backup_path = asar_path + ".bak"
    if not os.path.exists(backup_path):
        return {"status": "no_backup_found", "asar_path": asar_path}
    stop_gemini_processes()
    shutil.copy2(backup_path, asar_path)
    return {"status": "restored", "asar_path": asar_path}


def spawn_detached_wmi(exe_path, app_dir, hide=True):
    cmd_str = f'"{exe_path}" --hide' if hide else f'"{exe_path}"'
    ps_cmd = (
        f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{ CommandLine = '{cmd_str}'; CurrentDirectory = '{app_dir}' }} | Out-Null"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def is_asar_patched(asar_path, port=CDP_PORT):
    with open(asar_path, "rb") as f:
        data = f.read()
    marker = f'appendSwitch("remote-debugging-port","{port}");m.app.commandLine.appendSwitch("disable-renderer-backgrounding")'.encode("utf-8")
    return marker in data


def ensure_gemini_running_with_cdp(port=CDP_PORT, hide=True):
    app_dir = find_latest_app_dir()
    asar_path = os.path.join(app_dir, "resources", "app.asar")
    exe_path = os.path.join(app_dir, "Gemini.exe")

    if is_cdp_alive(port) and is_asar_patched(asar_path, port=port):
        return {"status": "already_running", "cdp_url": f"http://127.0.0.1:{port}", "app_dir": app_dir}

    stop_gemini_processes()
    patched = patch_asar(asar_path, port=port)
    spawn_detached_wmi(exe_path, app_dir, hide=hide)

    for _ in range(40):
        if is_cdp_alive(port):
            return {
                "status": "started",
                "patched": patched,
                "cdp_url": f"http://127.0.0.1:{port}",
                "app_dir": app_dir,
            }
        time.sleep(0.5)

    raise RuntimeError(f"Gemini started from {exe_path}, but CDP port {port} did not become ready in time.")


if __name__ == "__main__":
    if "--restore" in sys.argv:
        print(json.dumps(restore_asar(), indent=2))
    else:
        hide_flag = "--show" not in sys.argv
        info = ensure_gemini_running_with_cdp(port=CDP_PORT, hide=hide_flag)
        print(json.dumps(info, indent=2))
