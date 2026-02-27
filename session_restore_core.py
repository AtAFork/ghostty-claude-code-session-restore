#!/usr/bin/env python3
"""Shared parsing and restore planning helpers for Ghostty session restore."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Iterable, List

UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
SCHEMA_VERSION = 1

CODEX_INTERACTIVE_SUBCOMMANDS = {"resume", "fork"}
CODEX_NON_INTERACTIVE_SUBCOMMANDS = {
    "exec",
    "review",
    "login",
    "logout",
    "mcp",
    "mcp-server",
    "app-server",
    "app",
    "completion",
    "sandbox",
    "debug",
    "apply",
    "cloud",
    "features",
    "help",
}

# Options that consume a value token if provided as "--opt value".
# "--opt=value" is handled naturally by tokenization and does not consume extra tokens.
CLAUDE_OPTS_WITH_VALUE = {
    "--add-dir",
    "--agent",
    "--agents",
    "--allowedTools",
    "--allowed-tools",
    "--append-system-prompt",
    "--betas",
    "--debug",
    "--debug-file",
    "--disallowedTools",
    "--disallowed-tools",
    "--effort",
    "--fallback-model",
    "--file",
    "--from-pr",
    "--input-format",
    "--json-schema",
    "--max-budget-usd",
    "--mcp-config",
    "--model",
    "--output-format",
    "--permission-mode",
    "--plugin-dir",
    "--session-id",
    "--setting-sources",
    "--settings",
    "--system-prompt",
    "--tools",
    "-r",
}

CODEX_OPTS_WITH_VALUE = {
    "-c",
    "--config",
    "--enable",
    "--disable",
    "-i",
    "--image",
    "-m",
    "--model",
    "--local-provider",
    "-p",
    "--profile",
    "-s",
    "--sandbox",
    "-a",
    "--ask-for-approval",
    "-C",
    "--cd",
    "--add-dir",
}


def parse_shell_tokens(args: str) -> List[str]:
    """Tokenize a process command string into argv-like tokens."""
    if not args:
        return []
    try:
        return shlex.split(args)
    except ValueError:
        # Best-effort fallback for malformed process command lines.
        return args.split()


def _looks_like_uuid(value: str) -> bool:
    return bool(UUID_PATTERN.fullmatch(value or ""))


def _basename(token: str) -> str:
    return os.path.basename(token or "")


def extract_claude_resume_id(args: str) -> str | None:
    tokens = parse_shell_tokens(args)
    for idx, token in enumerate(tokens):
        if token in {"--resume", "-r"} and idx + 1 < len(tokens):
            candidate = tokens[idx + 1]
            if _looks_like_uuid(candidate):
                return candidate
    return None


def extract_codex_resume_id(args: str) -> str | None:
    tokens = parse_shell_tokens(args)
    for idx, token in enumerate(tokens):
        if token in CODEX_INTERACTIVE_SUBCOMMANDS and idx + 1 < len(tokens):
            candidate = tokens[idx + 1]
            if _looks_like_uuid(candidate):
                return candidate
    return None


def codex_is_interactive(args: str) -> bool:
    """True when a codex process is likely an interactive session worth restoring."""
    tokens = parse_shell_tokens(args)
    if not tokens:
        return False
    tokens = tokens[1:] if _basename(tokens[0]) == "codex" else tokens

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token.startswith("-"):
            if token in CODEX_OPTS_WITH_VALUE and idx + 1 < len(tokens):
                idx += 2
            else:
                idx += 1
            continue

        # First non-option token is the subcommand (if any).
        if token in CODEX_INTERACTIVE_SUBCOMMANDS:
            return True
        if token in CODEX_NON_INTERACTIVE_SUBCOMMANDS:
            return False
        # Unknown bare token is treated as prompt for interactive default mode.
        return True

    # No explicit subcommand => interactive default codex mode.
    return True


def _extract_tokens_with_values(
    args: str,
    *,
    tool: str,
    skip_tokens: set[str],
    opts_with_value: set[str],
    skip_following_uuid_after: set[str] | None = None,
) -> List[str]:
    tokens = parse_shell_tokens(args)
    if tokens and _basename(tokens[0]) == tool:
        tokens = tokens[1:]

    out: list[str] = []
    idx = 0
    skip_following_uuid_after = skip_following_uuid_after or set()
    while idx < len(tokens):
        token = tokens[idx]

        if token in skip_tokens:
            # Option-style skip that can consume a value.
            if token in opts_with_value and idx + 1 < len(tokens):
                idx += 2
            else:
                idx += 1
            continue

        if token in skip_following_uuid_after:
            idx += 1
            if idx < len(tokens) and _looks_like_uuid(tokens[idx]):
                idx += 1
            continue

        if token.startswith("-"):
            out.append(token)
            if token in opts_with_value and idx + 1 < len(tokens):
                nxt = tokens[idx + 1]
                if not nxt.startswith("-"):
                    out.append(nxt)
                    idx += 2
                    continue
            idx += 1
            continue

        # Non-option positional tokens are not preserved in restore flags.
        idx += 1

    return out


def extract_claude_flags(args: str) -> List[str]:
    return _extract_tokens_with_values(
        args,
        tool="claude",
        skip_tokens={"--resume", "-r", "--continue", "-c"},
        opts_with_value=CLAUDE_OPTS_WITH_VALUE,
    )


def extract_codex_flags(args: str) -> List[str]:
    return _extract_tokens_with_values(
        args,
        tool="codex",
        skip_tokens={"--last", "--all"},
        opts_with_value=CODEX_OPTS_WITH_VALUE,
        skip_following_uuid_after=CODEX_INTERACTIVE_SUBCOMMANDS,
    )


def extract_codex_session_id_from_lsof_text(text: str) -> str | None:
    """Extract codex session UUID from open rollout paths in lsof output."""
    for line in text.splitlines():
        match = re.search(
            r"/\.codex/(?:sessions/\d{4}/\d{2}/\d{2}|archived_sessions)/"
            r"rollout-[^-\s]+-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-"
            r"([0-9a-f-]{36})\.jsonl",
            line,
            flags=re.IGNORECASE,
        )
        if match and _looks_like_uuid(match.group(1)):
            return match.group(1)
    return None


def extract_claude_session_id_from_lsof_text(text: str) -> str | None:
    """Extract claude session UUID from open project session paths in lsof output."""
    for line in text.splitlines():
        match = re.search(
            r"/\.claude/projects/[^/\s]+/([0-9a-f-]{36})\.jsonl",
            line,
            flags=re.IGNORECASE,
        )
        if match and _looks_like_uuid(match.group(1)):
            return match.group(1)
    return None


def resolve_codex_session_id_for_pid(pid: int) -> str | None:
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return extract_codex_session_id_from_lsof_text(proc.stdout)


def resolve_claude_session_id_for_pid(pid: int) -> str | None:
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return extract_claude_session_id_from_lsof_text(proc.stdout)


def normalize_flags(flags: object) -> List[str]:
    if isinstance(flags, list):
        out = []
        for item in flags:
            if isinstance(item, str) and item:
                out.append(item)
        return out
    if isinstance(flags, str):
        return parse_shell_tokens(flags)
    return []


def normalize_restore_entry(entry: dict) -> dict:
    tool = str(entry.get("tool") or "claude").strip().lower()
    if tool not in {"claude", "codex"}:
        tool = "claude"
    result = {
        "tool": tool,
        "sessionId": entry.get("sessionId") or None,
        "cwd": str(entry.get("cwd") or ""),
        "flags": normalize_flags(entry.get("flags")),
    }
    # Pass through cmux metadata when present
    if entry.get("terminal"):
        result["terminal"] = entry["terminal"]
    if entry.get("workspaceName"):
        result["workspaceName"] = entry["workspaceName"]
    if entry.get("workspaceId"):
        result["workspaceId"] = entry["workspaceId"]
    if entry.get("surfaceId"):
        result["surfaceId"] = entry["surfaceId"]
    if "surfaceIndex" in entry:
        try:
            idx = int(entry["surfaceIndex"])
        except (TypeError, ValueError):
            idx = 0
        if idx < 0:
            idx = 0
        result["surfaceIndex"] = idx
    return result


def resolve_sessions(snapshot: list[dict]) -> list[dict]:
    sessions = []

    for raw in snapshot:
        tool = str(raw.get("tool") or "").strip().lower()
        if tool not in {"claude", "codex"}:
            # Backward compatibility: infer from command args.
            args_lower = str(raw.get("args") or "").lower()
            if "codex" in args_lower:
                tool = "codex"
            else:
                tool = "claude"

        args = str(raw.get("args") or "")
        if tool == "claude":
            sid = raw.get("sessionId") or extract_claude_resume_id(args)
            flags = extract_claude_flags(args)
        else:
            sid = raw.get("sessionId") or extract_codex_resume_id(args)
            flags = extract_codex_flags(args)

        session_dict: dict = {
            "tool": tool,
            "sessionId": sid if sid else None,
            "cwd": str(raw.get("cwd") or ""),
            "flags": flags,
        }
        # Copy through cmux metadata from raw snapshot entries
        if raw.get("workspaceId"):
            session_dict["terminal"] = "cmux"
        elif raw.get("terminal"):
            session_dict["terminal"] = raw["terminal"]
        if raw.get("workspaceName"):
            session_dict["workspaceName"] = raw["workspaceName"]
        if raw.get("workspaceId"):
            session_dict["workspaceId"] = raw["workspaceId"]
        if raw.get("surfaceId"):
            session_dict["surfaceId"] = raw["surfaceId"]
        if "surfaceIndex" in raw:
            session_dict["surfaceIndex"] = raw["surfaceIndex"]
        sessions.append(session_dict)

    return sessions


def build_restore_argv(session: dict) -> list[str]:
    session = normalize_restore_entry(session)
    tool = session["tool"]
    sid = session["sessionId"]
    flags = session["flags"]

    if tool == "codex":
        if sid:
            return ["codex", "resume", sid, *flags]
        return ["codex", *flags]

    if sid:
        return ["claude", "--resume", sid, *flags]
    return ["claude", "--continue", *flags]


def shell_join(argv: Iterable[str]) -> str:
    try:
        return shlex.join(list(argv))
    except AttributeError:
        return " ".join(shlex.quote(x) for x in argv)


def build_tab_command(session: dict) -> str:
    session = normalize_restore_entry(session)
    cwd = session["cwd"] or "."
    argv = build_restore_argv(session)
    return f"cd {shlex.quote(cwd)} && {shell_join(argv)}"


def load_restore_file(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(raw, dict):
        version = raw.get("version")
        sessions = raw.get("sessions")
        if version != SCHEMA_VERSION or not isinstance(sessions, list):
            return []
        raw = sessions
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            normalized = normalize_restore_entry(item)
            if normalized["cwd"]:
                out.append(normalized)
    return out
