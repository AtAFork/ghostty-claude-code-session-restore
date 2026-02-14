# Ghostty Session Manager

Save and restore Claude Code sessions across Ghostty restarts and reboots.

## Problem

When you quit Ghostty (or restart your computer), all your Claude Code sessions are lost. If you had 10 tabs open with different conversations — including multiple sessions in the same project directory — you'd have to manually re-open each one and figure out which conversation to resume.

## How It Works

### Architecture

Two components work together:

```
ghostty-session-watcher (launchd daemon, runs outside Ghostty)
├── Every 2s: check if Ghostty is running
├── On PID change: snapshot Ghostty-only claude processes (PIDs, CWDs, args)
├── On Ghostty quit: extract session IDs + flags, save restore file
└── Runs at login, survives reboots (KeepAlive)

.bashrc snippet + restore.sh (runs inside Ghostty on shell startup)
├── Detects restore file exists
├── Acquires a lock (prevents multiple shells from restoring simultaneously)
├── Calls ghostty-restore --auto to create tabs via AppleScript Cmd+T
├── Types cd + claude commands into each tab
└── Runs first session directly in the original shell
```

### Save: The Snapshot Approach

The watcher snapshots running sessions **every 2 seconds** (only when PIDs change — zero I/O otherwise). Each snapshot captures:

- **PID** and **TTY** (for tab ordering)
- **CWD** (working directory)
- **Full command args** (`--chrome`, `--dangerously-skip-permissions`, `--resume <id>`, etc.)

When Ghostty quits, the most recent snapshot — taken while everything was still alive — becomes the save state.

- **Close a single tab**: The claude process exits. Next snapshot no longer includes it. It won't be restored.
- **Quit Ghostty entirely**: All processes die simultaneously. The watcher uses the last snapshot (from before the quit) to save all sessions.

### Ghostty-Only Filtering

The watcher **only captures claude processes running inside Ghostty**, not other terminals. For each claude process on a TTY, it walks up the parent process chain (PPID, up to 6 levels) and checks if any ancestor is the `ghostty` binary. This prevents contamination from:

- Solo terminal sessions
- iTerm sessions
- VS Code / Cursor integrated terminals
- Any other terminal emulator

### Session ID Resolution

Session IDs are resolved using a three-tier approach:

1. **From `--resume <id>` in args** (exact match): If the process was started with `--resume`, the session ID is extracted directly from the command line arguments. This is always correct and covers all previously-restored sessions.

2. **From project directory session files** (for fresh sessions, including multiple tabs in the same project): For processes without `--resume`, the watcher finds the Claude project directory for the CWD, lists all `.jsonl` session files, and filters to **real conversations only** — files that contain at least one `"type":"user"` entry. Stub files from failed restore attempts (which only contain `file-history-snapshot` entries) are skipped. The N most recently modified real session files are assigned to the N unresolved processes in that project.

   This correctly handles multiple tabs in the same project directory: if you have 2 tabs in `~/project-a`, the 2 most recently active real conversations from that project are restored.

3. **`--continue` fallback**: If no real session file can be found (e.g., the user started claude but never sent a message, or the project has no session history), the restore uses `claude --continue` which automatically continues the most recent conversation in that project directory.

After a successful restore cycle, all sessions will have `--resume <id>` in their args (since the restore starts them with `--resume`). Tiers 2 and 3 are only needed for sessions the user started manually.

These tiers were chosen after extensive testing showed that simpler approaches are unreliable:

- **`history.jsonl`** stores the `project` field as the git root, not the CWD. Multiple subdirectory sessions map to the same project, making CWD-based matching fail.
- **File birth time matching** gets poisoned by stub `.jsonl` files created during failed restore attempts. Each failed `claude --resume 'bad-id'` creates a new session file that matches the next birth time search.
- **File mtime sorting** picks the most recently touched file in a directory that may contain dozens of old sessions, with no reliable way to correlate a specific PID to a specific file.

