#!/bin/bash
# ghostty-session-watcher — saves Claude Code sessions when Ghostty quits.
# Restore is handled by the .bashrc snippet + restore.sh (inside Ghostty).
#
# Hot loop: pgrep + sleep 2 = ~0% CPU, 0 disk I/O
# On PID change: ps + lsof per process = ~2ms
# On quit: one python3 call = ~50ms

set -uo pipefail

SNAPSHOT="/tmp/ghostty-session-snapshot.json"
RESTORE="$HOME/.claude/ghostty-restore.json"
LOG="$HOME/.claude/debug/ghostty-session-watcher.log"

mkdir -p "$(dirname "$LOG")"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"; }

truncate_log() {
  [[ -f "$LOG" ]] && [[ $(stat -f%z "$LOG" 2>/dev/null || echo 0) -gt 51200 ]] && \
    tail -100 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
}

prev_pids=""
ghostty_was_running=false
n=0

trap 'log "Shutting down"; rm -f "$SNAPSHOT"; exit 0' INT TERM

is_ghostty_child() {
  # Walk up the PPID chain (max 6 levels) and check if any ancestor is ghostty
  local current=$1
  local i
  for i in 1 2 3 4 5 6; do
    current=$(ps -p "$current" -o ppid= 2>/dev/null | tr -d ' ')
    [[ -z "$current" || "$current" == "0" || "$current" == "1" ]] && return 1
    local name
    name=$(ps -p "$current" -o comm= 2>/dev/null)
    [[ "$name" == *ghostty* ]] && return 0
  done
  return 1
}

take_snapshot() {
  # Find interactive claude processes on TTYs that are children of Ghostty
  local pids_list=""
  local entries="["
  local first=true

  while IFS= read -r line; do
    local pid tty
    pid=$(echo "$line" | awk '{print $1}')
    tty=$(echo "$line" | awk '{print $2}')
    [[ -z "$pid" ]] && continue

    # Only include Ghostty children (skip Solo, iTerm, etc.)
    is_ghostty_child "$pid" || continue

    # Get full command args
    local args
    args=$(ps -p "$pid" -o args= 2>/dev/null || true)
    [[ -z "$args" ]] && continue

    # Get CWD
    local cwd
    cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n/' | sed 's/^n//' || true)
    [[ -z "$cwd" ]] && continue

    # Escape args for JSON
    local args_escaped
    args_escaped=$(printf '%s' "$args" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()), end="")' 2>/dev/null || echo "\"$args\"")

    pids_list+="$pid "
    $first || entries+=","
    entries+="{\"pid\":$pid,\"tty\":\"$tty\",\"cwd\":\"$cwd\",\"args\":$args_escaped}"
    first=false
  done < <(ps -eo pid,tty,comm 2>/dev/null | grep 'ttys' | grep 'claude$' | sort -k2)

  [[ "$pids_list" == "$prev_pids" ]] && return
  prev_pids="$pids_list"

  entries+="]"
  echo "$entries" > "$SNAPSHOT"

  local count
  count=$(echo "$pids_list" | wc -w | tr -d ' ')
  log "Snapshot: ${count} session(s)"
}

