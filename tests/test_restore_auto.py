#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RESTORE_SCRIPT = REPO_ROOT / "restore.sh"


class RestoreAutoTests(unittest.TestCase):
    def run_restore_auto(
        self, home: Path, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOME"] = str(home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(RESTORE_SCRIPT), "--auto"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=env,
        )

    def test_auto_mode_single_session_with_quoted_path(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            restore_dir = home / ".claude"
            restore_dir.mkdir(parents=True, exist_ok=True)

            cwd = home / "project's one"
            cwd.mkdir(parents=True, exist_ok=True)

            payload = [
                {
                    "tool": "claude",
                    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
                    "cwd": str(cwd),
                    "flags": ["--model", "sonnet"],
                }
            ]
            (restore_dir / "ghostty-restore.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            out = json.loads(proc.stdout)
            self.assertEqual(out["tool"], "claude")
            self.assertEqual(out["sessionId"], "904135b4-8584-42dd-aeb9-08b920d0e02e")
            self.assertEqual(out["cwd"], str(cwd))
            self.assertEqual(out["flags"], ["--model", "sonnet"])

    def test_auto_mode_supports_legacy_string_flags_format(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            restore_dir = home / ".claude"
            restore_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project-two"
            cwd.mkdir(parents=True, exist_ok=True)

            payload = [
                {
                    "sessionId": None,
                    "cwd": str(cwd),
                    "flags": "--model gpt-5 --verbose",
                }
            ]
            (restore_dir / "ghostty-restore.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["tool"], "claude")
            self.assertIsNone(out["sessionId"])
            self.assertEqual(out["flags"], ["--model", "gpt-5", "--verbose"])

    def test_auto_mode_codex_payload_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            restore_dir = home / ".claude"
            restore_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project-codex"
            cwd.mkdir(parents=True, exist_ok=True)

            payload = [
                {
                    "tool": "codex",
                    "sessionId": "019c5bce-a952-7380-b204-bfe40bf783b6",
                    "cwd": str(cwd),
                    "flags": ["--model", "gpt-5", "--search"],
                }
            ]
            (restore_dir / "ghostty-restore.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["tool"], "codex")
            self.assertEqual(out["sessionId"], "019c5bce-a952-7380-b204-bfe40bf783b6")
            self.assertEqual(out["flags"], ["--model", "gpt-5", "--search"])

    def test_auto_mode_falls_back_to_live_state_when_restore_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project-live-only"
            cwd.mkdir(parents=True, exist_ok=True)

            payload = [
                {
                    "tool": "claude",
                    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
                    "cwd": str(cwd),
                    "flags": ["--model", "sonnet"],
                }
            ]
            live_state_path = claude_dir / "ghostty-live-state.json"
            live_state_path.write_text(json.dumps(payload), encoding="utf-8")

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            out = json.loads(proc.stdout)
            self.assertEqual(out["tool"], "claude")
            self.assertEqual(out["cwd"], str(cwd))
            self.assertFalse(
                live_state_path.exists(),
                "Live-state file should be cleared after successful restore.",
            )

    def test_auto_mode_uses_live_state_when_restore_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project-live-fallback"
            cwd.mkdir(parents=True, exist_ok=True)

            (claude_dir / "ghostty-restore.json").write_text("[]", encoding="utf-8")
            payload = [
                {
                    "tool": "codex",
                    "sessionId": "019c5bce-a952-7380-b204-bfe40bf783b6",
                    "cwd": str(cwd),
                    "flags": ["--model", "gpt-5"],
                }
            ]
            (claude_dir / "ghostty-live-state.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            out = json.loads(proc.stdout)
            self.assertEqual(out["tool"], "codex")
            self.assertEqual(out["cwd"], str(cwd))

    def test_auto_mode_preserves_cmux_entries_on_success(self) -> None:
        """Ghostty auto-restore must not delete pending cmux sessions from restore file."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            restore_dir = home / ".claude"
            restore_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project"
            cwd.mkdir(parents=True, exist_ok=True)

            ghostty_session = {
                "tool": "claude",
                "sessionId": "aaa-bbb-ccc",
                "cwd": str(cwd),
                "flags": [],
            }
            cmux_session = {
                "tool": "claude",
                "sessionId": "xxx-yyy-zzz",
                "cwd": str(cwd),
                "flags": [],
                "terminal": "cmux",
                "workspaceName": "dev",
                "workspaceId": "ws-1",
                "surfaceId": "sf-1",
                "surfaceIndex": 0,
            }
            restore_path = restore_dir / "ghostty-restore.json"
            restore_path.write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )

            proc = self.run_restore_auto(home)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            out = json.loads(proc.stdout)
            self.assertEqual(out["sessionId"], "aaa-bbb-ccc")

            # Cmux session must survive in restore file
            self.assertTrue(restore_path.exists(), "restore file should still exist")
            remaining = json.loads(restore_path.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["terminal"], "cmux")
            self.assertEqual(remaining[0]["sessionId"], "xxx-yyy-zzz")

    def test_auto_mode_does_not_replay_ghostty_from_live_state_after_preserve(self) -> None:
        """After preserving cmux entries, stale ghostty entries must not be replayed."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project"
            cwd.mkdir(parents=True, exist_ok=True)

            ghostty_session = {
                "tool": "claude",
                "sessionId": "ghostty-1",
                "cwd": str(cwd),
                "flags": [],
            }
            cmux_session = {
                "tool": "claude",
                "sessionId": "cmux-1",
                "cwd": str(cwd),
                "flags": [],
                "terminal": "cmux",
                "workspaceName": "dev",
                "workspaceId": "ws-1",
                "surfaceId": "sf-1",
                "surfaceIndex": 0,
            }

            (claude_dir / "ghostty-restore.json").write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )
            (claude_dir / "ghostty-live-state.json").write_text(
                json.dumps([ghostty_session, cmux_session]), encoding="utf-8"
            )

            first = self.run_restore_auto(home)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_out = json.loads(first.stdout)
            self.assertEqual(first_out["sessionId"], "ghostty-1")

            second = self.run_restore_auto(home)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), "")

            remaining = json.loads(
                (claude_dir / "ghostty-restore.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0].get("terminal"), "cmux")

    def test_auto_mode_partial_failure_dedupes_cmux_pending(self) -> None:
        """Partial ghostty restore should not duplicate cmux pending entries from both files."""
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            claude_dir = home / ".claude"
            claude_dir.mkdir(parents=True, exist_ok=True)
            cwd = home / "project"
            cwd.mkdir(parents=True, exist_ok=True)

            ghostty_one = {
                "tool": "claude",
                "sessionId": "ghostty-1",
                "cwd": str(cwd),
                "flags": [],
            }
            ghostty_two = {
                "tool": "codex",
                "sessionId": "ghostty-2",
                "cwd": str(cwd),
                "flags": [],
            }
            cmux_session = {
                "tool": "claude",
                "sessionId": "cmux-1",
                "cwd": str(cwd),
                "flags": [],
                "terminal": "cmux",
                "workspaceName": "dev",
                "workspaceId": "ws-1",
                "surfaceId": "sf-1",
                "surfaceIndex": 0,
            }

            (claude_dir / "ghostty-restore.json").write_text(
                json.dumps([ghostty_one, ghostty_two, cmux_session]), encoding="utf-8"
            )
            (claude_dir / "ghostty-live-state.json").write_text(
                json.dumps([ghostty_one, ghostty_two, cmux_session]), encoding="utf-8"
            )

            fake_bin = home / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            fake_osascript = fake_bin / "osascript"
            fake_osascript.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_osascript.chmod(0o755)

            proc = self.run_restore_auto(
                home, {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            remaining = json.loads(
                (claude_dir / "ghostty-restore.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(remaining), 2)
            self.assertEqual(
                sorted((s.get("terminal", "ghostty"), s.get("sessionId")) for s in remaining),
                [("cmux", "cmux-1"), ("ghostty", "ghostty-2")],
            )

    def test_auto_mode_preserves_unlaunched_sessions_when_osascript_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            home = Path(tempdir)
            restore_dir = home / ".claude"
            restore_dir.mkdir(parents=True, exist_ok=True)
            cwd1 = home / "project-one"
            cwd2 = home / "project-two"
            cwd1.mkdir(parents=True, exist_ok=True)
            cwd2.mkdir(parents=True, exist_ok=True)

            payload = [
                {
                    "tool": "claude",
                    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
                    "cwd": str(cwd1),
                    "flags": ["--model", "sonnet"],
                },
                {
                    "tool": "codex",
                    "sessionId": "019c5bce-a952-7380-b204-bfe40bf783b6",
                    "cwd": str(cwd2),
                    "flags": ["--model", "gpt-5"],
                },
            ]
            restore_path = restore_dir / "ghostty-restore.json"
            restore_path.write_text(json.dumps(payload), encoding="utf-8")

            fake_bin = home / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            fake_osascript = fake_bin / "osascript"
            fake_osascript.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_osascript.chmod(0o755)

            proc = self.run_restore_auto(
                home, {"PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            first = json.loads(proc.stdout)
            self.assertEqual(first["tool"], "claude")
            self.assertEqual(first["cwd"], str(cwd1))

            remaining = json.loads(restore_path.read_text(encoding="utf-8"))
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["tool"], "codex")
            self.assertEqual(remaining[0]["cwd"], str(cwd2))


if __name__ == "__main__":
    unittest.main()
