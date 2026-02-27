# Ghostty Session Manager

Save and restore Ghostty/Cmux tabs running Claude Code or Codex across terminal restarts and reboots.

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
├── On state change: continuously writes resolved live-state file
├── On Ghostty quit: writes restore file from live-state (or snapshot fallback)
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
3. Fallback to `codex` (start a new session in the same cwd with the same flags).

### Restore Behavior

For each saved session:

- `tool=claude` + `sessionId` -> `claude --resume <id> ...flags`
- `tool=claude` + no `sessionId` -> `claude --continue ...flags`
- `tool=codex` + `sessionId` -> `codex resume <id> ...flags`
- `tool=codex` + no `sessionId` -> `codex ...flags`

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
            codex "${_r_flags[@]}"
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

If you use Fish, add this to `~/.config/fish/config.fish`:

```fish
# Auto-restore Claude/Codex sessions in Ghostty (Fish)
if test "$TERM_PROGRAM" = "ghostty"; and test -f "$HOME/.claude/ghostty-restore.json"
    if mkdir "$HOME/.claude/.ghostty-restore-lock" 2>/dev/null
        set _restore_first_json ($HOME/.local/bin/ghostty-restore --auto 2>/dev/null)
        rmdir "$HOME/.claude/.ghostty-restore-lock" 2>/dev/null

        if test -n "$_restore_first_json"
            set _restore_parts (
                env RESTORE_FIRST_JSON="$_restore_first_json" python3 - <<'PY' | string split0
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

            if test (count $_restore_parts) -ge 3
                set _r_tool $_restore_parts[1]
                set _r_sid $_restore_parts[2]
                set _r_cwd $_restore_parts[3]
                set _r_flags $_restore_parts[4..-1]

                cd "$_r_cwd" 2>/dev/null

                if test "$_r_tool" = "codex"
                    if test -n "$_r_sid"
                        codex resume "$_r_sid" $_r_flags
                    else
                        codex $_r_flags
                    end
                else
                    if test -n "$_r_sid"
                        claude --resume "$_r_sid" $_r_flags
                    else
                        claude --continue $_r_flags
                    end
                end
            end
        end

        set -e _restore_first_json _restore_parts _r_tool _r_sid _r_cwd _r_flags
    end
end
```

### 6. Start (or restart) the watcher

```bash
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

### 7. Verify

```bash
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher | head -5
cat ~/.claude/debug/ghostty-session-watcher.log
```

## Resource Usage

The watcher is intentionally lightweight:

- Loop interval: every 2 seconds
- CPU: usually near idle between polls
- Memory: single Python process, typically low tens of MB
- Disk writes: small JSON snapshots/state files only on session-state changes

You can inspect active usage with:

```bash
ps -o pid,pcpu,pmem,rss,command -p "$(pgrep -f ghostty-session-watcher | head -1)"
```

## Runtime Files

```text
/tmp/ghostty-session-snapshot.json              # Latest live snapshot
~/.claude/ghostty-restore.json                  # Pending restore payload
~/.claude/ghostty-live-state.json               # Continuously updated latest state
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

- Verify the CLI process was launched from Ghostty (the watcher only captures
  processes whose parent chain contains `ghostty`).
- Check current snapshot:

```bash
cat /tmp/ghostty-session-snapshot.json | python3 -m json.tool
# Optional: inspect process ancestry for a running session
ps -o pid,ppid,command -p <CLAUDE_OR_CODEX_PID>
```

### Restore opens wrong Claude thread for unresolved sessions

This can happen only for Claude sessions without a known `sessionId`; fallback is `claude --continue`.

### Codex fallback picks the wrong thread

If no Codex `sessionId` is available, fallback is `codex` (new session).

### Watcher not starting