After a successful restore cycle, all sessions will have `--resume <id>` in their args (since the restore script starts them with `--resume`). The `--continue` fallback is only needed for sessions the user started manually.

### CLI Flag Preservation

All CLI flags are captured from the running process's command line and stored in the restore file. When sessions are restored, they launch with the same flags:

```
Original:   claude --chrome --dangerously-skip-permissions
Saved as:   { "flags": "--chrome --dangerously-skip-permissions", ... }
Restored:   claude --resume <id> --chrome --dangerously-skip-permissions
         or claude --continue --chrome --dangerously-skip-permissions
```

Flags that are preserved include:
- `--chrome` (Chrome MCP integration)
- `--dangerously-skip-permissions` (bypass permission prompts)
- `--verbose`, `--model`, and any other CLI flags

The `--resume` and `--continue` flags are handled separately and stripped from the saved flags to avoid duplication.

### Restore: Automatic via .bashrc + AppleScript

When Ghostty opens, the `.bashrc` snippet:

1. Checks if `TERM_PROGRAM == ghostty` (skips in other terminals)
2. Checks if `~/.claude/ghostty-restore.json` exists
3. Acquires a lock via `mkdir` (atomic, prevents race conditions from multiple tabs)
4. Calls `ghostty-restore --auto` which:
   - Parses the restore file into arrays of session IDs, CWDs, and flags
   - For sessions 2..N: creates new tabs via AppleScript `Cmd+T` keystrokes
   - Types `cd '/path' && claude --resume '<id>' <flags>` (or `claude --continue <flags>`) into each tab
   - Switches back to Tab 1 with `Cmd+1`
5. Runs the first session directly in the original shell
6. Deletes the restore file

**Why AppleScript from inside Ghostty?** Tab creation via `Cmd+T` requires Accessibility permission. Running AppleScript from inside Ghostty inherits Ghostty's existing Accessibility permission. Running from the launchd daemon would require adding `/bin/bash` to Accessibility, which macOS doesn't allow (only app bundles).

**Why not `open -na Ghostty.app`?** Each `open -na` invocation creates a **separate Ghostty process**, and windows from different instances cannot be merged into tabs. `Cmd+T` creates real tabs in the same window.

### No Merge — Clean Saves Only

Each save completely overwrites the restore file. Previous versions merged new saves with existing restore data, which caused stale sessions from failed restores to persist and reappear. The current design: whatever was running when Ghostty quit is exactly what gets restored.

## Prerequisites

- **macOS** (uses `launchd`, `osascript`, macOS-specific process tools)
- **Ghostty** terminal emulator
- **Claude Code** CLI (`claude`)
- **Python 3** (ships with macOS; used for JSON processing)
- **Ghostty Accessibility permission**: System Settings > Privacy & Security > Accessibility > enable Ghostty

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/AtAFork/ghostty-claude-code-session-restore.git
cd ghostty-claude-code-session-restore
```

### 2. Create required directories

```bash
mkdir -p ~/.local/bin
mkdir -p ~/.claude/debug
```

### 3. Create symlinks for the scripts

```bash
ln -sf "$(pwd)/watcher.sh" ~/.local/bin/ghostty-session-watcher
ln -sf "$(pwd)/restore.sh" ~/.local/bin/ghostty-restore
```

### 4. Install the launchd plist

The plist contains hardcoded paths that you must update for your username.

```bash
# Replace YOUR_USERNAME with your actual macOS username
sed "s/YOUR_USERNAME/$(whoami)/g" com.user.ghostty-session-watcher.plist \
  > ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

> **Note:** The plist is a **copy**, not a symlink. launchd on macOS sometimes fails to follow symlinks for plist files.

### 5. Add the auto-restore snippet to your .bashrc

Append the following to the end of your `~/.bashrc`:

