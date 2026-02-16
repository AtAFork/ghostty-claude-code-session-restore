# Ghostty Session Manager

Save and restore Ghostty tabs running Claude Code or Codex across Ghostty restarts and reboots.

## Problem

When Ghostty quits, running AI CLI sessions are gone. Rebuilding multi-tab setups manually is slow, especially when multiple tabs share the same project directory.

## What This Restores

- Claude sessions (`claude`)
- Codex sessions (`codex`)
- Original working directory per tab
- Relevant CLI flags (including flags with values, like `--model sonnet`)

## How It Works

### Components

Two pieces work together:

```text
ghostty-session-watcher (launchd daemon, runs outside Ghostty)
├── Every 2s: checks if Ghostty is running
├── On state change: snapshots Ghostty-only claude/codex processes
├── On Ghostty quit: resolves session IDs + flags and writes restore file
└── Runs at login and restarts automatically (KeepAlive)

shell startup snippet + ghostty-restore (runs inside Ghostty)
├── Detects pending restore file
├── Acquires lock (prevents concurrent restores)
├── Calls ghostty-restore --auto (creates tabs via AppleScript Cmd+T)
├── Starts sessions 2..N in new tabs
└── Starts session 1 in the original shell
```

### Snapshot Strategy

The watcher snapshots live interactive sessions while Ghostty is still running, then uses the last snapshot after quit.

Each snapshot entry stores:

- `pid`
- `tty` (tab ordering)
- `cwd`
- `tool` (`claude` or `codex`)
- full command `args`
- `sessionId` when directly detectable (notably Codex via open rollout file)

### Ghostty-Only Filtering

Only processes with a Ghostty ancestor in their PPID chain are captured. This excludes sessions from iTerm, VS Code/Cursor terminals, etc.

### Session ID Resolution

#### Claude

1. Use `--resume <id>` if present in process args.
2. Otherwise resolve by project history in `~/.claude/projects`, filtered to real conversations (`"type":"user"` present).
3. Fallback to `claude --continue` when no session ID can be resolved.

#### Codex

1. Prefer session ID extracted from open rollout file (`~/.codex/sessions/.../rollout-...-<uuid>.jsonl`) while the process is alive.
2. Otherwise use `codex resume <id>` if present in args.
3. Fallback to `codex resume --last`.

### Restore Behavior

For each saved session:

- `tool=claude` + `sessionId` -> `claude --resume <id> ...flags`
- `tool=claude` + no `sessionId` -> `claude --continue ...flags`
- `tool=codex` + `sessionId` -> `codex resume <id> ...flags`
- `tool=codex` + no `sessionId` -> `codex resume --last ...flags`

## Prerequisites

- macOS (uses `launchd`, `osascript`, macOS `ps/lsof` behavior)
- Ghostty
- Python 3
- At least one CLI to restore:
  - `claude`
  - `codex`
- Ghostty Accessibility permission:
  - System Settings -> Privacy & Security -> Accessibility -> enable Ghostty

## Installation

### 1. Clone

```bash
git clone https://github.com/AtAFork/ghostty-claude-code-session-restore.git
cd ghostty-claude-code-session-restore
```

### 2. Create required directories

```bash
mkdir -p ~/.local/bin
mkdir -p ~/.claude/debug
```

### 3. Symlink scripts

```bash
ln -sf "$(pwd)/watcher.sh" ~/.local/bin/ghostty-session-watcher
ln -sf "$(pwd)/restore.sh" ~/.local/bin/ghostty-restore
```

### 4. Install launchd plist

