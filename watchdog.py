#!/usr/bin/env python3
"""
watchdog.py — Runs play.py as a subprocess, captures output, auto-restarts on
crash, and fires Discord notifications on crash/restart.

Usage:
    python watchdog.py

Log file: play.log  (timestamped, ANSI stripped)
Discord:  uses DISCORD_WEBHOOK env var or discord_notify module
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import threading
import signal
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
PLAY_CMD = [sys.executable, "play.py"]
WORKDIR  = Path(__file__).parent
LOG_PATH = WORKDIR / "play.log"

RESTART_DELAY_S  = 5    # seconds to wait before restarting
MAX_RESTARTS     = 999  # effectively unlimited

# Patterns in output that indicate a crash worth flagging even if process is
# still alive (e.g. unhandled exception that was caught at top level)
ERROR_PATTERNS = re.compile(
    r"(Traceback \(most recent call last\)|"
    r"^\s*(Exception|Error|Critical|CRITICAL|FATAL):)",
    re.MULTILINE,
)

ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# ── Discord helper (standalone, no import of discord_notify to avoid side-effects)
def _discord_url() -> str | None:
    # Try env var first
    url = os.getenv("DISCORD_WEBHOOK")
    if url:
        return url
    # Try reading from DB setting via discord_notify module
    try:
        sys.path.insert(0, str(WORKDIR))
        import db
        return db.get_bot_setting("discord_webhook") or None
    except Exception:
        return None


def _send_discord(title: str, description: str, color: int = 0xFF4444) -> None:
    url = _discord_url()
    if not url:
        return
    payload = {
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "watchdog.py"},
        }]
    }
    def _post():
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
        except Exception as e:
            _log(f"[watchdog] Discord send failed: {e}")
    threading.Thread(target=_post, daemon=True).start()


# ── Logging ───────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _write_raw(line: str) -> None:
    """Write a raw (ANSI-stripped) line from play.py to the log file."""
    clean = ANSI_ESCAPE.sub("", line)
    with _log_lock:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(clean)


# ── Stream reader ─────────────────────────────────────────────────────────────
class StreamReader(threading.Thread):
    """Reads lines from a stream, mirrors to stdout, writes to log."""

    def __init__(self, stream, error_callback):
        super().__init__(daemon=True)
        self.stream         = stream
        self.error_callback = error_callback
        self._buf           = []

    def run(self):
        for raw_line in self.stream:
            try:
                line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
            except Exception:
                line = repr(raw_line)
            sys.stdout.write(line)
            sys.stdout.flush()
            _write_raw(line)
            self._buf.append(line)
            # Keep a rolling window of last 50 lines for traceback detection
            if len(self._buf) > 50:
                self._buf.pop(0)
            if ERROR_PATTERNS.search(line):
                self.error_callback("".join(self._buf[-20:]))


# ── Watchdog loop ─────────────────────────────────────────────────────────────
_shutdown = threading.Event()

def _handle_sigint(sig, frame):
    _log("[watchdog] Caught SIGINT — shutting down.")
    _shutdown.set()

def _handle_sigterm(sig, frame):
    _log("[watchdog] Caught SIGTERM — shutting down.")
    _shutdown.set()

signal.signal(signal.SIGINT,  _handle_sigint)
signal.signal(signal.SIGTERM, _handle_sigterm)


def run():
    restart_count = 0
    _log("=" * 60)
    _log("[watchdog] Starting — will auto-restart play.py on crash.")
    _log(f"[watchdog] Log: {LOG_PATH}")
    _log("=" * 60)

    while not _shutdown.is_set() and restart_count <= MAX_RESTARTS:
        if restart_count > 0:
            _log(f"[watchdog] Restart #{restart_count} in {RESTART_DELAY_S}s...")
            _send_discord(
                "play.py restarting",
                f"Restart #{restart_count} — waiting {RESTART_DELAY_S}s then relaunching.",
                color=0xFFA500,
            )
            time.sleep(RESTART_DELAY_S)
            if _shutdown.is_set():
                break

        _log(f"[watchdog] Launching: {' '.join(PLAY_CMD)}")

        proc = subprocess.Popen(
            PLAY_CMD,
            cwd=str(WORKDIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout
            bufsize=1,
        )

        error_snippets: list[str] = []

        def on_error(snippet: str):
            error_snippets.append(snippet)

        reader = StreamReader(proc.stdout, on_error)
        reader.start()

        # Wait for process to finish
        while proc.poll() is None and not _shutdown.is_set():
            time.sleep(1)

        if _shutdown.is_set():
            _log("[watchdog] Shutdown requested — terminating play.py.")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            break

        reader.join(timeout=5)
        exit_code = proc.returncode

        if exit_code == 0:
            _log("[watchdog] play.py exited cleanly (code 0). Stopping watchdog.")
            break

        # Crashed
        snippet = error_snippets[-1] if error_snippets else "(no traceback captured)"
        _log(f"[watchdog] play.py CRASHED (exit code {exit_code}).")
        _log(f"[watchdog] Last error snippet:\n{snippet}")

        _send_discord(
            f"play.py CRASHED (exit {exit_code})",
            f"```\n{snippet[-1800:]}\n```",
            color=0xFF0000,
        )

        restart_count += 1

    _log("[watchdog] Watchdog exiting.")


if __name__ == "__main__":
    run()
