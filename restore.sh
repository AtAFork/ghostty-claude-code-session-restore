#!/usr/bin/env python3
"""ghostty-restore — restore saved Claude/Codex sessions as Ghostty/Cmux tabs."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from session_restore_core import (  # noqa: E402
    build_restore_argv,
    build_tab_command,
    load_restore_file,
    normalize_restore_entry,
    shell_join,
)

RESTORE_FILE = Path.home() / ".claude" / "ghostty-restore.json"
LIVE_STATE_FILE = Path.home() / ".claude" / "ghostty-live-state.json"
CMUX_WORKSPACE_MAP_FILE = Path.home() / ".claude" / "cmux-workspace-map.json"
GHOSTTY_RESTORE_LOCK_DIR = Path.home() / ".claude" / ".ghostty-restore-lock"
CMUX_RESTORE_LOCK_DIR = Path.home() / ".claude" / ".ghostty-cmux-restore-lock"
STALE_LOCK_MAX_AGE_SECONDS = 300.0


def applescript_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def create_tab_and_run(command: str) -> bool:
    escaped = applescript_escape(command)
    script = (
        'tell application "System Events" to tell process "ghostty"\n'
        '  keystroke "t" using command down\n'
        "  delay 0.3\n"
        f'  keystroke "{escaped}"\n'
        "  delay 0.1\n"
        "  key code 36\n"
        "end tell\n"
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def switch_to_tab_one() -> bool:
    script = (
        'tell application "System Events" to tell process "ghostty"\n'
        '  keystroke "1" using command down\n'
        "end tell\n"
    )
    proc = subprocess.run(
        ["osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )


def cleanup_stale_lock(path: Path, max_age_seconds: float = STALE_LOCK_MAX_AGE_SECONDS) -> None:
    try:
        if not path.is_dir():
            return
        age = time.time() - path.stat().st_mtime
        if age > max_age_seconds:
            path.rmdir()
    except OSError:
        pass


def _parse_cmux_json(raw: str, key: str) -> list[dict]:
    """Parse cmux JSON output, handling both nested and flat formats.

    cmux wraps arrays under a named key (e.g. {"workspaces": [...]}).
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return inner
    return []


