#!/usr/bin/env python3
"""ghostty-restore — restore saved Claude/Codex sessions as Ghostty tabs."""

from __future__ import annotations

import argparse
import json
import os
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
)

RESTORE_FILE = Path.home() / ".claude" / "ghostty-restore.json"
LIVE_STATE_FILE = Path.home() / ".claude" / "ghostty-live-state.json"


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


def describe_entry(entry: dict, index: int) -> str:
    entry = normalize_restore_entry(entry)
    tool = entry["tool"]
    sid = entry["sessionId"]
    cwd = entry["cwd"]
    flags = " ".join(entry["flags"]).strip()
    flags_text = f" [{flags}]" if flags else ""

    if tool == "codex":
        mode = f"resume {sid[:8]}..." if sid else "resume --last"
    else:
        mode = f"resume {sid[:8]}..." if sid else "continue most recent"
    return f"  {index}. [{tool}] {cwd}  ({mode}){flags_text}"


def load_sessions(auto: bool) -> tuple[list[dict], Path | None]:
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
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    sessions, _ = load_sessions(args.auto)
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
        # Preserve unresolved tail sessions for a retry on next startup.
        try:
            RESTORE_FILE.write_text(
                json.dumps(remaining_sessions_for_retry, indent=2), encoding="utf-8"
            )
        except OSError:
            pass
        if not args.auto:
            print(
                f"Warning: failed to create some tabs; preserved "
                f"{len(remaining_sessions_for_retry)} session(s) for retry."
            )
    else:
        try:
            RESTORE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            LIVE_STATE_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    first = normalize_restore_entry(sessions[0])
    if args.auto:
        auto_output_first_session(first)
        return 0
    return run_first_session(first)


if __name__ == "__main__":
    raise SystemExit(main())
