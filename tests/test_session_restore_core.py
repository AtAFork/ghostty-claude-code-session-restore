#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(REPO_ROOT))

from session_restore_core import (  # noqa: E402
    build_restore_argv,
    codex_is_interactive,
    extract_claude_flags,
    extract_codex_flags,
    extract_codex_session_id_from_lsof_text,
    normalize_restore_entry,
    resolve_sessions,
)


class SessionRestoreCoreTests(unittest.TestCase):
    def test_extract_claude_flags_preserves_value_pairs(self) -> None:
        args = (
            "claude --resume 904135b4-8584-42dd-aeb9-08b920d0e02e "
            "--model sonnet --verbose --setting-sources user,project "
            "--dangerously-skip-permissions"
        )
        flags = extract_claude_flags(args)
        self.assertEqual(
            flags,
            [
                "--model",
                "sonnet",
                "--verbose",
                "--setting-sources",
                "user,project",
                "--dangerously-skip-permissions",
            ],
        )

    def test_extract_codex_flags_strips_resume_bits(self) -> None:
        args = (
            "codex --model gpt-5 resume 019c5bce-a952-7380-b204-bfe40bf783b6 "
            "--search --config reasoning_level=xhigh --last"
        )
        flags = extract_codex_flags(args)
        self.assertEqual(
            flags, ["--model", "gpt-5", "--search", "--config", "reasoning_level=xhigh"]
        )

    def test_codex_is_interactive(self) -> None:
        self.assertTrue(codex_is_interactive("codex --yolo resume"))
        self.assertTrue(codex_is_interactive("codex --model gpt-5"))
        self.assertFalse(codex_is_interactive("codex exec \"echo hi\""))

    def test_extract_codex_session_id_from_lsof_text(self) -> None:
        lsof_text = (
            "codex 61580 user 16w REG 1,4 123 "
            "/Users/user/.codex/sessions/2026/02/14/"
            "rollout-2026-02-14T19-59-56-019c5bce-a952-7380-b204-bfe40bf783b6.jsonl"
        )
        sid = extract_codex_session_id_from_lsof_text(lsof_text)
        self.assertEqual(sid, "019c5bce-a952-7380-b204-bfe40bf783b6")

    def test_build_restore_argv_for_codex_unknown_session(self) -> None:
        entry = normalize_restore_entry(
            {
                "tool": "codex",
                "sessionId": None,
                "cwd": "/tmp/project",
                "flags": ["--model", "gpt-5"],
            }
        )
        self.assertEqual(
            build_restore_argv(entry),
            ["codex", "resume", "--last", "--model", "gpt-5"],
        )

    def test_resolve_sessions_for_codex_entry(self) -> None:
        snapshot = [
            {
                "tool": "codex",
                "cwd": "/tmp/codex-proj",
                "args": (
                    "codex --model gpt-5 resume "
                    "019c5bce-a952-7380-b204-bfe40bf783b6 --search"
                ),
            }
        ]
        sessions = resolve_sessions(snapshot, "/tmp/unused")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["tool"], "codex")
        self.assertEqual(sessions[0]["sessionId"], "019c5bce-a952-7380-b204-bfe40bf783b6")
        self.assertEqual(sessions[0]["flags"], ["--model", "gpt-5", "--search"])

    def test_resolve_sessions_assigns_real_claude_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            projects = Path(tempdir) / "projects"
            projects.mkdir(parents=True, exist_ok=True)

            cwd = "/tmp/project-a"
            encoded = cwd.replace("/", "-")
            project_dir = projects / encoded
            project_dir.mkdir(parents=True, exist_ok=True)

            # Claimed by explicit --resume in snapshot.
            claimed_sid = "11111111-1111-1111-1111-111111111111"
            unresolved_should_get = "22222222-2222-2222-2222-222222222222"
            stub_sid = "33333333-3333-3333-3333-333333333333"

            (project_dir / f"{claimed_sid}.jsonl").write_text(
                json.dumps({"type": "user"}) + "\n", encoding="utf-8"
            )
            real_path = project_dir / f"{unresolved_should_get}.jsonl"
            real_path.write_text(json.dumps({"type": "user"}) + "\n", encoding="utf-8")
            stub_path = project_dir / f"{stub_sid}.jsonl"
            stub_path.write_text(
                json.dumps({"type": "file-history-snapshot"}) + "\n", encoding="utf-8"
            )

            # mtime order: stub newest, real next, claimed oldest.
            os.utime(project_dir / f"{claimed_sid}.jsonl", (1, 1))
            os.utime(real_path, (2, 2))
            os.utime(stub_path, (3, 3))

            snapshot = [
                {
                    "tool": "claude",
                    "cwd": cwd,
                    "args": f"claude --resume {claimed_sid} --model sonnet",
                },
                {
                    "tool": "claude",
                    "cwd": cwd,
                    "args": "claude --model sonnet --verbose",
                },
            ]

            sessions = resolve_sessions(snapshot, str(projects))
            self.assertEqual(sessions[0]["sessionId"], claimed_sid)
            self.assertEqual(sessions[1]["sessionId"], unresolved_should_get)
            self.assertEqual(sessions[1]["flags"], ["--model", "sonnet", "--verbose"])


if __name__ == "__main__":
    unittest.main()