```bash
# Auto-restore Claude Code sessions in Ghostty
if [[ "$TERM_PROGRAM" == "ghostty" ]] && [[ -f "$HOME/.claude/ghostty-restore.json" ]]; then
  if mkdir "$HOME/.claude/.ghostty-restore-lock" 2>/dev/null; then
    _restore_first=$("$HOME/.local/bin/ghostty-restore" --auto 2>/dev/null)
    rmdir "$HOME/.claude/.ghostty-restore-lock" 2>/dev/null
    if [[ -n "$_restore_first" ]]; then
      IFS=$'\t' read -r _r_sid _r_cwd _r_flags <<< "$_restore_first"
      cd "$_r_cwd" 2>/dev/null
      if [[ -n "$_r_sid" ]]; then
        if [[ -n "$_r_flags" ]]; then
          eval "claude --resume '$_r_sid' $_r_flags"
        else
          claude --resume "$_r_sid"
        fi
      else
        if [[ -n "$_r_flags" ]]; then
          eval "claude --continue $_r_flags"
        else
          claude --continue
        fi
      fi
    fi
    unset _restore_first _r_sid _r_cwd _r_flags
  fi
fi
```

### 6. Load and start the watcher

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

### 7. Verify installation

```bash
# Check the watcher is running
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher | head -5

# Check the log (should show "Started")
cat ~/.claude/debug/ghostty-session-watcher.log
```

Open Ghostty, start a Claude Code session, quit Ghostty, then reopen it. Your session should auto-restore.

## Resource Usage

The daemon is designed to be invisible:

| Phase | What runs | Cost |
|-------|-----------|------|
| **Hot loop** (normal) | `pgrep ghostty` + `sleep 2` | ~0% CPU, 0 disk I/O |
| **PID change** | `ps` + `lsof` + PPID chain walk per process | ~5ms, once per session open/close |
| **Ghostty quit** | One `python3` invocation | ~50ms, once per quit |

- **Memory**: ~2MB RSS for the bash process (just sleeping)
- **Disk**: Snapshot file is <1KB in `/tmp`, overwritten only on state change
- **Log file**: Auto-truncated at 50KB (never grows unbounded)
- **Nice level**: 10 (low scheduling priority)
- **I/O priority**: `LowPriorityBackgroundIO` in launchd

No growing data structures. No memory leaks. Everything is read fresh each cycle.

## Startup Behavior

The launchd agent starts the watcher:
- **On login** (RunAtLoad)
- **On reboot** (automatically, since it runs at load)
- **If it crashes** (KeepAlive restarts it with 5s throttle)

Full flow after a reboot:

```
1. macOS boots, you log in
2. launchd starts the watcher daemon
3. You open Ghostty
4. First shell's .bashrc detects the restore file
5. ghostty-restore --auto creates tabs for each saved session
6. Each tab starts claude with the original flags:
   - Known session ID  →  claude --resume <id> <flags>
   - Unknown session   →  claude --continue <flags>
7. Restore file is deleted
8. Watcher resumes snapshotting the new sessions
```

After this first restore cycle, all sessions will have `--resume <id>` in their args, so subsequent save/restore cycles will be exact.

## Files

### Source (this directory)

```
ghostty-session-manager/
├── watcher.sh                              # Background daemon (save only)
├── restore.sh                              # Restore script (creates tabs via AppleScript)
├── com.user.ghostty-session-watcher.plist  # launchd agent configuration (template — needs username substitution)
└── README.md                               # This file
```

### Installed

```
~/.local/bin/ghostty-session-watcher → ./watcher.sh  (symlink)
~/.local/bin/ghostty-restore         → ./restore.sh  (symlink)
~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist  (copy, not symlink)
~/.bashrc                            # Auto-restore snippet appended at end of file
```

Note: The plist is a **copy**, not a symlink. launchd on macOS sometimes fails to follow symlinks for plist files (I/O error on bootstrap).

### Runtime Files