save_sessions() {
  python3 - "$SNAPSHOT" "$HOME/.claude/projects" "$RESTORE" << 'PYEOF'
import json, os, re, sys
from collections import defaultdict

snapshot_f, projects_dir, restore_f = sys.argv[1:4]

try:
    with open(snapshot_f) as f:
        snapshot = json.loads(f.read())
except (FileNotFoundError, json.JSONDecodeError):
    sys.exit(0)

if not snapshot:
    sys.exit(0)

def extract_resume_id(args):
    """Extract session ID from --resume flag in command args."""
    m = re.search(r'--resume\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', args)
    return m.group(1) if m else None

def extract_flags(args):
    """Extract CLI flags, excluding --resume/--continue and their values, and the command name."""
    parts = args.split()
    flags = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part == '--resume':
            skip_next = True
            continue
        if part in ('--continue', '-c'):
            continue
        if part.startswith('-'):
            flags.append(part)
    return ' '.join(flags) if flags else ''

def find_project_dir(cwd):
    """Find the Claude project directory for a given CWD.
    Tries exact CWD first, then walks up to parent directories."""
    path = cwd
    while path and path != '/':
        encoded = path.replace('/', '-')
        candidate = os.path.join(projects_dir, encoded)
        if os.path.isdir(candidate):
            return candidate
        path = os.path.dirname(path)
    return None

def is_real_session(filepath):
    """Check if a session file has actual user messages (not just stubs from failed restores).
    Reads the first 10 lines looking for a 'type':'user' entry."""
    try:
        with open(filepath) as f:
            for i, line in enumerate(f):
                if i >= 10:
                    break
                try:
                    entry = json.loads(line.strip())
                    if entry.get('type') == 'user':
                        return True
                except json.JSONDecodeError:
                    continue
    except (OSError, IOError):
        pass
    return False

def get_recent_real_sessions(proj_dir, count, claimed_ids):
    """Get the N most recently modified REAL session files in a project directory.
    Skips stubs and already-claimed session IDs."""
    if not proj_dir:
        return []

    candidates = []
    for f in os.listdir(proj_dir):
        if not f.endswith('.jsonl') or f == 'sessions-index.json':
            continue
        sid = f.replace('.jsonl', '')
        if sid in claimed_ids:
            continue
        fpath = os.path.join(proj_dir, f)
        mtime = os.path.getmtime(fpath)
        candidates.append((sid, fpath, mtime))

    # Sort by mtime descending (most recent first)
    candidates.sort(key=lambda x: x[2], reverse=True)

    # Filter to real sessions (have user messages) and take top N
    result = []
    for sid, fpath, mtime in candidates:
        if len(result) >= count:
            break
        if is_real_session(fpath):
            result.append(sid)
    return result

# First pass: extract session IDs from --resume args
sessions = []
claimed_ids = set()
for entry in snapshot:
    args = entry.get('args', '')
    sid = extract_resume_id(args)
    flags = extract_flags(args)
    sessions.append({
        'sessionId': sid,
        'cwd': entry['cwd'],
        'flags': flags,
        '_index': len(sessions)
    })
    if sid:
        claimed_ids.add(sid)

# Second pass: resolve unresolved sessions by project directory
# Group unresolved sessions by project dir
proj_groups = defaultdict(list)
for s in sessions:
    if not s['sessionId']:
        proj_dir = find_project_dir(s['cwd'])
        if proj_dir:
            proj_groups[proj_dir].append(s)

# For each project, find N recent real sessions
for proj_dir, entries in proj_groups.items():
    needed = len(entries)
    found_sids = get_recent_real_sessions(proj_dir, needed, claimed_ids)
    for i, s in enumerate(entries):
        if i < len(found_sids):
            s['sessionId'] = found_sids[i]
            claimed_ids.add(found_sids[i])

# Clean up internal field and write
for s in sessions:
    s.pop('_index', None)

# Overwrite restore file completely (no merge)
with open(restore_f, 'w') as f:
    json.dump(sessions, f, indent=2)

resumed = len([s for s in sessions if s['sessionId']])
continued = len([s for s in sessions if not s['sessionId']])
print(f"Saved {len(sessions)} session(s) ({resumed} resume, {continued} continue)")
PYEOF
}

log "Started (PID $$)"

while true; do
  if pgrep -x ghostty >/dev/null 2>&1; then
    ghostty_was_running=true
    take_snapshot
  elif $ghostty_was_running; then
    log "Ghostty quit — saving"
    save_sessions 2>> "$LOG" && log "Sessions saved" || log "Save failed"
    ghostty_was_running=false
    prev_pids=""
    echo '[]' > "$SNAPSHOT"
  fi

  n=$(( (n + 1) % 500 ))
  [[ $n -eq 0 ]] && truncate_log

  sleep 2
done
