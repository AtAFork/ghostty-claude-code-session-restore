#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCHER_SCRIPT = REPO_ROOT / "watcher.sh"


def load_watcher_module():
    loader = SourceFileLoader("watcher_module", str(WATCHER_SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatcherBehaviorTests(unittest.TestCase):
    def test_should_save_on_shutdown_conditions(self) -> None:
        watcher = load_watcher_module()

        with mock.patch.object(watcher, "is_ghostty_running", return_value=True):
            self.assertTrue(watcher.should_save_on_shutdown())

        with mock.patch.object(watcher, "_is_cmux_socket_alive", return_value=False):
            with mock.patch.object(watcher, "is_ghostty_running", return_value=False):
                with mock.patch.object(watcher, "load_snapshot", return_value=[]):
                    self.assertFalse(watcher.should_save_on_shutdown())

        with mock.patch.object(watcher, "_is_cmux_socket_alive", return_value=False):
            with mock.patch.object(watcher, "is_ghostty_running", return_value=False):
                with mock.patch.object(watcher, "load_snapshot", return_value=[{"pid": 1}]):
                    self.assertTrue(watcher.should_save_on_shutdown())

    def test_save_sessions_prefers_live_state(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            live_payload = [
                {
                    "tool": "claude",
                    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
                    "cwd": "/tmp/proj",
                    "flags": ["--model", "sonnet"],
                }
            ]
            snapshot_payload = [
                {
                    "tool": "claude",
                    "cwd": "/tmp/other",
                    "args": "claude --continue",
                }
            ]
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(live_payload), encoding="utf-8"
            )
            watcher.SNAPSHOT_PATH.write_text(
                json.dumps(snapshot_payload), encoding="utf-8"
            )

            total, resumed, continued = watcher.save_sessions()
            self.assertEqual((total, resumed, continued), (1, 1, 0))

            restored = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(restored, live_payload)

    def test_save_sessions_filters_to_cmux_terminal(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            live_payload = [
                {
                    "tool": "claude",
                    "sessionId": "cmux-sid",
                    "cwd": "/tmp/cmux",
                    "flags": [],
                    "terminal": "cmux",
                },
                {
                    "tool": "claude",
                    "sessionId": "ghostty-sid",
                    "cwd": "/tmp/ghostty",
                    "flags": [],
                },
            ]
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(live_payload), encoding="utf-8"
            )

            total, resumed, continued = watcher.save_sessions(terminal="cmux")
            self.assertEqual((total, resumed, continued), (1, 1, 0))

            restored = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(len(restored), 1)
            self.assertEqual(restored[0]["terminal"], "cmux")

    def test_save_sessions_filters_to_ghostty_terminal(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            live_payload = [
                {
                    "tool": "claude",
                    "sessionId": "cmux-sid",
                    "cwd": "/tmp/cmux",
                    "flags": [],
                    "terminal": "cmux",
                },
                {
                    "tool": "claude",
                    "sessionId": "ghostty-sid",
                    "cwd": "/tmp/ghostty",
                    "flags": [],
                },
            ]
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(live_payload), encoding="utf-8"
            )

            total, resumed, continued = watcher.save_sessions(terminal="ghostty")
            self.assertEqual((total, resumed, continued), (1, 1, 0))

            restored = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(len(restored), 1)
            self.assertNotEqual(restored[0].get("terminal"), "cmux")

    def test_save_sessions_terminal_save_preserves_other_terminal_entries(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            watcher.RESTORE_PATH.write_text(
                json.dumps(
                    [
                        {
                            "tool": "claude",
                            "sessionId": "cmux-sid",
                            "cwd": "/tmp/cmux-existing",
                            "flags": [],
                            "terminal": "cmux",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(
                    [
                        {
                            "tool": "claude",
                            "sessionId": "ghostty-sid",
                            "cwd": "/tmp/ghostty-live",
                            "flags": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            total, resumed, continued = watcher.save_sessions(terminal="ghostty")
            self.assertEqual((total, resumed, continued), (1, 1, 0))

            restored = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(len(restored), 2)
            terminals = ["cmux" if r.get("terminal") == "cmux" else "ghostty" for r in restored]
            self.assertEqual(sorted(terminals), ["cmux", "ghostty"])

    def test_final_single_terminal_close_preserves_prior_terminal_save(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            # Initial mixed state while both terminals are active.
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(
                    [
                        {
                            "tool": "claude",
                            "sessionId": "cmux-sid",
                            "cwd": "/tmp/cmux-live",
                            "flags": [],
                            "terminal": "cmux",
                        },
                        {
                            "tool": "claude",
                            "sessionId": "ghostty-sid",
                            "cwd": "/tmp/ghostty-live",
                            "flags": [],
                        },
                    ]
                ),
                encoding="utf-8",
            )

            # Cmux closes first while ghostty remains active.
            total, resumed, continued = watcher.save_sessions(terminal="cmux")
            self.assertEqual((total, resumed, continued), (1, 1, 0))

            # Live state now reflects only ghostty, which is what the watcher
            # sees right before the final close event.
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(
                    [
                        {
                            "tool": "claude",
                            "sessionId": "ghostty-sid",
                            "cwd": "/tmp/ghostty-live",
                            "flags": [],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            save_terminal = watcher.terminal_scope_for_final_save({"ghostty"})
            self.assertEqual(save_terminal, "ghostty")
            total, resumed, continued = watcher.save_sessions(terminal=save_terminal)
            self.assertEqual((total, resumed, continued), (1, 1, 0))

            restored = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            terminals = ["cmux" if r.get("terminal") == "cmux" else "ghostty" for r in restored]
            self.assertEqual(sorted(terminals), ["cmux", "ghostty"])

    def test_terminal_scope_for_final_save(self) -> None:
        watcher = load_watcher_module()
        self.assertEqual(watcher.terminal_scope_for_final_save({"cmux"}), "cmux")
        self.assertEqual(watcher.terminal_scope_for_final_save({"ghostty"}), "ghostty")
        self.assertIsNone(watcher.terminal_scope_for_final_save({"cmux", "ghostty"}))
        self.assertIsNone(watcher.terminal_scope_for_final_save(set()))

    def test_main_loop_sequential_terminal_shutdown_keeps_both_sessions(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.SNAPSHOT_PATH = root / "ghostty-snapshot.json"
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.RESTORE_PATH = root / "ghostty-restore.json"
            watcher.LOG_PATH = root / "ghostty-session-watcher.log"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"
            watcher.RUNNING = True

            state_sequence = [
                {"cmux", "ghostty"},
                {"ghostty"},
                set(),
            ]
            sequence_iter = iter(state_sequence)

            all_entries = [
                {
                    "pid": 101,
                    "tty": "ttys001",
                    "cwd": "/tmp/cmux",
                    "args": "claude --resume cmux-sid",
                    "tool": "claude",
                    "sessionId": "cmux-sid",
                    "workspaceId": "ws-1",
                    "surfaceId": "sf-1",
                    "workspaceName": "Main",
                    "surfaceIndex": 0,
                },
                {
                    "pid": 102,
                    "tty": "ttys002",
                    "cwd": "/tmp/ghostty",
                    "args": "claude --resume ghostty-sid",
                    "tool": "claude",
                    "sessionId": "ghostty-sid",
                },
            ]
            ghostty_only_entries = [
                {
                    "pid": 102,
                    "tty": "ttys002",
                    "cwd": "/tmp/ghostty",
                    "args": "claude --resume ghostty-sid",
                    "tool": "claude",
                    "sessionId": "ghostty-sid",
                }
            ]

            def fake_detect_terminals():
                try:
                    return next(sequence_iter)
                except StopIteration:
                    return set()

            def fake_list_entries_for_terminals(terminals: set[str]):
                if terminals == {"cmux", "ghostty"}:
                    return all_entries
                if terminals == {"ghostty"}:
                    return ghostty_only_entries
                return []

            sleep_calls = {"count": 0}

            def fake_sleep(_seconds: float):
                sleep_calls["count"] += 1
                if sleep_calls["count"] >= 3:
                    watcher.RUNNING = False

            with mock.patch.object(watcher, "detect_terminals", side_effect=fake_detect_terminals):
                with mock.patch.object(
                    watcher,
                    "list_entries_for_terminals",
                    side_effect=fake_list_entries_for_terminals,
                ):
                    with mock.patch.object(watcher.time, "sleep", side_effect=fake_sleep):
                        with mock.patch.object(
                            watcher, "should_save_on_shutdown", return_value=False
                        ):
                            rc = watcher.main()

            self.assertEqual(rc, 0)
            restored = json.loads(watcher.RESTORE_PATH.read_text(encoding="utf-8"))
            terminals = ["cmux" if r.get("terminal") == "cmux" else "ghostty" for r in restored]
            self.assertEqual(sorted(terminals), ["cmux", "ghostty"])

    def test_persist_live_state_keeps_previous_non_empty_on_empty_snapshot(self) -> None:
        watcher = load_watcher_module()

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            watcher.LIVE_STATE_PATH = root / "ghostty-live-state.json"
            watcher.CLAUDE_PROJECTS_PATH = root / "projects"

            existing_payload = [
                {
                    "tool": "claude",
                    "sessionId": "904135b4-8584-42dd-aeb9-08b920d0e02e",
                    "cwd": "/tmp/proj",
                    "flags": ["--model", "sonnet"],
                }
            ]
            watcher.LIVE_STATE_PATH.write_text(
                json.dumps(existing_payload), encoding="utf-8"
            )

            saved = watcher.persist_live_state([])
            self.assertEqual(saved, 0)

            after = json.loads(watcher.LIVE_STATE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(after, existing_payload)

    def test_should_clear_live_state_for_empty_period(self) -> None:
        watcher = load_watcher_module()
        grace = watcher.EMPTY_LIVE_STATE_GRACE_SECONDS

        self.assertFalse(
            watcher.should_clear_live_state_for_empty_period(
                empty_started_at=None,
                now=100.0,
                already_cleared=False,
            )
        )
        self.assertFalse(
            watcher.should_clear_live_state_for_empty_period(
                empty_started_at=100.0,
                now=100.0 + grace - 0.1,
                already_cleared=False,
            )
        )
        self.assertTrue(
            watcher.should_clear_live_state_for_empty_period(
                empty_started_at=100.0,
                now=100.0 + grace,
                already_cleared=False,
            )
        )
        self.assertFalse(
            watcher.should_clear_live_state_for_empty_period(
                empty_started_at=100.0,
                now=100.0 + grace + 5.0,
                already_cleared=True,
            )
        )

    def test_snapshot_log_message_throttles_unresolved_codex_note(self) -> None:
        watcher = load_watcher_module()

        entries = [
            {"pid": 10, "tool": "claude", "sessionId": "a"},
            {"pid": 22, "tool": "codex", "sessionId": None},
            {"pid": 33, "tool": "codex", "sessionId": "b"},
        ]

        first_message, unresolved = watcher.snapshot_log_message(entries, tuple())
        self.assertIn("Snapshot: 3 session(s)", first_message)
        self.assertIn("1 codex unresolved", first_message)
        self.assertEqual(unresolved, (22,))

        second_message, unresolved_again = watcher.snapshot_log_message(entries, unresolved)
        self.assertEqual(second_message, "Snapshot: 3 session(s)")
        self.assertEqual(unresolved_again, (22,))


if __name__ == "__main__":
    unittest.main()
