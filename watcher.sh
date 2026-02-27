#!/usr/bin/env python3
"""ghostty-session-watcher — save Claude/Codex sessions when Ghostty/Cmux quits."""

from __future__ import annotations

import json
import os
import re
import signal
import socket
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
    resolve_claude_session_id_for_pid,
    resolve_codex_session_id_for_pid,
    resolve_sessions,
)

SNAPSHOT_PATH = Path("/tmp/ghostty-session-snapshot.json")
RESTORE_PATH = Path.home() / ".claude" / "ghostty-restore.json"
LIVE_STATE_PATH = Path.home() / ".claude" / "ghostty-live-state.json"
CMUX_WORKSPACE_MAP_PATH = Path.home() / ".claude" / "cmux-workspace-map.json"
LOG_PATH = Path.home() / ".claude" / "debug" / "ghostty-session-watcher.log"
CMUX_SOCKET_PATH = Path("/tmp/cmux.sock")
EMPTY_LIVE_STATE_GRACE_SECONDS = 8.0
POLL_INTERVAL_SECONDS = 2.0

RUNNING = True


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr="")


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


def _is_cmux_socket_alive() -> bool:
    """Check if the cmux socket exists and accepts connections."""
    if not CMUX_SOCKET_PATH.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(str(CMUX_SOCKET_PATH))
        s.close()
        return True
    except (OSError, socket.error):
        return False


def detect_terminals() -> set[str]:
    """Return active terminal modes as a set containing cmux and/or ghostty."""
    active: set[str] = set()
    if _is_cmux_socket_alive():
        active.add("cmux")
    if _run(["pgrep", "-x", "ghostty"]).returncode == 0:
        active.add("ghostty")
    return active


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


def get_cmux_env(pid: int) -> tuple[str, str] | None:
    """Extract CMUX_WORKSPACE_ID and CMUX_SURFACE_ID from a process environment."""
    out = _run(["ps", "eww", "-p", str(pid), "-o", "command="]).stdout
    ws_match = re.search(r"CMUX_WORKSPACE_ID=(\S+)", out)
    sf_match = re.search(r"CMUX_SURFACE_ID=(\S+)", out)
    if ws_match and sf_match:
        return (ws_match.group(1), sf_match.group(1))
    return None


def is_cmux_child(pid: int) -> tuple[str, str] | None:
    """Return (workspace_id, surface_id) if pid is a cmux child, else None."""
    return get_cmux_env(pid)


def _load_cmux_workspace_map() -> dict[str, str]:
    """Load workspace UUID -> name mapping written by shell startup snippet."""
    try:
        data = json.loads(CMUX_WORKSPACE_MAP_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def enrich_cmux_entries(entries: list[dict]) -> list[dict]:
    """Add workspaceName and surfaceIndex to cmux entries.

    Reads workspace UUID -> name mapping from a file maintained by the
    shell startup snippet (which runs inside cmux and can call the cmux CLI).
    Surface index is derived from the surface UUID ordering within each workspace.
    """
    if not entries:
        return entries

    ws_id_to_name = _load_cmux_workspace_map()

    for entry in entries:
        ws_id = entry.get("workspaceId", "")
        if ws_id and ws_id in ws_id_to_name:
            entry["workspaceName"] = ws_id_to_name[ws_id]

    # For surface index: group entries by workspace and assign index by
    # surface UUID sort order within each workspace. This gives a stable
    # ordering even without calling the cmux CLI.
    ws_surfaces: dict[str, list[str]] = {}
    for entry in entries:
        ws_id = entry.get("workspaceId", "")
        sf_id = entry.get("surfaceId", "")
        if ws_id and sf_id:
            ws_surfaces.setdefault(ws_id, [])
            if sf_id not in ws_surfaces[ws_id]:
                ws_surfaces[ws_id].append(sf_id)

    # Build surface id -> index map (sorted by surface UUID for stability)
    sf_id_to_index: dict[str, int] = {}
    for ws_id, sf_ids in ws_surfaces.items():
        sf_ids.sort()
        for idx, sf_id in enumerate(sf_ids):
            sf_id_to_index[sf_id] = idx

    for entry in entries:
        sf_id = entry.get("surfaceId", "")
        if sf_id and sf_id in sf_id_to_index:
            entry["surfaceIndex"] = sf_id_to_index[sf_id]

    return entries


def get_cwd(pid: int) -> str | None:
    out = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]).stdout
    for line in out.splitlines():
        if line.startswith("n/"):
            return line[1:]
    return None


def list_candidate_processes(mode: str = "ghostty") -> list[dict]:
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

        if mode == "cmux":
            cmux_ids = is_cmux_child(pid)
            if not cmux_ids:
                continue
        else:
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
        if mode == "cmux" and cmux_ids:
            entry["workspaceId"] = cmux_ids[0]
            entry["surfaceId"] = cmux_ids[1]
        if tool == "codex":
            sid = resolve_codex_session_id_for_pid(pid)
            if sid:
                entry["sessionId"] = sid
        else:
            sid = resolve_claude_session_id_for_pid(pid)
            if sid:
                entry["sessionId"] = sid
        entries.append(entry)

    entries.sort(key=lambda x: x["tty"])
    return entries


