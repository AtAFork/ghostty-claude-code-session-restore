#!/bin/bash
# ghostty-restore — Restore Claude Code sessions as Ghostty tabs.
#
# Must run INSIDE Ghostty (needs its Accessibility permission for AppleScript).
# Called automatically from .bashrc (--auto) or manually by the user.
#
# --auto: Create tabs silently, output first session's sid\tcwd\tflags for .bashrc to run.
# manual: Show sessions, confirm, create tabs, run first session in current shell.
#
# Session types:
#   sessionId present → claude --resume <id> <flags>
#   sessionId null    → claude --continue <flags>  (continues most recent conversation)

set -uo pipefail

RESTORE_FILE="$HOME/.claude/ghostty-restore.json"

auto=false
[[ "${1:-}" == "--auto" ]] && auto=true

if [[ ! -f "$RESTORE_FILE" ]]; then
  $auto || echo "No saved sessions to restore."
  exit 0
fi

# Parse restore file into parallel arrays
eval "$(python3 -c "
import json, os, sys

try:
    with open(os.path.expanduser('$RESTORE_FILE')) as f:
        sessions = json.loads(f.read())
except:
    sys.exit(1)

if not sessions:
    sys.exit(1)

sids = ' '.join(f\"'{s.get('sessionId') or ''}'\" for s in sessions)
cwds = ' '.join(f\"'{s['cwd']}'\" for s in sessions)
flags = ' '.join(f\"'{s.get('flags', '')}'\" for s in sessions)
print(f'_sids=({sids})')
print(f'_cwds=({cwds})')
print(f'_flags=({flags})')
" 2>/dev/null)" || { $auto || echo "No valid sessions."; exit 1; }

total=${#_sids[@]}
[[ $total -eq 0 ]] && exit 0

# Filter out sessions with missing directories
declare -a sids=()
declare -a cwds=()
declare -a flags=()
for i in $(seq 0 $((total - 1))); do
  if [[ -d "${_cwds[$i]}" ]]; then
    sids+=("${_sids[$i]}")
    cwds+=("${_cwds[$i]}")
    flags+=("${_flags[$i]}")
  else
    $auto || echo "  Skipping — ${_cwds[$i]} gone"
  fi
done
total=${#sids[@]}
[[ $total -eq 0 ]] && exit 0

if ! $auto; then
  echo "Found $total saved Claude Code session(s):"
  for i in $(seq 0 $((total - 1))); do
    local_flags=""
    [[ -n "${flags[$i]}" ]] && local_flags=" [${flags[$i]}]"
    if [[ -n "${sids[$i]}" ]]; then
      echo "  $((i+1)). ${cwds[$i]}  (resume ${sids[$i]:0:8}...)${local_flags}"
    else
      echo "  $((i+1)). ${cwds[$i]}  (continue most recent)${local_flags}"
    fi
  done
  read -rp "Restore all? [Y/n] " confirm
  [[ "$confirm" =~ ^[Nn] ]] && { echo "Cancelled."; exit 0; }
fi

# Build the claude command for a given session index
build_cmd() {
  local idx=$1
  local sid="${sids[$idx]}"
  local cwd="${cwds[$idx]}"
  local f="${flags[$idx]}"

  local cmd="cd '${cwd}'"
  if [[ -n "$sid" ]]; then
    # Known session ID: resume exactly
    cmd+=" && claude --resume '${sid}'"
  else
    # Unknown session ID: continue most recent conversation
    cmd+=" && claude --continue"
  fi
  if [[ -n "$f" ]]; then
    cmd+=" ${f}"
  fi
  echo "$cmd"
}

# Create tabs for sessions 2..N via AppleScript (runs inside Ghostty = has permission)
for i in $(seq 1 $((total - 1))); do
  cmd=$(build_cmd "$i")
  # Escape for AppleScript double-quoted string
  cmd_safe=$(printf '%s' "$cmd" | sed 's/\\/\\\\/g; s/"/\\"/g')

  osascript -e "
    tell application \"System Events\" to tell process \"ghostty\"
      keystroke \"t\" using command down
      delay 0.3
      keystroke \"${cmd_safe}\"
      delay 0.1
      key code 36
    end tell
  " 2>/dev/null

  sleep 0.3
done

# Switch back to first tab
if [[ $total -gt 1 ]]; then
  osascript -e '
    tell application "System Events" to tell process "ghostty"
      keystroke "1" using command down
    end tell
  ' 2>/dev/null
  sleep 0.2
fi

# Clean up
rm -f "$RESTORE_FILE"

if $auto; then
  # Output first session info for .bashrc to pick up and run
  # Format: sid\tcwd\tflags (sid may be empty for --continue sessions)
  printf '%s\t%s\t%s' "${sids[0]}" "${cwds[0]}" "${flags[0]}"
else
  # Manual mode: run first session directly
  cd "${cwds[0]}"
  echo "Restored $total session(s). Starting first session..."
  local_flags="${flags[0]}"
  local_sid="${sids[0]}"
  if [[ -n "$local_sid" ]]; then
    if [[ -n "$local_flags" ]]; then
      eval "claude --resume '$local_sid' $local_flags"
    else
      claude --resume "$local_sid"
    fi
  else
    if [[ -n "$local_flags" ]]; then
      eval "claude --continue $local_flags"
    else
      claude --continue
    fi
  fi
fi