def update_cmux_workspace_map() -> None:
    """Write workspace UUID -> name mapping for the watcher to read.

    This runs inside cmux where the cmux CLI works. The watcher (launchd agent)
    reads this file to enrich session entries with workspace names.
    """
    proc = _run(["cmux", "--json", "--id-format", "both", "list-workspaces"])
    if proc.returncode != 0:
        return
    workspaces = _parse_cmux_json(proc.stdout, "workspaces")
    if not workspaces:
        return
    mapping = {}
    for ws in workspaces:
        ws_id = ws.get("id") or ""
        ws_name = ws.get("title") or ws.get("name") or ""
        if ws_id and ws_name:
            mapping[ws_id] = ws_name
    try:
        CMUX_WORKSPACE_MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
        CMUX_WORKSPACE_MAP_FILE.write_text(
            json.dumps(mapping, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _build_workspace_maps() -> tuple[dict[str, str], dict[str, str]]:
    """Return (workspace_name->ref, workspace_id->ref)."""
    name_to_ref: dict[str, str] = {}
    id_to_ref: dict[str, str] = {}
    proc = _run(["cmux", "--json", "--id-format", "both", "list-workspaces"])
    if proc.returncode != 0:
        return name_to_ref, id_to_ref
    for ws in _parse_cmux_json(proc.stdout, "workspaces"):
        ws_id = ws.get("id") or ""
        ws_name = ws.get("title") or ws.get("name") or ""
        ws_ref = ws.get("ref") or ws_id
        if ws_name and ws_ref:
            name_to_ref[ws_name] = ws_ref
        if ws_id and ws_ref:
            id_to_ref[ws_id] = ws_ref
    return name_to_ref, id_to_ref


def _resolve_workspace_ref(
    session: dict,
    name_to_ref: dict[str, str],
    id_to_ref: dict[str, str],
) -> str:
    """Prefer workspaceId (stable), then fallback to workspaceName."""
    ws_id = session.get("workspaceId", "")
    if ws_id and ws_id in id_to_ref:
        return id_to_ref[ws_id]
    ws_name = session.get("workspaceName", "")
    if ws_name and ws_name in name_to_ref:
        return name_to_ref[ws_name]
    return ""


def _resolve_surface_ref(session: dict, surfaces: list[dict]) -> str:
    """Prefer surfaceId (stable), then fallback to legacy surfaceIndex."""
    sf_id = session.get("surfaceId", "")
    if sf_id:
        for surface in surfaces:
            if sf_id in {surface.get("id", ""), surface.get("ref", "")}:
                ref = surface.get("ref") or surface.get("id") or ""
                if ref:
                    return ref
    sf_index = session.get("surfaceIndex", 0)
    if 0 <= sf_index < len(surfaces):
        surface = surfaces[sf_index]
        return surface.get("ref") or surface.get("id") or ""
    return ""


def _dedupe_sessions(sessions: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for session in sessions:
        normalized = normalize_restore_entry(session)
        key = (
            normalized.get("tool"),
            normalized.get("sessionId"),
            normalized.get("cwd"),
            tuple(normalized.get("flags", [])),
            normalized.get("terminal", ""),
            normalized.get("workspaceId", ""),
            normalized.get("surfaceId", ""),
            int(normalized.get("surfaceIndex", 0)),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _clear_restore_state_files(*, preserve_terminal: str | None = None) -> None:
    """Remove state files, optionally preserving entries for another terminal.

    When ghostty finishes restoring, it should preserve cmux entries (and vice
    versa) so the other terminal can still retry.
    """
    if preserve_terminal:
        # Collect entries for the other terminal from both state files.
        keep: list[dict] = []
        for path in (RESTORE_FILE, LIVE_STATE_FILE):
            for s in load_restore_file(path):
                if s.get("terminal") == preserve_terminal:
                    keep.append(s)
        deduped = _dedupe_sessions(keep)
        if deduped:
            try:
                RESTORE_FILE.write_text(
                    json.dumps(deduped, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            # Keep live-state aligned to avoid replaying stale sessions.
            try:
                LIVE_STATE_FILE.write_text(
                    json.dumps(deduped, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
            return
    try:
        RESTORE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        LIVE_STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _restore_cmux_sessions(
    sessions: list[dict],
    *,
    verbose: bool,
) -> tuple[int, list[dict]]:
    """Return (restored_count, failed_sessions)."""
    if not sessions:
        return 0, []

    name_to_ref, id_to_ref = _build_workspace_maps()
    restored = 0
    failed: list[dict] = []

    for session in sessions:
        session = normalize_restore_entry(session)
        ws_name = session.get("workspaceName", "")
        sf_index = session.get("surfaceIndex", 0)
        workspace_ref = _resolve_workspace_ref(session, name_to_ref, id_to_ref)
        if not workspace_ref:
            if verbose:
                print(f"  Skipping - workspace '{ws_name or '?'}' not found")
            failed.append(session)
            continue

        proc = _run(["cmux", "--json", "list-pane-surfaces", "--workspace", workspace_ref])
        if proc.returncode != 0:
            if verbose:
                print(f"  Skipping - cannot list surfaces for '{ws_name or '?'}'")
            failed.append(session)
            continue

        surfaces = _parse_cmux_json(proc.stdout, "surfaces")
        if not surfaces:
            if verbose:
                print(f"  Skipping - no surfaces in '{ws_name or '?'}'")
            failed.append(session)
            continue

        surface_ref = _resolve_surface_ref(session, surfaces)
        if not surface_ref:
            if verbose:
                sf_id = session.get("surfaceId", "")
                if sf_id:
                    print(f"  Skipping - surface id '{sf_id}' not found in '{ws_name or '?'}'")
                else:
                    print(
                        f"  Skipping - surface index {sf_index} out of range in "
                        f"'{ws_name or '?'}'"
                    )
            failed.append(session)
            continue

        cwd = session.get("cwd", "")
        argv = build_restore_argv(session)
        cmd = f"cd {shlex.quote(cwd)} && {shell_join(argv)}"

        send_proc = _run(["cmux", "send", "--surface", surface_ref, "--", cmd])
        key_proc = _run(["cmux", "send-key", "--surface", surface_ref, "Return"])
        if send_proc.returncode == 0 and key_proc.returncode == 0:
            restored += 1
        else:
            if verbose:
                print(
                    f"  Skipping - failed to send command to '{ws_name or '?'}' "
                    f"(surface {surface_ref})"
                )
            failed.append(session)

    return restored, failed


def do_cmux_auto_restore(sessions: list[dict]) -> int:
    """Auto-restore cmux sessions, called from the shell startup snippet."""
    restored, failed_cmux = _restore_cmux_sessions(sessions, verbose=False)

    # Preserve non-cmux entries from both state files + any cmux sessions that failed.
    non_cmux: list[dict] = []
    for path in (RESTORE_FILE, LIVE_STATE_FILE):
        for s in load_restore_file(path):
            if s.get("terminal") != "cmux":
                non_cmux.append(s)
    keep = _dedupe_sessions(non_cmux + failed_cmux)
    non_cmux_kept = [s for s in keep if s.get("terminal") != "cmux"]
    if keep:
        try:
            RESTORE_FILE.write_text(json.dumps(keep, indent=2), encoding="utf-8")
        except OSError:
            pass
    else:
        try:
            RESTORE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    # Only delete live-state if no failures and no ghostty sessions need it.
    if not failed_cmux and not non_cmux_kept:
        try:
            LIVE_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return restored


def detect_terminal_mode() -> str:
    """Return 'cmux', 'ghostty', or 'unknown'."""
    if os.environ.get("CMUX_WORKSPACE_ID"):
        return "cmux"
    if os.environ.get("TERM_PROGRAM", "").lower() == "ghostty":
        return "ghostty"
    return "unknown"


def do_cmux_manual_restore(sessions: list[dict]) -> int:
    """Restore cmux sessions by sending commands to matching workspace surfaces."""
    if not sessions:
        print("No cmux sessions to restore.")
        return 0

    print(f"Found {len(sessions)} cmux session(s):")
    for idx, entry in enumerate(sessions, start=1):
        entry = normalize_restore_entry(entry)
        ws_name = entry.get("workspaceName", "?")
        sf_idx = entry.get("surfaceIndex", 0)
        tool = entry["tool"]
        sid = entry.get("sessionId")
        cwd = entry["cwd"]
        if tool == "codex":
            mode = f"resume {sid[:8]}..." if sid else "start new"
        else:
            mode = f"resume {sid[:8]}..." if sid else "continue most recent"
        print(f"  {idx}. [{tool}] {cwd}  ({mode})  workspace={ws_name} surface={sf_idx}")

    confirm = input("Restore all? [Y/n] ").strip().lower()
    if confirm.startswith("n"):
        print("Cancelled.")
        return 0

    restored, failed = _restore_cmux_sessions(sessions, verbose=True)
    print(f"Restored {restored}/{len(sessions)} session(s).")
    if failed:
        print(f"  {len(failed)} session(s) could not be restored.")
    _clear_restore_state_files()
    return 0


def describe_entry(entry: dict, index: int) -> str:
    entry = normalize_restore_entry(entry)
    tool = entry["tool"]
    sid = entry["sessionId"]
    cwd = entry["cwd"]
    flags = " ".join(entry["flags"]).strip()
    flags_text = f" [{flags}]" if flags else ""

    if tool == "codex":
        mode = f"resume {sid[:8]}..." if sid else "start new"
    else:
        mode = f"resume {sid[:8]}..." if sid else "continue most recent"
    return f"  {index}. [{tool}] {cwd}  ({mode}){flags_text}"


def load_sessions(auto: bool, terminal_mode: str = "ghostty") -> tuple[list[dict], Path | None]:
    candidates: list[Path] = []
    if RESTORE_FILE.exists():
        candidates.append(RESTORE_FILE)
    if LIVE_STATE_FILE.exists():
        candidates.append(LIVE_STATE_FILE)

    if not candidates:
        if not auto:
            print("No saved sessions to restore.")
        return [], None

    for source in candidates:
        sessions = load_restore_file(source)
        if not sessions:
            continue

        filtered = []
        for entry in sessions:
            # Filter by terminal mode
            entry_terminal = entry.get("terminal", "")
            if terminal_mode == "cmux":
                if entry_terminal != "cmux":
                    continue
            else:
                # Ghostty mode: accept entries without terminal field or non-cmux
                if entry_terminal == "cmux":
                    continue

            cwd = entry["cwd"]
            if Path(cwd).is_dir():
                filtered.append(entry)
            elif not auto:
                print(f"  Skipping - {cwd} gone")
        if filtered:
            return filtered, source

    if not auto:
        print("No valid sessions.")
    return [], None


def auto_output_first_session(entry: dict) -> None:
    # Keep auto mode output machine-readable for shell startup snippets.
    print(json.dumps(entry, separators=(",", ":")))


def run_first_session(entry: dict) -> int:
    entry = normalize_restore_entry(entry)
    cwd = entry["cwd"]
    argv = build_restore_argv(entry)

    try:
        os.chdir(cwd)
    except OSError as exc:
        print(f"Failed to cd into {cwd}: {exc}", file=sys.stderr)
        return 1

    print("Starting first session...")
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        print(f"Failed to launch {' '.join(argv)}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--auto-cmux", action="store_true")
    parser.add_argument("--update-cmux-map", action="store_true")
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    cleanup_stale_lock(GHOSTTY_RESTORE_LOCK_DIR)
    cleanup_stale_lock(CMUX_RESTORE_LOCK_DIR)

    # --update-cmux-map: write workspace UUID->name mapping for the watcher.
    if args.update_cmux_map:
        update_cmux_workspace_map()
        return 0

    # --auto-cmux: shell startup snippet triggered cmux restore.
    if args.auto_cmux:
        update_cmux_workspace_map()
        sessions, _ = load_sessions(auto=True, terminal_mode="cmux")
        if sessions:
            restored = do_cmux_auto_restore(sessions)
            if restored:
                print(f"Restored {restored} cmux session(s).", file=sys.stderr)
        return 0

    terminal_mode = detect_terminal_mode()

    if not args.auto:
        if terminal_mode == "cmux":
            sessions, _ = load_sessions(auto=False, terminal_mode="cmux")
            if not sessions:
                return 0
            return do_cmux_manual_restore(sessions)

        if terminal_mode == "unknown":
            print("Unknown terminal. Restore requires Ghostty or Cmux.")
            return 1

    # Ghostty / --auto mode (--auto always assumes ghostty since
    # the .bashrc snippet already gates on TERM_PROGRAM=ghostty,
    # and cmux doesn't use --auto).
    sessions, _ = load_sessions(args.auto, terminal_mode="ghostty")
    if not sessions:
        return 0

    if not args.auto:
        print(f"Found {len(sessions)} saved session(s):")
        for idx, entry in enumerate(sessions, start=1):
            print(describe_entry(entry, idx))
        confirm = input("Restore all? [Y/n] ").strip().lower()
        if confirm.startswith("n"):
            print("Cancelled.")
            return 0

    remaining_sessions_for_retry = []

    # Create tabs for sessions 2..N.
    for idx, entry in enumerate(sessions[1:], start=1):
        ok = create_tab_and_run(build_tab_command(entry))
        if not ok:
            remaining_sessions_for_retry = sessions[idx:]
            break
        time.sleep(0.3)

    if len(sessions) > 1 and not remaining_sessions_for_retry:
        switch_to_tab_one()
        time.sleep(0.2)

    if remaining_sessions_for_retry:
        # Preserve unresolved ghostty tail + any pending cmux sessions.
        cmux_pending: list[dict] = []
        for path in (RESTORE_FILE, LIVE_STATE_FILE):
            for s in load_restore_file(path):
                if s.get("terminal") == "cmux":
                    cmux_pending.append(s)
        keep = _dedupe_sessions(remaining_sessions_for_retry + cmux_pending)
        try:
            RESTORE_FILE.write_text(
                json.dumps(keep, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        if not args.auto:
            print(
                f"Warning: failed to create some tabs; preserved "
                f"{len(remaining_sessions_for_retry)} session(s) for retry."
            )
    else:
        _clear_restore_state_files(preserve_terminal="cmux")

    first = normalize_restore_entry(sessions[0])
    if args.auto:
        auto_output_first_session(first)
        return 0
    return run_first_session(first)


if __name__ == "__main__":
    raise SystemExit(main())