def list_entries_for_terminals(terminals: set[str]) -> list[dict]:
    entries: list[dict] = []
    seen_pids: set[int] = set()
    if "cmux" in terminals:
        for entry in enrich_cmux_entries(list_candidate_processes(mode="cmux")):
            pid = int(entry.get("pid") or 0)
            if pid and pid not in seen_pids:
                entries.append(entry)
                seen_pids.add(pid)
    if "ghostty" in terminals:
        for entry in list_candidate_processes(mode="ghostty"):
            pid = int(entry.get("pid") or 0)
            if pid and pid in seen_pids:
                continue
            entries.append(entry)
    entries.sort(key=lambda x: (x.get("tty", ""), int(x.get("pid") or 0)))
    return entries


def write_snapshot(entries: list[dict]) -> None:
    write_json_atomic(SNAPSHOT_PATH, entries)


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
    sessions = resolve_sessions(entries)
    if not sessions:
        # Keep the previous non-empty live state. This avoids clobbering restore
        # data on transient empty snapshots (for example during crash races).
        return 0
    write_json_atomic(LIVE_STATE_PATH, sessions)
    return len(sessions)


def _filter_sessions_by_terminal(sessions: list[dict], terminal: str | None) -> list[dict]:
    if not terminal:
        return sessions
    if terminal == "cmux":
        return [s for s in sessions if s.get("terminal") == "cmux"]
    # Ghostty entries are legacy/no-terminal or explicitly non-cmux.
    return [s for s in sessions if s.get("terminal") != "cmux"]


def _merge_with_existing_restore(
    sessions: list[dict], terminal: str | None
) -> list[dict]:
    if not terminal:
        return sessions
    existing = load_sessions_file(RESTORE_PATH)
    if terminal == "cmux":
        preserved = [s for s in existing if s.get("terminal") != "cmux"]
    else:
        preserved = [s for s in existing if s.get("terminal") == "cmux"]
    return preserved + sessions


def save_sessions(terminal: str | None = None) -> tuple[int, int, int]:
    sessions = load_sessions_file(LIVE_STATE_PATH)
    sessions = _filter_sessions_by_terminal(sessions, terminal)
    if not sessions:
        snapshot = load_snapshot()
        if not snapshot:
            return (0, 0, 0)
        sessions = resolve_sessions(snapshot)
        sessions = _filter_sessions_by_terminal(sessions, terminal)
    if not sessions:
        return (0, 0, 0)

    write_json_atomic(RESTORE_PATH, _merge_with_existing_restore(sessions, terminal))

    resumed = sum(1 for x in sessions if x.get("sessionId"))
    continued = len(sessions) - resumed
    return (len(sessions), resumed, continued)


def should_save_on_shutdown() -> bool:
    if is_ghostty_running() or _is_cmux_socket_alive():
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


def terminal_scope_for_final_save(prev_terminals: set[str]) -> str | None:
    """Use terminal-scoped save when exactly one terminal just closed."""
    if len(prev_terminals) == 1:
        return next(iter(prev_terminals))
    return None


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
            f"{base} ({len(unresolved_codex_pids)} codex unresolved -> new-session fallback)",
            unresolved_codex_pids,
        )
    return base, unresolved_codex_pids


def main() -> int:
    log(f"Started (PID {os.getpid()})")

    prev_terminals: set[str] = set()
    prev_signature: tuple = tuple()
    prev_unresolved_codex_pids: tuple[int, ...] = tuple()
    empty_started_at: float | None = None
    empty_live_state_cleared = False
    n = 0

    while RUNNING:
        terminals = detect_terminals()

        if terminals:
            closed_terminals = prev_terminals - terminals
            for closed in sorted(closed_terminals):
                log(f"{closed.capitalize()} closed while other terminal active - saving")
                total, resumed, continued = save_sessions(terminal=closed)
                if total:
                    log(
                        f"Sessions saved: {total} total "
                        f"({resumed} resume, {continued} continue)"
                    )
                else:
                    log(f"No {closed} sessions to save")
            if terminals != prev_terminals:
                detected = "+".join(sorted(terminals))
                log(f"Terminal detected: {detected}")
                prev_terminals = set(terminals)

            entries = list_entries_for_terminals(terminals)

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
                (
                    entry["pid"],
                    entry.get("sessionId"),
                    entry.get("cwd", ""),
                    entry["args"],
                    entry.get("workspaceId", ""),
                    entry.get("surfaceId", ""),
                )
                for entry in entries
            )
            if signature != prev_signature:
                prev_signature = signature
                write_snapshot(entries)
                persist_live_state(entries)
                message, prev_unresolved_codex_pids = snapshot_log_message(
                    entries, prev_unresolved_codex_pids
                )
                log(message)
        elif prev_terminals:
            label = "+".join(sorted(prev_terminals))
            log(f"{label.capitalize()} quit - saving")
            save_terminal = terminal_scope_for_final_save(prev_terminals)
            total, resumed, continued = save_sessions(terminal=save_terminal)
            if total:
                log(
                    f"Sessions saved: {total} total "
                    f"({resumed} resume, {continued} continue)"
                )
            else:
                log("No sessions to save")

            prev_terminals = set()
            prev_signature = tuple()
            prev_unresolved_codex_pids = tuple()
            empty_started_at = None
            empty_live_state_cleared = False
            write_snapshot([])

        n = (n + 1) % 500
        if n == 0:
            truncate_log()

        time.sleep(POLL_INTERVAL_SECONDS)

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