```
/tmp/ghostty-session-snapshot.json       # Current session snapshot (ephemeral, <1KB)
~/.claude/ghostty-restore.json           # Saved sessions pending restore (deleted after restore)
~/.claude/.ghostty-restore-lock/         # Lock directory (created/removed atomically)
~/.claude/debug/ghostty-session-watcher.log  # Daemon log (auto-truncated at 50KB)
```

### Snapshot Format

```json
[
  {
    "pid": 12345,
    "tty": "ttys007",
    "cwd": "/Users/you/project-a",
    "args": "claude --chrome --dangerously-skip-permissions"
  },
  {
    "pid": 67890,
    "tty": "ttys010",
    "cwd": "/Users/you/project-b",
    "args": "claude --resume 904135b4-... --chrome --dangerously-skip-permissions"
  }
]
```

### Restore File Format

```json
[
  {
    "sessionId": null,
    "cwd": "/Users/you/project-a",
    "flags": "--chrome --dangerously-skip-permissions"
  },
  {
    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
    "cwd": "/Users/you/project-b",
    "flags": "--chrome --dangerously-skip-permissions"
  }
]
```

- `sessionId` present → `claude --resume <id> <flags>`
- `sessionId` null → `claude --continue <flags>`

## Manual Usage

Normally everything is automatic. These are for debugging or edge cases.

```bash
# Manual restore (if auto-restore didn't trigger)
ghostty-restore

# Check watcher status
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher

# View watcher logs
tail -20 ~/.claude/debug/ghostty-session-watcher.log

# View current snapshot (what sessions are being tracked right now)
cat /tmp/ghostty-session-snapshot.json | python3 -m json.tool

# Restart the watcher
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist

# View the pending restore file
cat ~/.claude/ghostty-restore.json | python3 -m json.tool

# Clear the restore file (prevent next restore)
rm ~/.claude/ghostty-restore.json

# Remove a stale lock (if restore hangs)
rmdir ~/.claude/.ghostty-restore-lock
```

## Troubleshooting

### Sessions not being captured

Check the snapshot file:
```bash
cat /tmp/ghostty-session-snapshot.json | python3 -m json.tool
```

If a session is missing, it might be running in a non-Ghostty terminal. The watcher only captures claude processes whose parent process chain includes `ghostty`.

### "No conversation found" on restore

This means the session ID in the restore file doesn't match any real conversation. This can only happen with `--resume` sessions. The `--continue` fallback avoids this by always picking the most recent valid conversation.

To fix: clear the restore file and let sessions restart fresh:
```bash
rm ~/.claude/ghostty-restore.json
```

### Session picker appears instead of auto-resuming

If a tab shows the "Resume Session" picker UI, it means the session was started without `--resume` or `--continue`. This shouldn't happen with the current version — non-resume sessions always use `--continue`.

### Extra or phantom tabs

Previous versions merged saves, which caused stale sessions to persist. The current version overwrites completely on each save. If you see phantom tabs, clear the restore file:
```bash
rm ~/.claude/ghostty-restore.json
```

### Accessibility permission

Ghostty must have Accessibility permission in System Settings > Privacy & Security > Accessibility for AppleScript tab creation to work. The restore script runs inside Ghostty and inherits its permission.

### Watcher not starting

```bash
# Check if it's loaded
launchctl print gui/$(id -u)/com.user.ghostty-session-watcher

# Check for errors
cat ~/.claude/debug/ghostty-session-watcher-stderr.log

# Verify the plist is valid
plutil -lint ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist

# Re-bootstrap
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist
```

## Design Decisions

### Why filter session files by content instead of using simpler methods?

For sessions without `--resume` in their args, the session ID is not externally observable. We explored and rejected several simpler approaches:

1. **`history.jsonl` lookup**: The `project` field stores the git root, not the CWD. Sessions in `~/project/subdir-a/` and `~/project/subdir-b/` both log `project: ~/project`, making matching ambiguous.

