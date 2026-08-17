"""Keeps the CDP-attach debug Chrome (see README "SSO auth (enterprise ChatGPT
via CDP)") alive. Polls the debug port; relaunches Chrome with the same
profile/flags if it's down. Runs until killed (Ctrl+C, Task Scheduler stop).

Usage:
    python chrome_watchdog.py

Env vars (same ones gpt-tools itself uses):
    GPT_TOOLS_HOME     — base dir for the chrome_cdp_profile/ (default: next to this file)
    GPT_TOOLS_CDP_URL  — e.g. http://localhost:9222 (default: http://localhost:9222)
"""
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

_STATE_HOME = os.environ.get("GPT_TOOLS_HOME") or os.path.dirname(os.path.abspath(__file__))
CDP_URL = os.environ.get("GPT_TOOLS_CDP_URL") or "http://localhost:9222"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE_DIR = os.path.join(_STATE_HOME, "chrome_cdp_profile")
CHECK_INTERVAL_SECONDS = 20


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _launch_chrome():
    port = urlparse(CDP_URL).port
    print(f"[chrome_watchdog] launching Chrome on port {port} ({PROFILE_DIR})", flush=True)
    subprocess.Popen(
        [
            CHROME_EXE,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={PROFILE_DIR}",
            "https://chatgpt.com",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    parsed = urlparse(CDP_URL)
    host, port = parsed.hostname or "localhost", parsed.port or 9222

    if not os.path.isfile(CHROME_EXE):
        print(f"[chrome_watchdog] Chrome not found at {CHROME_EXE} — edit CHROME_EXE in this script.", file=sys.stderr)
        sys.exit(1)

    print(f"[chrome_watchdog] watching {host}:{port}, checking every {CHECK_INTERVAL_SECONDS}s", flush=True)
    while True:
        if not _port_open(host, port):
            print(f"[chrome_watchdog] {host}:{port} is down, relaunching", flush=True)
            _launch_chrome()
            # Give it a moment to actually bind before the next check, so we
            # don't launch a second instance on top of one still starting.
            time.sleep(10)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