```bash
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher
cat ~/.claude/debug/ghostty-session-watcher-stderr.log
plutil -lint ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

## Cmux Support

[Cmux](https://cmux.dev) is a native macOS terminal wrapping Ghostty's libghostty with workspace management. The session manager auto-detects Cmux and handles snapshotting and restoring sessions across Cmux restarts.

### How It Works with Cmux

- **Auto-detection**: The watcher detects Cmux by checking if `/tmp/cmux.sock` is alive. If connected, it uses Cmux mode; otherwise it falls back to standard Ghostty mode.
- **Shell startup snippet required**: A small snippet in your shell config keeps the workspace map fresh and triggers restore on Cmux restart. The watcher (launchd agent) cannot call the `cmux` CLI directly due to Cmux's access control — only processes started inside Cmux can use it.
- **Workspace name matching**: Cmux UUIDs change across restarts, but workspace names persist. Sessions are matched to workspaces by name.
- **Surface index matching**: When a workspace has multiple surfaces (splits), sessions are matched by surface index within the workspace.

### Cmux Shell Startup Snippet

Add this to your shell startup file (`~/.bashrc`, `~/.bash_profile`, or `~/.zshrc`):

```bash
# Ghostty Session Manager — Cmux support
if [[ -n "$CMUX_WORKSPACE_ID" ]]; then
  # Keep workspace UUID -> name map fresh for the watcher
  "$HOME/.local/bin/ghostty-restore" --update-cmux-map 2>/dev/null &

  # Auto-restore saved cmux sessions (once per Cmux restart)
  if [[ -f "$HOME/.claude/ghostty-restore.json" ]]; then
    if mkdir "$HOME/.claude/.ghostty-cmux-restore-lock" 2>/dev/null; then
      "$HOME/.local/bin/ghostty-restore" --auto-cmux 2>/dev/null
      rmdir "$HOME/.claude/.ghostty-cmux-restore-lock" 2>/dev/null
    fi
  fi
fi
```

If you use Fish, add this to `~/.config/fish/config.fish`:

```fish
# Ghostty Session Manager — Cmux support (Fish)
if test -n "$CMUX_WORKSPACE_ID"
    $HOME/.local/bin/ghostty-restore --update-cmux-map 2>/dev/null &
    if test -f "$HOME/.claude/ghostty-restore.json"
        if mkdir "$HOME/.claude/.ghostty-cmux-restore-lock" 2>/dev/null
            $HOME/.local/bin/ghostty-restore --auto-cmux 2>/dev/null
            rmdir "$HOME/.claude/.ghostty-cmux-restore-lock" 2>/dev/null
        end
    end
end
```

### Cmux-Specific Data

Each session entry includes additional fields when running under Cmux:

```json
{
  "tool": "claude",
  "sessionId": "904135b4-...",
  "cwd": "/Users/you/project",
  "flags": ["--model", "sonnet"],
  "terminal": "cmux",
  "workspaceName": "my-project",
  "surfaceIndex": 0
}
```

### Manual Cmux Restore

```bash
# From within a Cmux terminal
ghostty-restore
```

This shows saved cmux sessions with workspace names and surface indices, then sends commands to matching workspaces.

### Cmux Troubleshooting

- **Workspace name must match**: If you renamed a workspace after the session was saved, restore won't find it. Use the same workspace names.
- **Cmux must be running**: Restore must be run from within a Cmux terminal (the `cmux` CLI only works from inside Cmux).
- **Surface count must match**: If the workspace had 2 surfaces when saved but only 1 after restart, the second session is skipped.

## Limitations

- 2-second snapshot granularity: closing a tab right before quitting Ghostty can race.
- macOS/Ghostty-specific by design.
- Restores to one window as tabs (multi-window layouts are not preserved).

## Uninstall

```bash
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher
rm ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
rm ~/.local/bin/ghostty-session-watcher ~/.local/bin/ghostty-restore
rm -f ~/.claude/ghostty-restore.json ~/.claude/ghostty-live-state.json
rm -f ~/.claude/cmux-workspace-map.json /tmp/ghostty-session-snapshot.json
rmdir ~/.claude/.ghostty-restore-lock ~/.claude/.ghostty-cmux-restore-lock 2>/dev/null
```
