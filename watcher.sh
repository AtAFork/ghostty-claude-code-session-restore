#!/usr/bin/env python3
"""ghostty-session-watcher — save Claude/Codex sessions when Ghostty quits."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from session_restore_core import (  # noqa: E402
    codex_is_interactive,
    load_restore_file,
    resolve_codex_session_id_for_pid,
    resolve_sessions,
)

SNAPSHOT_PATH = Path("/tmp/ghostty-session-snapshot.json")
RESTORE_PATH = Path.home() / ".claude" / "ghostty-restore.json"
LIVE_STATE_PATH = Path.home() / ".claude" / "ghostty-live-state.json"
CLAUDE_PROJECTS_PATH = Path.home() / ".claude" / "projects"
LOG_PATH = Path.home() / ".claude" / "debug" / "ghostty-session-watcher.log"
EMPTY_LIVE_STATE_GRACE_SECONDS = 8.0

RUNNING = True


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def truncate_log(max_bytes: int = 51200, keep_lines: int = 100) -> None:
    try:
        if not LOG_PATH.exists() or LOG_PATH.stat().st_size <= max_bytes:
            return
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        LOG_PATH.write_text("\n".join(lines[-keep_lines:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def handle_signal(_signum: int, _frame: object) -> None:
    global RUNNING
    RUNNING = False


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


def is_ghostty_running() -> bool:
    return _run(["pgrep", "-x", "ghostty"]).returncode == 0


def _get_ppid(pid: int) -> int | None:
    out = _run(["ps", "-p", str(pid), "-o", "ppid="]).stdout.strip()
    if not out:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def _get_comm(pid: int) -> str:
    return _run(["ps", "-p", str(pid), "-o", "comm="]).stdout.strip()


def is_ghostty_child(pid: int, max_depth: int = 6) -> bool:
    current = pid
    for _ in range(max_depth):
        parent = _get_ppid(current)
        if parent in (None, 0, 1):
            return False
        comm = _get_comm(parent).lower()
        if "ghostty" in comm:
            return True
        current = parent
    return False


def get_cwd(pid: int) -> str | None:
    out = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).stdout
    for line in out.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


def list_candidate_processes() -> list[dict]:
    out = _run(["ps", "-eo", "pid=,tty=,comm=,args="]).stdout
    entries: list[dict] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, tty, comm, args = parts
        if not tty.startswith("ttys"):
            continue
        tool = Path(comm.strip()).name.lower()
        if tool not in {"claude", "codex"}:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue

        if not is_ghostty_child(pid):
            continue

        if tool == "codex" and not codex_is_interactive(args):
            continue

        cwd = get_cwd(pid)
        if not cwd:
            continue

        entry: dict = {
            "pid": pid,
            "tty": tty,
            "cwd": cwd,
            "args": args,
            "tool": tool,
        }
        if tool == "codex":
            sid = resolve_codex_session_id_for_pid(pid)
            if sid:
                entry["sessionId"] = sid
        entries.append(entry)

    entries.sort(key=lambda x: x["tty"])
    return entries


def write_snapshot(entries: list[dict]) -> None:
    SNAPSHOT_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_snapshot() -> list[dict]:
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def load_sessions_file(path: Path) -> list[dict]:
    return load_restore_file(path)


def persist_live_state(entries: list[dict]) -> int:
    sessions = resolve_sessions(entries, str(CLAUDE_PROJECTS_PATH))
    if not sessions:
        # Keep the previous non-empty live state. This avoids clobbering restore
        # data on transient empty snapshots (for example during crash races).
        return 0
    write_json_atomic(LIVE_STATE_PATH, sessions)
    return len(sessions)


def save_sessions() -> tuple[int, int, int]:
    sessions = load_sessions_file(LIVE_STATE_PATH)
    if not sessions:
        snapshot = load_snapshot()
        if not snapshot:
            return (0, 0, 0)
        sessions = resolve_sessions(snapshot, str(CLAUDE_PROJECTS_PATH))
    if not sessions:
        return (0, 0, 0)

    write_json_atomic(RESTORE_PATH, sessions)

    resumed = sum(1 for x in sessions if x.get("sessionId"))
    continued = len(sessions) - resumed
    return (len(sessions), resumed, continued)


def should_save_on_shutdown() -> bool:
    if is_ghostty_running():
        return True
    snapshot = load_snapshot()
    return bool(snapshot)


def should_clear_live_state_for_empty_period(
    *,
    empty_started_at: float | None,
    now: float,
    already_cleared: bool,
    grace_seconds: float = EMPTY_LIVE_STATE_GRACE_SECONDS,
) -> bool:
    if already_cleared or empty_started_at is None:
        return False
    return (now - empty_started_at) >= grace_seconds


def snapshot_log_message(
    entries: list[dict], prev_unresolved_codex_pids: tuple[int, ...]
) -> tuple[str, tuple[int, ...]]:
    unresolved_codex_pids = tuple(
        sorted(
            entry["pid"]
            for entry in entries
            if entry.get("tool") == "codex" and not entry.get("sessionId")
        )
    )
    base = f"Snapshot: {len(entries)} session(s)"
    if unresolved_codex_pids and unresolved_codex_pids != prev_unresolved_codex_pids:
        return (
            f"{base} ({len(unresolved_codex_pids)} codex unresolved -> --last fallback)",
            unresolved_codex_pids,
        )
    return base, unresolved_codex_pids


def main() -> int:
    log(f"Started (PID {os.getpid()})")

    ghostty_was_running = False
    prev_signature: tuple = tuple()
    prev_unresolved_codex_pids: tuple[int, ...] = tuple()
    empty_started_at: float | None = None
    empty_live_state_cleared = False
    n = 0

    while RUNNING:
        if is_ghostty_running():
            ghostty_was_running = True
            entries = list_candidate_processes()
            now = time.monotonic()
            if entries:
                empty_started_at = None
                empty_live_state_cleared = False
            else:
                if empty_started_at is None:
                    empty_started_at = now
                if should_clear_live_state_for_empty_period(
                    empty_started_at=empty_started_at,
                    now=now,
                    already_cleared=empty_live_state_cleared,
                ):
                    write_json_atomic(LIVE_STATE_PATH, [])
                    empty_live_state_cleared = True
                    log("No active sessions for 8s - cleared live state")

            signature = tuple(
                (entry["pid"], entry.get("sessionId"), entry["args"]) for entry in entries
            )
            if signature != prev_signature:
                prev_signature = signature
                write_snapshot(entries)
                persist_live_state(entries)
                message, prev_unresolved_codex_pids = snapshot_log_message(
                    entries, prev_unresolved_codex_pids
                )
                log(message)
        elif ghostty_was_running:
            log("Ghostty quit - saving")
            total, resumed, continued = save_sessions()
            if total:
                log(
                    f"Sessions saved: {total} total "
                    f"({resumed} resume, {continued} continue)"
                )
            else:
                log("No sessions to save")
            ghostty_was_running = False
            prev_signature = tuple()
            prev_unresolved_codex_pids = tuple()
            empty_started_at = None
            empty_live_state_cleared = False
            write_snapshot([])

        n = (n + 1) % 500
        if n == 0:
            truncate_log()

        time.sleep(2)

    log("Shutting down")
    # Best-effort: persist latest sessions if this process likely observed active state.
    if should_save_on_shutdown():
        try:
            total, resumed, continued = save_sessions()
            if total:
                log(
                    f"Shutdown save: {total} total "
                    f"({resumed} resume, {continued} continue)"
                )
        except Exception:
            pass
    try:
        SNAPSHOT_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