2. **File birth time matching**: A fresh `claude` invocation creates a `.jsonl` session file. We can match the file's birth time to the process's start time. However, failed restore attempts also create stub session files (containing only `file-history-snapshot` entries, no real conversation data), poisoning the pool. The birth time search then matches these stubs instead of real conversations.

3. **File mtime sorting without content check**: A project directory can contain dozens of old session files plus stubs from failed restores. Without checking content, mtime sorting picks stubs (which have very recent mtimes) over real conversations.

The current approach reads the first 10 lines of each candidate `.jsonl` file and checks for `"type":"user"` entries. This reliably distinguishes real conversations from stubs with zero false positives. The N most recently modified real files are assigned to the N unresolved processes. `--continue` remains as a final fallback when no real session files exist at all.

### Why Ghostty-only filtering?

Without filtering, the watcher captures claude processes from ALL terminals (Solo, iTerm, VS Code, etc.). This caused phantom sessions to appear in saves — a Solo terminal session would be saved as a "Ghostty session" and then fail to restore because it was never in Ghostty.

The PPID chain walk (checking if any ancestor process is `ghostty`) is the most reliable filter. It correctly handles the process hierarchy: `ghostty → login → bash → claude`.

### Why a background daemon instead of a Ghostty quit hook?

Ghostty doesn't have a "before quit" hook. By the time you could detect the quit, all child processes are already dead and their session info is lost. The daemon snapshots while everything is still alive.

### Why bash instead of Python for the daemon?

The hot loop is just `pgrep` + `sleep` — ~0% CPU. Python would add ~20MB RSS for the interpreter overhead. Python is only invoked for JSON escaping (on PID changes) and session saving (on quit).

### Why `mkdir` for locking?

`mkdir` is atomic on all filesystems. `flock` is not reliably available on all macOS versions. The lock prevents multiple `.bashrc` instances from running the restore simultaneously when Ghostty opens multiple tabs at once.

### Why a copy of the plist instead of a symlink?

launchd on macOS sometimes fails to follow symlinks for plist files, returning "I/O error" on bootstrap. A direct copy avoids this.

### Why no merge on save?

Merging caused stale sessions from failed restores to persist across multiple quit/reopen cycles. Clean overwrites ensure the restore file always reflects exactly what was running when Ghostty quit — nothing more, nothing less.

## Limitations

- **2-second granularity**: If you close a tab and quit Ghostty within 2 seconds, that closed tab's session might appear in the restore.
- **Accessibility permission**: Ghostty must have Accessibility permission for AppleScript tab creation.
- **`--continue` picks most recent**: For fresh sessions (no `--resume`), `--continue` resumes the most recent conversation in the project. If you had two fresh sessions in the same project directory, both would resume the same conversation. After one restore cycle, each gets a unique `--resume <id>` and this is no longer an issue.
- **macOS only**: Uses `launchd`, `osascript`, and Ghostty-specific AppleScript.
- **Single window**: All sessions restore as tabs in one window. Multi-window layouts are not preserved.

## Uninstall

```bash
# Stop and remove the launchd agent
launchctl bootout gui/$(id -u)/com.user.ghostty-session-watcher
rm ~/Library/LaunchAgents/com.user.ghostty-session-watcher.plist

# Remove symlinks
rm ~/.local/bin/ghostty-session-watcher ~/.local/bin/ghostty-restore

# Remove the .bashrc snippet
# Edit ~/.bashrc and delete from "# Auto-restore Claude Code sessions in Ghostty" to the final "fi"

# Clean up runtime files
rm -f ~/.claude/ghostty-restore.json /tmp/ghostty-session-snapshot.json
rmdir ~/.claude/.ghostty-restore-lock 2>/dev/null

# Remove the source directory (adjust path to where you cloned it)
rm -rf ~/ghostty-session-manager
```