```bash
sed "s/YOUR_USERNAME/$(whoami)/g" com.user.ghostty-session-watcher.plist \
  > ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

### 5. Add startup snippet

Add this to your shell startup file (`~/.bashrc`, `~/.bash_profile`, or `~/.zshrc` depending on your setup):

```bash
# Auto-restore Claude/Codex sessions in Ghostty
if [[ "$TERM_PROGRAM" == "ghostty" ]] && [[ -f "$HOME/.claude/ghostty-restore.json" ]]; then
  if mkdir "$HOME/.claude/.ghostty-restore-lock" 2>/dev/null; then
    _restore_first_json=$("$HOME/.local/bin/ghostty-restore" --auto 2>/dev/null)
    rmdir "$HOME/.claude/.ghostty-restore-lock" 2>/dev/null

    if [[ -n "$_restore_first_json" ]]; then
      _restore_parts=()
      while IFS= read -r -d '' _part; do
        _restore_parts+=("$_part")
      done < <(
        RESTORE_FIRST_JSON="$_restore_first_json" python3 - <<'PY'
import json
import os
import sys

try:
    entry = json.loads(os.environ.get("RESTORE_FIRST_JSON", ""))
except Exception:
    raise SystemExit(0)

tool = str(entry.get("tool") or "claude")
sid = str(entry.get("sessionId") or "")
cwd = str(entry.get("cwd") or "")
flags = entry.get("flags")
if not isinstance(flags, list):
    flags = []

for value in [tool, sid, cwd, *[x for x in flags if isinstance(x, str)]]:
    sys.stdout.buffer.write(value.encode("utf-8", "ignore"))
    sys.stdout.buffer.write(b"\0")
PY
      )

      if [[ ${#_restore_parts[@]} -ge 3 ]]; then
        _r_tool="${_restore_parts[0]}"
        _r_sid="${_restore_parts[1]}"
        _r_cwd="${_restore_parts[2]}"
        _r_flags=("${_restore_parts[@]:3}")

        cd "$_r_cwd" 2>/dev/null || true

        if [[ "$_r_tool" == "codex" ]]; then
          if [[ -n "$_r_sid" ]]; then
            codex resume "$_r_sid" "${_r_flags[@]}"
          else
            codex resume --last "${_r_flags[@]}"
          fi
        else
          if [[ -n "$_r_sid" ]]; then
            claude --resume "$_r_sid" "${_r_flags[@]}"
          else
            claude --continue "${_r_flags[@]}"
          fi
        fi
      fi
    fi

    unset _restore_first_json _restore_parts _part _r_tool _r_sid _r_cwd _r_flags
  fi
fi
```

### 6. Start the watcher

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

### 7. Verify

```bash
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher | head -5
cat ~/.claude/debug/ghostty-session-watcher.log
```

## Runtime Files

```text
/tmp/ghostty-session-snapshot.json              # Latest live snapshot
~/.claude/ghostty-restore.json                  # Pending restore payload
~/.claude/.ghostty-restore-lock/                # Startup lock
~/.claude/debug/ghostty-session-watcher.log     # Main watcher log
```

## Data Formats

### Snapshot format

```json
[
  {
    "pid": 61580,
    "tty": "ttys000",
    "cwd": "/Users/you/project-a",
    "tool": "codex",
    "args": "codex --yolo resume",
    "sessionId": "019c5bce-a952-7380-b204-bfe40bf783b6"
  },
  {
    "pid": 67890,
    "tty": "ttys010",
    "cwd": "/Users/you/project-b",
    "tool": "claude",
    "args": "claude --resume 904135b4-... --chrome"
  }
]
```

### Restore format

```json
[
  {
    "tool": "claude",
    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
    "cwd": "/Users/you/project-b",
    "flags": ["--chrome", "--dangerously-skip-permissions"]
  },
  {
    "tool": "codex",
    "sessionId": null,
    "cwd": "/Users/you/project-a",
    "flags": ["--model", "gpt-5"]
  }
]
```

## Manual Usage

```bash
# Manual restore
ghostty-restore

# Inspect snapshot
cat /tmp/ghostty-session-snapshot.json | python3 -m json.tool

# Inspect pending restore file
cat ~/.claude/ghostty-restore.json | python3 -m json.tool

# Restart watcher
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

## Troubleshooting

### Sessions are missing

- Confirm Ghostty ancestry filtering is expected for your workflow.
- Check current snapshot:

```bash
cat /tmp/ghostty-session-snapshot.json | python3 -m json.tool
```

### Restore opens wrong Claude thread for unresolved sessions

This can happen only for Claude sessions without a known `sessionId`; fallback is `claude --continue`.

### Codex fallback picks the wrong thread

If no Codex `sessionId` is available, fallback is `codex resume --last`.

### Watcher not starting

```bash
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher
cat ~/.claude/debug/ghostty-session-watcher-stderr.log
plutil -lint ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

## Limitations

- 2-second snapshot granularity: closing a tab right before quitting Ghostty can race.
- macOS/Ghostty-specific by design.
- Restores to one window as tabs (multi-window layouts are not preserved).

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher
rm ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
rm ~/.local/bin/ghostty-session-watcher ~/.local/bin/ghostty-restore
rm -f ~/.claude/ghostty-restore.json /tmp/ghostty-session-snapshot.json
rmdir ~/.claude/.ghostty-restore-lock 2>/dev/null
```
